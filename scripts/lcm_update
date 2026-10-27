#!/usr/bin/env bash
# lcm_update.sh — deploy/update the lcm-pg solution on THIS host (host-agnostic).
#
# The SAME script runs on coordination-host and every fleet host. It never edits code and
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
# HOST ROOT hermes home, always. An agent-launched deploy exports
# HERMES_HOME=<a profile home> (that is how agent surfaces run); honouring it
# here aimed the deploy at the launching profile instead of the host
# (beez 2026-10-27: skill tree pinned to profiles/mike/...; template: deploy
# ran from an agent shell and updated the wrong plugin clone). Surfaces
# resolve their own homes; the DEPLOY's job is the host-wide trees.
HERMES_ROOT="$HOME_DIR/.hermes"
[[ "${HERMES_HOME:-}" == "$HERMES_ROOT" || -z "${HERMES_HOME:-}" ]] || \
  log "HERMES_HOME=$HERMES_HOME (profile scope) -> deploy targets host root $HERMES_ROOT"
HERMES_HOME="$HERMES_ROOT"
# The CODE clone lives in the host root tree; per-profile plugins/lcm-pg are
# symlinks onto it (overlay convergence in 3c). Never aim the code update at
# a profile-scoped PLUGIN_DIR inherited from the environment.
unset PLUGIN_DIR
PLUGIN_DIR="${PLUGIN_DIR:-$HERMES_HOME/plugins/lcm-pg}"
PARAM_FILES="${LCM_PARAM_FILES:-}"
REF="origin/main"; LOCAL_REPO=""; DO_RESTART=1; DRY=""; MIGRATIONS_ONLY=""
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

# Canonical way to write ANY config.yaml value (Jan directive 2026-10-27):
# never edit config.yaml with sed/awk/editors — only each profile's .env may
# be file-edited. Set a dotted key in the root config or a named profile's
# config via hermes' own writer (creates files/keys as needed). --force so
# plugin-namespace keys the running Hermes doesn't enumerate (skills.*,
# lcm.*) still land. Failures WARN loudly: a missed convergence shows in
# step 5's report rather than vanishing.
cfg_set() {  # cfg_set <profile|root> <dotted.key> <value>
  local scope="$1" key="$2" val="$3" out rc
  local bin="${HERMES_BIN:-}"
  [[ -z "$bin" ]] && bin=$(command -v hermes || true)
  [[ -z "$bin" && -x "$HOME/.local/bin/hermes" ]] && bin="$HOME/.local/bin/hermes"
  [[ -z "$bin" ]] && { log "WARN: hermes binary not found; cannot set $key ($scope) via CLI"; return 1; }
  if [[ "$scope" == root ]]; then
    out=$("$bin" config set --force "$key" "$val" 2>&1); rc=$?
  else
    out=$("$bin" -p "$scope" config set --force "$key" "$val" 2>&1); rc=$?
  fi
  if [[ $rc -ne 0 ]]; then
    log "WARN: hermes config set $key ($scope) failed: $(echo "$out" | tail -1)"
    return 1
  fi
  return 0
}

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
# REMOTE: --ref <remote>/<branch> fetches from THAT remote, not hardcoded
# origin (template incident 2026-10-27: clone origin was a stale local
# push-repo and GitHub was remote `nexa`; the old code mangled nexa/main
# into refs/heads/nexa/main on origin and died on a misleading 'no .git dir').
FETCH_REMOTE=origin
[[ "$REF" == */* ]] && FETCH_REMOTE="${REF%%/*}"
case "$REF" in
  refs/*) FETCH_BRANCH="$REF" ;;          # fully-qualified ref: pass through
  */HEAD) FETCH_BRANCH=HEAD ;;            # symbolic HEAD: fetch resolves it
  *)      FETCH_BRANCH="${REF##*/}" ;;
