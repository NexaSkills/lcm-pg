"""Backend selection for the lcm-pg dual-backend fork.

SQLite remains the default. LCM_BACKEND=postgres routes stores to PostgreSQL
via backends/factory.py without touching upstream call sites (they keep
importing sqlite3; we hand them a shim at connect time).
"""

from __future__ import annotations

import os
import sys


def install() -> None:
    """Monkeypatch sqlite3.connect → backend factory when postgres is selected.

    Idempotent. When LCM_BACKEND is unset/sqlite this is a guaranteed no-op.
    """
    import sqlite3  # stdlib; already imported by every store module

    if getattr(sqlite3, "_lcm_backend_patched", False):
        return
    from . import factory

    _orig_connect = sqlite3.connect

    def connect(db_path, *args, **kwargs):
        if factory.backend_for() == "postgres" and str(db_path) != ":memory:":
            try:
                return factory.connect(db_path, **kwargs)
            except Exception:
                # never silently downgrade; log loudly then let sqlite try (it
                # will fail the same way, but the traceback shows both paths)
                print("lcm-pg: postgres connect failed, falling through to sqlite",
                      file=sys.stderr)
                raise
        return _orig_connect(db_path, *args, **kwargs)

    sqlite3.connect = connect  # type: ignore[assignment]
    sqlite3._lcm_backend_patched = True  # type: ignore[attr-defined]
