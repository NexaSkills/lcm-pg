"""Backend-aware sqlite3 for standalone plugin scripts.

scripts/*.py load via importlib WITHOUT the package on sys.path, so the usual
relative swap (`from .backends import install`) fails and the script keeps
stdlib sqlite3. Under the postgres backend that is wrong in BOTH directions:

- target DBs: writes go to a real SQLite file while the stores live in PG
  (importers produced empty targets / 'no such table' asserts);
- source DBs: a *plain* SQLite file passed by a test is a legitimate sqlite
  database, but a blind PG swap makes the importer read an EMPTY PG schema
  instead (every probe reports 'missing columns').

So `install_script_sqlite_swap` gives the script a proxy whose connect()
DECIDES PER PATH (backends.factory.pg_store_path()):

- a path that IS an existing on-disk SQLite database (magic header, or a
  :memory:/file: URI) connects with stdlib sqlite3;
- anything else (an empty/lcm.db-style path under HERMES_HOME, the shape
  stores use) routes through backends.factory — PG under the ambient schema,
  exactly like the plugin's own stores.

Script code stays untouched. The proxy also absorbs the sqlite3-isms that
only standalone scripts hit (store modules were already PG-audited), bound
once when a connection actually is a PG handle:

- `conn.row_factory = sqlite3.Row` silently ignored (PG handles return
  hybrid dict/positional rows; row["col"] and row[1] both work);
- `conn.backup(other)` routes to backends.backup_compat.copy_snapshot when
  either side is a PG handle (PG has no page-level backup API).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types as _types
from pathlib import Path

_PKG = "hermes_lcm"
_DIR = Path(__file__).resolve().parent           # .../backends
_PLUGIN_DIR = _DIR.parent                        # plugin root

_compat = None


def _compat_mod():
    global _compat
    if _compat is not None:
        return _compat
    name = f"{_PKG}.backends.sqlite3_compat"
    if name in sys.modules:
        _compat = sys.modules[name]
        return _compat
    try:
        _compat = importlib.import_module(name)
        return _compat
    except Exception:
        path = _DIR / "sqlite3_compat.py"
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        _compat = mod
        return _compat


def _register_submodules(prefix: str, directory: Path) -> None:
    """Register modules under `prefix` by file location. Needed because the
    plugin is normally imported by FILE, so the package in sys.modules is a
    synthetic ModuleType without __spec__ and the import machinery cannot
    find submodules through it ('attempted relative import with no known
    parent package'). Same shim shape tests/conftest.py uses."""
    for py in sorted(directory.glob("*.py")):
        if py.name == "__init__.py":
            continue
        name = f"{prefix}.{py.stem}"
        if name in sys.modules:
            continue
        try:
            spec = importlib.util.spec_from_file_location(name, str(py))
            if spec is None or spec.loader is None:
                continue
            sm = importlib.util.module_from_spec(spec)
            sm.__package__ = prefix
            sys.modules[name] = sm
            spec.loader.exec_module(sm)
        except Exception:
            sys.modules.pop(name, None)


def _ensure_package() -> None:
    """Make `import hermes_lcm.<module>` work WITHOUT executing __init__.py
    (it calls register(ctx) at import time) and WITHOUT editing sys.path."""
    if _PKG not in sys.modules:
        pkg = _types.ModuleType(_PKG)
        pkg.__path__ = [str(_PLUGIN_DIR)]
        pkg.__package__ = _PKG
        sys.modules[_PKG] = pkg
        _register_submodules(_PKG, _PLUGIN_DIR)
    bname = f"{_PKG}.backends"
    if bname not in sys.modules:
        bmod = _types.ModuleType(bname)
        bmod.__path__ = [str(_DIR)]
        bmod.__package__ = bname
        sys.modules[bname] = bmod
    _register_submodules(bname, _DIR)


def _is_pg_handle(conn) -> bool:
    return type(conn).__module__.startswith(_PKG)


def _ensure_handle_flavours(conn) -> None:
    """PG-only sqlite3-isms, bound once on the adapter class."""
    if not _is_pg_handle(conn):
        return
    cls = type(conn)
    if "row_factory" not in cls.__dict__:
        try:
            cls.row_factory = property(lambda self: None, lambda self, value: None)
        except Exception:
            pass
    if "backup" not in cls.__dict__:
        try:
            bc = importlib.import_module(f"{_PKG}.backends.backup_compat")
            cls.backup = lambda self, target, *a, **k: bc.copy_snapshot(self, target)
        except Exception:
            pass


def _sqlite_file_shape(s: str) -> bool:
    """True when `s` cannot be a store path: :memory:, file: URIs, or an
    existing on-disk file with the SQLite magic header. The importer's
    fixture/source DBs are exactly this shape; store DBs are lcm*.db paths
    whose content lives in PG (the file is a 0-byte placeholder)."""
    if s == ":memory:" or s.startswith("file:"):
        return True
    try:
        p = Path(s.split("?")[0])
        if not p.is_file():
            return False
        with open(p, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


class _ScriptModule:
    """Module-shaped sqlite3 for standalone scripts (see module docstring)."""

    def __init__(self, stdlib, compat):
        self._stdlib = stdlib
        self._compat = compat
        self._pg = compat.sqlite3_if_postgres(stdlib) if compat.is_postgres() else None

    def connect(self, db_path, *args, **kwargs):
        if self._pg is None or _sqlite_file_shape(str(db_path)):
            return self._stdlib.connect(db_path, *args, **kwargs)
        conn = self._pg.connect(db_path, *args, **kwargs)
        _ensure_handle_flavours(conn)
        return conn

    def backup(self, source_conn, dest_conn):
        if _is_pg_handle(source_conn) or _is_pg_handle(dest_conn):
            bc = importlib.import_module(f"{_PKG}.backends.backup_compat")
            return bc.copy_snapshot(source_conn, dest_conn,
                                    native_backup=lambda: source_conn.backup(dest_conn))
        return source_conn.backup(dest_conn)

    def __getattr__(self, name):
        return getattr(self._stdlib, name)


def install_script_sqlite_swap(script_globals: dict) -> bool:
    """Install the backend-aware sqlite3 proxy into a standalone script.

    Returns True when a postgres-aware proxy was applied. Never raises: a
    script must run unchanged when anything here fails.
    """
    try:
        import sqlite3 as _stdlib
        if script_globals.get("sqlite3") is not _stdlib:
            return False
        _ensure_package()
        compat = _compat_mod()
        if not compat.is_postgres():
            return False
        script_globals["sqlite3"] = _ScriptModule(_stdlib, compat)
        return True
    except Exception:
        return False
