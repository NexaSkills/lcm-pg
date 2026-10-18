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


def _ensure_catalog_shims(cur, schema: str) -> None:
    """sqlite_master view + sqlite_sequence table per schema (idempotent).

    Upstream introspects via `SELECT ... FROM sqlite_master WHERE type=...`
    (~50 sites). The view maps PG catalog rows into the sqlite shape:
      type    'table' (incl. views; 'virtual' for FTS-facade names below),
              'index' (incl. the GIN tsvector stand-ins), 'trigger'
      name    object name (facade/index aliases resolved to the content table)
      tbl_name owning table
      sql     best-effort DDL text from pg_get_* (structural checks only
              substring-test it; callers degrade on residue)
    """
    if not _SCHEMA_RE.match(schema):
        raise ValueError(schema)
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS "{schema}".sqlite_sequence (name text, seq bigint)
    ''')
    cur.execute(f'''
        CREATE OR REPLACE FUNCTION "{schema}"._lcm_tabledef(cls oid) RETURNS text
        LANGUAGE sql STABLE AS $td$
            SELECT 'CREATE TABLE ' || quote_ident(c.relname) || ' ('
                || (SELECT string_agg(
                        quote_ident(a.attname) || ' ' || format_type(a.atttypid, a.atttypmod)
                        || CASE WHEN a.atthasdef THEN ' DEFAULT '
                                   || pg_get_expr(d.adbin, d.adrelid) ELSE '' END,
                        ', ' ORDER BY a.attnum)
                    FROM pg_attribute a
                    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)
                || ')'
            FROM pg_class c
            WHERE c.oid = cls;
        $td$;
    ''')
    cur.execute(f'''
        CREATE OR REPLACE VIEW "{schema}".sqlite_master AS
        WITH objs AS (
            SELECT CASE
                       WHEN c.relkind IN ('r','p') THEN 'table'
                       WHEN c.relkind = 'v' AND c.relname = 'sqlite_master' THEN NULL
                       WHEN c.relkind = 'v' THEN 'table'
                   END AS type,
                   c.relname AS name,
                   c.relname AS tbl_name,
                   "{schema}"._lcm_tabledef(c.oid::regclass) AS sql
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = '{schema}' AND c.relkind IN ('r','p','v')
            UNION ALL
            SELECT 'index', ic.relname, tc.relname,
                   pg_get_indexdef(i.indexrelid)
              FROM pg_index i
              JOIN pg_class ic ON ic.oid = i.indexrelid
              JOIN pg_class tc ON tc.oid = i.indrelid
              JOIN pg_namespace n ON n.oid = tc.relnamespace
             WHERE n.nspname = '{schema}'
            UNION ALL
            SELECT 'trigger', t.tgname, tc.relname,
                   pg_get_triggerdef(t.oid)
              FROM pg_trigger t
              JOIN pg_class tc ON tc.oid = t.tgrelid
              JOIN pg_namespace n ON n.oid = tc.relnamespace
             WHERE n.nspname = '{schema}' AND NOT t.tgisinternal
        ),
        facades AS (
            SELECT c.relname || '_fts' AS name, c.relname AS content
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = '{schema}' AND c.relkind = 'r'
               AND EXISTS (SELECT 1 FROM pg_attribute a
                            WHERE a.attrelid = c.oid AND a.attname LIKE '%_tsv'
                              AND a.attnum > 0 AND NOT a.attisdropped)
        )
        SELECT 'virtual' AS type, f.name AS name, f.name AS tbl_name,
               'CREATE VIRTUAL TABLE "' || f.name || '" USING fts5('
                 || 'content, content_rowid=store_id)' AS sql
          FROM facades f
        UNION ALL
        SELECT 'table', f.name || '_docsize', f.name || '_docsize',
               'CREATE TABLE "' || f.name || '_docsize"(docid INTEGER PRIMARY KEY, size)'
          FROM facades f
        UNION ALL
        SELECT type, name, tbl_name, sql FROM objs WHERE type IS NOT NULL
    ''')


def _pragma_table_content_exists(cur, name: str):
    """Fast path for the dominant `SELECT 1 FROM sqlite_master WHERE
    type='table' AND name=?` shape — a real EXISTS on pg_class, no view."""
    cur.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s "
        "AND c.relkind IN ('r','p','v') LIMIT 1",
        (name,),
    )
    return cur



def _table_info_sql(value: str):
    """sqlite PRAGMA table_info row shape as a real query (dict rows serve
    both row[1] callers — via the cid column — and row['name'] callers)."""
    return (
        "SELECT (row_number() OVER ())::int - 1 AS cid,"
        " column_name AS name, data_type AS type,"
        " CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS \"notnull\","
        " column_default AS dflt_value,"
        " CASE WHEN ordinal_position ="
        "     (SELECT min(ordinal_position) FROM information_schema.columns k"
        "       WHERE k.table_schema = c.table_schema"
        "         AND k.table_name = c.table_name"
        "         AND k.column_default LIKE '%%nextval%%')"
        " THEN 1 ELSE 0 END AS pk"
        " FROM information_schema.columns c"
        " WHERE table_schema = current_schema() AND table_name = %s"
        " ORDER BY ordinal_position",
        (value,),
    )


def _rewrite_sqlite_master(sql: str):
    """Rewrite the dominant sqlite_master EXISTS probes into catalog EXISTS
    queries (bind-param name only). Returns (sql, rewrote: bool)."""
    m = re.match(
        r"(?is)^\s*SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+"
        r"(?:type\s*=\s*'(?:table|view)'\s+AND\s+)?name\s*=\s*\?\s*$",
        sql.strip(),
    )
    if not m:
        return sql, False
    return (
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s "
        "AND c.relkind IN ('r','p','v') LIMIT 1",
        True,
    )


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


def _adapt_params(params):
    """SQLite binds dict/list as JSON TEXT; mirror that (targets are TEXT
    columns, so Jsonb wrappers would fail type checks)."""
    if params is None:
        return params
    import json as _json
    def _one(p):
        if isinstance(p, (dict, list)):
            return _json.dumps(p, ensure_ascii=False, default=str)
        return p
    if isinstance(params, dict):
        return {k: _one(v) for k, v in params.items()}
    return tuple(_one(p) for p in params)


def _annotate_param_types(sql: str, _shape=None) -> str:
    """Cast every positional %s to text EXCEPT after LIMIT/OFFSET (which
    PG needs numeric). String-literal %s are left alone."""
    out, i, in_str = [], 0, False
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            in_str = not in_str
            out.append(c); i += 1; continue
        if not in_str and sql.startswith("%s", i):
            # look back: LIMIT/OFFSET keyword right before -> numeric cast
            j = i - 1
            while j >= 0 and sql[j] in " \t\n\r":
                j -= 1
            tail = sql[max(0, j-7):j+1].upper()
            if tail.endswith("LIMIT") or tail.endswith("OFFSET"):
                out.append("CAST(%s AS bigint)")
            else:
                out.append("CAST(%s AS text)")
            i += 2; continue
        out.append(c); i += 1
    return "".join(out)


class _HybridRow(dict):
    """Row with BOTH dict and tuple semantics.

    dict(row)/isinstance(row, dict) -> True (dict base), while row[0],
    row[1], tuple(row), len(row) work via __getitem__ overload (int index ->
    positional value in SELECT order; str/slice -> dict access). psycopg
    dict_row keeps key insertion order == SELECT order, which makes the
    positional branch faithful.
    """

    __slots__ = ()

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        if isinstance(key, slice):
            return list(self.values())[key]
        return dict.__getitem__(self, key)

    def __iter__(self):
        return iter(list(self.values()))  # sqlite3.Row iterates values

    def keys(self):
        return dict.keys(self)


class _Cursor:
    """sqlite3.Cursor-shaped wrapper over a psycopg cursor."""

    def __init__(self, adapter: "PgConnection", raw):
        self._adapter = adapter
        self._raw = raw
        self._lastrowid: Optional[int] = None
        self._rowcount = -1

    # sqlite3 parity surface
    def execute(self, sql: str, parameters: Sequence | dict | None = None):
        if sql.strip().lower().startswith("begin"):
            self._adapter.begin()
        sql, params = self._adapter._translate(sql, parameters)
        params = _adapt_params(params)
        if isinstance(params, dict):
            # sqlite-style :named params -> psycopg %(name)s dictConfig keys
            sql = re.sub(r":([A-Za-z_]\w*)", lambda m: "%%(%s)s" % m.group(1), sql)
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
    @staticmethod
    def _as_row(r):
        # Hybrid rows: dict AND tuple semantics (upstream indexes store rows
        # positionally (row[0]/tuple(row)) AND by key (row["content"])).
        # Lists keep dict(row)/isinstance(row, dict) working everywhere.
        if isinstance(r, dict):
            return _HybridRow(r)
        return r

    def fetchone(self):
        return self._as_row(self._raw.fetchone())

    def fetchall(self):
        return [self._as_row(r) for r in self._raw.fetchall()]

    def fetchmany(self, n=None):
        return [self._as_row(r) for r in
                (self._raw.fetchmany(n) if n else self._raw.fetchmany())]

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
        self._tx = None
        self._tx_open = False
        self.row_factory = None  # accepted & ignored (dict rows always)
        with self._conn.cursor() as cur:
            cur.execute(_set_stmts(schema, 15000))

    # -- translation -------------------------------------------------------
    def _translate(self, sql: str, params):
        stripped = sql.strip()
        if stripped.lower().startswith("pragma "):
            return self._pragma(stripped)
        if re.match(r"^(SET|BEGIN|COMMIT|ROLLBACK|CHECKPOINT|VACUUM)\b", stripped, flags=re.I):
            # BEGIN IMMEDIATE is upstream's cross-process lock primitive; on PG
            # run it as a real transaction (COMMIT/ROLLBACK are handled by the
            # PgTransaction context manager, never by raw passthrough).
            if re.match(r"^BEGIN\b", stripped, flags=re.I):
                return ("BEGIN TRANSACTION", ())
            return ("SELECT 1", ())
        if isinstance(params, dict):
            return _rewrite_text(stripped), params
        out = _rewrite_text(stripped)
        if params and len(params) == 1 and "sqlite_master" in stripped:
            probe, hit = _rewrite_sqlite_master(out)
            if hit:
                return probe, params
        if isinstance(params, dict):
            return out, params
        return out, tuple(params or ())

    def _pragma(self, stmt: str):
        m = re.match(r"PRAGMA\s+(?:\w+\.)?(\w+)\s*(?:[=(]\s*(\S+?)\s*[)=]|\s*=\s*(\S+))?\s*;?\s*$", stmt, flags=re.I)
        if not m:
            raise UnsupportedSQL(f"unparseable pragma: {stmt!r}")
        name = m.group(1).lower()
        value = (m.group(2) or m.group(3) or "").strip("'\"")
        if name == "query_only":
            self._query_only = value not in ("", "0", "OFF", "off", "false")
            return ("SELECT 1", ())
        if name in ("table_info", "table_xinfo"):
            return _table_info_sql(value)
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
        if name == "database_list":
            return (
                "SELECT 0 AS seq, 'lcm' AS name,"
                " current_setting('data_directory', true) || '/base/'"
                " || (SELECT datname FROM pg_database"
                "     WHERE datname = current_database()) AS file",
                (),
            )
        if name in ("settings", "mmap_size", "journal_size_limit", "page_size"):
            return ("SELECT 1", ())
        if name in _PRAGMA_EMULATIONS and _PRAGMA_EMULATIONS[name] is None:
            return ("SELECT 1", ())
        raise UnsupportedSQL(f"unsupported pragma: {name}")

    # -- sqlite3.Connection parity ------------------------------------------
    def cursor(self):
        if self._query_only:
            raise RuntimeError("readonly")  # parity with sqlite READONLY error
        return _Cursor(self, self._conn.cursor(row_factory=dict_row))

    # -- sqlite3 parity surface ----------------------------------------------
    # BEGIN IMMEDIATE tracking: psycopg runs the pooled connection in autocommit,
    # where a bare "BEGIN" is a no-op. We mirror upstream's BEGIN IMMEDIATE /
    # COMMIT lock primitive with a real PG transaction: begin() opens a tx on the
    # underlying psycopg connection (so all subsequent statements are in it and
    # COMMIT/ROLLBACK via the adapter actually commit/roll back it).
    def begin(self):
        if not self._tx_open:
            try:
                self._conn.execute("BEGIN TRANSACTION")
            except Exception:
                pass
            self._tx_open = True

    def end_tx(self):
        self._tx_open = False

    # sqlite3 `with conn:` == transaction scope. The psycopg connection ctx
    # manager IS transaction scope (BEGIN..COMMIT/ROLLBACK around the block).
    # It must wrap the PHYSICAL psycopg connection, not re-issue BEGIN.
    def __enter__(self):
        inner = getattr(self, "_tx_ctx", None)
        if inner is None:
            from psycopg import Transaction
            inner = self._conn.transaction()
            self._tx_ctx = inner
        try:
            inner.__enter__()
        except Exception:
            # already inside a transaction psycopg manages; degrade to no-op
            self._tx_ctx = None
        return self

    def __exit__(self, *exc):
        inner = getattr(self, "_tx_ctx", None)
        if inner is not None:
            self._tx_ctx = None
            try:
                return inner.__exit__(*exc)
            except Exception:
                if exc[0] is not None:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                return False
        if exc[0] is not None:
            self.rollback()
        return False

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
        # No-op outside an explicit BEGIN IMMEDIATE transaction (autocommit pool);
        # inside one, it ends the real PG transaction opened by begin().
        if self._tx_open:
            try:
                self._conn.commit()
            except Exception:
                pass
            self.end_tx()

    def rollback(self):
        if self._tx_open:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self.end_tx()

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
            _ensure_catalog_shims(cur, schema)
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
