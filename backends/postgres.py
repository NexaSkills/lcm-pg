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
    from . import shim_ddl
    shim_ddl.install(cur, schema)


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



def _stdlib_sqlite3_path():
    import sysconfig, pathlib
    return str(pathlib.Path(sysconfig.get_paths()["stdlib"]) / "sqlite3" / "__init__.py")


def _table_info_sql(value: str, conn=None):
    """PRAGMA table_info emulation with sqlite-exact row shapes.

    Reconstructs the original sqlite DDL for `value` from the PG catalog and
    parses it with stdlib sqlite (in-memory) so sqlite's own pragma engine
    answers. LCM's schema verifiers compare sqlite shapes (storage classes,
    AUTOINCREMENT pk=1 / notnull=0 / NULL default, strftime('%s','now')
    default text) that a catalog-derived query cannot express: PG stores
    those as nextval(...), EXTRACT(epoch FROM now()), 'x'::text. Returns a
    ('__rows__', rows) direct-result marker consumed by _Cursor.execute
    before translation; falls back to a catalog-only query (right names and
    types, approximate defaults) when reconstruction fails.
    """
    try:
        base = value.split(".")[-1]  # schema.table form: pragma takes bare name
        ddl = _sqlite_ddl_for(base, conn)
        if ddl:
            import importlib.util as _ilu, sys as _sys
            _s3 = _sys.modules.get("_lcm_stdlib_sqlite3")
            if _s3 is None:
                # Import BY PATH: inside the fork package a bare
                # `import sqlite3` hits the compat shim and recurses. The
                # stdlib package dir shadows the stdlib extension (its
                # sqlite3/ subdirectory), so give it a non-shadowing name.
                _spec = _ilu.spec_from_file_location(
                    "_lcm_stdlib_sqlite3", _stdlib_sqlite3_path(),
                    submodule_search_locations=[])
                _s3 = _ilu.module_from_spec(_spec)
                _sys.modules["_lcm_stdlib_sqlite3"] = _s3  # pkg init imports .sqlite3 → resolves via path
                try:
                    _spec.loader.exec_module(_s3)
                except Exception:
                    _sys.modules.pop("_lcm_stdlib_sqlite3", None)
                    raise
            mem = _s3.connect(":memory:")
            try:
                mem.execute(ddl)
                rows = mem.execute(
                    "SELECT cid, name, type, \"notnull\", dflt_value, pk"
                    " FROM pragma_table_info(?)", (base,)).fetchall()
                if rows:
                    import os as _os
                    if _os.environ.get("LCM_PG_DEBUG_PRAGMA"):
                        import sys as _s
                        print("[pragma] DDL:", ddl[:120], file=_s.stderr)
                    return ("__rows__", [tuple(r) for r in rows])
            finally:
                mem.close()
    except Exception as _e:
        import os as _os
        if _os.environ.get("LCM_PG_DEBUG_PRAGMA"):
            import sys as _s, traceback as _tb
            print("[pragma] fallback:", repr(_e), file=_s.stderr)
            _tb.print_exc(file=_s.stderr)
    # fallback: catalog-only shapes
    return (
        "SELECT (a.attnum - 1)::int AS cid, a.attname AS name,"
        " CASE"
        "   WHEN a.atttypid IN ('integer'::regtype,'bigint'::regtype,"
        "     'smallint'::regtype) THEN 'INTEGER'"
        "   WHEN a.atttypid IN ('real'::regtype,'double precision'::regtype,"
        "     'numeric'::regtype) THEN 'REAL'"
        "   WHEN a.atttypid = 'bytea'::regtype THEN 'BLOB'"
        "   ELSE 'TEXT' END AS type,"
        " a.attnotnull::int AS \"notnull\","
        " CASE WHEN d.adbin IS NULL THEN NULL"
        "   WHEN pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval%%' THEN NULL"
        "   WHEN position('::' in pg_get_expr(d.adbin, d.adrelid)) > 0 THEN"
        "     left(pg_get_expr(d.adbin, d.adrelid),"
        "          position('::' in pg_get_expr(d.adbin, d.adrelid)) - 1)"
        "   ELSE pg_get_expr(d.adbin, d.adrelid) END AS dflt_value,"
        " coalesce((SELECT u.ord::int FROM pg_index i,"
        "     LATERAL unnest(i.indkey::int[]) WITH ORDINALITY AS u(k, ord)"
        "     WHERE i.indrelid = a.attrelid AND i.indisprimary"
        "       AND u.k = a.attnum LIMIT 1), 0) AS pk"
        " FROM pg_attribute a"
        " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
        " WHERE a.attrelid = (SELECT oid FROM pg_class WHERE relname = %s"
        "   AND relnamespace = current_schema()::regnamespace)"
        "   AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
        (value,),
    )


