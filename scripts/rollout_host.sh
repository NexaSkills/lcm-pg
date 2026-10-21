#!/usr/bin/env bash
# Fleet rollout of the self-contained lcm-pg bundle: beez (source host) -> targets.
# Proven on warren(.35)/clone(.32)/template(.33) 2026-10-19. Idempotent; safe to re-run.
#
# Usage (run FROM the source host):
#   scripts/rollout_host.sh xfer   <ip>   # push golden bundle + fork (land-verifies)
#   scripts/rollout_host.sh install <ip>  # build pgvector, venv, cluster, migrate, systemd
#   scripts/rollout_host.sh verify  <ip>  # parity + hygiene checks (read-only)
#
# Pitfalls encoded here (each cost a failed run before):
#  - rsync SILENTLY sends nothing when the remote parent dir is missing -> mkdir first.
#  - malformed -e quoting breaks rsync with "IPC code (code 14)" -> use RSYNC_RSH env.
#  - always land-verify (fork git HEAD on target), never trust rsync exit alone.
#  - uv venv creation can hang minutes downloading cpython-3.12 — that's not stuck.
set -uo pipefail
MODE=${1:?xfer|install|verify}; HOST=${2:?target ip}
SRC=$HOME/lcm-pg
FORK=$HOME/.hermes/forks/lcm-pg
SSH=(-o BatchMode=yes -o ConnectTimeout=15 -o ControlMaster=auto
     -o "ControlPath=$HOME/.ssh/cm/%r@%h:%p" -o ControlPersist=600)
B=/home/mybuddy/lcm-pg
DSN="postgresql:///lcm?host=$B/run&port=54329&user=lcm01"

rsh() { ssh "${SSH[@]}" mybuddy@$HOST 'bash -s'; }

do_xfer() {
  rsh <<'T'
mkdir -p ~/lcm-pg ~/.hermes/forks
command -v rsync >/dev/null || { echo "RSYNC_MISSING on target"; exit 1; }
T
  export RSYNC_RSH="ssh -o BatchMode=yes -o ControlPath=$HOME/.ssh/cm/%r@%h:%p"
  rsync -a --info=stats1 "$SRC/" "mybuddy@$HOST:$B/" \
    --exclude instance --exclude run --exclude venv --exclude logfile --exclude __pycache__
  rsync -a "$SRC/src/pgvector" "mybuddy@$HOST:$B/src/"
  rsync -a "$SRC/fastembed/" "mybuddy@$HOST:$B/fastembed/"
  rsync -a "$FORK/" "mybuddy@$HOST:~/.hermes/forks/lcm-pg/"
  echo "== land-verify =="
  rsh <<T
test -f $B/pg/bin/postgres && echo bin-ok || echo bin-MISSING
test -f $B/install_lcm_pg.py && echo installer-ok || echo installer-MISSING
test -d $B/src/pgvector && echo src-ok || echo src-MISSING
(cd ~/.hermes/forks/lcm-pg && git log --oneline -1) || echo fork-MISSING
cd ~/.hermes/forks/lcm-pg && [ "\$(git rev-parse HEAD)" = "$(git -C $FORK rev-parse HEAD)" ] && echo HEAD_MATCH || echo HEAD_MISMATCH
T
}

