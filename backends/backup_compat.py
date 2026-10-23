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

logger = logging.getLogger(__name__)

# derived-state tables never copied between snapshots (rebuilt by triggers)
_SKIP_TABLES = {"messages_fts_docsize", "summary_nodes_fts_docsize",
                "messages_fts", "summary_nodes_fts"}


def _is_pg(conn) -> bool:
    return type(conn).__module__.startswith("hermes_lcm")


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


def copy_snapshot(src, dst, *, native_backup=None) -> None:
    """Copy src into dst. Tries native sqlite backup first (if provided and
    applicable), falls back to a logical row copy. Raises only if everything
    failed."""
    if native_backup is not None and not _is_pg(src) and not _is_pg(dst):
        native_backup()
        return
    errors = []
    if native_backup is not None and not _is_pg(src):
        try:
            native_backup()
            return
        except Exception as exc:
            errors.append(f"native backup: {exc}")
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
