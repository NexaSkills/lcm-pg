#!/usr/bin/env bash
# add-skill-external-dirs.sh — idempotently give every Hermes profile (root +
# profiles under $HERMES_ROOT) visibility of the skills shipped inside the
# lcm-pg plugin tree ($HERMES_ROOT/plugins/lcm-pg/skills).
#
# Why (verified against hermes-agent source): the skills catalog scans the
# profile's own skills dir + skills.external_dirs from config.yaml ONLY. A
# plain plugin dir's skills/ subtree is NOT scanned (only portable
# plugin.json packages auto-register), so a plugin-carried skill is invisible
# unless external_dirs names its tree. Entries anchor RELATIVE paths at the
# profile's HERMES_HOME — differ per profile — so this writes ONE absolute
# path. Appends the block only when 'external_dirs' is absent; backs up each
# config first; never rewrites an existing skills: section.
set -uo pipefail
ROOT=${HERMES_ROOT:-"$HOME/.hermes"}
skills_tree="$ROOT/plugins/lcm-pg/skills"
[[ -d "$skills_tree" ]] || { echo "skip: $skills_tree not present"; exit 0; }
for h in "$ROOT" "$ROOT/profiles"/*/; do
  [[ -d "$h" ]] || continue
  cfg="$h/config.yaml"
  [[ -f "$cfg" ]] || continue
  grep -q 'external_dirs' "$cfg" && continue
  cp -a "$cfg" "$cfg.pre-skilldirs.$(date +%Y%m%d%H%M%S)"
  printf '\nskills:\n  external_dirs:\n    - "%s"\n' "$skills_tree" >> "$cfg"
  echo "added: skills.external_dirs=$skills_tree -> $cfg"
done
