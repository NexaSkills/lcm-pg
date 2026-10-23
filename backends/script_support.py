"""Backend-aware sqlite3 for standalone plugin scripts.

scripts/*.py load via importlib WITHOUT the package on sys.path, so the usual
relative swap (`from .backends import install`) fails and the script keeps
stdlib sqlite3 — under LCM_BACKEND=postgres that silently writes an EMPTY
SQLite file while stores wrote to PG (importers produced 0-byte targets and
'no such table' asserts). This helper makes the plugin package importable by
PATH (its own location, no sys.path/PYTHONPATH assumptions) and applies the
same sqlite3 proxy the package modules use.

Why not just `from .backends import install` in the script: the plugin
package is normally never imported via its name (hermes imports it by file
location), so `hermes_lcm` isn't a real package in sys.modules; a relative
import from a script loaded by spec raises ImportError and the script's
try/except silently keeps stdlib.

The proxy absorbs the sqlite3-isms that only standalone scripts hit (the
store modules were already PG-audited):

- `conn.row_factory = sqlite3.Row` is SILENTLY IGNORED on PG handles (the
  adapter always returns hybrid dict/positional rows; row["col"] and row[1]
  both keep working — Row objects don't exist server-side).
- `conn.backup(other)` on a PG handle routes to backup_compat.copy_snapshot
  (PG has no page-level backup API).
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
    """The sqlite3_compat module, resolved via the package (or by file path
    when this module itself was loaded standalone)."""
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


def _ensure_package() -> None:
    """Register 'hermes_lcm' as a minimal package rooted at the plugin dir
    WITHOUT executing __init__.py (it calls register(ctx) at import time).

    A plain __path__ registration is NOT enough: when the plugin was loaded
    by file location (the normal hermes path), the top-level module object in
    sys.modules was never produced by a real package import, so the import
    machinery refuses to find `hermes_lcm.<submodule>` through it (no
    __spec__/__loader__ path hook). Mirror tests/conftest.py: register every
    sibling module explicitly by file location.
    """
    pkg = sys.modules.get(_PKG)
    if pkg is None:
        pkg = _types.ModuleType(_PKG)
        pkg.__path__ = [str(_PLUGIN_DIR)]
        pkg.__package__ = _PKG
        sys.modules[_PKG] = pkg
    # sibling top-level modules (store_schema, store, engine, tools, ...)
    for py in sorted(_PLUGIN_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        name = f"{_PKG}.{py.stem}"
        if name in sys.modules:
            continue
        try:
            spec = importlib.util.spec_from_file_location(name, str(py))
            if spec is None or spec.loader is None:
                continue
            sm = importlib.util.module_from_spec(spec)
            sm.__package__ = _PKG
            sys.modules[name] = sm
            spec.loader.exec_module(sm)
        except Exception:
            sys.modules.pop(name, None)
    # the backends subpackage (if not already a package module)
    bname = f"{_PKG}.backends"
    if bname not in sys.modules:
        bmod = _types.ModuleType(bname)
        bmod.__path__ = [str(_DIR)]
        bmod.__package__ = bname
        sys.modules[bname] = bmod
    for py in sorted(_DIR.glob("*.py")):
        name = f"{bname}.{py.stem}"
        if name in sys.modules or py.name == "__init__.py":
            continue
        try:
            spec = importlib.util.spec_from_file_location(name, str(py))
            if spec is None or spec.loader is None:
                continue
            sm = importlib.util.module_from_spec(spec)
            sm.__package__ = bname
            sys.modules[name] = sm
            spec.loader.exec_module(sm)
        except Exception:
            sys.modules.pop(name, None)


def _pg_flavour_proxy(module, compat) -> None:
    """Extend the shared proxy for script-side sqlite3-isms: row_factory
    no-op on PG handles + conn.backup() routing. Mutating the connection
    CLASS (not the module) keeps the change one-time and global, matching how
    the rest of the PG layer patches its adapters."""
    try:
        probe = module.connect(":memory:")
        cls = type(probe)
        try:
            probe.close()
        except Exception:
            pass
        if not type(probe).__module__.startswith(_PKG):
            return
        if "row_factory" not in cls.__dict__:
            cls.row_factory = property(lambda self: None, lambda self, value: None)
        if "backup" not in cls.__dict__:
            import importlib as _il
            bc = _il.import_module(f"{_PKG}.backends.backup_compat")

            def _backup(self, target, *a, **k):
                return bc.copy_snapshot(self, target)
            cls.backup = _backup
    except Exception:
        pass


def install_script_sqlite_swap(script_globals: dict) -> bool:
    """Swap sqlite3 inside a standalone script to the backend-aware proxy.

    Returns True when the postgres proxy was applied (script code unchanged
    thereafter talks to PG). Never raises: a script must run normally when
    anything here fails.
    """
    try:
        import sqlite3 as _stdlib
        if script_globals.get("sqlite3") is not _stdlib:
            return False
        _ensure_package()
        compat = _compat_mod()
        if not compat.is_postgres():
            return False
        module = compat._CompatModule(_stdlib)
        _pg_flavour_proxy(module, compat)
        script_globals["sqlite3"] = module
        return True
    except Exception:
        return False
