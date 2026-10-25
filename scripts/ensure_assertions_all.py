#!/usr/bin/env python3
"""Ensure assertion V4 on every real lcm_* schema (PG), repairing partial states.

The 6 lcm_assertion_* triggers are installed by db_bootstrap.ensure_assertion_
tables via a script that can abort midway when an OLD partial state lingers
(e.g. a source_delete trigger function whose DELETE references a dropped
lcm_assertion_relations -> relation does not exist -> everything after that
statement is skipped). ensure_assertion_tables alone then "succeeds" while
leaving the guard set incomplete.

Repair per schema (idempotent):
  1. drop the 3 assertion tables CASCADE (drops their triggers + _trg_* funcs)
  2. drop any orphan _trg_lcm_assertion_* functions CASCADE
  3. run ensure_assertion_tables fresh
  4. verify 6/6 triggers
Usage: ensure_assertions_all.py [DSN]  (default: LCM_PG_DSN or the standard
private-cluster DSN)
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.hermes/forks/lcm-pg"))
os.environ.setdefault("LCM_BACKEND", "postgres")
DSN = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "LCM_PG_DSN",
    "postgresql:///lcm?host=" + __import__('os').path.expanduser("~") + "/lcm-pg/run&port=54329&user=lcm01")
os.environ["LCM_PG_DSN"] = DSN

from backends import postgres  # noqa: E402
import db_bootstrap  # noqa: E402

WANT = [
    "lcm_assertion_source_insert_guard",
    "lcm_assertion_row_insert_guard",
    "lcm_assertion_relation_insert_guard",
    "lcm_assertion_message_update",
    "lcm_assertion_message_delete",
    "lcm_assertion_source_delete",
]

admin = postgres.connect(DSN, "public")
schemas = [r[0] for r in admin.execute(
    "select nspname from pg_namespace where nspname ~ '^lcm_[a-z0-9]+$' "
    "and nspname not like 'lcm\\_test\\_%' order by 1").fetchall()]
admin.close()
fail = 0
for schema in schemas:
    conn = postgres.connect(DSN, schema)
    has_tables = conn.execute(
        "select count(*) from sqlite_master where type='table' and name like 'lcm_assertion%'"
    ).fetchone()[0]
    have = set(r[0] for r in conn.execute(
        "select name from sqlite_master where type='trigger' and name like 'lcm_assertion%'").fetchall())
    if set(WANT) <= have:
        print("%s: complete (6/6), skip" % schema)
        conn.close()
        continue
    # repair partial state
    for t in ("lcm_assertions", "lcm_assertion_relations", "lcm_assertion_sources"):
        conn.execute("DROP TABLE IF EXISTS %s CASCADE" % t)
    for f in conn.execute(
            "select p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname = current_schema() and p.proname like '_trg\\_lcm\\_assertion%'").fetchall():
        conn.execute("DROP FUNCTION IF EXISTS \"%s\"() CASCADE" % f[0])
    try:
        db_bootstrap.ensure_assertion_tables(conn)
    except Exception as exc:
        print("%s: ensure FAILED: %s" % (schema, str(exc).splitlines()[0][:100]))
        fail = 1
        conn.close()
        continue
    have = set(r[0] for r in conn.execute(
        "select name from sqlite_master where type='trigger' and name like 'lcm_assertion%'").fetchall())
    missing = set(WANT) - have
    # functional guard smoke: bogus source insert must abort
    try:
        conn.execute("INSERT INTO lcm_assertion_sources VALUES (99999990,'v1',%s,'s','user','',1.0,%s,0,0,1.0,NULL,NULL)",
                     ("a" * 64, "a" * 64))
        conn.execute("DELETE FROM lcm_assertion_sources WHERE source_store_id=99999990")
        guard = "FAIL(inserted)"
        fail = 1
    except Exception as exc:
        guard = "FIRES OK" if "missing or metadata" in str(exc) else str(exc).splitlines()[0][:60]
    print("%s: triggers %d/6%s guard=%s%s" % (
        schema, len(have),
        " missing=" + ",".join(sorted(missing)) if missing else "",
        guard, "" if guard == "FIRES OK" else ""))
    if missing:
        fail = 1
    conn.close()
raise SystemExit(fail)
