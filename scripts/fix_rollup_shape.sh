#!/usr/bin/env bash
# Ensure the rollup tables are the canonical shape in EVERY profile schema.
#
# engine.__init__ hard-refuses ('temporal rollup schema incomplete after
# ensure: column-shape:...') when lcm_rollup_{sources,state,invalidations}
# carry an older column shape (pre-rollup-schema migration on this host).
# db_bootstrap.ensure_temporal_rollup_tables() includes the canonicalizer —
# it is idempotent and is the SAME code the engine runs — so run it
# directly over every schema. Done inside ensure_profile_bootstrap.py
# (ensure_temporal_rollup_tables is already in ENSURE_FNS); this script is
# for manual repair when an engine refuses to start.
set -euo pipefail
B="${LCM_PG_HOME:-$HOME/lcm-pg}"
[ -x "$B/venv/bin/python" ] || { echo "no $B/venv" >&2; exit 1; }
"$B/venv/bin/python" "$(dirname "$0")/ensure_profile_bootstrap.py" "$@"
echo "== post-check (column shapes):"
PGHOST=$B/run PGPORT=54329 PGUSER=lcm01 $B/pg/bin/psql -d lcm -Atc "
select n.nspname,
  (select count(*) from information_schema.columns c where c.table_schema=n.nspname and c.table_name='lcm_rollup_sources' and c.column_name='rollup_id') src_rollup_id,
  (select count(*) from information_schema.columns c where c.table_schema=n.nspname and c.table_name='lcm_rollup_state' and c.column_name='period_kind') state_kind,
  (select count(*) from information_schema.columns c where c.table_schema=n.nspname and c.table_name='lcm_rollup_invalidations' and c.column_name='created_at') inv_created
from pg_namespace n where n.nspname like 'lcm_%' order by 1"
