#!/usr/bin/env bash
# Convenience shim: run the repo's new-profile onboarding script from home.
# All logic lives in ~/.hermes/plugins/lcm-pg/scripts/lcm_new_profile_setup.py.
set -euo pipefail
for d in "$HOME/.hermes/plugins/lcm-pg" "$HOME/.hermes/forks/lcm-pg"; do
  [ -f "$d/scripts/lcm_new_profile_setup.py" ] && exec python3 "$d/scripts/lcm_new_profile_setup.py" "$@"
done
echo "lcm_new_profile_setup: no lcm-pg code root found" >&2
exit 1
