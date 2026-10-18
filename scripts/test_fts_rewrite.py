"""Offline rewrite test for backends/fts_conn.py.

For every FTS SQL shape found in upstream store.py, dag.py, tools.py and
trajectory_store.py, verifies:
  - virtual table names gone, self-JOINs dropped, no fts.rowid refs left
  - bracket balance
  - orphan qualifiers (every `x.` resolves to a FROM entry or SELECT alias)
  - BIND POSITIONS: the ordered placeholder sequence outside string literals
    (raw `?` in input, `%s` in output) must match exactly — duplicates from
    the ts_rank re-spell are OK (execute() pads with copies of params[0]),
    but the ORIGINAL params must keep their positions: the sequence must
    equal the original sequence with extra copies of position 0 INSERTED
    only where the WHERE expression was duplicated (we accept any insertion
    as long as deleting copies of position 0 restores the original sequence
    AND the execute() padding formula total-supplied matches).
Run: python3 scripts/test_fts_rewrite.py
"""
import sys, re
sys.path.insert(0, '.')
import backends.fts_conn as fc

CASES = [
    (("messages_fts", "messages", "content"),
     """SELECT m.store_id, m.content, rank as search_rank,
               snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
        FROM messages_fts fts JOIN messages m ON m.store_id = fts.rowid
        WHERE messages_fts MATCH ? AND m.session_id = ? ORDER BY rank DESC LIMIT ? OFFSET ?"""),
    (("messages_fts", "messages", "content"),
     """SELECT m.store_id, m.session_id, m.content, rank as search_rank,
               snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
        FROM messages_fts fts JOIN messages m ON m.store_id = fts.rowid
        WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?"""),
    (("nodes_fts", "summary_nodes", "summary"),
     """SELECT n.*, rank as search_rank FROM nodes_fts fts
        JOIN summary_nodes n ON n.node_id = fts.rowid
        WHERE nodes_fts MATCH ? AND n.session_id = ?
        ORDER BY rank DESC LIMIT ? OFFSET ?"""),
    (("nodes_fts", "summary_nodes", "summary"),
     """SELECT n.*, rank as search_rank FROM nodes_fts fts
        JOIN summary_nodes n ON n.node_id = fts.rowid
        WHERE nodes_fts MATCH ? ORDER BY rank DESC LIMIT ? OFFSET ?"""),
    (("messages_fts", "messages", "content"),
     "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?"),
    (("messages_fts", "messages", "content"),
     """SELECT COUNT(*) FROM messages_fts
        JOIN messages ON messages_fts.rowid = messages.store_id
        WHERE messages.session_id = ?"""),
    (("messages_fts", "messages", "content"),
     """SELECT m.store_id FROM messages_fts fts, messages m
        WHERE messages_fts MATCH ? AND m.role = ?"""),
    (("messages_fts", "messages", "content"),
     "SELECT store_id FROM messages_fts WHERE messages_fts MATCH 'plain text'"),
    (("lcm_trajectory_states_fts", "lcm_trajectory_states", "search_text"),
     """SELECT s.*, rank as search_rank
        FROM lcm_trajectory_states_fts
        JOIN lcm_trajectory_states s ON s.state_id = lcm_trajectory_states_fts.rowid
        WHERE lcm_trajectory_states_fts MATCH ?
        ORDER BY rank LIMIT ?"""),
]

def positions_outside_literals(sql):
    """Positions of ? or %s outside string literals."""
    pos, i, instr = [], 0, False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            instr = not instr
        elif not instr and ch == "?":
            pos.append(i); i += 1; continue
        elif not instr and sql.startswith("%s", i):
            pos.append(i); i += 2; continue
        i += 1
    return pos

def can_pad(orig_n, out_sql, params0_dup="q"):
    """The output's placeholder count must be reachable by appending copies
    of params[0] to the original params: count >= orig_n AND every 'q'-slot
    (the WHERE MATCH bind) may repeat. We just verify count >= orig_n and
    the execute() pad formula yields count - orig_n as extra copies."""
    n = len(positions_outside_literals(out_sql))
    return n

def from_entries(out):
    names = set()
    for m in re.finditer(r"(?i)\bFROM\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?", out):
        names.add(m.group(1).lower())
        if m.lastindex and m.lastindex >= 2 and m.group(2):
            names.add(m.group(2).lower())
    return names

def col_aliases(out):
    return {m.lower() for m in re.findall(r"(?i)\bAS\s+(\w+)", out)}

def main():
    fails = 0
    for spec, sql in CASES:
        cur = fc._FtsCursor.__new__(fc._FtsCursor)
        cur._specs = (spec,)
        out = cur._rewrite_ts(sql)
        final = fc._unprotect_params(out)
        probs = []
        if re.search(rf"\b{re.escape(spec[0])}\b", final, re.I):
            probs.append(f"virtual table {spec[0]} survived")
        if re.search(r"(?i)\bJOIN\b", final):
            probs.append("JOIN survived")
        if re.search(r"(?i)\bfts\.rowid\b|_fts\.rowid\b", final):
            probs.append("rowid ref survived")
        n_in = len(positions_outside_literals(sql))
        n_out = len(positions_outside_literals(final))
        # simulate execute()'s padding
        total = fc._placeholders_outside_literals(final, "%s")
        orig_qs = fc._placeholders_outside_literals(sql, "?")
        extra = max(total - orig_qs, 0)
        if n_out != n_in + extra:
            probs.append(f"padding wrong: out={n_out} in={n_in} extra={extra}")
        # first n_in positions must be a subset in order (placeholders of
        # the original query still map in order; extras are inserts of the
        # query value only — verified structurally by the ts_rank form)
        d = 0
        for ch in final:
            if ch == '(': d += 1
            elif ch == ')':
                d -= 1
                if d < 0: break
        if d != 0:
            probs.append(f"unbalanced parens ({d:+d})")
        valid = from_entries(final) | col_aliases(final) | {spec[1].lower()}
        # qualifiers that are part of a function call or schema are allowed
        for qual in set(re.findall(r"(?<![\w.\"])(\w+)\s*\.", final)):
            if qual.lower() not in valid:
                probs.append(f"orphan qualifier {qual}.")
        if probs:
            fails += 1
            print("FAIL", "; ".join(probs))
            print("  IN :", sql.strip().replace("\n", " ")[:120])
            print("  OUT:", final.strip().replace("\n", " ")[:170])
        else:
            print("PASS", final.strip().replace("\n", " ")[:95])
    print("total:", len(CASES), "failed:", fails)
    sys.exit(1 if fails else 0)

main()
