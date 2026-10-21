#!/usr/bin/env python3
"""Automatic summary-embedding backfill runner (self-managed lcm-pg).

The live runtime embeds query vectors only; summary/corpus embeddings are
populated by the `/lcm embed backfill` machinery, which upstream leaves
manual. Non-technical users must never learn or run lcm commands, so this
runner invokes that SAME shipped backfill headlessly for every profile schema
on a schedule (systemd timer). Zero new embedding logic -- it just drives
`command._embedding_backfill_text` once per schema with the lease/identity
guardrails intact.

Mechanism (grounded in the fork):
  - command.py routes `import sqlite3` through backends.sqlite3_compat, so the
    backfill's `_embedding_read_connection` (and the VectorStore it opens) hit
    the Postgres backend when LCM_BACKEND=postgres.
  - The target schema = LCM_PG_SCHEMA if set, else schema_for_profile(HERMES_HOME
    basename) (backends/postgres.py). So we set HERMES_HOME per profile -- same
    rule ensure_schemas.py uses -- one deterministic loop, no hardcoded lists.
  - Default = apply (self-heal drift). --dry-run reports pending only (preview).

Usage:
  embed_auto_backfill.py [--limit N] [--dry-run]
Exit code 0 always under a timer (non-fatal); a failing schema is logged and
the loop continues.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import types
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # fork root == package dir (has __init__.py)

# Keep the runner's OWN connection footprint tiny: the private cluster caps at
# max_connections=50 and the live daemons already hold most of it (idle pool
# floors), so we use a single connection per schema (pool 1/1) and release it
# before the next schema. 5 schemas x 1 conn slots into the daemon headroom.
os.environ.setdefault("LCM_PG_POOL_MIN", "1")
os.environ.setdefault("LCM_PG_POOL_MAX", "1")

# Must be present for the backfill to hit Postgres at all (systemd timer
# passes the gateway's LCM_* env; these defaults make bare/manual runs work).
os.environ.setdefault("LCM_BACKEND", "postgres")
os.environ.setdefault("LCM_EMBEDDING_PROVIDER", "fastembed")
os.environ.setdefault("LCM_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
os.environ.setdefault("LCM_EMBEDDINGS_ENABLED", "true")
if not os.environ.get("LCM_FASTEMBED_CACHE_DIR") and Path(
        os.path.expanduser("~/lcm-pg/fastembed")).is_dir():
    os.environ["LCM_FASTEMBED_CACHE_DIR"] = os.path.expanduser("~/lcm-pg/fastembed")
if not os.environ.get("LCM_PG_DSN") and Path(
        os.path.expanduser("~/lcm-pg/run")).is_dir():
    os.environ["LCM_PG_DSN"] = (
        "postgresql:///lcm?host=" + os.path.expanduser("~/lcm-pg/run")
        + "&port=54329&user=lcm01")


def _deps_ok() -> bool:
    """True if this interpreter already satisfies the fork's import set."""
    try:
        import psycopg  # noqa: F401
        import psycopg_pool  # noqa: F401
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False


def _bootstrap_interpreter() -> None:
    """Re-exec under an interpreter that can import the fork's dependency set.

    The fork needs psycopg3 + psycopg_pool + fastembed together. Different
    installs place these in different profile venvs, so we don't hardcode one:
    probe candidate interpreter paths and re-exec into the first that imports
    all three. Idempotent (env guard) and a no-op when the current interpreter
    is already capable. Under systemd the daemon's own PYTHONPATH is used
    directly (no re-exec) when it already satisfies the check.
    """
    if _deps_ok() or os.environ.get("LCM_EAB_BOOTSTRAPPED"):
        return
    os.environ["LCM_EAB_BOOTSTRAPPED"] = "1"
    import glob
    import subprocess
    cands = [
        os.environ.get("LCM_VENV_PY"),
        os.path.expanduser("~/lcm-pg/venv/bin/python"),  # lcm-pg's OWN venv (first)
        *sorted(glob.glob(os.path.expanduser(
            "~/.hermes/installs/*/environments/*/venv/bin/python")), reverse=True),
    ]
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        r = subprocess.run(
            [c, "-c", "import psycopg, psycopg_pool, fastembed"],
            capture_output=True)
        if r.returncode == 0:
            os.execve(c, [c, os.path.abspath(__file__), *sys.argv[1:]],
                      dict(os.environ))
    # No capable interpreter found: continue and let the import fail loudly.
    print("embed_auto_backfill: warning - no interpreter with psycopg+"
          "psycopg_pool+fastembed found; proceeding", file=sys.stderr)


