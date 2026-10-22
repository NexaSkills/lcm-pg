"""Backend-neutral connection factory for the lcm-pg dual-backend fork.

Set LCM_BACKEND=postgres (plus LCM_PG_DSN) to route ALL store connections to
PostgreSQL (one schema per profile, see backends/postgres.py). Default/absent
= SQLite, byte-for-byte the upstream behaviour.

Stores call connect(db_path) instead of sqlite3.connect(...); the returned
object exposes the sqlite3 surface the stores use (execute, executescript,
executemany, cursor, commit, row_factory assignment).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

# fts_table, content_table, indexed_column — mirrors upstream FtsSpec objects
_FTS_SPECS = (
    ("messages_fts", "messages", "content"),
    ("summary_nodes_fts", "summary_nodes", "summary"),
    # dag.SummaryDAG.search (the primary summary-node FTS path feeding
    # lcm_expand_query / lcm_recall) queries `FROM nodes_fts fts JOIN
    # summary_nodes n ON n.node_id = fts.rowid ... WHERE nodes_fts MATCH ?`.
    # nodes_fts is a SQLite external-content FTS5 table over summary_nodes
    # (dag.build_nodes_fts_spec) but was missing here, so on the PG backend the
    # rewrite never fired: `rank` (an FTS5 hidden column) leaked into the SQL
    # verbatim and PG raised `column "rank" does not exist`. Registering the
    # spec installs summary.summary_summary_tsv + its own triggers/docsize
    # (ensure_fts names every object after (table,column), so these never
    # collide with the summary_nodes_* pair above) and lets _rewrite_ts rewrite
    # the query. This schema already carries summary_nodes_message_link/
    # summary_children (see fix_summary_nodes_content_tsv.sql), confirming
    # nodes_fts was always meant to be one of these specs.
    ("nodes_fts", "summary_nodes", "summary"),
)


def _ensure_fts_tables(base) -> tuple:
    from . import fts_pg

    out = []
    for fts_t, content_t, col in _FTS_SPECS:
        try:
            fts_pg.ensure_fts(base, content_t, col)
            out.append((fts_t, content_t, col))
        except Exception:
            pass  # table not created (yet) in this schema
    return tuple(out)


def _existing_fts_tables(base) -> tuple:
    from . import fts_pg

    return tuple(t for t in _FTS_SPECS if fts_pg.has_fts(base, t[1], t[2]))


def backend_for() -> str:
    """Backend in force for this process.

    Postgres is the DEFAULT: an unset LCM_BACKEND must never select SQLite,
    because a host or profile missing that env var would otherwise silently run
    the whole engine against SQLite while its operators believe the fleet is on
    the Postgres solution — silent divergence, not a failure.

    SQLite remains reachable only by explicit declaration: set
    LCM_BACKEND=sqlite deliberately (upstream's SQLite-only test suite uses
    this). Any other value is a mistake and is reported as one.
    """
    b = (os.environ.get("LCM_BACKEND") or "postgres").strip().lower()
    if b in ("postgres", "postgresql", "pg"):
        return "postgres"
    if b == "sqlite":
        return "sqlite"
    raise RuntimeError(
        f"LCM_BACKEND={b!r} is not supported; this solution runs on PostgreSQL "
        "(use LCM_BACKEND=postgres; LCM_BACKEND=sqlite exists solely for "
        "upstream's SQLite-only tests)"
    )


def _pg_dsn() -> str:
    dsn = os.environ.get("LCM_PG_DSN")
    if not dsn:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        p = Path(home) / ".env"
        if p.is_file():
            for line in p.read_text().splitlines():
                if line.startswith("LCM_PG_DSN="):
                    dsn = line.split("=", 1)[1].strip()
                    break
    if not dsn:
        raise RuntimeError("LCM_BACKEND=postgres but LCM_PG_DSN is not set")
    return dsn


def connect(db_path: Any, *, mode: str = "rw", **sqlite_kwargs: Any):
    """Open a backend connection for this store.

    mode: 'rw' or 'ro' (mirrors the sqlite uri mode=ro call sites).
    Extra sqlite3.connect kwargs (timeout, check_same_thread, uri) are
    accepted and ignored on the Postgres path.
    """
    if backend_for() == "postgres":
        from .postgres import connect as pg_connect, schema_for_profile
        from .fts_conn import FtsConnection, set_spec
        from . import fts_pg

        s0 = str(db_path)
        # SQLite URI semantics: a '?mode=ro' path means the caller wants a
        # strictly read-only handle (upstream's read paths pass these).
        if "mode=ro" in s0:
            mode = "ro"
        hermes_home = ""
        p = Path(s0.split("?")[0])
        # lcm.db normally lives directly under HERMES_HOME
        if p.name.startswith("lcm"):
            hermes_home = str(p.parent)
        schema = os.environ.get("LCM_PG_SCHEMA") or schema_for_profile(hermes_home or None)
        query_only = (mode == "ro")
        if query_only:
            # DDL/idempotent-helper provisioning (lcm_normalize_fts, FTS
            # install) must not run over a read-only handle — a retired or
            # never-provisioned schema must fail closed, not self-provision.
            # Provision on a short-lived admin handle instead.
            admin = pg_connect(_pg_dsn(), schema)
            try:
                fts_pg.ensure_helper(admin)
            finally:
                try:
                    admin.close()
                except Exception:
                    pass
            base = pg_connect(_pg_dsn(), schema, query_only=True)
        else:
            base = pg_connect(_pg_dsn(), schema)
            fts_pg.ensure_helper(base)
        conn = FtsConnection(base)
        # Durability is configured where the handle is created so every store
        # gets it without each one having to opt in; the upstream
        # configure_connection() call sites only carry SQLite pragmas.
        try:
            from .. import db_bootstrap
            db_bootstrap.configure_postgres_connection(conn)
        except Exception:
            pass
        if mode == "ro":
            set_spec(conn, _existing_fts_tables(base))
        else:
            set_spec(conn, _ensure_fts_tables(base))
        return conn
    # --- SQLite path: upstream behaviour ------------------------------------
    s = str(db_path)
    if s.startswith("file:") or s == ":memory:":
        sqlite_kwargs.setdefault("uri", True)
    return sqlite3.connect(s, **sqlite_kwargs)
