"""Explicit backend install hook — no monkeypatching.

Call install() once at plugin load, BEFORE any store module opens a
connection. When LCM_BACKEND=postgres it binds backends.factory.connect as
the package-level `connect`, which every store module imports:

    from .backends.factory import connect

On the default sqlite backend, factory.connect falls through to plain
sqlite3.connect with identical semantics (uri handling included), so the
import switch is behaviour-neutral until LCM_BACKEND=postgres is set.

Why explicit instead of the earlier sqlite3.connect monkeypatch: stores
open connections in many threads at once; an import-time env check baked
into each store module keeps the decision in ONE place and leaves stdlib
sqlite3 untouched for every other consumer in the process.
"""

from __future__ import annotations

import os

from .factory import connect, backend_for as factory_backend

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    backend = factory_backend()  # validates and defaults to postgres
    _INSTALLED = True


# ---------------------------------------------------------------------------
# stdlib sqlite3.connect placeholder-path parity (PG backend)
#
# Under the PG backend, stores live SERVER-SIDE in the profile schema and
# the on-disk file at the store path is a 0-byte placeholder. Raw stdlib
# sqlite3.connect(store_path) (test verification handles, misc tooling)
# would see an empty database. The hook below routes ONLY such paths —
# placeholder-shaped file AND a `messages` table present in the ambient
# PG schema — to backends.factory.connect. Everything else is stdlib.
# ---------------------------------------------------------------------------

_LCM_CONNECT_HOOK_OFF = False
_LCM_PLACEHOLDER_CACHE: dict = {}


def _current_schema_name():
    import os
    try:
        from . import postgres as _pg
        return _pg.schema_for_profile(os.environ.get("HERMES_HOME"),
                                       os.environ.get("LCM_PG_SCHEMA"))
    except Exception:
        return None


def _schema_has_messages(schema: str | None) -> bool:
    """Live catalog probe (own connection; never re-enters the hook)."""
    if not schema:
        return False
    try:
        from . import factory as _f
        dsn = _f._pg_dsn()
    except Exception:
        return False
    import psycopg
    try:
        with psycopg.connect(dsn, connect_timeout=2) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = 'messages' "
                    "LIMIT 1", (schema,))
                return cur.fetchone() is not None
    except Exception:
        return False


def _placeholder_store_path(database: str) -> bool:
    import os
    try:
        p = str(database)
        if os.path.isfile(p) and os.path.getsize(p) > 512:
            with open(p, "rb") as fh:
                if fh.read(16) == b"SQLite format 3\x00":
                    return False  # genuine sqlite database
        key = _current_schema_name()
        cached = _LCM_PLACEHOLDER_CACHE.get(key)
        if cached is None:
            cached = _schema_has_messages(key)
            _LCM_PLACEHOLDER_CACHE[key] = cached
        return bool(cached)
    except Exception:
        return False


def install_stdlib_connect_hook() -> None:
    """Wrap the REAL sqlite3.connect once (idempotent)."""
    import sqlite3 as _stdlib_sqlite3
    if getattr(_stdlib_sqlite3.connect, "_lcm_pg_hook", False):
        return
    real_connect = _stdlib_sqlite3.connect

    def connect(database="", *args, **kwargs):
        global _LCM_CONNECT_HOOK_OFF
        if _LCM_CONNECT_HOOK_OFF:
            return real_connect(database, *args, **kwargs)
        try:
            if (isinstance(database, (str, os.PathLike)) and database
                    and not kwargs.get("uri")
                    and str(database) not in (":memory:",)
                    and not str(database).startswith("file:")
                    and _placeholder_store_path(str(database))):
                from . import factory
                _LCM_CONNECT_HOOK_OFF = True
                try:
                    return factory.connect(database, *args, **kwargs)
                finally:
                    _LCM_CONNECT_HOOK_OFF = False
        except Exception:
            pass
        return real_connect(database, *args, **kwargs)

    connect._lcm_pg_hook = True
    try:
        connect.__doc__ = real_connect.__doc__
    except Exception:
        pass
    _stdlib_sqlite3.connect = connect


import os as _os  # used by the connect hook above

_install_orig = install


def install(*args, **kwargs):
    out = _install_orig(*args, **kwargs)
    try:
        if _os.environ.get("LCM_BACKEND", "").strip().lower() == "postgres":
            install_stdlib_connect_hook()
    except Exception:
        pass
    return out


import os as _os2
try:
    if _os2.environ.get('LCM_BACKEND', '').strip().lower() == 'postgres':
        install_stdlib_connect_hook()
except Exception:
    pass
