# Event graph and disclosure coverage

Kin keeps events, participants, work versions, findings, deliveries and responses in one scoped SQLite database. The graph is an index over original evidence. A person can be a requester in one event and a creator in another; a finding can continue several projects. Time proximity supplies candidates, not proof of a relationship.

## Data and evidence

`mind_graph_nodes` stores event, thread, entity, finding and association nodes and projections of existing works, artifacts and explorations. Every node and edge has a stable ID, revision, source references, occurrence time and recording time. `mind_graph_revisions` and idempotent commands retain the previous values and inverse patches for corrections, merges and splits. Host-bound owners on different channels resolve to one entity. Display names alone never establish identity.

Supported relationships are participation, membership, continuation, response, production, delivery, sharing, correction, resolution, support, contradiction, cause, sequence and association. Cause requires an evidence basis. Subjective associations have their own layer; old unverified records remain visible with review status. They do not become independent evidence by being summarized again.

Source changes, deleted evidence, graph revisions, merged entities and revised disclosure mappings invalidate dependent context. Historical records keep their original evidential status. Corrections to a finding's text create a new content version; the older delivery remains in history.

## What has actually been shared

A finding has its own ID and version. Its coverage key is the bound recipient, finding and version. One result can therefore be partly shared. Filename changes, archive compression and root-folder renaming do not replace the existing work identity; ZIP identity uses a bag of member content fingerprints. File delivery and discussion of the file's conclusions remain separate records.

The common sending sequence is:

1. Recall the finding together with authorship, corrections and previous deliveries.
2. Register each public bubble's references, bound to its exact text.
3. Check current coverage, durable outbox receipts and competing reservations.
4. Freeze public text, references and bubble identifiers.
5. Settle only the bubbles with actual platform receipts.
6. Let the existing DeepSeek evaluation interpret feedback and update relationships.

`registered` is not `sent`. A successful server receipt is not a phone read receipt. Unknown outcomes retain the original identifier for reconciliation. Semantic backlog does not hide already ingested receipts, and the outbox remains queryable during a journal gap. Each coverage correction has revision history.

Old conclusions may be revisited as an explicit development, reflection, reminiscence or requested retelling. The relation to the earlier discussion is recorded. Merely changing wording does not create a new finding. Affection and ordinary conversation are not finding-level duplicates.

Older replies without references use a small DeepSeek Flash/max matching request when they concern past discoveries or claim a new discovery. The checker accepts only supplied finding IDs and structured results. An incomplete check preserves the candidate. Ordinary conversation without historical claims does not require this extra request. Model reasoning is never stored as a finding or sent as a bubble.

## One evaluation pipeline

Raw host events are durable before semantic evaluation. Actual operations and receipts are registered locally. The existing DeepSeek appraisal then submits memory links, graph proposals, disclosure mappings, habits, affect and wishes in one transaction. Only current evaluated sources can authorize new judgments.

The queue combines complete events under a 24,000-token input target and a 40-source ceiling. Oversized individual events use the existing source-preserving compression route. Historical graph interpretation has a persisted newest-first cursor, reuses the same appraisal worker, and evaluates old receipts against their matching findings. Live events take priority over historical backfill; failed jobs retain their cursors and retry timing. A backfill does not create feelings, reopen completed wishes or send old findings.

Idle appraisals remain independently scheduled by DeepSeek within 20–120 minutes. Exploration remains curiosity-driven and Kimi executions have a twenty-minute ceiling. The work lock, shared conversation, proactive threshold of 75 and Singapore 00:00–09:00 quiet hours remain host responsibilities.

## Recall and budgets

A graph lookup first considers identifiers, relevant text and entities, then recent 72-hour and open-thread candidates. Explicit matches can reach the full history. Focused expansion has at most three automatic hops; query expansion contributes at most forty additional candidates per hop. Explicit reading can continue by cursor.

| Context | Default tokens |
|---|---:|
| New native context / recovery | 2,000 |
| Added casual-chat background | 800 |
| Proactive draft | 2,500 |
| Work background | 4,000 |
| Explicit reading page | 2,000 |
| Accumulated automatic background per native window | 12,000 |

