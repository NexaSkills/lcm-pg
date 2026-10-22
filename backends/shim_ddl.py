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
    # CHECK-fold: pure text surgery in plpgsql (position/substr, NO regex,
    # NO backreferences, NO backslashes in the body). Replaces the old
    # regexp_replace backref chain that corrupted the shim's CHECK text.
    # _peel/_start are inlined here as nested functions so this stays a single
    # DDL_STATEMENTS entry (install runs each entry as one execute()).
    # CHECK-fold helpers (schema-local): pure text surgery (position/substr/left, NO regex, NO backreferences, NO backslashes in any body).
    # _peel strips wrapping parens; _start finds the identifier start; _fold_any/_range/_eq canonicalize to IN/between.
    """DROP FUNCTION IF EXISTS "{schema}"._lcm_checkfold(oid);
DROP FUNCTION IF EXISTS "{schema}"._peel(text);
DROP FUNCTION IF EXISTS "{schema}"._start(text,int);
DROP FUNCTION IF EXISTS "{schema}"._fold_range(text);
DROP FUNCTION IF EXISTS "{schema}"._fold_eq(text);
DROP FUNCTION IF EXISTS "{schema}"._fold_any(text);
DROP FUNCTION IF EXISTS "{schema}"._fold(text);

-- CHECK-fold as PURE plpgsql: position()/substr()/left() only.
-- ZERO regex, ZERO backreferences, ZERO backslashes in the body.
-- Transport-proof (psql -f / DO / catalog rewrite / git) and kills the
-- backref/SOH corruption class structurally.
--
-- CRITICAL plpgsql rule: any char-test loop must EXIT on k<=0 FIRST, and test
-- characters NULL-safely (c >= '0' AND c <= '9', never NOT(...), since an
-- out-of-range substr() yields NULL and NOT(NULL) is NULL, not TRUE, so an
-- EXIT WHEN NOT(...) never fires -> index runs off the end, off-by-one bugs).

-- strip balanced wrapper parens: while s='( ... )' with matching closer at the
-- end, drop it. Extra closers stay (they become harmless trailing parens the
-- shim wraps around CHECK(...)).
CREATE OR REPLACE FUNCTION {schema}._peel(f text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE s text := btrim(f);
BEGIN
  -- match Python s.strip()[1:-1]: when the whole expr is wrapped '(...)',
  -- drop ONE leading '(' and ONE trailing ')' (the outermost wrapper). Repeat
  -- while the result is still fully wrapped. Inner parens stay.
  WHILE length(s) >= 2 AND left(s,1) = '(' AND right(s,1) = ')' LOOP
    s := btrim(substr(s, 2, length(s) - 2));
  END LOOP;
  -- leftover wrapper after 'CHECK (' strip on a doubly-wrapped expr
  IF length(s) > 1 AND left(s, 2) = '((' THEN
    s := btrim(substr(s, 2));
  END IF;
  -- PG's pg_get_constraintdef nests ('CHECK (((expr)))'), so the fold's
  -- substr(cd,8) CHECK-prefix strip can leave a trailing ')' the loop above
  -- cannot see (s no longer starts with '('). Those are NOT harmless here:
  -- the reconstruction wraps each folded clause as CHECK(<clause>), so a
  -- stray closer yields 'CHECK(length(x) = 64))' -- an extra paren that
  -- makes the rebuilt sqlite DDL unparseable (verify's PRAGMA table_info
  -- then silently falls back to the catalog view and reports every
  -- PRIMARY KEY column as nullable -> false 'malformed table'). Drop any
  -- residual trailing closers so _peel returns a balanced expression.
  WHILE length(s) > 0 AND right(s, 1) = ')' LOOP
    s := btrim(rtrim(s, ')'));
  END LOOP;
  RETURN s;
END $fn$;

-- start (1-based) of the identifier left of the char at i (scans back to a
-- delimiter ' ' / ')' or string start). Returns that start position (>=1).
CREATE OR REPLACE FUNCTION {schema}._start(s text, i int) RETURNS int
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE j int := i; c text;
BEGIN
  WHILE j > 0 LOOP
    c := substr(s,j,1);
    IF c = '(' OR c = ' ' OR c = ')' THEN EXIT; END IF;   -- delimiter stops scan
    j := j - 1;
  END LOOP;
  RETURN j + 1;
END $fn$;

-- (A >= lo) AND (A <= hi)  ->  A between lo and hi
CREATE OR REPLACE FUNCTION {schema}._fold_range(f text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE i int; j2 int; k0 int; k1 int; p1 int; k2 int; k3 int; p2 int;
        a0 int; a text; b text; lo text; hi text; c text; guard int := 0;
BEGIN
  LOOP
    i := position(' >= ' in f);
    EXIT WHEN i = 0 OR guard >= 20;
    guard := guard + 1;
    a0 := {schema}._start(f, i - 1);
    a := substr(f, a0, (i - 1) - a0 + 1);     -- 'col'
    j2 := position(' <= ' in substr(f, i + 4));
    EXIT WHEN j2 = 0;
    j2 := i + 4 + j2 - 1;                       -- 1-based pos of the space before ' <= '
    b := substr(f, {schema}._start(f, j2 - 1), (j2 - 1) - {schema}._start(f, j2 - 1) + 1);
    EXIT WHEN b <> a;
    k0 := i + 4;  WHILE k0 <= length(f) AND substr(f,k0,1) = ' ' LOOP k0 := k0 + 1; END LOOP;
    k1 := k0;     WHILE k1 <= length(f) AND ((substr(f,k1,1) >= '0' AND substr(f,k1,1) <= '9') OR substr(f,k1,1) = '.') LOOP k1 := k1 + 1; END LOOP;
    lo := substr(f, k0, k1 - k0);
    -- extend past the block's trailing ')' so it is dropped, not left stray
    p1 := k1;     WHILE p1 <= length(f) AND substr(f,p1,1) = ')' LOOP p1 := p1 + 1; END LOOP;
    EXIT WHEN lo = '';
    k2 := j2 + 4; WHILE k2 <= length(f) AND substr(f,k2,1) = ' ' LOOP k2 := k2 + 1; END LOOP;
    k3 := k2;     WHILE k3 <= length(f) AND ((substr(f,k3,1) >= '0' AND substr(f,k3,1) <= '9') OR substr(f,k3,1) = '.') LOOP k3 := k3 + 1; END LOOP;
    hi := substr(f, k2, k3 - k2);
    -- extend past the block's trailing ')' so it is dropped, not left stray
    p2 := k3;     WHILE p2 <= length(f) AND substr(f,p2,1) = ')' LOOP p2 := p2 + 1; END LOOP;
    EXIT WHEN hi = '';
    f := substr(f, 1, a0 - 1) || a || ' between ' || lo || ' and ' || hi || substr(f, p2);
  END LOOP;
  RETURN f;
END $fn$;

-- A = 'lit'  ->  A IN ('lit')   (single = ; 'ANY' handled by _fold_any)
CREATE OR REPLACE FUNCTION {schema}._fold_eq(f text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE out text := ''; rest text := f; i int; a0 int; a text; k int; k2 int; lit text; c text; guard int := 0;
BEGIN
  LOOP
    i := position(' = ' in rest);
    IF i = 0 THEN out := out || rest; EXIT; END IF;
    guard := guard + 1; EXIT WHEN guard >= 64;
    a0 := {schema}._start(rest, i - 1);
    a := substr(rest, a0, (i - 1) - a0 + 1);
    lit := NULL;
    k := i + 3;
    c := substr(rest, k, 1);
    IF c = '''' THEN
      k2 := position('''' in substr(rest, k + 1));
      IF k2 > 0 THEN k2 := k + k2; lit := substr(rest, k, k2 - k + 1); k := k2 + 1; END IF;
    ELSIF c >= '0' AND c <= '9' THEN
      WHILE k <= length(rest) AND ((substr(rest,k,1) >= '0' AND substr(rest,k,1) <= '9') OR substr(rest,k,1) = '.') LOOP k := k + 1; END LOOP;
      lit := substr(rest, i + 3, k - (i + 3));
    END IF;
    IF a = '' OR lit IS NULL THEN
      out := out || substr(rest, 1, i + 2);   -- keep through ' = '
      rest := substr(rest, i + 3);
      CONTINUE;
    END IF;
    WHILE k <= length(rest) AND (substr(rest,k,1) = ')' OR substr(rest,k,1) = ' ') LOOP k := k + 1; END LOOP;
    out := out || substr(rest, 1, a0 - 1) || a || ' IN (' || lit || ')';
    rest := substr(rest, k);
  END LOOP;
  RETURN out;
END $fn$;

-- A = ANY (ARRAY[..])  ->  A IN (..)
CREATE OR REPLACE FUNCTION {schema}._fold_any(f text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE i int; j int; a0 int; a text; items text; mk text := ' = ANY (ARRAY['; guard int := 0;
BEGIN
  LOOP
    i := position(mk in f);
    EXIT WHEN i = 0 OR guard >= 20;
    guard := guard + 1;
    a0 := {schema}._start(f, i - 1);
    a := substr(f, a0, (i - 1) - a0 + 1);
    j := position(']' in substr(f, i));           -- first ']' from i
    IF j = 0 THEN EXIT; END IF;
    j := i + j - 1;
    EXIT WHEN a = '';
    items := substr(f, i + length(mk), j - (i + length(mk)));  -- between ARRAY[ and ]
    -- tail: after '] )' (both consumed; ARRAY's closing paren dropped)
    f := substr(f, 1, a0 - 1) || a || ' IN (' || items || ')' || substr(f, j + 3);
  END LOOP;
  RETURN f;
END $fn$;

-- top level: CHECK(...) -> folded
CREATE OR REPLACE FUNCTION {schema}._fold(cd text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE f text;
BEGIN
  -- PG renders pg_get_constraintdef as either 'CHECK (expr)' (simple, one
  -- space) or 'CHECK ((expr))' (when it adds its own paren layer). The old
  -- hardcoded substr(cd, 8) assumed the two-space form ONLY; on the simple
  -- form it cut one char INTO the first token (e.g. 'length(' -> 'ength('),
  -- leaving a stray ')' that corrupted the reconstructed CHECK clause. Peel
  -- either prefix by its actual length so _peel always sees clean content.
  IF left(cd, 7) = 'CHECK (' THEN f := substr(cd, 8);
  ELSEIF left(cd, 6) = 'CHECK(' THEN f := substr(cd, 7);
  ELSE f := substr(cd, 7); END IF;
  f := {schema}._peel(f);
  f := replace(f, '::text', '');
  f := replace(f, '::integer', '');
  f := replace(f, '::bigint', '');
  f := replace(f, '::smallint', '');
  f := {schema}._fold_any(f);
  f := {schema}._fold_range(f);
  f := {schema}._fold_eq(f);
  RETURN {schema}._peel(f);
END $fn$;""",
    # _lcm_checkfold(oid): the CHECK-clause fold the shim calls (normalises pg CHECK(...) prefix, then folds).
    """CREATE OR REPLACE FUNCTION "{schema}"._lcm_checkfold(p oid) RETURNS text
LANGUAGE sql IMMUTABLE AS $cf$
  SELECT "{schema}"._fold(
    CASE WHEN left(pg_get_constraintdef(p), 6) = 'CHECK ' THEN
      'CHECK (' || btrim(substr(pg_get_constraintdef(p), 7)) || ')'
    ELSE pg_get_constraintdef(p) END)
$cf$""",

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
                 'CHECK(' || "{schema}"._lcm_checkfold(p.oid) || ')', ', ' ORDER BY p.oid)
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
    cur.execute(BIGINT_TEXT_FN)
    cur.execute(BIGINT_TEXT_EQ)
    for stmt in DDL_STATEMENTS:
        cur.execute(stmt.format(schema=schema))
    cur.execute(VIEW.format(schema=schema))


class _ConnShim:
    """Expose cursor.execute(sql) for helper installers written against
    connections (they never pass params, so this is sufficient)."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, *args, **kw):
        return self._cur.execute(sql, *args, **kw)
