#!/usr/bin/env python3
"""lcm_deploy_migrations.py — apply+track the lcm-pg solution's DB migrations per schema.

Idempotent and host-agnostic:
  * DSN/paths from env (LCM_PG_DSN, HERMES_HOME) — no host literals.
  * Schemas enumerated from ~/.hermes/profiles/*/ (via the repo's own
    ensure_schemas.py naming rule) UNION pg_namespace lcm_% (minus probes).
  * Each migration = the repo's existing idempotent repair scripts, run
    per schema with LCM_PG_SCHEMAS=<schema>.
  * Tracking = one marker table per schema (lcm_deploy_migrations), so the
    DB itself records what was applied at which git ref; re-runs no-op.

Core table bootstrap/upgrade (SCHEMA_VERSION) is NOT done here — the plugin's
db_bootstrap does that on engine start; this script only layers the parity +
ops migrations the engine does not run.

Usage:  python3 lcm_deploy_migrations.py [--ref <git-ref>] [--force] [--list] [--dry-run]
Exit:   0 all ok/skipped, 1 any migration failed (per-schema detail printed).
"""
import argparse, os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = [
    # (migration id, kind, script)  kind: sql|py|sh   applied via LCM_PG_SCHEMAS per schema
    ("parity",             "sh", "scripts/schema_parity.sh"),
    ("fix_summary_nodes_content_tsv", "sql", "scripts/fix_summary_nodes_content_tsv.sql"),
    ("fix_fts_shim_and_dup_triggers", "sh", "scripts/fix_fts_shim_and_dup_triggers.sh"),
    ("fix_guard_triggers",  "py", "scripts/fix_guard_triggers.py"),
    ("fix_rollup_shape",    "sh", "scripts/fix_rollup_shape.sh"),
    ("fix_sequences",       "sql", "scripts/fix_sequences.sql"),
    ("drop_zombie_schemas", "sh", "scripts/drop_zombie_schemas.sh"),
    ("drop_zombie_rows",    "sh", "scripts/drop_zombie_rows.sh"),
]

def _dsn():
    dsn = os.environ.get("LCM_PG_DSN")
    if not dsn:
        sys.exit("LCM_PG_DSN not set (required; resolved per-host by systemd/.env)")
    return dsn

def _py():
    # prefer the host's lcm-pg venv python (has psycopg); fall back to current
    v = Path.home() / "lcm-pg/venv/bin/python"
    return str(v) if v.exists() else sys.executable

def _connect():
    import psycopg
    return psycopg.connect(_dsn(), autocommit=True)

def _schemas(conn):
    conn.execute("CREATE SCHEMA IF NOT EXISTS public")
    rows = conn.execute(
        "select nspname from pg_namespace where nspname like 'lcm\\_%' "
        "and nspname not like 'lcm\\_test\\_%' and nspname not like 'lcm\\_probe%' "
        "order by 1").fetchall()
    return [r[0] for r in rows]

def _ensure_marker(conn, schema):
    conn.execute(f'create table if not exists "{schema}".lcm_deploy_migrations ('
                 "migration text primary key, applied_at timestamptz default now(), git_ref text)")

def _applied(conn, schema, mig):
    try:
        r = conn.execute(
            f'select git_ref from "{schema}".lcm_deploy_migrations where migration=%s', (mig,)).fetchone()
        return r[0] if r else None
    except Exception:
        return None  # marker table missing (fresh schema) -> treat as not applied

def _run(mig, schema, dry):
    kind, script = MIGRATIONS_DICT[mig]
    path = REPO / script
    if not path.exists():
        return False, f"missing {script}"
    env = dict(os.environ, LCM_PG_SCHEMAS=schema)
    if kind == "sql":
        # psql reads no LCM_PG_DSN: point it at the same cluster via libpq env.
        # Without this it defaults to /run/postgresql and dies with
        # 'role "<osuser>" does not exist' while psycopg steps succeed.
        try:
            from psycopg.conninfo import conninfo_to_dict
            kv = {k: v for k, v in conninfo_to_dict(_dsn()).items() if v}
        except Exception:
            kv = {}
        for k in ("host", "port", "dbname", "user"):
            if kv.get(k):
                env[k.upper()] = str(kv[k])
        env.setdefault("PGHOST", str(Path.home() / "lcm-pg/run"))
        env.setdefault("PGPORT", "54329")
        env.setdefault("PGUSER", "lcm01")
        env.setdefault("PGDATABASE", "lcm")
        cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)]
    elif kind == "py":
        cmd = [_py(), str(path)]
    else:
        cmd = ["bash", str(path)]
    if dry:
        return True, "dry-run"
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    ok = p.returncode == 0
    return ok, (p.stdout.strip()[-300:] + "\n" + p.stderr.strip()[-300:]).strip()

MIGRATIONS_DICT = {mid: (k, s) for mid, k, s in MIGRATIONS}
# whole-DB helper invoked as a pre-pass (not stamped; idempotent by design)
MIGRATIONS_DICT["ensure_bootstrap"] = ("py", "scripts/ensure_profile_bootstrap.py")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=os.environ.get("LCM_DEPLOY_REF", "unknown"))
    ap.add_argument("--force", action="store_true", help="re-run migrations even if stamped")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.list:
        for mid, k, s in MIGRATIONS:
            print(f"{mid}\t{k}\t{s}")
        return 0
    conn = _connect()
    schemas = _schemas(conn)
    # safety-net pre-pass: run the whole-DB bootstrap over EVERY existing
    # lcm_% schema (idempotent; the same script fix_rollup_shape invokes).
    # A schema that never had a profile dir (retired/renamed profile) can
    # lack rollup/assertion tables entirely — later per-schema migrations
    # then churn or no-op against tables that don't exist.
    okb, detailb = _run("ensure_bootstrap", "__all__", a.dry_run)
    print(("OK   " if okb else "FAIL ") + "pre-pass ensure_bootstrap | " + detailb[-200:])
    fails = 0 if okb else 1
    for schema in schemas:
        for mid, _k, _s in MIGRATIONS:
            have = _applied(conn, schema, mid)
            if have == str(a.ref) and not a.force:
                print(f"SKIP {schema} {mid} (stamped @{have})")
                continue
            if mid == "drop_zombie_schemas":
                # whole-database scope: the script itself enumerates every
                # schema and never drops the one it is stamped into. Run it
                # once, stamped into lcm_default only.
                if schema != "lcm_default":
                    continue
                ok, detail = _run(mid, schema, a.dry_run)
            else:
                ok, detail = _run(mid, schema, a.dry_run)
            if ok:
                if not a.dry_run:
                    _ensure_marker(conn, schema)
                    conn.execute(f'insert into "{schema}".lcm_deploy_migrations(migration, git_ref) '
                                 "values (%s,%s) on conflict (migration) do update set applied_at=now(), git_ref=excluded.git_ref",
                                 (mid, str(a.ref)))
                print(f"OK   {schema} {mid}" + ("" if not detail else f" | {detail[:120]}"))
            else:
                fails += 1
                print(f"FAIL {schema} {mid} | {detail[:300]}")
    conn.close()
    print(f"migrations: {len(schemas)} schemas x {len(MIGRATIONS)} migrations, failures={fails}")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
