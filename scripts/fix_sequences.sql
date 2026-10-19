-- One-time sequence alignment for lcm_<profile> schemas migrated by the
-- pre-a476c03 migrate script (COPY with explicit ids left serials lagging).
-- Idempotent: only touches sequences whose last_value <= max(col).
DO $$
DECLARE r record; mx bigint; lv bigint; fixed int := 0;
BEGIN
  FOR r IN
    SELECT n.nspname AS sch, c.relname AS seq, tc.relname AS tbl, aa.attname AS colname
    FROM pg_class c
    JOIN pg_depend d ON d.objid=c.oid AND d.deptype='a'
    JOIN pg_class tc ON tc.oid=d.refobjid
    JOIN pg_attribute aa ON aa.attrelid=tc.oid AND aa.attnum=d.refobjsubid
    JOIN pg_namespace n ON n.oid=c.relnamespace AND tc.relnamespace=n.oid
    WHERE c.relkind='S' AND n.nspname LIKE 'lcm\_%'
  LOOP
    BEGIN
      EXECUTE format('select max(%I) from %I.%I', r.colname, r.sch, r.tbl) INTO mx;
      EXECUTE format('select last_value from %I.%I', r.sch, r.seq) INTO lv;
      IF mx IS NOT NULL AND mx >= lv THEN
        EXECUTE format('select setval(%L, %s, false)', r.sch||'.'||r.seq, mx+1);
        RAISE NOTICE 'FIXED %.% (was % on %.%) -> %', r.sch, r.seq, lv, r.sch, r.tbl, mx+1;
        fixed := fixed + 1;
      END IF;
    EXCEPTION WHEN others THEN RAISE NOTICE 'ERR %.%: %', r.sch, r.seq, SQLERRM;
    END;
  END LOOP;
  RAISE NOTICE 'DONE fixed=%', fixed;
END $$;
