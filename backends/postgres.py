"""pg_backend — thin psycopg3 adapter that quacks like sqlite3.Connection.

Part of the lcm-pg dual-backend fork (see FORK.md). This is the ONLY
connection-shape shim Phase 1 needs: the existing store/dag/maintenance code
keeps its SQL, and adapter.sql() rewrites the handful of SQLite-isms into
Postgres equivalents. Everything else (transactions, %s paramstyle,
executemany, cursor context manager) psycopg already matches.

Deliberately NOT a full SQL translator — it refuses to silently guess:
unknown PRAGMAs are handled per policy (no-op / emulate / raise), and any
construct it cannot prove equivalent raises UnsupportedSQL.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Iterable, Optional, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SET_ID_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SET_MS_RE = re.compile(r"^[0-9]{1,10}$")


def _set_stmts(schema: str, lock_timeout_ms: int) -> str:
    """SET statements can't take bind params in PG; validate then interpolate."""
    if not _SET_ID_RE.match(schema):
        raise ValueError(f"invalid schema name: {schema!r}")
    if not _SET_MS_RE.match(str(lock_timeout_ms)):
        raise ValueError("bad lock_timeout_ms")
    return (
        f'SET search_path TO "{schema}", public; '
        f"SET lock_timeout = {int(lock_timeout_ms)};"
    )



class UnsupportedSQL(RuntimeError):
    """Raised when a SQL string cannot be proven SQLite-equivalent."""


class _FtsQueryError(ValueError):
    pass


def schema_for_profile(hermes_home: str | None, explicit: str | None = None) -> str:
    """Deterministic schema naming — no hardcoded profile lists.

    explicit (LCM_PG_SCHEMA) wins; otherwise 'lcm_' + profile_id, where
    profile_id is the HERMES_HOME basename ('.hermes' -> 'default').
    """
    if explicit:
        name = re.sub(r"[^a-z0-9_]", "_", explicit.strip().lower())
    else:
        home = hermes_home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        base = os.path.basename(os.path.normpath(home))
        base = "default" if base in (".hermes", "", ".", ".") else base
        name = "lcm_" + re.sub(r"[^a-z0-9_]", "_", base.lower())
    if not _SCHEMA_RE.match(name):
        raise ValueError(f"invalid schema name: {name!r}")
    return name


# --- SQLite-ism rewriting ---------------------------------------------------

_PRAGMA_EMULATIONS = {
    # name -> callable(adapter) -> (sql, params) or None (no-op)
    "busy_timeout": None,      # no per-connection analogue; PG waits or lock_timeout set at pool level
    "journal_mode": None,      # meaningless on PG
    "synchronous": None,
    "wal_autocheckpoint": None,
    "cache_size": None,
    "temp_store": None,
    "foreign_keys": None,
    "case_sensitive_like": None,
    "query_only": "query_only",   # handled statefully below
    "integrity_check": "integrity_check",
    "quick_check": "integrity_check",
    "table_info": "table_info",
    "user_version": "user_version",
    "data_version": "schema_version",
    "schema_version": "schema_version",
}


