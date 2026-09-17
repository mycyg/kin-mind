# Mobile recovery and operational progress

A delivered task can retain a work lock when an earlier tool or upload failed.
The lifecycle now distinguishes terminal execution, verified task completion and
platform delivery. DeepSeek Flash/high judges the goal; the host owns the proof
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

Invalid graph fields receive one bounded schema correction. Output whose only
fault is fields the schema does not define is stripped host-side, with no model
call and no retry; the removed paths stay with the attempt as
`receipt.dropped_fields`. Any other schema fault receives one bounded repair —
the historical lane always had it, and while section isolation is on every lane
does. Historical migration cursors retain references to deferred repair jobs
rather than claiming those records were integrated.

Eight sections — `habits`, `plan_changes`, `action_decisions`,
`procedure_candidates`, `concerns`, `wishes`, `wish_updates` and
`session_advice` — are applied inside a savepoint and a state snapshot, so one
can be refused alone with its SQL and its in-memory changes undone together. An
explicit table maps every appraisal field to the sections it rests on; the
module does not import while a field is unregistered. A refusal is recorded as
`rejected_sections` with a static code and literal host text only, never the
proposal, its evidence or any owner words. `habits` is an upstream section, so a
refused owner preference holds what rests on it — explore wishes and resumes,
`execute` on an explore or contact step, plan changes, curiosity — and each held
group is recorded as `held_sections` beside the refusal that caused it. A
refusal that leaves something to ask again, and anything held, arms one bounded
`held-sections` follow-up which carries its own evidence of the refusal. That
follow-up restates only what a commit can refuse or hold; it takes in no pending
event and leaves the source cursor where it is, so the parent's event is neither
scored nor remembered a second time, and it never queues a follow-up of its own.
A preference left out of it stays refused. Invalid advice on a
`session-maintenance` appraisal is repaired once, after which the appraisal
completes with the refusal recorded rather than blocking the lane. The switch is
`appraisal_section_isolation`, default on; an explicit `false` restores
all-or-nothing, drops no unknown field and makes no extra repair call.

Every lane has one cap. A charged attempt is a real appraisal call; after
`max_charged_attempts` (default 5, settable 1–20 through `configure-memory`), or
the same error signature twice in a row, the row is quarantined instead of paid
for again. Quarantine reuses the existing `needs-repair` state, which now means
the same on every lane: no further model call is paid for on that row until an
operator resumes it, while its evidence, errors and receipts stay readable and
subsequent batches continue. It records `repair_reason` with the count or code
that ended the budget — `repeated-failure`, `charged-attempts-exhausted`,
`compression-stalled`, `compression-passes-exhausted`,
`transient-failures-exhausted` or `preparation-conflicts-exhausted` — rebuilds the frozen context and releases
whatever the row had absorbed. A timeout is charged but never quarantines as a
repeat, because a long context can time out deterministically.
Transient provider failures — network errors, 5xx and 429 — produce no model
output, so they spend no repair budget and have a counter and backoff of their
own. Evidence-compression waits are continuations rather than attempts: they
keep the frozen inputs so cached parts keep their keys, and are bounded both by
real progress, meaning newly cached parts, and by a total number of passes. The
frozen context survives only while paid-for compression is tied to it; every
charged failure and every quarantine rebuilds it.

A conflict raised while the context is still being assembled, before any model
call, is not a charged attempt either: it has a counter and a bound of its own
and quarantines as `preparation-conflicts-exhausted`, rebuilding the frozen
context each time because that snapshot is what went stale. A conflict no retry
can resolve ends the row instead of spending the budget: the evidence it was
enqueued for is gone or was already appraised, so the row finishes in the
existing terminal `superseded` state with its code in `error_detail` and
releases whatever it had absorbed. When another attempt has already committed
the same judgment, the row completes from that durable receipt, with no second
model call and nothing scored again.

