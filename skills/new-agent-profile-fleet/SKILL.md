---
name: new-agent-profile-fleet
description: "Use when creating a new Hermes agent profile on the fleet. Full order: shape, create, per-key LiteLLM probe, lcm-pg onboard, delegation 1/1, SOUL.md, tools/MCP/gateway/cron, verification."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [hermes, profiles, fleet, litellm, lcm-pg]
    related_skills: [lcm-pg-new-profile-setup, hermes-agent, planning-with-files-hermes-integration]
---

# New agent profile on the fleet

Authoritative corrected procedure (verified on the fleet 2026-10-28). Load
`lcm-pg-new-profile-setup` for the onboard detail — step 4 below is the
#1-fleet-wide item and lives there.

## 0. Shape decisions (ask first)

Blank vs `--clone` vs `--clone-all`; role sentence (kanban routing); messaging
platforms or CLI-only; worker vs orchestrator. Clone semantics: `--clone`
copies config/.env/SOUL.md/skills/memories/plugins + external-memory-provider
config; never messaging channels (opt in with `--clone-channels`; two profiles
on one bot token collide), never sessions/state.db, never cron (double-fire
by design). Fresh profiles seed NO auxiliary block (verified probe
2026-10-28) — that's correct per step 2, don't add one. OAuth (Anthropic/Codex/xAI) is single-use refresh — clones drop
the rows and keep reading root `~/.hermes/auth.json`; a separate login needs
`hermes -p <p> auth add <provider>`.

## 1. Create

`hermes profile create <p> [--clone|--clone-all|--clone-from <src>]
[--description "<role>"] [--no-skills]` — name lowercase alphanumeric. Alias
command auto-created (`--no-alias` to skip). Description later: `hermes
profile describe <p>` (takes a name; bare `profile describe` errors).

## 2. Per-key LiteLLM model access (corrections 2–3)

- Model access is PER-KEY, not per-config. Only `default` may use the shared
  `main` alias on ai.nexaskills.co.za. Every other profile gets its OWN
  LiteLLM virtual key in its profile `.env` and may use ONLY models that key
  reports.
- Probe before pinning anything: `curl -s <base_url>/v1/model/list -H
  "Authorization: Bearer $LITELLM_API_KEY"`. Pin nothing absent from that
  list. No goldie.lan references in any profile config/.env (verified clean).
- The `aux` alias is RETIRED (Jan directive 2026-10-28): all auxiliary tasks
  use the profile's ACTIVE model (`model: main` = same as model.default) or
  stay unset. Never hardcode a model name anywhere beyond the `main` alias —
  config, .env, SOUL.md and scripts alike. Sweep stray pins (root + dev hosts
  carry `auxiliary.curator.model: aux`-style entries as of 2026-10-28).
- Gotcha: a fresh BLANK profile seeds provider custom →
  ai.nexaskills.co.za with the MAIN key route. A non-default profile MUST
  get its own key in .env + the catalogue probe, or it silently borrows
  default's access.

## 3. Config writes — discipline

ALL config writes via `hermes -p <p> config set section.key value`. Never
freehand yaml, never "carry entries over" by editing files. Structural edits
use the documented backup+structural-edit pattern. NEVER write
`plugins.enabled` via `config set` with a list value — the list-writer
corrupts it (template incident 2026-10-28). Then `hermes -p <p> config
check`. Fleet keys: `browser.cloud_provider: camofox` (+ `CAMOFOX_URL=http://
127.0.0.1:9377` in the profile .env — 'local' is an explicit selection and
does NOT mean camofox); `memory.provider` per fleet (Mnemosyne; give a clone
its OWN bank/profile id).

## 4. lcm-pg onboarding — the #1 fleet step

