#!/usr/bin/env python3
"""Install/refresh the self-contained lcm-pg bundle.

Layout (all under /home/mybuddy, self-contained):
  /home/mybuddy/lcm-pg/
    pg/          private PostgreSQL 18.6 build (binaries + libs)
    instance/    private cluster data dir (port 54329, socket lcm-pg/run/)
    venv/        own python env: psycopg3 + fastembed (+ model cache)
    run/         unix socket dir
    fork ->      symlink to /home/mybuddy/.hermes/forks/lcm-pg (source of truth)

Plugin surface:
  ~/.hermes/plugins/lcm-pg   -> symlink to fork (one plugin for ALL profiles)
  old per-profile hermes-lcm copies are replaced by the same symlink.

Env: LCM_BACKEND=postgres + LCM_PG_DSN (new DSN) written to root .env and
every profiles/*/.env. New profiles need nothing (engine auto-creates
lcm_<profile> schema; discovery follows the root symlink).

Usage: install_lcm_pg.py [--plan] [--skip-pg-build]
"""
import argparse, json, os, shutil, subprocess, sys, time

HOME = "/home/mybuddy"
BASE = f"{HOME}/lcm-pg"
FORK = f"{HOME}/.hermes/forks/lcm-pg"
PG = f"{BASE}/pg"
DATA = f"{BASE}/instance"
RUN = f"{BASE}/run"
PORT = 54329
DBROLE = "lcm01"
NEW_DSN = f"postgresql:///lcm?host={RUN}&port={PORT}&user={DBROLE}"
ROOT_PLUGIN = f"{HOME}/.hermes/plugins/lcm-pg"
OLD_PLUGIN = f"{HOME}/.hermes/plugins/hermes-lcm"
VENV = f"{BASE}/venv"

