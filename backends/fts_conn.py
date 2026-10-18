"""Connection-shaped adapter exposing the FTS5 virtual-table facade on Postgres.

Upstream search SQL uses:
  FROM <fts_table> fts JOIN <content_table> c ON c.<pk> = fts.rowid
  WHERE <fts_table> MATCH ?
  rank AS search_rank
  snippet(<fts_table>, 0, '>>>', '<<<', '...', 40)

The shim rewrites all of it to the trigger-maintained tsvector column that
backends/fts_pg.ensure_fts put on the content table. Ranking uses ts_rank.
The fork treats FTS as a fallback path (vector recall is the primary), so
exact score parity with SQLite BM25 is explicitly NOT promised — callers keep
their LIKE fallback on error, unchanged.

Placeholder policy: `?` becomes a sentinel that finally becomes `%s`. Rank
rewrites COPY the WHERE clause's tsquery expression (which contains the
sentinel) into ts_rank(...) slots; execute() then pads the bound-parameter
tuple so every sentinel occurrence receives the same query value — SQLite
allows one `?` to sit in two places, psycopg needs one bind per occurrence.
"""

from __future__ import annotations

import re

from . import fts_pg
from .postgres import _rewrite_text as _pg_rewrite

# Sentinel for a positional `?`: MUST contain NO word characters (so alias-
# capture regexes like (?!_KW\b)(\w+) can never match across it) and must
# never occur in real SQL.
_SENTINEL = "\x00\x01\x00"

_SPEC_REGISTRY: dict[int, tuple[tuple[str, str, str], ...]] = {}

# content-less INSERT targets whose generated PK must be recoverable via
# RETURNING for sqlite lastrowid parity. Extend if another store needs it.
_INSERT_ROWID_TABLES = {"messages"}

# SQL keywords that can never be a table alias
_KW = r"(?:WHERE|JOIN|ON|ORDER|GROUP|LIMIT|OFFSET|SET|USING|AS|UNION|LEFT|RIGHT|INNER|CROSS|HAVING|RETURNING)"

_TQ = "to_tsquery('english', lcm_normalize_fts("  # literal, built in code


def set_spec(conn: "FtsConnection", specs: tuple[tuple[str, str, str], ...]) -> None:
    """Register (fts_table, content_table, indexed_column) for this connection."""
    if len(_SPEC_REGISTRY) > 256:
        _SPEC_REGISTRY.clear()
    _SPEC_REGISTRY[id(conn)] = tuple(specs)
    conn._specs = tuple(specs)


def _protect_params(sql: str) -> str:
    """Uniform sentinel for positional `?` outside string literals."""
    out, i, in_str = [], 0, False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_str = not in_str
        if ch == "?" and not in_str:
            out.append(_SENTINEL)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _unprotect_params(sql: str) -> str:
    return sql.replace(_SENTINEL, "%s")


def _sentinels_outside_literals(sql: str) -> int:
    """Count sentinel occurrences outside single-quoted string literals.
    Each is a positional bind (they all trace back to the WHERE's MATCH value,
    re-spelled into ORDER BY's ts_rank by the rewriter)."""
    n, i, in_str = 0, 0, False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_str = not in_str
        elif not in_str and sql.startswith(_SENTINEL, i):
            n += 1
            i += len(_SENTINEL)
            continue
        i += 1
    return n


def _placeholders_outside_literals(sql: str, tok: str) -> int:
    """Count `tok` ('?' or '%s') occurrences outside string literals."""
    n, i, in_str = 0, 0, False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_str = not in_str
        elif not in_str and sql.startswith(tok, i):
            n += 1
            i += len(tok)
            continue
        i += 1
    return n


def _pre_pg(sql: str) -> str:
    """Neutralize bound params, then run pg text rewrites safely."""
    return _pg_rewrite(_protect_params(sql))


