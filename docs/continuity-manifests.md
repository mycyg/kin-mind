# Continuity manifests and verified context delivery

Kin uses one sourced working set for native recovery and linked-memory recall. The shared database remains authoritative for events, task results, works, discoveries, concerns, wishes and actual sharing receipts. A manifest is a derived view; neither a model summary nor a native compaction summary becomes new evidence.

## Selection and recovery

`ContinuityManifest.select` combines local graph traversal, task-linked works, current matters and exact pending journal receipts. Explicit task and intent dependencies take priority; keyword matches are candidates. Open matters can be retrieved beyond the recent-message window. Time proximity alone does not establish a relationship.

Each selected discovery travels with its current sharing coverage and evidence basis. An unmerged, accepted outbox receipt with a platform message ID participates immediately. File-operation records carry the observed author, fingerprint and task ID even before semantic ingestion. A public task-result claim remains public output; it does not certify execution or delivery. Uncertain sends remain uncertain.

`SessionCheckpoint` preserves the question behind a short answer and complete recent reply bubbles. Required task conditions and source corrections must fit; optional associations use spare room and otherwise remain in the reading index. DeepSeek can compress complete older evidence outside the dispatch mutex. Incomplete critical coverage defers host compaction or promotion.

The local journal persists completed turns and operation receipts immediately. An idle minute tick refreshes a shadow manifest without a model call. Before compaction the host collects a fresh snapshot, then revalidates input, task and source revisions. Native auto-compactions are reconciled both on ticks and before dispatch; restoration is prepared from fresh sources, not a checkpoint from a previous epoch. The dispatch path uses existing summaries and local projection only. If it needs new compression, preparation continues outside dispatch.

Current native compaction remains the first option. Missing facts call for recall; native rotation still requires a completed compaction in the current generation and specific, valid degradation evidence afterwards. GPT work remains on its work model.

## Preparation is separate from injection

`mind_context_deliveries` stores a frozen body, SHA-256, marker, source revisions, window epoch and stable operation ID.

1. `prepare` records a candidate without increasing window usage or reading depth.
2. `begin` rechecks its sources, current epoch, outstanding appends and remaining budget.
3. The mobile host appends one internal assistant context item through the native injection API.
4. It verifies the exact body hash and assistant marker in the correct native rollout.
5. `acknowledge` atomically records acceptance, window usage and actual reading depth.

A lost acknowledgement is reconciled against the same native item. Absence of proof never authorizes a second append. A correction after injection still counts the bytes that entered the context, but stale facts are excluded from automatic deduplication. A delayed receipt from an earlier epoch is historical and cannot clear or charge the current window.

Restore text and automatic chat/proactive background use this delivery path. They are internal context, not owner messages, channel bubbles or repeatable task instructions. The provider's reasoning is never part of these records. Only the background actually accepted by the native thread is counted; explicit reads remain available after automatic deduplication.

## Budget and caches

| Use | Default budget |
| --- | ---: |
| Fresh window or post-compaction restoration | 2,000 tokens |
| Ordinary added chat background | 800 tokens |
| Proactive draft | 2,500 tokens |
| Work background / idle work restoration | 4,000 tokens |
| Explicit history page | 2,000 tokens |
| Automatic additions in one native window | 12,000 tokens |

Counts use the host tokenizer and include the injected envelope. Provider billing and native window pressure are measured separately. Native retention after compaction is marked unknown until a sourced check provides evidence; it is not inferred from a successful request alone.

Content overviews use stable content and source revisions. Mutable sharing facts are compiled alongside them, so a new receipt can refresh coverage without recompressing unchanged text. Graph, source, entity and coverage dependencies still invalidate affected views. Unrelated appends do not invalidate the working set.

Full-result and segment caches report current `model_requests`; a reused receipt is not another API call. Memory-summary cache hits, native-provider cache usage and local injection deduplication remain separate metrics. Missing provider cache data stays unknown.

## Host interfaces

| Action | Input / result |
| --- | --- |
| `session-snapshot` | Pending journal, current tasks and intent → public events, linked units and durable/semantic watermarks |
| `session-checkpoint` | Snapshot, binding, budget, `allow_model`, optional `shadow` → manifest, coverage, dependencies and counters |
| `session-validate` | Checkpoint → current-source validation; optional deterministic quality result |
| `continuity-manifest` | ID or conversation, cursor and limit → bounded manifest history and source metadata |
| `context-delivery-prepare` | Session, epoch, event ID, public text, dependencies and budget → frozen prepared body |
| `context-delivery-begin` | Session, epoch and ID → sending, waiting or stale |
| `context-delivery-ack` | Exact native session, marker, text hash and verified receipt → accepted |
| `context-delivery-uncertain` / `pending` | Persist or reconcile outstanding appends |
| `context-delivery-metrics` | Session → receipt-state counts and current window usage |

These are authenticated host actions, not model-controlled claims of execution. Existing `read_continuity_context`, `read_share_history`, `read_work_history` and `read_conversation_checkpoint` remain the public reading tools. Ordinary replies, proactive drafts and desktop reads share the same selection module. Candidate native segments inherit the manifest payload through the existing verified handover.

## Rollout and rollback

Five independent memory settings default to false:

- `manifests`: linked selection and shadow manifests.
- `manifest_restore`: use manifests in native restoration.
- `context_receipts`: verified automatic-background accounting.
- `continuity_overviews`: include linked sources in existing background overview work.
- `continuity_quality`: report deterministic coverage/freshness checks.

The last switch does not launch an independent model on every read. Sourced post-compaction failures continue through the existing session-advice lane. Disabling switches retains all source records and receipts. The host's existing session-management and native-compatibility switches remain in force.

Before enabling, back up SQLite through its online backup API and verify a restored copy. Replay real cases privately, then exercise native injection, completion, restart and recovery with synthetic data in an isolated process. Check final service health and unchanged mobile binding. Private bodies, credentials, recipients and native IDs stay outside the public repository.

Synthetic tests cover pending authorship and sharing, correction invalidation, a fifth required unit, exact acknowledgement proofs, late epochs, atomic rollback, reading depth, query-independent cache reuse and no extra requests for ordinary local context. A native probe tests the actual API path; it does not establish long-term conversational quality or a production cache improvement.

See [mobile session management](mobile-sessions.md) and [linked-memory integration](memory-continuity.md).
