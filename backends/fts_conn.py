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


def add_spec(conn: "FtsConnection", spec: tuple[str, str, str]) -> None:
    """Append one spec post-connect (store's own ensure_external_content_fts
    path discovered a new FTS surface after factory.connect decided specs)."""
    cur = getattr(conn, "_specs", ())
    if spec not in cur:
        set_spec(conn, (*cur, spec))


def _protect_percents(sql: str) -> str:
    """Literal `%s` OUTSIDE string literals -> sentinel (defensive; SQLite-
    shaped SQL should not contain %s). Inside quotes, leave untouched — a
    strftime('%s',...) pattern is a format spec, not a psycopg bind."""
    out, i, in_str = [], 0, False
    while i < len(sql):
        c = sql[i]
        if c == "'":
            in_str = not in_str
            out.append(c); i += 1; continue
        if not in_str and sql.startswith("%s", i):
            out.append(_SENTINEL); i += 2; continue
        out.append(c); i += 1
    return "".join(out)


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
    """Convert sentinel placeholders to psycopg %s. Used by paths WITHOUT
    positional re-binding (executemany). The FTS SELECT path binds
    sentinels positionally via _bind_sentinels instead, so late copies
    become lcm_q($1) references rather than new placeholders."""
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


def _where_start(sql: str) -> int:
    """Index of the first WHERE keyword outside string literals (else -1)."""
    i, in_str, n = 0, False, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            in_str = not in_str
            i += 1; continue
        if not in_str and (i == 0 or not (sql[i-1].isalnum() or sql[i-1] == "_")) \
                and sql.startswith("WHERE", i):
            j = i + 5
            if j >= n or not (sql[j].isalnum() or sql[j] == "_"):
                return i
        i += 1
    return -1


_TSQ = "lcm_normalize_fts("