A fresh profile defaults `context.engine: compressor` → lossy built-in
compression (demo is the live example of what an un-onboarded profile becomes — it was
since onboarded, engine=lcm). Sanctioned flow:
`bash ~/lcm_new_profile_setup.sh <p>` (config keys via hermes config set
only, both overlay symlinks, deploy, checker verdict; `--dry-run`,
`--no-deploy`). CRITICAL sub-step: `hermes -p <p> plugins enable lcm-pg` — a
profile without its OWN plugins.enabled list does NOT inherit root's
allow-list; root's disabled entries (e.g. kev-router, fleet-disabled — don't
port it) silently block lcm-pg. Do NOT add a `hermes-lcm` entry
(double-registration). Verify skills visibility: `skills.external_dirs` must
include `~/.hermes/plugins/lcm-pg/skills` (deploy step 3e; check after
create). Full detail/pitfalls: skill `lcm-pg-new-profile-setup`.

## 5. Delegation posture (fleet standard 2026-10-28)

Every profile ends with, via `hermes -p <p> config set`:
`delegation.max_concurrent_children 1` and `delegation.max_spawn_depth 1`.

## 6. SOUL.md

Write `~/.hermes/profiles/<p>/SOUL.md`. Slot #1 of the system prompt, verbatim
after injection-scanning + truncation; loaded ONLY from `$HERMES_HOME` (never
cwd); never overwrites an existing file; empty/unreadable falls back to the
built-in identity. Content = durable identity/voice/tone/avoid-list, NOT
paths/repo/workflow (that's AGENTS.md/.hermes.md, cwd, first-match-wins).
Keep well under 20k chars. Temporary voice = `/personality`, not the file.
Restart the session to pick up SOUL.md edits (frozen snapshot).

## 7. Tools / skills / plugins

`hermes -p <p> tools list` is the authority (✓/✗ per platform); batch-fix with
`hermes -p <p> tools enable a b c` (bare toolset names; leading platform arg
errors). Changes take effect only on a new session (prompt caching).
Enabled ≠ configured: grep the profile .env for credential gates (x_search,
image_gen, video_gen). planning-with-files arrives via the USER timer
`pwf-profile-sync.timer` (~/.hermes/bin/pwf-profile-sync sweeps every profile
itself) — verify with `systemctl --user list-timers | grep pwf`. /etc/hermes
is GONE (no managed overlay; config lives only under ~/.hermes). kev-router
stays disabled. Catalog plugins: `hermes plugins install <name>`.

## 8. MCP

`hermes -p <p> mcp add <name> (--url|--command)` or `mcp install <name>` from
the catalog; `mcp configure` for tool selection; `mcp test <name>` to verify.
config.yaml `mcp_servers:` — `command`+`args`+`env` OR `url`+`headers` (never
both); stdio env is filtered, so pass creds via `env:`. Tools register as
`mcp_<server>_<tool>`; restart needed after add/remove. Enumerate real tool
names from discovery (test/logs), not the server README.

## 9. Gateway / cron

Gateway only if messaging: `hermes -p <p> gateway setup` — channels never
cloned unless `--clone-channels`; bot-token collision refused. Cron dir
starts EMPTY by design; recreate schedules explicitly with correct `deliver`.

## 10. Verification suite

1. `hermes -p <p> doctor` + `status --full`.
2. Model-list probe (step 2) passes with the profile's OWN key.
3. LCM proof: run 3 sessions in the profile, then
   `journalctl --user --since -5m | grep -c "Context engine 'lcm' not found"`
   MUST be 0 and `lcm_<p>.metadata` gains rows. CLI row counts and
   root-scope `plugins list` prove nothing.
4. `lcm_check_all.py` 0 FAIL (PARAMS/context-engine/profile-env rows PASS).
   Caveat: profiles named test*/probe* are excluded from checker coverage — a
   PASS there proves nothing.
5. Browser smoke test (camofox) + one MCP call + one tool-call round-trip.

## Never

Two agent processes on one profile home. Hand-edited config.yaml. Hardcoded
model names beyond `main`. `aux` anywhere. Sharing default's key with another
profile.
