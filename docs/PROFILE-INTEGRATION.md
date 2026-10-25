# Profile Integration Guide — making lcm-pg work on ANY Hermes agent profile

Goal: every Hermes agent profile on a host running lcm-pg uses the LCM
native-PostgreSQL context engine (lossless, DAG-backed) with the same
hardened connection behaviour proven in production — no built-in lossy
compression, no pool timeouts, no manual DB surgery.

This guide is **host-agnostic**: `lcm_update.sh` contains no host literals.
Host-specific values are declared in the host's root `~/.hermes/.env` and in
the env vars listed under "Host inputs" below; everything profile-level is
either repo-declared or derived automatically.

## 1. The end-state contract (what "working" means, per profile)

For profile `<p>` with `HERMES_HOME=~/.hermes/profiles/<p>`:

### 1.1 `.env` — required lines (deploy-synced; verify, don't hand-edit)

```
LCM_BACKEND=postgres
LCM_PG_DSN=postgresql:///lcm?host=<socket-dir>&port=<port>&user=lcm01
LCM_PG_POOL_MIN=1
LCM_PG_POOL_MAX=16
LCM_PG_POOL_MAX_IDLE=60
```

- `LCM_PG_DSN` is copied from the **host's root `.env`** (the local PG cluster
  endpoint: socket dir + port + role). Profiles never invent their own DSN.
- Pool policy (why these numbers): the binding constraint for a timeout is the
  **per-process pool**, not the server ceiling. 16 (min 1) per surface
  process; `MAX_IDLE=60` arms psycopg's idle-connection reaper so a quiet
  surface actually releases backends; `max_lifetime` (code default 3600 s,
  env `LCM_PG_POOL_MAX_LIFETIME`) recycles stale members. Cluster-side
  `max_connections=100` is enforced by the deploy so
  `surfaces × pool_max` stays well under the ceiling.
- **Never pin `LCM_PG_SCHEMA` in a profile** — the schema is derived (below)
  and the deploy strips stray pins.

### 1.2 `config.yaml` — required keys

```yaml
context:
  engine: lcm            # THE switch: unset/non-lcm = built-in lossy compression
plugins:
  enabled:
    - hermes-lcm
    - lcm-pg
    - mnemosyne          # durable memory provider
memory:
  provider: mnemosyne
  mnemosyne:
    profile_isolation: true
auxiliary:
  compression:
    model: <profile's own aux/default model>   # never assume a global alias
    provider: <profile's provider>
    base_url: <profile's base_url>
```

- `context.engine: lcm` is the #1 silent failure: if it's absent AND the root
  config lacks it, the profile quietly runs built-in compression and LCM
  never engages. The deploy's config migration ensures `engine: lcm`.
- `auxiliary.compression` must point at **the profile's own model/route**
  (a hard-coded global alias breaks on profiles with different routing). The
  deploy's params sync sets the summary/compression model from the profile's
  configured default.

### 1.3 Plugin overlay symlinks (gate keys on the DIRECTORY name)

```
$HERMES_HOME/plugins/hermes-lcm -> <root>/plugins/lcm-pg
$HERMES_HOME/plugins/lcm-pg     -> <root>/plugins/lcm-pg
```

Both point at the one materialised plugin install (the repo deployed into the
plugin location — never at a dev/fork working copy). The deploy
(`migrate_hermes_lcm_config.py` step) creates/repairs these for every profile
dir it finds; hand-create them only if no deploy has run since the profile
was made.

## 2. Schema derivation (automatic)

`backends/postgres.py::schema_for_profile()` maps a profile to its schema:
`lcm_` + lowercased `basename(HERMES_HOME)` with non-`[a-z0-9_]` → `_`.
So `profiles/pip` → `lcm_pip`, root → `lcm_default` style naming. `LCM_PG_SCHEMA`
overrides only for tooling; per-profile derivation is the norm. Each schema
gets the full native DDL (messages, FTS, summary DAG, embeddings/kNN,
assertion guards) from `db_bootstrap`, bootstrapped idempotently by
`ensure_profile_bootstrap.py` during deploy — a brand-new profile needs zero
manual DB work as long as a deploy runs after it is created.

## 3. Deploy pipeline (what the scripts do)

`lcm_update.sh [--ref <git-ref>]` (default `origin/HEAD`, i.e. the repo's
deploy branch `main`) — identical on every host:

