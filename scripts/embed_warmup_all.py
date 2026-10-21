#!/usr/bin/env python3
"""Register the embedding profile in EVERY profile schema (lcm-pg self-contained).

The shipped `/lcm embed warmup` command registers the model profile in ONE
schema (the current engine's). A fresh profile schema has the table but zero
rows, and the shipped backfill then refuses with "no current embedding
profile is registered; run `/lcm embed warmup` first" until that profile's
daemon happens to warm up. Non-technical users never run lcm commands, so
the auto-backfill pipeline registers here instead — driving the SAME
VectorStore.register_profile the shipped warmup calls. Zero new embedding
logic: probe once (fastembed load + 1 embedding, cached model), register
the identical (model, provider, dim) into every schema.

Runs before embed_auto_backfill.py in the timer chain. Idempotent
(register_profile is an upsert). Exit 0 under a timer; a failing schema is
logged and skipped.

Usage: embed_warmup_all.py [--dry-run]
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
ROOT = HERE.parent  # fork root == package dir

os.environ.setdefault("LCM_BACKEND", "postgres")
os.environ.setdefault("LCM_EMBEDDING_PROVIDER", "fastembed")
os.environ.setdefault("LCM_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
os.environ.setdefault("LCM_EMBEDDINGS_ENABLED", "true")
os.environ.setdefault("LCM_PG_POOL_MIN", "1")
os.environ.setdefault("LCM_PG_POOL_MAX", "1")
if not os.environ.get("LCM_FASTEMBED_CACHE_DIR") and Path(
        os.path.expanduser("~/lcm-pg/fastembed")).is_dir():
    os.environ["LCM_FASTEMBED_CACHE_DIR"] = os.path.expanduser("~/lcm-pg/fastembed")
if not os.environ.get("LCM_PG_DSN") and Path(
        os.path.expanduser("~/lcm-pg/run")).is_dir():
    os.environ["LCM_PG_DSN"] = ("postgresql:///lcm?host="
        + os.path.expanduser("~/lcm-pg/run") + "&port=54329&user=lcm01")


def _bootstrap_interpreter() -> None:
    try:
        import psycopg, psycopg_pool, fastembed  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("LCM_EWA_BOOTSTRAPPED"):
        return
    os.environ["LCM_EWA_BOOTSTRAPPED"] = "1"
    import glob, subprocess
    cands = [os.environ.get("LCM_VENV_PY"),
             os.path.expanduser("~/lcm-pg/venv/bin/python"),
             *sorted(glob.glob(os.path.expanduser(
                 "~/.hermes/installs/*/environments/*/venv/bin/python")), reverse=True)]
    for c in cands:
        if c and os.path.isfile(c) and subprocess.run(
                [c, "-c", "import psycopg, psycopg_pool, fastembed"],
                capture_output=True).returncode == 0:
            os.execve(c, [c, os.path.abspath(__file__), *sys.argv[1:]], dict(os.environ))


def _load_package():
    if "lcmpg" in sys.modules:
        return sys.modules["lcmpg"]
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
    init = ROOT / "__init__.py"
    if not init.is_file():
        pkg = types.ModuleType("lcmpg")
        pkg.__path__ = [str(ROOT)]
        sys.modules["lcmpg"] = pkg
        return pkg
    spec = importlib.util.spec_from_file_location("lcmpg", str(init),
                                                 submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["lcmpg"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def _schemas(pg_backend) -> list[tuple[str, str]]:
    base = Path(os.path.expanduser("~/.hermes"))
    homes = [base]
    prows = base / "profiles"
    if prows.is_dir():
        homes.extend(sorted(p for p in prows.iterdir() if p.is_dir()))
    out, seen = [], set()
    for h in homes:
        try:
            s = pg_backend.schema_for_profile(str(h))
        except ValueError:
            continue
        if s not in seen:
            seen.add(s)
            out.append((str(h), s))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    _bootstrap_interpreter()
    pkg = _load_package()
    import lcmpg.config as lcm_config      # noqa: F401
    import lcmpg.embedding_provider as ep  # noqa: F401 (resolve_provider)
    import lcmpg.vector_store as vs        # noqa: F401
    import lcmpg.backends.postgres as pg   # noqa: F401

    # config from env, exactly like the daemon builds it
    cfg = lcm_config.LCMConfig.from_env()
    provider = ep.resolve_provider(cfg)
    if provider is None:
        print("embed_warmup_all: no provider configured; nothing to register")
        return 0
    model = getattr(cfg, "embedding_model", "") or provider.model_id
    provider_id = getattr(cfg, "embedding_provider", "") or provider.provider_id

    # ONE probe for dim (model load uses the shared fastembed cache dir)
    vec = provider.embed_query("warmup")
    dim = len(vec)
    if dim < 1:
        print("embed_warmup_all: provider returned empty probe; aborting")
        return 1

    schemas = _schemas(pg)
    print(f"embed_warmup_all: model={model} provider={provider_id} dim={dim} schemas={len(schemas)}")
    # Pre-ensure schema + embedding tables with plain psycopg (connect/commit/
    # close per schema — zero pool residency). pg.connect() itself opens a
    # pool, and with VectorStore opening its OWN handle per schema a pooled
    # pass can wedge pool-waits under daemon pool floors (observed 2026-10-21
    # 'couldn't get a connection after 10.00 sec'). The shipped daemons
    # ensure these anyway; this path must not *depend* on that timing.
    import lcmpg.db_bootstrap as dbb
    for home, schema in schemas:
        try:
            # ensure via db_bootstrap through a PgConnection (executescript
            # available), then DROP the pool from the module cache (closing a
            # cached pool would poison its entry for the next connect()).
            conn = pg.connect(os.environ["LCM_PG_DSN"], schema)
            dbb.ensure_metadata_table(conn)
            dbb.ensure_embedding_tables(conn)
        except Exception as exc:
            print(f"  [{schema}] ensure-tables note: {str(exc)[:120]}")
        finally:
            for key in list(getattr(pg, "_POOLS", {})):
                if key[1] == schema:
                    try: pg._POOLS.pop(key).close()
                    except Exception: pass
    for home, schema in schemas:
        os.environ["HERMES_HOME"] = home
        if a.dry_run:
            print(f"  [{schema}] would register profile")
            continue
        try:
            os.environ["LCM_PG_SCHEMA"] = schema  # explicit pin; no engine needed
            store = vs.VectorStore(Path(home) / "lcm.db", config=cfg)
            try:
                store.register_profile(model, provider_id, dim)
                try:  # chunk task shares the probe when same model (parity with warmup)
                    store.register_profile(model, provider_id, dim, task="chunk")
                except Exception:
                    pass
                print(f"  [{schema}] registered")
            finally:
                try: store.close()
                except Exception: pass
            for pool in list(getattr(pg, "_POOLS", {}).values()):
                try: pool.close()
                except Exception: pass
        except Exception as exc:
            print(f"  [{schema}] ERROR: {str(exc)[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
