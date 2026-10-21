#!/usr/bin/env bash
# Converge the pragma-notnull patch onto a host's RUNTIME plugin dir
# (and the fork copy it keeps, if any). Idempotent; no gateway restarts.
#
# The fix makes _sqlite_ddl_for consult db_bootstrap's canonical sqlite DDL
# for whether a PRIMARY KEY member's column line declared NOT NULL — the
# ONLY source of truth, since PG's attnotnull is identical for declared
# (rollup tables) and implicit (embedding tables) PK members. Without it,
# verify_embedding_schema reports 'malformed table: lcm_embedding_*' and
# embed_auto_backfill fails its after-ensure check.
#
# Usage: bash pragma_notnull_rollout.sh [host...]
#   (default: 192.168.1.32 192.168.1.33 192.168.1.35)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_PY="$SCRIPT_DIR/pragma_notnull_patch.py"
[ -f "$PATCH_PY" ] || { echo "missing $PATCH_PY" >&2; exit 1; }
HOSTS=("$@"); [ ${#HOSTS[@]} -gt 0 ] || HOSTS=(192.168.1.32 192.168.1.33 192.168.1.35)
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10"
rc=0
for h in "${HOSTS[@]}"; do
  echo "== $h"
  # runtime dir first (gateways + timer use it); fork copy second
  for d in "$HOME/lcm-pg/plugins/lcm-pg" "$HOME/.hermes/forks/lcm-pg"; do
    # only patch dirs that actually contain the fork (verify marker)
    has=$($SSH "mybuddy@$h" "test -f $d/scripts/pragma_notnull_patch.py && echo yes || echo no" 2>/dev/null || echo "ssh-fail")
    [ "$has" = "yes" ] || { echo "   $d: skip (no fork marker / no dir)"; continue; }
    $SSH "mybuddy@$h" "bash -s" 2>/dev/null <<REMOTE
cd $d 2>/dev/null || exit 0
timeout 60 \$HOME/lcm-pg/venv/bin/python scripts/pragma_notnull_patch.py 2>&1 | tail -1
# runtime import check (fresh interpreter, real plugin path)
timeout 120 \$HOME/lcm-pg/venv/bin/python - <<'PY'
import importlib.util, os, sys
from pathlib import Path
D = "$d"
os.environ["LCM_BACKEND"] = "postgres"
os.environ.setdefault("LCM_PG_DSN",
    "postgresql:///lcm?host=" + str(Path.home()) + "/lcm-pg/run&port=54329&user=lcm01")
spec = importlib.util.spec_from_file_location("lcmpg", str(Path(D)/"__init__.py"),
                                              submodule_search_locations=[D])
pkg = importlib.util.module_from_spec(spec); sys.modules["lcmpg"] = pkg
spec.loader.exec_module(pkg)
import importlib
pg = importlib.import_module("lcmpg.backends.postgres")
dbb = importlib.import_module("lcmpg.db_bootstrap")
schemas = [r[0] for r in pg.connect(os.environ["LCM_PG_DSN"], "information_schema").execute(
    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'lcm[_\\]_%' ESCAPE chr(92)"
    " AND nspname NOT LIKE '%deleted' AND nspname NOT LIKE '%corrupt%'").fetchall()]
bad = 0
for s in sorted(schemas):
    c = pg.connect(os.environ["LCM_PG_DSN"], s)
    e = dbb.verify_embedding_schema(c); r = dbb.verify_temporal_rollup_schema(c)
    if e or r:
        print(f"   {s}: embed={e or 'OK'} rollup={r or 'OK'}"); bad += 1
print(f"   runtime verify: {len(schemas)} schemas, {bad} dirty")
PY
REMOTE
    [ $? -ne 0 ] && { echo "   $d: REMOTE ERROR"; rc=1; }
  done
done
exit $rc
