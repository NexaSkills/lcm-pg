#!/usr/bin/env python3
"""lcm_check_all.py — lcm-pg whole-solution health report (host-agnostic).

Deterministic, read-only: never mutates. Writes
  $HOME/lcm-pg-check-<host>.md  (overwrites prior report)
and prints a summary. Exit 0 = no FAIL rows, 1 = at least one FAIL.

Host-agnostic: home/paths via $HOME+env, DSN via LCM_PG_DSN (default the
standard private-cluster socket under $HOME), hermes units discovered via
systemctl --user, schemas from pg_namespace.

Sections: VERSION-LOAD / CLUSTER / SCHEMAS / STRUCTURE / VERIFIER / INGEST /
RECALL / TIMERS / PARAMS / RESIDUE.
"""
from __future__ import annotations
import datetime, json, os, re, subprocess, sys
from pathlib import Path

# deps: prefer the host's venv312 site-packages (psycopg/psycopg_pool); works
# even when this script runs under a bare interpreter.
for _v in (Path.home() / "lcm-pg/venv/lib",):
    if _v.is_dir():
        for _s in sorted(_v.glob("python3.*")):
            _sp = _s / "site-packages"
            if _sp.is_dir() and str(_sp) not in sys.path:
                sys.path.append(str(_sp))

# host-local config resolution order: exported env > host config file > defaults.
_HOSTCFG = Path.home() / ".config" / "lcm-pg" / "host.conf"
if _HOSTCFG.exists():
    for _ln in _HOSTCFG.read_text().splitlines():
        if _ln.startswith("LCM_PG_DSN=") and "LCM_PG_DSN" not in os.environ:
            os.environ["LCM_PG_DSN"] = _ln.split("=", 1)[1]

HOME = Path(os.environ.get("HOME", str(Path.home())))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(HOME / ".hermes")))
PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR", str(HERMES_HOME / "plugins" / "lcm-pg")))
FORK_DIR = Path(os.environ.get("LCM_FORK_DIR", str(HERMES_HOME / "forks" / "lcm-pg")))
DSN = os.environ.get("LCM_PG_DSN", f"postgresql://lcm01@lcm-pg/lcm?host={HOME}/lcm-pg/run&port=54329")
REPORT = Path(os.environ.get("LCM_CHECK_REPORT", str(HOME / f"lcm-pg-check-{os.uname().nodename}.md")))

ROWS: list[dict] = []
NOW = datetime.datetime.now().astimezone()

def add(section, name, status, detail=""):
    ROWS.append({"section": section, "name": name, "status": status, "detail": str(detail)[:500]})

def sh(cmd, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 120), **kw)
        return p.returncode, p.stdout + p.stderr
    except Exception as e:
        return 99, f"{type(e).__name__}: {e}"

def _sql(query, dsn=None):
    dsn = dsn or DSN
    py = HOME / "lcm-pg/venv/bin/python"
    if not py.exists():
        py = Path(sys.executable)
    code = (
        "import psycopg,sys,json\n"
        "c=psycopg.connect(sys.argv[1],autocommit=True)\n"
        "cur=c.execute(sys.argv[2])\n"
        "print(json.dumps(cur.fetchall(),default=str))\n"
    )
    rc, out = sh([str(py), "-c", code, dsn, query], timeout=90)
    if rc != 0:
        return None, out.strip()[-300:]
    try:
        return json.loads(out.strip().splitlines()[-1]), None
    except Exception as e:
        return None, f"parse: {e}"

