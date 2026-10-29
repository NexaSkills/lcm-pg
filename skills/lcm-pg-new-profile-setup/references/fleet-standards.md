# Fleet standards (lcm-pg, Jan 2026-10-27)

These are converged automatically by `lcm_update.sh` on every deploy
(params-sync step 3c0 + migration `migrate_hermes_lcm_config.py`), so a new
profile needs NOTHING by hand — verify, never hand-edit. A profile created
AFTER the last deploy is unconverged until the next deploy runs (re-run
`lcm_update.sh` after creating any profile).

## 1. Compression follows the ACTIVE model (no aux pin)

`auxiliary.compression.model` must be ABSENT (unset) in root and every
profile's config.yaml. Hermes resolves an unset compression model to the
main/current runtime model (# compression-current-model in
agent/auxiliary_client.py), so a user chatting on qwen/qwen3.8-flash gets
compression summaries on qwen/qwen3.8-flash. An explicit pin — even `main`
— FREEZES the route against runtime model switches and is converged away by
params-sync via `hermes config unset` (CLI-only; `config unset` exists since
0.22). check_all row `profile:<p>` PASSes only when the key is absent.

```bash
hermes -p <p> config get auxiliary.compression.model   # expect: Config key not set
```

## 2. Compression engine is always lcm (never the built-in compressor)

`context.engine=lcm` effective for every profile (params-sync sets it via
`hermes config set` when not already lcm/root-inherited). Verify with the
CLI, which resolves profile > root > DEFAULT_CONFIG like the runtime does:

```bash
hermes -p <p> config get context.engine   # expect: lcm
```

## 3. The loaded plugin is always lcm-pg, never upstream hermes-lcm

Gate keys: `plugins.enabled` must contain `lcm-pg` (dir-name gate).
`hermes-lcm` may stay in the list (upstream-compat entry) but is HARMLESS
and must not be loadable: on a converged host no pip package named
hermes-lcm is installed anywhere, and every `hermes-lcm` plugin dir is a
symlink to `~/.hermes/plugins/lcm-pg` (verified with `readlink -f`). Check:

```bash
pip list | grep -i hermes-lcm        # expect: nothing (any host)
readlink -f ~/.hermes/plugins/hermes-lcm   # expect: .../plugins/lcm-pg
```

If a real upstream hermes-lcm pip package ever appears on a host, that is
two engines on one surface — uninstall the pip package, do not disable
via config (the deploy would re-converge it).

## 4. Autocompress fires at a FLAT 50% of the context window

Fleet standard (Jan 2026-10-27): lcm-pg compresses at 0.50 of the window for
every profile regardless of provider context size (retired: window buckets
262k/512k/1M -> 60/45/35%). Implemented in engine.py
`_runtime_context_threshold`: when no explicit `LCM_CONTEXT_THRESHOLD` /
`lcm.context_threshold` override exists, the effective threshold is the flat
0.50 (telemetry source `flat_window_default`); `config.py`
default `context_threshold` is 0.50. Host `compression.threshold`
(fleet hosts carry 0.45) is overridden by this — do NOT bother changing it.
An operator override stays authoritative if Jan explicitly sets one.
Exception: Hermes core keeps a raise-only floor under the LCM engine too
(agent/context_compressor._effective_threshold_percent): windows under 512K
still trigger at >=75% so compaction can't re-fire every 1-2 turns. Flat 50%
applies to >=512K windows (all LiteLLM beez-mike routes).

## 4b. One plugin load identity: the key is lcm-pg (never a double import)

plugin.yaml declares `key: lcm-pg` (commit 2e47598) because Hermes keys
directory manifests by DIRECTORY name, NOT manifest name. The legacy
hermes-lcm overlay dirs (symlinks into plugins/lcm-pg) are harmless now:
both dirs resolve to key lcm-pg, so one engine import per home scope. The
deploy (migrate_hermes_lcm_config.py) also converges `hermes-lcm` OUT of
plugins.enabled lists. Symptom this fixed: gateway boot 'Failed to load
plugin hermes-lcm: load timed out after 10s' (plugins.load_timeout_seconds,
default 10). Never delete the hermes-lcm overlay symlinks — skill visibility
+ dir-gate target depend on them. Verify clean boot after every deploy:

```bash
journalctl --user -u hermes-gateway --since -10m | grep -c 'Failed to load plugin'   # 0
```

## 5. Embeddings (warmup + backfill) need zero manual commands

`/lcm embed warmup` / `/lcm embed backfill --apply` are NOT run by
deploy/check. The heal is `hermes-lcm-embed-backfill.timer` (systemd user
timer running `embed_auto_backfill.py --apply`): it registers the model
profile into every schema (the warmup job) and drains the corpus with lease
guardrails — new profiles get embedded with no user action. Verify on a
host:

```bash
systemctl --user list-timers | grep embed   # timer present + firing
journalctl --user -u hermes-lcm-embed-backfill.service --since -24h | tail
```

A host that only ever ran `lcm_update.sh` (never rollout_host.sh / 7c) has
NO timer = corpora stay unembedded (FTS-only recall). Run
`bash ~/.hermes/plugins/lcm-pg/scripts/embed_timer_install.sh` once there
(idempotent).

## 4c. Fleet runbooks (workspace docs) are dated snapshots, superseded on threshold

The handover/params runbooks under `~/workspace/` (lcm-pg-handover-2026-10-22*.md,
lcm-pg-fleet-params-addendum-2026-10-22.md) predate the flat-50 standard and state
`compression.threshold = 0.45` / `LCM_COMPRESS_THRESHOLD 0.45`. That policy was
retired 2026-10-27/29: the authoritative compression policy is §4 above (flat 0.50,
engine-enforced; host `compression.threshold` is inert under engine=lcm). Do not
re-derive thresholds from the 2026-10-22 runbooks.
