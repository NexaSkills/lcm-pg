---
name: lcm-pg-new-profile-setup
description: Use to onboard a new Hermes profile to lcm-pg. Fleet hosts.
version: 1.0.0
author: mike
license: MIT
metadata:
  hermes:
    tags: [lcm-pg, hermes-profiles, fleet, postgres]
    related_skills: [lcm-pg-fleet-ops, hermes-profile-management]
---

# Onboard a new agent profile to lcm-pg (native PG)

## When to Use

A new Hermes agent profile was just created (or an existing one is being audited) on an lcm-pg fleet host (beez, trade, clone, template, warren) and must get LCM native-PG working end-to-end — the same real-world-validated state `mybuddy-trade/pip` has.

Reference-validated state: `mybuddy-trade/pip` (real-world e2e compression PASS, 2026-10-25). The goal for any new profile is the identical state below. Most of it is applied AUTOMATICALLY by the next `bash ~/lcm_update.sh` (params sync + migration + bootstrap ensure), so the manual work is small — but verify, never assume.

## Required end-state (verified from trade/pip)

`~/.hermes/profiles/<p>/.env` — deploy syncs root LCM_* params into every profile .env; the load-bearing lines:
```
LCM_BACKEND=postgres
LCM_PG_DSN=postgresql:///lcm?host=/home/mybuddy/lcm-pg/run&port=54329&user=lcm01
LCM_PG_POOL_MIN=1
LCM_PG_POOL_MAX=16
LCM_PG_POOL_MAX_IDLE=60
```
(DSN socket dir/port follow the HOST — read root `~/.hermes/.env`; all fleet hosts use socket dir `~/lcm-pg/run`, port 54329, role lcm01. Never pin `LCM_PG_SCHEMA` — the deploy strips it; per-profile derivation sets it.)

`~/.hermes/profiles/<p>/config.yaml` — the critical keys:
```yaml
context:
  engine: lcm        # without this the profile uses built-in LOSSY compression
plugins:
  enabled:
    - hermes-lcm
    - lcm-pg
    - mnemosyne      # memory provider (memory.mnemosyne.profile_isolation: true)
```
A profile with `context.engine` unset inherits the root config; deploy migration 74aae2d flips any explicit non-lcm engine and ensures `engine: lcm`. If root has no `context: engine: lcm` either, the profile silently falls back to built-in compression — the #1 'LCM not working' cause on a new profile.

Per-profile plugin overlay (the gate key is the DIRECTORY name, so both symlinks matter):
```
~/.hermes/profiles/<p>/plugins/hermes-lcm -> ~/.hermes/plugins/lcm-pg
~/.hermes/profiles/<p>/plugins/lcm-pg     -> ~/.hermes/plugins/lcm-pg
```
Created by deploy step 3d (migrate_hermes_lcm_config.py); create by hand only if no deploy has run since the profile was made.

## Setup recipe (new profile)

0. Skill visibility (fleet-wide fix, verified 2026-10-26): Hermes lists skills
only from the profile skills dir + `skills.external_dirs` in config.yaml —
plain plugins' skills/ trees are NOT scanned (portable plugin.json packages
are the exception). Fix lives in the plugin repo: `scripts/add-skill-external-
dirs.sh` (run automatically by lcm_update.sh step 3e) appends to root + every
profile config.yaml:
```yaml
skills:
  external_dirs:
    - "/home/mybuddy/.hermes/plugins/lcm-pg/skills"   # absolute; relative anchors per-profile
```
Verify: `hermes -p <p> chat -q "list skills containing lcm"` must show
lcm-pg-new-profile-setup + lcm-pg-self-contained. One-liner for a
single missed profile: run the script, or add the block by hand + backup.
First run only (older deploys): probe answers vary between cold-start races —
re-probe once before chasing shadows.

1. Create the profile normally.
2. Edit `~/.hermes/profiles/<p>/config.yaml`: `context.engine: lcm`; enabled plugins include `hermes-lcm`, `lcm-pg`, `mnemosyne`; `auxiliary.compression.model` = the profile's own default/aux model (Jan's rule: never assume main/aux — use the profile's configured model).
3. Run `bash ~/lcm_update.sh` on that host — DETACHED if it's the host serving your own session (`setsid bash -c 'bash ~/lcm_update.sh > ~/lcm_deploy_$(date +%s).log 2>&1'`): it syncs the .env params, bootstraps the `lcm_<p>` schema, ensures 6/6 assertion guards, restarts surfaces.
4. `lcm_check_all.py` must show 0 FAIL with PARAMS pool-pin + context-engine rows PASS.

## Verify end-to-end (pip-equivalence proof)

```bash
# guards on the profile schema (real pg_trigger, not the shim view)
PGHOST=$HOME/lcm-pg/run PGPORT=54329 PGUSER=lcm01 psql -d lcm -Atc \
  "select count(distinct tgname) from pg_trigger where tgrelid::regclass::text like 'lcm_<p>.lcm_%'"
# effective env of the profile's surface process
tr '\0' '\n' < /proc/<pid>/environ | grep -E 'LCM_PG_POOL|LCM_BACKEND|LCM_PG_DSN'
# live compression test: in the profile exceed the threshold, fire /lcm compress (or auto), then
journalctl --user --since -10m | grep -iE 'pool|compress' | tail
```
Expected: no `PoolTimeout`, no `sorry, too many clients`; new rows in `lcm_<p>.summary_nodes`.

## Gotchas

- Profile created AFTER the last deploy = no params sync ran = built-in compression + default pool 8. Re-run lcm_update.sh.
- `PoolTimeout ... 10.00 sec` on a new profile: check the profile .env pins (deploy ENFORCES MAX>=16 since f837776; create-only before that) — the constraint is the per-process pool, not the server.
- The surface's process env is what counts (`/proc/PID/environ`), not the file — restart the surface after editing .env.
- Never run the deploy foreground from a session hosted on that surface — it restarts webui and severs the session.
