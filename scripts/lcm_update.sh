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
# deploy the wrong code while reporting success.
git -C "$PLUGIN_DIR" fetch -q origin "$REF" 2>/dev/null && fetch_ok=1
[[ $fetch_ok -eq 0 && "$REF" == origin/* ]] \
  && git -C "$PLUGIN_DIR" fetch -q origin "${REF#origin/}" 2>/dev/null && fetch_ok=1
if [[ $fetch_ok -eq 0 ]] && git -C "$PLUGIN_DIR" rev-parse -q --verify "$REF^{commit}" >/dev/null 2>&1; then
  # already-present ref (fully-crafted remote ref or local object): FETCH_HEAD from a full fetch
  git -C "$PLUGIN_DIR" fetch -q origin 2>/dev/null
  git -C "$PLUGIN_DIR" rev-parse "$REF^{commit}" > "$(git -C "$PLUGIN_DIR" rev-parse --git-dir)/FETCH_HEAD" && fetch_ok=1
fi
if git -C "$PLUGIN_DIR" fetch -q origin 2>/dev/null; then fetch_ok=1; fi
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