def _install_agent_shim():
    """Shim the trivial `agent.context_engine.ContextEngine` ABC (agent-free).

    lcm-pg's engine.py imports it (`from agent.context_engine import
    ContextEngine`) purely as a marker base class. Under the bundle's own
    interpreter that module isn't present (agent lives in the Hermes install,
    which lcm-pg must not use), so we register a stand-in with the SAME abstract
    surface. This lets the fork's engine/vector-store/config modules import
    cleanly from lcm-pg's own venv. It is lcm-pg's plugin contract, NOT a
    borrowed dependency: no logic, just the 4 abstract method signatures the
    base declares. If the real agent package is present (daemon path), we do
    nothing and defer to it.
    """
    import abc
    if "agent.context_engine" in sys.modules:
        return
    try:  # prefer the real thing if this interpreter can see it
        import agent.context_engine  # noqa: F401
        return
    except Exception:
        pass
    agent_mod = types.ModuleType("agent")
    ce_mod = types.ModuleType("agent.context_engine")

    class ContextEngine(abc.ABC):  # mirror agent.context_engine.ContextEngine
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


def _release_pools(pg_backend) -> None:
    """Close cached connection pools so the runner doesn't leak one per schema.

    The backend caches one pool per (dsn,schema) for the process lifetime; the
    per-schema loop would otherwise accumulate pools and hit the cluster's
    max_connections. Call each pool's close() (psycopg_pool uses .close, and
    also exposes closeall when present); the backend's on-pool-close hook
    removes the _POOLS entry, so we do not pop it ourselves.
    """
    pools = getattr(pg_backend, "_POOLS", None)
    if not pools:
        return
    # POP before close: closing in place leaves the cache entry pointing at a
    # dead pool, and connect() happily reuses it -> every later getconn() on
    # that (dsn,schema) waits forever (pool max=1). Pop-first is race-safe
    # under _POOLS_LOCK and matches embed_warmup_all's proven hygiene.
    live = []
    lock = getattr(pg_backend, "_POOLS_LOCK", None)
    import threading as _th
    if lock is None:
        lock = _th.Lock()
    with lock:
        for key in list(pools.keys()):
            live.append(pools.pop(key, None))
    for pool in [p for p in live if p is not None]:
        for closer in ("closeall", "close"):
            fn = getattr(pool, closer, None)
            if callable(fn):
                try:
                    fn(timeout=2.0) if closer == "closeall" else fn()
                    break
                except TypeError:
                    try:
                        fn()
                        break
                    except Exception:
                        pass
                except Exception:
                    pass


