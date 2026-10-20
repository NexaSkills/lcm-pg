#!/usr/bin/env bash
# SQLite-compat layer for an LCM-PG private cluster (idempotent, run once per
# host after updating the fork to >= 4102cbf):
#  1) public.has_object_identity(bytea)->bigint + public.blob domain (bytea)
#  2) generated `rowid` on lcm_embedding_vectors / lcm_chunk_vectors
#     (upstream SELECTs v.rowid; SQLite's hidden rowid has no PG equivalent.
#      Shim skips STORED generated cols in DDL replay -> verifiers stay OK.)
#  3) lcm_rollup_invalidations.created_at NOT NULL (bootstrap contract)
set -euo pipefail
PSQL="${PSQL:-/home/mybuddy/lcm-pg/pg/bin/psql}"
PGHOST_DIR="${PGHOST_DIR:-$HOME/lcm-pg/run}"
PGPORT="${PGPORT:-54329}"
PGUSER="${PGUSER:-lcm01}"
P="$PSQL -h $PGHOST_DIR -p $PGPORT -U $PGUSER -d lcm -q"
$P <<'SQL'
create or replace function public.has_object_identity(bytea) returns bigint
language sql immutable parallel safe as $$ select 1::bigint $$;
do $$ begin if not exists (select 1 from pg_type where typname='blob'
  and typnamespace='public'::regnamespace) then create domain public.blob as bytea; end if; end $$;
do $$
declare s text;
begin
  for s in select nspname from pg_namespace where nspname ~ '^lcm_' and nspname not in
           ('lcm_lcm','lcm_probe','lcm_probe_guard') loop
    begin
      if not exists (select 1 from pg_attribute a join pg_class c on c.oid=a.attrelid
                     join pg_namespace n on n.oid=c.relnamespace
                     where n.nspname=s and c.relname='lcm_embedding_vectors'
                       and a.attname='rowid' and a.attnum>0) then
        execute format('alter table %I.lcm_embedding_vectors add column rowid bigint '
                       'generated always as (abs(public.has_object_identity(embedded_id::public.blob))) stored', s);
      end if;
      if not exists (select 1 from pg_attribute a join pg_class c on c.oid=a.attrelid
                     join pg_namespace n on n.oid=c.relnamespace
                     where n.nspname=s and c.relname='lcm_chunk_vectors'
                       and a.attname='rowid' and a.attnum>0) then
        execute format('alter table %I.lcm_chunk_vectors add column rowid bigint '
                       'generated always as (abs(public.has_object_identity(chunk_id::public.blob))) stored', s);
      end if;
      -- rollup invalidations created_at must be NOT NULL (bootstrap: REAL NOT NULL)
      if exists (select 1 from pg_attribute a join pg_class c on c.oid=a.attrelid
                 join pg_namespace n on n.oid=c.relnamespace
                 where n.nspname=s and c.relname='lcm_rollup_invalidations'
                   and a.attname='created_at' and not a.attnotnull) then
        execute format('update %I.lcm_rollup_invalidations set created_at = '
                       'extract(epoch from now()) where created_at is null', s);
        execute format('alter table %I.lcm_rollup_invalidations alter column created_at set not null', s);
      end if;
    exception when others then
      raise notice 'skip %: %', s, sqlerrm;
    end;
  end loop;
end $$;
SQL
echo "rowid parity applied"
