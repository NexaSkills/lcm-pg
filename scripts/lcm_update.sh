#!/usr/bin/env bash
# lcm_update.sh — deploy/update the lcm-pg solution on THIS host (host-agnostic).
#
# The SAME script runs on beez and every fleet host. It never edits code and
# never hand-mutates databases: it fetches a pinned git ref into the PLUGIN
# location, applies + stamps DB migrations via the repo's migration runner,
# syncs the repo-declared config files (params), restarts hermes surfaces,
# then runs lcm_check_all.sh and leaves a health report.
#
# Usage:
#   lcm_update.sh [--ref <git-ref>]            # fetch that ref (default: origin/HEAD)
#   lcm_update.sh --local <path>               # skip git; use a local git repo dir as source
#   lcm_update.sh --no-restart | --dry-run | --list | --migrations-only
#
# Host config (all via env, with sane $HOME-based defaults; no host literals):
#   LCM_PG_DSN, HERMES_HOME (default ~/.hermes), PLUGIN_DIR (default
#   $HERMES_HOME/plugins/lcm-pg), LCM_PARAM_FILES (space-separated config
#   files this deploy syncs from the repo's configs/ dir; default empty =
#   migration+code deploy only).
set -uo pipefail

HOME_DIR="${HOME}"
HERMES_HOME="${HERMES_HOME:-$HOME_DIR/.hermes}"
PLUGIN_DIR="${PLUGIN_DIR:-$HERMES_HOME/plugins/lcm-pg}"
PARAM_FILES="${LCM_PARAM_FILES:-}"
REF="origin/HEAD"; LOCAL_REPO=""; DO_RESTART=1; DRY=""; MIGRATIONS_ONLY=""
CMD=deploy

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2;;
    --local) LOCAL_REPO="$2"; shift 2;;
    --no-restart) DO_RESTART=0; shift;;
    --dry-run) DRY=1; shift;;
    --migrations-only) MIGRATIONS_ONLY=1; DO_RESTART=0; shift;;
    --list) CMD=list; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