# ----------------------------------------------------------------- sections
def s_version():
    roots = _plugin_roots()
    # canonical plugin location = the real (non-fork) plugin dirs the agent discovers
    canon = [r for r in roots if "forks" not in str(r)]
    detail, ok = [], False
    for r in roots:
        rc, out = sh(["git", "-C", str(r), "log", "-1", "--format=%h %ci"])
        kind = "fork" if "forks" in str(r) else ("plugin-git" if rc == 0 else ("plugin-symlink/dir" if (r / "plugin.yaml").exists() or (r / "__init__.py").exists() else "?"))
        head = out.strip() if rc == 0 else "-"
        detail.append(f"{r} [{kind}] {head}")
        if "forks" not in str(r) and (r / "__init__.py").exists():
            ok = True
    add("VERSION-LOAD", "plugin-locations", "PASS" if ok else "FAIL",
        " | ".join(detail) if detail else "no lcm-pg plugin dir found in root/profile plugins dirs")
    git_head = None
    for r in roots:
        rc, out = sh(["git", "-C", str(r), "log", "-1", "--format=%h %ci %s"])
        if rc == 0:
            git_head = out.strip()
            break
    rc, out = sh(["journalctl", "--user", "--since", "today", "--no-pager"])
    if git_head:
        m = re.search(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)", git_head)
        try:
            commit_ts = datetime.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").astimezone()
        except Exception:
            commit_ts = None
        units = subprocess.run(["systemctl", "--user", "list-units", "--type=service",
                                "--state=loaded", "--no-legend"],
                               capture_output=True, text=True).stdout
        for u in sorted(set(re.findall(r"hermes-(?:gateway|webui|dashboard)\.service", units))):
            ts = subprocess.run(["systemctl", "--user", "show", u, "-p", "ActiveEnterTimestamp", "--value"],
                                capture_output=True, text=True).stdout.strip()
            try:
                start = datetime.datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %Z").astimezone()
                ok2 = commit_ts is None or start >= commit_ts
                add("VERSION-LOAD", u, "PASS" if ok2 else "FAIL",
                    f"started {start:%F %T} vs latest commit {commit_ts:%F %T}"
                    + ("" if ok2 else " -> restart needed to load current code"))
            except Exception:
                add("VERSION-LOAD", u, "INFO", f"ActiveEnterTimestamp={ts!r}")
    # Gate on errors since each service STARTED, not since midnight: a host
    # repaired at 14:41 must not stay FAIL because of lines the pre-fix code
    # logged at 13:36 — that reads as an unfixed defect and buries whatever is
    # actually broken now. The since-midnight total stays visible as INFO.
    bad = []
    for u in sorted(set(re.findall(r"hermes-\S+\.service", subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service",
             "--state=loaded", "--no-legend"],
            capture_output=True, text=True).stdout))):
        ts = subprocess.run(["systemctl", "--user", "show", u,
                             "-p", "ActiveEnterTimestamp", "--value"],
                            capture_output=True, text=True).stdout.strip()
        if not ts or ts == "n/a":
            continue
        rc_u, out_u = sh(["journalctl", "--user", "-u", u, "--since", ts, "--no-pager"])
        if rc_u == 0:
            bad += [ln for ln in out_u.splitlines() if "Failed to load plugin" in ln]
    add("VERSION-LOAD", "plugin-load-errors", "PASS" if not bad else "FAIL",
        f"{len(bad)} 'Failed to load plugin' since service start"
        + (f" e.g. {bad[0][-160:]}" if bad else " (clean boot)"))
    today = [ln for ln in out.splitlines() if "Failed to load plugin" in ln]
    add("VERSION-LOAD", "plugin-load-errors-today", "INFO",
        f"{len(today)} such lines since midnight (historical, incl. pre-restart)")
    rc, out = sh(["hermes", "plugins", "list"])
    add("VERSION-LOAD", "plugins-list", "PASS" if "hermes-lcm" in out and "error" not in out.lower()[-200:] else "INFO",
        "hermes-lcm visible" if "hermes-lcm" in out else out[-160:])

def s_cluster():
    rows, err = _sql("select (select setting from pg_settings where name='server_version_num'),"
                     "(select setting from pg_settings where name='max_connections'),"
                     "(select count(*) from pg_stat_activity), pg_is_in_recovery()")
    if rows is None:
        add("CLUSTER", "connect", "FAIL", f"DSN unreachable: {err}")
        return
    v, maxc, conns, rec = rows[0]
    add("CLUSTER", "connect", "PASS", f"PG {v}, {conns} sessions, recovery={rec}")
    add("CLUSTER", "max_connections", "PASS" if int(maxc) >= 80 else "INFO",
        f"{maxc} (fleet policy 80)" + ("  <-- below policy" if int(maxc) < 80 else ""))
    rows2, _ = _sql("select coalesce(state,'?'), count(*) from pg_stat_activity group by 1")
    add("CLUSTER", "activity", "PASS", ", ".join(f"{a}:{b}" for a, b in sorted(rows2 or [])))

