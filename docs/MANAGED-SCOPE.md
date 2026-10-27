# Managed scope (`/etc/hermes`) — policy: not used on this fleet

**Policy (Jan, 2026-10-27):** All Hermes configuration on the fleet lives under
`~/.hermes` only. `/etc/hermes` must not exist on any fleet host. Deploy and
checker treat a stray directory as a FAIL, and the fix is a human
`sudo rm -rf /etc/hermes`. No lcm-pg tooling ever reads-modifies-writes it.

## What /etc/hermes actually is (verified against Hermes docs + source)

It is **not** Hermes reading configuration from a root-owned path. Hermes
v0.22+ ships an opt-in feature called *managed scope*
(`hermes_cli/managed_scope.py`, docs: `website/docs/user-guide/managed-scope.md`,
PR NousResearch/hermes-agent#49098). If — and only if — an administrator
creates a root-owned `/etc/hermes/`, Hermes treats its `config.yaml`/`.env` as
**administrator-pinned policy that wins per-leaf-key** over every profile —
even over the shell environment — so a normal user cannot override it. If the
directory is absent, the feature is completely inert and config resolution is
byte-for-byte unchanged. `HERMES_MANAGED_DIR` can relocate it.

So "hermes can't use /etc" is half right: the agent/user can't *write* there
(root-owned — that is the whole enforcement model), but Hermes *reads* it at
startup as the top precedence tier when an admin placed it.

## Why it bit us (template, 2026-10-27)

A stray overlay existed listing `plugins.enabled: [planning-with-files]`.
The overlay is leaf-level, but **list leaves REPLACE** — so `plugins.enabled`
became exactly that one-item list for *every* profile, silently removing
`lcm-pg` from the plugin gate on every surface. Result:
`Context engine 'lcm' not found` → built-in lossy compressor fleet-wide,
with no error anywhere. An earlier deploy step appended fixes to the overlay
via sudo; per the 2026-10-27 directive that step is removed.

## How to check a host

```bash
test -e /etc/hermes && echo "MANAGED SCOPE PRESENT (unexpected)" || echo "clean (no managed scope)"
hermes doctor            # reports the resolved managed dir + pinned keys
hermes config            # header names the managed source if active
```

`scripts/lcm_check_all.py` reports `PARAMS managed-overlay-stray FAIL` if the
directory exists; `scripts/lcm_update.sh` refuses to deploy against it.

## If it is found

```bash
sudo rm -rf /etc/hermes
```

then restart the Hermes surfaces. Do not "fix" the overlay by editing it —
the fleet policy is that the directory does not exist. (If Jan ever *wants*
genuinely admin-pinned policy on customer hosts, managed scope is the
legitimate mechanism — but that decision is his, not the deploy's.)
