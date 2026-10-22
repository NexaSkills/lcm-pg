#!/usr/bin/env bash
# install_lcm_plugin.sh — turn a host's lcm-pg plugin dirs into git checkouts
# (from origin or a local repo) at a pinned ref, so the plugin location IS the
# deployed code. One-time per host migration from symlink/dir layout; afterwards
# lcm_update.sh does all updates. Host-agnostic: $HOME-based paths only.
#
# Usage: install_lcm_plugin.sh [--ref <ref>] [--local <repo-or-bundle>] [--dry-run]
set -uo pipefail
HOME_DIR="${HOME}"
HERMES_ROOT="${HERMES_ROOT:-$HOME_DIR/.hermes}"
REF=""; LOCAL_REPO=""; DRY=""
while [[ $# -gt 0 ]]; do case "$1" in
  --ref) REF="$2"; shift 2;; --local) LOCAL_REPO="$2"; shift 2;; --dry-run) DRY=1; shift;;
  *) echo "unknown arg $1" >&2; exit 2;;
esac; done
log() { printf '[install-plugin] %s\n' "$*"; }

SRC="$LOCAL_REPO"
[[ -z "$SRC" ]] && SRC="https://github.com/NexaSkills/lcm-pg.git"
# dev fork stays a valid git source: prefer it when present (same repo), else origin
for d in "$HERMES_ROOT/forks/lcm-pg"; do
  [[ -d "$d/.git" ]] && { SRC="$d"; log "git source: $d"; break; }
done

targets=()
[[ -d "$HERMES_ROOT/plugins" ]] && targets+=("$HERMES_ROOT/plugins/lcm-pg")
if [[ -d "$HERMES_ROOT/profiles" ]]; then
  for p in "$HERMES_ROOT"/profiles/*/; do
    [[ -d "$p/plugins" ]] && targets+=("$p/plugins/lcm-pg")
  done
fi
[[ ${#targets[@]} -eq 0 ]] && { log "WARN: no plugins dirs found under $HERMES_ROOT"; }

# fetch once into the dev fork if it exists, else into root plugins clone
fetch_into() {
  # fetch a local source repo's heads into $1 as remote 'src', and publish
  # src/main so callers can resolve it by a stable name.
  local d="$1"
  [[ -d "$d/.git" ]] || { git init -q "$d" 2>/dev/null || return 1; git -C "$d" remote add src "$SRC" 2>/dev/null || git -C "$d" remote set-url src "$SRC"; }
  git -C "$d" fetch -q src 'refs/heads/*:refs/remotes/src/*' 2>/dev/null || git -C "$d" fetch -q "$SRC" 2>/dev/null || return 1
  # if the fetched repo names its head something other than main (upstream
  # naming), expose the fetched FETCH_HEAD/main as src/main for stable resolve
  if ! git -C "$d" rev-parse --verify -q src/main >/dev/null; then
    local alt; alt=$(git -C "$d" rev-parse --verify -q src/HEAD 2>/dev/null || git -C "$d" rev-parse --verify -q FETCH_HEAD 2>/dev/null || true)
    [[ -n "$alt" ]] && git -C "$d" update-ref refs/remotes/src/main "$alt" 2>/dev/null
  fi
  return 0
}
FETCH_DIR=""
for cand in "$HERMES_ROOT/forks/lcm-pg" "${targets[0]:-/dev/null}"; do
  [[ "$cand" == /dev/null ]] && continue
  if [[ -z "$DRY" ]]; then fetch_into "$cand" && { FETCH_DIR="$cand"; break; }; else FETCH_DIR="$cand"; break; fi
done
[[ -z "$FETCH_DIR" ]] && { log "FAIL: no fetch source reachable ($SRC)"; exit 1; }
REFFULL="$REF"; [[ -z "$REFFULL" ]] && REFFULL=$(git -C "$FETCH_DIR" rev-parse src/main 2>/dev/null || git -C "$FETCH_DIR" rev-parse FETCH_HEAD 2>/dev/null || echo "")
[[ -z "$REFFULL" ]] && { log "FAIL: cannot resolve ref from $FETCH_DIR"; exit 1; }
log "resolved ref: $REFFULL ($(git -C "$FETCH_DIR" log -1 --format='%s' "$REFFULL" 2>/dev/null | head -c 60))"

# root plugins/lcm-pg becomes the real clone; profile plugin dirs become
# symlinks to it (agent resolves profile HERMES_HOME/plugins; a symlink keeps
# them in sync automatically). Fork dir on dev hosts stays the authoring copy.
ROOT_TARGET="${targets[0]}"
if [[ "$ROOT_TARGET" != *"/profiles/"* && -n "$ROOT_TARGET" ]]; then
  if [[ -n "$DRY" ]]; then
    log "DRY: would replace $ROOT_TARGET with git clone of $REFFULL"
  else
    if [[ -L "$ROOT_TARGET" ]]; then rm "$ROOT_TARGET"; fi
    if [[ ! -d "$ROOT_TARGET/.git" ]]; then
      [[ -e "$ROOT_TARGET" ]] && { mv "$ROOT_TARGET" "${ROOT_TARGET}.pre-git.$(date +%Y%m%d%H%M%S)"; log "moved old $(basename "$ROOT_TARGET") dir aside"; }
      git init -q "$ROOT_TARGET" && git -C "$ROOT_TARGET" remote add src "$SRC" 2>/dev/null
      git -C "$ROOT_TARGET" fetch -q "$FETCH_DIR" "src/main" 2>/dev/null || git -C "$ROOT_TARGET" fetch -q "$FETCH_DIR" 2>/dev/null
      git -C "$ROOT_TARGET" checkout -qB main "$REFFULL" 2>/dev/null || { git -C "$ROOT_TARGET" fetch -q "$FETCH_DIR" '+refs/heads/*:refs/remotes/f/*' && git -C "$ROOT_TARGET" checkout -qB main f/main; }
      log "cloned plugin into $ROOT_TARGET @ $(git -C "$ROOT_TARGET" rev-parse --short HEAD)"
    else
      git -C "$ROOT_TARGET" fetch -q "$FETCH_DIR" 2>/dev/null
      git -C "$ROOT_TARGET" checkout -qB main "$REFFULL" 2>/dev/null || true
      log "root plugin clone updated @ $(git -C "$ROOT_TARGET" rev-parse --short HEAD)"
    fi
  fi
fi
for t in "${targets[@]}"; do
  [[ "$t" == "$ROOT_TARGET" ]] && continue
  if [[ -n "$DRY" ]]; then log "DRY: would symlink $t -> $ROOT_TARGET"; continue; fi
  if [[ -L "$t" || ! -e "$t" ]]; then
    rm -f "$t"; ln -s "$ROOT_TARGET" "$t"; log "symlinked $t -> $ROOT_TARGET"
  else
    mv "$t" "$t.pre-git.$(date +%Y%m%d%H%M%S)"; ln -s "$ROOT_TARGET" "$t"; log "replaced dir with symlink $t -> $ROOT_TARGET (backup kept)"
  fi
done
log "done"
