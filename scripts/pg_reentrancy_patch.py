#!/usr/bin/env python3
"""Reentrancy guard for lcm-pg's borrow-per-operation connection model.

PgConnection holds ONE pooled backend while a transaction scope is open and
releases it only at scope close (32238fb). The lazy pragma introspection
helper _sqlite_ddl_for (behind `PRAGMA table_info`) borrows a SECOND
connection from the SAME pool while the first is still held. With pool
max=1 (the embed runner's tightened config) that second checkout blocks
forever in psycopg_pool.getconn — a deterministic self-deadlock, observed
via py-spy at store._ensure_*_columns / db_bootstrap migrations.

Fix: when the call chain already holds a live physical connection, run the
helper's catalog query on THAT connection (it is schema-relative via
current_schema()::regnamespace, and the held conn has this adapter's
search_path pinned — results are identical). Only the catalog READ moves;
the rendering tail is the fork's own _render_ddl_rows() verbatim.

The DDL string is consumed only by sqlite3-compat shape verifiers
(table_info / SHOW-shaped pragmas), so mirroring the shipped tail is
mandatory; this module therefore imports NOTHING from the fork and is
installed by backends/postgres.connect() calling install(module).
"""
from __future__ import annotations

# The catalog query mirrors backends/postgres.py::_sqlite_ddl_for exactly
# (9 columns: attnum, name, storage class, notnull, is-seq-default, default
# expression, pk ordinal, attgenerated, table-level CHECK aggregate).
_QUERY = (
    "SELECT a.attnum, a.attname,"
    " CASE"
    "   WHEN a.atttypid IN ('integer'::regtype,'bigint'::regtype,"
    "     'smallint'::regtype) THEN 'INTEGER'"
    "   WHEN a.atttypid IN ('real'::regtype,'double precision'::regtype,"
    "     'numeric'::regtype) THEN 'REAL'"
    "   WHEN a.atttypid = 'bytea'::regtype OR"
    "     EXISTS (SELECT 1 FROM pg_type b WHERE b.oid = a.atttypid"
    "       AND b.typname = 'blob' AND b.typtype = 'd')"
    "     THEN 'BLOB'"
    "   ELSE 'TEXT' END,"
    " a.attnotnull,"
    " (d.adbin IS NOT NULL AND pg_get_expr(d.adbin, d.adrelid)"
    "     LIKE 'nextval%%'),"
    " CASE WHEN d.adbin IS NULL THEN NULL"
    "   WHEN pg_get_expr(d.adbin, d.adrelid)"
    "     LIKE 'nextval%%' THEN NULL"
    "   WHEN position('::' in pg_get_expr(d.adbin, d.adrelid)) > 0 THEN"
    "     left(pg_get_expr(d.adbin, d.adrelid),"
    "          position('::' in pg_get_expr(d.adbin, d.adrelid)) - 1)"
    "   ELSE pg_get_expr(d.adbin, d.adrelid) END,"
    " (SELECT u.ord::int FROM pg_index i,"
    "     LATERAL unnest(i.indkey::int[])"
    "       WITH ORDINALITY AS u(k, ord)"
    "     WHERE i.indrelid = a.attrelid AND i.indisprimary"
    "       AND u.k = a.attnum LIMIT 1),"
    " a.attgenerated,"
    " (SELECT string_agg("
    "     'CHECK(' || _lcm_checkfold(p.oid) || ')', ', ' ORDER BY p.oid)"
    "   FROM pg_constraint p"
    "   WHERE p.conrelid = a.attrelid AND p.contype = 'c')"
    " FROM pg_attribute a"
    " LEFT JOIN pg_attrdef d"
    "   ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE a.attrelid = (SELECT oid FROM pg_class"
    "   WHERE relname = %s"
    "     AND relnamespace = current_schema()::regnamespace)"
    "   AND a.attnum > 0 AND NOT a.attisdropped"
    "   AND a.attname <> 'rowid'"
    " ORDER BY a.attnum"
)


def install(pg) -> None:
    if pg is None or getattr(pg, "_LCM_PG_REENTRANT_INSTALL_DONE", False):
        return
    orig = pg._sqlite_ddl_for
    default_to_sqlite = getattr(pg, "_pg_default_to_sqlite", None)

    def _render(rows, table):
        # verbatim mirror of _sqlite_ddl_for's rendering tail
        if not rows:
            return None
        cols = []
        pk_cols = sorted(
            (p for p in rows if p[6] and not (len(p) > 7 and p[7] == "s")),
            key=lambda p: p[6])
        autoincrement_pk = (len(pk_cols) == 1 and bool(pk_cols)
                            and pk_cols[0][6] == 1
                            and pk_cols[0][4] and pk_cols[0][2] == "INTEGER")
        for (attnum, name, stype, notnull, is_seq, dflt, pkord, gen,
             _checks) in rows:
            if gen == "s":
                continue  # STORED generated col (rowid shim)
            bits = ['"' + str(name).replace('"', '""') + '"', stype]
            if autoincrement_pk and pkord == 1:
                bits.append("PRIMARY KEY AUTOINCREMENT")
            else:
                if notnull and not (autoincrement_pk and pkord == 1):
                    bits.append("NOT NULL")
                if dflt is not None:
                    d = str(dflt)
                    if default_to_sqlite is not None:
                        d = default_to_sqlite(d)
                    bits.append("DEFAULT " + d)
            cols.append(" ".join(bits))
        if pk_cols and not autoincrement_pk:
            cols.append("PRIMARY KEY (%s)" % ", ".join(
                '"' + str(p[1]).replace('"', '""') + '"' for p in pk_cols))
        checks = next((r[8] for r in rows if len(r) > 8 and r[8]), None)
        if checks:
            depth = 0
            buf = []
            parts = []
            for ch in str(checks):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if depth == 0 and ch == "," and buf and buf[-1] == " ":
                    parts.append("".join(buf).strip())
                    buf = []
                    continue
                buf.append(ch)
            if "".join(buf).strip():
                parts.append("".join(buf).strip())
            cols.extend(p for p in parts if p)
        return "CREATE TABLE %s (%s)" % ('"' + str(table).replace('"', '""') + '"',
                                         ", ".join(cols))

    def _held_conn(conn):
        probe = conn
        seen = set()
        for _ in range(8):
            if probe is None or id(probe) in seen:
                return None
            seen.add(id(probe))
            c = getattr(probe, "_conn", None)
            if c is not None and not getattr(c, "closed", 0):
                return c
            probe = (getattr(probe, "_adapter", None)
                     or getattr(probe, "_base", None)
                     or (c if c is not None else None))
        return None

    def _sqlite_ddl_for(table, conn=None):
        held = _held_conn(conn)
        if held is not None:
            try:
                with held.cursor() as cur:
                    cur.execute(_QUERY, (table,))
                    return _render(cur.fetchall(), table)
            except Exception:
                pass  # never let the shortcut break the caller
        return orig(table, conn)

    pg._sqlite_ddl_for = _sqlite_ddl_for
    pg._LCM_PG_REENTRANT_INSTALL_DONE = True
