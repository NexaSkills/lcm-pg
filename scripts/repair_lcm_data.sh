#!/bin/bash
# repair-lcm-data: rebuild lcm_data's canonical table set from db_bootstrap DDL.
# The legacy 'data' profile schema lost its core tables (messages, summary_nodes,
# files) to an old sqlite-era artifact cleanup; nothing references lcm_data
# (legacy profile removed) and drop_zombie_schemas already treats it as
# residue, so only an empty canonical shell is recreated. No data is restored.
set -o pipefail
cd ~/.hermes/plugins/lcm-pg || exit 1
export LCM_PG_DSN="${LCM_PG_DSN:-postgresql://lcm01@/lcm?host=$HOME/lcm-pg/run&port=54329}"
SCHEMA="${1:-lcm_data}"
~/lcm-pg/venv/bin/python - <<PY
import sys, re
REPO = os.path.expanduser("~/.hermes/plugins/lcm-pg")
sys.path.insert(0, REPO)
import importlib, importlib.util
spec = importlib.util.spec_from_file_location("lcmpg", REPO + "/__init__.py",
                                              submodule_search_locations=[REPO])
m = importlib.util.module_from_spec(spec); sys.modules["lcmpg"] = m; spec.loader.exec_module(m)
pg  = importlib.import_module("lcmpg.backends.postgres")
dbb = importlib.import_module("lcmpg.db_bootstrap")

schema = "$SCHEMA"
DSN = "$LCM_PG_DSN"
conn = pg.connect(DSN, schema)

have = {r[0] for r in conn.execute(
    "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace"
    " where n.nspname = current_schema() and c.relkind in ('r','v')")}

# canonical tables: replay the fork's own ensure_* entrypoints (same code
# path a fresh schema takes at first open; all CREATE IF NOT EXISTS).
for fn in ("ensure_metadata_table", "ensure_migration_state_table",
           "ensure_lifecycle_state_table", "ensure_message_origin_columns",
           "ensure_temporal_rollup_tables", "ensure_embedding_tables",
           "ensure_chunk_tables"):
    f = getattr(dbb, fn, None)
    if f is None:
        print("skip (no entrypoint):", fn); continue
    try:
        f(conn); conn.commit(); print("ok:", fn)
    except Exception as e:
        conn.rollback(); print("ERR:", fn, type(e).__name__, str(e)[:120])
# core content tables: create from canonical DDL. messages lives in store.py
# (first-open path); summary_nodes in db_bootstrap. No 'files' table exists
# anywhere in the fork — not required by any check; do not invent one.
DDL_SRCS = [open(REPO + "/db_bootstrap.py").read(), open(REPO + "/store.py").read()]
for t in ("messages", "summary_nodes"):
    m = None
    for src in DDL_SRCS:
        m = re.search(r"CREATE TABLE(?: IF NOT EXISTS)?\s+%s\s*\((.*?)\n\s*\);" % t, src, re.S)
        if m: break
    if not m:
        print("DDL not found for", t); continue
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS %s (%s)" % (t, m.group(1)))
        conn.commit(); print("ok:", t)
    except Exception as e:
        conn.rollback(); print("ERR:", t, type(e).__name__, str(e)[:120])

after = {r[0] for r in conn.execute(
    "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace"
    " where n.nspname = current_schema() and c.relkind in ('r','v')")}
missing = [t for t in ("messages","summary_nodes","lcm_rollups",
                      "lcm_assertions","lcm_assertion_sources",
                      "lcm_assertion_relations") if t not in after]
if missing:
    print("STILL MISSING:", missing)
    sys.exit(1)
print("lcm_data canonical shell ok:", len(after), "tables")
conn.close()
PY
rc=$?
# now converge guards via the idempotent self-heal (messages exists -> no abort)
if [ $rc -eq 0 ]; then
  ~/lcm-pg/venv/bin/python ~/.hermes/plugins/lcm-pg/scripts/ensure_assertion_guards.py 2>&1 | grep -E "^${SCHEMA} |failures" || true
fi
exit $rc