esac
git -C "$PLUGIN_DIR" fetch -q "$FETCH_REMOTE" "$FETCH_BRANCH" 2>/dev/null && fetch_ok=1
if [[ $fetch_ok -eq 0 ]] && git -C "$PLUGIN_DIR" rev-parse -q --verify "$REF^{commit}" >/dev/null 2>&1; then
  # ref already present locally (bare branch name / local object): FETCH_HEAD
  # format wants '<sha>\t<why>' — a bare sha line parses as a bad ref name.
  gd="$(git -C "$PLUGIN_DIR" rev-parse --git-dir 2>/dev/null)" && [[ -d "$gd" ]] || { die "$PLUGIN_DIR has no .git dir (repo was stripped by an external sync?) — reinstall the plugin dir"; }
  printf '%s\t\tlocal ref\n' "$(git -C "$PLUGIN_DIR" rev-parse "$REF^{commit}")" \
    > "$gd/FETCH_HEAD" && fetch_ok=1
fi
if [[ $fetch_ok -eq 0 && "$REF" == *"/HEAD" ]]; then
  git -C "$PLUGIN_DIR" fetch -q "$FETCH_REMOTE" 2>/dev/null && fetch_ok=1
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
  # 2a. INVENTORY HEAL (fleet incident skilltest 2026-10-27): profile dirs
  # created AFTER the last deploy have no schema, and the migration loop
  # below enumerates pg_namespace BEFORE bootstrapping — so such a profile
  # would run forever on un-stamped, parity-incomplete DDL while every
  # deploy says RESULT: OK. heal_profile_schemas.py creates the missing
  # lcm_<profile> schemas (official naming rule), runs the canonical
  # ensure_* bootstrap, then the stamp loop. BEFORE migrations so the whole
  # run sees one converged inventory.
  log "heal: $("$PY" "$PLUGIN_DIR/scripts/heal_profile_schemas.py" 2>&1 | tail -4)"
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

# ---- 3b3. stale idle-backend sweep (connection hygiene) --------------------
# The plugin borrows per statement and returns promptly (verified probe: an
# idle PgConnection holds 0 backends; with-conn scopes release at __exit__).
# Idle-in-pool backends ARE idle-in-server: psycopg_pool's reaper is off
# (no max_idle/max_lifetime), so slots from dead processes linger for hours
# (fleet-host held one 7h52m) and idle-in-transaction holders starve pools.
# A LOCAL backend's pid IS the client's OS process (postmaster forks the
# listener), so /proc/$pid gone = definite orphan. (client_pid exists only
# when client_backends_info is loaded -- not installed here, don't depend on
# it.) Sweep: (a) idle local backends (ANY age) whose holder PROCESS IS GONE
# (dead-holder sockets are pure slots; churn via lifecycle tmp-schemas makes
# these by the minute), (b) idle-in-transaction older than 10 min (held
# slots starve pools; aborted idle-in-tx blocks vacuum too), (c) idle with a
# LIVE holder older than 12h (pool member reaping nothing all day). NEVER
# touches active queryers or young live-holder pool members. Once per deploy
# run via stamp file; delete the stamp to re-force.
SWEEP_STAMP="$HOME_DIR/lcm-pg/.stale-swept"
if [[ -z "$DRY" && ! -e "$SWEEP_STAMP" ]]; then
  "$PGCTL" -D "$HOME_DIR/lcm-pg/instance" start >/dev/null 2>&1 || true
  _sweep=$(PGHOST="$HOME_DIR/lcm-pg/run" PGPORT=54329 PGUSER=lcm01 psql -d lcm -Atc \
    "select pid, coalesce(state,'?'), extract(epoch from now()-state_change)::int, coalesce(backend_type,'') from pg_stat_activity where usename=current_user and pid<>pg_backend_pid() and state is not null" 2>/dev/null \
    | while IFS='|' read -r pid state age btype; do
        why=""
        if [[ "$btype" == "client backend" && ! -d "/proc/$pid" ]]; then
          why="orphan"
        else
          [[ "$state" == "idle in transaction" && "$age" -gt 600 ]] && why="stale-tx"
          [[ "$state" == "idle" && "$age" -gt 43200 ]] && why="stale-idle"
        fi
        [[ -n "$why" ]] && echo "$pid|$state|$age|$btype|$why"
      done)
  if [[ -n "$_sweep" ]]; then
    while IFS='|' read -r pid state age btype why; do
      log "stale-backends: terminate pid=$pid state=$state age=${age}s type=${btype:-?} ($why)"
      PGHOST="$HOME_DIR/lcm-pg/run" PGPORT=54329 PGUSER=lcm01 psql -d lcm -qc \
        "select pg_terminate_backend($pid)" >/dev/null 2>&1 || true
    done <<< "$_sweep"
  else
    log "stale-backends: none"
  fi
  touch "$SWEEP_STAMP"