def s_schemas():
    rows, err = _sql("select nspname from pg_namespace where nspname like 'lcm\\_%' "
                     "and nspname not like 'lcm\\_test\\_%' and nspname not like 'lcm\\_lcm\\_%' order by 1")
    schemas = sorted(r[0] for r in (rows or []))
    phantoms = _phantoms()
    if phantoms:
        add("SCHEMAS", "phantom", "WARN", f"double-prefixed schemas (ensure_schemas bug): {phantoms}")
    profs = sorted(p.name for p in (HERMES_HOME / "profiles").glob("*/")) if (HERMES_HOME / "profiles").is_dir() else []
    # profiles.glob("*/") matches ".deleted/" itself -> would demand an
    # "lcm_.deleted" schema that must never exist (deletion graveyard schema
    # is lcm__deleted, created ad hoc by the delete flow, not per-profile).
    expect = {"lcm_default"} | {f"lcm_{p}" for p in profs if p != ".deleted"}
    missing = sorted(expect - set(schemas))
    residue = sorted(s for s in schemas if re.search(r"(probe|tmp)\d*$", s) or s.endswith("_legacy"))
    add("SCHEMAS", "inventory", "PASS" if not missing else "FAIL",
        f"{len(schemas)} schemas; profiles={len(profs)} missing={missing or '-'}")
    if residue:
        add("SCHEMAS", "residue", "INFO", f"probe/legacy schemas: {residue}")
    return schemas

def _phantoms():
    rows, _ = _sql("select nspname from pg_namespace where nspname like 'lcm\\_lcm\\_%' order by 1")
    return [r[0] for r in rows or []]

def s_structure(schemas):
    rows, _ = _sql(
        "select n.nspname, count(distinct t.tgname) filter (where t.tgname like 'lcm_assertion%'),"
        " count(*) filter (where pg_get_triggerdef(t.oid) like '%NEW.\"content\"%')"
        " from pg_trigger t join pg_class r on r.oid=t.tgrelid join pg_namespace n on n.oid=r.relnamespace"
        f" where n.nspname like 'lcm\\_%' and n.nspname not like 'lcm\\_test\\_%%' and not t.tgisinternal group by 1")
    for s, guards, dead in sorted(rows or []):
        status = "PASS" if int(guards) == 6 and int(dead) == 0 else ("INFO" if s.startswith("lcm_test_") else "FAIL")
        add("STRUCTURE", f"guards:{s}", status,
            f"guards={guards}/6 dead_content_triggers={dead}" + (" (test schema)" if s.startswith("lcm_test_") else ""))
    rows, _ = _sql("select n.nspname, c.relname, bool_or(a.attname='rowid') from pg_class c"
                    " join pg_namespace n on n.oid=c.relnamespace left join pg_attribute a on a.attrelid=c.oid"
                    " where n.nspname like 'lcm\\_%' and n.nspname not like 'lcm\\_test\\_%' and n.nspname not like 'lcm\\_lcm\\_%'"
        " and c.relname in ('lcm_embedding_vectors','lcm_chunk_vectors')"
                    " group by 1,2")
    for s, t, has in sorted(rows or []):
        add("STRUCTURE", f"rowid:{s}.{t}", "PASS" if has else "FAIL", "rowid shim column present" if has else "MISSING rowid col (schema_parity migration not applied)")
    rows, _ = _sql("select n.nspname, count(*) from pg_trigger t join pg_class r on r.oid=t.tgrelid"
                   " join pg_namespace n on n.oid=r.relnamespace where n.nspname like 'lcm\\_%'"
                   " and n.nspname not like 'lcm\\_test\\_%' and n.nspname not like 'lcm\\_lcm\\_%'"
                   " and tgname like '%\\_tsv' and not tgisinternal group by 1")
    for s, n in sorted(rows or []):
        add("STRUCTURE", f"tsv-triggers:{s}", "PASS" if int(n) >= 2 else "FAIL", f"{n} tsv triggers")

def s_verifier(schemas):
    rows, _ = _sql("select n.nspname, conname from pg_constraint c join pg_namespace n on n.oid=c.connamespace"
                   " where n.nspname like 'lcm\\_%' and n.nspname not like 'lcm\\_test\\_%'"
                   " and n.nspname not like 'lcm\\_lcm\\_%' and conrelid is not null"
                   " and conname in ('lcm_rollups_period_kind_check','lcm_rollups_status_check')")
    have: dict[str, set] = {}
    for s, c in rows or []:
        have.setdefault(s, set()).add(c)
    for s in schemas:
        ok = len(have.get(s, set())) == 2
        add("VERIFIER", f"rollup-checks:{s}", "PASS" if ok else "FAIL",
            "both CHECK constraints present in pg_constraint" if ok else f"present={sorted(have.get(s,set()))}")
        # Constraints present yet the code path still complains => VERIFIER-BUG.
        # Count since service start, not since midnight: before the native
        # pg_catalog gate these complaints were emitted on every load, so a
        # since-midnight count reports a fixed bug as if it were still firing.
        rc, out = sh(["journalctl", "--user", "--since", "today", "--no-pager"])
        today_total = len([ln for ln in out.splitlines() if "temporal rollup schema incomplete" in ln])
        rc_s, out_s = sh(["journalctl", "--user", "--since",
                          subprocess.run(["systemctl", "--user", "show", "hermes-gateway.service",
                                          "-p", "ActiveEnterTimestamp", "--value"],
                                         capture_output=True, text=True).stdout.strip(),
                          "--no-pager"])
        complaints = [ln for ln in (out_s if rc_s == 0 else "").splitlines()
                      if "temporal rollup schema incomplete" in ln]
        if ok and complaints:
            add("VERIFIER", f"verifier-bug:{s}", "VERIFIER-BUG",
                f"DB correct but the code path still complains ({len(complaints)}x since "
                f"service start, {today_total}x today) — DDL-synthesis/shape compare bug")
            break

