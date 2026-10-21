#!/usr/bin/env python3
"""Make _sqlite_ddl_for emit sqlite-faithful NOT NULL for PK members.

sqlite's PRAGMA table_info reports `notnull` = "the column's DDL line
carried NOT NULL". PG's attnotnull cannot be used directly because PG sets
it for every PK member regardless of the declarative form. The fork's
canonical bootstrap DDL is split exactly two ways:
  - PK members with an inline `NOT NULL` declaration (rollup tables)
  - PK members with NO declaration (embedding tables; the PRIMARY KEY
    clause alone provides non-nullness)
PG records both forms identically (attnotnull + a <t>_<col>_not_null
constraint row from the PG-DDL emitter), so the catalog cannot tell them
apart. But the EMBEDDED sqlite DDL inside db_bootstrap.py can: this patch
loads the canonical CREATE TABLE text once, and a PK member counts as
declared only if its column line carries NOT NULL before the next comma.

Non-PK columns keep attnotnull (there the flag is the declaration).
The AUTOINCREMENT rowid-alias rule (single sequence-backed INTEGER PK) is
untouched.

Idempotent: markers checked. --revert supported.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

B = Path.home() / "lcm-pg"
CANDIDATES = [B / "plugins" / "lcm-pg",
              Path.home() / ".hermes" / "forks" / "lcm-pg"]
START = "# >>> lcm-pg pragma-notnull patch >>>\n"
END = "# <<< lcm-pg pragma-notnull patch <<<\n"

HELPER = '''
# >>> lcm-pg pragma-notnull patch >>>
import os as _pn_os

_CANON_DDL_CACHE: dict[str, dict[str, bool]] = {}


def _canonical_declared_notnull(table: str) -> dict[str, bool]:
    """attname -> column line declares NOT NULL, from db_bootstrap's own
    canonical sqlite DDL (single source of truth for expected shapes)."""
    cached = _CANON_DDL_CACHE.get(table)
    if cached is not None:
        return cached
    out: dict[str, bool] = {}
    try:
        here = _pn_os.path.dirname(_pn_os.path.abspath(__file__))
        src = open(_pn_os.path.join(here, "..", "db_bootstrap.py"),
                   encoding="utf-8").read()
        import re as _pn_re
        m = _pn_re.search(
            r"CREATE TABLE (?:IF NOT EXISTS )?" + _pn_re.escape(table) +
            r"\\s*\\((.*?)\\n\\s*\\)", src, _pn_re.S)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip().rstrip(",")
                if not line or line.upper().startswith(
                        ("PRIMARY KEY", "CHECK", "UNIQUE", "FOREIGN KEY",
                         "CONSTRAINT")):
                    continue
                mm = _pn_re.match(r'(?:"?\\w+"?|\\w+)\\s+\\w+(\\s+.*)?', line)
                if not mm:
                    continue
                name = _pn_re.match(r'"?(\\w+)"?', line).group(1)
                out[name] = bool(
                    line.upper().find(" NOT NULL") >= 0
                    or line.upper().endswith("NOT NULL"))
    except Exception:
        out = {}
    _CANON_DDL_CACHE[table] = out
    return out
# <<< lcm-pg pragma-notnull patch <<<
'''

PATCH = '''def _sqlite_ddl_for(table: str, conn=None):'''

COND_OLD = '''            if notnull and not (autoincrement_pk and pkord == 1):
                bits.append("NOT NULL")'''
COND_NEW = '''            if notnull and not (autoincrement_pk and pkord == 1):
                # PK members: trust the canonical DDL declaration (PG's
                # attnotnull fires for every PK member, declared or not).
                # Non-PK columns: attnotnull IS the declaration.
                if pkord:
                    if _canonical_declared_notnull(table).get(name, False):
                        bits.append("NOT NULL")
                else:
                    bits.append("NOT NULL")'''


def target_file():
    env = os.environ.get("LCM_PG_FILE", "").strip()
    if env:
        return Path(env)
    for t in CANDIDATES:
        f = t / "backends" / "postgres.py"
        if f.exists() and "def _sqlite_ddl_for(" in f.read_text():
            return f
    return CANDIDATES[0] / "backends" / "postgres.py"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--file", default="")
    args = ap.parse_args()
    f = Path(args.file) if args.file else target_file()
    if not f.exists():
        print(f"ERROR: no lcm-pg backends/postgres.py at {f}", file=sys.stderr)
        return 2
    src = f.read_text()
    if args.revert:
        if START not in src:
            print(f"[{f}] no patch present")
            return 0
        i = src.index(START); j = src.index(END) + len(END)
        src = src[:i] + src[j:]
        try:
            k = src.index(COND_NEW); src = src[:k] + COND_OLD + src[k + len(COND_NEW):]
        except ValueError:
            pass
        f.write_text(src)
        print(f"[{f}] reverted")
        return 0
    if START in src:
        if COND_NEW in src:
            print(f"[{f}] patch already present (skip)")
            return 0
        k = src.index(COND_OLD); src = src[:k] + COND_NEW + src[k + len(COND_OLD):]
        f.write_text(src)
        print(f"[{f}] condition updated")
        return 0
    assert PATCH in src, "unexpected postgres.py shape (no _sqlite_ddl_for)"
    src = src.replace(PATCH, HELPER + "\n" + PATCH, 1)
    assert COND_OLD in src, "unexpected NOT NULL condition text"
    src = src.replace(COND_OLD, COND_NEW, 1)
    f.write_text(src)
    import py_compile
    py_compile.compile(str(f), doraise=True)
    print(f"[{f}] canonical-DDL notnull rule installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
