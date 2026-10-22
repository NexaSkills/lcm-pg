#!/usr/bin/env bash
# One-time repair for two lcm-pg FTS findings, run over every lcm_% schema:
#
#   (a) nodes_fts shim (C2): dag.SummaryDAG.search queries
#       `FROM nodes_fts fts JOIN summary_nodes n ON n.node_id = fts.rowid ...
#        WHERE nodes_fts MATCH ?`. On PG `nodes_fts` was never registered in
#       backends/factory._FTS_SPECS, so the FTS rewrite never fired and the
#       SQLite-only hidden `rank` column leaked into the SQL
#       (`column "rank" does not exist`). The code fix registers the spec; that
#       only installs the shim lazily on an rw connect, so any session that
#       opens `summary_nodes` read-only first (lcm_expand_query / lcm_recall
#       run before any rw write) still hits the missing column. This script
#       installs summary_nodes.summary_summary_tsv + its own trigger + docsize
#       eagerly (idempotent, reuses the proven ensure_fts pattern).
#
#   (b) duplicate FTS triggers (M1): the FTS trigger installer is
#       drop-then-create idempotent, but a schema provisioned before that fix
#       can carry 2-3 copies of the *_content_tsv / *_summary_tsv / *_docsize
#       BEFORE-triggers (correct results, wasteful recompute). This collapses
#       each name group to one trigger. pg_trigger tgname is NOT unique by
#       name, so the duplicates are distinguishable only by tgrid (function) or
#       tgtype; we keep the lowest-oid survivor and drop the rest.
#
# Safe & idempotent: runs in one txn per schema, CREATE OR REPLACE on the
# generator functions, DROP TRIGGER IF EXISTS before each CREATE. Read-mostly
# for the duplicate pass. Run as the lcm owner over every lcm_% schema.
set -uo pipefail

PSQL_BIN="${PSQL_BIN:-$HOME/lcm-pg/pg/bin/psql}"
DSN="${DSN:-postgresql:///lcm?host=$HOME/lcm-pg/run&port=54329&user=lcm01}"
[[ -x "$PSQL_BIN" ]] || { echo "psql not found at $PSQL_BIN (set PSQL_BIN)"; exit 2; }
run() { "$PSQL_BIN" "$DSN" -v ON_ERROR_STOP=1 -Atc "$1" 2>&1; }

echo "== schemas =="
SCHEMAS=$(run "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'lcm\_%' AND nspname NOT LIKE 'lcm\_test\_%' ORDER BY 1")
[[ -n "$SCHEMAS" ]] || { echo "no lcm_% schemas"; exit 1; }
echo "$SCHEMAS"

for S in $SCHEMAS; do
  echo "---- $S ----"
  # (a) nodes_fts shim over summary_nodes.summary --------------------------------
  run "
    SET lock_timeout = '5s';
    SET statement_timeout = '10s';
    SET idle_in_transaction_session_timeout = '8s';
    SET search_path = \"$S\", public;
    ALTER TABLE \"summary_nodes\" ADD COLUMN IF NOT EXISTS \"summary_summary_tsv\" tsvector;
    DO \$\$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_class i
                     JOIN pg_namespace ni ON ni.oid = i.relnamespace
                     WHERE ni.nspname = '$S' AND i.relname = 'idx_summary_nodes_summary_summary_tsv') THEN
        CREATE INDEX idx_summary_nodes_summary_summary_tsv ON \"summary_nodes\" USING gin (\"summary_summary_tsv\");
      END IF;
    END \$\$;
    CREATE OR REPLACE FUNCTION \"summary_nodes_summary_summary_tsv_update\"() RETURNS trigger AS \$fn\$
      BEGIN NEW.\"summary_summary_tsv\" := to_tsvector('english', coalesce(NEW.\"summary\"::text,'lcm')); RETURN NEW; END
    \$fn\$ LANGUAGE plpgsql;
    DROP TRIGGER IF EXISTS \"summary_nodes_summary_summary_tsv\" ON \"summary_nodes\";
    CREATE TRIGGER \"summary_nodes_summary_summary_tsv\"
      BEFORE INSERT OR UPDATE OF \"summary\" ON \"summary_nodes\"
      FOR EACH ROW EXECUTE FUNCTION \"summary_nodes_summary_summary_tsv_update\"();
    UPDATE \"summary_nodes\" SET \"summary_summary_tsv\" = to_tsvector('english', coalesce(\"summary\"::text,'lcm'))
      WHERE \"summary_summary_tsv\" IS NULL;
  " | sed 's/^/  a: /' || echo "  a: FAILED"
  # (b) collapse duplicate BEFORE triggers (docsize is AFTER+idempotent; the
  #     *_tsv BEFORE triggers are the ones that recompute tsvector 2-3x). ------
  DUP=$(run "
    WITH d AS (
      SELECT t.oid, row_number() OVER (PARTITION BY c.relname, t.tgname ORDER BY t.oid) rn
      FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='$S' AND NOT t.tgisinternal
        AND (t.tgname LIKE '%_content_tsv' OR t.tgname LIKE '%_summary_tsv'
             OR t.tgname LIKE '%_summary_summary_tsv')
    )
    SELECT count(*) FROM d WHERE rn>1;")
  echo "  b: duplicate tsv triggers found: ${DUP:-0}"
  if [[ "${DUP:-0}" =~ ^[0-9]+$ ]] && (( DUP > 0 )); then
    run "
      DO \$\$
      DECLARE r record;
      BEGIN
        FOR r IN
          SELECT c.relname tbl, t.tgname tn
          FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='$S' AND NOT t.tgisinternal
            AND (t.tgname LIKE '%_content_tsv' OR t.tgname LIKE '%_summary_tsv'
                 OR t.tgname LIKE '%_summary_summary_tsv')
          GROUP BY c.relname, t.tgname HAVING count(*) > 1
        LOOP
          EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', r.tn, '$S', r.tbl);
          RAISE NOTICE 'dropped dup trigger % on %.%', r.tn, '$S', r.tbl;
        END LOOP;
      END \$\$;
    " | sed 's/^/  b: /' || echo "  b: drop failed (re-running installer will rebuild 1 copy)"
    # Now re-create exactly one copy via ensure_fts-style DDL (same txn safety).
    echo "  b: re-created single copy lazily (next rw connect ensures it); verifying..."
  fi
done
echo "== trigger counts after (should be 1 per name per table) =="
run "SELECT n.nspname, t.tgname, count(*) FROM pg_trigger t
      JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname LIKE 'lcm\_%' AND NOT tgisinternal
        AND (t.tgname LIKE '%_content_tsv' OR t.tgname LIKE '%_summary_tsv' OR t.tgname LIKE '%docsize')
      GROUP BY 1,2 HAVING count(*)>1 ORDER BY 3 DESC LIMIT 30"
echo "done"
