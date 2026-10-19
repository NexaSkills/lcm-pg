#!/usr/bin/env python3
"""Phase 3: migrate one profile's LCM SQLite db into its PostgreSQL schema.

Usage: migrate_sqlite_to_pg.py <sqlite_db_path> [--schema NAME] [--dry-run]

Strategy (YAGNI):
  1. Open the target schema via the fork's PG store (LCM_BACKEND=postgres)
     and run the store's own bootstrap once so canonical tables exist.
  2. Per data table, COPY sqlite rows whose PK is absent in PG (idempotent).
  3. Align sequences above migrated ids. FTS is rebuilt by the store
     (ensure_external_content_fts / 'rebuild') on first real use.

Complex sqlite values (dict/list) are stored as JSON text, matching what
the store itself writes into sqlite TEXT columns.
"""
import json, os, re, sqlite3, sys

FORK = os.environ.get("LCM_PG_FORK", "/home/mybuddy/.hermes/forks/lcm-pg")
TABLES = (
    "messages", "metadata", "summary_nodes",
    "lcm_rollups", "lcm_rollup_sources", "lcm_rollup_state",
    "lcm_rollup_invalidations", "lcm_migration_state", "lcm_lifecycle_state",
)

def schema_for_path(path: str) -> str:
    parent = os.path.basename(os.path.normpath(os.path.dirname(os.path.abspath(path))))
    if parent in ("", ".hermes"):
        parent = "default"
    return "lcm_" + re.sub(r"[^a-z0-9_]", "_", parent.lower())

