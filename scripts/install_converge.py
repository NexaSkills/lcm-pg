#!/usr/bin/env python3
"""Idempotent lcm-pg fleet install on ONE host (safe to run repeatedly).

Converges the host onto the fork's CURRENT code without gateway restarts or
cluster re-provisioning (that heavy path is rollout_host.sh install, meant
for fresh hosts):

  1. rsync the fork working tree into place (git tree on remote -> symlink
     target; falls back to the live plugin dir if it's a real dir)
  2. land-verify HEAD == coordination-host HEAD
  3. schema_parity.sh (rowid/CHECK shims for schemas created pre-parity)
  4. embed_auto_backfill.py --limit N (first fill; non-fatal)
  5. embed timer install (hourly drift self-heal afterwards)
Usage: install_converge.py [--limit 500] [--skip-backfill]
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
FORK = HOME / ".hermes/forks/lcm-pg"
BUNDLE = HOME / "lcm-pg"
DSN = f"postgresql:///lcm?host={BUNDLE}/run&port=54329&user=lcm01"


def sh(cmd, **kw):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def plugin_dir():
    for cand in (HOME / ".hermes/plugins/lcm-pg", HOME / ".hermes/plugins/hermes-lcm"):
        if cand.is_dir() or cand.is_symlink():
            return cand.resolve()
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--skip-backfill", action="store_true")
    a = ap.parse_args()

    # 1. fork tree must exist here (xfer step already put it); verify it's current
    ok, head, _ = sh(["git", "-C", str(FORK), "rev-parse", "--short", "HEAD"])
    if ok != 0:
        print(f"FATAL: fork missing/not a git tree at {FORK}")
        return 1
    print(f"fork HEAD here: {head}")
    src = Path(sys.argv[0]).resolve()  # not used; fork already synced by caller

    # plugin wiring: the live plugin must resolve to the fork
    live = plugin_dir()
    if live is None:
        print("WARNING: no plugin dir found (install_lcm_pg.py not run?) — continuing")
    elif live != FORK.resolve():
        print(f"NOTE: plugin resolves to {live}, not the fork; run install_lcm_pg.py once for rewiring")
    else:
        print("plugin -> fork: OK (live code == fork HEAD)")

    env = dict(os.environ, LCM_PG_DSN=DSN)
    py = str(BUNDLE / "venv/bin/python")
    if not Path(py).is_file():
        print(f"FATAL: {py} missing (run install_lcm_pg.py first)")
        return 1

    # 2. schema parity (idempotent)
    rc, out, err = sh(["bash", str(FORK / "scripts/schema_parity.sh")], env=env)
    print("schema_parity:", (out or err).splitlines()[-1] if (out or err) else "ok", f"rc={rc}")

    # 3. first-fill backfill
    if not a.skip_backfill:
        rc, out, err = sh([py, str(FORK / "scripts/embed_auto_backfill.py"),
                           "--limit", str(a.limit)], env=env)
        tail = (out or err).splitlines()[-6:]
        print("backfill rc=", rc)
        for ln in tail:
            print("  ", ln)

    # 4. timer (drift self-heal forever)
    rc, out, err = sh(["bash", str(FORK / "scripts/embed_timer_install.sh")], env=env)
    print("timer:", (out or err) or f"rc={rc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
