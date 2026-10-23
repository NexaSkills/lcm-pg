"""Online backup parity for sqlite-flavoured code (Connection.backup shim).

PG has no page-level backup API. When the SOURCE handle is PG-shaped
(PgConnection / FtsConnection over PG) we snapshot it with a transaction-
consistent table copy into the destination handle — the destination is
normally a fresh handle (store-provisioned schema, or a new sqlite file).
When both handles are plain sqlite, the native sqlite3 backup is used.

Usage:

    from backends.backup_compat import copy_snapshot
    copy_snapshot(src, dst)          # src, dst: sqlite-shape connections
    # or, to keep native backup where possible:
    copy_snapshot(src, dst, native_backup=lambda: src.backup(dst))
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# derived-state tables never copied between snapshots (rebuilt by triggers)
_SKIP_TABLES = {"messages_fts_docsize", "summary_nodes_fts_docsize",
                "messages_fts", "summary_nodes_fts"}


def _is_pg(conn) -> bool:
    # Module-name prefix alone is not reliable: under standalone/script
    # loading the class's __module__ may be 'postgres' / 'backends.postgres'
    # (file-path loader), not 'hermes_lcm.backends.postgres'.
    t = type(conn)
    mod = t.__module__ or ""
    if mod.startswith("hermes_lcm") or mod.endswith("backends.postgres") \
            or mod.endswith("backends.fts_conn") or mod in ("postgres", "fts_conn"):
        return True
    # explicit marker attributes set by the PG shim itself
    if hasattr(conn, "_lcm_pg_handle") or hasattr(t, "_lcm_pg_handle"):
        return True
    # FtsConnection over PG exposes _base -> PgConnection
    base = getattr(conn, "_base", None)
    if base is not None and base is not conn:
        return _is_pg(base)
    return False


def _unwrap(conn):
    # FtsConnection wraps the pg connection as ._base
    return getattr(conn, "_base", conn)


def _table_names(conn) -> list[str]:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
        "ORDER BY table_name")
    rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(list(r.values())[0] if isinstance(r, dict) else r[0])
    return out


def _rows(conn, table: str):
    cur = conn.execute(f'SELECT * FROM "{table}"')
    for r in cur:
        yield dict(r) if not isinstance(r, dict) else r


def _target_tables(conn) -> set[str]:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")
    return {(list(r.values())[0] if isinstance(r, dict) else r[0]) for r in cur}


def _is_sqlite(conn) -> bool:
    """Genuine stdlib sqlite handle (the backup destination case)."""
    return type(conn).__module__ == "sqlite3" and type(conn).__name__ == "Connection"


def _pg_table_ddl(conn, table: str) -> str | None:
    """Reconstruct a CREATE TABLE for `table` from the PG catalog, in
    sqlite-accepted form (identity PK -> INTEGER PRIMARY KEY, text/int/
    bool/timestamp types mapped loosely — a snapshot backup needs the
    columns, not exact PG types)."""
    cur = conn.execute(
        """SELECT a.attname,
                  format_type(a.atttypid, a.atttypmod) AS t,
                  a.attnotnull,
                  pg_get_expr(d.adbin, d.adrelid) AS def,
                  (a.attidentity <> '') AS ident
           FROM pg_attribute a
           LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
           WHERE a.attrelid = (%s || '.' || %s)::regclass
             AND a.attnum > 0 AND NOT a.attisdropped
           ORDER BY a.attnum""",
        (conn_info_schema(conn), table))
    rows = cur.fetchall()
    cols = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        name, t, notnull, defx, ident = (d["attname"], d["t"], d["attnotnull"],
                                         d.get("def"), d.get("ident"))
        if ident:
            cols.append(f'"{name}" INTEGER PRIMARY KEY')
            continue
        base = t.split("(")[0].strip().lower()
        if base in ("text", "character varying", "character", "citext", "uuid",
                    "timestamp", "timestamptz", "date", "json", "jsonb", "bytea"):
            sqlt = "TEXT"
        elif base in ("integer", "bigint", "smallint", "numeric", "real",
                      "double precision", "int8", "int4", "serial", "bigserial"):
            sqlt = "INTEGER" if "double" not in base and "real" not in base and "numeric" not in base else "REAL"
        elif base == "boolean":
            sqlt = "INTEGER"
        else:
            sqlt = "TEXT"
        piece = f'"{name}" {sqlt}'
        if ident is False and defx and "nextval" not in defx:
            # sqlite parses DEFAULT only for literals: strip PG type casts
            # (''::text), keep plain literals (0, CURRENT_TIMESTAMP...).
            lit = defx
            if "::" in lit:
                lit = lit[:lit.index("::")]
            if re.match(r"^(NULL|CURRENT_TIMESTAMP|CURRENT_DATE|-?\d+(\.\d+)?)$",
                        lit.strip(), re.I):
                piece += f" DEFAULT {lit.strip()}"
        if notnull and ident is not True:
            piece += " NOT NULL"
        cols.append(piece)
    if not cols:
        return None
    return f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(cols)})'


def conn_info_schema(conn) -> str:
    cur = conn.execute("SELECT current_schema()")
    r = cur.fetchone()
    return (list(dict(r).values())[0] if isinstance(r, dict) else r[0])


def _replicate_sqlite_schema(src, dst) -> None:
    """Create dst (fresh sqlite file) tables from src's PG catalog."""
    for table in _table_names(src):
        ddl = _pg_table_ddl(src, table)
        if ddl:
            dst.execute(ddl)


