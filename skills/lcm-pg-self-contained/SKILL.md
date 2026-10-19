---
name: lcm-pg-self-contained
description: "Use when operating, migrating, or extending the self-contained lcm-pg bundle (private PG cluster under /home/mybuddy/lcm-pg). Covers how to use it and how to integrate it when new agent profiles are created."
version: 1.0.0
author: mike
license: MIT
metadata:
  hermes:
    tags: [lcm, postgres, hermes-lcm, memory, mybuddy-beez]
    related_skills: [hermes-lcm, lcm-pg-post-migration-hygiene, lcm-diagnostics]
---

# lcm-pg self-contained bundle

Operation + integration guide for the Postgres-backed LCM on mybuddy-beez.
Everything lives under `/home/mybuddy/lcm-pg/` — no system PostgreSQL, no
shared databases, own binaries, own venv, own embedding model cache.

## Layout (single source of truth)

```
/home/mybuddy/lcm-pg/
  pg/            PostgreSQL 18.6 built from source (bin/, lib/, share/extension incl. pgvector)
  instance/      private cluster data dir (chmod 700)
  run/           unix socket dir (.s.PGSQL.54329) — local-only, no TCP listener
  venv/          own python env: psycopg3 + fastembed (created by scripts/make_venv.sh)
  fastembed/     fastembed model cache (bge-small-en-v1.5; set via LCM_FASTEMBED_CACHE_DIR)
  src/           postgresql-18.6 + pgvector source trees (rebuild input)
  logfile        cluster log
  install_lcm_pg.py   idempotent installer (cluster + plugin symlink + env files)
  scripts/
    pg.sh        start/stop/status wrapper for the private cluster
    make_venv.sh build the self-contained venv
    ensure_schemas.py  backfill missing lcm_<profile> schemas (idempotent)
    migrate_sqlite_to_pg.py / migrate_from_shared_pg.py
/home/mybuddy/.hermes/forks/lcm-pg/   git fork (source of the plugin)
/home/mybuddy/.hermes/plugins/lcm-pg  -> symlink to the fork (ONE plugin, all profiles)
```

DSN (every profile, every daemon):
`LCM_PG_DSN=postgresql:///lcm?host=/home/mybuddy/lcm-pg/run&port=54329&user=lcm01`
plus `LCM_BACKEND=postgres`. Schema per profile is derived automatically:
`lcm_<profile-id>` (default profile -> `lcm_default`).

## How to use it (agent-facing)

- All tools are the standard hermes-lcm surface (`lcm_grep`, `lcm_recall`,
  `lcm_status`, ...). The backend is transparent — nothing about usage changes.
- PG specifics worth knowing:
  - FTS: SQLite FTS5 is emulated with tsvector + GIN indexes (triggers keep
    `messages.content_tsv` and `summary_nodes.summary_tsv`/`content_tsv`
    current automatically). `rowid` in queries is mapped to the PK column.
  - `lcm_embedding_*` tables store vectors as bytea (float32 LE) in
    `lcm_embedding_vectors` / `lcm_chunk_vectors`; KNN is numpy-in-Python, not
    pgvector ops — pgvector exists as an optional accelerator only.
  - Embeddings: fastembed `BAAI/bge-small-en-v1.5`, cache in
    `/home/mybuddy/lcm-pg/fastembed` (via `LCM_FASTEMBED_CACHE_DIR`). If
    `/lcm embed` says "not cached", run `/lcm embed warmup` once (downloads
    ~130 MB to the bundle cache).
- Introspection: `psql -h /home/mybuddy/lcm-pg/run -p 54329 -U lcm01 -d lcm`
  (or `/home/mybuddy/lcm-pg/scripts/pg.sh psql ...`).

## Cluster lifecycle

- Start (idempotent): `~/lcm-pg/scripts/pg.sh start` (pg_ctl -w, safe to re-run).
- Status: `~/lcm-pg/scripts/pg.sh status` / `logs`.
- The cluster is LOCAL-SOCKET ONLY (`listen_addresses=''`, port 54329, socket
  dir 0700). It must never be exposed on TCP. Auth is `local all all trust`
  (isolation comes from the 0700 socket dir, not peer mapping — peer maps
  broke on multi-user targets; the installer enforces trust idempotently).
