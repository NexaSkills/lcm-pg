# lcm-pg — dual-backend fork of hermes-lcm

Baseline: upstream hermes-lcm commit 8d1b1e6d3d63f5fc7b209e8d7ec1dc9b814f2e54 (vendored 2026-10-18).
Goal: PostgresBackend for the storage layer (corruption-resistant, multi-process-safe);
SQLite path stays as upstream default so rollout/rollback is a config flip.

## Config surface
- LCM_BACKEND=postgres (default: sqlite)
- LCM_PG_DSN=postgresql://... (libpq DSN; also honours standard PG* env vars)
- LCM_PG_SCHEMA=lcm_<profile> (default derived from profile name; default profile -> lcm_default)

## Layout
- backends/__init__.py  — StorageBackend protocol (DB-API style, connection() context mgr)
- backends/sqlite.py    — upstream code, unchanged
- backends/postgres.py  — psycopg3 (3.6), %s placeholders, RETURNING for rowid
- backends/dialect.py   — the ONLY place SQLite->PG SQL translations live (INSERT OR IGNORE->ON CONFLICT, rowid->BIGSERIAL, FTS5 MATCH->tsvector/ts_rank+pg_trgm, savepoints, busy_timeout->lock_timeout)
- migrate/              — Phase 3 sqlite->pg per-profile migrator + verifiers

## Rules
- Every sqlite3.connect() site (32 total, 12 files) routes through backends.get_connection().
- Upstream parity tests run against BOTH backends (make test-sqlite / test-pg).
- Rebase cadence: pull upstream every 2-4 weeks; dialect.py conflicts are the accepted tax.

## Infra (already provisioned 2026-10-18)
- Local PG 18.6, db "lcm", schemas lcm_default|lcm_mike|lcm_brandbuddy|lcm_buttercup|lcm_debbie (owned by role mybuddy).
- Auth: unix-socket peer (systemd socket-activation ready) + TCP/SCRAM via LCM_PG_DSN in profile .env files.
- psycopg 3.6 + pgvector installed into the gateway site-packages (9527c3f3.../site-packages).

## Deterministic profile provisioning (no hardcoded profile lists)
- schema_name(profile) = "lcm_" + profile_id (profile_id = basename of HERMES_HOME;
  default profile -> ".hermes" -> maps to "default" -> lcm_default).
- Engine startup with LCM_BACKEND=postgres runs
  `CREATE SCHEMA IF NOT EXISTS <schema> AUTHORIZATION mybuddy` (idempotent),
  then `SET search_path`, then versioned CREATE TABLE IF NOT EXISTS migrations.
  => a brand-new agent profile needs ZERO manual provisioning steps.
- Backfill for pre-existing profiles: scripts/ensure_schemas.py scans
  ~/.hermes/profiles/*/ and creates any missing lcm_<profile> schemas (idempotent).
- Host provisioning itself is equally deterministic (recorded in FORK.md infra section):
  data dir /home/postgres/data (ext4, /dev/sdb), systemd drop-in PGROOT + ProtectHome=false,
  socket /var/run/postgresql (peer) + TCP/SCRAM via LCM_PG_DSN.