do_install() {
  rsh <<EOSCRIPT
set -uo pipefail
echo "== 1. pgvector (idempotent patch+build) =="
bash $B/scripts/build_pgvector.sh 2>&1 | tail -2 || exit 1
echo "== 2. venv (py3.12; uv download may take minutes) =="
[ -x $B/venv/bin/python ] || uv venv --python 3.12 $B/venv -q
VIRTUAL_ENV=$B/venv VIRTUAL_ENV_DISABLE_WARNING=1 uv pip install -q --python $B/venv/bin/python \
  "psycopg[binary]==3.3.6" "psycopg_pool==3.3.3" "pgvector==0.5.1" "fastembed==0.8.1"
# NOTE: psycopg_pool is NOT pulled in by psycopg[binary] — it must be explicit
# (fork postgres.py imports it at module level; absence => PoolTimeout-at-init).
$B/venv/bin/python -c "import psycopg, psycopg_pool, pgvector, fastembed; print('VENV_OK', psycopg.__version__)"
echo "== 3. installer (cluster + plugin symlink + .env DSNs; idempotent) =="
python3 $B/install_lcm_pg.py 2>&1 | tail -8
echo "== 4. environment.d + systemd layers =="
mkdir -p ~/.config/environment.d ~/.config/systemd/user
cat > ~/.config/environment.d/lcm-pg.conf <<ENVD
PYTHONPATH=$B/venv/lib/python3.12/site-packages
LCM_BACKEND=postgres
LCM_PG_DSN=$DSN
LCM_FASTEMBED_CACHE_DIR=$B/fastembed
ENVD
for u in \$(systemctl --user list-units --all --no-legend | grep -oE 'hermes[a-z0-9-]*\.service' | sort -u); do
  d=~/.config/systemd/user/\$u.d; mkdir -p \$d
  printf '[Service]\n# lcm-pg self-contained venv (psycopg/pgvector/fastembed)\nEnvironment=PYTHONPATH=%s/lib/python3.12/site-packages\n' "$B/venv" > \$d/30-lcm-pg.conf
done
cat > ~/.config/systemd/user/lcm-pg.service <<UNIT
[Unit]
Description=LCM private PostgreSQL cluster (self-contained, socket 54329)
After=default.target

[Service]
Type=simple
ExecStart=$B/pg/bin/postgres -D $B/instance
Restart=on-failure
RestartSec=2
TimeoutStopSec=30

[Install]
WantedBy=default.target
UNIT
$B/pg/bin/pg_ctl -D $B/instance -m fast -w stop >/dev/null 2>&1
loginctl enable-linger \$USER 2>/dev/null
systemctl --user daemon-reload
systemctl --user enable --now lcm-pg.service
sleep 2
echo "== 5. migrate schemas (idempotent; lossy utf-8 salvage built in) =="
BK=\$HOME/.hermes/backups/lcm-pg-pre-migrate-\$(date +%Y%m%d); mkdir -p \$BK
mig() { local src=\$1; [ -f "\$src" ] || return 0
  cp -a "\$src" "\$BK/" 2>/dev/null
  LCM_PG_DSN=$DSN $B/venv/bin/python ~/.hermes/forks/lcm-pg/scripts/migrate_sqlite_to_pg.py "\$src" 2>&1 | tail -3
}
mig \$HOME/.hermes/lcm.db
for p in \$HOME/.hermes/profiles/*/lcm.db; do mig "\$p"; done
echo "== 6. legacy sqlite workaround units off (archived) =="
LKB=\$HOME/.hermes/backups/lcm-pg-bundle-\$(date +%Y%m%d-%H%M%S); mkdir -p \$LKB
for u in lcm-ghost-fd-check lcm-local-patch-watchdog hermes-lcm-integrity-check mmap-patch-guard; do
  systemctl --user disable --now \$u.timer \$u.service 2>/dev/null
  mv ~/.config/systemd/user/\$u.service ~/.config/systemd/user/\$u.timer \$LKB/ 2>/dev/null
done
mv ~/.config/systemd/user/lcm-ghost-fd-check.sh ~/.hermes/scripts/lcm-local-patch-watchdog.sh ~/.hermes/scripts/mmap-patch-guard.sh \$LKB/ 2>/dev/null
systemctl --user daemon-reload; systemctl --user reset-failed
echo "== 7. restart daemons =="
systemctl --user restart \$(systemctl --user list-units --all --no-legend | grep -oE 'hermes-(gateway|webui|dashboard)[a-z0-9-]*\.service' | sort -u) 2>/dev/null
sleep 6
systemctl --user is-active lcm-pg.service
echo "== 7b. auto-embed summaries (hybrid recall live; non-fatal) =="
LCM_PG_DSN=$DSN LCM_PG_POOL_MIN=1 LCM_PG_POOL_MAX=2 $B/venv/bin/python \
  ~/.hermes/forks/lcm-pg/scripts/embed_auto_backfill.py --limit 500 2>&1 | tail -5 \
  || echo "  embed backfill non-fatal (run manually if recall looks FTS-only)"
echo "INSTALL_DONE"
EOSCRIPT
}

do_verify() {
  rsh <<EOSCRIPT
B=$B
PG() { PGHOST=\$B/run PGPORT=54329 PGUSER=lcm01 \$B/pg/bin/psql -d lcm -Atc "\$1"; }
echo "daemons: \$(systemctl --user is-active \$(systemctl --user list-units --all --no-legend | grep -oE 'hermes-[a-z0-9-]*\.service' | sort -u) | paste -sd' ')"
echo "failed units: $(systemctl list-units --failed --no-legend | wc -l) sys + \$(systemctl --user list-units --failed --no-legend | wc -l) user"
echo "lcm timers: \$(systemctl --user list-timers --no-legend | grep -vi boot-audit | grep -ci lcm)"
echo "schemas: \$(PG "select string_agg(nspname,' ') from pg_namespace where nspname like 'lcm_%'")"
echo "-- PG messages+FTS per schema --"
for s in \$(PG "select nspname from pg_namespace where nspname like 'lcm_%'"); do
  PG "select '\$s', count(*), count(*) filter (where content_tsv is not null) from \$s.messages"
done
echo "-- sqlite source messages --"
echo "root \$HOME/.hermes/lcm.db: \$(sqlite3 -readonly \$HOME/.hermes/lcm.db 'select count(*) from messages' 2>/dev/null)"
for db in \$HOME/.hermes/profiles/*/lcm.db; do
  [ -f "\$db" ] && echo "\$db: \$(sqlite3 -readonly "\$db" 'select count(*) from messages' 2>/dev/null)"
done
echo "(compare row-for-row with the schema list above; profile dir name -> lcm_<name>, root .hermes -> lcm_default)"
echo "lcm.db fds held by daemons: \$(for p in \$(systemctl --user show -p MainPID --value \$(systemctl --user list-units --all --no-legend | grep -oE 'hermes-[a-z0-9-]*\.service' | sort -u)); do ls -l /proc/\$p/fd 2>/dev/null; done | grep -c lcm.db)"
echo "pg backends: \$(PG "select count(*) from pg_stat_activity where usename='lcm01'")"
EOSCRIPT
}

case $MODE in
  xfer) do_xfer ;;
  install) do_install ;;
  verify) do_verify ;;
  *) echo "mode must be xfer|install|verify"; exit 2 ;;
esac
