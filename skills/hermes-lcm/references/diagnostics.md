# Diagnostics

Use read-only product tools before changing configuration or running an apply path.

## Fast path

1. `hermes plugins`: confirm `hermes-lcm` is enabled and the selected context engine is `lcm`.
2. Send one normal message if the session has not been bound since restart.
3. `lcm_status`: inspect runtime identity, database path, context pressure, summary/store counts, filters, and lifecycle state.
4. `lcm_inspect`: inspect current-session lineage, frontiers, fresh tail, externalized-ref readability, and skip/no-op reasons without retrieving content.
5. `lcm_doctor`: run database, FTS, lifecycle, configuration, and context-pressure diagnostics.

If optional slash commands are enabled, `/lcm status` and `/lcm doctor` expose the corresponding operator views.

## Safe mutation order

For cleanup, repair, source normalization, or rotate:

1. run the read-only preview;
2. inspect exact candidates and paths;
3. create/confirm a backup;
4. obtain user authorization for the specific apply operation;
5. run one bounded apply and verify integrity afterward.

Cleanup apply is separately feature-gated. Never infer permission to enable it from a diagnosis request.

## Common states

- Unbound status after restart: send a normal message, then check again.
- Database exists but stays empty: verify plugin enablement, `context.engine`, profile, database path, and ignore/stateless patterns.
- Weak exact recall: verify source rows exist, query construction/scope is correct, summary health is sound, and embedding coverage/provenance matches the requested mode.
- Conflicting summary and raw evidence: prefer the newer exact raw evidence and inspect lineage.
- Path B/context-engine schema log: expected on hosts where plugin-registry handlers do not receive active messages; context-engine schemas and dispatch remain the healthy route.

## Forcing a genuine threshold_full_sweep (not overflow recovery)

Two compaction paths exist and the log line tells them apart:

- `threshold_full_sweep` — the normal path. Fires between turns when the prompt crosses `threshold_tokens = context_length * LCM_CONTEXT_THRESHOLD`. Reported in `lcm_status` under `threshold_full_sweep` (status, leaf/condensation/total_passes, duration_ms, tokens_before→after, summary_prefix_target, stop_reason). `never_run` there means it has not fired yet.
- `forced overflow recovery` — preempts the sweep when `observed_prompt_tokens >= min(LCM_MAX_ASSEMBLY_TOKENS, context_length - reserve_floor)`. A single turn can jump the prompt past the threshold in one step, so the sweep is skipped entirely and only overflow recovery runs.

To observe the normal sweep: grow context GENTLY in ~8–12K-token chunks so the prompt crosses `threshold_tokens` while staying under the assembly cap. A big single read (e.g. two ~90K-char source reads) overshoots the cap and only triggers overflow. Lowering `LCM_MAX_ASSEMBLY_TOKENS` or raising `LCM_RESERVE_TOKENS_FLOOR` makes overflow fire EARLIER — the opposite of what you want when chasing the sweep path.

## Verifying compaction losslessness

Summaries are cues; verbatim recovery is the guarantee. After a compaction, confirm every injected canary is still recoverable — it lives in ONE of two places:

- the `messages` table in `lcm.db` (`session_id` + `content` columns; `content LIKE '%CANARY-%'`), for inline outputs, and
- `~/.hermes/lcm-large-outputs/*.json` payload files, for large tool outputs that were externalized.

Scan both and union the canary set; anything absent from BOTH is a real loss. It is normal (correct, not a bug) for meaningless filler to be omitted from the summaries themselves — check the store + payloads, not the summary text.
