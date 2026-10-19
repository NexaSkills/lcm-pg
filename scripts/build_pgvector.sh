#!/usr/bin/env bash
# Patch pgvector for PG18's vacuum_delay_point(bool) signature (uncommitted upstream change).
# PG18 made vacuum_delay_point take an is_analyze arg; pgvector 0.8.x still calls it with 0 args.
set -eu
BASE="${LCM_PG_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
[ -d "$BASE/src/pgvector" ] || BASE="/home/mybuddy/lcm-pg"   # invoked from git fork
cd "$BASE/src/pgvector"
PGC="$BASE/pg/bin/pg_config"
grep -n "vacuum_delay_point(bool" "$BASE/pg/include/postgresql/server/commands/vacuum.h"
if grep -q "vacuum_delay_point(bool" "$BASE/pg/include/postgresql/server/commands/vacuum.h"; then
  # idempotency: drop any previously applied compat macro first — a source tree
  # copied from an already-patched host would otherwise end up with a
  # recursive macro (make: macro takes just 0 args) and the build dies.
  sed -i '/#define vacuum_delay_point() vacuum_delay_point(false)/d' src/hnswvacuum.c src/ivfvacuum.c
  sed -i 's/vacuum_delay_point();/vacuum_delay_point(false);/g' src/hnswvacuum.c src/ivfvacuum.c
  echo "PATCHED vacuum_delay_point call sites"
fi
make PG_CONFIG=$PGC clean >/dev/null
make PG_CONFIG=$PGC >/tmp/pgvmake2.log 2>&1
make install PG_CONFIG=$PGC >/tmp/pgvinstall2.log 2>&1
# install lands in pkglibdir (pg/lib/postgresql/), not pg/lib/ — accept either
PKGLIBDIR=$("$PGC" --pkglibdir)
test -f "$PKGLIBDIR/vector.so" && echo "PGVECTOR_BUILT $PKGLIBDIR/vector.so"