- After a host reboot: daemons fail fast until the cluster is up. Either start
  it manually or enable the user unit (below).

## Integrating a NEW agent profile (zero-config by design)

A new profile needs NOTHING installed per-profile:

1. Plugin discovery: `~/.hermes/plugins/lcm-pg` is the single symlink into the
   fork. Every profile dir only needs `profiles/<name>/plugins/lcm-pg`
   (symlink -> the root symlink). `install_lcm_pg.py --add-profile <name>`
   creates it; already-existing profiles are covered by the base install.
2. Env: the fork routes to PG purely from `LCM_BACKEND`/`LCM_PG_DSN`, and
   Hermes re-hydrates `~/.hermes/.env` + `profiles/<name>/.env` at startup
   (root `.env` is loaded for every profile). New profiles created by
   `hermes profile create` inherit the root env — no edit needed. If a profile
   was created by hand/copy and misses LCM vars, run
   `install_lcm_pg.py --env-only` to refresh all `.env` files.
3. Schema: on the engine's first connection the fork runs
   `CREATE SCHEMA IF NOT EXISTS lcm_<profile>` + versioned migrations. Verify
   with `scripts/ensure_schemas.py` (idempotent; also fixes drift).
4. Skills: the plugin dir carries `skills/lcm-pg-self-contained` (this file);
   symlink it into the new profile like the hermes-lcm skill
   (`profiles/<name>/skills/lcm-pg-self-contained ->
   ~/.hermes/plugins/lcm-pg/skills/lcm-pg-self-contained`).

## Verification (run after install / migration / reboot)

1. `~/lcm-pg/scripts/pg.sh status` -> running.
2. For each profile: `lcm_status` reports a live DAG; newest `messages.timestamp`
   in `lcm_<profile>` advances while the profile session is active.
3. `lcm_recall` with 2-3 distinctive terms returns hits and
   `provenance.coverage.fts == "ok"` (semantic arm ok once embeddings are warm).
4. `/proc/<daemon pid>/environ` contains `LCM_BACKEND=postgres` and the NEW
   DSN (run dir socket, not `/run/postgresql`).

## systemd (optional, survives reboot)

User unit at `~/.config/systemd/user/lcm-pg.service` (Type=forking,
ExecStart=pg_ctl start, ExecStop=pg_ctl stop, WantedBy=default.target). Gate
hermes daemons with `After=lcm-pg.service Wants=lcm-pg.service` ONLY if the
unit is enabled. (Install script notes this; enabling it is an explicit step.)

## Migration notes (shared system PG -> private cluster)

- Old DSN was `postgresql:///lcm?host=/run/postgresql&user=mybuddy` with
  schemas `lcm_<profile>` — shared instance, to be retired.
- Migrate with `scripts/migrate_from_shared_pg.py` (pg_dump each
  `lcm_<profile>` schema, restore into the private `lcm` db; sequences
  realigned; `_ensure_fts` triggers re-created by engine on first connect).
- Only migrate while BOTH sides are quiescent (stop hermes daemons; no CLI
  sessions running). Verify row counts per table match before dropping old
  schemas. Keep the old schemas (renamed `retired_*`) until Jan confirms.

## Gotchas (verified on this host)

- The private cluster's `max_connections=50`: daemons + CLI sessions + probes
  share it. A wedged session leaves idle-in-transaction connections; check
  `pg_stat_activity` before blaming LCM.
- Engine init runs DDL on first connect — a schema whose role can't create
  triggers fails with the embedding/FTS arms degraded, not a hard error.
  `lcm_recall.coverage` (fts/summary/chunk) is the health signal.
- fastembed import pulls onnxruntime (~200 MB venv). Keep it in the bundle
  venv, NOT the gateway venv; the fork embeds in the daemon process, so the
  daemon venv is the one that needs fastembed importable (gateway venv already
  has it here).
- Never run two clusters on the same run dir. If `pg.sh start` complains about
  the socket, check stale `lcm-pg/run/.s.PGSQL.54329.*` files from a killed postmaster.
