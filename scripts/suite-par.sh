#!/bin/bash
# lcm-pg parallel suite runner (postgres backend)
# Usage: suite-par.sh [n_workers]   (default 6)
# Requires: lcm-pg cluster with max_connections>=200 (verify: SHOW max_connections)
set -u
N=${1:-6}
export LCM_BACKEND=postgres
export LCM_PG_DSN="postgresql:///lcm?host=$HOME/lcm-pg/run&port=54329&user=lcm01"
export PYTHONPATH="$HOME/.hermes/forks/lcm-pg:$HOME/.hermes/hermes-agent"
cd "$HOME/.hermes/forks/lcm-pg" || exit 1
~/lcm-pg/venv/bin/python -m pytest tests/ -q -p no:cacheprovider \
  -n "$N" --dist loadfile --timeout=60 --timeout-method=thread --tb=line -o addopts=
RC=$?
# teardown: drop any leaked per-test isolation schemas (conftest normally drops them itself)
PGHOST="$HOME/lcm-pg/run" PGPORT=54329 PGUSER=lcm01 psql -d lcm -Atqc \
  "SELECT 'DROP SCHEMA IF EXISTS \"'||schema_name||'\" CASCADE' FROM information_schema.schemata WHERE schema_name LIKE 'lcm\_t\_%'" \
  | while read -r s; do PGHOST="$HOME/lcm-pg/run" PGPORT=54329 PGUSER=lcm01 psql -d lcm -qc "$s" >/dev/null 2>&1; done
echo "SUITE_RC=$RC"
exit $RC