def s_ingest(schemas):
    for s in schemas:
        rows, _ = _sql(f"select coalesce(max(ingested_at)::text,'-'), coalesce(max(timestamp)::text,'-') from {s}.messages")
        ing = (rows[0][0] if rows else "-")
        try:
            ts = datetime.datetime.fromisoformat(ing)
            age_h = (NOW - ts).total_seconds() / 3600
            st = "PASS" if age_h < 48 else "WARN"
            add("INGEST", f"last-message:{s}", st, f"latest ingested_at {age_h:.1f}h ago")
        except Exception:
            add("INGEST", f"last-message:{s}", "INFO", f"raw={ing[:40]}")

def _plugin_roots():
    """Canonical plugin locations. Per fleet convention the lcm-pg plugin lives
    in the ACTIVE hermes profile's plugins dir: ~/.hermes/profiles/<p>/plugins/.
    We enumerate: root plugins dir, every profile's plugins dir, and the
    root forks dir (dev hosts). Deduped by realpath; all are valid load paths
    (agent resolves plugins per-profile via get_hermes_home)."""
    roots, seen = [], set()
    cands = []
    for rh in (HOME / ".hermes", HERMES_HOME):
        cands.append(rh / "plugins" / "lcm-pg")
        if (rh / "profiles").is_dir():
            cands += sorted((rh / "profiles").glob("*/plugins/lcm-pg"))
        cands.append(rh / "forks" / "lcm-pg")
    cands.insert(0, PLUGIN_DIR)
    for c in cands:
        try:
            r = c.resolve()
        except Exception:
            continue
        if (r / "__init__.py").exists() and r not in seen:
            seen.add(r)
            roots.append(r)
    return roots

def s_recall(schemas):
    import importlib, importlib.util
    roots = _plugin_roots()
    if not roots:
        add("RECALL", "import", "FAIL", "no lcm-pg source dir found under plugin/fork locations")
        return
    src = str(roots[0])
    pkg = importlib.util.module_from_spec(importlib.util.spec_from_file_location("lcmpkg", src + "/__init__.py"))
    pkg.__path__ = [src]; sys.modules["lcmpkg"] = pkg
    spec2 = importlib.util.spec_from_file_location("lcmpkg", src + "/__init__.py")
    if spec2 and spec2.loader:
        spec2.loader.exec_module(pkg)
    add("RECALL", "source-dir", "INFO", src)
    os.environ.setdefault("LCM_BACKEND", "postgres")
    try:
        store = importlib.import_module("lcmpkg.store")
    except Exception as e:
        add("RECALL", "import", "FAIL", f"{type(e).__name__}: {e}")
        return
    for s in schemas:
        os.environ["LCM_PG_SCHEMA"] = s
        try:
            ms = store.MessageStore(f"{s}/lcm.db")
            r = ms.search("the", limit=2)
            ok = isinstance(r, list) and all(isinstance(x.get("search_rank", 0.0), float) for x in r)
            add("RECALL", f"fts:{s}", "PASS" if ok else "INFO", f"rows={len(r) if isinstance(r,list) else '-'}")
            ms.close()
        except Exception as e:
            add("RECALL", f"fts:{s}", "FAIL", f"{type(e).__name__}: {str(e)[:160]}")
        rows, _ = _sql(f"select count(*) from {s}.lcm_chunk_vectors")
        if rows and int(rows[0][0]) > 0:
            rows2, _ = _sql(f"select count(*) from {s}.lcm_chunk_binary")
            add("RECALL", f"chunk-binary:{s}", "PASS" if rows2 and int(rows2[0][0]) > 0 else "WARN",
                f"chunk_vectors={rows[0][0]} binary={rows2[0][0] if rows2 else '-'}")
        rows3, _ = _sql(f"select count(*) from {s}.summary_nodes n where n.depth=0 and not exists"
                        f" (select 1 from {s}.lcm_embedding_meta m where m.embedded_id=cast(n.node_id as text)"
                        f" and m.embedded_kind='summary')")
        if rows3 is not None:
            add("RECALL", f"emb-pending:{s}", "PASS" if int(rows3[0][0]) == 0 else "WARN", f"pending={rows3[0][0]}")

