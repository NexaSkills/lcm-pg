"""usage — honest context-occupancy stats for surfaces that read the
compressor protocol generically (gateway /stats and /usage, via
``agent.context_compressor`` == the LCM engine).

Why this file exists (fleet incident 2026-10-25/27): the LCM engine kept
``last_prompt_tokens`` as the *raw turn usage* from the model response. The
generic stats lines render ``last_prompt_tokens / context_length`` as context
occupancy, so every Hermes surface showed a wrong "85% context used" on every
host except the coordination box. Hermes' own marker
(``agent.context_breakdown.context_display_source``) only "works" on engines
that keep a ``last_real_prompt_tokens`` ledger — the built-in compressor's.

Contract (mirrors the built-in compressor, from its consumer code):
  * ``last_prompt_tokens`` = CURRENT session-window occupancy (what /stats
    divides by ``context_length``),
  * ``last_real_prompt_tokens`` = the provider's last real prompt figure,
    kept EQUAL to ``last_prompt_tokens`` here so the display source reports
    ``provider_usage`` (no "~estimate" tilde) instead of a misleading number.

Occupancy is computed, not guessed: summaries in window + fresh-tail messages
+ system/tools overhead, clamped to the window. The raw turn usage still
lives on in ``last_prompt_tokens_raw`` (verifier/tests) and every lifecycle
record keeps its own ``compression_count_at_record`` epoch baseline.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _window_messages(engine: Any) -> Optional[list]:
    """The session's current window: active DAG summaries + the fresh tail
    (the same shape _assemble_context shows the provider), or None when
    nothing is computable. Compacted spans are represented by their summary
    nodes only — never double-counted with the raw rows they replaced."""
    sid = getattr(engine, "session_id", "") or ""
    msgs = None
    if sid:
        try:
            msgs = engine._store.get_session_tail(sid, limit=1000)
        except Exception:
            msgs = None
    if not isinstance(msgs, list) or not msgs:
        return None
    # fresh tail stays verbatim
    try:
        tail = msgs[engine._fresh_tail_start(msgs):]
    except Exception:
        tail = msgs
    summaries: list = []
    try:
        nodes = engine._dag.get_session_nodes(sid) or []
        uncondensed: list = []
        for d in sorted({n.depth for n in nodes}, reverse=True):
            uncondensed += list(engine._dag.get_uncondensed_at_depth(sid, d) or [])
        summaries = [{"role": "assistant",
                      "content": getattr(n, "content", "") or ""} for n in uncondensed]
    except Exception:
        # DAG unreadable: fall back to counting the tail only (still a
        # current-window figure, never a lifetime-throughput lie)
        summaries = []
    return summaries + tail


def compute_context_occupancy(engine: Any) -> Optional[int]:
    """Current-context occupancy in tokens, or None when not computable.

    Never falls back to lifetime throughput (that is precisely how the 85%
    stat was born). A failed computation returns None and leaves the previous
    displayed value untouched.
    """
    window = _window_messages(engine)
    if window is None:
        return None
    try:
        from .tokens import count_messages_tokens
        used = count_messages_tokens(window)
    except Exception:
        return None
    overhead = 0
    try:
        # the engine keeps no system-prompt cache; the anchor it replays is
        # counted via a bounded default (system+tools overhead per session)
        anchor = getattr(engine, "_pending_context_anchor_messages", None)
        if anchor:
            from .tokens import count_messages_tokens
            overhead = count_messages_tokens(anchor)
    except Exception:
        overhead = 0
    used += min(overhead, 16000)  # system+tools overhead, sane ceiling
    cap = int(getattr(engine, "context_length", 0) or 0)
    if cap > 0:
        used = min(used, cap)
    return int(used)


def sync_display_usage(engine: Any) -> None:
    """Point the compressor-protocol display fields at real occupancy.

    Called after every successful usage record (record_response / the
    auxiliary ledger update) and after every compression pass.
    """
    occ = compute_context_occupancy(engine)
    if occ is None:
        return
    engine.last_prompt_tokens = int(occ)
    # equal figures => context_display_source() == "provider_usage" (no tilde)
    engine.last_real_prompt_tokens = int(occ)
