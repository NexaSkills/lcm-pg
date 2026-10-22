"""Native pg_catalog introspection for LCM (replaces PRAGMA emulation).

The shim answers `PRAGMA table_info` by reconstructing SQLite DDL from the
Postgres catalog and feeding it to an in-memory stdlib sqlite3 instance, so
SQLite runs at runtime on the Postgres path and its answers are artefacts of
reconstruction rather than of the live database. That indirection is what made
the rollup verifier report defects that did not exist.

`native_table_info()` gives the same row shape straight from pg_catalog: same
column names, same ordering (attnum), same `pk` convention (1 for the first
column of the primary key, as SQLite reports for an INTEGER PRIMARY KEY rowid
alias). The `type` text deliberately reports the *declared* SQLite type where
the Postgres type is a widened equivalent (bigint/serial for an INTEGER
PRIMARY KEY), because LCM's shape checks compare storage classes.
"""

from __future__ import annotations

from typing import Any, Iterable


# sqlite storage class for the postgres types LCM's DDL maps onto.
_TYPE_MAP = {
    "bigint": "INTEGER",
    "integer": "INTEGER",
    "smallint": "INTEGER",
    "numeric": "NUMERIC",
    "real": "REAL",
    "double precision": "REAL",
    "timestamp with time zone": "TEXT",
    "timestamp without time zone": "TEXT",
    "date": "TEXT",
    "character varying": "TEXT",
    "character": "TEXT",
    "text": "TEXT",
    "bytea": "BLOB",
    "boolean": "INTEGER",
    "jsonb": "TEXT",
    "json": "TEXT",
    "tsvector": "TEXT",
    "uuid": "TEXT",
}


def _sqlite_type(pg_type: str) -> str:
    t = (pg_type or "").strip().lower()
    if t in _TYPE_MAP:
        return _TYPE_MAP[t]
    if t.startswith("character varying") or t.startswith("varchar"):
        return "TEXT"
    if t.startswith("numeric") or t.startswith("decimal"):
        return "NUMERIC"
    if t.startswith("timestamp"):
        return "TEXT"
    if t.startswith("double") or t.startswith("float"):
        return "REAL"
    if t.startswith("int") or t.startswith("serial") or t.startswith("bigint"):
        return "INTEGER"
    if "vector" in t:
        return "BLOB"
    return t.upper()


def _vals(row: Any) -> tuple:
    """Positional values for a cursor row.

    The Postgres shim returns a dict subclass whose integer indexing works but
    whose iteration yields column *names*; psycopg returns tuples. Reading
    `.values()` handles both without touching either implementation.
    """
    if isinstance(row, dict):
        return tuple(row.values())
    return tuple(row)


def _pk_first_columns(cur, rel_oid: int) -> set[int]:
    """attnums that are the *first* column of some primary key (sqlite pk=1)."""
    cur.execute(
        "select k.ord from pg_constraint c"
        " join LATERAL unnest(c.conkey) with ordinality k(col, ord) on true"
        " where c.conrelid = %s and c.contype = 'p' and k.ord = 1",
        (rel_oid,),
    )
    return {int(_vals(r)[0]) for r in (cur.fetchall() or [])}