def _pg_post(sql: str) -> str:
    """Final pg text rewrite (no-op if already applied) + %s restore."""
    return _unprotect_params(_pg_rewrite(sql))


class FtsConnection:
    """Wraps a backend connection; rewrites FTS5 virtual-table SQL to PG."""

    def __init__(self, base):
        self._base = base
        self._specs: tuple[tuple[str, str, str], ...] = ()  # set via set_spec()

    # -- passthroughs -------------------------------------------------------
    def __getattr__(self, name):
        return getattr(self._base, name)

    def cursor(self):
        return _FtsCursor(self._base.cursor(), self._specs)

    def execute(self, sql, parameters=()):
        c = self.cursor()
        return c.execute(sql, parameters)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def executescript(self, script):
        return self._base.executescript(script)

    def close(self):
        _SPEC_REGISTRY.pop(id(self), None)
        return self._base.close()


class _FtsCursor:
    def __init__(self, cur, specs):
        self._cur = cur
        self._specs = specs
        self._extra_params = 0

    def _rewrite_ts(self, sql: str) -> str:
        self._extra_params = 0
        if not self._specs:
            return sql
        if "match" not in sql.lower() and not re.search(r"\b\w+_fts\b", sql, flags=re.I):
            return sql
        out = _pre_pg(sql)
        specs = []
        for fts_table, content_table, indexed_col in self._specs:
            if not re.search(rf"\b{fts_table}\b", out, flags=re.I):
                continue
            m = re.search(rf"\b{fts_table}\s+(?:AS\s+)?(?!{_KW}\b)(\w+)", out, flags=re.I)
            alias = m.group(1) if m else fts_table
            if re.fullmatch(_KW, alias, flags=re.I):
                alias = fts_table
            specs.append((fts_table, alias, content_table, fts_pg.ts_colname(indexed_col)))
        for fts_table, alias, content_table, tcol in specs:
            src = tcol[: -len("_tsv")]
            tsq_open = "to_tsquery('english', lcm_normalize_fts("

            # FROM entry as written: `FROM <fts_table> [alias]`. The FROM
            # table token becomes the content table later (step 2), so the
            # visible name after all surgery is the FROM entry's ALIAS if it
            # has one, else the bare content table — the FTS name itself is
            # never a valid qualifier afterwards.
            mfr = re.search(rf"\bFROM\s+{fts_table}\b(\s+(?!{_KW}\b)(\w+))?", out, flags=re.I)
            f_alias = (mfr.group(2) if mfr and mfr.group(2) else "")

            # The self-JOIN (explicit or comma form) contributes a second name
            # for the SAME content table (`JOIN messages m`). Postgres allows
            # only ONE name per relation in the collapsed single-table form,
            # so the FROM entry adopts the JOIN's alias when one exists and the
            # FROM had none, and every alias for the FTS/content pair is then
            # mapped onto that single visible name at the end.
            jname = ""
            mj = re.search(rf"(?i)\s*,?\s*(?:LEFT\s+|RIGHT\s+|INNER\s+|CROSS\s+)?JOIN\s+{content_table}\s+(?:AS\s+)?(?!{_KW}\b)(\w+)\s+ON\s+{alias}\.rowid\s*=\s*\1\.\w+|\s*,?\s*(?:LEFT\s+|RIGHT\s+|INNER\s+|CROSS\s+)?JOIN\s+{content_table}\s+(?:AS\s+)?(?!{_KW}\b)(\w+)\s+ON\s+\1\.\w+\s*=\s*(?:{alias}|{fts_table}|{content_table})\.rowid", out)
            if mj:
                jname = mj.group(1) or mj.group(2) or ""
            # ONE visible name for the collapsed single-table query. Prefer the
            # JOIN's alias when there is a self-JOIN: the upstream SQL refers to
            # content columns THROUGH the JOIN (m.col), and the FTS alias (fts)
            # disappears with the join. Otherwise the FROM entry's own alias,
            # else the bare content table name.
            _vis = jname or f_alias or content_table
            if jname and jname.lower() != content_table.lower():
                # FROM entry takes the visible name (renaming `FROM x fts` ->
                # `FROM x m` or `FROM messages m`).
                out = re.sub(rf"(?i)\bFROM\s+{fts_table}(\s+(?!{_KW}\b)\w+)?",
                             f"FROM {content_table} {jname}", out, count=1)
                f_alias = jname
            # mj above only matches a SUBSET of the valid ON-clause spellings;
            # rescan with the full drop-pattern so jname is never under-detected
            # (an undetected JOIN alias leaves `m.col` refs orphaned).
            if not jname:
                _mj_full = re.search(
                    rf"(?i)\s*,?\s*(?:LEFT\s|RIGHT\s|INNER\s|CROSS\s)?JOIN\s+{content_table}\s*(?:AS\s+)?"
                    rf"(?!{_KW}\b)(\w+)\s+ON\s+"
                    rf"(?:(?:{alias}|{f_alias}|{fts_table}|{content_table})\.\w+\s*=\s*\w+\.\w+"
                    rf"|\w+\.\w+\s*=\s*(?:{alias}|{f_alias}|{fts_table}|{content_table})\.\w+)", out)
                if _mj_full:
                    jname = _mj_full.group(1)
                    if not f_alias and jname.lower() != content_table.lower():
                        out = re.sub(rf"(?i)\bFROM\s+{fts_table}\b(?!\s+{jname}\b)",
                                     f"FROM {content_table} {jname}", out, count=1)
                        f_alias = jname
                        _vis = jname

            # 1. MATCH predicate -> tsvector match (before FROM/JOIN surgery).
            def _match_sub(m):
                q = _qvar(m.group(1))
                return f'"{tcol}" @@ {tsq_open}{q}))'

            out = re.sub(
                rf'(?:\b{alias}\b|\b{fts_table}\b)\s+MATCH\s+(\'(?:[^\']|\'\')*\'|{_SENTINEL})',
                _match_sub, out, flags=re.I)

            # 1b. Bare self-join form `FROM messages_fts JOIN messages` (no
            # alias on the FTS entry). The later FROM-surgery patterns expect
            # the FTS name in the FROM clause, so normalize the TABLE TOKEN
            # ONLY (keep any trailing alias the JOIN entry provides).
            out = re.sub(rf"(?i)\bFROM\s+{fts_table}\s+(?=(?:LEFT |RIGHT |INNER |CROSS )?JOIN\b)",
                         f"FROM {content_table} ", out, count=1)
            # (no alias on the FTS entry itself: \s+ before the comma).
            out = re.sub(rf"(?i)\bFROM\s+{fts_table}\s+(?=,\s*{content_table}\b)",
                         f"FROM {content_table} ", out, count=1)
            # 2. FROM <fts> [alias] -> FROM <content_table> [alias] — SKIPPED
            # entirely for comma-join forms (`FROM fts [x], content [y]`): step
            # 3a owns those and must see the FTS table name to match.
            _comma_from = re.search(
                rf"(?i)\bFROM\s+{fts_table}(?:\s+(?!{_KW}\b)\w+)?\s*,\s*{content_table}\b", out)
            if not _comma_from:
                out = re.sub(rf"(?i)\bFROM\s+{fts_table}(\s+(?!{_KW}\b)\w+)?",
                             f"FROM {content_table}" + (r"\1" if f_alias else ""),
                             out, count=1)

            # 3. drop the fts<->content self-JOIN on rowid (all operand orders,
            # LEFT/RIGHT/INNER/CROSS variants, and the comma-join form). The
            # dropped JOIN's alias is a second name for the SAME table now
            # living in FROM under `f_alias`; after the drops, every alias in
            # use (FTS alias, JOIN alias) maps onto that one visible name.
            def _drop_join(jm):
                return " "
            # `FROM <fts> t1, <content> t2` comma join (older sqlite style).
            # The collapsed query keeps the CONTENT table's own entry (its
            # alias if it had one) as the single FROM entry; the FTS entry
            # vanishes, and the remap below maps the FTS alias onto it.
            def _comma_join(cm):
                ca = cm.group(1) or ""
                return f"FROM {content_table} {ca}".rstrip()
            out = re.sub(rf"(?i)\bFROM\s+{fts_table}(?:\s+(?!{_KW}\b)\w+)?\s*,\s*{content_table}(?:\s+(?!{_KW}\b)(\w+))?(?=\s+WHERE\b|\s*$|\s+\))",
                         _comma_join, out, count=1)
            # (?:\1\.|\w+\.) on the rhs: `JOIN messages ON messages_fts.rowid =
            # messages.store_id` has no captured alias reference in the ON
            # clause at all — the rhs is qualified by the JOIN's own (or the
            # content table's) name, which the alias-remap below normalises.
            out = re.sub(
                rf"(?i)\s*,?\s*(?:LEFT\s|RIGHT\s|INNER\s|CROSS\s)?JOIN\s+{content_table}\s*(?:AS\s+)?"
                rf"(?!{_KW}\b)(\w*)\s+ON\s+"
                rf"(?:(?:{alias}|{f_alias}|{jname}|{fts_table}|{content_table})\.\w+\s*=\s*(?:\1|\w+)\.\w+"
                rf"|(?:\1|\w+)\.\w+\s*=\s*(?:{alias}|{f_alias}|{jname}|{fts_table}|{content_table})\.\w+)",
                _drop_join, out, count=1)
            # one visible name: remap every other alias for this relation.
            for _other in {alias, fts_table, jname, content_table} - {"", _vis}:
                if _other.lower() == _vis.lower():
                    continue
                out = re.sub(rf"(?<![\w.\"]){_other}(?=\s*\.)", _vis, out, flags=re.I)

            # 4. snippet() -> ts_headline over the source column
            out = re.sub(rf"snippet\s*\(\s*(?:{alias}|{fts_table})\s*,[^)]*\)",
                         "ts_headline('english', coalesce(\"" + src + "\"::text, ''), "
                         "'StartSel=>>>, StopSel=<<<, MaxFragments=1, MaxWords=40')",
                         out, flags=re.I)

            # 5. rank -> ts_rank over the SAME tsquery expression from WHERE.
            # Balanced-paren capture of the @@ RHS (the old lazy `\)` +
            # clause-keyword lookahead lost the final paren of nested calls).
            iat = out.find("@@")
            tsexpr = "''"
            if iat != -1:
                j = iat + 2
                while j < len(out) and out[j] == " ": j += 1
                depth, k = 0, j
                while k < len(out):
                    ch = out[k]
                    if ch == "(": depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0: k += 1; break
                    elif ch == "'" and depth == 0:
                        k += 1
                        while k < len(out) and out[k] != "'": k += 1
                    elif depth == 0 and re.match(r"(?i)\b(AND|ORDER|GROUP|LIMIT|OFFSET|WHERE)\b", out[k:]):
                        break
                    k += 1
                tsexpr = out[j:k].strip()
            ref = 'ts_rank("' + tcol + '", ' + tsexpr + ')'
            out = re.sub(rf"\b{alias}\.rank\b", ref, out, flags=re.I)
            # `rank AS x` / `rank x` in a SELECT list is an ALIAS DEFINITION —
            # FTS5's hidden `rank` column does not exist on PG, so substitute
            # the full ts_rank expression while keeping the alias clause.
            # (In PG, ORDER BY resolves to the output alias, so ordering is
            # preserved without touching the alias name itself.)
            out = re.sub(r"\brank(?=\s+(?:AS\s+|as\s+)?[a-z_]\w*\s*(?:,|\n|$))", ref, out,
                         flags=re.I)
            # Bare `rank` in WHERE/ORDER BY contexts (before ASC/DESC or
            # clause-terminating boundaries) becomes the full ts_rank
            # expression. `rank AS x` in a SELECT list is an ALIAS definition —
            # never touch it; ORDER BY rank then resolves to the alias column.
            _BOUND = r"\s*(?:\bASC\b|\bDESC\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|\bOFFSET\b|\bFETCH\b|\bUNION\b|EXCEPT|INTERSECT|\bWINDOW\b|$)"
            out = re.sub(r"(?<![\w.\"])rank\b(?=" + _BOUND + ")", ref, out,
                         flags=re.I)
            # Placeholder accounting: every occurrence of the WHERE clause's
            # bound query literal (sentinel) OUTSIDE string literals needs one
            # bind. ORDER BY ts_rank(...) re-spells the WHERE's expression, so
            # those occurrences add to the original WHERE bind.
            _i, _c, _in_str = 0, 0, False
            while _i < len(out):
                if out[_i] == "'":
                    _in_str = not _in_str
                elif not _in_str and out.startswith(_SENTINEL, _i):
                    _c += 1
                    _i += len(_SENTINEL)
                    continue
                _i += 1
            self._extra_params = max(self._extra_params, _c - 1)
        return out

    def execute(self, sql, parameters=()):
        sql_in = sql
        sql = self._rewrite_ts(sql)
        final = _unprotect_params(sql)
        # Padding: the rewriter duplicates the WHERE's bound MATCH expression
        # into ORDER BY's ts_rank(...). Every placeholder outside string
        # literals is served by the SAME value as the original WHERE bind.
        # Count total placeholders in `final` and the original `?` binds
        # (outside string literals) in the input; the difference is how many
        # extra copies of params[0] to append.
        total = _placeholders_outside_literals(final, "%s")
        supplied_n = len(parameters) if (parameters and not isinstance(parameters, dict)) else 0
        orig_qs = _placeholders_outside_literals(sql_in, "?")
        # original ? outside literals == binds the query had (orig_qs count);
        # params may carry MORE binds (session_id, limit...) than the ts part
        # — never pad below what the original SQL's ? count.
        extra = max(total - orig_qs, 0)
        if supplied_n and extra > 0 and supplied_n >= 1:
            params = tuple(parameters) + (list(parameters)[0],) * extra
        elif not parameters:
            params = ()
        else:
            params = tuple(parameters) if not isinstance(parameters, dict) else parameters
        up = final.upper()
        # sqlite3 exposes lastrowid for plain INSERTs; the pg shim recovers the
        # generated key via RETURNING, but ONLY for simple single-row INSERTs
        # without INSERT...SELECT or ON CONFLICT DO * clauses (those make
        # RETURNING return 0 or many rows).
        aug = ""
        if (re.match(r"^\s*INSERT\s+INTO\b", final, flags=re.I)
                and "RETURNING" not in up and " SELECT" not in up
                and re.search(r"\bVALUES\b", up)
                and not re.search(r"DO\s+(NOTHING|UPDATE|SELECT)\b", up)):
            t = re.match(r"^\s*INSERT\s+INTO\s*\W?(\w+)", final, flags=re.I).group(1)
            aug = " RETURNING *" if t in _INSERT_ROWID_TABLES else ""
        try:
            self._cur.execute(final + aug, params)
        except Exception as exc:
            if not aug or "returning" not in str(exc).lower():
                raise
            self._cur.execute(final, params)
        return self

    def executemany(self, sql, seq):
        rows = [tuple(r) if not isinstance(r, tuple) else r for r in seq]
        self._cur.executemany(_pg_post(_protect_params(sql)), rows)
        return self

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _qvar(q: str) -> str:
    return _SENTINEL if q.strip() in ("%s", "?", _SENTINEL) else q
