"""sqlite_master shim DDL v2 — sqlite-shaped DDL reconstruction.

LCM's rollup schema verifier compares sqlite_master.sql TEXT against
sqlite-authored expectations (CHECK clauses, partial-index column lists +
WHERE predicates, trigger bodies). Echoing pg_get_* output fails those
comparisons, so object DDL is rebuilt into sqlite syntax from the PG
catalog here (plain strings; {schema} substituted via .format).

Installed per schema:
  _lcm_sqlite_default(text)  PG default expr -> sqlite text (epoch->strftime,
                             ::type casts stripped)
  _lcm_tabledef(oid)         CREATE TABLE w/ sqlite storage classes,
                             PRIMARY KEY AUTOINCREMENT for serial single-col
                             integer PKs, composite/non-serial PK clause,
                             CHECK clauses
  _lcm_indexdef(oid)         CREATE [UNIQUE] INDEX (col list, lowercase
                             partial predicate, casts stripped)
  _lcm_triggerdef(oid)       CREATE TRIGGER reverse-translated from the
                             shadow _trg_<name>() plpgsql function the
                             translator installed (NEW./OLD. -> new./old.,
                     LEAST/GREATEST -> MIN/MAX, RETURN line dropped)
"""

from __future__ import annotations

import re

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


DDL_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS "{schema}".sqlite_sequence (name text, seq bigint)""",
    # default-expression mapper ------------------------------------------------
    """CREATE OR REPLACE FUNCTION "{schema}"._lcm_sqlite_default(expr text)
       RETURNS text LANGUAGE sql STABLE AS $df$
         SELECT CASE
           WHEN expr ~* '\\yextract\\s*\\(\\s*epoch\\s+from\\s+now\\s*\\(\\s*\\)\\s*\\)'
             THEN 'strftime(''%s'',''now'')'
           WHEN expr ~* '\\ynow\\s*\\(\\s*\\)\\s*$'
             THEN 'strftime(''%s'',''now'')'
           ELSE regexp_replace(expr, '::[a-z_ ]+$', '', 'g')
         END
       $df$""",
    # table definition --------------------------------------------------------
    """CREATE OR REPLACE FUNCTION "{schema}"._lcm_tabledef(cls oid) RETURNS text
       LANGUAGE sql STABLE AS $td$
         SELECT 'CREATE TABLE ' || quote_ident(c.relname) || ' ('
           || coalesce((SELECT string_agg(
                 quote_ident(a.attname) || ' ' ||
                 CASE
                   WHEN a.atttypid IN ('integer'::regtype,'bigint'::regtype,
                                        'smallint'::regtype) THEN 'INTEGER'
                   WHEN a.atttypid IN ('real'::regtype,'double precision'::regtype,
                                        'numeric'::regtype) THEN 'REAL'
                   WHEN a.atttypid = 'bytea'::regtype THEN 'BLOB'
                   ELSE 'TEXT' END
                 || CASE
                      WHEN seq.f IS NOT NULL THEN ' PRIMARY KEY AUTOINCREMENT'
                      ELSE CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
                           || CASE WHEN d.adbin IS NULL THEN ''
                                WHEN pg_get_expr(d.adbin, d.adrelid)
                                  LIKE 'nextval%' THEN ''
                                ELSE ' DEFAULT ' ||
                                     "{schema}"._lcm_sqlite_default(
                                       pg_get_expr(d.adbin, d.adrelid))
                              END
                     END,
                 ', ' ORDER BY a.attnum)
               FROM pg_attribute a
               LEFT JOIN pg_attrdef d
                 ON d.adrelid = a.attrelid AND d.adnum = a.attnum
               LEFT JOIN LATERAL (
                 SELECT 1 AS f FROM pg_index i
                   JOIN LATERAL unnest(i.indkey::int[])
                     WITH ORDINALITY AS u(k, ord) ON TRUE
                  WHERE i.indrelid = a.attrelid AND i.indisprimary
                    AND u.k = a.attnum AND u.ord = 1
                    AND EXISTS (SELECT 1 FROM pg_attrdef d2
                                 WHERE d2.adrelid = a.attrelid
                                   AND d2.adnum = a.attnum
                                   AND pg_get_expr(d2.adbin, d2.adrelid)
                                       LIKE 'nextval%')
                  LIMIT 1) seq ON TRUE
               WHERE a.attrelid = c.oid AND a.attnum > 0
                 AND NOT a.attisdropped), '')
           || coalesce((SELECT
                 CASE WHEN i.indnatts = 1
                       AND (i.indkey::int[])[0] IN (
                            SELECT a9.attnum FROM pg_attribute a9
                             JOIN pg_attrdef d9
                               ON d9.adrelid = a9.attrelid AND d9.adnum = a9.attnum
                             WHERE a9.attrelid = c.oid
                               AND pg_get_expr(d9.adbin, d9.adrelid) LIKE 'nextval%'
                               AND a9.atttypid IN ('integer'::regtype,'bigint'::regtype))
                      THEN ''   -- already rendered inline as AUTOINCREMENT
                      ELSE ', PRIMARY KEY (' || (SELECT string_agg(
                             quote_ident(a3.attname), ',' ORDER BY u3.ord)
                           FROM unnest(i.indkey::int[])
                             WITH ORDINALITY AS u3(k, ord)
                           JOIN pg_attribute a3
                             ON a3.attrelid = i.indrelid AND a3.attnum = u3.k)
                           || ')' END
               FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary
               LIMIT 1), '')
           || coalesce((SELECT ', ' || string_agg(
                 'CHECK(' || replace(replace(replace(replace(
                    regexp_replace(
                        regexp_replace(
                          regexp_replace(
                            substring(pg_get_constraintdef(p.oid) from 8),
                            '\\(([a-z_0-9]+) >= ([0-9]+)\\) AND \\(([a-z_0-9]+) <= ([0-9]+)\\)',
                            '\\1 between \\2 and \\4', 'g'),
                          '\(([a-z_0-9]+) = (''[^'']*'')\)', '\\1 IN \\2', 'g'),
                        ' = ANY [(]ARRAY[[]', ' IN (', 'g'),
                        '::text', ''),
                        ']', ''),
                        '((', '('),
                        '))', ')')                   || ')',
                 ', ' ORDER BY p.oid)
               FROM pg_constraint p
              WHERE p.conrelid = c.oid AND p.contype = 'c'), '')
           || ')'
         FROM pg_class c WHERE c.oid = cls
       $td$""",
    # index definition --------------------------------------------------------
    """CREATE OR REPLACE FUNCTION "{schema}"._lcm_indexdef(idx oid) RETURNS text
       LANGUAGE sql STABLE AS $id$
         SELECT 'CREATE '
           || CASE WHEN i.indisunique THEN 'UNIQUE ' ELSE '' END
           || 'INDEX ' || quote_ident(ic.relname)
           || ' ON ' || quote_ident(t.relname)
           || '(' || coalesce((SELECT string_agg(
                  CASE WHEN i.indkey[x.ord - 1] = 0
                       THEN pg_get_expr(i.indexprs, i.indrelid)
                       ELSE quote_ident(a.attname) ||
                            CASE WHEN (i.indoption::int[])[x.ord - 1] & 3 = 3
                                     THEN ' DESC'
                                 WHEN (i.indoption::int[])[x.ord - 1] & 1 = 1
                                     THEN ' DESC'
                                 ELSE '' END
                       END, ', ' ORDER BY x.ord)
                FROM generate_series(1, i.indnatts) AS x(ord)
                LEFT JOIN pg_attribute a
                  ON a.attrelid = i.indrelid
                 AND a.attnum = i.indkey[x.ord - 1]), '')
           || ')'
           || CASE WHEN i.indpred IS NOT NULL
                   THEN ' WHERE ' || "{schema}"._lcm_pred(i.indpred, i.indrelid)
                   ELSE '' END
         FROM pg_index i
         JOIN pg_class ic ON ic.oid = i.indexrelid
         JOIN pg_class t  ON t.oid = i.indrelid
         WHERE i.indexrelid = idx
       $id$""",
    # trigger definition lives in backends/triggerdef_pl.py (plpgsql; the
    # SQL-language version could not strip the lcm-event sentinel).
)

VIEW = """CREATE OR REPLACE VIEW "{schema}".sqlite_master AS
WITH objs AS (
    SELECT CASE
               WHEN c.relkind IN ('r','p') THEN 'table'
               WHEN c.relkind = 'v' AND c.relname = 'sqlite_master' THEN NULL
               WHEN c.relkind = 'v' THEN 'table'
           END AS type,
           c.relname AS name,
           c.relname AS tbl_name,
           "{schema}"._lcm_tabledef(c.oid) AS sql
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = '{schema}' AND c.relkind IN ('r','p','v')
    UNION ALL
    SELECT 'index', ic.relname, tc.relname,
           "{schema}"._lcm_indexdef(i.indexrelid)
      FROM pg_index i
      JOIN pg_class ic ON ic.oid = i.indexrelid
      JOIN pg_class tc ON tc.oid = i.indrelid
      JOIN pg_namespace n ON n.oid = tc.relnamespace
     WHERE n.nspname = '{schema}'
    UNION ALL
    SELECT 'trigger', t.tgname, tc.relname,
           "{schema}"._lcm_triggerdef(t.oid)
      FROM pg_trigger t
      JOIN pg_class tc ON tc.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = tc.relnamespace
     WHERE n.nspname = '{schema}' AND NOT t.tgisinternal
       AND t.tgname NOT LIKE '\\_trg\\_%' ESCAPE '\\'
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
SELECT type, name, tbl_name, sql FROM objs WHERE type IS NOT NULL"""


# partial-index predicate -> sqlite text shape: pg renders '((a = b) and
# (c = any (array[''x''::text])))'; sqlite drops the per-comparison parens.
# A paren is kept only when it opens an IN-list (follows ' in '/' not in '/an
# identifier char); array tails (']'/'[') are dropped as a pair.
PREDFUNC = r'''CREATE OR REPLACE FUNCTION "{schema}"._lcm_pred(p pg_node_tree, t oid)
  RETURNS text LANGUAGE plpgsql STABLE AS $pp$
DECLARE
  raw text;
  out text := '';
  i int := 1;
  c1 text;
  keep_stack text := '';
  depth int := 0;
BEGIN
  raw := lower(pg_get_expr(p, t));
  raw := replace(raw, '::text', '');
  raw := replace(raw, '::integer', '');
  raw := replace(raw, '::bigint', '');
  raw := replace(raw, '::smallint', '');
  raw := replace(raw, ' = any (array[', ' in (');
  raw := replace(raw, ' <> all (array[', ' not in (');
  WHILE i <= length(raw) LOOP
    c1 := substring(raw from i for 1);
    IF c1 = '(' THEN
      IF right(out, 4) IN (' in ', 'not ') OR right(out, 1) ~ '[a-z0-9_]' THEN
        keep_stack := keep_stack || '1';
        out := out || '(';
      ELSE
        keep_stack := keep_stack || '0';
      END IF;
      depth := depth + 1;
    ELSIF c1 = '[' THEN
      keep_stack := keep_stack || '0';
      depth := depth + 1;
    ELSIF c1 = ']' THEN
      IF depth > 0 AND right(keep_stack, 1) = '1' THEN
        out := out || ')';
      END IF;
      keep_stack := left(keep_stack, length(keep_stack) - 1);
      depth := depth - 1;
    ELSIF c1 = ')' THEN
      IF depth > 0 AND right(keep_stack, 1) = '1' THEN
        out := out || ')';
      END IF;
      keep_stack := left(keep_stack, length(keep_stack) - 1);
      depth := depth - 1;
    ELSE
      out := out || c1;
    END IF;
    i := i + 1;
  END LOOP;
  RETURN btrim(replace(out, '  ', ' '));
END
$pp$'''


# sqlite's json_each(x) for text arrays (LCM uses json_each(source_ids) etc.)
# DROP first: CREATE OR REPLACE cannot change OUT-column types.
JSON_EACH = r"""DROP FUNCTION IF EXISTS "{schema}".json_each(text);
CREATE OR REPLACE FUNCTION "{schema}".json_each(x text)
  RETURNS TABLE(key text, value text, type text, atom jsonb)
  LANGUAGE sql IMMUTABLE AS $je$
    SELECT (n - 1)::text AS key,
           CASE WHEN jsonb_typeof(jv) = 'string'
                THEN trim(both '"' from jv #>> '{{}}')
                ELSE jv #>> '{{}}' END AS value,
           jsonb_typeof(jv) AS type,
           jv AS atom
    FROM jsonb_array_elements(x::jsonb) WITH ORDINALITY AS e(jv, n)
  $je$"""

# sqlite compares INTEGER to TEXT with affinity coercion; PG needs an
# explicit bigint=text operator for NOT-IN/IN over json_each.value.
BIGINT_TEXT_EQ = r"""DO $o$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_operator o
                  JOIN pg_namespace n ON n.oid = o.oprnamespace
                 WHERE n.nspname = 'public' AND o.oprname = '='
                   AND o.oprleft = 'int8'::regtype
                   AND o.oprright = 'text'::regtype) THEN
    EXECUTE 'CREATE OPERATOR public.= (LEFTARG = int8, RIGHTARG = text,
             FUNCTION = public._bigint_text_eq)';
  END IF;