def _bind_sentinels_by_input(sentinel_sql: str, params, orig_qs: int):
    """Bind sentinels: genuine placeholders -> %s (positional, params
    untouched), rewriter COPIES -> baked literal via lcm_tsq('...').

    Rules (verified against store.search's FTS SQL shape):

    1. The tsvector MATCH predicate is `col @@ to_tsquery('lang',
       lcm_normalize_fts(SENT))` (from _rewrite_ts step 1). The sentinel
       INSIDE that @@ RHS span is the genuine MATCH bind -> %s. Its
       positional slot = count of operand-position sentinels before it,
       which for store shapes is 0 (params[0] = the query).

    2. Every OTHER sentinel is a COPY (SELECT-list ts_rank / ts_headline /
       ORDER BY ts_rank re-spellings of that same expression) -> literal
       bake of the MATCH param's value: `lcm_tsq('<params[pred_slot]>')`.
       Copies add zero placeholders.

    3. Sentinels outside any to_tsquery span keep the operand classifier
       (right of =, <, >, !=, <>, <=, >=, LIKE, ILIKE, IS, IN(, LIMIT,
       OFFSET, arithmetic = genuine %s; otherwise argument position —
       a copy of some function's arg = also bake with its positional
       value if identifiable, else params[0]).

    4. If the classifier finds no to_tsquery spans (non-search SQL), this
       degrades to pure positional binding: sentinel -> %s everywhere —
       identical to the legacy shim."""
    n_params = 0 if isinstance(params, dict) else len(params)
    if n_params == 0:
        return sentinel_sql.replace(_SENTINEL, "%s"), params

    # -- span of the FIRST @@ predicate's tsquery function-arg parens ----
    pred_span = None
    jj = 0
    n = len(sentinel_sql)
    while True:
        jj = sentinel_sql.find("@@", jj)
        if jj == -1:
            break
        # quote-parity at jj decides whether @@ is real (outside literals)
        if sentinel_sql.count("'", 0, jj) % 2 == 0:
            k = jj + 2
            depth = 0
            in2 = False
            end = None
            while k < n:
                ch = sentinel_sql[k]
                if ch == "'":
                    in2 = not in2
                elif not in2:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            end = k + 1
                            break
                    elif depth == 0 and re.match(
                            r"(?i)\b(AND|ORDER|GROUP|LIMIT|OFFSET|WHERE)\b",
                            sentinel_sql[k:]):
                        end = k
                        break
                k += 1
            if end is not None:
                pred_span = (jj, end)
                break
        jj += 2

    def _in_pred(pos):
        return pred_span is not None and pred_span[0] < pos < pred_span[1]

    # -- scan occurrences: classify operand vs argument ------------------
    occs = []
    j, in_str = 0, False
    while j < n:
        c = sentinel_sql[j]
        if c == "'":
            in_str = not in_str
            j += 1; continue
        if in_str:
            j += 1; continue
        if sentinel_sql.startswith(_SENTINEL, j):
            p = j - 1
            while p >= 0 and sentinel_sql[p] in " \t\r\n":
                p -= 1
            operand = False
            if p < 0:
                operand = True
            else:
                ch = sentinel_sql[p]
                if ch in "=<>+-*/%@!":
                    operand = True
                elif ch == "(":
                    w = p
                    while w >= 0 and (sentinel_sql[w].isalnum() or sentinel_sql[w] == "_"):
                        w -= 1
                    operand = sentinel_sql[w + 1:p + 1].upper() == "IN"
                elif ch.isalnum() or ch == "_":
                    w = p
                    while w >= 0 and (sentinel_sql[w].isalnum() or sentinel_sql[w] == "_"):
                        w -= 1
                    operand = sentinel_sql[w + 1:p + 1].upper() in (
                        "LIMIT", "OFFSET", "LIKE", "ILIKE", "IN", "IS",
                        "AND", "OR", "NOT", "BETWEEN")
            if _in_pred(j):
                operand = True  # predicate bind is genuine regardless
            occs.append((j, operand))
            j += len(_SENTINEL); continue
        j += 1
    if not occs:
        return sentinel_sql, params
    if not pred_span and len(occs) <= orig_qs:
        # plain positional SQL (non-fts or fts without copies)
        return sentinel_sql.replace(_SENTINEL, "%s"), params

    # -- positional mapping: the k-th operand occurrence takes params
    #    assigned by psycopg to %s order = original `?` order. The original
    #    slot of occurrence j (1-based SQL order) is j-1 ONLY for genuine
    #    placeholders; copies consumed no placeholder. Genuine occurrences
    #    in SQL order are exactly the input's `?` order, so the m-th
    #    genuine (operand) occurrence takes params[m-1].
    # -- BUT copies appear interleaved; psycopg %s order = order we emit
    #    %s = genuine order = params order. So the bind tuple is params
    #    AS-IS iff the number of %s emitted == n_params. When copies stole
    #    slots (genuine count < n_params) — e.g. LIMIT ? misclassified —
    #    we must NOT bake genuine ones. The predicate rule guarantees the
    #    MATCH bind is never baked, so mismatch can only mean the
    #    classifier missed an operand; detect: operand_count < orig_qs ->
    #    the surplus params belong to those missed operands. Positional
    #    binding can't express "skip the missed slot", so reorder params:
    #    %s k takes params[slot_k] where slot_k = occurrence-index based.
    g_list = [pos for pos, o in occs if o]
    c_list = [pos for pos, o in occs if not o]
    if len(g_list) == n_params:
        # clean split: positional params as-is
        out_s, last = [], 0
        for pos, operand in occs:
            out_s.append(sentinel_sql[last:pos])
            if operand:
                out_s.append("%s")
            else:
                out_s.append("%s")  # placeholder below replaced by literal
            last = pos + len(_SENTINEL)
        out_s.append(sentinel_sql[last:])
        sql = "".join(out_s)
        # bake copies: they stand for the predicate slot = the operand
        # inside pred_span, whose slot = count operands before it.
        pslot = 0
        for m, (pos, operand) in enumerate(occs):
            if _in_pred(pos):
                pslot = sum(1 for q, o in occs[:m] if o)
                break
        v = params[pslot] if pslot < n_params else ""
        lv = str(v).replace("'", "''") if v is not None else ""
        # rebuild copies with literal
        out_s, last = [], 0
        for pos, operand in occs:
            out_s.append(sentinel_sql[last:pos])
            if operand:
                out_s.append("%s")
            else:
                out_s.append(f"lcm_tsq('{lv}')")
            last = pos + len(_SENTINEL)
        out_s.append(sentinel_sql[last:])
        return "".join(out_s), params
    # mismatch: map by occurrence index (occurrence j -> params[j-1]);
    # copies bake their own occurrence param (== the value their source
    # would have carried in naive positional order — for tsquery copies
    # the source is always the predicate param; for others the naive slot
    # is correct by construction of naive %s numbering).
    out_s, last = [], 0
    kept = []
    for m, (pos, operand) in enumerate(occs):
        out_s.append(sentinel_sql[last:pos])
        if operand:
            if m < n_params:
                out_s.append("%s")
                kept.append(m)
            else:
                out_s.append("%s")
                kept.append(None)
        else:
            src_i = m if m < n_params else 0
            v = params[src_i]
            lv = str(v).replace("'", "''") if v is not None else ""
            out_s.append(f"lcm_tsq('{lv}')")
        last = pos + len(_SENTINEL)
    out_s.append(sentinel_sql[last:])
    new_params = tuple((params[k] if k is not None and k < n_params else None)
                       for k in kept)
    return "".join(out_s), new_params

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