fi

env_files=("$HOME_DIR/.hermes/.env")
[[ -d "$HOME_DIR/.hermes/profiles" ]] && env_files+=("$HOME_DIR"/.hermes/profiles/*/.env)

# ---- 3c0. full per-profile param convergence (repo-owned script) -----------
# ONE deterministic pass that converges EVERY profile against the root .env's
# COMPLETE LCM_* surface (every key, not a curated subset) and every profile
# config.yaml's context.engine via `hermes config get/set` ONLY. The
# hand-curated key lists elsewhere in this script are minimum guarantees;
# this is the authority (fleet drift incidents: keys someone forgot to add).
if [[ -z "$DRY" && -f "$PLUGIN_DIR/scripts/lcm_params_sync.py" ]]; then
  _ps_py="$HOME/lcm-pg/venv/bin/python"; [[ -x "$_ps_py" ]] || _ps_py=python3
  log "params-sync: $($_ps_py "$PLUGIN_DIR/scripts/lcm_params_sync.py" 2>&1 | tail -3)"
fi

# ---- 3c. plugin overlay for EVERY profile (incl. profiles created after the
# install). Discovery gates on the DIRECTORY name, so the overlay MUST be
# named lcm-pg; a profile dir without one silently loads NO lcm plugin (tools
# missing from its catalog) and falls back to a zero-byte per-profile lcm.db.
# The legacy 'hermes-lcm' name is retired (Jan directive 2026-10-27): it is
# deleted here, never created. Do it even when the profile has no .env yet.
if [[ -d "$HOME_DIR/.hermes/profiles" && ( -e "$HOME_DIR/.hermes/plugins/lcm-pg" || -L "$HOME_DIR/.hermes/plugins/lcm-pg" ) ]]; then
  for pd in "$HOME_DIR"/.hermes/profiles/*/; do
    prof="$pd/plugins"
    lp="$prof/lcm-pg"
    legacy="$prof/hermes-lcm"
    mkdir -p "$prof"
    if [[ -L "$legacy" ]]; then
      rm -f "$legacy"; log "overlay: removed retired $legacy"
    elif [[ -e "$legacy" ]]; then
      mv "$legacy" "$legacy.retired.$(date +%Y%m%d%H%M%S)"; log "overlay: retired $legacy (moved aside)"
    fi
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
    # lcm.db instead of the shared cluster (pluginless fallback). The FULL
    # LCM_* surface is converged once per deploy by scripts/lcm_params_sync.py
    # (step 3c0 below): EVERY LCM_* key in the root .env must equal the same
    # value in every profile .env. The hand-picked key list kept here is the
    # minimum guarantee for older plugin clones without the sync script (a
    # key missing from a list = silent profile drift; fleet incidents
    # 2026-10-25). config.yaml keys are converged via hermes config get/set
    # ONLY — never a file edit (Jan directive 2026-10-27).
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
  # Idle-connection recycling (fleet policy): the fork arms psycopg's reaper
  # with LCM_PG_POOL_MAX_IDLE (default 60s). It closes returned-idle members
  # and recycles anything older than max_lifetime, so a live-but-quiet surface
  # genuinely releases backends instead of parking 15 idle ones for hours.
  if ! grep -q '^LCM_PG_POOL_MAX_IDLE=' "$ef"; then
    echo 'LCM_PG_POOL_MAX_IDLE=60' >> "$ef"
    log "pool-pin: LCM_PG_POOL_MAX_IDLE=60 -> $ef"
  fi
  # Pool pin (fleet policy): the fork defaults LCM_PG_POOL_MAX=8 and a
  # compaction-heavy profile saturates its own 8-slot pool (PoolTimeout
  # 'couldn't get a connection after 10.00 sec' killed pip's compression on
  # fleet-host, 2026-10-25). 16 slots over a max_connections=100 cluster (enforced
  # in 3b2) fits 3 surfaces + headroom. Append-only; never rewrites a custom
  # per-host value (check_all reports a non-16 pin).
  if grep -q '^LCM_PG_POOL_MAX=' "$ef"; then
    # Idempotent enforcement (not create-only): a host whose launcher predated
    # the create path may still hold a wrong value; never deploy below 16.
    cur_max=$(sed -n 's/^LCM_PG_POOL_MAX=//p' "$ef" | tail -1)
    if [ "${cur_max:-0}" -lt 16 ] 2>/dev/null; then
      sed -i "s/^LCM_PG_POOL_MAX=.*/LCM_PG_POOL_MAX=16/" "$ef"
      grep -q '^LCM_PG_POOL_MIN=' "$ef" || echo 'LCM_PG_POOL_MIN=1' >> "$ef"
      log "pool-pin: LCM_PG_POOL_MAX=$cur_max -> 16 -> $ef"
    fi
  else
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

# ---- 3c2. profile config convergence via `hermes config set` ONLY ---------
# Jan directive 2026-10-27: config.yaml is NEVER written by sed/echo/file
# edits anywhere in the pipeline; only each profile's .env may be file-edited
# (steps 3b/3c keep that). This step guarantees every profile dir has a
# config.yaml whose context.engine is lcm (else the profile silently runs
# the built-in LOSSY compressor — the #1 'LCM not working' cause). Read via
# the CLI (no writes unless needed); write via the CLI with --force (the
# dotted path may live under a section this Hermes build doesn't enumerate).
if [[ -z "$DRY" && -d "$HOME_DIR/.hermes/profiles" ]]; then
  _ctxfix=0
  for pd in "$HOME_DIR"/.hermes/profiles/*/; do
    [[ -d "$pd" ]] || continue
    prof=$(basename "$pd")
    _eng=""
    ${HERMES_BIN:-hermes} -p "$prof" config get context.engine >/dev/null 2>&1 && \
      _eng=$(${HERMES_BIN:-hermes} -p "$prof" config get context.engine 2>/dev/null)
    if [[ "$_eng" == "lcm" ]]; then
      continue
    elif [[ -z "$_eng" ]]; then
      # unset (or unreadable config) may be legitimate inheritance from a
      # root that sets engine: lcm
      _rooteng=""
      ${HERMES_BIN:-hermes} config get context.engine >/dev/null 2>&1 && \
        _rooteng=$(${HERMES_BIN:-hermes} config get context.engine 2>/dev/null)
      [[ "$_rooteng" == "lcm" ]] && continue
    fi
    cfg_set "$prof" context.engine lcm && _ctxfix=$((_ctxfix+1))
  done
  [[ "${_ctxfix:-0}" -gt 0 ]] && log "config-sync: set context.engine=lcm via hermes config set in $_ctxfix profile(s)" \
    || log "config-sync: every profile config resolves context.engine=lcm"
fi

# ---- 3c3. managed-scope overlay check (NO /etc writes, ever) ----------------
# If a stray /etc/hermes/config.yaml exists, hermes_cli/managed_scope.py
# applies it OVER every profile and a LIST leaf REPLACES plugins.enabled
# fleet-wide (template 2026-10-27: an overlay listing only
# planning-with-files silently disabled lcm-pg on every surface). Fleet
# policy (Jan 2026-10-27): ALL Hermes config lives under ~/.hermes — /etc is
# root-owned, out of bounds, and must NOT exist on a fleet host. The deploy
# never reads-modifies-or-writes /etc itself; it DETECTS a stray overlay and
# RESCUES the load-bearing keys into root config.yaml BEFORE FAILING, so the
# operator's `sudo rm -rf /etc/hermes` can never strand the host.
# (Hermes hard-blocks `config set` for any key the overlay pins, so writing
# root plugins.enabled is impossible while the overlay exists — copy the
# pinned values down, then fail with the same instruction.)
if [[ -e /etc/hermes/config.yaml ]]; then
  # ONE-TIME rescue: if root lacks a key the overlay pins AND that key is
  # load-bearing for this plugin (plugins.enabled), copy the overlay's value
  # into root. `hermes config set` CANNOT do this (config.py hard-rejects any
  # key the overlay pins), so this is the single sanctioned exception to the
  # config-set-only rule — a direct root config.yaml write, read-only on the
  # overlay, and it loses its trigger the moment the overlay is gone.
  # Net effect equals `hermes plugins enable` run after the overlay is removed.
  # root_has_enabled: does root config.yaml have a plugins: section WITH an
  # enabled: key? (Bare `grep -q enabled:` is wrong — unrelated sections like
  # checkpoints.enabled satisfy it; template incident 2026-10-27.)
  # Trigger is UNION-based: every overlay-pinned plugin must be in root's
  # enabled list, not merely 'does root have an enabled key' — a partial
  # root list (template: ['lcm-pg'] while overlay pinned 3) must still merge.
  if ! python3 - "$HOME_DIR/.hermes/config.yaml" <<'PYEOF' 2>/dev/null
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
m = yaml.safe_load(open("/etc/hermes/config.yaml")) or {}
root = set((d.get("plugins") or {}).get("enabled") or [])
pin = set((m.get("plugins") or {}).get("enabled") or [])
sys.exit(0 if pin and pin <= root else 1)
PYEOF
  then
    _managed_en=$(python3 -c 'import yaml,sys;d=yaml.safe_load(open("/etc/hermes/config.yaml")) or {};e=(d.get("plugins") or {}).get("enabled");print(",".join(e) if isinstance(e,list) else "")' 2>/dev/null) || _managed_en=""
    if [[ -n "$_managed_en" ]]; then
      _need=""
      for _k in lcm-pg hermes-lcm; do [[ "$_managed_en" == *"$_k"* ]] || _need="$_need $_k"; done
      [[ -n "$_need" ]] && _managed_en="$_managed_en$_need"
      python3 - "$HOME_DIR/.hermes/config.yaml" "$_managed_en" <<'PYEOF' 2>/dev/null && \
        log "rescue: copied plugins.enabled [$_managed_en] into root config.yaml (was pinned only in /etc overlay)" || \
        log "WARN: rescue of plugins.enabled failed — after rm /etc/hermes run: hermes plugins enable lcm-pg"
import sys, yaml, os, tempfile
p, keys = sys.argv[1], [k for k in sys.argv[2].split(",") if k]
d = yaml.safe_load(open(p)) or {}
pl = d.setdefault("plugins", {})
cur = set(pl.get("enabled") or [])
pl["enabled"] = sorted(cur | set(keys))
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
with os.fdopen(fd, "w") as f: yaml.safe_dump(d, f, sort_keys=False)
os.replace(tmp, p)
PYEOF
    fi
  fi
  log "FAIL: stray /etc/hermes/config.yaml — fleet policy: Hermes config lives under ~/.hermes ONLY."
  log "      managed_scope applies it OVER every profile (plugins.enabled REPLACED fleet-wide)."
  log "      fix:  sudo rm -rf /etc/hermes   (then rerun this deploy)"
  exit 1
fi

# ---- 3d. plugin migration hermes-lcm -> lcm-pg (assumed on every host) ------
# The deploy ASSUMES the host is migrating FROM the upstream hermes-lcm plugin
# TO lcm-pg: code migration happened in step 1 (materialize) and overlays in
# 3c; this converges plugins.enabled in root + every profile config.yaml
# (enabled += lcm-pg, - hermes-lcm — the retired name must not stay listed
# once the lcm-pg plugin dir is live; disabled -= lcm-pg). Idempotent; backs
# up each touched file.
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

# ---- 3e. skill visibility (plugin-carried skills -> every profile) ---------
# Hermes lists skills from the profile skills dir + skills.external_dirs ONLY
# (agent/skill_utils.get_skill_search_roots; plain plugins' skills/ trees are
# NOT scanned). Point every hermes home at this host's plugin skills tree so
# skills like lcm-pg-new-profile-setup are visible to every profile, incl.
# ones created after this deploy. WRITTEN VIA `hermes config set` ONLY (Jan
# directive 2026-10-27: config.yaml is never file-edited; only .env may be).
# The writer keeps YAML validity, creates missing config.yaml files (a new
# profile dir had none = the skill silently invisible = fleet drift), and
# preserves an existing custom skills: section (read-only probe first).
if [[ -z "$DRY" ]]; then
  # Host ROOT plugins tree, NOT $HERMES_HOME: an agent-spawned deploy exports
  # HERMES_HOME=<some profile home>, which would point every config at one
  # profile's materialised symlink (beez incident 2026-10-27: all profiles
  # got .../profiles/mike/plugins/lcm-pg/skills instead of the root clone).
  _sktree="$HOME_DIR/.hermes/plugins/lcm-pg/skills"
  [[ -d "$_sktree" ]] || _sktree="$HERMES_HOME/plugins/lcm-pg/skills"
  if [[ -d "$_sktree" ]]; then
    _sk=0
    for pd in "$HOME_DIR"/.hermes/profiles/*/; do
      [[ -d "$pd" ]] || continue
      prof=$(basename "$pd")
      _cur=$(${HERMES_BIN:-hermes} -p "$prof" config get skills.external_dirs 2>/dev/null) || _cur=""
      case "$_cur" in *"$_sktree"*) continue;; esac
      cfg_set "$prof" skills.external_dirs "['$_sktree']" && _sk=$((_sk+1))
    done
    # root config: only when root itself lacks the tree
    _rootcur=$(${HERMES_BIN:-hermes} config get skills.external_dirs 2>/dev/null) || _rootcur=""
    case "$_rootcur" in *"$_sktree"*) ;; *) cfg_set root skills.external_dirs "['$_sktree']" && _sk=$((_sk+1));; esac
    [[ "${_sk:-0}" -gt 0 ]] && log "skill-visibility: set external_dirs via hermes config set in $_sk config(s)" \
      || log "skill-visibility: all configs already carry external_dirs"
  else
    log "skill-visibility: no skills tree at $_sktree; skipped"
  fi
fi

# ---- 5. health check -> report -----------------------------------------------
# `stdbuf -oL` so the checker's summary reaches this log even when the deploy
# output is redirected to a file/pipe (os._exit in the checker skips the
# interpreter flush; the summary printed last and silently vanished — the
# single most confusing deploy symptom, fixed 2026-10-27).
stdbuf -oL -eL python3 "$PLUGIN_DIR/scripts/lcm_check_all.py" || true   # report is the artifact; exit code of update = 0
REPORT="$HOME_DIR/lcm-pg-check-$(hostname).md"
log "health report: $REPORT"
grep -qE '\*\*OVERALL: FAIL' "$REPORT" 2>/dev/null && { log "RESULT: FAIL (see report)"; exit 1; }
log "RESULT: OK  (report OVERALL not FAIL @ $HEAD_SHA)"