A failure keeps structured facts for the next attempt. `error_detail` holds the
exception class, the host's own static code and message, the conflicting object
and `kind` — what moved: `semantic` for a property of the proposal itself,
`runtime` for a version that changed under it, `unknown` for a failure the host
has not classified, which is always treated conservatively. `Conflict` and
`Missing` carry optional `kind`, `code`, `target`, `expected` and `actual`; a
runtime conflict whose `actual` moved is progress, not the same error twice.
Host error output and the API's 409 body carry `code` and `kind` beside their
existing fields. None of them ever holds payload text or a validation repr. The next attempt is told as data,
in `previous_attempt`, why the host refused the previous proposal: that detail
plus the refused sections, held sections, held decisions and dropped fields. An
operator resume clears it, so a row judged afresh is not argued with about a
proposal it no longer holds.

A commit conflict does not have to cost a whole new judgment. While it builds
the context, the host records an input manifest of what that attempt is shown:
the judgment type and lane, when it was built and until when it is valid,
digests of the prompt, schema, model parameters and projected context, policy
and persona versions, the latest owner input, the clock, the time-derived values
and the time boundaries shown, root evidence and every citable source as source,
hash and revision, and per class of input the identifiers, revisions and
enumerated states of what was shown — dimensions, wishes, concerns, sharing
decisions, rhythm, the stage-1 plan view, methods, preferences, graph, topic,
work and share candidates, pending events, dialogue items and the session
snapshot. It is content addressed in `mind_appraisal_manifests`; the queue row
keeps the digest. It holds no message body and no proposal.

When a commit then fails on a conflict the taxonomy marks reusable, the row
keeps the proposal, its receipt, the manifest it rests on and the static facts
of the conflict in `data.reuse`. The next attempt rebuilds its context exactly
as any attempt does and compares the two manifests. Relevance follows the
judgment type, never what the proposal happened to output: history
organization does not rest on mood, wishes, plans, methods, timing or the
session; a session judgment rests on the session and its evidence alone; a
receipt settlement does not rest on concerns or rhythm; everything else rests on
every class it was shown, and an unknown judgment type or class is relevant.
Inside a class only declared bookkeeping is ignored: compare-and-swap revision
counters of objects whose shown content is identical, review times, leases and a
decay curve re-anchored where it already was.

*Tier A* commits the stored proposal with no model call when nobody wrote what it
writes, nothing relevant moved, no owner input arrived, policy, persona,
prompt and configuration are the same and the manifest is still valid. Validity
ends after one ordinary attempt's bound or at the nearest time boundary the
model was shown, whichever comes first: a reuse never accepts more staleness
than a single attempt already accepts, and an open window with two minutes left
is not the window with two hours left that the model judged. *Tier B* asks
DeepSeek one light question, `revalidate_appraisal`: the stored proposal, the
host's conflict list — object, before, after, the fragments of the proposal
resting on it, and the evidence that may be cited now — the latest four public
turns and the clock. Each conflict gets one verdict: `keep`, `adjust` with a
patch confined to the listed fragment, `append`, `correct` or `link` for an
event route only, `wait` to withdraw the fragment, or `replan`. The host refuses
an answer that leaves a conflict unanswered, patches outside its fragment,
patches without `adjust`, uses a route verdict elsewhere, or no longer
validates; a refused answer and `replan` mean the next attempt judges afresh,
with no repair call. Only an entry answered with a standing verdict has its
compare-and-swap expectation reset to the current value. A *full rerun* follows
a change of root evidence, policy, persona or configuration, a spent light
budget, and every conflict that is blocked or unknown: evidence out of bounds,
insufficient authority and a lost lease never reach DeepSeek.