log() { printf '[update] %s\n' "$*"; }
die() { printf '[update] FAIL: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null || die "git not found"
command -v psql >/dev/null || command -v "$HOME/lcm-pg/pg/bin/psql" >/dev/null || die "psql not found"
export LCM_PG_DSN="${LCM_PG_DSN:-postgresql://lcm01@lcm-pg/lcm?host=$HOME/lcm-pg/run&port=54329}"

if [[ "$CMD" == list ]]; then
  exec python3 "$PLUGIN_DIR/scripts/lcm_deploy_migrations.py" --list
fi

# ---- 1. resolve target dir (plugin location) ---------------------------------
mkdir -p "$(dirname "$PLUGIN_DIR")"
if [[ ! -d "$PLUGIN_DIR/.git" ]]; then
  [[ -e "$PLUGIN_DIR" && ! -d "$PLUGIN_DIR" ]] && die "$PLUGIN_DIR exists and is not a dir (retire any symlink first)"
  if [[ -d "$PLUGIN_DIR" && -z "$(ls -A "$PLUGIN_DIR" 2>/dev/null)" ]]; then
    : # empty dir -> clone target
  elif [[ -d "$PLUGIN_DIR" ]]; then
    # materialize: first-time replace of a symlink/dir with a git clone
    if [[ -L "$PLUGIN_DIR" ]]; then die "$PLUGIN_DIR is a symlink; run: rm $PLUGIN_DIR  (dev hosts keep their work in the authoring clone)"; fi
    die "$PLUGIN_DIR not empty and not a git repo; move it away first"
  fi
  [[ -n "$DRY" ]] && log "DRY: git clone -> $PLUGIN_DIR"
  [[ -z "$DRY" ]] && { mkdir -p "$PLUGIN_DIR"; git init -q "$PLUGIN_DIR"; git -C "$PLUGIN_DIR" remote add origin "$([[ -n "$LOCAL_REPO" ]] && echo "$LOCAL_REPO" || echo "https://github.com/NexaSkills/lcm-pg.git")"; }
fi

SRC="$LOCAL_REPO"
if [[ -z "$SRC" ]]; then
  SRC="$(git -C "$PLUGIN_DIR" remote get-url origin 2>/dev/null || echo '')"
  [[ -z "$SRC" ]] && SRC="https://github.com/NexaSkills/lcm-pg.git"
fi
# hosts without GitHub: their origin remote is pre-configured (staging repo/bundle) — this script honours whatever origin says. Host-agnostic.

fetch_ok=0
# fetch the EXPLICIT ref: a plain `git fetch origin` leaves FETCH_HEAD at
# origin/HEAD (repo default branch), which silently ignores --ref and can
# deploy the wrong code while reporting success. The ref-name fetch must
# SUCCEED on its own — never fall back to the generic fetch.
REFSPEC="$REF"
[[ "$REFSPEC" == refs/* ]] || REFSPEC=refs/heads/${REF#origin/}
git -C "$PLUGIN_DIR" fetch -q origin "$REFSPEC" 2>/dev/null && fetch_ok=1
if [[ $fetch_ok -eq 0 ]] && git -C "$PLUGIN_DIR" rev-parse -q --verify "$REF^{commit}" >/dev/null 2>&1; then
  # ref already present locally (bare branch name / local object): FETCH_HEAD
  # format wants '<sha>\t<why>' — a bare sha line parses as a bad ref name.
  printf '%s\t\tlocal ref\n' "$(git -C "$PLUGIN_DIR" rev-parse "$REF^{commit}")" \
    > "$(git -C "$PLUGIN_DIR" rev-parse --git-dir)/FETCH_HEAD" && fetch_ok=1
fi
if [[ $fetch_ok -eq 0 && "$REF" == "origin/HEAD" ]]; then
  git -C "$PLUGIN_DIR" fetch -q origin 2>/dev/null && fetch_ok=1
fi
# fallback: --local may point at a git repo dir or a .bundle file
if [[ $fetch_ok -eq 0 && -n "$LOCAL_REPO" && -e "$LOCAL_REPO" ]]; then
  if git -C "$PLUGIN_DIR" fetch -q "$LOCAL_REPO" 'refs/heads/*:refs/remotes/origin/*' 2>/dev/null \
     || git -C "$PLUGIN_DIR" fetch -q "$LOCAL_REPO" 'refs/heads/main:refs/remotes/origin/main' 2>/dev/null; then
    fetch_ok=1
  fi
fi
[[ $fetch_ok -eq 1 ]] || die "git fetch failed (origin${LOCAL_REPO:+ / $LOCAL_REPO})"

git -C "$PLUGIN_DIR" checkout -qB deploy-track "FETCH_HEAD" 2>/dev/null \
  || git -C "$PLUGIN_DIR" reset -q --hard FETCH_HEAD || die "checkout of FETCH_HEAD failed"
HEAD_SHA=$(git -C "$PLUGIN_DIR" rev-parse --short HEAD)
export LCM_DEPLOY_REF="$HEAD_SHA"
log "plugin at $PLUGIN_DIR now @ $HEAD_SHA ($(git -C "$PLUGIN_DIR" log -1 --format='%s' | head -c 70))"

# ---- 2. DB migrations (idempotent + stamped; abort on failure) --------------
if [[ -n "$DRY" ]]; then
  log "DRY: migrations skipped"
else
  PY="$HOME/lcm-pg/venv/bin/python"; [[ -x "$PY" ]] || PY=python3
  log "migrations: $("$PY" "$PLUGIN_DIR/scripts/lcm_deploy_migrations.py" --ref "$HEAD_SHA" | tail -20)"
  "$PY" "$PLUGIN_DIR/scripts/lcm_deploy_migrations.py" --ref "$HEAD_SHA" >/dev/null \
    || die "DB migrations failed (see output above; fix on the dev host, rerun)"
fi
[[ -n "$MIGRATIONS_ONLY" ]] && { log "migrations-only done"; exit 0; }

# ---- 3. config sync (params from the repo's configs/ dir; empty = skip) -----
if [[ -n "$PARAM_FILES" && -z "$DRY" ]]; then
  for f in $PARAM_FILES; do
    src="$PLUGIN_DIR/configs/$f"
    [[ -f "$src" ]] || { log "WARN: no configs/$f in repo; skip"; continue; }
    dst="$HOME_DIR/$f"
    [[ -f "$dst" ]] && cp -a "$dst" "$dst.pre-deploy.$(date +%Y%m%d%H%M%S)"
    install -D -m 600 "$src" "$dst" && log "params: synced $f (backup kept)"
  done
fi

# ---- 3b. data-boundary pin (repo scripts only; NEVER hand-edit hosts) --------
# Mandate: all LCM data lives under ~/lcm-pg only. engine._resolve_db_path
# falls back to HERMES_HOME/lcm.db when LCM_DATABASE_PATH is unset, so an
# exported sqlite-only env could silently write outside the boundary. Pin the
# fallback path (PG intercepts it anyway) into every hermes .env — main and
# per-profile — idempotently.
BOUNDARY_DB="$HOME_DIR/lcm-pg/data/lcm.db"
mkdir -p "$HOME_DIR/lcm-pg/data"

# ---- 3b2. cluster max_connections policy (fleet = 100) ----------------------
# Pool arithmetic: every surface process runs its own psycopg pool per schema
# (LCM_PG_POOL_MAX default 8). 3 surfaces x 8 is already 24; long compaction
# checkouts make the server cap relevant. The cluster must therefore sit at 100
# (Jan fleet policy). Idempotent edit of the FIRST assignment (never append:
# the instance postgres.conf's LAST assignment wins, per the duplicate-
# assignment pitfall). A restart is required for it to take effect; a CLUSTER
# restart never SIGTERMs hermes surfaces (they reconnect), unlike restarting
# the surface hosting this session.
CLUSTER_CONF="$HOME_DIR/lcm-pg/instance/postgresql.conf"
PGCTL="$HOME_DIR/lcm-pg/pg/bin/pg_ctl"
if [[ -z "$DRY" && -f "$CLUSTER_CONF" ]]; then
  "$PGCTL" -D "$HOME_DIR/lcm-pg/instance" start >/dev/null 2>&1 || true
  cur_max=$(PGHOST="$HOME_DIR/lcm-pg/run" PGPORT=54329 PGUSER=lcm01 psql -d lcm \
            -Atc "show max_connections" 2>/dev/null) || cur_max=""
  if [[ "$cur_max" == "100" ]]; then
    log "cluster-policy: max_connections=100 (OK)"
  elif [[ -n "$cur_max" ]]; then
    log "cluster-policy: max_connections=$cur_max -> 100"
    cp -a "$CLUSTER_CONF" "$CLUSTER_CONF.pre-policy.$(date +%Y%m%d%H%M%S)"
    if grep -qE '^[[:space:]]*max_connections[[:space:]]*=' "$CLUSTER_CONF"; then
      sed -i '0,/^[[:space:]]*max_connections[[:space:]]*=/ s/^[[:space:]]*max_connections[[:space:]]*=.*/max_connections = 100/' "$CLUSTER_CONF"
    else
      printf '\nmax_connections = 100\n' >> "$CLUSTER_CONF"
    fi
    if [[ -x "$PGCTL" ]]; then
      "$PGCTL" -D "$HOME_DIR/lcm-pg/instance" restart -W 30 >/dev/null 2>&1 \
        && log "cluster-policy: restarted; now $(PGHOST="$HOME_DIR/lcm-pg/run" PGPORT=54329 PGUSER=lcm01 psql -d lcm -Atc 'show max_connections' 2>/dev/null)" \
        || log "WARN: cluster-policy: pg_ctl restart failed (edit applied; verify + restart manually)"
    else
      log "WARN: cluster-policy: pg_ctl not found; conf edited but cluster NOT restarted"
    fi
  else
    log "WARN: cluster-policy: cluster not queryable (offline?); max_connections NOT verified"
  fi
fi

env_files=("$HOME_DIR/.hermes/.env")
[[ -d "$HOME_DIR/.hermes/profiles" ]] && env_files+=("$HOME_DIR"/.hermes/profiles/*/.env)

# ---- 3c. plugin overlay for EVERY profile (incl. profiles created after the
# install). Discovery only finds hermes-lcm through a per-profile symlink;
# a profile dir without one silently loads NO lcm plugin (tools missing from
# its catalog) and falls back to a zero-byte per-profile lcm.db. Do it even
# when the profile has no .env yet.
if [[ -d "$HOME_DIR/.hermes/profiles" && ( -e "$HOME_DIR/.hermes/plugins/lcm-pg" || -L "$HOME_DIR/.hermes/plugins/lcm-pg" ) ]]; then
  for pd in "$HOME_DIR"/.hermes/profiles/*/; do
    prof="$pd/plugins"
    lp="$prof/hermes-lcm"
    mkdir -p "$prof"
    if [[ -L "$lp" ]]; then
      tgt=$(readlink "$lp")
      [[ "$tgt" == "$HOME_DIR/.hermes/plugins/lcm-pg" ]] || {
        log "overlay: relink $lp (was -> $tgt)"
        rm -f "$lp"; ln -s "$HOME_DIR/.hermes/plugins/lcm-pg" "$lp"; }
    elif [[ -e "$lp" ]]; then
      log "overlay: $lp is a real dir/copy (version drift); moving aside"
      mv "$lp" "$lp.retired.$(date +%Y%m%d%H%M%S)"
      ln -s "$HOME_DIR/.hermes/plugins/lcm-pg" "$lp"
    else
      ln -s "$HOME_DIR/.hermes/plugins/lcm-pg" "$lp"
      log "overlay: created $lp -> root plugin"
    fi
    # a routed profile's .env must carry the backend+DSN the daemons get from
    # the root .env, else a profile-local session can write a per-profile
    # lcm.db instead of the shared cluster (pluginless fallback). The other
    # keys are operator-surface params the fork reads from os.environ ONLY:
    # LCM_ENABLE_SLASH_COMMAND gates the /lcm command registration (default
    # OFF), LCM_EMBEDDINGS_ENABLED + LCM_EMBEDDING_* gate embedding warmup /
    # semantic recall (default OFF). systemd surfaces inherit the root .env
    # (20-active-profile-env.conf), but CLI/launcher-run profile sessions
    # resolve the PROFILE .env — a profile missing these silently loses /lcm
    # and reports 'embeddings are disabled' (fleet incident 2026-10-25).
    src_env="$HOME_DIR/.hermes/.env"
    penv="$pd.env"
    if [[ -f "$src_env" ]]; then
      for k in LCM_BACKEND LCM_PG_DSN LCM_DATABASE_PATH \
               LCM_PG_POOL_MIN LCM_PG_POOL_MAX \
               LCM_ENABLE_SLASH_COMMAND LCM_EMBEDDINGS_ENABLED \
               LCM_EMBEDDING_PROVIDER LCM_EMBEDDING_MODEL LCM_EMBEDDING_QUERY_TIMEOUT_S \
               LCM_FASTEMBED_CACHE_DIR; do
        val=$(grep -m1 "^${k}=" "$src_env" | cut -d= -f2-) || true
        [[ -n "$val" ]] || continue
        if [[ ! -f "$penv" ]] || ! grep -q "^${k}=" "$penv"; then
          printf '%s=%s\n' "$k" "$val" >> "$penv"
          log "overlay-env: $k -> $penv"
        fi
      done
    fi
  done
fi
for ef in "${env_files[@]}"; do
  [[ -f "$ef" ]] || continue
  # Pool pin (fleet policy): the fork defaults LCM_PG_POOL_MAX=8 and a
  # compaction-heavy profile saturates its own 8-slot pool (PoolTimeout
  # 'couldn't get a connection after 10.00 sec' killed pip's compression on
  # trade, 2026-10-25). 16 slots over a max_connections=100 cluster (enforced
  # in 3b2) fits 3 surfaces + headroom. Append-only; never rewrites a custom
  # per-host value (check_all reports a non-16 pin).
  if ! grep -q '^LCM_PG_POOL_MAX=' "$ef"; then
    cp -a "$ef" "$ef.pre-pool.$(date +%Y%m%d%H%M%S)"
    printf 'LCM_PG_POOL_MIN=1\nLCM_PG_POOL_MAX=16\n' >> "$ef"
    log "pool-pin: LCM_PG_POOL_MIN=1 LCM_PG_POOL_MAX=16 -> $ef"
  fi
  if grep -q '^LCM_DATABASE_PATH=' "$ef"; then
    cur=$(grep -m1 '^LCM_DATABASE_PATH=' "$ef" | cut -d= -f2-)
    [[ "$cur" == "$BOUNDARY_DB" ]] || log "WARN: $ef pins LCM_DATABASE_PATH=$cur (expected $BOUNDARY_DB)"
  else
    cp -a "$ef" "$ef.pre-boundary.$(date +%Y%m%d%H%M%S)"
    printf 'LCM_DATABASE_PATH=%s\n' "$BOUNDARY_DB" >> "$ef"
    log "boundary-pin: LCM_DATABASE_PATH=$BOUNDARY_DB -> $ef"
  fi
done

# ---- 3d. plugin migration hermes-lcm -> lcm-pg (assumed on every host) ------
# The deploy ASSUMES the host is migrating FROM the upstream hermes-lcm plugin
# TO lcm-pg: code migration happened in step 1 (materialize) and overlays in
# 3c; this converges plugins.enabled in root + every profile config.yaml
# (enabled += lcm-pg, - hermes-lcm once the lcm-pg plugin dir is live;
# disabled -= lcm-pg). Idempotent; backs up each touched file.
if [[ -z "$DRY" ]]; then
  _py="$HOME/lcm-pg/venv/bin/python"; [[ -x "$_py" ]] || _py=python3
  log "plugin-migration: $($_py "$PLUGIN_DIR/scripts/migrate_hermes_lcm_config.py" 2>&1 | tail -4)"
fi

# ---- 4. restart hermes surfaces (discovered; profile units skipped) --------
if [[ $DO_RESTART -eq 1 && -z "$DRY" ]]; then
  units=$(systemctl --user list-units --type=service --state=loaded --no-legend 2>/dev/null \
          | grep -oE 'hermes-(gateway|webui|dashboard)\.service' | sort -u)
  [[ -z "$units" ]] && { log "WARN: no hermes gateway/webui/dashboard units (headless dev host?)"; units=""; }
  if [[ -n "$units" ]]; then
    log "restart: $units"
    systemctl --user restart $units || log "WARN: restart reported failure"
    sleep 4
  fi
fi

# ---- 5. health check -> report -----------------------------------------------
python3 "$PLUGIN_DIR/scripts/lcm_check_all.py" || true   # report is the artifact; exit code of update = 0
REPORT="$HOME_DIR/lcm-pg-check-$(hostname).md"
log "health report: $REPORT"
grep -qE '\*\*OVERALL: FAIL' "$REPORT" 2>/dev/null && { log "RESULT: FAIL (see report)"; exit 1; }
log "RESULT: OK  (report OVERALL not FAIL @ $HEAD_SHA)"
