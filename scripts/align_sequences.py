#!/usr/bin/env python3
"""Setval every lcm_* schema's owned sequences to max(col) so new inserts
never collide with migrated ids. Run after migrate_sqlite_to_pg.py."""
import os, psycopg, re

DSN = os.environ.get("LCM_PG_DSN", "postgresql:///lcm?host=/run/postgresql&user=mybuddy")
pg = psycopg.connect(DSN, autocommit=True)
schemas = [r[0] for r in pg.execute(
    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'lcm\\_%' AND nspname NOT LIKE '%e2e%' "
    "AND nspname NOT LIKE '%probe%' AND nspname NOT LIKE '%mig%' ORDER BY 1")]
fixed = 0
for schema in schemas:
    rows = pg.execute("""
        SELECT s.relname, t.relname, a.attname
        FROM pg_class s
        JOIN pg_depend d ON d.objid=s.oid AND d.deptype='a'
        JOIN pg_class t ON t.oid=d.refobjid
        JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=%s AND s.relkind='S'""", (schema,)).fetchall()
    for seq, tbl, col in rows:
        mx = pg.execute(f'SELECT coalesce(max("{col}"),0) FROM "{schema}"."{tbl}"').fetchone()[0]
        try:
            last, called = pg.execute(f'SELECT last_value, is_called FROM "{schema}"."{seq}"').fetchone()
            nextv = last + (1 if called else 0)
            if nextv <= mx:
                pg.execute("SELECT setval(%s::regclass, %s)", (f'"{schema}"."{seq}"', int(mx)))
                print(f"fixed {schema}.{seq}: next {nextv} -> {int(mx)+1}")
                fixed += 1
        except Exception as e:
            print(f"skip {schema}.{seq}: {str(e)[:60]}")
print(f"align_sequences: {fixed} fixed across {len(schemas)} schemas")
