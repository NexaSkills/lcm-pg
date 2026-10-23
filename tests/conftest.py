"""Test configuration for hermes-lcm plugin tests.

Patches the plugin modules so they can be imported both as a package
(relative imports during plugin loading) and directly during testing.
"""
import sys
import importlib
from pathlib import Path

# Make the repo root importable (for agent.context_engine etc.)
repo_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

# The fork dir's parent has no `agent` package; this checkout's live Hermes
# install does (upstream checkouts live under an install root that carries
# it). Add it so `from agent.context_engine import ContextEngine` resolves
# exactly as it does on a live plugin load.
_hermes_agent = Path.home() / ".hermes" / "hermes-agent"
if _hermes_agent.is_dir() and str(_hermes_agent) not in sys.path:
    sys.path.append(str(_hermes_agent))

# Register the plugin directory as a proper package
plugin_dir = Path(__file__).resolve().parent.parent
pkg_name = "hermes_lcm"

if pkg_name not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        str(plugin_dir / "__init__.py"),
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(plugin_dir)]
    mod.__package__ = pkg_name
    sys.modules[pkg_name] = mod
    # Don't exec the module (it tries to register with ctx)
    # Just make submodules importable

    # Register each submodule
    for py_file in plugin_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        sub_name = f"{pkg_name}.{py_file.stem}"
        if sub_name not in sys.modules:
            sub_spec = importlib.util.spec_from_file_location(
                sub_name, str(py_file),
                submodule_search_locations=[],
            )
            sub_mod = importlib.util.module_from_spec(sub_spec)
            sub_mod.__package__ = pkg_name
            sys.modules[sub_name] = sub_mod
            setattr(mod, py_file.stem, sub_mod)
            try:
                sub_spec.loader.exec_module(sub_mod)
            except Exception:
                pass  # some modules may fail (e.g. engine needs agent)


# --- PG test isolation -------------------------------------------------
# In postgres mode every store handle lands in the profile's schema
# (factory.connect -> schema_for_profile), so tests that derive session ids
# from tmp_path (e.g. test_active_tool_stubbing's make_engine) leave rows
# behind that contaminate the NEXT run: session ids repeat across runs while
# the PG schema persists. Give every test its own schema via LCM_PG_SCHEMA,
# keyed by tmp_path so all handles of one test share it. sqlite path is
# untouched (LCM_PG_SCHEMA stays unset).
import os as _os
import hashlib as _hashlib

# The plugin's default backend is postgres (factory.backend_for). Mirror that
# default here so test processes get PG isolation schemas even when the runner
# forgot to export LCM_BACKEND — without this, a DSN present in ~/.hermes/.env
# silently gave stores PG writes under an UNisolated schema (cross-run
# contamination: idempotency tables persist, later imports fail or double-import).
if _os.environ.setdefault("LCM_BACKEND", "postgres").strip().lower() == "postgres":
    import pytest as _pytest

    @_pytest.fixture(autouse=True)
    def _lcm_pg_test_isolation(tmp_path, monkeypatch):
        tag = _hashlib.sha1(str(tmp_path).encode()).hexdigest()[:10]
        # under pytest-xdist each worker's tmp_path base differs, but make the
        # schema per-process-unique anyway (pid) so parallel workers can never
        # collide even with custom basetemp setups.
        name = f"lcm_t_{tag}_{_os.getpid() % 100000}"
        monkeypatch.setenv("LCM_PG_SCHEMA", name)
        yield
        # drop the test schema (idempotent GC; connection is lazy so this is
        # cheap and never creates a pool). Bounds schema sprawl.
        try:
            import importlib as _il
            _pg = _il.import_module("hermes_lcm.backends.postgres")
            _factory = _il.import_module("hermes_lcm.backends.factory")
            dsn = _factory._pg_dsn()
            # Close every pool bound to this test's schema FIRST: without this
            # each test's min_size=1 backend lingers for the whole session
            # (~90 tests -> saturates max_connections=100).
            for k in [k for k in list(_pg._POOLS) if k[1] == name]:
                try:
                    _pg._POOLS.pop(k).close()
                except Exception:
                    _pg._POOLS.pop(k, None)
            import psycopg as _psycopg
            with _psycopg.connect(dsn) as _c:
                _c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        except Exception:
            pass