A finding, its provenance, important corrections and disclosure coverage form one context item. The graph itself stays in storage. Automatic context exposes at most three works and five shares, with cursors for deeper reading. Explicit reads are not blocked by automatic-injection deduplication.

Overflow is compressed by DeepSeek in complete paragraphs/events. Validated intermediate batches are checkpointed and reused after a later batch fails. Cache keys bind source content and revisions, graph/coverage revisions, query, budget and compression configuration. Original evidence, uncertainty, negation, dates and completion conditions remain available. Failed compression returns complete evidence items and a coverage-insufficient state. Host-rendered background, rather than the debugging JSON envelope, is what counts against the injection budget.

## Conversation can change habits

Explicit conversation preferences can update exploration frequency, directions, minimum interval and pausing without editing the core persona. A habit update includes owner evidence, an expected revision, a command ID and a short reason. Assistant self-descriptions cannot authorize preference changes. Stale consent falls back to the default pending review.

With `reply_choice: autonomous`, Kin can choose `reply`, `silent` or `merged` for an actually received casual input. Silence is deliberate, scoped to that input and separately recorded from transport failures. Every new input starts with its own choice. A merged decision identifies another received input; the host keeps work deliveries on the work path.

## Interfaces

HTTP, MCP and generated SDKs provide:

| Operation | Purpose |
|---|---|
| `read_graph` | Focus/query/time/layer/type filters, evidence-bearing edges, cursor and bounded expansion |
| `read_graph_object` (HTTP) | Original projection and graph revision history |
| `read_event_thread` | Budgeted event → work → delivery → follow-up context |
| `revise_graph` | Correct, retract, restore, merge, split or undo with source and revision checks |
| `register_reply_references` | Bind exact public bubbles to content references; never sends |
| `read_share_history`, `read_work_history` | Existing interfaces with finding references and delivery/provenance information |
| `read_continuity_context` | Related history, bounded summaries and continued reading |
| `update_conversation_habits` | Apply a sourced owner preference |
| `choose_reply` | Persist the decision for one received input |

The console opens in 2D and offers a 3D toggle, filters, incremental expansion, a keyboard-accessible timeline, source links, actual sent text and revision controls. The first page contains at most 150 nodes; a page and the retained incremental view contain at most 300.

## Host integration and migration

Enable `sharing`, `graph`, `associations` and `graph_recall` independently through `MemoryContinuity.configure`. Disabling graph recall preserves the evidence and delivery ledger. Connect `ReplyGuard` and `createContactBatch` to the authenticated sender, journal and actual work-state probe. The native session identifier and provider are owned by the existing router.

Before migration, take a consistent SQLite backup and copy the source vault; rehearse against that isolated copy. `GraphMigration.batch()` processes newest runtime events, explorations and existing relations with durable cursors and per-item savepoints. It preserves unresolved records and records deferred items. `match_shares()` interprets only real public message bodies and recorded receipts, stores a resumable matching result, and never schedules contact. Re-run with `force=True` when additional receipts justify a new match.

Examples, tests and benchmark data are synthetic. Keep actual conversations, graph contents, recipient IDs, source files and credentials outside the public checkout.

## Verification

Run `pytest`, `node --test adapters/*.test.mjs` and the console's Playwright suite. The synthetic [scale probe](../scripts/benchmark_event_graph.py) builds 50,000 events, 1,000 works and 10,000 disclosure records, then checks paging, old-event retrieval and chat injection limits. Focused tests cover partial/unconfirmed sends, cross-channel duplicates, concurrent reservations, corrections, merge/split undo, source invalidation, atomic rollback and per-input silence.

See [the recorded validation results](event-graph-validation.md) for measured results and the limits of those measurements.

Pending ordinary replies retain their public body, task association and stable transport ID across restarts. The host rechecks the original input and active task before retrying a completed semantic review. Uncertain transport receipts remain held for reconciliation.
