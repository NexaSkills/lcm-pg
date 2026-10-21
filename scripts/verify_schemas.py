#!/usr/bin/env python3
"""Verify embedding + rollup schema shapes across all lcm_* schemas here.

Usage: verify_schemas.py [schema ...]   (default: auto-enumerate via psql)
Prints one line per schema: OK or the verifier complaints. Exit 0 clean.
"""
import importlib.util, os, subprocess, sys
from pathlib import Path

D = str(Path.home() / ".hermes" / "forks" / "lcm-pg")
os.environ.setdefault("LCM_BACKEND", "postgres")
os.environ.setdefault("LCM_PG_DSN",
    "postgresql:///lcm?host=" + str(Path.home()) + "/lcm-pg/run&port=54329&user=lcm01")
_spec = importlib.util.spec_from_file_location(
    "lcmpg", str(Path(D) / "__init__.py"), submodule_search_locations=[D])
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lcmpg"] = _pkg
_spec.loader.exec_module(_pkg)
import importlib
pg = importlib.import_module("lcmpg.backends.postgres")
dbb = importlib.import_module("lcmpg.db_bootstrap")

schemas = sys.argv[1:]
if not schemas:
    out = subprocess.run(
        ["psql", "-h", str(Path.home() / "lcm-pg/run"), "-p", "54329",
         "-U", "lcm01", "-d", "lcm", "-Atc",
         "select nspname from pg_namespace"
         " where nspname like 'lcm\\_%' escape E'\\\\'"
         " and nspname not like '%deleted'"
         " and nspname not like '%corrupt%' order by 1"],
        capture_output=True, text=True)
    schemas = out.stdout.split()
bad = 0
for s in schemas:
    try:
        c = pg.connect(os.environ["LCM_PG_DSN"], s)
        e = dbb.verify_embedding_schema(c)
        r = dbb.verify_temporal_rollup_schema(c)
        if e or r:
            bad += 1
            print(f"{s}: embed={e or 'OK'} rollup={r or 'OK'}")
        else:
            print(f"{s}: OK")
    except Exception as exc:
        bad += 1
        print(f"{s}: ERROR {str(exc)[:80]}")
print(f"TOTAL {len(schemas)} schemas, {bad} dirty")
sys.exit(1 if bad else 0)
