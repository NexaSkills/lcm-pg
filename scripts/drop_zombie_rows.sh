#!/usr/bin/env bash
# drop_zombie_rows: delete lcm_rollups rows whose scope has NO messages in the
# same schema (test/probe residue: e.g. lcm_default's scope='s'
# period_start='d1' row, stuck in 'building' forever with an empty lease).
# Idempotent; stamped per-schema via lcm_deploy_migrations. Real scopes (with
# message rows) are never touched.
set -uo pipefail
PSQL="${PSQL:-/home/mybuddy/lcm-pg/pg/bin/psql}"
PGHOST_DIR="${PGHOST_DIR:-$HOME/lcm-pg/run}"
PGPORT="${PGPORT:-54329}"
PGUSER="${PGUSER:-lcm01}"
P="$PSQL -h $PGHOST_DIR -p $PGPORT -U $PGUSER -d lcm -qAt"
SCHEMAS="${LCM_PG_SCHEMAS:-}"
if [[ -z "$SCHEMAS" ]]; then
  SCHEMAS=$($P "select nspname from pg_namespace where nspname like 'lcm\_%'
                and nspname not like 'lcm\_test%' and nspname <> 'lcm_public' order by 1")
fi
total=0
for s in $SCHEMAS; do
  ok=$($P "select count(distinct c.relname) from pg_class c join pg_namespace n on n.oid=c.relnamespace
           where n.nspname='$s' and c.relname in ('lcm_rollups','messages')" 2>/dev/null)
  [[ "${ok:-0}" -lt 2 ]] && { echo "skip (tables missing): $s"; continue; }
  n=$($P <<SQL 2>/dev/null
begin
  create temp table if not exists _dr_ids(rollup_id bigint);
  delete from _dr_ids;
  insert into _dr_ids
    select r.rollup_id from "$s".lcm_rollups r
     where not exists (select 1 from "$s".messages m where m.session_id = r.scope);
end
SQL
)
  cnt=$($P "select count(*) from _dr_ids" 2>/dev/null); cnt=${cnt:-0}
  if [[ "$cnt" != "0" ]]; then
    for id in $($P "select rollup_id from _dr_ids" 2>/dev/null); do
      $P "delete from \"$s\".lcm_rollup_sources where rollup_id=$id" 2>/dev/null
      $P "delete from \"$s\".lcm_rollups where rollup_id=$id" 2>/dev/null
    done
    left=$($P "select count(*) from \"$s\".lcm_rollups r where not exists
               (select 1 from \"$s\".messages m where m.session_id = r.scope)" 2>/dev/null)
    echo "junk rollup rows removed: $s (n=$cnt, remaining_zombies=${left:-?})"
    total=$((total+cnt))
  fi
  $P "drop table if exists _dr_ids" 2>/dev/null
done
echo "drop_zombie_rows: removed=$total"