def s_timers():
    rc, out = sh(["systemctl", "--user", "list-timers", "--all", "--no-pager"])
    row = [ln for ln in out.splitlines() if "embed" in ln]
    add("TIMERS", "embed-timer", "PASS" if row else "INFO", row[0][:140] if row else "no embed timer listed")
    rc, out = sh(["journalctl", "--user", "-u", "hermes-lcm-embed-backfill.service", "--since", "-24h", "--no-pager"])
    fails = [ln for ln in out.splitlines() if re.search(r"remaining:\s*[1-9]", ln)]
    add("TIMERS", "backfill-drain", "PASS" if not fails else "WARN", f"nonzero-remaining lines: {len(fails)}")
    rc, out = sh(["journalctl", "--user", "-u", "hermes-lcm-embed-backfill.service", "--since", "-24h", "--no-pager"])
    st = [ln for ln in out.splitlines() if "remaining" in ln][-3:] if out else []
    add("TIMERS", "last-backfill", "INFO", " ; ".join(x[-90:] for x in st))

def s_params(schemas):
    env_f = HERMES_HOME / ".env"
    keys = {}
    if env_f.exists():
        keys = dict(l.split("=", 1) for l in env_f.read_text().splitlines() if l.startswith("LCM_") and "=" in l)
    add("PARAMS", "env", "PASS" if keys.get("LCM_BACKEND") == "postgres" else "FAIL",
        f"{len(keys)} LCM_* keys in .env; LCM_BACKEND={keys.get('LCM_BACKEND','MISSING')}")
    # Data-boundary mandate: LCM_DATABASE_PATH must be pinned (inside ~/lcm-pg)
    # in .env AND live in the running daemons' environ, so a default-path
    # fallback can never resolve to HERMES_HOME/lcm.db outside the boundary.
    pinned = str(HOME / "lcm-pg" / "data" / "lcm.db")
    dbp = keys.get("LCM_DATABASE_PATH", "MISSING")
    add("PARAMS", "boundary-pin:env", "PASS" if dbp == pinned else "FAIL",
        f"LCM_DATABASE_PATH={dbp} (expected {pinned}; pin via lcm_update.sh)")
    for f in sorted((HERMES_HOME / "profiles").glob("*/config.yaml")) if (HERMES_HOME / "profiles").is_dir() else []:
        p = f.parent.name
        try:
            import yaml
            d = yaml.safe_load(f.read_text()) or {}
            comp = (d.get("compression") or {})
            aux = (d.get("auxiliary") or {}).get("compression") or {}
            md = (d.get("model") or {}).get("default")
            am = aux.get("model")
            thr = comp.get("threshold")
            ok = am == md
            add("PARAMS", f"profile:{p}", "PASS" if ok else "WARN",
                f"model.default={md} auxiliary.compression.model={am} threshold={thr}"
                + ("" if ok else "  <-- summary model must equal profile default"))
        except Exception as e:
            add("PARAMS", f"profile:{p}", "INFO", f"parse: {type(e).__name__}")
    for u in ("hermes-gateway", "hermes-webui"):
        pid = subprocess.run(["systemctl", "--user", "show", u, "-p", "MainPID", "--value"],
                             capture_output=True, text=True).stdout.strip()
        if not pid or pid == "0":
            continue
        try:
            env = dict(l.split("=", 1) for l in Path(f"/proc/{pid}/environ").read_text().split("\0") if "=" in l)
            add("PARAMS", f"daemon-env:{u}", "PASS" if env.get("LCM_BACKEND") == "postgres" else "FAIL",
                f"LCM_BACKEND={env.get('LCM_BACKEND','-')}")
            add("PARAMS", f"daemon-boundary:{u}", "PASS" if env.get("LCM_DATABASE_PATH") == pinned else "FAIL",
                f"LCM_DATABASE_PATH={env.get('LCM_DATABASE_PATH','-')}")
        except Exception as e:
            add("PARAMS", f"daemon-env:{u}", "INFO", str(e)[:120])
    rc, out = sh(["journalctl", "--user", "--since", "today", "--no-pager"])
    for kind, pat in (("syntax-where", r'syntax error at or near "WHERE"'),
                      ("syntax-not", r'syntax error at or near "NOT"'),
                      ("savepoint-err", r"SAVEPOINT can only be used in transaction blocks"),
                      ("aborted", r"current transaction is aborted")):
        n = len(re.findall(pat, out))
        add("PARAMS", f"journal:{kind}", "PASS" if n == 0 else "WARN", f"{n}x today")

