#!/usr/bin/env python3
"""Idempotent assertion-guard self-heal over EVERY lcm_% schema (deploy pre-pass).

Calls db_bootstrap.ensure_assertion_tables() per schema — its DDL is
CREATE TRIGGER IF NOT EXISTS throughout, so a partial 6-name guard set
(e.g. 4/6 after an over-drop rebuild) converges to 6/6 without touching
existing objects. Complements (does not replace) fix_guard_triggers.py,
which stays the stamped one-shot repair for the over-drop pathology.
"""
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("LCM_BACKEND", "postgres")
DSN = os.environ.get(
    "LCM_PG_DSN",
    "postgresql:///lcm?host=%s/lcm-pg/run&port=54329&user=lcm01" % Path.home())

import psycopg  # noqa: E402
# Load the fork as a package (relative imports inside db_bootstrap need it)
_spec = importlib.util.spec_from_file_location(
    "lcmpg", str(REPO / "__init__.py"), submodule_search_locations=[str(REPO)])
_mod = importlib.util.module_from_spec(_spec)
sys.modules["lcmpg"] = _mod
_spec.loader.exec_module(_mod)
db_bootstrap = importlib.import_module("lcmpg.db_bootstrap")  # noqa: E402
_pg = importlib.import_module("lcmpg.backends.postgres")  # noqa: E402


def main() -> int:
    admin = psycopg.connect(DSN, autocommit=True)
    schemas = [r[0] for r in admin.execute(
        "select nspname from pg_namespace where nspname ~ '^lcm_[a-z0-9]+$' "
        "and nspname not like 'lcm\\_test\\_%' order by 1").fetchall()]
    admin.close()
    fail = 0
    for schema in schemas:
        conn = _pg.connect(DSN, schema)   # full shim like fix_guard_triggers
        try:
            db_bootstrap.ensure_assertion_tables(conn)
            conn.commit()
            have = conn.execute(
                "select count(distinct t.tgname) as n from pg_trigger t "
                "join pg_class c on c.oid=t.tgrelid "
                "join pg_namespace n on n.oid=c.relnamespace "
                "where not t.tgisinternal and t.tgname like 'lcm_assertion_%%' "
                "and n.nspname = %s", (schema,)).fetchone() or (0,)
            have = int(have[0])
            print("%-30s %d/6" % (schema, have), flush=True)
            if int(have) != 6:
                fail = 1
        except Exception as exc:
            conn.rollback()
            print("%-30s ERR %s %s" % (schema, type(exc).__name__,
                                        str(exc).splitlines()[0][:80]), flush=True)
            fail = 1
        finally:
            conn.close()
    return fail


if __name__ == "__main__":
    raise SystemExit(main())
