#!/usr/bin/env python3
"""Delta-catch-up shared->private for lcm_* schemas: copies rows whose PK value
is missing in the destination. Anti-join on the table's PK (any column count).
Triggers disabled during COPY; FTS shadow tables regenerated after. Idempotent.
Skip tables are: sqlite_sequence, _fix_*/probe leftovers, *_fts* (regenerated)."""
import os, subprocess, sys

SHARED = ["psql", "-h", "/run/postgresql", "-U", "mybuddy", "-d", "lcm"]
PRIV = ["/home/mybuddy/lcm-pg/pg/bin/psql", "-h", "/home/mybuddy/lcm-pg/run",
        "-p", "54329", "-U", "lcm01", "-d", "lcm"]

def priv_for(schema):
    return PRIV  # schema pinning is done via env PGOPTIONS at call sites
SKIP = ("sqlite_sequence", "_fs_probe", "_fix_")

def q(conn, sql):
    r = subprocess.run([*conn, "-Atc", sql], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"query failed: {r.stderr.strip()}")
    return r.stdout.strip()

def base_tables(s):
    rows = q(SHARED, f"""select t.table_name||'|'||coalesce(string_agg(k.column_name,',' order by k.ordinal_position),'')
        from information_schema.tables t
        left join information_schema.table_constraints tc
               on tc.table_schema=t.table_schema and tc.table_name=t.table_name and tc.constraint_type='PRIMARY KEY'
        left join information_schema.key_column_usage k
               on k.constraint_name=tc.constraint_name and k.table_schema=t.table_schema
        where t.table_schema='{s}' and t.table_type='BASE TABLE' group by t.table_name order by t.table_name""")
    out = []
    for row in rows.splitlines():
        name, _, pk = row.partition("|")
        if "_fts" in name or any(name.startswith(p) or name == p for p in SKIP):
            continue
        out.append((name, pk.split(",") if pk else []))
    return out

schemas = q(SHARED, "select schema_name from information_schema.schemata "
                    "where schema_name like 'lcm\\_%'").splitlines()
grand = 0
for s in schemas:
    if q(PRIV, f"select count(*) from information_schema.schemata where schema_name='{s}'") != "1":
        print(f"{s}: MISSING in private — run migrate_from_shared_pg.py first"); continue
    total = 0
    for t, pkcols in base_tables(s):
        if not q(PRIV, f"select count(*) from information_schema.tables where table_schema='{s}' and table_name='{t}'"):
            continue
        if not pkcols:
            # no PK: compare by whole-row hash (rare; e.g. sqlite_sequence)
            print(f"{s}.{t}: no PK — hash compare"); pkcols = None
        if pkcols:
            key_expr = " || '\\x1f' || ".join(f"coalesce({c}::text,'\\x1e')" for c in pkcols) if len(pkcols) > 1 else f"{pkcols[0]}::text"
            dst_keys = q(PRIV, f"select coalesce(string_agg({key_expr}, chr(10)), '\u00a7EMPTY\u00a7') from {s}.{t}")
            if dst_keys == "§EMPTY§":
                where = "true"
            else:
                vals = ",".join("'"+k.replace("'","''")+"'" for k in dst_keys.splitlines())
                where = f"{key_expr} not in ({vals})"
        else:
            dst_hash = q(PRIV, f"select coalesce(md5(string_agg(t::text,'\\n' order by t::text)),'') from {s}.{t} t")
            src_hash = q(SHARED, f"select coalesce(md5(string_agg(t::text,'\\n' order by t::text)),'') from {s}.{t} t")
            if src_hash == dst_hash:
                continue
            print(f"{s}.{t}: PK-less table differs (src={src_hash[:8]} dst={dst_hash[:8]}) — skipped, inspect manually")
            continue
        p = subprocess.run([*SHARED, "-c", f"COPY (select * from {s}.{t} where {where}) TO stdout WITH (FORMAT csv)"],
                           capture_output=True)
        if p.returncode:
            print(f"{s}.{t}: extract failed: {p.stderr.decode().strip().splitlines()[-1][:140]}"); continue
        if not p.stdout:
            continue
        P = priv_for(s)
        subprocess.run([*PRIV, "-c", f'ALTER TABLE "{s}"."{t}" DISABLE TRIGGER USER'], capture_output=True)
        load = subprocess.run([*P, "-c", f"COPY {s}.{t} FROM stdin WITH (FORMAT csv)"],
                              input=p.stdout, capture_output=True,
                              env={**os.environ, "PGOPTIONS": f"-c search_path={s},public"})
        subprocess.run([*PRIV, "-c", f'ALTER TABLE "{s}"."{t}" ENABLE TRIGGER USER'], capture_output=True)
        if load.returncode:
            print(f"{s}.{t}: LOAD FAILED {load.stderr.decode().strip()[-200:]}"); continue
        for c in pkcols:
            subprocess.run([*PRIV, "-c", f"SELECT setval(pg_get_serial_sequence('{s}.{t}','{c}'), "
                                  f"(select coalesce(max({c}),1) from {s}.{t}))"], capture_output=True)
        n = p.stdout.count(b"\n"); total += n
        print(f"{s}.{t}: +{n}")
    # regenerate FTS shadow tables for this schema
    P = priv_for(s)
    tables = q(PRIV, f"select table_name from information_schema.tables where table_schema='{s}'").split()
    for base in ("messages", "summary_nodes", "summaries"):
        if f"{base}_fts" in tables:
            cols = q(PRIV, f"select column_name from information_schema.columns where table_schema='{s}' and table_name='{base}_fts' and column_name<>'rowid' order by 1").split()
            tcol = next((c for c in cols if "tsv" in c or c.endswith("_tsv") or "content" in c), cols[-1] if cols else "content")
            sql = (f"UPDATE {base} SET {tcol} = {tcol};")
            if f"{base}_fts_docsize" in tables:
                dcols = q(PRIV, f"select column_name from information_schema.columns where table_schema='{s}' and table_name='{base}_fts_docsize' order by 1").split()
                dcol = "docid" if "docid" in dcols else dcols[0]
                sql += (f" DELETE FROM {base}_fts_docsize;"
                        f" INSERT INTO {base}_fts_docsize({dcol}, size)"
                        f" SELECT store_id, token_estimate FROM {base};")
            r = subprocess.run([*P, "-c", sql], capture_output=True, text=True,
                         env={**os.environ, "PGOPTIONS": f"-c search_path={s},public"})
            if r.returncode: print(f"  {s}.{base} FTS regen FAILED: {r.stderr.strip()[:140]}")
    print(f"{s}: delta {total}"); grand += total
print("TOTAL DELTA:", grand)
