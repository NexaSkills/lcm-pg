#!/usr/bin/env python3
"""Backfill/ensure lcm_<profile> schemas on the shared lcm database.

Idempotent: scans ~/.hermes/profiles/*/ (plus the default profile) and runs
CREATE SCHEMA IF NOT EXISTS for each. Safe from cron. Schema naming follows
backends/postgres.py:schema_for_profile — one rule, no hardcoded lists.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backends.postgres import schema_for_profile  # noqa: E402
import psycopg  # noqa: E402
from psycopg import sql as psql  # noqa: E402


def main() -> int:
    dsn = os.environ.get("LCM_PG_DSN")
    if not dsn:
        print("LCM_PG_DSN not set", file=sys.stderr)
        return 1
    home = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
    homes = [home]  # default profile
    profiles = home / "profiles"
    if profiles.is_dir():
        homes.extend(sorted(p for p in profiles.iterdir() if p.is_dir()))
    with psycopg.connect(dsn, autocommit=True) as conn:
        for h in homes:
            schema = schema_for_profile(str(h))
            conn.execute(psql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(psql.Identifier(schema)))
            print(f"{h}: {schema}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
