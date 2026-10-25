#!/usr/bin/env python3
"""Restore the canonical embedding shape for mis-created lcm_* schemas.

Root cause (verified 2026-10-21 on coordination-host): some schemas' embedding tables
were created by the sqlite-DDL *translation* fallback during earlier repair
work. The translator renders PRIMARY KEY members with explicit column-level
NOT NULL, so PG ends up with standalone <t>_<col>_not_null pg_constraint
rows on PK columns that the canonical bootstrap DDL never declared. The
fork's PRAGMA emulation reconstructs sqlite DDL from the catalog and emits
NOT NULL wherever attnotnull holds, which makes those PK members read
notnull=1 — but verify_embedding_schema's expected shapes require 0 for PK
members. verify_temporal_rollup_schema does not conflict because rollup
tables' NOT NULLs are genuine canonical declarations.

Repair: drop + recreate the four embedding tables from the CANONICAL sqlite
DDL in db_bootstrap (CREATE statements copied verbatim) executed through
the fork's own PG translation path — exactly what a fresh bootstrap does.
All rows are preserved (profile registrations, meta rows, vectors).

Idempotent: a schema whose tables already verify clean is skipped.
"""
from __future__ import annotations
import importlib.util, os, re, sys
from pathlib import Path

FORK = Path(__file__).resolve().parent.parent
os.environ.setdefault("LCM_BACKEND", "postgres")
DSN = os.environ.get("LCM_PG_DSN") or (
    "postgresql:///lcm?host=" + str(Path.home() / "lcm-pg/run") +
    "&port=54329&user=lcm01")

_spec = importlib.util.spec_from_file_location(
    "lcmpg", str(FORK / "__init__.py"), submodule_search_locations=[str(FORK)])
assert _spec and _spec.loader
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lcmpg"] = _pkg
_spec.loader.exec_module(_pkg)

import importlib  # noqa: E402
pg = importlib.import_module("lcmpg.backends.postgres")  # noqa: E402
dbb = importlib.import_module("lcmpg.db_bootstrap")  # noqa: E402

TABLES = ("lcm_embedding_profile", "lcm_embedding_meta",
          "lcm_embedding_vectors", "lcm_embedding_binary")

src = (FORK / "db_bootstrap.py").read_text()

schemas = sys.argv[1:]
if not schemas:
    schemas = os.environ.get("SCHEMAS", "").split()
if not schemas:
    c0 = pg.connect(DSN, "information_schema")
    schemas = [r[0] for r in c0.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'lcm\\_%'"
        " ESCAPE chr(92) AND nspname NOT LIKE '%deleted'"
        " AND nspname NOT LIKE '%corrupt%' ORDER BY 1").fetchall()]

for schema in schemas:
    conn = pg.connect(DSN, schema)
    errs = dbb.verify_embedding_schema(conn)
    if not errs:
        print(f"[{schema}] already canonical")
        continue
    # capture rows (missing tables capture as empty; the canonical CREATE
    # below brings them back and nothing is lost).
    data = {}
    for t in TABLES:
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = %s"
            " ORDER BY ordinal_position", (t,)).fetchall()]
        if not cols:
            data[t] = ([], [])
            continue
        rows = conn.execute(
            'SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in cols), t)
            ).fetchall()
        data[t] = (cols, rows)
    if data is None:
        continue
    # rebuild via the canonical DDL through the fork's own translator
    conn.execute("SET LOCAL lock_timeout = '20s'")
    for t in TABLES:
        m = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)? %s \((.*?)\n\s*\);" % t, src,
            re.S)
        assert m, f"canonical DDL not found for {t}"
        conn.execute("DROP TABLE IF EXISTS %s CASCADE" % t)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s (%s)" % (t, m.group(1)))
    # restore rows (explicit column lists; PKs are non-null by construction)
    for t in TABLES:
        cols, rows = data[t]
        if not rows:
            continue
        collist = ", ".join('"%s"' % c for c in cols)
        ph = ", ".join(["%s"] * len(cols))
        for r in rows:
            conn.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (t, collist, ph),
                         tuple(r))
    # indexes/vecs: canonical ensure adds them idempotently
    dbb.ensure_embedding_tables(conn)  # indexes are included
    after = dbb.verify_embedding_schema(conn)
    print(f"[{schema}] rebuilt {', '.join(TABLES)}"
          f" ({sum(len(v[1]) for v in data.values())} rows;"
          f" verify after: {after or 'OK'})")
