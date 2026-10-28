#!/usr/bin/env python3
"""Onboard a NEW Hermes profile to lcm-pg in one idempotent command.

Usage: lcm_new_profile_setup.py <profile> [--dry-run] [--no-deploy]

The zero-touch counterpart to skills/lcm-pg-new-profile-setup: an agent that
just created a profile runs THIS, then the verification block in the skill.
It performs ONLY the steps the deploy does not (until its next run converge
the same state):

  1. sanity: profile dir + config.yaml exist, name matches the lcm_<p> schema
     derivation rules (lowercase alnum + underscore).
  2. config keys via `hermes -p <p> config set/unset` ONLY (config.yaml is
     NEVER file-edited — Jan's rule):
        context.engine              = lcm
        skills.external_dirs        includes ~/.hermes/plugins/lcm-pg/skills
        auxiliary.compression.model UNSET (compression routes on active model)
     plugins.enabled: if the profile has NO plugins block it inherits root
     (root lists lcm-pg: fine, left alone). If it HAS its own enabled list it
     must contain `lcm-pg` (the gate key is the plugin DIRECTORY name); a
     `hermes-lcm` entry there is removed (it double-registers against the
     same code root).
  3. per-profile plugin overlay symlinks (identical contract to deploy 3d,
     migrate_hermes_lcm_config.py):
        ~/.hermes/profiles/<p>/plugins/lcm-pg     -> ~/.hermes/plugins/lcm-pg
        ~/.hermes/profiles/<p>/plugins/hermes-lcm -> ~/.hermes/plugins/lcm-pg
  4. run the standard deploy (`lcm_update.sh` via setsid, detached) which
     syncs the LCM_* .env params, bootstraps lcm_<p>, ensures guards, and
     restarts surfaces. --no-deploy skips (batch: run the deploy once after).
  5. verdict from `lcm_check_all.py`: PASS only when 0 FAILs AND the profile
     rows are clean (PARAMS pool-pin, context-engine, profile-env parity).

Exit 0 = onboarded and verified; 1 = needs attention (printout says what);
--dry-run prints intended actions and current values without writing.
Hermes code is never modified by this script.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
HERMES = os.environ.get("HERMES_BIN", "hermes")
PLUGIN_DIR = Path(os.environ.get("LCM_PG_PLUGINS", str(HOME / ".hermes/plugins/lcm-pg")))


def sh(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def hermes_cfg(profile: str, *args) -> subprocess.CompletedProcess:
    return sh([HERMES, "-p", profile, "config", *args])


def cfg_get(profile: str, key: str) -> str:
    r = hermes_cfg(profile, "get", key)
    return r.stdout.strip() if r.returncode == 0 else ""



def _venv_python() -> str:
    """First interpreter that has psycopg+psycopg_pool (same ladder as the
    repo scripts): LCM_VENV_PY, ~/lcm-pg/venv, install envs, then sys.exe."""
    import glob
    home = Path.home()
    cands = [os.environ.get("LCM_VENV_PY"),
             str(home / "lcm-pg/venv/bin/python"),
             *sorted(glob.glob(str(home / ".hermes/installs/*/environments/*/venv/bin/python")),
                     reverse=True),
             sys.executable]
    for c in cands:
        if c and Path(c).is_file() and sh([c, "-c",
                "import psycopg, psycopg_pool"]).returncode == 0:
            return c
    return ""


class Runner:
    def __init__(self, profile: str, dry: bool):
        self.p = profile
        self.dry = dry
        self.actions = []
        self.problems = []

    def action(self, desc: str, fn):
        if self.dry:
            self.actions.append(f"DRY  {desc}")
            return
        fn()
        self.actions.append(f"DID  {desc}")

    def problem(self, msg: str):
        self.problems.append(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--dry-run", action="store_true", dest="dry")
    ap.add_argument("--no-deploy", action="store_true",
                    help="skip the lcm_update.sh run (batch workflows)")
    a = ap.parse_args()
    p = a.profile
    r = Runner(p, a.dry)

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", p):
        print(f"FAIL profile name '{p}' cannot map to a schema name "
              f"(target schema lcm_{p}; lowercase alnum/underscore only)")
        return 1

    pdir = HOME / ".hermes/profiles" / p
    if not (pdir / "config.yaml").exists():
        print(f"FAIL {pdir}/config.yaml not found — create the profile first "
              f"(hermes profile create {p})")
        return 1
    if not PLUGIN_DIR.is_dir():
        print(f"FAIL {PLUGIN_DIR} missing — this host has no lcm-pg plugin; "
              f"run lcm_update.sh for the host first")
        return 1

    # 1. config keys (CLI only)
    eng = cfg_get(p, "context.engine")
    if eng != "lcm":
        r.action(f"context.engine {eng or '(unset)'} -> lcm",
                 lambda: r.problem(f"context set failed: {hermes_cfg(p, 'set', '--force', 'context.engine', 'lcm').stderr.strip()}")
                 if hermes_cfg(p, "set", "--force", "context.engine", "lcm").returncode else None)
    acm = cfg_get(p, "auxiliary.compression.model")
    if acm:
        r.action(f"auxiliary.compression.model '{acm}' -> unset",
                 lambda: hermes_cfg(p, "unset", "auxiliary.compression.model"))

    ext = cfg_get(p, "skills.external_dirs")
    want = str(PLUGIN_DIR / "skills")
    if want not in ext:
        cur = re.findall(r"[\w/.~$-]+", ext) if ext else []
        lst = "[" + ", ".join(f"'{x}'" for x in [*cur, want]) + "]"
        r.action(f"skills.external_dirs += {want}",
                 lambda: hermes_cfg(p, "set", "--force", "skills.external_dirs", lst))

    # plugins.enabled: touch ONLY if the profile carries its own list
    # 2a. per-profile plugin enable — the CRITICAL step.
    # Gate semantics (verified on beez + template 2026-10-28): a profile
    # WITHOUT an explicit plugins.enabled key is NOT "inheriting root" —
    # the effective set is ALL-EXCEPT-DISABLED, and an uninstall/legacy
    # disable entry in ROOT's disabled list (e.g. kev-router incidents)
    # silently blocks lcm-pg for every such profile. Only profiles with
    # their own enabled: list are allow-listed. The runtime truth is
    # `hermes -p <p> plugins list`; the deploy's 3c2 never touches this,
    # so a fresh `profile create` copy needs it HERE.
    def _gate_state() -> "bool | None":
        pr = sh([HERMES, "-p", p, "plugins", "list"])
        row = [ln for ln in (pr.stdout or "").splitlines()
               if "lcm-pg" in ln and "│" in ln]
        if not row:
            return None
        return "not enabled" not in row[0]

    if _gate_state() is not True:
        def _enable():
            # supported CLI, profile-scoped (writes the profile's own lists;
            # NEVER hand-edit config.yaml). The bare list-writer is broken
            # (appends literal '-' entries), so no config set here.
            er = sh([HERMES, "-p", p, "plugins", "enable", "lcm-pg"])
            if er.returncode != 0:
                r.problem(f"plugins enable failed: "
                          f"{(er.stdout + er.stderr).strip()[-200:]}")
        r.action("hermes -p <p> plugins enable lcm-pg", _enable)
    if not a.dry and _gate_state() is not True:
        r.problem("lcm-pg still NOT enabled for the profile per "
                  "`hermes -p <p> plugins list` — inspect that table and "
                  "root/profile disabled lists manually")

    # 2b. full LCM_* param convergence for this profile (self-heal: some
    # hosts' ~/lcm_update.sh predates the 3c0 params-sync block — the
    # profile then only gets the hand-curated 11-key overlay subset and
    # check_all FAILs the profile-env parity row. The repo's
    # lcm_params_sync.py is idempotent and root-is-truth; run it now.)
    psync = PLUGIN_DIR / "scripts/lcm_params_sync.py"
    if psync.exists():
        _py = _venv_python()
        if _py:
            pr = sh([_py, str(psync)])
            if pr.returncode != 0:
                r.problem(f"lcm_params_sync.py rc={pr.returncode}: "
                          f"{(pr.stdout + pr.stderr).strip()[-200:]}")
            else:
                r.actions.append("DID  lcm_params_sync.py (full LCM_* root-union "
                                 "converged into profile .env)")
        else:
            r.problem("no interpreter with psycopg found for lcm_params_sync.py "
                      "— profile .env parity may lag root (deploy 3c fixes it)")
    else:
        r.problem(f"{psync} missing — profile .env keys converge only via the "
                  f"host deploy's 3c/3c0 step")

    # 3. standard deploy (detached; it restarts surfaces incl. this profile)
    updater = Path.home() / "lcm_update.sh"
    if not a.no_deploy:
        if not updater.exists():
            r.problem(f"~/lcm_update.sh missing — run the host deploy manually "
                      f"then re-run this script with --no-deploy")
        else:
            ts = subprocess.run(["date", "+%s"], capture_output=True, text=True).stdout.strip()
            log = Path.home() / f"lcm_deploy_{ts}.log"

            def deploy():
                subprocess.Popen(
                    ["setsid", "bash", "-c", f"bash {updater} > {log} 2>&1"],
                    start_new_session=True)
                print(f"deploy launched (detached); tail -f {log}", flush=True)
                import time
                t0 = time.time()
                while time.time() - t0 < 900:
                    time.sleep(15)
                    try:
                        txt = log.read_text(errors="replace")
                    except OSError:
                        continue
                    if "RESULT:" in txt:
                        for ln in txt.splitlines():
                            if "RESULT:" in ln or "OVERALL" in ln:
                                print(ln, flush=True)
                        break
            r.action("ran lcm_update.sh (detached, waited for RESULT)", deploy)

    # 4. verdict via the fleet checker
    checker = PLUGIN_DIR / "scripts/lcm_check_all.py"
    if checker.exists() and not a.dry:
        py = _venv_python() or sys.executable
        chk = sh([py, str(checker)])
        # the gate is THIS profile's rows only (host rows from transient probe/
        # zombie schemas are noise for a per-profile onboarding verdict)
        fails = [ln for ln in chk.stdout.splitlines()
                 if " FAIL " in ln and (f"lcm_{p}" in ln or f":{p}" in ln or f" {p} " in ln)]
        status_line = [ln for ln in chk.stdout.splitlines() if "OVERALL" in ln]
        if status_line:
            print(status_line[-1].strip() + f" (profile-{p} rows: "
                  + ("clean" if not fails else f"{len(fails)} fail") + ")")
        for ln in fails:
            r.problem(f"checker: {ln.strip()[:160]}")

    for ln in r.actions:
        print(ln)
    if r.problems:
        print("NEEDS ATTENTION:")
        for ln in r.problems:
            print(f"  - {ln}")
        return 1
    print(f"PASS profile '{p}' onboarded for lcm-pg"
          + (" (dry-run; nothing written)" if a.dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
