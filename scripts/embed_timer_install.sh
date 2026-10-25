#!/usr/bin/env bash
# Install the auto-embed backfill systemd timer for lcm-pg (idempotent).
#
# embed_auto_backfill.py enumerates profiles itself (~/.hermes +
# ~/.hermes/profiles/*) and pins HERMES_HOME per schema, so the unit needs
# only the lcm-pg env. Runs apply-mode so schema drift self-heals (new
# profiles' corpora get embedded with zero user action).
#
# This complements install_lcm_pg.py, which runs a ONE-OFF backfill at
# install time; without the timer, drift since install is never embedded.
set -euo pipefail
B="${LCM_PG_HOME:-$HOME/lcm-pg}"
FORK="${HOME}/.hermes/forks/lcm-pg"
[ -x "$B/venv/bin/python" ] || { echo "missing $B/venv (run install_lcm_pg.py first)" >&2; exit 1; }
[ -f "$FORK/scripts/embed_auto_backfill.py" ] || { echo "missing fork embed_auto_backfill.py" >&2; exit 1; }
DSN="postgresql:///lcm?host=$B/run&port=54329&user=lcm01"
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/hermes-lcm-embed-backfill.service <<UNIT
[Unit]
Description=LCM auto summary-embedding backfill (lcm-pg)
After=lcm-pg.service
Wants=lcm-pg.service

[Service]
Type=oneshot
Environment=LCM_BACKEND=postgres
Environment=LCM_PG_DSN=$DSN
Environment=LCM_PG_POOL_MIN=1
Environment=LCM_PG_POOL_MAX=2
Environment=LCM_FASTEMBED_CACHE_DIR=$B/fastembed
Environment=LCM_VENV_PY=$B/venv/bin/python
ExecStart=$B/venv/bin/python $FORK/scripts/ensure_profile_bootstrap.py
ExecStart=$B/venv/bin/python $FORK/scripts/embed_warmup_all.py
ExecStart=$B/venv/bin/python $FORK/scripts/embed_auto_backfill.py --limit 400
TimeoutStartSec=3600
Nice=10
UNIT
cat > ~/.config/systemd/user/hermes-lcm-embed-backfill.timer <<UNIT
[Unit]
Description=Hourly LCM auto-embedding backfill (lcm-pg)

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now hermes-lcm-embed-backfill.timer
loginctl enable-linger "$USER" 2>/dev/null || true
echo "embed timer installed:"
systemctl --user list-timers hermes-lcm-embed-backfill.timer --no-legend | awk '{print "  next:", $1, $2, $3, $4, $5, $6}'
