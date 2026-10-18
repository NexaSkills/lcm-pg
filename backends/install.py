"""Explicit backend install hook — no monkeypatching.

Call install() once at plugin load, BEFORE any store module opens a
connection. When LCM_BACKEND=postgres it binds backends.factory.connect as
the package-level `connect`, which every store module imports:

    from .backends.factory import connect

On the default sqlite backend, factory.connect falls through to plain
sqlite3.connect with identical semantics (uri handling included), so the
import switch is behaviour-neutral until LCM_BACKEND=postgres is set.

Why explicit instead of the earlier sqlite3.connect monkeypatch: stores
open connections in many threads at once; an import-time env check baked
into each store module keeps the decision in ONE place and leaves stdlib
sqlite3 untouched for every other consumer in the process.
"""

from __future__ import annotations

import os

from .factory import connect

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    backend = (os.environ.get("LCM_BACKEND") or "sqlite").strip().lower()
    if backend not in ("sqlite", "postgres"):
        raise RuntimeError(
            f"LCM_BACKEND={backend!r} is not a supported backend (sqlite|postgres)"
        )
    _INSTALLED = True
