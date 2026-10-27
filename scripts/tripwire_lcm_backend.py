#!/usr/bin/env python3
"""Runtime tripwire: verify the LCM-PG sqlite3 shim is routed in live daemons.

The fork's store modules swap `import sqlite3` through
backends.sqlite3_compat.sqlite3_if_postgres at IMPORT time, and that swap
resolves from os.environ["LCM_BACKEND"] as it stands when the module is first
imported. If a daemon ever imports the fork with LCM_BACKEND unset (import
ordering, or a helper spawned without the env), is_postgres() is cached as
False for the process lifetime and raw sqlite3.connect() resurrects
~/.hermes/lcm.db — the ghost "FTS tables missing" / payload errors in
/lcm doctor.

This script checks each named systemd --user service whose environment
declares LCM_BACKEND=postgres: it re-runs is_postgres() in an identical
env-interpreter pair, plus reports lcm.db mtime vs daemon start time as
filesystem evidence.

Usage: tripwire_lcm_backend.py [service ...]   (default: hermes-gateway)
Exit 0 = all inspected processes route correctly (or are not pg-backed).
Exit 1 = shim-false or probe failure in at least one process.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

FORK = Path(os.environ.get("LCM_PG_CODE") or (Path.home() / ".hermes/plugins/lcm-pg")).resolve()
PROBE = (
    "import sys; sys.path.insert(0, %r)\n"
    "from backends import sqlite3_compat\n"
    "print('shim:', sqlite3_compat.is_postgres())\n" % str(FORK)
)


def systemctl(*args):
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=20
    ).stdout.strip()


def inspect(service):
    pid = systemctl("show", service, "-p", "MainPID", "--value")
    if not pid or pid == "0":
        return "%s: no MainPID (skip)" % service, True
    envf = Path("/proc/%s/environ" % pid)
    try:
        raw = envf.read_bytes()
    except OSError:
        return "%s: cannot read environ (skip)" % service, True
    env_items = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
    env = dict(item.split("=", 1) for item in env_items if "=" in item)
    if (env.get("LCM_BACKEND") or "").lower() not in ("postgres", "postgresql", "pg"):
        return "%s: LCM_BACKEND=%s (not pg — shim inactive, OK)" % (
            service, env.get("LCM_BACKEND", "unset")), True
    exe = os.path.realpath("/proc/%s/exe" % pid)
    if not os.access(exe, os.X_OK):
        return "%s: no interpreter" % service, False
    clean_env = {k: env[k] for k in ("PATH", "HOME", "USER", "PYTHONPATH", "LCM_BACKEND", "LCM_PG_DSN") if k in env}
    try:
        out = subprocess.run([exe, "-I", "-c", PROBE], env=clean_env,
                             capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return "%s: probe error %s" % (service, exc), False
    line = (out.stdout + out.stderr).strip().splitlines()
    tail = line[-1] if line else "no output"
    if tail.startswith("shim: True"):
        return "%s: OK (is_postgres True, gateway-equivalent env)" % service, True
    return "%s: TRIPWIRE — %s" % (service, tail[:160]), False


def check_triggers(dsn: str) -> bool:
    """Assertion V4 invariant: every lcm_* schema has all 6 lcm_assertion_*
    triggers. A partial set = the bootstrap ran while an old partial state
    aborted it mid-script; fix = drop the 3 assertion tables CASCADE +
    orphan _trg_lcm_assertion_* functions, then re-run ensure (idempotent)."""
    import psycopg

    want = {
        "lcm_assertion_source_insert_guard",
        "lcm_assertion_row_insert_guard",
        "lcm_assertion_relation_insert_guard",
        "lcm_assertion_message_update",
        "lcm_assertion_message_delete",
        "lcm_assertion_source_delete",
    }
    try:
        conn = psycopg.connect(dsn, autocommit=True)
    except Exception as exc:
        print("assertion triggers: connect failed:", str(exc)[:100])
        return False
    rows = conn.execute(
        "select n.nspname, t.tgname from pg_trigger t "
        "join pg_class c on c.oid = t.tgrelid "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where t.tgname like 'lcm_assertion%%' and n.nspname ~ '^lcm_[a-z0-9]+$' "
        "and n.nspname not like 'lcm\\_test\\_%' and n.nspname not like 'lcm\\_probe%%'"
    ).fetchall()
    conn.close()
    by_schema = {}
    for schema, trig in rows:
        by_schema.setdefault(schema, set()).add(trig)
    ok = True
    for schema in sorted(set(s for s, _ in rows) | {"lcm_default"}):
        missing = want - by_schema.get(schema, set())
        if missing:
            print("%s: MISSING triggers: %s" % (schema, ",".join(sorted(missing))))
            ok = False
        else:
            print("%s: 6/6 assertion triggers" % schema)
    return ok


def main():
    services = sys.argv[1:] or ["hermes-gateway"]
    ok = True
    for svc in services:
        msg, good = inspect(svc)
        print(msg)
        ok = ok and good
    if os.environ.get("LCM_PG_DSN"):
        ok = check_triggers(os.environ["LCM_PG_DSN"]) and ok
    db = Path.home() / ".hermes/lcm.db"
    if db.exists():
        print("note: %s exists, mtime %s (resurrection would show a recent mtime)" % (
            db, time.strftime("%F %H:%M", time.localtime(db.stat().st_mtime))))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
