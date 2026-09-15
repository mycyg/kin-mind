# Compression-first mobile sessions

The host keeps a long-lived logical conversation across its channels. Each native thread is a numbered segment of that conversation. Shared memory, tasks, input IDs and delivery receipts remain in their existing stores.

## Decision order

DeepSeek Flash / max proposes `keep`, `recall`, `compact`, `defer`, `prepare` or `rotate` in the existing appraisal flow. Missing evidence calls for recall. Context pressure calls for compression of the current native thread. A new segment requires a completed compaction in the current generation and specific, still-valid degradation evidence observed afterwards.

The host estimates pressure from the verified effective window, current input usage and reserved output/tool space. Defaults request review at 65%, with 85% classified as critical. Cumulative billing, rollout size, elapsed time, generic warnings and compaction counts do not authorize rotation. A successful compaction preserves the current thread and task IDs. The 30-minute cooldown allows observation before another compaction.

A local minute tick reads lifecycle and usage records. It does not request a model on each tick. New pressure edges and sourced review requests enter the durable appraisal queue; session-maintenance jobs use a small operational view and cannot change affect, wishes, contact preferences or user activity. Normal appraisals may include the same observation. Advice is bound to an observation hash, so a newer input or task revision can invalidate it before action.

## Checkpoints and budgets

`SessionCheckpoint` joins durable public runtime events with the pending journal. It retains the preceding question, short user answers and associated reply bubbles. Runtime warnings and uncertain sends cannot become successful public dialogue. Repeated user words remain separate events; matching model-output/delivery records can share one displayed utterance.

Full source dependencies, hashes and revisions remain in the private registry. The injected payload contains public dialogue, task identity, recent input dispositions and shared-store reading links. Its default budget is 2,000 tokens, or 4,000 for idle work. Older material that exceeds the budget uses the existing DeepSeek compression cache; whole evidence is retained when compression is unavailable, with an incomplete checkpoint preventing promotion. Original evidence is never replaced or character-truncated.

The 12,000-token automatic-background budget is separate from native context pressure. Only a native compaction completion can reset that ledger. Durable epoch receipts make delayed duplicates harmless, including duplicates from an earlier window. A restored checkpoint is reconciled by its persisted native marker and accounted once. Source corrections invalidate the checkpoint and its dependent summaries.

## Execution and recovery

| Boundary | Host behavior |
| --- | --- |
| Active native turn, background tool, queued input or pending delivery | Wait; preserve the current model and work lock |
| Idle work with a complete checkpoint | Compact in the same thread; retain task and input versions |
| Uncertain compaction receipt | Reconcile the native event; do not repeat the operation |
| Candidate creation response is uncertain | Retain the original attempt; do not create another candidate |
| Candidate injection response is uncertain | Check the original marker in native history |
| New input or source revision while preparing | Invalidate the prepared checkpoint before commit |
| Crash after binding commit | Roll forward to the committed generation; never replay accepted input |
| Late output from an old native segment | Keep operation evidence; reject its authority to send |

The session manager shares the model router's dispatch mutex and owns a process lease. Preparation and optional DeepSeek compression happen outside the commit mutex. Promotion rechecks real native status, source revisions, task versions, input cursors and actual model/effort/Fast settings. Paused work handover is disabled by default.

After a host restart, the same coordinator restores the persisted routing mode: unfinished work keeps GPT, while an idle automatic conversation restores DeepSeek. Pending native input or delivery blocks this recovery. No synthetic user message or new work task is created. Pressure measurement resumes from native history; a post-compaction context-size estimate is retained even when the native receipt has zero input/output counters.

Candidates use a separate read-only mobile app-server process with configured MCP servers disabled and no channel transport. The host injects public history with `thread/inject_items`, verifies its persisted marker, and runs a structured internal continuity check. This check is a host event, not a synthetic user message. Candidate-only execution restrictions are supplied by the process and verification turn rather than a permanent persona change. Promotion loads the new thread in the shared mobile ACP process and retires the old subscription.

DeepSeek's Responses API does not accept a named native `toolOutput` without a `call_id`. The gateway handles only the named continuity-check event: it supplies the public transcript as quoted historical data and the verification request as an internal event. This also avoids inventing reasoning state for imported assistant messages. Ordinary user/tool messages retain their existing roles; internal verification output never goes to channel transport. See the [App Server injection interface](https://learn.chatgpt.com/docs/app-server#inject-items-into-a-thread) and [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/).

## Integration

The generic adapters are `session-manager.mjs`, `session-policy.mjs`, `native-window.mjs`, `native-candidate.mjs` and `mobile-session-host.mjs`. `codex-runtime-patch.mjs` adds usage, compaction events, bounded public injection and retirement to a compatible ACP build. Installation fails closed when expected source anchors differ; updates require a compatibility probe.

Host configuration supplies private paths and the existing model catalog. It does not modify desktop Codex settings. The durable registry is the authority for `conversationId`, `generation`, `threadId` and `nativeSessionId`; these are not inferred from each other.

| Host interface | Purpose |
| --- | --- |
| `read_mobile_runtime` | Current native model, generation, pressure, policy revision, advice and operation receipts |
| `request_session_rotation` | Register review/compact/rotate intent with a stable command ID; return without waiting for the current turn |
| `read_conversation_checkpoint` | Public checkpoint, sources, coverage and reading entry points |
| `configure_mobile_sessions` | Version-checked mobile policy changes with source and command receipts |
| `session-snapshot`, `session-checkpoint`, `session-validate` | Private host projections and dependency validation |

`observe` and `compact` default to enabled. `prepare` and `rotate` default to disabled until native compatibility and private replay checks pass on a particular host. The readiness switches never remove the completed-compaction and post-compaction-evidence requirements. `pausedTaskHandover` remains disabled for the initial mobile rollout.

## Validation and limits

Synthetic tests cover pressure calculation, compression before rotation, fifty prior compactions without automatic rotation, protected work, input races, candidate profile checks, stale sources, process leases, crash recovery and out-of-order compaction receipts. Python tests cover bounded checkpoints, pending journals, source deletion and maintenance-only appraisal commits. An isolated native probe should additionally verify create → inject → restart/resume → internal check → short user continuation, followed by ACP load → compact → restore → continuation.

Keep private replay results, message bodies and native identifiers outside the public repository. A compatibility probe proves the tested path and version; it does not guarantee perfect conversational recall. Current-token measurements are distinct from cumulative usage, and a post-compaction input measurement may remain unknown until the next native turn. The system preserves uncertainty instead of treating a model's confidence as a successful operation or delivery receipt.