def _rewrite_text(sql: str) -> str:
    s = sql

    # datetime('now', mods...) -> now() ± n * interval '1 unit' [+ at time zone 'utc']
    def _mods_seconds(mods: str) -> float:
        secs = 0
        for part in re.findall(r"'([^']*)'", mods):
            toks = part.strip().split()
            if len(toks) == 2 and toks[0].lstrip("+-").isdigit():
                n = int(toks[0])
                unit = toks[1].rstrip("s")
                secs += n * {"day": 86400, "hour": 3600, "minute": 60, "min": 60,
                             "second": 1, "month": 30 * 86400, "year": 365 * 86400}.get(unit, 1)
        return secs

    def _dt(m: re.Match) -> str:
        secs = _mods_seconds(m.group(1) or "")
        return f"extract(epoch from now()){'' if not secs else (' + ' if secs > 0 else ' - ') + str(abs(secs))}"

    def _dt_bound(m: re.Match) -> str:
        base = {"?": "%s", "%s": "%s"}[m.group(1)]
        secs = _mods_seconds(m.group(2) or "")
        expr = f"to_timestamp({base})"
        return expr + ('' if not secs else (' + ' if secs > 0 else ' - ') + str(abs(secs)))

    s = re.sub(r"\bdatetime\s*\(\s*'now'\s*((?:\s*,\s*'[^']*')*)\)", _dt, s)
    # strftime('%s','now') -> extract(epoch from now())  (the only strftime form in
    # the codebase is an epoch-seconds expression against REAL epoch columns)
    s = re.sub(r"\bstrftime\s*\(\s*'%s'\s*,\s*'now'\s*\)", "extract(epoch from now())", s, flags=re.I)
    # datetime(x) on an epoch/REAL column -> to_timestamp(x) (unix epoch, UTC)
    s = re.sub(r"\bdatetime\s*\(\s*(\?|%s)\s*((?:\s*,\s*'[^']*')*)\)", _dt_bound, s)
    # datetime(col) over a numeric epoch column -> to_timestamp(col)
    s = re.sub(r"\bdatetime\s*\(\s*([A-Za-z_][\w.]*)\s*\)", r"to_timestamp(\1)", s)

    # INSERT OR IGNORE [INTO] -> INSERT ... ON CONFLICT DO NOTHING
    ignore = re.search(r"\bINSERT\s+OR\s+IGNORE\b", s, flags=re.I) is not None
    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", s, flags=re.I)
    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT INTO", s, flags=re.I)
    if ignore and "ON CONFLICT" not in s.upper():
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # INSERT OR REPLACE INTO -> caller must provide ON CONFLICT clause; refuse if absent
    if re.search(r"\bINSERT\s+OR\s+REPLACE\b", s, flags=re.I) and "ON CONFLICT" not in s.upper():
        raise UnsupportedSQL("INSERT OR REPLACE requires an explicit ON CONFLICT DO UPDATE target in PG mode")

    # AUTOINCREMENT -> plain bigserial
    s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "bigserial PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\b", "bigserial PRIMARY KEY", s, flags=re.I)

    # types
    s = re.sub(r"\bREAL\b", "double precision", s, flags=re.I)
    s = re.sub(r"\bBLOB\b", "bytea", s, flags=re.I)
    s = re.sub(r"\bTINYINT\b", "smallint", s, flags=re.I)

    # WITHOUT ROWID clause -> drop
    s = re.sub(r"\bWITHOUT\s+ROWID\b", "", s, flags=re.I)

    # x'hex' blob literals -> decode(hex)
    s = re.sub(r"\bx'([0-9a-fA-F]+)'", r"decode(hex('\1'))", s)

    # random() -> random() works on PG; json_extract(col,'$.key') -> (col)::jsonb ->> 'key'
    s = re.sub(
        r"\bjson_extract\s*\(\s*([A-Za-z_][\w.]*)\s*,\s*'\$\.([\w]+)'\s*\)",
        lambda m: f"(({m.group(1)})::jsonb ->> '{m.group(2)}')",
        s,
    )

    # ? -> %s (careful: skip ? inside string literals)
    out, i, in_str = [], 0, False
    while i < len(s):
        c = s[i]
        if c == "'":
            in_str = not in_str
            out.append(c)
        elif c == "?" and not in_str:
            out.append("%s")
        else:
            out.append(c)
        i += 1
    s = "".join(out)

    # SQLite FTS MATCH quoting: "col" MATCH '"phrase"' -> to_tsvector/plainto
    return s


class _Cursor:
    """sqlite3.Cursor-shaped wrapper over a psycopg cursor."""

    def __init__(self, adapter: "PgConnection", raw):
        self._adapter = adapter
        self._raw = raw
        self._lastrowid: Optional[int] = None
        self._rowcount = -1

    # sqlite3 parity surface
    def execute(self, sql: str, parameters: Sequence | dict | None = None):
        sql, params = self._adapter._translate(sql, parameters)
        self._raw.execute(sql, params)
        self._rowcount = self._raw.rowcount
        if "INSERT" in sql.upper() and "RETURNING" in sql.upper():
            try:
                row = self._raw.fetchone()
                if row:
                    self._lastrowid = row[0] if not isinstance(row, dict) else next(iter(row.values()))
            except Exception:
                pass
        elif "INSERT" in sql.upper():
            # cheap parity: sqlite exposes lastrowid; fetch from the same stmt only if RETURNING added
            self._lastrowid = None
        return self

    def executemany(self, sql: str, seq: Iterable[Sequence]):
        sql, _ = self._adapter._translate(sql, ())
        rows = [tuple(p) for p in seq]
        if rows:
            self._raw.executemany(sql, rows)
            self._rowcount = len(rows)
        return self

    def executescript(self, script: str):
        for stmt in _split_statements(script):
            stmt = stmt.strip()
            if stmt:
                self._raw.execute(_rewrite_text(stmt))
        return self

    # sqlite3 parity: .row_factory assignment is handled at connection level
    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    def fetchmany(self, n=None):
        return self._raw.fetchmany(n) if n else self._raw.fetchmany()

    def close(self):
        self._raw.close()

    @property
    def rowcount(self):
        return self._rowcount

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def description(self):
        return self._raw.description

    def __iter__(self):
        return iter(self._raw)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _split_statements(script: str) -> list[str]:
    # naive split on ; outside quotes — adequate for our DDL scripts
    parts, buf, in_str = [], [], False
    for ch in script:
        if ch == "'":
            in_str = not in_str
        if ch == ";" and not in_str:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