Whatever is reused or revalidated goes through the same commit as any proposal,
where sources, revisions, the lease and idempotency are checked again; a verdict
cannot make superseded evidence valid. Sources the stored proposal was given and
that are still current are merged back into what may be cited, so evidence an
earlier expansion round recalled does not fall out of bounds. A light attempt is
never a charged attempt: a charged attempt remains one full appraisal call. A
light attempt that meets a fresh reusable conflict waits about fifteen seconds
and spends one of two light attempts per stored proposal; anything else hands
the row to a full rerun, so the stage-1 caps still end every row. On the history
lane that is one full call, at most two light attempts and one more full call.
The same predicate guards the commit itself: when the mind revision moved during
the model call, the rebase passes only if nobody wrote the dimensions, wishes or
concerns the proposal writes and nothing this judgment type was shown of the mind
state was written. `manifest_rebase`, `appraisal_reuse` and
`appraisal_revalidation` default to on; all three off is the previous behavior.

Absorbed appraisals settle transitively. Nested `batch_ids` are flattened when a
candidate is absorbed, so a failed candidate's own children are absorbed with
it, and every terminal path settles the whole closure: a completed parent
completes them from its receipt, while a quarantined, superseded or otherwise
non-terminal parent returns them to the queue. A judgment that already holds a
durable commit receipt absorbs nothing that commit never saw, and an
already-integrated batch completes each child with the child's own data. A final
update that matches no row means the worker lost its attempt token; its receipt,
proposal and error are discarded and recorded as the
`appraisal_attempt_discarded` metric, with the attempt's usage where it is known
and its `usage_status` otherwise.

Four properties hold across these paths. The same input is never evaluated twice
while new evidence always wakes exactly one review. An advisory field can no
longer veto an appraisal: a refused section costs that section, not the owner
message it arrived with. Nothing is retried without bound — every lane, wait and
outage has a cap, and the end of a cap is quarantine rather than another charge.
Failure history and usage survive quarantine and resume, and usage that is not
known is recorded as unknown, never as zero.

Idle evaluation has its own persisted schedule. DeepSeek chooses 20–120 minutes;
the initial recovery evaluates current context. A pending independent idea does
not block another verified intent. A pending review of that intent or an
invalidated source still blocks it. Sending retains owner-epoch, source,
deduplication, work-lock, threshold and quiet-hour checks.

New exploration results join the due operational priority queue so their share,
defer or keep decisions do not wait behind an older interaction backlog. They
still respect the active action lease and retry backoff; a failed urgent result
does not stop the next due batch.

## Operator recovery

Three host actions serve an operator here. All are host-only, call no model and
write no memory of their own.

`appraisal-attempts` reads the attempt ledger: one append-only row per attempt,
written when that attempt ends, with its ordinal, attempt token, lane, stimulus,
start and finish, outcome (`committed`, `failed`, `quarantined`, `discarded`,
`abandoned`, `reused` for a stored proposal committed with no model call or
`revalidated` for one committed after a light revalidation), the tier, manifest
digest and verdicts of a light attempt, the classification of the failure, sha256 digests of the proposal
and of the rendered request, and every model call the attempt made — purpose,
model, request id, elapsed time and the usage the provider reported, or an
explicit `usage_status: "unknown"` where it reported none. It takes an optional
`job_id` and a `limit` of 1–200. No proposal body, context body or chat text is
stored, so the ledger is safe to read in full. The attempt that left a row
`running` past its lease is back-filled as `abandoned` with unknown usage by the
next claimer, and an attempt whose token was taken over is recorded as
`discarded`. The queue row's own `error`, `failed_call_receipt`,
`proposed_result` and `receipt` are unchanged: the ledger is history beside
them, never a replacement. Setting `attempt_ledger` to false stops the rows
without changing anything else.