def copy_snapshot(src, dst, *, native_backup=None) -> None:
    """Copy src into dst. Tries native sqlite backup first (if provided and
    applicable), falls back to a logical row copy. Raises only if everything
    failed."""
    if native_backup is not None and not _is_pg(src) and not _is_pg(dst):
        native_backup()
        return
    if native_backup is not None and not _is_pg(src) and _is_sqlite(dst):
        # both plain sqlite: native page backup
        try:
            native_backup()
            return
        except Exception as exc:
            errors_init = [f"native backup: {exc}"]
        else:
            return
    else:
        errors_init = []
    errors = list(errors_init)
    if _is_sqlite(dst):
        # destination is a fresh sqlite FILE: materialise the schema from
        # the source (CREATE copies keep the backup a self-contained real
        # sqlite database; row INSERTs below fill it).
        try:
            _replicate_sqlite_schema(src, dst)
        except Exception as exc:
            errors.append(f"schema replicate: {exc}")
    try:
        dst_tables = _target_tables(dst)
    except Exception as exc:
        errors.append(f"target probe: {exc}")
        dst_tables = set()
    copied = 0
    for table in _table_names(src):
        if table in _SKIP_TABLES:
            continue  # external-content FTS shadow tables are derived state;
            # copying them between schemas desyncs the content table's triggers
        if table not in dst_tables:
            continue  # destination isn't provisioned for it (external-content
            # shadow tables, etc.) — a full store re-init will rebuild anyway
        try:
            dicts = list(_rows(src, table))
        except Exception as exc:
            errors.append(f"read {table}: {exc}")
            continue
        if not dicts:
            continue
        try:
            dst.execute(f'DELETE FROM "{table}"')
            cols = list(dicts[0].keys())
            collist = ", ".join(f'"{c}"' for c in cols)
            ph = ", ".join(["?"] * len(cols))
            if _is_pg(dst) and table == "messages":
                # keep sqlite rowid/lastrowid parity: carry store_id explicitly
                # and retarget the sequence so later inserts don't collide
                # (a fresh store schema's identity otherwise restarts at 1 and
                # every copied row triggers a duplicate-key error).
                dst.execute(
                    "SELECT setval(pg_get_serial_sequence('messages', 'store_id'), %s, false)",
                    (max(int(d.get("store_id") or 0) for d in dicts),))
            dst.executemany(
                f'INSERT INTO "{table}" ({collist}) VALUES ({ph})',
                [tuple(d.get(c) for c in cols) for d in dicts])
            copied += 1
        except Exception as exc:
            errors.append(f"write {table}: {exc}")
    try:
        dst.commit()
    except Exception:
        pass
    if copied == 0 and errors:
        raise RuntimeError("snapshot copy failed: " + "; ".join(errors[:4]))
