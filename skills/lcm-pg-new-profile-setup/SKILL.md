---
name: lcm-pg-new-profile-setup
description: Use to onboard a new Hermes profile to lcm-pg. Fleet hosts.
version: 1.0.0
author: mike
license: MIT
metadata:
  hermes:
    tags: [lcm-pg, hermes-profiles, postgres]
    related_skills: [lcm-pg-fleet-ops]
---

# Onboard a new agent profile to lcm-pg (native PG)

Canonical implementation doc: **docs/PROFILE-INTEGRATION.md** in this repo —
the full contract (end-state, schema derivation, deploy pipeline, failure
signatures). This skill is the operational short form.

## When to Use

A new Hermes agent profile was just created (or an existing one is being
audited) on a host running lcm-pg, and it must reach the production-proven
end-state (real-world e2e compression PASS validated 2026-10-25).

## Required end-state

`~/.hermes/profiles/<p>/.env` (deploy-synced; the deploy copies root LCM_*
params into every profile .env):
```
LCM_BACKEND=postgres
LCM_PG_DSN=<host's root DSN — socket dir + port + role from ~/.hermes/.env>
LCM_PG_POOL_MIN=1
LCM_PG_POOL_MAX=16
LCM_PG_POOL_MAX_IDLE=60
```
Never pin `LCM_PG_SCHEMA` — schema derives as `lcm_` + basename(HERMES_HOME)
lowercased, non-`[a-z0-9_]` → `_` (e.g. profiles/pip → lcm_pip).

`~/.hermes/profiles/<p>/config.yaml`:
```yaml
context:
  engine: lcm        # #1 silent failure: unset (+ root unset) = built-in LOSSY compression
plugins:
  enabled: [hermes-lcm, lcm-pg, mnemosyne]
memory:
  provider: mnemosyne
  mnemosyne:
    profile_isolation: true
```
`auxiliary.compression.*` = the profile's OWN model/provider/base_url (never
assume a global alias; params sync sets it from the profile default).

Plugin overlay (gate key = DIRECTORY name, both links required):
```
~/.hermes/profiles/<p>/plugins/hermes-lcm -> ~/.hermes/plugins/lcm-pg
~/.hermes/profiles/<p>/plugins/lcm-pg     -> ~/.hermes/plugins/lcm-pg
```
Deploy step 3d (migrate_hermes_lcm_config.py) creates these; hand-create only
if no deploy ran since the profile was made.

## Recipe

1. Create the profile normally.
2. Edit its config.yaml per the keys above.
3. Run `bash ~/lcm_update.sh` on the host — DETACHED if you are an agent on a
   surface it restarts:
   `setsid bash -c 'bash ~/lcm_update.sh > ~/lcm_deploy_$(date +%s).log 2>&1'`
   It syncs params, bootstraps `lcm_<p>` idempotently, ensures 6/6 assertion
   guards, restarts surfaces.
4. `lcm_check_all.py` → 0 FAIL (PARAMS pool-pin + context-engine rows PASS).

## Verify (e2e proof)

```bash
PGHOST=<socket-dir> PGPORT=<port> PGUSER=lcm01 psql -d lcm -Atc \
  "select count(distinct tgname) from pg_trigger where tgrelid::regclass::text like 'lcm_<p>.lcm_%'"  # = 6
tr '\0' '\n' < /proc/<surface-pid>/environ | grep -E 'LCM_BACKEND|LCM_PG_DSN|LCM_PG_POOL'
# then drive the profile past threshold, fire a compaction, check:
journalctl --user --since -10m | grep -iE 'pool|compress'
```
Pass: no PoolTimeout / too-many-clients; new rows in lcm_<p>.summary_nodes.

## Gotchas

- Profile created AFTER the last deploy = no params sync = built-in
  compression + pool default 8. Re-run the deploy.
- PoolTimeout: the constraint is the per-process pool (pins), not the server.
- Env counts only after a surface restart (`/proc/PID/environ`, not the file).
- Overlays must point at the materialised plugin install, never a dev/fork dir.
- Never run the deploy foreground from a session on a surface it restarts.