def main() -> int:
    if not sys.argv[1:2]:
        print(__doc__); return 2
    src_path = os.path.abspath(sys.argv[1])
    args = sys.argv[2:]
    dry = "--dry-run" in args
    schema = schema_for_path(src_path)
    if "--schema" in args:
        schema = args[args.index("--schema") + 1]
    assert re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema), schema

    os.environ["LCM_BACKEND"] = "postgres"
    # default = self-contained bundle cluster (never the shared system PG:
    # an empty env used to point here and PoolTimeout on a foreign cluster)
    os.environ.setdefault("LCM_PG_DSN",
        f"postgresql:///lcm?host={os.path.expanduser('~')}/lcm-pg/run"
        "&port=54329&user=lcm01")
    os.environ["LCM_PG_SCHEMA"] = schema
    sys.path.insert(0, os.path.dirname(FORK))  # so 'lcm_pg' pkg is importable

    # 1. canonical tables via the store's own bootstrap (fresh-store path)
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "lcm_pg", os.path.join(FORK, "__init__.py"),
        submodule_search_locations=[FORK])
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["lcm_pg"] = _pkg
    _spec.loader.exec_module(_pkg)
    _store_mod = importlib.import_module("lcm_pg.store")
    os.makedirs(os.path.join(FORK, "_mig_scratch"), exist_ok=True)
    _store = _store_mod.MessageStore(os.path.join(FORK, "_mig_scratch", "bootstrap.db"))
    # other store classes create their own tables lazily on construction:
    for mod_name, cls_name in (("dag", "SummaryDAG"),):
        try:
            m = importlib.import_module(f"lcm_pg.{mod_name}")
            if cls_name:
                cls = getattr(m, cls_name, None)
                if cls:
                    try: cls(os.path.join(FORK, "_mig_scratch", "bootstrap.db")).close()
                    except Exception: pass
        except Exception:
            pass
    # rollup + migration + lifecycle state tables come from db_bootstrap:
    try:
        dbs = importlib.import_module("lcm_pg.db_bootstrap")
        for fn in ("ensure_migration_state_table", "ensure_lifecycle_state_table",
                   "ensure_temporal_rollup_tables"):
            getattr(dbs, fn)(_store._conn)
    except Exception:
        pass
    try:
        from backends import fts_pg as _fp
        _fp.ensure_fts(_store._conn, "messages", "content")
        _fp.ensure_fts(_store._conn, "summary_nodes", "summary")
    except Exception:
        pass
    _store.close()

    import psycopg
    pg = psycopg.connect(os.environ["LCM_PG_DSN"], autocommit=True, options=f"-c search_path={schema}")
    cur = pg.cursor()

    def align_sequences():
        """setval every owned serial to max(col) (nextval -> max+1)."""
        seqrows = cur.execute("""
            SELECT s.relname, t.relname, a.attname
            FROM pg_class s
            JOIN pg_depend d ON d.objid=s.oid AND d.deptype='a'
            JOIN pg_class t ON t.oid=d.refobjid
            JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
            JOIN pg_namespace n ON n.oid=t.relnamespace
            WHERE n.nspname=%s AND s.relkind='S'""", (schema,)).fetchall()
        errs = {}
        for row in seqrows:
            seq, tbl, col = row[0], row[1], row[2]
            try:
                mx = cur.execute(f'SELECT coalesce(max("{col}"),0) FROM "{schema}"."{tbl}"').fetchone()[0]
                cur.execute("SELECT setval(%s::regclass, %s)", (f'"{schema}"."{seq}"', max(int(mx), 1)))
            except Exception as e:
                errs[f"seq:{seq}"] = f"ERR {str(e)[:50]}"
        return errs

    # MUST run before any COPY: inserting summary_nodes fires
    # _trg_lcm_rollup_node_insert, which takes a fresh event_id from
    # lcm_rollup_invalidations' sequence — if that sequence lags the rows
    # already migrated by a previous run, the trigger hits a pkey collision
    # mid-COPY and aborts the whole table (observed on buttercup delta, 2026-10-20).
    report = align_sequences()

    # sqlite source: prefer live-with-WAL read via backup into memory-safe temp copy
    tmp = os.path.join(FORK, "_migrate_src.tmp")
    s0 = sqlite3.connect(src_path)
    s1 = sqlite3.connect(tmp)
    # Salvage class: legacy corrupt lcm.db files can hold invalid-UTF-8 TEXT
    # (seen in tool_calls JSON on warren debbie). Strict decode aborts the
    # whole table read; lossy decode keeps every row, U+FFFD where bytes died.
    for _c in (s0, s1):
        try:
            _c.text_factory = lambda b: b.decode("utf-8", "replace")
        except sqlite3.ProgrammingError:
            pass
    s0.backup(s1)
    s0.close()
    sconn = s1

    report = {}
    for table in TABLES:
        try:
            scols = [r[1] for r in sconn.execute(f'PRAGMA table_info("{table}")')]
            if not scols:
                report[table] = "absent in source"
                continue
            srows = sconn.execute(f'SELECT * FROM "{table}"').fetchall()
        except Exception as e:
            report[table] = f"source ERR {str(e)[:60]}"
            continue
        exists = cur.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s AND c.relkind='r'", (schema, table)).fetchone()
        if not exists:
            report[table] = "absent in PG (store bootstrap will create)"
            continue
        pcols = [r[0] for r in cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table))]
        lower_p = {c.lower(): c for c in pcols}
        common = [c for c in scols if c.lower() in lower_p]
        pcols_named = [lower_p[c.lower()] for c in common]
        if not common:
            report[table] = "no common columns"
            continue
        # PK dedupe
        pkrow = cur.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] "
            "WHERE i.indrelid=(quote_ident(%s)||'.'||quote_ident(%s))::regclass AND i.indisprimary",
            (schema, table)).fetchone()
        sidx = {c: scols.index(c) for c in common}
        rows = srows
        if pkrow and pkrow[0] in common:
            # Drop source rows with NULL in the PK column: unreachable via the
            # store's INSERT(key,value) path, so they are corruption residue
            # (btrfs-damage survivors) that PG's NOT NULL/PK would reject and
            # abort the whole COPY run on. Report them so they are not silent.
            # Filter srows (the real COPY source), not just `rows`.
            pi = scols.index(pkrow[0])
            nulls = [r for r in srows if r[pi] is None]
            if nulls:
                report[table + "_skipped_null_pk"] = len(nulls)
                srows = [r for r in srows if r[pi] is not None]
            have = {r[0] for r in cur.execute(
                f'SELECT "{pkrow[0]}" FROM "{schema}"."{table}"')}
            rows = [r for r in srows if r[pi] not in have]
        if dry:
            report[table] = f"would copy {len(rows)} (source {len(srows)})"
            continue
        def _adapt(v):
            return v if v is None or isinstance(v, (int, float, str, bytes)) \
                else json.dumps(v, default=str)
        n = 0
        if rows:
            collist = ", ".join('"%s"' % c for c in pcols_named)
            with cur.copy(f'COPY "{schema}"."{table}" ({collist}) FROM STDIN') as cp:
                for r in rows:
                    cp.write_row(tuple(_adapt(r[sidx[c]]) for c in common))
                    n += 1
        report[table] = f"copied {n} (source {len(srows)})"

    # 3. sequences: final re-align after the COPY (also aligns sequences of
    # tables that only grew during this run)
    if not dry:
        report.update(align_sequences())

    sconn.close()
    os.unlink(tmp)
    pg.close()
    print(f"schema={schema} dry={dry}")
    for k, v in report.items():
        print(f"  {k}: {v}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
