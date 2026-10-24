"""PG savepoint-hygiene parity: the sqlite implicit-tx emulation must survive
every _sqlite_savepoint statement sequence — including the outermost
ROLLBACK TO + RELEASE pair that the rollup builder's frontier helpers emit.

Regression: ROLLBACK TO decremented _savepoint_depth to 0, which made
_guarded_one commit and close the synthetic tx; the matching passthrough
RELEASE then raised NoActiveSqlTransaction and marked every rollup build
failed ('RELEASE SAVEPOINT can only be used in transaction blocks').
"""

import os

import pytest

pytest.importorskip("psycopg")

from backends import postgres as pg  # noqa: E402


def _dsn():
    return os.environ.get(
        "LCM_PG_DSN",
        f"postgresql://lcm01@/lcm?host={os.path.expanduser('~')}/lcm-pg/run&port=54329",
    )


@pytest.fixture()
def conn():
    c = pg.connect(_dsn(), "public")
    yield c
    c.close()


def _clean_end_state(conn):
    assert conn._tx_open is False
    assert conn._keep is False
    assert getattr(conn, "_synthetic_tx", False) is False
    conn.execute("SELECT 1")  # backend still usable (autocommit, unpoisoned)


def test_nested_savepoint_release(conn):
    for sql in ("SAVEPOINT a", "SAVEPOINT b", "RELEASE SAVEPOINT b", "RELEASE SAVEPOINT a"):
        conn.execute(sql)
    _clean_end_state(conn)


def test_rollback_to_then_release_outermost(conn):
    # The rollup-builder failure path: ROLLBACK TO closed the outermost
    # synthetic savepoint; the trailing RELEASE must be a no-op, not an error.
    for sql in (
        "SAVEPOINT a",
        "SAVEPOINT b",
        "ROLLBACK TO SAVEPOINT b",
        "RELEASE SAVEPOINT b",
        "RELEASE SAVEPOINT a",
    ):
        conn.execute(sql)
    _clean_end_state(conn)


def test_rollback_to_outer_then_release(conn):
    for sql in ("SAVEPOINT a", "ROLLBACK TO SAVEPOINT a", "RELEASE SAVEPOINT a"):
        conn.execute(sql)
    _clean_end_state(conn)


def test_repeated_cycles_on_one_connection(conn):
    for _ in range(3):
        conn.execute("SAVEPOINT a")
        conn.execute("SAVEPOINT b")
        conn.execute("ROLLBACK TO SAVEPOINT b")
        conn.execute("RELEASE SAVEPOINT b")
        conn.execute("RELEASE SAVEPOINT a")
    _clean_end_state(conn)


def test_real_query_after_rollback_to_cycle(conn):
    conn.execute("SAVEPOINT a")
    conn.execute("ROLLBACK TO SAVEPOINT a")
    conn.execute("RELEASE SAVEPOINT a")
    conn.execute("SELECT 1")
    _clean_end_state(conn)
