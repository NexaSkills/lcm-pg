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

ZERO-TOUCH PATH (preferred since 2026-10-28): after `hermes profile create <p>`,
run `bash ~/lcm_new_profile_setup.sh <p>` (repo script
scripts/lcm_new_profile_setup.py; runs the steps below in order: config keys
via hermes config set ONLY, both overlay symlinks, then the standard
detached lcm_update.sh, then a per-profile checker verdict). Exit 0 =
onboarded + verified; non-zero prints exactly what needs attention. The
manual steps below remain valid for auditing or when the script is
unavailable. Flags: --dry-run (print intended changes), --no-deploy (batch
workflows: converge configs, run the deploy once afterwards for all profiles).
Host deploy coverage: lcm_update.sh step 3f (commit 4a18da7) now runs
`hermes -p <p> plugins enable lcm-pg` for EVERY profile each deploy (no-op
when already enabled), so hosts stay gate-clean even without the script;
verify hosts run 3f via the deploy log line `plugins-enable:`. Older host
scripts (~750B ~/lcm_update.sh on some fleet hosts) lack BOTH 3c0 and 3f —
the script self-heals both (runs lcm_params_sync.py + enable itself).

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
2. Per-profile plugin gate (CRITICAL — the #1 miss; verified beez+template
   2026-10-28): `hermes -p <p> plugins enable lcm-pg`. Gate semantics: a
   profile WITHOUT its own plugins.enabled list is NOT "inheriting root's
   allow-list" — effective set = all-discovered EXCEPT disabled, and root's
   disabled list (uninstall leftovers, kev-router-style entries) silently
   blocks lcm-pg for every such profile. (Precision: the ROOT launch scope
   still honours root's own enabled list, so a host-wide multiplexed
   gateway can load lcm for such a profile anyway — skilltest/demo showed
   0 fallback warnings in live sessions while `plugins list -p` said
   not-enabled; profile-scoped/CLI processes DO fall back. Treat
   `not enabled` as must-fix regardless.)
   Only profiles WITH their own enabled: list are allow-listed. Runtime
   truth = `hermes -p <p> plugins list` shows `enabled`; `hermes plugins
   list` (root scope) proves NOTHING for a profile. The deploy's 3c2 never
   touches plugins.enabled — do it per profile here. NEVER write
   plugins.enabled via `config set` with a list value: the list-writer
   appends a literal '-' entry (broken; template incident 2026-10-28).
   Do NOT add a `hermes-lcm` entry (double-registration); the DIRECTORY-
   named overlay + `lcm-pg` are what load the fork. `auxiliary.compression
   .model` stays UNSET (params-sync converges pins away; compression
   follows the profile's active model).
3. Run `bash ~/lcm_update.sh` on that host — DETACHED if it's the host serving your own session (`setsid bash -c 'bash ~/lcm_update.sh > ~/lcm_deploy_$(date +%s).log 2>&1'`): it converges ALL params (root-union LCM_* .env surface for every profile + context.engine, via hermes config get/set ONLY — scripts/lcm_params_sync.py, step 3c0), heals profile schemas before migrations, bootstraps the `lcm_<p>` schema, ensures 6/6 assertion guards, restarts surfaces.
4. `lcm_check_all.py` must show 0 FAIL with PARAMS pool-pin + context-engine + profile-env (full LCM_* parity per profile) rows PASS.

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

## Fleet standards (converged by deploy — see references/fleet-standards.md)

Compression runs on the profile's ACTIVE model (auxiliary.compression.model
UNSET everywhere — an explicit pin is converged away by params-sync); the
engine is always lcm (context.engine) and the loaded plugin is always lcm-pg
(dir-name gate in plugins.enabled; no upstream hermes-lcm pip package on any
fleet host); autocompress fires at a FLAT 50% of the window regardless of
provider context size (engine-level flat_window_default; LCM_CONTEXT_THRESHOLD
still overrides); embed warmup/backfill heal via the systemd
hermes-lcm-embed-backfill.timer, never via manual /lcm commands. All four
are verified per-deploy by lcm_check_all.py.

## Gotchas

- CLI `chat -q` probes are USELESS for ingest/compression tests. A one-shot CLI session never exceeds the context threshold and may never write LCM rows even when the engine is healthy. Only live surfaces (gateway/webui sessions) and `/lcm compress` force real compression. Judge a host by engine-registration log lines + config parity, never by CLI row counts.
- Two distinct engine-fallback errors: "Context engine 'lcm' not found" = plugin did not discover/register in THAT process; "could not be safely copied (tuple concurrently updated / connection after 10 sec)" = engine IS registered but the per-agent clone failed (concurrent-copy race, or per-process pool pin missing -> default 8 exhausted). Check the profile .env and restart surfaces after editing. On a host where the two warnings ALTERNATE per turn, discovery is nondeterministic per process -- per-turn warnings are the ground truth and engine-registration log lines are NOT proof of a working engine.
- Profile created AFTER the last deploy = no params sync ran = built-in compression + default pool 8. Re-run lcm_update.sh.
- `PoolTimeout ... 10.00 sec` on a new profile: check the profile .env pins (deploy ENFORCES MAX>=16 since f837776; create-only before that) — the constraint is the per-process pool, not the server.
- The surface's process env is what counts (`/proc/PID/environ`), not the file — restart the surface after editing .env.
- Never run the deploy foreground from a session hosted on that surface — it restarts webui and severs the session.
- Post-onboard functional proof (the ONLY acceptable e2e check): restart surfaces, run 3 sessions in the profile (`hermes -p <p> chat -q ...`), then `journalctl --user --since -5m | grep -c "Context engine 'lcm' not found"` MUST be 0 and `lcm_<p>.metadata` gains rows. Engine-registration log lines and root-scope `plugins list` are NOT proof. Per-turn warnings after 3+ turns = real failure (discovery nondeterminism gotcha above).
- Test profile names starting with `test` make the checker SILENT for that profile (its SQL excludes `lcm_test_%` schemas by design) — a checker run right after can look clean while the deploy's own embedded report still shows a stale mid-run FAIL row (e.g. tsv-triggers counted before the FTS migration completed). Always re-run lcm_check_all.py after the deploy finishes before concluding.
- A fresh profile's `context.engine` defaults to `compressor` — the script/deploy flip it to `lcm`; a profile that predates both silently runs lossy built-in compression.
