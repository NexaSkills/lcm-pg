"""Drop-in `sqlite3` replacement for the Postgres backend.

Store modules do `import sqlite3` and use two kinds of things:

1. `sqlite3.connect(path, ...)`  — routed through backends.factory.connect
   (plain sqlite3.connect on the default backend; PG-shaped connection when
   LCM_BACKEND=postgres). `:memory:` always stays real SQLite, which is what
   upstream's scratch/DDL-contract probes expect.
2. `sqlite3.Row` / `sqlite3.Error` / `sqlite3.OperationalError` / ...
   — plain passthroughs to stdlib sqlite3. On the PG path rows are dicts
   (dict_row), so sqlite3.Row isinstance checks are already false there and
   callers use their dict branches; nothing needs faking.

Swap-in (per-module, import-time, race-free — no global monkeypatch):

    import sqlite3
    try:
        from .backends import install
        sqlite3 = install.sqlite3_if_postgres(sqlite3)
    except Exception:
        pass  # standalone/script import: keep stdlib

Standalone scripts (importlib-loaded, outside the package) cannot use the
relative-import form above; they call backends.script_support.
install_script_sqlite_swap(globals()) instead — it imports the plugin package
by PATH (from __file__), so it works with zero sys.path/PYTHONPATH assumptions.
"""

from __future__ import annotations

import os
import sqlite3 as _stdlib

from . import factory

_POSTGRES = ("postgres", "postgresql", "pg")


def is_postgres() -> bool:
    # Default is postgres (see factory.backend_for): an unset LCM_BACKEND must
    # not silently select SQLite. SQLite requires an explicit declaration.
    return factory.backend_for() == "postgres"


class _CompatModule:
    """Module-shaped wrapper: connect() routed, everything else delegated."""

    def __init__(self, real):
        self._real = real

    def connect(self, db_path, *args, **kwargs):
        if is_postgres() and str(db_path) != ":memory:":
            return factory.connect(db_path, *args, **kwargs)
        return self._real.connect(db_path, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


_compat_cache: dict = {}


def sqlite3_if_postgres(real_module):
    """Return a backend-aware module proxy when postgres is selected, else the input."""
    if not is_postgres():
        return real_module
    key = id(real_module)
    proxy = _compat_cache.get(key)
    if proxy is None:
        proxy = _CompatModule(real_module)
        _compat_cache[key] = proxy
    return proxy