1. **Fetch** the pinned ref, fast-forward only. Hosts reach the repo either
   directly or via their own local staging repo (transport is per-host env,
   never a code path).
2. **Materialise** into the plugin location `$HERMES_HOME/../plugins/lcm-pg`
   (default `~/.hermes/plugins/lcm-pg`) — the single artifact all profiles
   load through their overlays.
3. **Migrate**: stamped per-(schema, ref) migrations run BEFORE restart —
   core schema bootstrap, FTS/guard/rollup-shim repairs, sequences,
   assertion guards (6 per schema), pool-pin enforcement (`>= 16`), cluster
   `max_connections=100`, stale-backend sweep.
4. **Params sync**: repo-declared config from `configs/` into root + every
   profile `.env` (LCM_* params, pool pins); profile `config.yaml` engine +
   compression-model alignment; overlay symlinks (step 3d).
5. **Restart** all hermes surfaces (discovered, not hardcoded).
6. **Verify** with `lcm_check_all.py` → per-host report: migrations applied,
   guards 6/6, params pins PASS, context engine PASS, pool/backends census.
   Contract: **0 FAIL** before the deploy counts as done.

Host inputs (env, `$HOME`-based defaults, no host literals in code):
`LCM_PG_DSN`, `HERMES_HOME`, `PLUGIN_DIR`, `LCM_PARAM_FILES`.

## 4. New-profile setup recipe

1. Create the profile (`hermes profiles create <p>` or the host's normal flow).
2. In `~/.hermes/profiles/<p>/config.yaml` set `context.engine: lcm`, enable
   plugins `hermes-lcm, lcm-pg, mnemosyne`, set `auxiliary.compression.*` to
   the profile's own model/provider/base_url.
3. Run the deploy on the host (it discovers the new profile dir):
   `bash ~/lcm_update.sh` — run it DETACHED if you are an agent on a surface
   it will restart: `setsid bash -c 'bash ~/lcm_update.sh > ~/lcm_deploy_$(date +%s).log 2>&1'`.
4. Verify: check_all 0 FAIL, then the e2e proof below.

## 5. E2E verification (must all pass)

```bash
# a) schema + guards (real pg_trigger, not shim views): 6 per schema
PGHOST=<socket-dir> PGPORT=<port> PGUSER=lcm01 psql -d lcm -Atc \
  "select count(distinct tgname) from pg_trigger where tgrelid::regclass::text like 'lcm_<p>.lcm_%'"

# b) effective env of the profile's live surface process (file is not enough)
tr '\0' '\n' < /proc/$(systemctl --user show <unit> -p MainPID --value)/environ \
  | grep -E 'LCM_BACKEND|LCM_PG_DSN|LCM_PG_POOL'

# c) live compression smoke: drive the profile past its compression threshold,
#    trigger a compaction (auto or /lcm compress), then:
journalctl --user --since -10m | grep -iE 'pool|compress' | tail
```

Pass = no `PoolTimeout`, no `sorry, too many clients`, new rows in
`lcm_<p>.summary_nodes`, and `/lcm status` reporting the LCM store.

## 6. Failure signatures → causes

| Symptom | Root cause | Fix |
|---|---|---|
| profile still compresses lossily, `/lcm` missing | `context.engine: lcm` absent (profile + root) | set it; restart surface |
| `PoolTimeout ... 10.00 sec` | per-process pool default 8 (pins missing — profile created after last deploy, or pre-enforcement deploy) | re-run deploy; check profile `.env` pins; restart |
| `sorry, too many clients` | cluster `max_connections` too low for surfaces × pool | deploy step enforces 100 |
| hour-old `idle` backends | reaper unarmed (pre-`max_idle` deploy) | deploy ≥ the recycler commit; sweep runs in deploy |
| schema missing for new profile | no deploy ran after profile creation | run deploy (bootstrap is idempotent) |
| LCM loads from a dev copy | overlay symlink → fork/dev dir | repoint both symlinks to the plugin location |
| profile uses wrong/global compression model | hardcoded auxiliary route | params sync sets profile default; verify live config |

## 7. Verification ledger

The contract in §1–2 was validated end-to-end in production on 2026-10-25: a
real-world compression ran clean on a fleet host profile after the native-PG
conversion, with fleet-wide 0-FAIL check_all reports. The gotcha table in §6
comes from incidents actually chased to root cause (per-process pool
saturation, never-armed idle reaper, create-only pin logic, stale overlay
targets) — see the fork ops doc for the connection-hygiene backstory.