def s_residue():
    rc, out = sh(["journalctl", "--user", "--since", "-24h", "--no-pager"])
    n = out.count("relation \"lcm_")
    add("RESIDUE", "missing-relation-noise", "PASS" if n < 10 else "WARN", f"{n}x 'relation lcm_* does not exist' 24h")
    rc, out = sh(["journalctl", "--user", "--since", "-24h", "--no-pager"])
    n2 = len(re.findall(r"could not be safely copied", out))
    add("RESIDUE", "pool-contention", "PASS" if n2 == 0 else "WARN", f"{n2}x 'could not be safely copied' 24h")
    db = HERMES_HOME / "lcm.db"
    if db.exists():
        import time
        age_d = (time.time() - db.stat().st_mtime) / 86400
        add("RESIDUE", "legacy-lcm-db", "PASS" if age_d > 7 else "WARN",
            f"exists, mtime {age_d:.0f}d old (resurrection = recent mtime)")
    rows, _ = _sql("select nspname from pg_namespace where nspname ~ 'lcm_(probe|tmp)[^_]*'")
    for (s,) in rows or []:
        add("RESIDUE", f"probe:{s}", "INFO", "leftover probe schema (remove via migration on dev host)")

def main():
    s_version()
    s_cluster()
    schemas = s_schemas()
    s_structure(schemas)
    s_verifier(schemas)
    s_ingest(schemas)
    s_recall(schemas)
    s_timers()
    s_params(schemas)
    s_residue()
    fails = [r for r in ROWS if r["status"] == "FAIL"]
    warns = [r for r in ROWS if r["status"] in ("WARN", "VERIFIER-BUG")]
    host = os.uname().nodename
    head = (f"# lcm-pg health report — {host}\n\n"
            f"- generated {NOW:%Y-%m-%d %H:%M:%S %Z} by scripts/lcm_check_all.py\n"
            f"- plugin dir: {PLUGIN_DIR}"
            f" @ {(subprocess.run(['git','-C',str(PLUGIN_DIR),'rev-parse','--short','HEAD'],capture_output=True,text=True).stdout.strip() or 'unknown')}\n"
            f"- hermes home: {HERMES_HOME}  schemas: {len(schemas)}\n\n"
            f"**OVERALL: {'FAIL' if fails else ('WARN' if warns else 'PASS')}**"
            f" — {len([r for r in ROWS if r['status']=='PASS'])} pass / {len(warns)} warn / {len(fails)} fail\n")
    body = ""
    for sec in dict.fromkeys(r["section"] for r in ROWS):
        rs = [r for r in ROWS if r["section"] == sec]
        body += f"\n## {sec}\n\n| status | check | detail |\n|---|---|---|\n"
        for r in sorted(rs, key=lambda x: (x["status"] != "FAIL", x["status"] != "WARN", x["name"])):
            icon = {"PASS": "✅", "FAIL": "❌", "VERIFIER-BUG": "🐞"}.get(r["status"], "⚠️")
            body += f"| {icon} {r['status']} | {r['name']} | {r['detail']} |\n"
    REPORT.write_text(head + body)
    print(f"report: {REPORT}")
    print(f"OVERALL: {'FAIL' if fails else ('WARN' if warns else 'PASS')} ({len(fails)} fail / {len(warns)} warn)")
    for r in fails:
        print(f"  FAIL {r['section']}/{r['name']}: {r['detail'][:200]}")
    return 1 if fails else 0

if __name__ == "__main__":
    # os._exit, not sys.exit: under Python 3.14 the interpreter's finalization
    # can raise PythonFinalizationError ("cannot join thread at interpreter
    # shutdown") on the subprocess bookkeeping this script does after main()
    # returns. Everything relevant is already flushed above (report written,
    # summary printed); letting finalization fail would turn a clean exit code
    # into a traceback after the verdict line.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(main())
