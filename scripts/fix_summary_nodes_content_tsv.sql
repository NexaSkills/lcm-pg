-- One-time cleanup for the nodes_fts FTS spec (fork commit: factory.py nodes_fts
-- indexed_column content -> summary). The old wrong spec built a dead
-- summary_nodes_content_tsv trigger whose function read the nonexistent
-- summary_nodes.content column, aborting transactions on any path that installs
-- FTS triggers. summary_nodes has no content column; its real text column is
-- summary, already driving summary_nodes_summary_tsv.
--
-- Idempotent, lossless, run over every lcm_% schema:
--   * drop the dangling summary_nodes_content_tsv trigger (harmless but dead);
--   * if the content_tsv generator function still lingers, repoint its body to
--     summary and backfill so a facade keyed on content_tsv stays populated
--     (no column or data dropped). If it was already dropped, this is a no-op.
DO $$
DECLARE r record; tcount int := 0; fcount int := 0;
BEGIN
  FOR r IN
    SELECT n.nspname AS sch
    FROM pg_namespace n
    JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = 'summary_nodes'
    WHERE n.nspname LIKE 'lcm\_%'
  LOOP
    BEGIN
      -- 1. drop the dead content-trigger (drop ... if exists => safe if absent)
      EXECUTE format('DROP TRIGGER IF EXISTS summary_nodes_content_tsv ON %I.summary_nodes', r.sch);
      tcount := tcount + 1;

      -- 2. if the generator function remains, re-point its body at summary
      IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = r.sch AND p.proname = 'summary_nodes_content_tsv_update') THEN
        EXECUTE format(
          $sql$
            CREATE OR REPLACE FUNCTION %1$I.summary_nodes_content_tsv_update()
            RETURNS trigger LANGUAGE plpgsql AS $body$
            BEGIN
              NEW."content_tsv" := to_tsvector('english', coalesce(NEW."summary"::text, 'lcm'));
              RETURN NEW;
            END $body$
          $sql$, r.sch);
        fcount := fcount + 1;
      END IF;
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'ERR schema %: %', r.sch, SQLERRM;
    END;
  END LOOP;
  RAISE NOTICE 'done: triggers dropped in % schema(s), generator repointed in %', tcount, fcount;
END $$;