def _load_package():
    """Import the fork ROOT as package 'lcmpg', then its command/engine modules.

    command.py and engine.py use relative imports (`from .db_bootstrap import
    ...`), so they must load as submodules of a real package (same trick
    lcm_pg_warmup.py uses). Importing command applies the module-level sqlite3
    -> postgres backend swap; importing engine gives LCMEngine.
    """
    if "lcmpg" in sys.modules:
        return sys.modules["lcmpg"]
    _install_agent_shim()
    init = ROOT / "__init__.py"
    if not init.is_file():  # fallback: synthesize a namespace package from root
        pkg = types.ModuleType("lcmpg")
        pkg.__path__ = [str(ROOT)]
        sys.modules["lcmpg"] = pkg
    else:
        spec = importlib.util.spec_from_file_location(
            "lcmpg", str(init), submodule_search_locations=[str(ROOT)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["lcmpg"] = pkg
        spec.loader.exec_module(pkg)
    for sub in ("command", "engine"):
        spec = importlib.util.spec_from_file_location(
            f"lcmpg.{sub}", str(ROOT / f"{sub}.py"))
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "lcmpg"
        sys.modules[f"lcmpg.{sub}"] = mod
        spec.loader.exec_module(mod)
    return pkg


def _home_schema_pairs(schema_for_profile) -> list[tuple[str, str]]:
    """(HERMES_HOME, schema) for the root default + every profiles/* dir.

    Pin HERMES_HOME to the literal root (~/.hermes) rather than inherit it:
    a running session exports HERMES_HOME=<profile home>, which would collapse
    enumeration to one profile. Mapping: ~/.hermes -> lcm_default, and
    ~/.hermes/profiles/<p> -> lcm_<p> (backends/postgres:schema_for_profile).
    """
    base = Path(os.path.expanduser("~/.hermes"))
    homes = [base]
    profiles = base / "profiles"
    if profiles.is_dir():
        homes.extend(sorted(p for p in profiles.iterdir() if p.is_dir()))
    pairs = []
    seen = set()
    for h in homes:
        try:
            schema = schema_for_profile(str(h))
        except ValueError:
            continue
        if schema in seen:  # e.g. root + profiles/default both -> lcm_default
            continue
        seen.add(schema)
        pairs.append((str(h), schema))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=int(os.environ.get("LCM_EMBED_AUTO_LIMIT", "200")),
                    help="max summary nodes to embed per schema per run")
    ap.add_argument("--dry-run", action="store_true",
                    help="report pending only; no provider calls, no writes")
    a = ap.parse_args()

    _bootstrap_interpreter()
    apply = not a.dry_run
    tokens = (["--apply"] if apply else []) + ["--corpus", "summary", "--limit", str(a.limit)]

    _load_package()
    import lcmpg.command as lcm_command  # noqa: WPS433  (executes the sqlite3->PG swap)
    import lcmpg.engine as lcm_engine    # noqa: WPS433  (LCMEngine)
    import lcmpg.backends.postgres as pg_backend  # noqa: WPS433  (schema_for_profile)

    pairs = _home_schema_pairs(pg_backend.schema_for_profile)
    print(f"embed_auto_backfill: apply={'no' if a.dry_run else 'yes'} limit={a.limit} schemas={len(pairs)}")
    fail = 0
    for home, schema in pairs:
        os.environ["HERMES_HOME"] = home
        eng = None
        try:
            # release any prior pool FIRST so we never exceed headroom, then
            # construct (opens exactly 1 conn: pool 1/1).
            import lcmpg.backends.postgres as _pg
            _release_pools(_pg)
            eng = lcm_engine.LCMEngine(hermes_home=home)
            report = lcm_command._embedding_backfill_text(tokens, eng)
        except Exception as exc:  # noqa: BLE001 - a bad schema must not stop the loop
            fail += 1
            print(f"[{schema}] ERROR: {exc}")
            traceback.print_exc(limit=1)
            continue
        finally:
            # Tear the engine down so its per-schema pool returns to idle, then
            # close the cached pools entirely so we never accumulate one pool per
            # schema against the 50-connection cluster.
            try:
                if eng is not None:
                    eng.shutdown()
            except Exception:
                pass
            try:
                import lcmpg.backends.postgres as _pg
                _release_pools(_pg)
            except Exception:
                pass
        # compress the multi-line report to one log line
        summary = " ".join(
            ln.strip() for ln in report.splitlines()
            if any(k in ln.lower() for k in ("pending", "embedded", "in_flight", "uncertain", "profile", "error", "next", "remaining", "no missing", "already"))
        )[:240]
        print(f"[{schema}] {summary or report[:160]}")
    return 0  # never non-fatal under a timer; failures are logged, not raised


if __name__ == "__main__":
    raise SystemExit(main())
