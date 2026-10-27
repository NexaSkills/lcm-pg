#!/usr/bin/env python3
"""lcm_params_sync.py — deploy-time convergence of EVERY profile's LCM
parameter set, written exclusively via `hermes config set` / the profile
.env writer (Jan directives 2026-10-27):

  * config.yaml values  -> hermes -p <profile> config set --force <key> <value>
  * .env values         -> the profile .env file (the ONE file deploy may edit)

Never reads or writes /etc for any config.yaml. Never seds a config.yaml.

Convergence target (params are repo-owned, host values flow root->profile):
  .env      : every LCM_* key present in the ROOT .env must carry the SAME
              value in every profile .env (backend/DSN/boundary/pool/embeddings
              /slash/fresh-tail/thresholds/rollups/externalization — the full
              set, not a hand-picked subset: the fleet drift incidents were
              always a key someone forgot to add to the sync list).
  config    : context.engine == lcm (root-inheritance honoured: an unset key
              that resolves to root's lcm is already correct).

Idempotent; --dry-run prints planned writes only. Exit 0 = converged,
1 = a write failed (check_all re-verifies independently).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
# IMPORTANT: an agent shell exports HERMES_HOME=.../profiles/<name> (profile
# home). The HOST's hermes tree is $HOME/.hermes; HERMES_HOME there would
# point one profile into itself. Only honour it when it names the root tree
# (an actual deploy running with HERMES_HOME=$HOME/.hermes).
_env_home = os.environ.get("HERMES_HOME", "")
_env_home_p = Path(_env_home).expanduser() if _env_home else None
if _env_home_p and _env_home_p.parent == HOME / ".hermes" and _env_home_p.name != "profiles":
    HERMES = _env_home_p          # custom root install dir
elif _env_home_p and _env_home_p.parent.parent == HOME / ".hermes":
    HERMES = _env_home_p.parent.parent   # a profile home => use the HOST root
else:
    HERMES = HOME / ".hermes"
TS = time.strftime("%Y%m%d%H%M%S")


def _hermes_bin() -> str:
    for cand in (os.environ.get("HERMES_BIN"),
                 shutil.which("hermes"),
                 str(HOME / ".local/bin/hermes")):
        if cand and Path(cand).exists() and Path(cand).stat().st_mode & 0o111:
            return str(cand)
    return "hermes"


def cfg_get(profile: str | None, key: str) -> tuple[bool, str]:
    """(found, value) as the running Hermes resolves it for this profile."""
    bin_ = _hermes_bin()
    cmd = [bin_] + (["-p", profile] if profile else []) + ["config", "get", key]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return False, ""
    return True, p.stdout.strip()


def cfg_set(profile: str | None, key: str, value: str) -> bool:
    bin_ = _hermes_bin()
    cmd = [bin_] + (["-p", profile] if profile else []) + \
          ["config", "set", "--force", key, value]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        print(f"ERROR cfg set {key} ({profile or 'root'}): "
              f"{(p.stdout + p.stderr).strip().splitlines()[-1:]}")
        return False
    return True


def _cfg_unset(profile: str | None, key: str) -> bool:
    """Remove a config.yaml key via the CLI only (hermes [-p p] config unset).
    Falls back to a WARN + manual hint when this Hermes build has no unset
    subcommand; never touches the file directly."""
    import subprocess
    b = _hermes_bin()
    cmd = [b] + (["-p", profile] if profile else []) + ["config", "unset", key]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        return True
    msg = (r.stderr or r.stdout or "").strip().splitlines()
    print(f"cfg unset FAILED ({profile or 'root'} {key}): {msg[-1] if msg else 'rc=' + str(r.returncode)}"
          "  -> manual: hermes" + (f" -p {profile}" if profile else "") + f" config unset {key}")
    return False


def _env_keys(path: Path) -> dict[str, str]:
    # same parsing as backends/postgres.py:_load_dotenv_from_home (the runtime
    # reader this check must mirror): the LAST definition of a key wins (a
    # duplicated export late in the file silently overrides earlier ones),
    # and an EMPTY value counts as unset — the runtime `or` chains fall back
    # to the code default, which the profile must not silently diverge from.
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for ln in path.read_text(errors="ignore").splitlines():
        ln = ln.strip()
        if ln.startswith("export "):
            ln = ln[len("export "):]
        if ln.startswith("LCM_") and "=" in ln and not ln.startswith("#"):
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return {k: v for k, v in out.items() if v}


def _env_set(path: Path, key: str, value: str, dry: bool) -> bool:
    # .env is THE file deploy may edit on file level (config.yaml never is).
    if dry:
        print(f"dry: {path} {key}={value}")
        return True
    try:
        if path.is_file():
            shutil.copy2(path, f"{path}.pre-params-sync.{TS}")
        lines = path.read_text().splitlines(keepends=True) if path.is_file() else []
        hit = [i for i, l in enumerate(lines)
               if l.strip().startswith(f"{key}=") or
               l.strip().startswith(f"export {key}=")]
        if hit:
            lines[hit[0]] = f"{key}={value}\n"
            for i in hit[1:]:            # dedupe stray duplicates
                lines[i] = ""
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}={value}\n")
        path.write_text("".join(lines))
        return True
    except Exception as e:
        print(f"ERROR env write {path} {key}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    errors = 0

    # Host-wide LCM_* surface = root .env UNION every profile .env (root
    # values win ties). systemd surfaces load root .env + the ACTIVE profile
    # .env (20-active-profile-env.conf), so a key set ANYWHERE on the host is
    # the host's value — every profile must carry it or the same surface
    # behaves differently per profile (drift = the fleet incidents).
    root_env = _env_keys(HERMES / ".env")
    for _ef in sorted((HERMES / "profiles").glob("*/.env")) if (HERMES / "profiles").is_dir() else []:
        for k, v in _env_keys(_ef).items():
            root_env.setdefault(k, v)
    if not root_env:
        print("FAIL no LCM_* keys anywhere (root .env and profile .envs empty?)")
        return 1

    # A profile whose dir .env is NOT the same file as root .env must be
    # synced to that file independently. The bare name "default" resolves to
    # ROOT .env (that is how Hermes names root), but profiles/default/ can be
    # a DISTINCT dir (beez) — skipping it "because the name matches" left a
    # 14-key hole that check_all then FAILs on (self-contradiction seen
    # 2026-10-27: sync OK, verify FAIL). Dedupe by RESOLVED FILE, not name.
    profiles = ["default"]
    if (HERMES / "profiles").is_dir():
        profiles += sorted(p.name for p in (HERMES / "profiles").iterdir()
                           if p.is_dir() and not p.name.startswith("."))

    # ---- .env convergence (full LCM_* surface, root is the source of truth)
    seen_envs: set = set()
    for prof in profiles:
        penv = (HERMES / "profiles" / prof / ".env")
        if not penv.is_file() and prof == "default":
            penv = HERMES / ".env"
        rp = penv.resolve()
        if rp in seen_envs:
            continue  # same physical file under two names — sync once
        seen_envs.add(rp)
        have = _env_keys(penv)
        drift = {k: v for k, v in root_env.items() if have.get(k) != v}
        if not drift:
            print(f"env {prof}: OK ({len(root_env)} LCM_* keys match root)")
            continue
        for k, v in sorted(drift.items()):
            ok = _env_set(penv, k, v, a.dry_run)
            print(f"env {prof}: {k} {have.get(k, '<unset>')!r} -> {v!r} "
                  f"{'ok' if ok else 'FAILED'}")
            if not ok:
                errors += 1

    # ---- config.yaml convergence via hermes config get/set ONLY ------------
    root_engine_found, root_engine = cfg_get(None, "context.engine")
    for prof in profiles:
        found, eng = cfg_get(prof if prof != "default" else None, "context.engine")
        if found and eng == "lcm":
            continue
        if not found and root_engine_found and root_engine == "lcm":
            continue  # legitimate root inheritance
        if a.dry_run:
            print(f"dry: cfg set context.engine lcm -> profile {prof}")
            continue
        ok = cfg_set(prof if prof != "default" else None, "context.engine", "lcm")
        print(f"cfg {prof}: context.engine {eng!r} -> lcm {'ok' if ok else 'FAILED'}")
        if not ok:
            errors += 1

    # ---- auxiliary.compression.model: unset everywhere (follow main) ------
    # Jan 2026-10-27: the compression summary must run on the profile's ACTIVE
    # model, never a separate aux pin. Hermes resolves an UNSET aux compression
    # model to the main/current runtime model (# compression-current-model),
    # so the correct converged value is ABSENT, not "main".  An explicit pin
    # (anything set, incl. "main") is removed via `hermes config set --unset`;
    # if the CLI cannot unset, WARN with the manual command.
    for prof in profiles:
        pa = prof if prof != "default" else None
        found, val = cfg_get(pa, "auxiliary.compression.model")
        if not found or not str(val).strip():
            continue  # unset/blank already means "follow the main model"
        if a.dry_run:
            print(f"dry: cfg unset auxiliary.compression.model -> profile {prof} (was {val!r})")
            continue
        ok = _cfg_unset(pa, "auxiliary.compression.model")
        print(f"cfg {prof}: auxiliary.compression.model {val!r} -> unset (follow main) "
              f"{'ok' if ok else 'FAILED'}")
        if not ok:
            errors += 1

    print(f"params-sync: profiles={len(profiles)} errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
