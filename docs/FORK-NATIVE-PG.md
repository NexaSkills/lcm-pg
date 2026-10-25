# NexaSkills fork — native PostgreSQL backend & fleet operations

> This section documents what the **NexaSkills/lcm-pg** fork adds on top of
> upstream [hermes-lcm](https://github.com/stephenschoettler/hermes-lcm): a
> native PostgreSQL backend (`LCM_BACKEND=postgres`) and the deploy/verification
> pipeline that runs it across the NexaSkills fleet. Everything above this
> section is upstream behaviour and still applies.

## Status — real-world validated

**2026-10-25:** first true end-to-end success on native PG since the sqlite
conversion — a real-world LCM compression ran clean on the `mybuddy-trade`
host for the `pip` profile (Jan's manual test; fleet-wide `check_all` 0 FAIL on
all five hosts at `3a1e041`). The fleet now runs a single deploy branch
(`main`) with every host on the same artifact.

## What the fork adds

- **Native PG backend** (`backends/postgres.py`): the whole LCM store —
  messages, FTS, summary DAG, embeddings/kNN, assertion guards — lives in
  PostgreSQL schemas (`lcm_<profile>`), with the sqlite shim kept only as a
  compatibility view layer. `PgConnection` opens lazily, borrows a pooled
  connection per transaction/savepoint scope, and returns it when the scope
  closes; aborted transactions are reset before reuse so a failed statement
  never poisons a pooled backend.
- **Deploy pipeline** (`scripts/lcm_update.sh`): one script, same path on every
  host — git fetch ff-only → materialise into
  `~/.hermes/plugins/lcm-pg` → stamped per-schema migrations → params sync →
  restart all surfaces → verify. Hosts without GitHub access fetch their own
  `~/lcm-pg-push.git` staging repo (beez fans out to it).
- **Fleet health checks** (`scripts/lcm_check_all.py`): per-host markdown
  report — migrations, guards, params, pools, residue — 0-FAIL contract before
  a deploy counts as done.

## Connection hygiene (the 2026-10-25 hardening)

A real `PoolTimeout: couldn't get a connection after 10.00 sec` on a compaction
led to a full root-cause chain. Findings, all live-verified:

1. **The bottleneck was per-process, not server-wide.** psycopg's pool default
   (`LCM_PG_POOL_MAX=8`) saturated inside one profile's process while the
   server sat at 30/100 backends. Fleet policy pins **16** (3 surfaces × 16 =
   48 < 100) and enforces cluster **`max_connections=100`**.
2. **The idle reaper was never armed.** psycopg_pool grows to N connections
   and keeps them for the process lifetime; its shrink task only closes a
   member after a full interval of *zero* pool activity, so a live-but-quiet
   surface parked 15+ idle backends for hours. Fix: the pool is now created
   with `max_idle` (default 60 s, pinned via `LCM_PG_POOL_MAX_IDLE`) and
   `max_lifetime` (3600 s), so idle members are genuinely released and stale
   ones recycled. Live proof: hour-old idle backends gone fleet-wide after
   deploy.
3. **Orphans from churn.** Dead processes leave zombie backends. The deploy
   now sweeps: idle backends whose holder process is gone (any age),
   `idle in transaction` older than 10 min, and live-holder idles older than
   the sweep threshold.
4. **The pool pin must be enforced, not just created.** beez silently ran on
   the default 8 for days because the pin logic was create-only; it now
   enforces ≥ 16 on every pass.

### Ops commands

```bash
# backends in use / ceiling
PGHOST=$HOME/lcm-pg/run PGPORT=54329 PGUSER=lcm01 psql -d lcm -Atc \
  "select count(*) from pg_stat_activity where backend_type='client backend'"
# oldest idle connection
... -Atc "select max(now()-state_change) from pg_stat_activity where state='idle'"
# pool pins actually effective in a surface's env
tr '\0' '\n' < /proc/$(systemctl --user show hermes-gateway -p MainPID --value)/environ | grep LCM_PG_POOL
# socket-holder attribution (ss -p alone lies; join socket inodes via /proc/<pid>/fd)
ss -xup | grep lcm-pg/run/.s.PGSQL
```

## Fleet deploy workflow (pipeline v2)

Code changes are authored **only on beez** in the fork → `git push origin main`
→ beez fans the same ref out to each host's `~/lcm-pg-push.git` staging repo
→ on each host: `bash ~/lcm_update.sh` (fetch → migrate → params → restart →
verify) → `lcm_check_all.py` must report 0 FAIL.

**Critical gotcha (learned the hard way, 2026-10-25):** the deploy restarts
every hermes surface, including the webui serving your own session. A
foreground run therefore severs the running agent's connection mid-deploy.
Always fire the deploy **detached** on the host itself (`setsid bash -c
'bash ~/lcm_update.sh > ~/lcm_deploy_$(date +%s).log 2>&1'`) and read the log
back afterwards. One restart per release, never one per verification step.

## Lessons recorded (do not regress)

- `tuple concurrently updated` during plugin load = transient CPython/catalog
  race; sporadic (1–8 per host), self-heals, not fleet breakage.
- Pool timeouts: check `LCM_PG_POOL_MAX` (per-process) *before* suspecting the
  server ceiling; attribute holders via socket inodes, not `ss -p` pids.
- Hosts fetch their **staging repo's `main`**, so a GitHub push alone changes
  nothing host-side — push to GitHub *and* fan out to staging, then verify
  `plugin_<sha>` per host.
- Host launcher `~/lcm_update.sh` must stay in sync with the repo's
  `scripts/lcm_update.sh`; a stale launcher silently skips config-sync steps.