def _position_cast(sql: str, params):
    """CAST every positional %s by context: bigint after LIMIT/OFFSET, text
    everywhere else. PG 18 then never falls back to the smallint default.
    LIMIT/OFFSET binds get Int32 (exact assignment to bigint); text slots
    pass through unchanged (psycopg sends str with oid=text; bools become
    '1'/'0'). Param COUNT is preserved so pad math is untouched."""
    try:
        from psycopg.types.numeric import Int32
    except ImportError:  # pragma: no cover
        Int32 = None
    out, i, n = [], 0, len(sql)
    in_str = False
    bigint_pos: list[int] = []  # occurrence index -> needs int32
    occ = 0
    while i < n:
        c = sql[i]
        if c == "'":
            in_str = not in_str
            out.append(c); i += 1; continue
        if not in_str and sql.startswith("%s", i):
            # numeric context: LIMIT/OFFSET directly before the placeholder
            # (skip parens/whitespace). Keyword is a whole token, so LIKE
            # patterns/aliases containing "limit" cannot trigger this.
            k = i
            depth = 0
            while k > 0:
                k -= 1
                if sql[k] in " \t\r\n":
                    continue
                if sql[k] == "(":
                    depth += 1
                    continue
                break
            kw = sql[:k + 1].rstrip().upper()
            if kw.endswith("LIMIT") or kw.endswith("OFFSET"):
                out.append("CAST(%s AS bigint)")
                bigint_pos.append(occ)
            else:
                out.append("CAST(%s AS text)")
            occ += 1
            i += 2; continue
        out.append(c); i += 1
    if not bigint_pos and occ == 0:
        return sql, params
    newp = list(params)
    for o in bigint_pos:
        if o < len(newp):
            v = newp[o]
            if isinstance(v, int) and not isinstance(v, bool):
                newp[o] = Int32(int(v)) if Int32 is not None else v
    for o in range(occ):
        if o < len(newp) and o not in bigint_pos:
            v = newp[o]
            if isinstance(v, bool):
                newp[o] = "1" if v else "0"
    return "".join(out), tuple(newp)


