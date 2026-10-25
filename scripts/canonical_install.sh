#!/usr/bin/env bash
# Canonicalize the self-contained lcm-pg install (idempotent).
#
# 1) retire the per-profile lcm-pg symlinks: one plugin means ONE discovery
#    path (the root symlink). The profile overlay used to trip Hermes'
#    duplicate-plugin guard on hosts that also carry a profile named 'default'.
# 2) neutralize any hermes-lcm entry the platform pip-installed: its tools.py
#    registers 30 tools (20 colliding with lcm-pg). A broken editable install
#    left in place crashes tool import ("file ... does not start with ...").
#    Neutralizing = replace the package dir with an empty stub (recoverable,
#    recorded in ~/.hermes/backups/), NOT an uninstall (pip metadata for a
#    broken editable install can itself fail).
# 3) report what the gateway will load (root lcm-pg only).
set -uo pipefail
HOME_DIR="${HOME:-$HOME}"
BAK="$HOME_DIR/.hermes/backups/lcm-canonical-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BAK"

echo "== 1. profile overlay retirement =="
removed=0
for p in "$HOME_DIR"/.hermes/profiles/*/plugins/lcm-pg; do
  [ -L "$p" ] || continue
  rm "$p" && removed=$((removed+1))
done
p="$HOME_DIR/.hermes/profiles/default/plugins/lcm-pg"
if [ -L "$p" ]; then rm "$p"; removed=$((removed+1)); fi
echo "  removed $removed overlay link(s); root symlink intact: $(readlink "$HOME_DIR/.hermes/plugins/lcm-pg" 2>/dev/null || echo MISSING)"

echo "== 2. hermes-lcm neutralization =="
SP=$("$HOME_DIR"/.hermes/venv/bin/python -c "import site;print(site.getusersitepackages())" 2>/dev/null || echo "$HOME_DIR/.local/lib/python3.12/site-packages")
if [ -d "$SP/hermes_lcm" ]; then
  if grep -l "does not start with" "$SP"/hermes_lcm*.egg-link "$SP"/__editable__*hermes_lcm* 2>/dev/null | grep -q . \
     || ! "$HOME_DIR"/.hermes/venv/bin/python -c "import hermes_lcm" 2>/dev/null; then
    mv "$SP/hermes_lcm" "$BAK/hermes_lcm-stubbed" && mkdir -p "$SP/hermes_lcm"
    cat > "$SP/hermes_lcm/__init__.py" <<'STUB'
# Neutralized by scripts/canonical_install.sh: the authoritative LCM engine is
# the ~/.hermes/plugins/lcm-pg plugin. This stub exists solely so the broken
# editable-install finder cannot crash tool import. Original preserved in
# ~/.hermes/backups/lcm-canonical-*/hermes_lcm-stubbed.
STUB
    echo "  hermes_lcm stubbed (was broken editable install); original -> $BAK/hermes_lcm-stubbed"
  else
    echo "  hermes_lcm imports cleanly; left alone"
  fi
else
  echo "  no hermes_lcm package dir present"
fi

echo "== 3. verification (plugin discovery) =="
"$HOME_DIR"/.hermes/venv/bin/python - <<'PY' 2>&1 | tail -4
import sys
sys.path.insert(0, __import__("pathlib").Path.home()/".hermes")
from hermes import plugins as _  # noqa
PY
echo "  plugins dir: $(ls "$HOME_DIR/.hermes/plugins" 2>/dev/null | paste -sd' ')"
echo "  profiles with plugins: $(for d in "$HOME_DIR"/.hermes/profiles/*/plugins; do [ -e "$d" ] && basename $(dirname $d); done | paste -sd' ')"
systemctl --user restart $(systemctl --user list-units --all --no-legend | grep -oE 'hermes-(gateway|webui|dashboard)[a-z0-9-]*\.service' | sort -u) 2>/dev/null
sleep 8
echo "  daemons: $(systemctl --user is-active $(systemctl --user list-units --all --no-legend | grep -oE 'hermes-[a-z0-9-]*\.service' | sort -u) 2>/dev/null | paste -sd'/')"
echo "CANONICAL_DONE"