END $o$"""

# sqlite BLOB affinity -> a 'blob' DOMAIN over bytea, so upstream DDL text
# (`vec BLOB NOT NULL`) runs unmodified on PG.
BLOB_DOMAIN = (
    'DO $d$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t'
    " JOIN pg_namespace n ON n.oid=t.typnamespace"
    " WHERE n.nspname = '{schema}' AND t.typname = 'blob' AND t.typtype = 'd')"
    ' THEN EXECUTE $$CREATE DOMAIN "{schema}".blob AS bytea$$;'
    " END IF; END $d$"
)

BIGINT_TEXT_FN = r"""CREATE OR REPLACE FUNCTION public._bigint_text_eq(a bigint, b text)
  RETURNS bool LANGUAGE sql IMMUTABLE AS $$ SELECT a::text OPERATOR(pg_catalog.=) b $$"""

def install(cur, schema: str) -> None:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(schema)
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    # helper functions that the sqlite_master view references at *definition*
    # time (_lcm_pred/_lcm_triggerdef) must exist before CREATE VIEW:
    shim = _ConnShim(cur)
    from . import triggerdef_pl
    triggerdef_pl.install(shim, schema)
    cur.execute(PREDFUNC.format(schema=schema))
    cur.execute(JSON_EACH.format(schema=schema))
    cur.execute(BLOB_DOMAIN.format(schema=schema))
    cur.execute(BIGINT_TEXT_FN)
    cur.execute(BIGINT_TEXT_EQ)
    for stmt in DDL_STATEMENTS:
        cur.execute(stmt.format(schema=schema))
    cur.execute(VIEW.format(schema=schema))
    # FTS facades: command.py's read-only scan does
    # `SELECT COUNT(*) FROM <fts_name>` where <fts_name> is a *literal*
    # table name — the shim's rewrite only fires for MATCH/qualifier
    # contexts, so a bare COUNT on the facade name reaches PG and 404s.
    # A view over the content table makes the name resolve for plain
    # SELECT/COUNT without touching the hot search path.
    for fts, content, col, pk in (
        ("messages_fts", "messages", "content", "store_id"),
        ("nodes_fts", "summary_nodes", "summary", "node_id"),
    ):
        # facade only when the content table exists in this schema
        if not cur.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname=%s AND c.relname=%s AND c.relkind='r'",
            (schema, content)).fetchone():
            continue
        cur.execute(
            f'CREATE OR REPLACE VIEW "{schema}"."{fts}" AS '
            f'SELECT "{col}" AS "{col}", "{pk}" AS rowid '
            f'FROM "{schema}"."{content}"'
        )


class _ConnShim:
    """Expose cursor.execute(sql) for helper installers written against
    connections (they never pass params, so this is sufficient)."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, *args, **kw):
        return self._cur.execute(sql, *args, **kw)
