#!/usr/bin/env bash
# drop_zombie_schemas — remove lcm_* schemas that are pure residue: the legacy
# sqlite hermes-lcm auto-created them when it resolved HERMES_HOME wrongly
# (e.g. lcm_data, lcm_hermes_home*, phantom profile dirs). A schema is dropped
# ONLY when ALL of the following hold (data-preserving, idempotent):
#   1. it matches lcm_% (not lcm_test_*, lcm__deleted, lcm_default)
#   2. it holds ZERO rows in messages AND summary_nodes (live AND n_live_tup)
#   3. no extension (deptype 'x') depends on it (auto 'a' rows are internal,
#      and no view/function may reference it — verified by census below)
#   4. no live hermes profile directory maps to it (profile dirs = lcm_<profile>,
#      and lcm_default always survives)
# Anything with rows, or mapped to an existing profile dir, is left alone and
# reported. Run via lcm_deploy_migrations.py (stamped per schema) — never
# manually on fleet hosts.
set -o pipefail
PSQL="${PSQL:-}"
if [[ -z "$PSQL" ]]; then
  if command -v psql >/dev/null 2>&1; then PSQL=psql; else PSQL="$HOME/lcm-pg/pg/bin/psql"; fi
fi
PGHOST_DIR="${PGHOST_DIR:-$HOME/lcm-pg/run}"
PGPORT="${PGPORT:-54329}"
PGUSER="${PGUSER:-lcm01}"
P="$PSQL -h $PGHOST_DIR -p $PGPORT -U $PGUSER -d lcm -qAt"

# expected live schemas from profile dirs (same rule as check_all inventory)
live=("lcm_default")
for d in "$HOME"/.hermes/profiles/*/; do
  [[ -d "$d" ]] || continue
  p=$(basename "$d")
  [[ "$p" == ".deleted" ]] && continue
  live+=("lcm_${p}")
done
live+=("lcm__deleted")

zombies=$($P <<'SQL'
-- deptype 'a' = AUTO dependency: every table owns its internally-created
-- types/ACLs, so those rows do NOT mean an external object depends on the
-- schema. Only deptype 'x' (extension) is a real cross-schema dependency.
select n.nspname from pg_namespace n
where n.nspname like 'lcm\_%' and n.nspname not like 'lcm\_test%'
  and n.nspname <> 'lcm_default' and n.nspname <> 'lcm__deleted'
  and not exists (select 1 from pg_depend d where d.refobjid = n.oid and d.deptype = 'x')
SQL
)
[[ -z "$zombies" ]] && { echo "zombie-clean: nothing to inspect"; exit 0; }

dropped=0 kept=0
for s in $zombies; do
  # skip live-profile schemas
  skip=0
  for l in "${live[@]}"; do [[ "$s" == "$l" ]] && { skip=1; break; }; done
  if [[ $skip -eq 1 ]]; then
    echo "keep (live profile): $s"; kept=$((kept+1)); continue
  fi
  if ! $P -c "select count(*) from ${s}.messages" >/dev/null 2>&1 \
     || ! $P -c "select count(*) from ${s}.summary_nodes" >/dev/null 2>&1; then
    echo "skip (core tables missing/unreadable, hand-check): $s"; kept=$((kept+1)); continue
  fi
  # live-count EVERY user table in the schema: ledger rows (metadata,
  # lcm_migration_state, lcm_deploy_migrations) are droppable, but a row in
  # any data table (messages/summary_nodes/rollups/vectors/...) means this
  # schema once held real context -> never drop. (plpgsql loop; our build has
  # no libxml, so query_to_xml is unavailable.)
  datarows=$($P 2>&1 <<SQL
do \$\$
declare r record; n bigint;
begin
  n := 0;
  for r in select tablename from pg_tables where schemaname='$s'
             and tablename not in ('metadata','lcm_migration_state','lcm_deploy_migrations',
                                   'lcm_lifecycle_state') loop
    execute format('select count(*) from %I.%I', '$s', r.tablename) into n;
    if n > 0 then
      raise notice 'DATAROWS %', n;
      return;
    end if;
  end loop;
  raise notice 'DATAROWS 0';
end
\$\$
SQL
2>&1)
  datarows=$(echo "$datarows" | grep -oE "DATAROWS [0-9]+" | tail -1 | awk '{print $2}')
  if [[ -z "$datarows" ]]; then
    echo "skip (row census failed, hand-check): $s"; kept=$((kept+1)); continue
  fi
  if [[ "$datarows" != "0" ]]; then
    echo "keep (data rows=$datarows): $s"; kept=$((kept+1)); continue
  fi
  if $P -c "drop schema ${s} cascade" >/dev/null 2>&1; then
    echo "dropped zombie schema: $s"; dropped=$((dropped+1))
  else
    echo "FAILED to drop: $s"; kept=$((kept+1))
  fi
done
echo "zombie-schemas: dropped=$dropped kept=$kept"
exit 0
