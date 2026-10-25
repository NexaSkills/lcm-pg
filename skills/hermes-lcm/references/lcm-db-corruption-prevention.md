# lcm.db corruption prevention (verified on coordination-host, Oct 2026)

## Signature taxonomy (read-only `PRAGMA integrity_check` on quarantined copies)
- "wrong # of entries in index ..." + isolated "Rowid <huge> out of order" / "2nd reference to page": lost-flush / stale-write artifacts from the storage layer, not a plugin or SQLite-logic bug.
- Widespread "btreeInitPage() error" across trees: deep torn-page damage.

## Facts verified on coordination-host
- Plugin applies `journal_mode=WAL` + `synchronous=FULL` + `mmap_size=256MiB` + `busy_timeout` on EVERY store connection (`db_bootstrap.configure_connection`, used by all 9 store modules). Do not lower synchronous below FULL.
- NOTE: `pragma synchronous` read back from a fresh sqlite3/CLI connection shows the CLI default (2), NOT what the gateway connections use — pragmas are per-connection. Verify in code, not via a probe connection.
- Host is a KVM guest (QEMU HARDDISK, queue write_cache="write back"). The durability guarantee lives in the hypervisor's flush handling, not in SQLite/Postgres.
- Gateway systemd unit: KillMode=mixed, TimeoutStopSec=70. Graceful SIGTERM shutdown normally takes ~3s; slow stops that hit the 70s SIGKILL are the in-guest process-kill vector.
- `lcm-swap.sh` boot repair covers only `~/.hermes/lcm.db`; profile DBs under `profiles/*/lcm.db` are NOT covered.

## Prevention checklist (no-backend-change route)
1. Hypervisor: VM disk must honor FLUSH (QEMU cache=writeback with flush, cache=none, or directsync). Postgres on the same disk shares the identical exposure — full_page_writes only fixes torn pages, not lost commits.
2. Nightly online backups of every lcm.db via sqlite backup API (`sqlite3 "file:db?mode=ro" ".backup dst"`) — never `cp` a live db.
3. Periodic read-only `PRAGMA integrity_check` per db → alarm while backups are fresh.
4. Optional hardening: set mmap_size=0 in configure_connection for VM/CoW-FS environments (one-line local patch).
5. btrfs: monthly scrub; data extents are NOT checksummed on single-device btrfs by default.

## UPDATE 2026-10-16: ROOT CAUSE FOUND (not the hypervisor)
- Proxmox disk cache=writeback was CORRECT and never the vector (flushes land;
  corruption continued on correctly-configured hosts). Signature = torn/duplicate
  pages, not lost commits.
- Cause: guest-kernel btrfs regression 095be159f3eb (~6.13+; fix posted 2026-07
  'btrfs: write-protect folios during data writeback') - dropped folio_mkclean()
  write-protect lets a WRITABLE shared mmap (SQLite mmap_size>0) mutate a page
  during checksum/copy for writeback -> silent lost write / bad csum. Guest kernel
  7.2.5-3-omarchy predates the fix. Damage pattern matched mmap_size=0 LOCAL PATCH
  coverage EXACTLY (unpatched buttercup standalone plugin copy = only db corrupting live).
- Permanent invariants on coordination-host: (1) mmap_size=0 LOCAL PATCH in
  ~/.hermes/plugins/hermes-lcm/db_bootstrap.py; (2) profile plugin dirs must be
  SYMLINKS to the default plugin dir (buttercup stale copy fixed 10-16, kept as
  plugins/hermes-lcm.unpatched-20261016-093900); (3) lcm-local-patch-watchdog.sh
  scans plugins + installs + profiles/*; (4) lcm-boot-audit offline integrity =
  trusted detector; (5) boot-gated swaps: hermes-lcm-swap.service (default db via
  lcm.db.repaired-*) + hermes-lcm-profile-swap.service (profiles/*, candidates
  staged in backups/repaired-*; quarantines .corrupt-TS; self-disarm on success).
  Both verified fail-safe: zero-open-fd check ABORTS while daemons live, retries next boot.
- Profile db with unreadable page 0 (brandbuddy Oct-16): unrecoverable - accept
  empty LCM; transcripts live in that profile's state.db. fts-only damage ->
  INSERT INTO <fts>(<fts>) VALUES('rebuild').
- Never run .dump on a LIVE corrupted db (mmap read of a corrupt page killed
  webui pid 27284 at 07:54). Snapshot via cp -a --reflink=auto + immutable=1 first.