`recover-appraisals` takes 1–50 unique `job_ids`, a `command_id` and a sourced
`source`. It returns quarantined rows of any lane to the queue in one write
transaction, so it needs no worker shutdown: a quarantined row is held by no
worker, and the claim query takes it from there atomically. Each row is judged
afresh — attempts reset to zero, the whole retry budget is restored, and a
stored proposal stays audit data instead of being replayed as a seed: whatever a
conflict had kept for reuse or revalidation (`data.reuse`) is dropped. The
failure moves into `recovery_history` with its attempts, error, `error_detail`,
`repair_reason`, receipts and retry counters. The command is idempotent and
fenced to its own batch: a different job list under the same `command_id` is a
conflict, a row that is not quarantined is refused, and a row that finished
meanwhile is reported under `already_complete` rather than resumed. A resumed
parent still carries the evidence of the children its quarantine released, so
each of those children finds that evidence already integrated and completes
without a model call of its own.

`recover-batched` takes a `command_id` and `workers_stopped`, and is one-shot
and idempotent for that command. It settles the jobs an interrupted parent left
batched. A child is completed only when a really committed ancestor evaluated
its exact current source version: that ancestor's commit receipt and event must
exist, and every source must still be current, lie inside the ancestor's
evaluated manifest and have a committed event of its own. Everything else
returns to the queue for a new judgment, with its reason recorded per row —
`no-committed-ancestor`, `evidence-unresolved`, `source-no-longer-current`,
`outside-ancestor-manifest` or `commit-receipt-missing`. The separate
[`recover-history`](exploration-recovery-continuity.md) path remains the one
that may carry verified replacement proposals for historical enrichment.

## Visibility and deployment

`operational-status` reports queue counts, action success and next review,
enrichment progress, exploration and accepted contact records without messages
or model reasoning. The native runtime separately reports execution, current
model, task review and notification receipts.

Queue counts are grouped by state and lane, so quarantined work is visible as
`needs-repair` for the lane it belongs to. The `read` action returns the most
recent appraisal rows, where a failed or partly refused attempt is legible
without opening a proposal: `error`, `error_detail`, `repair_reason`,
`waiting_reason`, `admission_waits`, `compression_waits`, `compression_stalls`,
`transient_failures` and `preparation_conflicts`, together with the committed `result` — which carries
`rejected_sections` as section, code and host message, `held_sections` as the
section, the part held, how many items and the upstream refusal, `held_decisions`
as plan, step and one of `plan-inactive`, `view-missing`, `step-touched` or
`basis-changed`, and `follow_up_id` when one was armed — and the `receipt`, which
carries `dropped_fields` and any repair receipts. Quarantine emits the
`appraisal_quarantined` metric with the lane, stimulus, reason, error, charged
attempts and retry counters; a lost attempt token emits
`appraisal_attempt_discarded`.

Queue availability, attempt start, lease expiry, last successful result and
collection time are separate fields. A fresh collection does not turn an old
failure into a new one, and an old queued job may have a newly started attempt.
Unknown historical attempt times remain unknown. Action requests omit unused
graph schema definitions as well as graph background.

Mobile health review uses a 64K output ceiling and an eight-minute deadline.
Failures retry after five, fifteen and then fifteen minutes; success restores
the four-hour cycle. A fault identity survives changing timestamps. Host control
replies settle the relevant pending input only after a platform receipt, without
clearing a newer input's wait.

Deploy after synthetic tests and a private replay: back up the database and
mutable host state, verify restoration, stop only verified owned idle processes,
migrate once, then resume the same native conversation. Do not clear a live
task lock manually. Record both the model review and actual switch/send receipts.
An exclusive process file prevents duplicate starters from replacing the active
PID; shutdown removes only the process's own file. Retired standalone channel
failover services remain disabled when shared-session routing replaces them.
Keep a four-hour observation spanning at least two autonomous decisions.
The daily desktop fallback remains separate and proposes improvements for
owner approval.

Rollback disables `operational_lanes` and restores host code.
`appraisal_section_isolation` rolls back separately: setting it to `false`
restores an all-or-nothing appraisal without touching the lanes, and the
per-lane cap, the batched settlement and both recovery actions remain in force
either way. Keep new records and deliveries; do not overwrite an active database
with an older snapshot. Private recordings, reconciliation evidence and
credentials are never published.
