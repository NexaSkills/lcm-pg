"""Regression: the ON-less DROP TRIGGER resolver must be CURRENT-SCHEMA scoped.

Root cause (source_insert_guard 5/6 drift on the one-schema-per-profile fleet):
backends/postgres._drop_trigger_pg rendered an unqualified SQLite
``DROP TRIGGER IF EXISTS name`` as a DO block whose target was resolved by a
GLOBAL ``FROM pg_trigger WHERE tgname = name LIMIT 1`` scan -- with no schema
qualifier and no ORDER BY. search_path is ``"<schema>, public"``, so when the
SAME trigger name lives in MULTIPLE schemas (the fleet case) the scan resolves
to an ARBITRARY schema and the trailing ``CASCADE`` drops THAT schema's shadow
_trg_<name>() function -- silently wiping an unrelated schema's guard, leaving
exactly one (the last-processed) schema at 6/6.

Fix: resolve through pg_namespace filtered to current_schema().
"""
import os
import types
import uuid
from pathlib import Path

import pytest

if "hermes_lcm" not in __import__("sys").modules:
    package = types.ModuleType("hermes_lcm")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    package.__package__ = "hermes_lcm"
    __import__("sys").modules["hermes_lcm"] = package

DSN = os.environ.get("LCM_PG_DSN")


def _guard_def(schema):
    # identical trigger + shadow fn in `schema`, named to emulate the 6th guard
    name = "z_probe_guard_" + uuid.uuid4().hex[:10]
    fn = "_trg_" + name
    return name, (
        'CREATE FUNCTION "{s}"."{fn}"() RETURNS trigger LANGUAGE plpgsql AS $t$ '
        "BEGIN RETURN NEW; END $t$;\n"
        'CREATE TRIGGER "{name}" BEFORE INSERT ON "{s}"."z_probe_src" '
        'FOR EACH ROW EXECUTE FUNCTION "{s}"."{fn}"();'
    ).format(s=schema, fn=fn, name=name)


@pytest.mark.skipif(not DSN, reason="requires LCM_PG_DSN (PG backend)")
def test_unqualified_drop_trigger_is_schema_scoped():
    from hermes_lcm.backends import postgres

    a = "z_probe_a_" + uuid.uuid4().hex[:8]
    b = "z_probe_b_" + uuid.uuid4().hex[:8]
    admin = postgres.connect(DSN, "public")
    admin.execute("CREATE SCHEMA %s" % a)
    admin.execute("CREATE SCHEMA %s" % b)
    for s in (a, b):
        admin.execute('CREATE TABLE "%s"."z_probe_src"(id int primary key)' % s)
    try:
        # identical trigger name in BOTH schemas
        for s in (a, b):
            _name, ddl = _guard_def(s)
            name = _name
            admin.execute(ddl)

        # unqualified DROP TRIGGER from schema `a`'s pinned connection
        ca = postgres.connect(DSN, a)
        ca.execute("DROP TRIGGER IF EXISTS %s" % name)  # ON-less, SQLite form
        ca.commit()

        def present(s):
            r = admin.execute(
                "select count(*) from pg_trigger t join pg_class c on c.oid=t.tgrelid "
                "join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname = %s and t.tgname = %s", (s, name)).fetchone()
            return int(r[0])

        # the fix: schema a's drop hits ONLY a; the same-named trigger in b survives
        assert present(a) == 0, "schema a drop must have removed a's trigger"
        assert present(b) == 1, (
            "REGRESSION: unqualified drop wiped schema b's same-named trigger "
            "(cross-schema guard wipe)")
    finally:
        admin.execute("DROP SCHEMA IF EXISTS %s CASCADE" % a)
        admin.execute("DROP SCHEMA IF EXISTS %s CASCADE" % b)
        admin.close()
