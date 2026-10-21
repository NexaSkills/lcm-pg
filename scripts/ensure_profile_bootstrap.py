#!/usr/bin/env python3
"""Idempotent per-profile bootstrap for lcm-pg self-contained clusters.

Ensures EVERY profile schema on the host (lcm_default for ~/.hermes plus
lcm_<profile> for each ~/.hermes/profiles/*) has the FULL canonical table
set via db_bootstrap's own ensure_* functions — the identical bootstrap the
live engine runs on first connect.

Why this exists: schemas created by a bare CREATE SCHEMA (installer
enumeration) or a partial import hold only a subset of tables. The
embedding machinery then aborts on the first missing one (observed fleet-
wide 2026-10-21: `relation "lcm_embedding_profile" does not exist` — the
backfill's identity probe — because ensure_embedding_tables had never run in
that schema). A daemon WOULD self-heal on first write, but idle profiles
never get one, leaving backfill erroring until then.

Idempotent (all ensure_* are CREATE IF NOT EXISTS + idempotent ALTERs) and
connection-lean (one 1/1 pool per schema, released before the next, same
discipline as embed_auto_backfill.py).

Usage: ensure_profile_bootstrap.py [--dsn DSN]
Exit 0 always (schema problems are printed and skipped) — safe under a timer.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORK = HERE.parent  # fork root == package dir

os.environ.setdefault("LCM_BACKEND", "postgres")


def _bootstrap_interpreter() -> None:
    """Same contract as embed_auto_backfill: re-exec into an interpreter with
    psycopg+psycopg_pool (fastembed not needed here)."""
    try:
        import psycopg, psycopg_pool  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("LCM_EPB_BOOTSTRAPPED"):
        return
    os.environ["LCM_EPB_BOOTSTRAPPED"] = "1"
    import glob, subprocess
    cands = [os.environ.get("LCM_VENV_PY"),
             os.path.expanduser("~/lcm-pg/venv/bin/python"),
             *sorted(glob.glob(os.path.expanduser(
                 "~/.hermes/installs/*/environments/*/venv/bin/python")), reverse=True)]
    for c in cands:
        if c and os.path.isfile(c) and subprocess.run(
                [c, "-c", "import psycopg, psycopg_pool"], capture_output=True).returncode == 0:
            os.execve(c, [c, os.path.abspath(__file__), *sys.argv[1:]], dict(os.environ))


def _load_package():
    """Load the fork root as package 'lcmpg' (command.py-style relative imports)."""
    if "lcmpg" in sys.modules:
        return sys.modules["lcmpg"]
    # agent shim (db_bootstrap/engine import paths may touch it)
    if "agent.context_engine" not in sys.modules:
        try:
            import agent.context_engine  # noqa: F401
        except Exception:
            import abc
            agent_mod = types.ModuleType("agent")
            ce_mod = types.ModuleType("agent.context_engine")

            class ContextEngine(abc.ABC):
                @property
                @abc.abstractmethod
                def name(self): ...
                @abc.abstractmethod
                def should_compress(self, *a, **k): ...
                @abc.abstractmethod
                def compress(self, *a, **k): ...
                @abc.abstractmethod
                def update_from_response(self, *a, **k): ...
            ce_mod.ContextEngine = ContextEngine
            agent_mod.context_engine = ce_mod
            sys.modules.setdefault("agent", agent_mod)
            sys.modules["agent.context_engine"] = ce_mod
    init = FORK / "__init__.py"
    if not init.is_file():
        pkg = types.ModuleType("lcmpg")
        pkg.__path__ = [str(FORK)]
        sys.modules["lcmpg"] = pkg
        return pkg
    spec = importlib.util.spec_from_file_location("lcmpg", str(init),
                                                  submodule_search_locations=[str(FORK)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["lcmpg"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def _profiles() -> list[str]:
    base = Path(os.environ.get("HERMES_ROOT", Path.home() / ".hermes"))
    homes = [str(base)]
    prows = base / "profiles"
    if prows.is_dir():
        homes.extend(sorted(str(p) for p in prows.iterdir() if p.is_dir()))
    return homes


ENSURE_FNS = (
    "ensure_metadata_table", "ensure_migration_state_table",
    "ensure_lifecycle_state_table", "ensure_lifecycle_state_columns",
    "ensure_message_origin_columns", "ensure_temporal_rollup_tables",
    "ensure_embedding_tables", "ensure_chunk_tables", "ensure_assertion_tables",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("LCM_PG_DSN", ""))
    a = ap.parse_args()
    if not a.dsn:
        b = Path.home() / "lcm-pg"
        a.dsn = f"postgresql:///lcm?host={b}/run&port=54329&user=lcm01" if (b / "run").is_dir() else ""
    if not a.dsn:
        print("no LCM_PG_DSN and no bundle cluster", file=sys.stderr)
        return 1

    _bootstrap_interpreter()
    pkg = _load_package()
    dbb = importlib.import_module("lcmpg.db_bootstrap")
    pg = importlib.import_module("lcmpg.backends.postgres")

    os.environ.pop("LCM_PG_SCHEMA", None)  # let schema_for_profile derive per home
    seen: set[str] = set()
    total = 0
    for home in _profiles():
        schema = pg.schema_for_profile(home)
        if schema in seen:
            continue
        seen.add(schema)
        try:
            conn = pg.connect(a.dsn, schema)
        except Exception as exc:
            print(f"[{schema}] SKIP (connect): {exc}")
            continue
        for fn in ENSURE_FNS:
            f = getattr(dbb, fn, None)
            if f is None:
                continue
            try:
                f(conn)
            except Exception as exc:
                print(f"[{schema}] {fn}: {str(exc)[:120]}")
        total += 1
        # release this schema's pool before the next (never accumulate)
        for pool in list(getattr(pg, "_POOLS", {}).values()):
            try:
                pool.close()
            except Exception:
                pass
    print(f"bootstrap ensured for {total} schemas: {' '.join(sorted(seen))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