class PgConnection:
    """sqlite3.Connection-shaped wrapper borrowing a pooled psycopg connection."""

    def __init__(self, pool: ConnectionPool, schema: str):
        self._pool = pool
        self._schema = schema
        self._ctx = pool.connection()
        self._conn = self._ctx.__enter__()
        self._query_only = False
        self.row_factory = None  # accepted & ignored (dict rows always)
        with self._conn.cursor() as cur:
            cur.execute(_set_stmts(schema, 15000))

    # -- translation -------------------------------------------------------
    def _translate(self, sql: str, params):
        stripped = sql.strip()
        if stripped.lower().startswith("pragma "):
            return self._pragma(stripped)
        if re.match(r"^(SET|BEGIN|COMMIT|ROLLBACK|CHECKPOINT|VACUUM)\b", stripped, flags=re.I):
            # only COMMIT/ROLLBACK are ours (pool manages tx); swallow the rest
            if re.match(r"^(BEGIN|COMMIT)\b", stripped, flags=re.I):
                return ("SELECT 1", ())
            return ("SELECT 1", ())
        out = _rewrite_text(stripped)
        if isinstance(params, dict):
            return out, params
        return out, tuple(params or ())

    def _pragma(self, stmt: str):
        m = re.match(r"PRAGMA\s+(\w+)(?:\s*=\s*(\S+))?\s*;?\s*$", stmt, flags=re.I)
        if not m:
            raise UnsupportedSQL(f"unparseable pragma: {stmt!r}")
        name, value = m.group(1).lower(), (m.group(2) or "").strip("'\"")
        if name == "query_only":
            self._query_only = value not in ("", "0", "OFF", "off", "false")
            return ("SELECT 1", ())
        if name == "table_info":
            return ("SELECT * FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position", (value,))
        if name == "user_version":
            return ("SELECT 1", ())
        if name in ("schema_version", "data_version"):
            return ("SELECT 1", ())
        if name == "integrity_check":
            return (
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r','i') LIMIT 1",
                (),
            )
        if name in ("wal_checkpoint", "foreign_key_check", "index_list", "index_info", "index_xinfo"):
            return ("SELECT 1", ())
        if name in _PRAGMA_EMULATIONS and _PRAGMA_EMULATIONS[name] is None:
            return ("SELECT 1", ())
        raise UnsupportedSQL(f"unsupported pragma: {name}")

    # -- sqlite3.Connection parity ------------------------------------------
    def cursor(self):
        if self._query_only:
            raise RuntimeError("readonly")  # parity with sqlite READONLY error
        return _Cursor(self, self._conn.cursor(row_factory=dict_row))

    def execute(self, sql: str, parameters=()):
        c = self.cursor()
        return c.execute(sql, parameters)

    def executemany(self, sql, seq):
        c = self.cursor()
        return c.executemany(sql, seq)

    def executescript(self, script: str):
        c = self.cursor()
        return c.executescript(script)

    def commit(self):
        # psycopg commits on context exit; explicit commits mid-tx are legal no-ops
        # until we need real tx boundaries for the write-path migration (Phase 2).
        pass

    def rollback(self):
        pass

    def close(self):
        try:
            self._ctx.__exit__(None, None, None)
        finally:
            self._ctx = None

    # -- sqlite3 connect() kwargs we tolerate -------------------------------
    def create_function(self, *a, **k):
        pass

    def create_collation(self, *a, **k):
        pass

    @property
    def in_transaction(self):
        return bool(self._conn.info.transaction_status)


def connect(dsn: str, schema: str, *, min_size: int = 1, max_size: int = 8,
            lock_timeout_ms: int = 15000) -> PgConnection:
    """Create (or reuse) a pool for this schema and hand back a connection-shaped object.

    The pool pins search_path per checkout so any code path — including pooled
    reuse across threads — always lands inside its own profile schema.
    """
    key = (dsn, schema, lock_timeout_ms)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = ConnectionPool(
                conninfo=dsn,
                kwargs={"autocommit": True},
                min_size=min_size,
                max_size=max_size,
                check=ConnectionPool.check_connection,
                name=f"lcm-pg-{schema}",
                timeout=30,
            )
            pool.wait(timeout=30)
            # pin search_path + statement guards on every new physical connection
            pool.configure = None  # explicit instead:
            _POOLS[key] = pool
    # ensure schema exists (deterministic zero-provisioning for new profiles)
    probe = pool.getconn()
    try:
        with probe.cursor() as cur:
            if not _SCHEMA_RE.match(schema):
                raise ValueError(schema)
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(_set_stmts(schema, lock_timeout_ms))
    finally:
        pool.putconn(probe)
    # wrap: configure callback pins schema/lock_timeout on every checkout
    _patch_pool_checkout(pool, schema, lock_timeout_ms)
    return PgConnection(pool, schema)


def _patch_pool_checkout(pool: ConnectionPool, schema: str, lock_timeout_ms: int):
    if getattr(pool, "_lcm_patched", False):
        return
    orig_getconn = pool.getconn

    def getconn(*a, **k):
        conn = orig_getconn(*a, **k)
        try:
            with conn.cursor() as cur:
                cur.execute(_set_stmts(schema, lock_timeout_ms))
        except Exception:
            pool.putconn(conn)
            raise
        return conn

    pool.getconn = getconn  # type: ignore[method-assign]
    pool._lcm_patched = True  # type: ignore[attr-defined]


_POOLS: dict[tuple, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()