def _rel_oid(cur, table: str) -> int | None:
    # search_path is the connection's schema, same resolution sqlite uses.
    cur.execute(
        "select c.oid from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = current_schema() and c.relname = %s"
        " and c.relkind in ('r','p','v','f')",
        (table,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return int(_vals(row)[0])


def _is_autoincrement_pk(cur, rel_oid: int, attnum: int) -> bool:
    """True when this column is a single-column PK fed by a sequence.

    SQLite reports an `INTEGER PRIMARY KEY AUTOINCREMENT` column as
    notnull=0 with an empty default, because the value is assigned implicitly
    rather than by a DEFAULT clause. Postgres stores the same column as NOT
    NULL DEFAULT nextval(...). Marking those columns lets callers keep the
    sqlite-exact shape without the shim reconstructing DDL.
    """
    cur.execute(
        "select 1 from pg_constraint c"
        " join LATERAL unnest(c.conkey) with ordinality k(col, ord) on true"
        " where c.conrelid = %s and c.contype = 'p' and k.ord = 1 and k.col = %s"
        " and exists (select 1 from pg_attrdef d"
        "   where d.adrelid = c.conrelid and d.adnum = k.col"
        "     and pg_get_expr(d.adbin, d.adrelid) like 'nextval%%')",
        (rel_oid, attnum),
    )
    return cur.fetchone() is not None


def native_table_info(conn, table: str) -> list[tuple]:
    """Rows shaped like `PRAGMA table_info(t)`: (cid,name,type,notnull,dflt_value,pk)."""
    cur = conn.cursor()
    oid = _rel_oid(cur, table)
    if oid is None:
        return []
    pk_first = _pk_first_columns(cur, oid)
    pk_position = _pk_positions(cur, oid)
    cur.execute(
        "select a.attnum::int, a.attname::text,"
        " format_type(a.atttypid, a.atttypmod)::text,"
        " a.attnotnull::int, coalesce(pg_get_expr(d.adbin, d.adrelid), cast(null as text))"
        " from pg_attribute a"
        " left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum"
        " where a.attrelid = %s and a.attnum > 0 and not a.attisdropped"
        " order by a.attnum",
        (oid,),
    )
    rows = cur.fetchall() or []
    out: list[tuple] = []
    for row in rows:
        attnum, name, pgtype, notnull, default = _vals(row)
        attnum = int(attnum)
        auto = _is_autoincrement_pk(cur, oid, attnum)
        # SQLite's INTEGER PRIMARY KEY is the rowid alias and carries no NOT
        # NULL, whether or not a sequence feeds it; every other column keeps
        # the constraint the server actually enforces.
        implicit_notnull = auto or bool(pk_position.get(attnum))
        out.append((
            attnum,
            str(name),
            _sqlite_type(str(pgtype)),
            0 if implicit_notnull else (1 if notnull else 0),
            None if auto else _normalize_default(default),
            int(pk_position.get(attnum, 0)),
        ))
    cur.close()
    return out


def _normalize_default(expr: str | None) -> str | None:
    """Render a Postgres default expression the way LCM's own DDL states it.

    Only the two forms LCM actually emits are rewritten; anything else is
    returned unchanged so callers see the real server text rather than a guess.
    """
    if expr is None:
        return None
    t = str(expr).strip()
    low = t.lower().replace("  ", " ")
    if "extract(epoch from now())" in low:
        return "strftime('%s','now')"
    if low in ("now()", "now()::text", "current_timestamp", "current_timestamp()"):
        return "strftime('%s','now')"
    if low.startswith("nextval("):
        # sqlite's INTEGER PRIMARY KEY autoincrement has no default text.
        return None
    # A typed literal such as 'building'::text or ''::text: sqlite records the
    # bare literal it was given, so drop the Postgres cast annotation.
    if t.endswith("::text") and t.count("::") == 1:
        return t[: -len("::text")]
    return t


def _pk_positions(cur, rel_oid: int) -> dict[int, int]:
    """attnum -> 1-based position within the primary key (0/absent if not in a PK)."""
    cur.execute(
        "select k.col::int, k.ord::int from pg_constraint c"
        " join LATERAL unnest(c.conkey) with ordinality k(col, ord) on true"
        " where c.conrelid = %s and c.contype = 'p'",
        (rel_oid,),
    )
    return {int(_vals(r)[0]): int(_vals(r)[1]) for r in (cur.fetchall() or [])}


def native_column_names(conn, table: str) -> set[str]:
    return {r[1] for r in native_table_info(conn, table)}


def table_exists_native(conn, name: str) -> bool:
    return _rel_oid(conn.cursor(), name) is not None