class FtsConnection:
    """Wraps a backend connection; rewrites FTS5 virtual-table SQL to PG."""

    def __init__(self, base):
        self._base = base
        self._specs: tuple[tuple[str, str, str], ...] = ()  # set via set_spec()

    # -- passthroughs -------------------------------------------------------
    def __getattr__(self, name):
        return getattr(self._base, name)

    def cursor(self):
        return _FtsCursor(self._base.cursor(), lambda: self._specs)

    def execute(self, sql, parameters=()):
        c = self.cursor()
        return c.execute(sql, parameters)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def executescript(self, script):
        return self._base.executescript(script)

    # -- sqlite3.Connection context-manager parity --------------------------
    # Upstream uses `with self._conn:` (implicit transaction). Nested use is
    # safe: psycopg.Transaction is re-entrant via savepoints.
    def __enter__(self):
        base_enter = getattr(self._base, "__enter__", None)
        if base_enter is not None:
            base_enter()
        return self

    def __exit__(self, *exc):
        base_exit = getattr(self._base, "__exit__", None)
        if base_exit is not None:
            return base_exit(*exc)
        return False

    def close(self):
        _SPEC_REGISTRY.pop(id(self), None)
        return self._base.close()


class _FtsCursor:
    def __init__(self, cur, specs):
        # specs may arrive as an empty tuple at connect time (tables not yet
        # created); re-read the connection's current specs at use time so
        # ensure_external_content_fts running later still gets the rewrite.
        self._parent_specs_ref = specs
        self._cur = cur
        self._specs = specs
        self._extra_params = 0

    def _live_specs(self):
        specs = self.__dict__.get("_parent_specs_ref", self.__dict__.get("_specs", ()))
        if callable(specs):
            try:
                specs = specs()
            except Exception:
                specs = ()
        return specs

    def _rewrite_ts(self, sql: str) -> str:
        self._extra_params = 0
        specs_all = self._live_specs()
        if not specs_all:
            return sql
        if "match" not in sql.lower() and not re.search(r"\b\w+_fts\b", sql, flags=re.I):
            return sql
        out = _pre_pg(sql)
        specs = []
        for fts_table, content_table, indexed_col in specs_all:
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

            # 4. snippet() -> ts_headline over the source column. tsquery
            # comes from the WHERE's normalized expression (set below via
            # {TSQ} placeholder resolved in step 5); options use the
            # StartSel/StopSel form FTS5 callers expect visually.
            out = re.sub(rf"snippet\s*\(\s*(?:{alias}|{fts_table})\s*,[^)]*\)",
                         "SNIP" + src,
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
            # The captured expression may already carry %s from an earlier
            # per-spec pass; restore to sentinel tokens so _bind_sentinels
            # treats copies as COPIES (lcm_q($1)), not fresh placeholders.
            tsexpr = tsexpr.replace("%s", _SENTINEL)
            ref = 'ts_rank("' + tcol + '", ' + tsexpr + ')'
            # Resolve deferred snippet() markers (step 4) with the real
            # tsquery expression discovered from WHERE.
            def _snip_sub(m):
                return ("ts_headline('english', coalesce(\"" + m.group(1) + "\"::text, ''), "
                        + tsexpr + ", 'StartSel=>>>, StopSel=<<<, MaxFragments=1, MaxWords=40')")
            out = re.sub(r"SNIP(\w+)", _snip_sub, out)
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
        return out

    # FTS5 maintenance commands arrive as special INSERTs
    # (`INSERT INTO x(x, rank) VALUES('integrity-check', 1)`). With trigger-
    # maintained tsvectors there is no separate index to check: verify the
    # docsize parity invariant for integrity-check, and no-op the rest
    # (rebuild/optimize/merge).
    _MAINT = re.compile(
        r"""INSERT\s+INTO\s+"?(\w*fts)"?\s*\(\s*"?(\w+)"?\s*,\s*rank\s*\)\s+VALUES""",
        re.I)
    _MAINT_VALS = re.compile(r"""VALUES\s*\(\s*'([A-Za-z -]+)'""", re.I)

    def _intercept_maintenance(self, sql):
        m = self._MAINT.search(sql)
        if not m:
            return False
        table = m.group(1)
        content = m.group(2)
        if content == table:
            content = table[:-4] if table.endswith("_fts") else table
        mv = self._MAINT_VALS.search(sql)
        cmd = (mv.group(1).strip().lower() if mv else "")
        if cmd == "integrity-check":
            # db_bootstrap runs its structural check BEFORE calling this; the
            # remaining invariant is docsize-vs-content row parity.
            row = self._cur.execute(
                'SELECT (SELECT count(*) FROM "%s") - (SELECT count(*) FROM "%s_docsize")'
                % (content, table)).fetchone()
            diff = next(iter(row.values())) if isinstance(row, dict) else row[0]
            if diff != 0:
                import sqlite3 as _sqlite3
                raise _sqlite3.DatabaseError(
                    "fts integrity-check: %s docsize differs from content by %d"
                    % (table, diff))
            return True
        # rebuild / optimize / merge / * : triggers keep the tsvector column,
        # there is nothing FTS5-specific to do.
        return True

    def execute(self, sql, parameters=()):
        if self._intercept_maintenance(sql):
            self._rowcount = 0
            return self
        sql_in = sql
        # Protect FIRST: every literal `?` outside strings becomes the
        # internal sentinel token so the rewriter can track which
        # expressions are bound, then _bind_sentinels assigns real slots
        # (WHERE binds -> %s, copies -> lcm_q($1)).
        sql = _protect_params(_protect_percents(sql))
        sql = self._rewrite_ts(sql)
        # Param binding.
        #
        # Two kinds of sentinel survive rewriting:
        #   GENUINE binds  — the original SQL's own `?` placeholders. The
        #     rewriter NEVER adds these; they exist in the INPUT (sql_in).
        #     They bind positionally: psycopg assigns %s left-to-right, and
        #     the rewriter preserves the relative order of genuine
        #     placeholders, so the k-th genuine sentinel takes slot $k.
        #   COPIES — rewriter re-spellings of the tsquery expression
        #     (ts_rank/ts_headline). They must add NO placeholder: they
        #     become `lcm_q($k)` where $k is the genuine MATCH param's slot.
        # Classification: a sentinel is genuine iff its byte offset appears
        # in the sentinel-protected input; a copy is any sentinel not at an
        # input offset (offsets are stable because all rewrites preserve
        # non-placeholder text 1:1 at the sentinel level).
        if isinstance(parameters, dict):
            params = parameters
            final = _unprotect_params(sql)
        else:
            params = tuple(parameters)
            # literals are baked from the params themselves: each copy
            # becomes lcm_tsq(lit) with the MATCH value inlined (single-
            # quoted, doubled-quote escaped), so no placeholder and no
            # parameter-reference trickery is involved.
            final, params = _bind_sentinels_by_input(
                sql, params,
                orig_qs=_placeholders_outside_literals(sql_in, "?"))
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
            # RETURNING * would type the INSERT's literal binds through the
            # target columns; RETURNING store_id keeps them independent and
            # still serves lastrowid for the content-less-PK tables.
            aug = " RETURNING store_id" if t in _INSERT_ROWID_TABLES else ""
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

    def fetchone(self):
        from .postgres import _HybridRow
        r = self._cur.fetchone()
        return _HybridRow(r) if isinstance(r, dict) else r

    def fetchall(self):
        from .postgres import _HybridRow
        return [_HybridRow(r) if isinstance(r, dict) else r
                for r in self._cur.fetchall()]

    def fetchmany(self, n=None):
        from .postgres import _HybridRow
        rows = self._cur.fetchmany(n) if n else self._cur.fetchmany()
        return [_HybridRow(r) if isinstance(r, dict) else r for r in rows]

    def __iter__(self):
        from .postgres import _HybridRow
        def _gen():
            while True:
                r = self.fetchone()
                if r is None:
                    return
                yield r
        return _gen()

    def __getattr__(self, name):
        if name.startswith("__") or name in ("_cur", "_specs", "_extra_params",
                                            "_parent_specs_ref"):
            raise AttributeError(name)
        return getattr(self._cur, name)


def _qvar(q: str) -> str:
    return _SENTINEL if q.strip() in ("%s", "?", _SENTINEL) else q