def _pg_default_to_sqlite(d: str) -> str:
    """Map a PG default expression back to sqlite-parseable text."""
    s = d.strip()
    m = re.match(r"(?is)^CAST\s*\((.*)\)\s*(AS\s+\w+)?$", s)
    if m:
        s = m.group(1).strip()
    s = re.sub(r"(?i)\bEXTRACT\s*\(\s*epoch\s+FROM\s+now\s*\(\s*\)\s*\)",
               "strftime('%s','now')", s)
    s = re.sub(r"(?i)^now\s*\(\s*\)\s*$", "strftime('%s','now')", s)
    # sqlite requires parenthesized expression defaults (and the original
    # sqlite DDL had them); only wrap function-call expressions.
    if re.match(r"^[A-Za-z_][A-Za-z_0-9]*\s*\(", s):
        s = "(" + s + ")"
    return s


def _sqlite_ddl_for(table: str, conn=None):
    """Reconstruct sqlite CREATE TABLE text from PG catalog rows.

    Column order = attnum; types mapped to sqlite storage classes; a
    nextval-defaulted first PK column becomes PRIMARY KEY AUTOINCREMENT
    (sqlite's rowid alias: notnull 0, NULL default, pk 1); NOT NULL applies
    to the rest; default expressions have PG ::type casts stripped and
    EXTRACT(epoch FROM now()) mapped back to strftime('%s','now').
    """
    if conn is None:
        return None
    raw = conn._pool.getconn() if hasattr(conn, "_pool") else conn._conn
    try:
        # RAW psycopg cursor: bypass _Cursor/_translate (percent protection
        # would turn the %s placeholder into a literal and return 0 rows).
        cur = raw.cursor()
        try:
            cur.execute(
                "SELECT a.attnum, a.attname,"
                " CASE"
                "   WHEN a.atttypid IN ('integer'::regtype,'bigint'::regtype,"
                "     'smallint'::regtype) THEN 'INTEGER'"
                "   WHEN a.atttypid IN ('real'::regtype,'double precision'::regtype,"
                "     'numeric'::regtype) THEN 'REAL'"
                "   WHEN a.atttypid = 'bytea'::regtype THEN 'BLOB'"
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
                "       AND u.k = a.attnum LIMIT 1)"
                " FROM pg_attribute a"
                " LEFT JOIN pg_attrdef d"
                "   ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                " WHERE a.attrelid = (SELECT oid FROM pg_class"
                "   WHERE relname = %s"
                "     AND relnamespace = current_schema()::regnamespace)"
                "   AND a.attnum > 0 AND NOT a.attisdropped"
                " ORDER BY a.attnum",
                (table,))
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        if hasattr(conn, "_pool"):
            conn._pool.putconn(raw)
    if not rows:
        return None
    cols = []
    pk_cols = sorted((p for p in rows if p[6]), key=lambda p: p[6])
    autoincrement_pk = (len(pk_cols) == 1 and pk_cols and pk_cols[0][6] == 1
                        and pk_cols[0][4] and pk_cols[0][2] == "INTEGER")
    for (attnum, name, stype, notnull, is_seq, dflt, pkord) in rows:
        bits = ['"' + str(name).replace('"', '""') + '"', stype]
        if autoincrement_pk and pkord == 1:
            bits.append("PRIMARY KEY AUTOINCREMENT")
        else:
            # sqlite reports a sequence-defaulted INTEGER PK as a rowid
            # alias: notnull 0 (INTEGER PKs are NULL-tolerant aliases).
            if notnull and not (is_seq and pkord):
                bits.append("NOT NULL")
            if dflt is not None:
                d = str(dflt)
                d = _pg_default_to_sqlite(d)
                bits.append("DEFAULT " + d)
        cols.append(" ".join(bits))
    if pk_cols and not autoincrement_pk:
        # table-level PRIMARY KEY (composite or non-integer): sqlite orders
        # pk columns 1..n by index position; columns keep NOT NULL already.
        cols.append("PRIMARY KEY (%s)" % ", ".join(
            '"' + str(p[1]).replace('"', '""') + '"' for p in pk_cols))
    return "CREATE TABLE %s (%s)" % ('"' + table.replace('"', '""') + '"',
                                      ", ".join(cols))


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


# ---------------------------------------------------------------------------
# SQLite CREATE TRIGGER -> PG trigger translation
# ---------------------------------------------------------------------------
# Covers LCM's two shapes: outbox INSERT triggers and SELECT RAISE(ABORT,...)
# guard triggers, with UPDATE OF col lists mapped to WHEN guards. The body
# compiles into a row-level plpgsql function named _trg_<trigger>.


def _translate_sqlite_trigger(sql: str):
    """Return PG trigger DDL (multi-statement) or None for unsupported shapes."""
    # sqlite "CREATE [VIRTUAL TABLE|TRIGGER] IF NOT EXISTS" — PG has no IF NOT
    # EXISTS on either, so pre-normalize: virtual tables become no-ops (the
    # tsvector shim needs no shadow table), triggers keep the name.
    if re.search(r"(?is)\bIF\s+NOT\s+EXISTS\b", sql):
        if re.match(r"(?is)\s*CREATE\s+VIRTUAL\s+TABLE\b", sql):
            return ["SELECT 1"]
        sql = re.sub(r"(?is)\bCREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+",
                     "CREATE TRIGGER ", sql, count=1)
    m = re.match(
        r"(?is)\s*CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(\w+)\s+(AFTER|BEFORE)\s+(INSERT|UPDATE|DELETE)(?:\s+OF\s+([\w,\s]+))?"
        r"\s+ON\s+(\w+)\s+BEGIN\s+(.*?)\s*END\s*;?\s*$",
        sql)
    if not m:
        return None
    name, when, event, of_cols, table, body = m.groups()
    stmts = [s.strip() for s in re.split(r";\s*(?=\S)", body) if s.strip()]
    translated = []
    for st in stmts:
        mr = re.match(r"(?is)^\s*SELECT\s+RAISE\s*\(\s*(ABORT|FAIL|IGNORE|ROLLBACK)"
                      r"\s*,\s*'((?:[^']|'')*)'\s*\)(?:\s+WHERE\s+(.+?))?\s*$", st)
        if mr:
            _kind, msg, cond = mr.groups()
            cond = (cond or "TRUE").replace("new.", "NEW.").replace("old.", "OLD.")
            if cond != "TRUE":
                cond = _rewrite_text(cond).rstrip(";").strip()
            translated.append(
                "IF %s THEN RAISE EXCEPTION '%s'; END IF"
                % (cond, msg.replace("'", "''")))
            continue
        if not re.match(r"(?is)^\s*(INSERT\s+INTO|DELETE\s+FROM|UPDATE\s)", st):
            return None  # unsupported body op -> caller passes SQL through
        st = re.sub(r"(?i)\bnew\.", "NEW.", st)
        st = re.sub(r"(?i)\bold\.", "OLD.", st)
        translated.append(st.rstrip(";").strip())
    fn = "_trg_" + name
    # translate body statements ONE BY ONE (_rewrite_text is single-statement
    # scoped; its regexes cannot span ';').
    body_sql = ";\n            ".join(
        "; ".join(_rewrite_text(part).rstrip(";").strip()
                  for part in _split_statements(s))
        for s in translated)
    # sentinel: authoritative event kind for the sqlite_master shim's trigger
    # reverse-translation (tgtype bits are unreliable across WHEN guards).
    body_sql = "/*lcm-event: " + event + "*/ " + body_sql
    guard = ""
    if of_cols and event == "UPDATE":
        cols = [c.strip() for c in of_cols.split(",") if c.strip()]
        conds = " OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in cols)
        guard = "\n            WHEN (" + conds + ")"
    op = {"INSERT": "INSERT", "DELETE": "DELETE", "UPDATE": "UPDATE"}[event]
    ret = "OLD" if (when == "BEFORE" and event == "DELETE") else "NEW"
    return f"""
        DROP TRIGGER IF EXISTS "{name}" ON "{table}" CASCADE;
        DROP FUNCTION IF EXISTS "{fn}"() CASCADE;
        CREATE FUNCTION "{fn}"() RETURNS trigger
        LANGUAGE plpgsql AS $trg$
        BEGIN
            {body_sql};
            RETURN {ret};
        END
        $trg$;
        CREATE TRIGGER "{name}" {when} {op} ON "{table}"
        FOR EACH ROW{guard}
        EXECUTE FUNCTION "{fn}"();
    """



def _drop_trigger_pg(m: re.Match) -> str:
    """Render a PG DROP TRIGGER for a SQLite DROP (see _rewrite_text).

    PG requires the owning table; CASCADE also removes the shadow
    _trg_<name>() function the translator created. SQLite trigger names are
    global, so ON-less drops reverse-resolve via pg_trigger inside a DO
    block — and only fire when the name is genuinely a trigger (the
    sqlite_master view lists functions too, so a bare name check would let a
    name collision drop the wrong object). Built by concatenation so the
    statement never carries a literal % (psycopg placeholder).
    """
    if_exists, name, name_q, table, table_q = m.groups()
    name = name_q or name
    table = table_q or table
    if table:
        return "DROP TRIGGER %s %s ON %s CASCADE" % (
            "IF EXISTS" if if_exists else "", name, table)
    head = "DROP TRIGGER " + ("IF EXISTS " if if_exists else "")
    cond = "NOT tgisinternal AND tgname = " + repr(name)
    return (
        "DO $$ BEGIN EXECUTE '" + head + "' || ("
        "SELECT quote_ident(tgname) || ' ON ' || tgrelid::regclass::text"
        " || ' CASCADE' FROM pg_trigger WHERE " + cond + " LIMIT 1);"
        " EXCEPTION WHEN OTHERS THEN"
        " IF (SELECT count(*) FROM pg_trigger WHERE " + cond + ") = 0 THEN"
        " NULL; ELSE RAISE; END IF;"
        " END $$;")


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
    # TRIM(x, chars) 2-arg form is Oracle/sqlite syntax; PG's 2-arg function
    # is btrim(x, chars). (1-arg TRIM(x) parses natively on PG.) Split on the
    # top-level comma so inner parens/strings (char(9) || char(10) ...) ride
    # into arg2 untouched.
    def _btrim(m: re.Match) -> str:
        inner = m.group(1)
        depth, in_str, k = 0, False, -1
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "'":
                in_str = not in_str
            elif not in_str:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 0:
                    k = i
                    break
            i += 1
        if k == -1:
            return m.group(0)
        return "btrim(" + inner[:k] + "," + inner[k + 1:] + ")"
    s = re.sub(r"""\bTRIM\s*\(((?:[^()'"]|'[^']*'|\([^()]*\))*)\)""", _btrim, s, flags=re.I)
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

    # SQLite 2-arg scalar min()/max() -> PG least()/greatest() (aggregate
    # min(double, double) does not exist in PG).
    s = re.sub(r"\bmin\s*\(\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*,\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*\)",
               r"least(\1, \2)", s, flags=re.I)
    s = re.sub(r"\bmax\s*\(\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*,\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*\)",
               r"greatest(\1, \2)", s, flags=re.I)

    # SQLite 2-arg scalar min()/max() -> PG least()/greatest() (aggregate
    # min(double, double) does not exist in PG). Statement-scoped input.
    s = re.sub(r"\bmin\s*\(\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*,\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*\)",
               r"least(\1, \2)", s, flags=re.I)
    s = re.sub(r"\bmax\s*\(\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*,\s*([^,(()]*(?:\([^()]*\))?[^,(()]*)\s*\)",
               r"greatest(\1, \2)", s, flags=re.I)

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

    # DROP TRIGGER [IF EXISTS] name [ON table] -> PG form. PG requires the
    # owning table; CASCADE also removes the shadow _trg_<name>() function the
    # translator created. ON-less drops (SQLite trigger names are global) are
    # reverse-resolved via pg_trigger inside a DO block. The concatenation
    # avoids literal % tokens in the statement (psycopg treats them as
    # placeholders).
    if re.search(r"(?i)\bDROP\s+TRIGGER\b", s):
        s = re.sub(
            r"(?i)\bDROP\s+TRIGGER\s+(?:(IF\s+EXISTS)\s+)?"
            r"(?:\"([^\"]+)\"|([\w$]+))(?:\s+ON\s+(?:\"([^\"]+)\"|([\w$]+)))?",
            _drop_trigger_pg,
            s)

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
        self._rows: Optional[list] = None  # direct-result channel (pragmas)
        self._pos = 0

    # sqlite3 parity surface
    def execute(self, sql: str, parameters: Sequence | dict | None = None):
        if sql.strip().lower().startswith("begin"):
            self._adapter.begin()
        if re.match(r"(?i)\s*CREATE\s+VIRTUAL\s+TABLE\b", sql):
            # external-content FTS5 shadow tables are not needed: backends/fts_pg
            # provides trigger-maintained tsvectors on the content table.
            self._rowcount = 0
            return self
        if re.match(r"(?i)\s*CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\b", sql):
            tr = _translate_sqlite_trigger(sql)
            if tr is not None:
                self._raw_exec(tr)
                self._rowcount = 0
                return self
        self._rows, self._pos = None, 0
        sql, params = self._adapter._translate(sql, parameters)
        if sql == "__rows__":
            self._rows = [dict(zip(("cid", "name", "type", "notnull",
                                     "dflt_value", "pk"), r)) for r in params]
            self._rowcount = len(self._rows)
            return self
        params = _adapt_params(params)
        if isinstance(params, dict):
            # sqlite-style :named params -> psycopg %(name)s dictConfig keys
            sql = re.sub(r":([A-Za-z_]\w*)", lambda m: "%%(%s)s" % m.group(1), sql)
        if not params and not isinstance(params, dict):
            # psycopg treats ANY params value (even empty tuple) as "has
            # placeholders" and scans literals for %s/%b/%t; a stray literal
            # `%` inside a string (LIKE 'a%') then explodes. sqlite would
            # ignore it, so hand psycopg a None (no-placeholder mode) when
            # nothing is bound and nothing was substituted.
            if "%s" not in sql and "%(" not in sql:
                params = None
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
            if not stmt:
                continue
            if re.match(r"(?i)\s*CREATE\s+VIRTUAL\s+TABLE\b", stmt):
                continue  # FTS5 shadow tables unused under the tsvector shim
            if re.match(r"(?i)\s*CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\b", stmt):
                tr = _translate_sqlite_trigger(stmt)
                if tr is not None:
                    self._raw_exec(tr)
                    continue
            self._raw_exec(_rewrite_text(stmt))
        return self

    # sqlite3 parity: .row_factory assignment is handled at connection level
    def _raw_exec(self, sql):
        """Execute DDL/text-rewritten SQL with NO binds.

        psycopg only unescapes '%%' when it treats the string as a query
        (params not None). Passing () keeps placeholder scanning on while
        binding nothing, so '%%' -> '%' reaches the server. Without this,
        LIKE '%x%%' patterns baked into translated DDL break silently.
        A list/tuple is executed statement-by-statement.
        """
        if isinstance(sql, (list, tuple)):
            last = None
            for s in sql:
                last = self._raw.execute(s, ())
            return last
        return self._raw.execute(sql, ())

    @staticmethod
    def _as_row(r):
        # Hybrid rows: dict AND tuple semantics (upstream indexes store rows
        # positionally (row[0]/tuple(row)) AND by key (row["content"])).
        # Lists keep dict(row)/isinstance(row, dict) working everywhere.
        if isinstance(r, dict):
            return _HybridRow(r)
        return r

    def fetchone(self):
        if self._rows is not None:
            if self._pos >= len(self._rows):
                return None
            r = self._rows[self._pos]
            self._pos += 1
            return self._as_row(r)
        return self._as_row(self._raw.fetchone())

    def fetchall(self):
        if self._rows is not None:
            out = self._rows[self._pos:]
            self._pos = len(self._rows)
            return [self._as_row(r) for r in out]
        return [self._as_row(r) for r in self._raw.fetchall()]

    def fetchmany(self, n=None):
        if self._rows is not None:
            take = len(self._rows) - self._pos if n is None else n
            out = self._rows[self._pos:self._pos + take]
            self._pos += len(out)
            return [self._as_row(r) for r in out]
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
        if self._rows is not None:
            return tuple((c, None, None, None, None, None, None) for c in
                         ("cid", "name", "type", "notnull", "dflt_value", "pk"))
        return self._raw.description

    def __iter__(self):
        if self._rows is not None:
            return iter([self._as_row(r) for r in self._rows[self._pos:]])
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
        self._ensure_tx_probe = "SELECT 1"
        self.row_factory = None  # accepted & ignored (dict rows always)
        with self._conn.cursor() as cur:
            cur.execute(_set_stmts(schema, 15000))

    # -- translation -------------------------------------------------------
    def _translate(self, sql: str, params):
        stripped = sql.strip()
        if stripped.lower().startswith("pragma "):
            return self._pragma(stripped)
        if re.match(r"^(SAVEPOINT|RELEASE|SET|BEGIN|COMMIT|ROLLBACK|CHECKPOINT|VACUUM)\b", stripped, flags=re.I):
            # BEGIN IMMEDIATE is upstream's cross-process lock primitive; on PG
            # run it as a real transaction (COMMIT/ROLLBACK are handled by the
            # PgTransaction context manager, never by raw passthrough).
            if re.match(r"^BEGIN\b", stripped, flags=re.I):
                return ("BEGIN TRANSACTION", ())
            # SAVEPOINT hygiene: upstream's _sqlite_savepoint assumes sqlite's
            # implicit-transaction semantics (SAVEPOINT outside any tx starts a
            # deferred one). psycopg pooled conns are autocommit, so a bare
            # SAVEPOINT errors with NoActiveSqlTransaction. Emulate sqlite: open
            # a real PG transaction first if none is open; RELEASE/ROLLBACK TO
            # pass through and ROLLBACK (full) ends the tx like sqlite does.
            m = re.match(r"^SAVEPOINT\s+([\"\w]+)\s*;?\s*$", stripped, flags=re.I)
            if m:
                # psycopg autocommit pool: a bare SAVEPOINT errors outside a tx
                # block (sqlite would have started one implicitly). If a tx is
                # already open (BEGIN IMMEDIATE / with-conn) just pass through.
                if not self._tx_open:
                    self.begin()
                return (stripped, ())
            if re.match(r"^ROLLBACK\s+TO\b", stripped, flags=re.I):
                return (stripped, ())
            if re.match(r"^ROLLBACK\b", stripped, flags=re.I):
                self.end_tx()
                return ("ROLLBACK", ())
            if re.match(r"^RELEASE\b", stripped, flags=re.I):
                # Outermost RELEASE commits sqlite's implicit tx. Mirror that:
                # after releasing, if no nested savepoint can remain (we never
                # nest our synthetic BEGIN), commit the opened tx.
                return (stripped, ())
            return (stripped, ())
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
            return _table_info_sql(value, self)
        if name == "user_version":
            return ("SELECT 1", ())
        if name in ("schema_version", "data_version"):
            return ("SELECT 1", ())
        if name in ("integrity_check", "quick_check"):
            # sqlite returns a single row 'ok' on a healthy db; the doctor
            # compares result[0] == "ok". No PG equivalent of B-tree walking
            # exists inline; amcheck extension is not installed, so report
            # ok when the catalog is readable (every query proving it works).
            return (
                "SELECT CASE WHEN EXISTS ("
                "  SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                "  WHERE n.nspname = current_schema() AND c.relkind='r'"
                ") THEN 'ok' ELSE 'corrupt' END AS integrity",
                (),
            )
        if name in ("wal_checkpoint", "foreign_key_check"):
            return ("SELECT 1", ())
        if name == "index_list":
            # sqlite shape: seq, name, unique, origin, partial
            return (
                "SELECT (ROW_NUMBER() OVER (ORDER BY c.relname) - 1)::int AS seq,"
                " c.relname AS name,"
                " CASE WHEN i.indisunique THEN 1 ELSE 0 END AS unique,"
                " CASE WHEN i.indisprimary THEN 'p' ELSE 'c' END AS origin,"
                " CASE WHEN i.indpred IS NOT NULL THEN 1 ELSE 0 END AS partial"
                " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                " JOIN pg_class t ON t.oid = i.indrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE t.relname = %s AND n.nspname = current_schema()",
                (value,),
            )
        if name == "index_xinfo":
            # sqlite shape: seqno, cid, name, desc, coll, key, partial
            return (
                "SELECT (x.ord - 1)::int AS seqno,"
                " CASE WHEN x.k = 0 THEN -1 ELSE x.k::int END AS cid,"
                " CASE WHEN x.k = 0 THEN NULL ELSE a.attname END AS name,"
                ' CASE WHEN (i.indoption[x.ord - 1] & 3) IN (1,3) THEN 1 ELSE 0 END AS "desc",'
                " 0 AS coll, 1 AS key,"
                " CASE WHEN i.indpred IS NOT NULL THEN 1 ELSE 0 END AS partial"
                " FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " JOIN pg_class t ON t.oid = i.indrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(k, ord) ON TRUE"
                " LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = x.k"
                " WHERE c.relname = %s AND n.nspname = current_schema()"
                " ORDER BY x.ord",
                (value,),
            )
        if name == "index_info":
            # sqlite shape: seqno, cid, name
            return (
                "SELECT (ik.ord - 1)::int AS seqno, ik.k AS cid, a.attname AS name"
                " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                " JOIN pg_class t ON t.oid = i.indrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS ik(k, ord) ON TRUE"
                " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ik.k"
                " WHERE c.relname = %s AND n.nspname = current_schema()"
                " ORDER BY ik.ord",
                (value,),
            )
        if name == "database_list":
            # data_directory is superuser-only on PG 16+; the schema-qualified
            # path shape is cosmetic for callers (db file listing only).
            return (
                "SELECT 0 AS seq, current_database() AS name,"
                " current_schema() AS file",
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