def sh(cmd, check=True, quiet=False):
    print("+", cmd)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out and not quiet:
        print("  " + out.replace("\n", "\n  ")[:500])
    if check and r.returncode != 0:
        raise SystemExit(f"FAILED ({r.returncode}): {cmd}\n{out[:1000]}")
    return r

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()

    print("== 0. preflight ==")
    assert os.path.isdir(FORK), f"fork missing: {FORK}"
    assert os.path.isfile(f"{PG}/bin/initdb") or a_plan(a), "private PG not built yet (run build_pg.sh first)"

    if a.plan:
        print(f"would: ensure cluster at {DATA} (port {PORT}), db 'lcm', schema-owner role {DBROLE}")
        print(f"would: symlink {ROOT_PLUGIN} -> {FORK} (backup old hermes-lcm dirs)")
        print(f"would: point profiles/*/plugins/hermes-lcm at {ROOT_PLUGIN}")
        print(f"would: set LCM_PG_DSN={NEW_DSN} in root + profile .env files")
        print("would: auto-backfill summary embeddings into every profile schema "
              "(scripts/embed_auto_backfill.py, summary corpus)")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{HOME}/.hermes/backups/lcm-pg-bundle-{stamp}"
    os.makedirs(backup, exist_ok=True)

    # -- 1. cluster ----------------------------------------------------------------
    print("== 1. private cluster ==")
    if not os.path.isfile(f"{DATA}/PG_VERSION"):
        os.makedirs(DATA, exist_ok=True); os.makedirs(RUN, exist_ok=True); os.chmod(DATA, 0o700)
        os.chmod(RUN, 0o700)
        sh(f"{PG}/bin/initdb -D {DATA} -U {DBROLE} --auth=trust --encoding=UTF8 --locale=C")
    # idempotent config enforcement: a rsync-copied instance/ may carry another
    # host's postgresql.conf/pg_hba.conf — always converge to the canonical shape
    # (local socket only, port 54329, socket dir in the bundle, trust auth, 0700).
    conf = open(f"{DATA}/postgresql.conf").read()
    want = (f"\nport = {PORT}\nlisten_addresses = ''\n"
            f"unix_socket_directories = '{RUN}'\nunix_socket_permissions = 0700\n"
            f"max_connections = 50\nshared_buffers = 256MB\n")
    import re as _re
    for key in ("port", "listen_addresses", "unix_socket_directories",
                "unix_socket_permissions", "max_connections", "shared_buffers"):
        conf = _re.sub(rf"^\s*{key}\s*=.*$", "", conf, flags=_re.M)
    with open(f"{DATA}/postgresql.conf", "w") as f:
        f.write(conf + want)
    with open(f"{DATA}/pg_hba.conf", "w") as f:
        f.write("# local-only, self-contained instance (socket dir is 0700)\nlocal all all trust\n")
    sh(f"{PG}/bin/pg_ctl -D {DATA} -l {BASE}/logfile -w -t 30 start", check=False)  # start if not running
    psql = f"{PG}/bin/psql -h {RUN} -p {PORT} -U {DBROLE} -d postgres -v ON_ERROR_STOP=1"
    sh(f"{psql} -c \"CREATE DATABASE lcm OWNER {DBROLE}\"", check=False)
    sh(f"{PG}/bin/psql -h {RUN} -p {PORT} -U {DBROLE} -d lcm -c 'CREATE EXTENSION IF NOT EXISTS vector'", check=False)
    sh(f"{PG}/bin/psql -h {RUN} -p {PORT} -U {DBROLE} -d lcm -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'")
    print(sh(f"{PG}/bin/psql -h {RUN} -p {PORT} -U {DBROLE} -d lcm -Atc 'select current_user, current_database(), extname from pg_extension limit 3'").stdout.strip())

    # -- 2. plugin symlink (one plugin, all profiles) --------------------------------
    print("== 2. plugin symlink ==")
    for old in (OLD_PLUGIN,):
        if os.path.isdir(old) and not os.path.islink(old):
            sh(f"mv {old} {backup}/orig-" + os.path.basename(old))
    if os.path.islink(ROOT_PLUGIN):
        os.remove(ROOT_PLUGIN)
    if os.path.isdir(ROOT_PLUGIN):
        sh(f"mv {ROOT_PLUGIN} {backup}/orig-lcm-pg")
    os.symlink(FORK, ROOT_PLUGIN)
    print("symlink:", ROOT_PLUGIN, "->", FORK)
    for p in sorted(sh(f"ls -d {HOME}/.hermes/profiles/*/plugins 2>/dev/null", check=False).stdout.split()):
        lp = os.path.join(p, "hermes-lcm")
        if os.path.islink(lp):
            os.remove(lp)
        elif os.path.isdir(lp):
            sh(f"mv {lp} {backup}/profile-" + lp.replace('/', '__'))
        os.makedirs(p, exist_ok=True)
        os.symlink(ROOT_PLUGIN, lp)
        print("profile overlay:", lp, "->", ROOT_PLUGIN)

    # -- 3. env files ----------------------------------------------------------------
    print("== 3. env files ==")
    env_files = [f"{HOME}/.hermes/.env"] + sorted(
        subprocess.run(f"ls {HOME}/.hermes/profiles/*/.env 2>/dev/null", shell=True,
                       capture_output=True, text=True).stdout.split())
    for ef in env_files:
        if not os.path.exists(ef):
            continue
        shutil.copy2(ef, f"{backup}/{os.path.basename(os.path.dirname(ef)) or 'root'}-env")
        txt = open(ef).read()
        lines = []
        for ln in txt.splitlines():
            s = ln.strip()
            if s.startswith("LCM_PG_DSN=") or s.startswith("LCM_BACKEND="):
                continue
            lines.append(ln)
        lines.append("LCM_BACKEND=postgres")
        lines.append(f"LCM_PG_DSN={NEW_DSN}")
        open(ef, "w").write("\n".join(lines) + "\n")
        print("updated:", ef)

    # -- 4. auto-embed summaries (semantic recall live from install) --------------
    # Populate summary embeddings for every profile schema so hybrid (vector+FTS)
    # recall is active out of the box. The runner defaults to apply + summary
    # corpus; --limit bounds per-schema work. Postgres-native borrow-per-operation
    # means it holds only 1 idle backend/schema, so it slots into the cluster
    # headroom without competing with the daemons. Chunk embeddings (raw-message
    # text) are opt-in separately. Non-fatal: an embedding hiccup must not fail an
    # otherwise-good install.
    print("== 3a. schema parity (rowid shims; backfill maintenance SELECTs rowid on inflight) ==")
    parity = f"{FORK}/scripts/schema_parity.sh"
    if os.path.isfile(parity):
        r = subprocess.run(["bash", parity], capture_output=True, text=True)
        for line in (r.stdout or "").splitlines()[-3:]:
            print("  ", line)
        if r.returncode != 0:
            print(f"  schema_parity exited {r.returncode} (non-fatal): "
                  f"{(r.stderr or '').strip()[:200]}")
    else:
        print("  skipped (schema_parity.sh not found; run it manually before backfill)")
    print("== 4. auto-backfill summary embeddings ==")
    runner = f"{FORK}/scripts/embed_auto_backfill.py"
    interpreters = [f"{VENV}/bin/python", f"{BASE}/venv/bin/python", sys.executable]
    interp = next((p for p in interpreters
                   if p and os.path.exists(p)), None)
    if interp and os.path.isfile(runner):
        env = dict(os.environ,
                   LCM_BACKEND="postgres", LCM_PG_DSN=NEW_DSN,
                   LCM_PG_POOL_MIN="1", LCM_PG_POOL_MAX="1")
        # runner defaults to apply + summary corpus; --limit bounds per-schema work
        r = subprocess.run([interp, runner, "--limit", "500"], env=env,
                           capture_output=True, text=True)
        for line in (r.stdout or "").splitlines():
            print("  ", line)
        if r.returncode != 0:
            print(f"  backfill exited {r.returncode} (non-fatal): "
                  f"{(r.stderr or '').strip()[:200]}")
    else:
        print("  skipped (runner or interpreter not found; run "
              "scripts/embed_auto_backfill.py --apply manually)")

    print("done. DSN:", NEW_DSN)
    print("next: restart hermes daemons (systemctl --user restart <units>) then run verify_lcm_pg.py")

def a_plan(a):
    return getattr(a, "plan", False)

if __name__ == "__main__":
    main()
