# Mobile recovery and operational progress

A delivered task can retain a work lock when an earlier tool or upload failed.
The lifecycle now distinguishes terminal execution, verified task completion and
platform delivery. DeepSeek Flash/max judges the goal; the host owns the proof
and the serialized provider transition.

## Work and delivery

Successful, failed and canceled tools are terminal. Active tools, unknown
execution, native turns, queued work and unsettled handoffs continue protecting
the work model. The reviewer receives terminal counts, failure details, original
requests, context follow-ups, public replies and delivery evidence. It runs at
completion/delivery/exit boundaries and retries kept or failed reviews every
twenty minutes. A local minute check records its own last check time.

Work review allows up to 128K output tokens and twelve minutes, including max
reasoning, after a real replay exhausted 64K without returning a decision.
Only a complete structured result can close work; hitting a token ceiling keeps
the lock and records the provider receipt. The smaller routine health review
has its own limits below. Reasoning tokens share the output budget under the
[provider's thinking API](https://api-docs.deepseek.com/guides/thinking_mode/).

Uploads checkpoint uploading, uploaded, message-submitting and platform-accepted
separately. Only message submission can create an uncertain delivery. Files and
text use a stable UUID; an uncertain submission is reconciled under its original
ID. A failed upload can be fulfilled by a received replacement with byte-for-byte
artifact proof, including ordered split archives and an identical archive member.
The attempt stays failed. The host verifies receipts and bytes again before
applying DeepSeek's completion decision.

Owner input has preparing and submitting states. Optional background memory
defers during an active native turn; the existing steering path still accepts
the message. A pre-submission failure can safely retry the same input ID.
Post-submission ambiguity requires reconciliation. Casual and unclassified
follow-ups preserve the task's requirement version and completion proposal;
confirmed work adds a requirement. All follow-ups remain available to review.

Owner subscriptions to a switch notification survive internal completion
requests and restarts. The notification follows actual provider/model/session
verification, has one durable send ID and reconciles unknown receipts.

## Independent appraisal progress

The rollback flag is `operational_lanes`, enabled after a stopped-worker migration.
Original evidence and existing scores remain in place. Old overdue idle wakeups
are superseded by one current-state evaluation.

| Lane | Input | Commit |
|---|---|---|
| Action | Complete selected evidence, latest interaction, compact state and habits | Emotion, motives, wishes, concerns and next review; atomic enrichment job |
| Enrichment | Frozen source batch, work/share candidates and graph references | Memory, associations and coverage; no new emotion or contact |
| Session maintenance | Native context and continuity observations | Existing session advice only |

Action and enrichment have independent host workers and persistent leases.
Commits remain serialized SQLite transactions. A heavy failure cannot hold the
action lease. Structured memory output is reused when available; otherwise
enrichment uses the original sources in a separate request. Compression reuses
completed parts under the frozen source/record revisions. Referenced source
changes require a new snapshot.

Invalid graph fields receive one bounded schema correction. Repeated enrichment
failures enter a needs-repair queue with their evidence and errors; subsequent
batches continue. Historical migration cursors retain references to deferred
repair jobs rather than claiming those records were integrated.

Idle evaluation has its own persisted schedule. DeepSeek chooses 20–120 minutes;
the initial recovery evaluates current context. A pending independent idea does
not block another verified intent. A pending review of that intent or an
invalidated source still blocks it. Sending retains owner-epoch, source,
deduplication, work-lock, threshold and quiet-hour checks.

## Visibility and deployment

`operational-status` reports queue counts, action success and next review,
enrichment progress, exploration and accepted contact records without messages
or model reasoning. The native runtime separately reports execution, current
model, task review and notification receipts.

Mobile health review uses a 64K output ceiling and an eight-minute deadline.
Failures retry after five, fifteen and then fifteen minutes; success restores
the four-hour cycle. A fault identity survives changing timestamps. Host control
replies settle the relevant pending input only after a platform receipt, without
clearing a newer input's wait.

Deploy after synthetic tests and a private replay: back up the database and
mutable host state, verify restoration, stop only verified owned idle processes,
migrate once, then resume the same native conversation. Do not clear a live
task lock manually. Record both the model review and actual switch/send receipts.
Keep a four-hour observation spanning at least two autonomous decisions.
The daily desktop fallback remains separate and proposes improvements for
owner approval.

Rollback disables `operational_lanes` and restores host code. Keep new records
and deliveries; do not overwrite an active database with an older snapshot.
Private recordings, reconciliation evidence and credentials are never published.
