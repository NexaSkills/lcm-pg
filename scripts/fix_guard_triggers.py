#!/usr/bin/env python3
"""Repair the 6th assertion trigger (lcm_assertion_source_insert_guard) on every
real lcm_* schema — WITHOUT wiping other schemas.

WHY THIS EXISTS
---------------
db_bootstrap.ensure_assertion_tables recreates the guard by FIRST issuing an
unqualified ``DROP TRIGGER IF EXISTS lcm_assertion_source_insert_guard`` (line
1896) and only later ``CREATE TRIGGER`` (line 1986). Under the PG shim that DROP
is reverse-resolved (backends/postgres.py _drop_trigger_pg) via a DO block over
pg_trigger. When the same-named trigger exists in MULTIPLE schemas (the fleet
case: one schema per profile) the resolver emits an OVER-SCOPED drop, so running
the full ensure over a batch of schemas DESTROYS the guard in the not-yet- and
already-processed schemas. A full run therefore leaves exactly ONE schema (the
last) with a guard -- the 5/6-vs-6/6 drift, misattributed to a restart.

The old scripts/ensure_assertions_all.py made this worse: it DROPs the three
assertion tables + ``_trg_lcm_assertion_*`` functions with ``CASCADE`` and
rebuilds. The CASCADE-on-DROP-FUNCTION reuses the buggy resolver and clears the
guard trigger in every OTHER schema (debbie 1->0 was observed here), and its
"verify" reads the shim's sqlite_master VIEW -- which reports a trigger that
isn't in real pg_trigger, printing a false "6/6".

THE FIX (per schema, no cross-schema damage)
--------------------------------------------
The guard is ALWAYS ``BEFORE INSERT ON <schema>.lcm_assertion_sources FOR EACH
ROW EXECUTE FUNCTION <schema>._trg_lcm_assertion_source_insert_guard()`` and the
shadow function persists (only the trigger goes missing). So a *schema-qualified*
DROP/CREATE rebuild restores it without a CASCADE DROP FUNCTION and without
dropping the tables. Verification uses pg_trigger (authoritative), plus a
functional smoke-test INSERT that must abort with the guard's RAISE message.

Usage: fix_guard_triggers.py [DSN]   (default LCM_PG_DSN or the private cluster)
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.hermes/forks/lcm-pg"))
os.environ.setdefault("LCM_BACKEND", "postgres")
DSN = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "LCM_PG_DSN",
    "postgresql:///lcm?host=/home/mybuddy/lcm-pg/run&port=54329&user=lcm01")
os.environ["LCM_PG_DSN"] = DSN

from backends import postgres  # noqa: E402

GUARD = "lcm_assertion_source_insert_guard"
TRG_FN = "_trg_" + GUARD
SRC = "lcm_assertion_sources"


def _assert_triggers(conn):
    rows = conn.execute(
        "select tgname from pg_trigger t "
        "join pg_class c on c.oid=t.tgrelid "
        "join pg_namespace n on n.oid=c.relnamespace "
        "where not t.tgisinternal and t.tgname like 'lcm_assertion_%' "
        "and n.nspname = current_schema()").fetchall()
    return set(r[0] for r in rows)


def fix_one(conn):
    have = _assert_triggers(conn)
    if GUARD in have:
        return "already (6/6), skip" if len(have) == 6 else "guard present, %d/6" % len(have)
    fn = conn.execute(
        "select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
        "where p.proname = %s and n.nspname = current_schema()", (TRG_FN,)).fetchone()
    if fn is None:
        # no shadow fn -> must rebuild via ensure (may still over-drop; flag it)
        conn.execute("DROP TABLE IF EXISTS lcm_assertion_relations CASCADE")
        conn.execute("DROP TABLE IF EXISTS lcm_assertion_sources CASCADE")
        conn.execute("DROP TABLE IF EXISTS lcm_assertions CASCADE")
        conn.commit()
        import db_bootstrap  # noqa
        db_bootstrap.ensure_assertion_tables(conn)
        conn.commit()
        return "rebuilt via ensure (no fn): now %d/6" % len(_assert_triggers(conn))
    conn.execute('DROP TRIGGER IF EXISTS "%s" ON "%s"' % (GUARD, SRC))
    conn.execute('CREATE TRIGGER "%s" BEFORE INSERT ON "%s" '
                 'FOR EACH ROW EXECUTE FUNCTION current_schema()."%s"()'
                 % (GUARD, SRC, TRG_FN))
    conn.commit()
    n = len(_assert_triggers(conn))
    # functional smoke: bogus source insert must abort with the guard message
    smoke = ""
    try:
        conn.execute(
            "INSERT INTO \"%s\" VALUES "
            "(99999990,'v1',repeat('a',64),'probe','user','',1.0,repeat('a',64),0,0,1.0,NULL,NULL)"
            % SRC)
        conn.execute("DELETE FROM \"%s\" WHERE source_store_id=99999990" % SRC)
        conn.commit()
        smoke = " guard=FAIL(inserted)"
    except Exception as exc:
        conn.rollback()
        smoke = " guard=FIRES" if "missing or metadata" in str(exc) \
            else " guard=" + str(exc).splitlines()[0][:40]
    return "%d/6%s" % (n, smoke)


def main():
    admin = postgres.connect(DSN, "public")
    schemas = [r[0] for r in admin.execute(
        "select nspname from pg_namespace where nspname ~ '^lcm_[a-z0-9]+$' "
        "and nspname not like 'lcm\\_test\\_%' order by 1").fetchall()]
    admin.close()
    fail = 0
    for schema in schemas:
        conn = postgres.connect(DSN, schema)
        try:
            status = fix_one(conn)
        except Exception as exc:
            status = "ERR %s %s" % (type(exc).__name__, str(exc).splitlines()[0][:80])
            fail = 1
        print("%-30s %s" % (schema, status), flush=True)
        conn.close()
    # final authoritative pass (all connections closed -> no in-flight state)
    conn = postgres.connect(DSN, "public")
    bad = conn.execute(
        "select count(*) from pg_namespace n where n.nspname ~ '^lcm_[a-z0-9]+$' "
        "and n.nspname not like 'lcm\\_test\\_%' and not exists ("
        "  select 1 from pg_trigger t join pg_class c on c.oid=t.tgrelid "
        "  join pg_namespace m on m.oid=c.relnamespace "
        "  where m.nspname=n.nspname and t.tgname='" + GUARD + "')").fetchone()[0]
    conn.close()
    print("schemas missing guard after run:", int(bad))
    raise SystemExit(1 if (bad or fail) else 0)


main()
