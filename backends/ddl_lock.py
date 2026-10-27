"""Advisory-serialised DDL helper — prevents `tuple concurrently updated`.

Postgres does not lock single-row catalog writes (pg_proc / pg_type /
pg_trigger / pg_amop ...) for concurrent DDL: two sessions issuing
`CREATE OR REPLACE FUNCTION` / `CREATE TRIGGER` / `ADD COLUMN IF NOT
EXISTS` / `CREATE TABLE IF NOT EXISTS` against the SAME object raise
`ERROR: tuple concurrently updated` for all but the first-to-arrive
(known PG behaviour; the standard remedy is an advisory lock around the
statement — see the PG wiki / langchain#12933 / hindsight#2817 precedent).

Every lcm-pg backend `ensure_*` runs on EVERY connection open. When
several surfaces (gateway + webui + dashboard) start together after a
restart, their provisioning races on the same schema — on template this
dropped the whole plugin on boot ("Failed to load plugin 'hermes-lcm':
tuple concurrently updated", 2026-10-27).

Fix: run idempotent provisioning DDL under a transaction-level advisory
lock keyed by (schema, DDL text). Same DDL serialises across processes;
different DDL never blocks unrelated work; the lock never leaks past
commit. Fallback keeps the first statement on a wrapped 40001 so one
losing racer degrades to a wait, never an error.
"""
from __future__ import annotations

import hashlib

try:
    from psycopg import errors as _pg_errs
    _SERIALIZATION = getattr(
        getattr(_pg_errs, "KnownSQLState", None), "SERIALIZATION_FAILURE", "40001")
    _RETRY_SQLSTATES = {str(_SERIALIZATION)}  # 40001
except Exception:  # pragma: no cover
    _RETRY_SQLSTATES = {"40001"}

# `tuple concurrently updated` is raised by Postgres as InternalError
# (sqlclass XX000 — NOT a serialization class), so the state set alone
# never catches it; the message marker below is the primary detector.
# Verified locally: psycopg.errors.InternalError_.sqlstate == 'XX000'.
_RETRY_MESSAGE_MARKERS = (
    "tuple concurrently updated",
    "tuple concurrently deleted",
)


def is_concurrency_conflict(exc: BaseException) -> bool:
    """True when `exc` is a Postgres catalog-DDL race: a serialization
    class (40001) OR the InternalError (XX000) whose message names a
    concurrent catalog-tuple race (`tuple concurrently updated`)."""
    state = str(getattr(exc, "sqlstate", "") or getattr(exc, "pgcode", "") or "")
    if state in _RETRY_SQLSTATES:
        return True
    if state.startswith("40") or state == "40001":
        return True
    msg = str(exc)
    return any(m in msg for m in _RETRY_MESSAGE_MARKERS)


def ddl_key(*parts: object) -> str:
    """Stable text key for one idempotent DDL unit (schema + statements)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _lock_id(key: str) -> int:
    # advisory keys are int8; take the low 63 bits of the digest
    return int(key[:16], 16) & 0x7FFFFFFFFFFFFFFF


def _run_with_retry(conn, sql: str, attempts: int = 3) -> None:
    for i in range(attempts):
        try:
            conn.execute(sql)
            return
        except Exception as exc:
            if is_concurrency_conflict(exc) and i + 1 < attempts:
                import time
                time.sleep(0.1 * (i + 1))
                continue
            raise


def retry_serialization(label: str = "", attempts: int = 3):
    """Decorator: rerun an idempotent provisioning function when the DB
    rejects it with a serialization-class error (40001 — includes
    `tuple concurrently updated` from racing catalog DDL). Every lcm-pg
    ensure_*/install body is idempotent BY CONTRACT, so a full-body rerun
    after a jittered pause is safe. Non-serialization errors propagate
    untouched. `label` only names the function in debug logs."""
    import functools
    import time

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except Exception as exc:
                    if is_concurrency_conflict(exc) and i + 1 < attempts:
                        time.sleep(0.05 * (2 ** i))
                        continue
                    raise
        return wrapper
    return deco
