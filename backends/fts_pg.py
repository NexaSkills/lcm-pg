"""FTS translation for the lcm-pg dual-backend fork.

Two jobs:
1. ensure_fts(conn, spec) — create PG trigger-maintained tsvector columns that
   mirror SQLite external-content FTS5 tables (same spec objects upstream
   builds in store.py/dag.py).
2. rewrite_fts_sql(sql) — convert `"col" MATCH 'query'` into
   `col @@ to_tsvector('english', normalize_fts_query('query'))` at runtime,
   so upstream MATCH SQL keeps working on Postgres.

FTS is intentionally a FALLBACK path on Postgres: tsvector ranking will not be
bit-identical to BM25. The shim never claims otherwise — callers degrade to
their existing Python-side fallback ranking when FTS raises.
"""

from __future__ import annotations

import re

import psycopg

# tsvector column added per FTS table spec (kept out of upstream column lists)
_TS_SUFFIX = "_tsv"

_MATCH_RE = re.compile(
    r'(?P<col>"?(?P<colname>[\w]+)"?)\s+MATCH\s+(?P<q>\'(?:[^\']|\'\')*\'|%s)',
    re.IGNORECASE,
)


def _normalize_query(q: str) -> str:
    """Convert an FTS5 MATCH query into an tsquery-compatible string.

    Strips FTS5-only operators; keeps OR explicitly (it is valid tsquery);
    collapses NEAR/^/*/"..." to plain OR-joined lexemes. Plainto then
    tolerates the residue.
    """
    s = q.strip()
    s = re.sub(r'^"(.*)"$', r"\1", s)          # "phrase" -> phrase
    s = s.replace("*", " ")                     # prefix -> plain token (plainto)
    s = re.sub(r"\bNEAR\s*/\s*\d+\b", " OR ", s, flags=re.I)
    s = re.sub(r"[^0-9A-Za-z_]+", " ", s)       # punctuation collapse
    s = re.sub(r"\s+", " ", s).strip()
    # words without explicit connectors: tsquery ANDs adjacent words by
    # default, FTS5 ORs in our callers' semantics; make it explicit
    toks = s.split()
    out, prev_word = [], False
    for t in toks:
        if t.upper() == "OR":
            out.append("OR")
            prev_word = False
        else:
            if prev_word:
                out.append("OR")
            out.append(t.lower())
            prev_word = True
    return " ".join(out)


def normalize_expr_sql(inner: str) -> str:
    """SQL expression: normalize_fts_query(<text>) — deterministic, in-DB."""
    return f"lcm_normalize_fts({inner})"


def ensure_helper(conn) -> None:
    """Create the normalize function + tsvector triggers idempotently."""
    conn.execute(
        """
        CREATE OR REPLACE FUNCTION lcm_normalize_fts(q text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE AS $fn$
        DECLARE s text;
        BEGIN
            s := coalesce(q, '');
            s := regexp_replace(s, '^"(.*[^"])"$', '\\1', 'g');
            s := regexp_replace(s, '\\*', ' ', 'g');
            s := regexp_replace(s, '(?i)\\bNEAR\\s*/\\s*[0-9]+\\b', ' or ', 'g');
            s := regexp_replace(s, '[^0-9A-Za-z_]+', ' ', 'g');
            s := regexp_replace(s, '\\s+', ' ', 'g');
            s := btrim(s);
            -- lowercase FIRST so connectors inserted below are never
            -- duplicated by the global lexeme-join pass.
            s := lower(s);
            -- insert explicit or between adjacent lexemes; the lookahead
            -- skips pairs already joined by an explicit or
            s := regexp_replace(s, '([0-9A-Za-z_]+) +([0-9A-Za-z_]+)(?![^ ]*\\bor\\b)',
                                '\\1 or \\2', 'g');
            -- collapse doubled connectors ("a or or b" is invalid tsquery)
            s := regexp_replace(s, '\\m(or(\\s+or)+)\\M', 'or', 'g');
            s := regexp_replace(s, '^(or\\s+|\\s+or$)', '', 'g');
            RETURN btrim(s);
        END $fn$;
        """
    )


def ts_colname(indexed_column: str) -> str:
    return indexed_column + _TS_SUFFIX


def ensure_fts(conn, table: str, indexed_column: str) -> None:
    """Idempotent tsvector column + triggers for one (table, column) pair."""
    ensure_helper(conn)
    tcol = ts_colname(indexed_column)
    conn.execute(
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{tcol}" tsvector'
    )
    conn.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_{table}_{tcol}') THEN
                CREATE INDEX idx_{table}_{tcol} ON "{table}" USING gin ("{tcol}");
            END IF;
        END $$;
        """
    )
    conn.execute(
        f"""
        CREATE OR REPLACE FUNCTION "{table}_{indexed_column}_tsv_update"() RETURNS trigger AS $$
        BEGIN
            NEW."{tcol}" := to_tsvector('english',
                coalesce(NEW."{indexed_column}"::text, 'lcm'));
            RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """
    )
    conn.execute(
        f"""
        DROP TRIGGER IF EXISTS "{table}_{indexed_column}_tsv" ON "{table}";
        CREATE TRIGGER "{table}_{indexed_column}_tsv"
            BEFORE INSERT OR UPDATE OF "{indexed_column}" ON "{table}"
            FOR EACH ROW EXECUTE FUNCTION "{table}_{indexed_column}_tsv_update"();
        """
    )
    # backfill rows that predate the trigger
    conn.execute(
        f'UPDATE "{table}" SET "{tcol}" = to_tsvector(\'english\', coalesce("{indexed_column}"::text, \'lcm\')) '
        f'WHERE "{tcol}" IS NULL'
    )


def has_fts(conn, table: str, indexed_column: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                (table, ts_colname(indexed_column)),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def rewrite_fts_sql(sql: str) -> str:
    """'"col" MATCH <expr>' -> tsvector match against the trigger column."""
    def repl(m: re.Match) -> str:
        col = m.group("colname")
        q = m.group("q")
        tcol = ts_colname(col)
        if q == "%s":  # bound param: normalize in SQL — impossible without the
            # value; use plainto with the raw query (FTS5 syntax subset that
            # tsquery accepts passes through; otherwise rows=0, caller falls back)
            return (
                f'"{tcol}" @@ plainto_tsquery(\'english\', lcm_normalize_fts({q}))'
            )
        # literal query string (may contain doubled quotes)
        return (
            f'"{tcol}" @@ to_tsquery(\'english\', lcm_normalize_fts({q}))'
        )

    return _MATCH_RE.sub(repl, sql)
