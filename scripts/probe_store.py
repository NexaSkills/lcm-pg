"""Standalone probe: does the postgres path behave like SQLite for the
MessageStore surface (DDL, append, scan, FTS search, metadata)?

Run from the fork directory:
    LCM_BACKEND=postgres LCM_PG_SCHEMA=lcm_probe python3 scripts/probe_store.py
Nothing here imports hermes; it exercises backends/ only.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backends import factory  # noqa: E402
from backends import fts_pg  # noqa: E402

DSN = os.environ.get("LCM_PG_DSN") or f"postgresql:///lcm?host=/var/run/postgresql&user={os.environ.get('USER', 'postgres')}"
SCHEMA = os.environ.get("LCM_PG_SCHEMA", "lcm_probe")


def main() -> int:
    conn = factory.connect(f"{SCHEMA}/lcm.db")  # path only informs schema fallback
    cur = conn.cursor()

    # DDL (SQLite text through the shim)
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            store_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            source TEXT DEFAULT '',
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL,
            token_estimate INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, store_id);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
    """)

    # FTS via the pg path (messages_fts facade -> tsvector on messages)
    fts_pg.ensure_fts(conn, "messages", "content")

    now = time.time()
    rows = [
        ("s_probe", "cli", "user", "the quick brown fox jumps over the lazy dog", now, 10, 0),
        ("s_probe", "cli", "assistant", "paper towels and laundry detergent are on sale", now - 60, 12, 0),
        ("s_probe", "cli", "user", "unrelated content about postgres schemas", now - 120, 8, 0),
    ]
    for r in rows:
        cur.execute(
            "INSERT INTO messages (session_id, source, role, content, timestamp, token_estimate, pinned) VALUES (?,?,?,?,?,?,?)",
            r,
        )
    cur.execute("INSERT INTO metadata (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("probe", '{"ok": true}'))
    cur.execute("SELECT value FROM metadata WHERE key='probe'")
    assert cur.fetchone()["value"] == '{"ok": true}'

    cur.execute("SELECT store_id, role, content FROM messages WHERE session_id=? ORDER BY store_id DESC LIMIT 2",
                ("s_probe",))
    rows = cur.fetchall()
    assert len(rows) == 2 and rows[0]["role"] == "user", rows

    # FTS5-facade search SQL (exact shape upstream store.py uses)
    q = "paper OR towels"
    rows = cur.execute(
        """SELECT m.store_id, m.content, rank as search_rank
           FROM messages_fts fts
           JOIN messages m ON m.store_id = fts.rowid
           WHERE messages_fts MATCH ?
           ORDER BY rank LIMIT 10""",
        (q,),
    ).fetchall()
    assert len(rows) == 1 and "paper" in rows[0]["content"], rows
    print("FTS facade search OK:", [r["content"][:30] for r in rows])

    # datetime-in-epoch-space
    cur.execute("SELECT count(*) c FROM messages WHERE timestamp > datetime('now','-1 hour')")
    assert cur.fetchone()["c"] == 3

    # LIKE fallback shape used by dag.py fallback
    cur.execute("SELECT count(*) c FROM messages WHERE content LIKE ?", ("%postgres%",))
    assert cur.fetchone()["c"] == 1

    conn.close()
    print(f"PROBE PASSED on schema {SCHEMA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
