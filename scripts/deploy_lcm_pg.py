#!/usr/bin/env python3
"""Phase 4: deploy lcm-pg to this host.

Safe order: stop daemons -> swap plugin dir -> write env -> migrate all
profiles -> start daemons. Rollback = restore plugin symlink + remove env.

Usage: deploy_lcm_pg.py [--plan]
"""
import json, os, shutil, subprocess, sys, time

FORK = "/home/mybuddy/.hermes/forks/lcm-pg"
PLUGIN = "/home/mybuddy/.hermes/plugins/hermes-lcm"
PROFILE_PLUGIN_GLOB = "/home/mybuddy/.hermes/profiles/*/plugins/hermes-lcm"
DSN = "postgresql:///lcm?host=/run/postgresql&user=mybuddy"
ROOT_DB = "/home/mybuddy/.hermes/lcm.db"
UNITS = ["hermes-gateway.service", "hermes-webui.service", "hermes-dashboard.service"]
ENV_FILES = ["/home/mybuddy/.hermes/.env"]
ENV_LINES = (f"LCM_BACKEND=postgres\nLCM_PG_DSN={DSN}\n")

def sh(cmd, check=True):
    print("+", cmd)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print("  " + out.replace("\n", "\n  ")[:600])
    if check and r.returncode != 0:
        raise SystemExit(f"FAILED: {cmd}")
    return r

def main():
    plan = "--plan" in sys.argv
    if plan:
        sh("systemctl --user list-units --all --no-legend | grep -oE 'hermes[a-z0-9-]*\\.service(\\.timer)?' | sort -u", check=False)
        sh(f"ls -ld {PLUGIN} {PROFILE_PLUGIN_GLOB}", check=False)
        sh(f"ls {FORK}/backends/postgres.py", check=False)
        print("would: stop units -> cp fork over plugin dirs (backup first) ->"
              " append LCM_BACKEND/LCM_PG_DSN to ~/.hermes/.env ->"
              " migrate root+4 profiles -> start units -> verify")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"/home/mybuddy/.hermes/backups/lcm-pg-deploy-{stamp}"
    os.makedirs(backup, exist_ok=True)

    # 1. stop daemons
    for u in UNITS:
        if sh(f"systemctl --user is-active {u}", check=False).returncode == 0:
            sh(f"systemctl --user stop {u}", check=False)
    # any standalone profile gateways
    r = sh("systemctl --user list-units --all --no-legend | grep -oE 'hermes-gateway-[a-z0-9]+\\.service' | sort -u", check=False)
    for u in r.stdout.split():
        sh(f"systemctl --user stop {u}", check=False)
    time.sleep(2)

    # 2. swap plugin dirs (root + every profile overlay)
    r = sh(f"bash -c 'ls -d {PLUGIN} {PROFILE_PLUGIN_GLOB} 2>/dev/null'")
    for d in r.stdout.split():
        rel = d.removeprefix("/home/mybuddy/.hermes/").replace("/", "__")
        dst = os.path.join(backup, "orig", rel)
        if os.path.islink(d):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(os.readlink(d), dst)
        else:
            shutil.copytree(d, dst, symlinks=True)
        # remove FTS/cruft files not in fork, then copy fork content in
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.islink(p):
                os.remove(p)
            elif os.path.isdir(p) and not os.path.isdir(os.path.join(FORK, name)):
                shutil.rmtree(p)
            elif os.path.isfile(p) and not os.path.exists(os.path.join(FORK, name)):
                os.remove(p)
        for name in os.listdir(FORK):
            if name == ".git":
                continue
            s, t = os.path.join(FORK, name), os.path.join(d, name)
            if os.path.isdir(s):
                if os.path.isdir(t):
                    shutil.rmtree(t)
                shutil.copytree(s, t, symlinks=True)
            else:
                shutil.copy2(s, t)
        print(f"swapped {d}")

    # 3. env
    for f in ENV_FILES:
        cur = open(f).read() if os.path.exists(f) else ""
        if "LCM_BACKEND" not in cur:
            with open(f, "a") as fh:
                fh.write(("\n" if cur and not cur.endswith("\n") else "") + ENV_LINES)
            print(f"env appended to {f}")

    # 4. migrate root + profiles (root DB is corrupt: salvage what reads)
    mig = os.path.join(FORK, "scripts", "migrate_sqlite_to_pg.py")
    env = dict(os.environ, LCM_PG_DSN=DSN)
    targets = [ROOT_DB] + sorted(
        subprocess.run(f"ls /home/mybuddy/.hermes/profiles/*/lcm.db 2>/dev/null",
                       shell=True, capture_output=True, text=True).stdout.split())
    for db in targets:
        env.pop("LCM_PG_SCHEMA", None)
        print(f"== migrate {db}")
        subprocess.run([sys.executable, mig, db], env=env)

    # 4b. align every schema's sequences above migrated ids
    import subprocess as _sp
    _sp.run([sys.executable, os.path.join(FORK, "scripts", "align_sequences.py")], env=env)

    # 5. start daemons
    for u in UNITS:
        if sh(f"systemctl --user list-unit-files {u} --no-legend", check=False).stdout.strip():
            sh(f"systemctl --user start {u}", check=False)
    for u in r.stdout.split() if r else []:
        sh(f"systemctl --user start {u}", check=False)
    print(f"deployed; backup at {backup}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
