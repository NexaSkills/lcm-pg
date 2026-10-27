#!/usr/bin/env python3
"""migrate_hermes_lcm_config.py — converge plugins.enabled onto the lcm-pg plugin.

Migration assumption (fleet mandate): the host moves FROM the upstream
hermes-lcm plugin TO lcm-pg. Ground truth in this Hermes build
(hermes_cli/plugins_discovery.py parse_manifest_file): a directory plugin's
gate name is its DIRECTORY name, not the name: in plugin.yaml. So the fork
under ~/.hermes/plugins/lcm-pg is enabled ONLY by listing `lcm-pg`;
`hermes-lcm` in enabled can also match the pip-installed upstream plugin.
That is exactly the confusion that lets a host "have lcm" while running the
wrong engine.

Behaviour (idempotent; --dry-run prints changes without writing):
  * root + every profile config.yaml: plugins.enabled += lcm-pg (keep
    hermes-lcm: harmless fallback for hosts where the fork dir isn't live).
  * profiles/*/plugins/hermes-lcm that resolves into plugins/lcm-pg (the
    3c overlay symlink): symlink alone does NOT make discovery see it under
    the lcm-pg key, so a per-profile dir plugins/lcm-pg -> same target is
    created (dir name is what gates).
  * plugins.disabled -= lcm-pg if present (a disable silently defeats deploy).
  * each touched file backed up as <file>.pre-lcm-pg-migration.<ts>.
Exit 0 always unless a write/parse fails (then 1, with the file named).
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # PyYAML ships with hermes envs; bundle venv may lack it
    print("migrate-config: PyYAML unavailable; skipping (configs unchanged)")
    sys.exit(0)

HOME = Path(os.environ.get("HOME", "$HOME"))
# Host root tree — an agent shell's HERMES_HOME points ONE profile home; only
# a deploy running with HERMES_HOME=$HOME/.hermes (or a custom root) is honoured.
HERMES = HOME / ".hermes"
_hh = Path(os.environ.get("HERMES_HOME", "")).expanduser() if os.environ.get("HERMES_HOME") else None
if _hh and _hh.parent == HERMES and _hh.name != "profiles":
    HERMES = _hh
PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR", str(HERMES / "plugins" / "lcm-pg")))
TS = time.strftime("%Y%m%d%H%M%S")


def _hermes_bin() -> str:
    for cand in (os.environ.get("HERMES_BIN"),
                 shutil.which("hermes"),
                 str(HOME / ".local/bin/hermes")):
        if cand and Path(cand).exists() and Path(cand).stat().st_mode & 0o111:
            return str(cand)
    return "hermes"


# profile name as `hermes -p` expects it ("default" = root home, no -p)
def _profile_arg(path: Path) -> str | None:
    if path.parent.parent == HERMES / "profiles":
        name = path.parent.name
        return None if name == "default" else name
    return None  # root config.yaml


# config.yaml writes go through `hermes config set` ONLY (Jan directive
# 2026-10-27). Reads may parse the file (harmless); writes may not touch it.
def _cfg_set(profile: str | None, key: str, value: str, dry: bool) -> bool:
    if dry:
        print(f"dry: hermes{' -p ' + profile if profile else ''} config set {key} {value}")
        return True
    cmd = [_hermes_bin()] + (["-p", profile] if profile else []) + \
          ["config", "set", "--force", key, value]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        _fail(f"hermes config set {key} ({profile or 'root'}): "
              f"{(p.stdout + p.stderr).strip().splitlines()[-1:]}")
        return False
    return True


class _St:
    errors = 0


def _fail(msg: str) -> None:
    print(f"ERROR {msg}")
    _St.errors += 1


def _backup_write(path: Path, text: str) -> bool:
    # .env writes only (the one file deploy may file-edit; config.yaml goes
    # via `hermes config set` exclusively — Jan directive 2026-10-27).
    try:
        shutil.copy2(path, f"{path}.pre-lcm-pg-migration.{TS}")
        path.write_text(text)
        return True
    except Exception as e:
        _fail(f"write {path}: {e}")
        return False


def _configs():
    out = []
    if (HERMES / "config.yaml").is_file():
        out.append(HERMES / "config.yaml")
    if (HERMES / "profiles").is_dir():
        out += sorted((HERMES / "profiles").glob("*/config.yaml"))
    return out


def _migrate_config(path: Path, plugin_live: bool, dry: bool) -> None:
    # READ the file (parsing is harmless); all WRITES go via _cfg_set
    # (`hermes config set` only — never a file edit of config.yaml).
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        _fail(f"parse {path}: {e}")
        return
    if not isinstance(data, dict):
        _fail(f"{path}: top level not a mapping")
        return
    prof = _profile_arg(path)
    plugins = data.get("plugins")
    if plugins is not None and not isinstance(plugins, dict):
        _fail(f"{path}: plugins is not a mapping")
        return
    plugins = plugins or {}
    enabled = plugins.get("enabled")
    if enabled is not None and not isinstance(enabled, list):
        _fail(f"{path}: plugins.enabled is not a list")
        return
    ok = True
    acted = False
    # enabled: set the LIST ONLY when it already exists (a list literal via
    # config set). When the key is absent the profile INHERITS root's
    # plugins.enabled — which this script converges root-side first — so an
    # explicit per-profile list here is unnecessary (and would freeze the
    # inherited set). YAGNI: don't materialise inheritance.
    if plugin_live and isinstance(enabled, list) and "lcm-pg" not in enabled:
        ok = _cfg_set(prof, "plugins.enabled",
                      yaml.safe_dump([*enabled, "lcm-pg"], default_flow_style=True).strip(),
                      dry) and ok
        acted = True
    # 2026-10-27 (Jan directive): hermes-lcm is RETIRED — lcm-pg completely
    # replaces it, and 'hermes-lcm' must not appear in any plugins.enabled.
    # plugin.yaml now says name: lcm-pg + key: lcm-pg, so nothing loads under
    # the legacy identity; this strips stale entries wherever they exist
    # (root, profiles, or a lingering /etc overlay would be refused by
    # config.py — the operator removes that; deploy step 3c3 checks it).
    if isinstance(enabled, list) and "hermes-lcm" in enabled:
        ok = _cfg_set(prof, "plugins.enabled",
                      yaml.safe_dump([x for x in enabled if x != "hermes-lcm"],
                                     default_flow_style=True).strip(),
                      dry) and ok
        acted = True
    disabled = plugins.get("disabled")
    if isinstance(disabled, list) and "lcm-pg" in disabled:
        ok = _cfg_set(prof, "plugins.disabled",
                      yaml.safe_dump([x for x in disabled if x != "lcm-pg"],
                                     default_flow_style=True).strip(),
                      dry) and ok
        acted = True
    # context.engine: an EXPLICIT non-lcm engine defeats lcm-pg for this
    # profile (agent_init falls back to the built-in lossy compressor). An
    # absent block legitimately inherits root — leave it unset.
    ctx = data.get("context")
    if isinstance(ctx, dict):
        eng = ctx.get("engine")
        if eng is not None and eng != "lcm":
            ok = _cfg_set(prof, "context.engine", "lcm", dry) and ok
            acted = True
    elif ctx is not None and not isinstance(ctx, dict):
        _fail(f"{path}: context is not a mapping")
    if acted:
        print(f"config converged via hermes config set -> {path}")


def _env_schema_stripping(dry: bool) -> None:
    """Remove stray LCM_PG_SCHEMA pins from root + profile .env files.
    The fork's factory honours LCM_PG_SCHEMA ABOVE the per-profile derivation
    (backends/factory.py: schema = LCM_PG_SCHEMA or schema_for_profile(home)).
    Since the host-wide LCM_DATABASE_PATH boundary pin already forces the
    dbpath-parent derivation, ANY LCM_PG_SCHEMA pin (e.g. the manual
    LCM_PG_SCHEMA=lcm_mike applied on fleet-host) freezes that surface onto ONE
    schema for every profile — breaking per-profile isolation across webui,
    cli, telegram and dashboard. Derivation from HERMES_HOME is the correct
    mechanism; strip the override so it works again. Backed up first."""
    envs = [HERMES / ".env"]
    if (HERMES / "profiles").is_dir():
        envs += sorted((HERMES / "profiles").glob("*/.env"))
    for ef in envs:
        if not ef.is_file():
            continue
        # root .env IS the daemon env (EnvironmentFile in the units): pinning
        # LCM_PG_SCHEMA there would freeze EVERY profile onto one schema.
        # Profile .env files apply only to profile-scoped daemons. Strip any
        # pin — the derivation below (HERMES_HOME/dbpath) is the mechanism.
        try:
            lines = ef.read_text().splitlines(keepends=True)
        except Exception as e:
            _fail(f"read {ef}: {e}")
            continue
        hit = [i for i, l in enumerate(lines) if l.lstrip().startswith("LCM_PG_SCHEMA=")]
        if not hit:
            continue
        if dry:
            print(f"dry: strip LCM_PG_SCHEMA -> {ef}")
            continue
        try:
            shutil.copy2(ef, f"{ef}.pre-lcm-pg-migration.{TS}")
            ef.write_text("".join(l for i, l in enumerate(lines) if i not in hit))
            print(f"env: stripped LCM_PG_SCHEMA -> {ef.name} ({ef.parent}) (per-profile derivation restored)")
        except Exception as e:
            _fail(f"rewrite {ef}: {e}")


def _overlay_dirs(dry: bool) -> None:
    """Profile discovery resolves a plugin under a DIRECTORY name; a symlink
    named hermes-lcm does not grant the lcm-pg key. Ensure every profile has
    plugins/lcm-pg pointing at the same target as its hermes-lcm symlink."""
    if not (HERMES / "profiles").is_dir():
        return
    try:
        target = PLUGIN_DIR.resolve()
    except Exception:
        return
    if not target.is_dir():
        return
    for pd in sorted((HERMES / "profiles").glob("*/plugins")):
        want = pd / "lcm-pg"
        old = pd / "hermes-lcm"
        if want.exists() or want.is_symlink():
            cur = want.resolve() if want.is_symlink() else want
            if cur != target and want.is_symlink():
                if dry:
                    print(f"dry: relink {want} -> {target}")
                    continue
                try:
                    want.unlink()
                    want.symlink_to(target)
                    print(f"overlay: relink {want} -> {target}")
                except Exception as e:
                    _fail(f"relink {want}: {e}")
            continue
        # only add where this profile already references the lcm code
        references = old.is_symlink() and (old.resolve() == target)
        if not references:
            continue
        if dry:
            print(f"dry: {want} -> {target} (profile needs the lcm-pg dir name to gate)")
            continue
        try:
            want.symlink_to(target)
            print(f"overlay: created {want} -> {target}")
        except Exception as e:
            _fail(f"overlay {want}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    plugin_live = (PLUGIN_DIR / "plugin.yaml").is_file() or (PLUGIN_DIR / "__init__.py").is_file()
    if not plugin_live:
        print("migrate-config: lcm-pg plugin dir not live yet; nothing to gate (run after step 1 materialize)")
        return 0
    for cfg in _configs():
        _migrate_config(cfg, plugin_live, args.dry_run)
    _env_schema_stripping(args.dry_run)
    _overlay_dirs(args.dry_run)
    print(f"migrate-config: {'dry' if args.dry_run else 'done'}; errors={_St.errors}")
    return 1 if _St.errors else 0


if __name__ == "__main__":
    sys.exit(main())
