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
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # PyYAML ships with hermes envs; bundle venv may lack it
    print("migrate-config: PyYAML unavailable; skipping (configs unchanged)")
    sys.exit(0)

HOME = Path(os.environ.get("HOME", "/home/mybuddy"))
HERMES = Path(os.environ.get("HERMES_HOME", str(HOME / ".hermes")))
PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR", str(HERMES / "plugins" / "lcm-pg")))
TS = time.strftime("%Y%m%d%H%M%S")


class _St:
    errors = 0


def _fail(msg: str) -> None:
    print(f"ERROR {msg}")
    _St.errors += 1


def _backup_write(path: Path, text: str) -> bool:
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
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        _fail(f"parse {path}: {e}")
        return
    if not isinstance(data, dict):
        _fail(f"{path}: top level not a mapping")
        return
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        if not plugin_live:
            return
        plugins = {}
        data["plugins"] = plugins
    enabled = plugins.get("enabled")
    if enabled is not None and not isinstance(enabled, list):
        _fail(f"{path}: plugins.enabled is not a list")
        return
    actions = []
    if plugin_live:
        if "lcm-pg" not in (enabled or []):
            plugins["enabled"] = [*(enabled or []), "lcm-pg"]
            actions.append("enabled += lcm-pg")
    disabled = plugins.get("disabled")
    if isinstance(disabled, list) and "lcm-pg" in disabled:
        plugins["disabled"] = [x for x in disabled if x != "lcm-pg"]
        actions.append("disabled -= lcm-pg (would defeat deploy)")
    if not actions:
        return
    if dry:
        print(f"dry: {'; '.join(actions)} -> {path}")
        return
    if _backup_write(path, yaml.safe_dump(data, sort_keys=False)):
        print(f"{'; '.join(actions)} -> {path.name} ({path.parent})")


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
    _overlay_dirs(args.dry_run)
    print(f"migrate-config: {'dry' if args.dry_run else 'done'}; errors={_St.errors}")
    return 1 if _St.errors else 0


if __name__ == "__main__":
    sys.exit(main())
