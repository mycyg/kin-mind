# Continuity manifests and verified context delivery

Kin uses one selection of sourced material for native recovery and linked-memory recall. The shared database remains authoritative for events, task results, works, discoveries, concerns, wishes and actual sharing receipts. A manifest is a derived view and is not stored: the checkpoints built from it live in the host's conversation registry. Neither a model summary nor a native compaction summary becomes new evidence.

## Selection and recovery

With the `manifests` setting on, `ContinuityManifest.select` combines current matters, task-linked works, graph traversal (when the graph is enabled), keyword history and exact pending journal receipts. It runs again for each session snapshot and each context build. Explicit task and intent dependencies take priority; keyword matches are candidates. Open matters can be retrieved beyond the recent-message window. Time proximity alone does not establish a relationship.

Each selected discovery travels with its current sharing coverage and evidence basis. An unmerged, accepted outbox receipt with a platform message ID participates immediately. File-operation records carry the observed author, fingerprint and task ID even before semantic ingestion. A public task-result claim remains public output; it does not certify execution or delivery. Uncertain sends remain uncertain.

`SessionCheckpoint` preserves the question behind a short answer and complete recent reply bubbles. Critical linked units (current matters, task-linked works and pending operations) must be covered. At most four linked units, the critical ones first, use spare room; the rest, associations included, stay in the reading index. DeepSeek can compress complete older evidence outside the dispatch mutex. When linked material is part of restoration (`manifest_restore`), incomplete critical coverage defers host compaction or promotion.

The local journal persists completed turns and operation receipts immediately. The minute tick rebuilds a shadow checkpoint without a model call when the session is idle and the snapshot cursors have changed. Before compaction the host collects a fresh snapshot, then revalidates input, task and source revisions. Native auto-compactions are reconciled both on ticks and before dispatch, and each completed compaction replaces the checkpoint waiting to be restored. A host-run compaction restores the checkpoint it built just before compacting, once its sources validate again; an automatic compaction, or a checkpoint whose sources changed, gets a fresh one. Restoration inside dispatch uses existing summaries and local projection only; when it needs new compression, it waits for preparation outside dispatch.

Native compaction is the first option. Missing facts call for recall; native rotation requires a completed compaction in the current generation and specific, valid degradation evidence afterwards. Compaction and promotion keep the thread's verified model.

## Preparation is separate from injection

`mind_context_deliveries` stores a frozen body, SHA-256, marker, source revisions, window epoch and stable operation ID.

1. `prepare` records a candidate without increasing window usage or reading depth.
2. The host reconciles outstanding appends; `begin` then rechecks the sources, the current epoch and, without native-window accounting, the remaining allowance.
3. The mobile host appends one internal assistant context item through the native injection API.
4. It verifies the exact body hash and assistant marker in the correct native rollout.
5. `acknowledge` atomically records acceptance, window usage and actual reading depth.

Before a new delivery the host looks up the outstanding appends of the current epoch (up to 16) by marker and hash: one that is found is acknowledged, and one that is not stays unconfirmed and is not appended again. A record still marked `sending` when its delivery resumes is looked up first and appended only when the history does not show it. A correction after injection still counts the bytes that entered the context, but stale facts are excluded from automatic deduplication. A delayed receipt from an earlier epoch is historical and cannot clear or charge the current window.

Restore text uses this path when the checkpoint carries linked material (`manifests` with `manifest_restore`); otherwise the host injects the checkpoint under its own marker and records it with `memory-injection-ack`. Automatic chat, work and proactive background use it with `context_receipts`; otherwise the window is charged when the context is built. Both are internal context, not owner messages, channel bubbles or repeatable task instructions. The provider's reasoning is never part of these records. With receipts, only background actually accepted by the native thread is counted; explicit reads remain available after automatic deduplication.

## Budget and caches

| Use | Default budget |
| --- | ---: |
| First chat addition in a fresh or compacted window | 2,000 tokens |
| Ordinary added chat background | 800 tokens |
| Proactive draft | 2,500 tokens |
| Work background | 4,000 tokens |
| Explicit history page | 2,000 tokens |
| Automatic additions in one native window | 12,000 tokens |

These budgets apply without native-window accounting (`native_window_context` off). With it on, an automatic addition takes what its selection needs within a fixed 8,000-token ceiling and the room the native window reports, and no per-window total applies. A restoration checkpoint starts from 2,000 tokens, or 4,000 with open tasks, and can grow to the same ceiling ([mobile sessions](mobile-sessions.md#checkpoints-and-budgets)). Counts use the host tokenizer and include the injected envelope. Provider billing and native window pressure are measured separately. Native retention after compaction is recorded as unknown; a successful request does not establish it.

Content overviews use stable content and source revisions. Mutable sharing facts are compiled alongside them, so a new receipt can refresh coverage without recompressing unchanged text. Graph, source, entity and coverage dependencies still invalidate affected views. Unrelated appends do not invalidate the working set.

Full-result and segment caches report current `model_requests`; a reused receipt is not another API call. Memory-summary cache hits, native-provider cache usage and local injection deduplication remain separate metrics. Missing provider cache data stays unknown.

## Host interfaces

| Action | Input / result |
| --- | --- |
| `session-snapshot` | Pending journal, current tasks and intent → public events and, with `manifests`, linked units and durable/semantic watermarks |
| `session-checkpoint` | Snapshot, binding, budget, `allow_model`, optional `shadow` → checkpoint with coverage, dependencies and counters |
| `session-validate` | Checkpoint → current-source validation; optional deterministic quality result |
| `context-delivery-prepare` | Session, epoch, event ID, public text, dependencies and budget → frozen prepared body |
| `context-delivery-begin` | Session, epoch and ID → sending, stale, waiting (without native-window accounting) or the state an earlier call left |
| `context-delivery-ack` | Exact native session, marker, text hash and verified receipt → accepted |
| `context-delivery-uncertain` / `pending` | Persist or reconcile outstanding appends |
| `context-delivery-metrics` | Session → receipt-state counts and current window usage |

These are authenticated host actions, not model-controlled claims of execution. `read_continuity_context`, `read_share_history`, `read_work_history` and `read_conversation_checkpoint` are the public reading tools. Ordinary replies, proactive drafts and desktop reads share the same selection module. Candidate native segments inherit the manifest payload through the verified handover.

## Rollout and rollback

Five memory settings default to false; `manifest_restore` takes effect only with `manifests`:

- `manifests`: linked selection and the shadow checkpoint.
- `manifest_restore`: use linked material in native restoration.
- `context_receipts`: verified automatic-background accounting.
- `continuity_overviews`: include linked sources in existing background overview work.
- `continuity_quality`: report deterministic coverage/freshness checks.

The last switch does not launch an independent model on every read. Sourced post-compaction failures go through the session-advice lane. Disabling switches retains all source records and receipts. The host's session-management and native-compatibility switches stay in force. A store that holds a `mind_continuity_manifests` table keeps it untouched; nothing writes or reads it, and archiving it is an operator step. Private bodies, credentials, recipients and native IDs stay outside the public repository.

`tests/business/context-delivery.test.mjs` covers the host side of delivery with simulated host calls: a busy thread defers background, an uncertain append is kept without replay, one uncertain identity does not hold back unrelated background, the receipt index keeps no text, and old unsettled identities are reconciled once. `tests/business/test_memory_bounds.py` covers records that go stale when their window moves on or when they are never begun. No native probe exercises checkpoint injection; the runtime proof covers compaction and resume ([mobile runtime](mobile-runtime.md)).

See [mobile session management](mobile-sessions.md) and [linked-memory integration](memory-continuity.md).
