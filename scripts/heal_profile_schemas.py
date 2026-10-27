#!/usr/bin/env python3
"""heal_profile_schemas.py — close the inventory gap between profile dirs and
deployed schemas (fleet incident skilltest, 2026-10-27).

The deploy's migration runner enumerates schemas from pg_namespace BEFORE any
bootstrap runs. A profile dir whose schema does not exist yet (created after
the last deploy, or made by a first-open of the store without db_bootstrap)
is therefore invisible to that loop: drop_zombie_schemas never touches it
(profile keep rule) and no parity/rowid/guard migration is ever applied to it
— while check_all's verifiers enumerate pg_namespace and FAIL forever on the
half-built DDL, all with deploy RESULT: OK.

This script is the tiny deterministic heal step:
  1. enumerate EVERY profile dir (main ~/.hermes + $HERMES_HOME, deduped),
  2. CREATE SCHEMA IF NOT EXISTS lcm_<profile> (the plugin's naming rule:
     'lcm_' + profile-id; no LCM_PG_SCHEMA pin is ever read or written),
  3. run ensure_profile_bootstrap.py (full canonical ensure_* DDL over every
     profile schema; idempotent, the same script embed_timer runs),
  4. run lcm_deploy_migrations.py (the stamp loop no-ops where migrations are
     already stamped, so healed schemas are the only ones that move).

Idempotent; never drops or rewrites data. Called by lcm_update.sh (step 2a)
and by lcm_check_all.py before verification. Exit 0 = nothing healed or heal
ok; exit 1 = a sub-step failed (check_all still reports ground truth).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORK = HERE.parent


def _py() -> str:
    v = Path.home() / "lcm-pg/venv/bin/python"
    return str(v) if v.exists() else sys.executable


def _profile_dirs() -> list[Path]:
    homes = []
    for base in (Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))),
                 Path.home() / ".hermes"):
        pdir = base / "profiles"
        if pdir.is_dir():
            homes += [p for p in sorted(pdir.iterdir())
                      if p.is_dir() and not p.name.startswith(".")]
    # dedup by profile name, keep first
    seen: set[str] = set()
    out: list[Path] = []
    for p in homes:
        if p.name not in seen:
            seen.add(p.name)
            out.append(p)
    return out


def main() -> int:
    dsn = os.environ.get("LCM_PG_DSN")
    if not dsn:
        dsn = f"postgresql://lcm01@lcm-pg/lcm?host={Path.home()}/lcm-pg/run&port=54329"
        os.environ["LCM_PG_DSN"] = dsn

    import psycopg
    from psycopg import sql as _sqlmod
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        existing = {r[0] for r in conn.execute(
            "select nspname from pg_namespace where nspname like 'lcm\\_%'").fetchall()}
        missing = []
        for prof in _profile_dirs():
            schema = f"lcm_{prof.name}"
            if schema in existing:
                continue
            conn.execute(_sqlmod.SQL('CREATE SCHEMA IF NOT EXISTS "{}"')
                         .format(_sqlmod.Identifier(schema)))
            print(f"created schema {schema} (profile dir {prof})")
            missing.append(schema)
        # lcm_default must always exist (the root home's schema)
        if "lcm_default" not in existing:
            conn.execute('CREATE SCHEMA IF NOT EXISTS "lcm_default"')
            print("created schema lcm_default")
            missing.append("lcm_default")
    finally:
        conn.close()

    # canonical ensure_* DDL over every profile schema (idempotent, cheap)
    r = subprocess.run([_py(), str(HERE / "ensure_profile_bootstrap.py")],
                       capture_output=True, text=True, timeout=600)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    print(f"ensure_profile_bootstrap rc={r.returncode} | {' / '.join(tail)}")
    if r.returncode != 0:
        return 1

    # migrations: the stamp loop no-ops where stamped; newly created schemas
    # have empty marker tables, so --force only matters to them anyway.
    r = subprocess.run([_py(), str(HERE / "lcm_deploy_migrations.py")],
                       capture_output=True, text=True, timeout=1800)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    print(f"migrations rc={r.returncode}"
          + (f" (new: {','.join(missing)})" if missing else "")
          + f" | {' / '.join(tail)}")
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
