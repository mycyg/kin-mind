# Exploration recovery and timestamped continuity

Exploration sharing decisions now name the result source and revision being settled. The durable original event selects the target; a referenced older exploration cannot replace it through source sorting. Legacy records are resolved only when the original host event or a single result identifies the target. Ambiguity stays pending. A missing target decision can receive one bounded DeepSeek repair; structured judgments and receipts remain auditable without thinking blocks.

Historical enrichment creates memory notes and graph nodes before resolving their cross references, within one transaction. Both can use each other's unique local keys. Source revisions and referenced graph revisions still govern commits. Unrelated mood changes no longer invalidate a history-only commit. The history request schema contains just a reason and memory proposals.

The host-only `recover-history` command takes unique `job_ids`, a sourced `command_id` and optional verified replacement proposals. It still accepts the older `workers_stopped` and asks nothing of it: the rows it resumes are quarantined, and a quarantined row is held by no worker. [What proves the workers stopped](mobile-recovery.md#what-proves-the-workers-stopped) covers the evidence the other recovery commands ask for in its place. It only resumes quarantined historical jobs, keeps original IDs, attempts and errors in recovery history, and reruns normal validators. It does not mark jobs complete, alter scores or replay messages. Reused results keep their original provider receipt. A rejected seed proceeds to a fresh historical review instead of repeatedly retrying the same proposal. When the resumed result is the row's own and the row recorded an input manifest for it, the seed carries that manifest and is compared like any other reuse: it commits without a model call only while what a history judgment is shown has not moved, and is revalidated or judged afresh otherwise. A seed without a manifest — an older row, or a replacement an operator supplied — is checked as before, against its own sources.

Quarantine is no longer confined to this lane. `recover-appraisals` resumes a quarantined row of any lane, needs no worker shutdown and never replays a stored proposal as a seed; `recover-batched` settles the jobs an interrupted parent left batched. Both preserve the failure history and are covered by [mobile recovery and operational progress](mobile-recovery.md).

## Dialogue and clocks

Owner input enters a durable journal before memory ingestion. It does not wait behind older artifact or delivery events. Failed journal entries keep their original identity and retry independently after five minutes, then fifteen minutes; later inputs and receipts continue. A split ZIP tail can contain a directory without its member bytes. Such a file retains its actual byte fingerprint and delivery receipt, while member coverage remains unavailable.

Appraisals read a fresh window of the latest four available real dialogue exchanges on every attempt, independently of frozen historical batches. Consecutive user messages belong to one input block; assistant bubbles do not consume the turn count. Retrieval pages beyond a single bubble window. Imported real conversations retain their historical identity; internal notices, unconfirmed deliveries and tool reasoning do not become public dialogue.

DeepSeek compression leaves this recent window verbatim. Older evidence may be summarized with its original source times. Session checkpoints likewise keep the latest four exchanges, all their bubbles and the preceding question; older exchanges can be compressed. If the required evidence exceeds the budget, restoration reports incomplete coverage rather than truncating or claiming success.

The host can expand its normal 2,000-token restoration allowance to fit the pinned exchanges, with a hard limit of 8,000 tokens and a saved `budgetPlan`. Validation and delivery use that same allowance; injected tokens still count toward the native window ledger. Older history uses remaining space and DeepSeek compression. This prevents ordinary multi-bubble history from permanently blocking restoration without allowing unbounded context growth.

Recovered real input is eligible for this historical view, while remaining excluded from new-input execution and emotional replay. An exact aggregate of nearby accepted bubbles appears only once in the view. Its original output source and each delivery record remain in storage; uncertain or partial text matches do not establish receipt coverage.

Time fields have distinct meanings:

| Field | Meaning |
|---|---|
| `occurred_at` / existing `at` | Original event time, with its recorded provenance |
| `received_at` | Host receipt time when supplied; legacy memory receipt time otherwise |
| `delivery_at` | Accepted platform delivery time, when separately linked |
| `clock.current_time` | Host time at this evaluation or dispatch |
| `clock.local_time`, `timezone` | Singapore time with UTC offset and `Asia/Singapore` |

The mobile adapter adds dynamic clock facts to owner input, work interjections and proactive drafts. Original message IDs and timestamps survive dispatch delay and compaction. Missing platform timestamps remain unknown; the host receipt is not fabricated as a platform event. Clock facts do not modify the stable persona prefix or summarize old events as happening now. Platform acceptance remains distinct from reading a message.

Hosts should reserve the clock envelope in their background budget and deliver it on the actual dispatch boundary. `adapters/conversation-time.mjs` handles ISO timestamps with offsets and second/millisecond epochs. The `memory-context` host response also exposes fresh clock metadata outside cached evidence.

## Verification

Synthetic tests cover cross-kind forward references and transaction rollback, unrelated concurrent mood updates, exploration target ordering, history recovery idempotency, recent multi-bubble exchanges, timezone conversion, compression preserving recent public text, and thinking-block isolation. The existing task locks, quiet hours, send reconciliation and session identity checks remain in force.

Operational rollout uses a private SQLite backup, isolated replay, validation of current dependencies, a safe idle host boundary, and post-restart receipts. Private histories and provider credentials are excluded from the public repository.


A completed exploration with invalid final JSON may use one text-only repair within the original deadline, with at most 60 seconds remaining. The existing attempt receipt records repair admission before the request. Repair never reruns tools or adds sources; citation validation remains mandatory. Prose fields have no arbitrary short character cap. Multiple ambiguous JSON objects are rejected until a valid single result is supplied.

Compression progress counts newly completed valid parts for the current frozen input only. Cached parts from another job cannot reset a stalled attempt. Provenance and message envelopes count toward capacity; chunk completion and appraisal commit are separate outcomes.
