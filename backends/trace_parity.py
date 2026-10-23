"""sqlite3 set_trace_callback parity for the PG backend (lcm-pg).

sqlite's trace callback reports the CALLER's SQL with bound parameters
rendered as literals ('... a=5 LIMIT 20' — verified against stdlib). The
hooks live in postgres.py (_Cursor.execute) and fts_conn.py
(_FtsCursor.execute); this module owns the shared renderer and the
suppress sentinel so the two layers never double-report one statement.
"""


class _SuppressTrace:
    __slots__ = ()

    def __repr__(self):
        return "<trace-suppressed>"


SUPPRESS = _SuppressTrace()


def _val(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "X'" + bytes(v).hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def render(sql, parameters):
    """Render `sql` sqlite-trace style: `?` placeholders replaced by the
    bound literals (quote-aware; never raises — degrades to raw sql)."""
    try:
        if parameters is SUPPRESS:
            return None
        if parameters is None:
            return sql
        if isinstance(parameters, dict):
            import re as _re
            return _re.sub(r":(\w+)", lambda m: _val(parameters.get(m.group(1))), sql)
        vals = tuple(parameters)
        out, i, vi, n, q = [], 0, 0, len(vals), None
        while i < len(sql):
            ch = sql[i]
            if q:
                out.append(ch)
                if ch == q:
                    if i + 1 < len(sql) and sql[i + 1] == q:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    q = None
                i += 1
                continue
            if ch in ("'", '"'):
                q = ch
                out.append(ch)
                i += 1
                continue
            if ch == "?" and vi < n:
                out.append(_val(vals[vi]))
                vi += 1
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    except Exception:
        return sql
