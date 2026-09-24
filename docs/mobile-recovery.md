# Mobile recovery and operational progress

A task Kin has taken on keeps the work lock until she declares how it ended and
the host's facts are in. Terminal execution, Kin's declared outcome and platform
delivery stay separate records. DeepSeek Flash (high effort) classifies messages,
summarizes idle work and reads health, and decides none of them; the host owns
the facts and the serialized provider transition.

## Work and delivery

Completed, failed and canceled tools are terminal. Active tools, unknown
execution, native turns, queued work and unsettled handoffs keep the router busy,
and an open task keeps the work model. A task closes on the outcome Kin declares —
`completed`, `partial`, `declined` or `deferred` — never on a model's verdict; the
declaration and the facts checked before closing are described in
[mobile routing](mobile-routing.md#work-owns-the-conversation). Rejected,
undeliverable, retired, deferred and never-submitted deliveries count as settled;
an unconfirmed or unknown one holds the task only until `reportWaitMinutes`
(default 30) passes, and the closure lists what was left undelivered, unsent or
unknown.

When Kin has taken a task on and has not declared on it for
`workSummaryIdleMinutes` (default 60), DeepSeek Flash writes its facts down on the
background lane: tool counts by status, the owner's requests and follow-ups,
outputs with their states and message IDs, unsent drafts and the last public
reply. The host keeps the summary on the task and states it to Kin in a turn of
its own; it moves no lock, withdraws no draft and decides nothing. A summary
allows 32K output tokens and five minutes. A token ceiling or a malformed answer
fails that attempt, whose usage row keeps the provider receipt, and it is retried
after `workReviewIntervalMinutes` (default 20); a lane that could not be admitted
is asked again after five minutes. Evidence quotes each input and output up to
4,000 characters, a longer one as head and tail with the omitted count stated.

Uploads checkpoint uploading, uploaded, message-submitting and platform-accepted
separately. Only message submission can create an uncertain delivery. Files and
text use a stable UUID; an uncertain submission is reconciled under its original
ID. A failed upload stays failed and is never replaced by another file. File
bytes are checked against their recorded hash before each send; closing a task
reads receipts only.

Owner input has preparing and submitting states. Optional background memory is
not prepared while a native turn is active; the steering path takes the message
into that turn. A pre-submission failure can safely retry the same input ID.
Post-submission ambiguity requires reconciliation; the
[input ledger](mobile-routing.md#the-input-ledger) keeps every input's facts. Chat
follow-ups ride on the task as context and change neither its input version nor
Kin's declaration; new work raises the input version and clears an earlier
declaration. Follow-ups attached to the task reach its facts summary, while an
input whose classification attempts ran out reaches Kin unlabelled and is not
attached.

Owner subscriptions to a switch notification pass to a superseding request of the
same mode and survive restarts with the router state; a pending request of
another mode, such as Kin's `auto` request that declares an outcome, supersedes
it without them. The notification follows actual provider/model/session
verification, has one durable send ID and reconciles unknown receipts.

## Complete phone replies

A reply is a group of bubbles, and a bubble the channel cannot carry in one
message is a group of fragments. `adapters/transport-manifest.mjs` is the only
writer of one durable file per group, beside which it keeps the replaced revision
as `.prev`, quarantined bytes in `quarantine/`, settled groups filed by month in
`done/` and the group leases in `leases/`; `tail/` beside them holds the owner's
literal stops, written by `adapters/reply-tail.mjs`. Four rules hold over
everything below: the final manifest is on disk before the first byte is sent,
and a retry reads it back rather than deciding again; a fragment's state follows
the transport's own receipt, which the transport writes before it submits; the
lease decides who may write, never what was sent; and the memory host hears
about whole bubbles only.

The order is draft, check, manifest, send. `createDraft` persists the group
before anything else happens to it — the text exists, nothing is checked or cut
— and the same bubbles always give the same manifest back. The whole-group
check then freezes each body, its references and its fragment identities, after
which the group is final and later passes only read it. The check calls no
model and nobody rewrites Kin's words: Kin's own `choose_reply` choice of
`silent` or `merged` for the input retires the group under that choice, and a
body that cannot go out as written — empty, carrying a private marker or
envelope, or flagged by the host for repair — is withdrawn with its reason.
A group state is `draft`,
`reviewed`, `sending`, `held`, `blocked-unknown`, `interrupted`, or one of the
terminal `accepted`, `retired`, `partial` and `undeliverable`. A bubble is
`unsent`, `sending`, `accepted`, `unconfirmed`, `rejected`, `undeliverable` or
`canceled`, and its state is derived from its fragments rather than set: any
fragment whose outcome is unknown makes the bubble unconfirmed, all accepted
makes it accepted, a refusal makes it rejected, a failed file makes it
undeliverable.

Identity is stored once and never derived again. A fragment's transport ID comes
from its bubble ID, its index and the hash of its own bytes, and it is written
into the manifest at freeze time, so no later pass can renumber a fragment or
compute a second ID for bytes that were already submitted. A group imported from
the older pending journal keeps its bubble ID as its transport ID, because that
is where its receipts are; groups that had already ended are never imported, so
no old reply is replayed.

Fragments are contiguous offset slices of the frozen text, so joining them
returns it exactly, and the same text and limit always produce the same
fragments. A code fence, a run of non-CJK characters — a word, a URL, a number —
and a grapheme cluster are atoms and are never cut; the whitespace after an atom
belongs to it, so a cut never falls inside or in front of a whitespace run. Among
the cuts that cost no extra fragment the boundary is chosen by quality — a blank
line, then a line break, a sentence end, a clause end, a space, and only then any
other atom boundary — and within the best class the latest one. Size is measured
in the unit the platform actually counts. Nothing is ever shortened to fit: when
one atom alone exceeds the limit, or a cut would leave a fragment of nothing but
whitespace, the complete bubble goes as a single file instead, named from its own
content and with no words of the host's own. A body past the channel's file limit
ends that bubble as undeliverable rather than being truncated.

Receipts decide, in both directions. No receipt at all, or a receipt that proves
nothing was submitted, means the send never began and the fragment returns to
unsent. A receipt showing that the host refused the send itself before
submission, because the turn was superseded, cancels that bubble and every
bubble of the group that has not begun; fragments already accepted stay as
partial evidence. An acceptance must name a platform message; an acceptance without one is
an unknown outcome. A refusal is final for that fragment. Anything else is
unknown, and an unknown fragment moves its group to `blocked-unknown`, which
stops that group and nothing else — every other group keeps sending. A transport
that answers "this ID already has a receipt I cannot resolve" is never read as
"nothing was sent". An unknown fragment is submitted again under its own
transport ID at most once, only while the platform's own deduplication provably
still covers the first attempt — a published window less a safety margin — and
only when the contract the host injects for that channel says a repeated submit
is safe. Neither published contract says so, so by default nothing is resent and
the group waits for a receipt or an operator. Only an acceptance settles a
resend: a refusal of a duplicate says nothing about the first attempt.

Every write is taken under a lease with generation fencing. Each acquisition wins
a new generation through an exclusive directory creation, so a generation has
exactly one owner and works as a fencing token; a write is refused once the lease
is lost, close enough to expiry that it could land after a takeover, or outranked
by a higher generation already on disk. Expiry only frees the lease — it says
nothing about what the previous holder already put on the network. The manifest
that counts is the current file, unless it is unreadable or a lower-generation
write landed over a higher one, in which case `.prev` is. A lease holder repairs
the files and keeps the bad bytes; anyone else only reads. A second entrant to a
group that is being worked on is told it is busy and has changed nothing at all.

The memory host is told about bubbles, never about fragments. Once a bubble is
accepted, left unconfirmed, or ended by a refusal, a failure or a withdrawal, its
side effects run at least once and in order: a dead bubble's content reservation
is released, then one bubble-level delivery event is emitted, carrying the
message ID of every accepted fragment. Both are idempotent on the other side and
each is tried a bounded number of times. A group is moved out of the live set —
filed by month, so the live set stays small and old months can be pruned whole —
only when it is terminal, owes no side effect and owes no words to Kin's next
turn.

What the owner never received of a reply group is owed to Kin's next owner turn.
When a group finishes, every bubble that was withdrawn, refused or undeliverable
is recorded on the manifest as `tail_owed`, unless Kin withdrew it herself with
`silent` or `merged` or its body was empty, and only within 24 hours of the
group being written; a body withdrawn because it could not go out as written is
owed as a fact, without its words. The host adds what is owed to the next owner
message's prompt — whole groups, oldest first, about 8,000 characters at a time —
where Kin decides whether and how to say it; once that prompt has reached the
native session, the obligation ends and the group is filed away. No other model decides
about those words, nothing rewrites them and no count of tries drops them. A
bubble that has begun is always finished before anyone may stop the group. A
transport that stays unavailable ends the group after a bounded number of
failures instead of leaving it pending for ever.

`node adapters/transport-manifest.mjs <command> --dir <manifests>` is the
operator's entry point. It never sends and never prints message text.

| Command | Effect |
| --- | --- |
| `status` | Group, bubble and fragment states, reasons, lease generations and what each group owes Kin's next turn, with no message text |
| `reconcile` | Re-read receipts for one group or all of them, from the directories named by `--receipts`; the exit from `blocked-unknown` once a receipt can tell |
| `resolve` | State what really happened to one fragment nobody can tell about: `--outcome accepted --message-id <id>`, `--outcome rejected`, or `--outcome not-submitted` |
| `retire` | Withdraw the unsent remainder of `--group` at once, under an optional `--reason`; what was sent stays sent |

`resolve` accepts only a fragment whose outcome is genuinely open, and an
acceptance without a platform message ID is refused. `not-submitted` is accepted
only for an isolated single-fragment group whose stored receipt is the host's
own refusal before submission, when the receipts read from `--receipts` show no
submission either. What the operator settles is reported to memory, released
and filed away by the service's next pass, because the tool has no memory host
of its own. The manifest has no switch. How a new owner message stops a group
that is still going out is described in
[mobile routing](mobile-routing.md#the-unsent-rest-of-an-interrupted-reply).

## Durable adapter state

Adapter state files are written through one writer, which gives each attempt a
temporary name no other writer can share and fsyncs before the rename. The files
that must survive a corrupt revision — the router state, the session registry and
the work-summary state — keep the revision each write replaces as `<file>.prev`
and are read through one loader. The loader takes the file, else that `.prev`
copy; a revision it cannot read is moved into a `quarantine/` directory beside
it, never deleted, and reported as a text-free fact that status can show. A file
written under a schema this build does not know is reported to the caller and
left where it is: another version's state is not damage.

What a host does when nothing is left to restore depends on what the file was
for. The router's state is a record of what it accepted, so it is rebuilt: every
input named in the last MiB of the append-only event journal beside the file
comes back `unconfirmed` and marked recovered, a replay of it is refused rather
than submitted a second time, and the watchdog reconciles it by its ID. Open
tasks are not rebuilt, and the restart profile waits only for a busy runtime or
an input still in flight. The session registry refuses to open at all, because
its binding is a fencing token: reopening at generation 1 would let an older
fence pass again. The quarantined bytes stay on disk and restoring them is an
operator step.

The memory event journal keeps its retry notes in a bounded ledger of its own:
one file per event that is still waiting, never more than a fixed number of them,
carrying IDs, counts and times and never message text. Entries whose event is
gone are removed, and only the most recently checked are kept, so losing one
costs its event an earlier retry and nothing else. One unreadable queue file is
moved aside rather than allowed to end ingestion, and an unreadable retry note
only means its event is tried again sooner.

A maintenance restart that puts the model back the way the owner already knows it
changes nothing they can see. The host tracks the model the owner was last told
about — the most recent runtime-status notice the platform accepted, which is the
only durable record of what they heard, since a delivery carries a message ID and
a time but not the model that wrote it. When a restart-initiated switch ends on
that same model, its notice is settled as suppressed under
`restart-restored-known-model` and is never sent. Any other verified change is
announced as usual, and status answers and watch subscriptions are unaffected.

## Independent appraisal progress

The rollback flag is `operational_lanes`, enabled by the `recover-operational`
migration, which runs with the workers stopped, keeps original evidence and
scores, and supersedes pending idle wakeups with one current-state evaluation.

| Lane | Input | Commit |
|---|---|---|
| Action | Complete selected evidence, latest interaction, compact state and habits | Emotion, motives, wishes, concerns, plans, habits, sharing decisions and next review; an enrichment job in the same transaction when the batch carries new material |
| Enrichment | Frozen source batch, work/share candidates and graph references | Memory, associations and coverage; no new emotion or contact |
| Session maintenance | Native context and continuity observations | Session advice only |

Action and enrichment have independent host workers and persistent leases; the
action worker also claims session maintenance, under a lease class of its own.
Commits are serialized SQLite transactions. A heavy failure cannot hold the
action lease. Enrichment runs its own request over the original sources; a
stored seed is used only by a row that still carries one. Compression reuses
completed parts under the frozen source/record revisions. Referenced source
changes require a new snapshot.

Invalid graph fields receive one bounded schema correction. While section
isolation is on, output whose only fault is fields the schema does not define is
stripped host-side, with no model call and no retry; the removed paths stay with
the attempt as `receipt.dropped_fields`. Any other schema fault receives one
bounded repair on the history lane, and on every lane while section isolation is
on. Historical migration cursors keep references to deferred repair jobs rather
than claiming those records were integrated.

Fourteen sections are applied inside a savepoint and a state snapshot each, so
one can be refused alone with its SQL and its in-memory changes undone together:
`habits`, `plan_changes`, `action_decisions`, `procedure_candidates`,
`concerns`, `wishes`, `wish_updates` and `session_advice`, and the audited
`trait_observations`, `trait_decisions`, `self_hypothesis`,
`prediction_outcomes`, `expression_intent` and `next_move`. An explicit table
maps every appraisal field to the sections it rests on; the module does not
import while a field is unregistered. A refusal is recorded as
`rejected_sections` with a static code and literal host text only, never the
proposal, its evidence or any owner words. `habits` is an upstream section, so a
refused owner preference holds what rests on it — explore wishes and resumes,
`execute` on an explore or contact step, plan changes, curiosity — and each held
group is recorded as `held_sections` beside the refusal that caused it. A
refusal that leaves something to ask again, and anything held, arms one bounded
`held-sections` follow-up which carries its own evidence of the refusal; an
audited section's refusal, and anything held behind it, is only recorded. That
follow-up restates only what a commit can refuse or hold; it takes in no pending
event and leaves the source cursor where it is, so the parent's event is neither
scored nor remembered a second time, and it never queues a follow-up of its own.
A preference left out of it stays refused. Invalid advice on a
`session-maintenance` appraisal is repaired once, after which the appraisal
completes with the refusal recorded rather than blocking the lane. The switch is
`appraisal_section_isolation`, default on; an explicit `false` makes the
appraisal all-or-nothing, offers none of the audited sections, drops no unknown
field and makes no extra repair call.

What a commit failure costs and what a later attempt may do with it are decided
by one registry of every static `Conflict` and `Missing` message the commit path
raises, keyed by the message literal and overridden by an explicit code at the
raise site. Each entry states a `kind` — what moved: `semantic` for a property of
the proposal itself, so the same proposal fails the same way however often it is
retried, `runtime` for a version that changed under it, `unknown` for a failure
the host has not classified — and a handling: `reuse`, where a later attempt may
reuse the stored proposal; `block`, which the host decides alone and never sends
back to the model; `terminal`, where no retry can succeed; and `section`, raised
inside an isolated section and refused on its own. An unclassified failure is
treated as the strictest of these and is never reusable. Two conflicts that read
alike are deliberately kept apart: evidence cited from outside this evaluation is
semantic and blocks, while cited evidence that moved is runtime and may be
reused. Missing root evidence is terminal. The registry has no switch: it
classifies, and adds fields to the failure records.

The memory section is not one of those fourteen: without the event there is
nothing to anchor it to, so it is isolated item by item instead. One item whose
failure the conflict registry classifies as `block`, and not as `unknown`, is
undone inside a savepoint of its own and the rest of the section commits; a
version that moved, a terminal conflict and anything unclassified fail the
attempt, and the host never edits an item — it commits it whole or not at all. A
note or a graph node introduces a key other items may name, so dropping one drops
whatever names it, transitively, with the cause's code and a `cascaded_from`. The
record sits beside the refused sections as the section, the item's kind and
position in the assessment the host applied, the static code and the host's
message; the position is never the model's own key, and an item drop is not
restated in the `held-sections` follow-up. Notes and graph nodes are also what
carry a source's content into memory, so a source whose only carrier was dropped
is never written down as organised: it stays out of the semantic source index and
waits in `mind_memory_unorganized` for one more memory-only pass, which the
host's minute review queues as history, so nothing is scored again and no wish is
made. Withheld a second time, it is left for an operator rather than retried
without end. The source cursor advances all the same, because holding it back
would hand an already scored event to a full appraisal a second time and every
later event with it. `memory_items_dropped` counts the drops per static code and
kind, with the withheld and abandoned source counts, and
`memory_sources_abandoned` records what the memory-only pass gave up on. The
switch is `memory_item_isolation`; an explicit `false` makes any fault in the
memory section fail the whole appraisal.

Every lane has one cap on what it pays for. A charged attempt is one real
appraisal call; after four of them, or the same error signature twice in a row,
the row is quarantined instead of paid for again. `max_charged_attempts`
(default 5, settable 1–20 through `configure-memory`) can lower that cap but
never raise it above four. A DeepSeek timeout is charged but never quarantines as
a repeat, because a long context can time out deterministically. A main-session
assessment the host defers — no fork to run it in, or a fork that failed, timed
out or was interrupted — is an admission wait instead, uncharged even when the
fork spent tokens.

Waiting is not attempting, so five kinds of wait are uncharged, and each has a
counter and a backoff of its own:

| Uncharged wait | Why it is not charged | Where it ends |
|---|---|---|
| Admission | No lane admitted the request, or the host deferred a main-session assessment | Nowhere: the row waits behind its own backoff, doubling from 30 seconds to ten minutes |
| Evidence compression | A continuation of one attempt, which keeps the frozen inputs so cached parts keep their keys | `compression-stalled` at the third pass in a row without real progress, meaning newly cached parts, and `compression-passes-exhausted` past twelve passes in all |
| Transient provider failure | Network errors, 5xx, 429, a 200 with an invalid body and a main-session review the host could not confirm leave no usable output | `transient-failures-exhausted` after retries at 1, 2, 4, 8, 16, 30, 30 and 30 minutes |
| Preparation conflict | Something moved while the context was being assembled, before any call | `preparation-conflicts-exhausted` at the fifth |
| Light revalidation | One light question about a proposal that already exists, not a new judgment | two light attempts per stored proposal, then the full rerun that the charged cap bounds |

Quarantine is where those bounds and the charged cap end. It is the
`needs-repair` state, the same on every lane: no further model call is paid for
on that row until an operator resumes or retires it, while its evidence, errors
and receipts stay readable and subsequent batches continue. The host resumes one
kind by itself: a row set aside only because history compaction refused its
commit goes back to the queue when compaction ends. The row records
`repair_reason` with the count or code that ended the budget —
`repeated-failure:<code>`, `charged-attempts-exhausted:<n>`,
`compression-stalled:<n>`, `compression-passes-exhausted:<n>`,
`transient-failures-exhausted:<n>`, `preparation-conflicts-exhausted:<n>`, or
`deterministic-preparation-error:<class>` for a host error raised before any
model call, which is set aside at once — and quarantine rebuilds the frozen
context and releases whatever the row had absorbed. The frozen context survives
only while paid-for compression is tied to it: a preparation conflict, every
charged failure and every quarantine rebuild it, because that snapshot is what
went stale.

A terminal conflict ends a row instead of retrying it. The evidence the row was
enqueued for is gone, or for an exploration result has changed, or it was
already appraised: the row finishes in the terminal `superseded` state with its
code in `error_detail` and releases whatever it had absorbed. The one terminal
conflict that is not a failure is a judgment another attempt already committed —
the row then completes from that durable receipt, with no second model call and
nothing scored again.

A failure keeps structured facts for the next attempt. `error_detail` holds the
exception class, the host's own static code and message, the conflicting object
and that `kind`. `Conflict` and `Missing` carry optional `kind`, `code`,
`target`, `expected` and `actual`; a runtime conflict whose `actual` moved is
progress, not the same error twice, and the failure signature counts it as
such. Host error output and the API's 409 body carry `code` and `kind` beside
their other fields. None of them ever holds payload text or a validation repr.
The next attempt is told as data, in `previous_attempt`, why the host refused
the previous proposal: the failure's class, static code and message, plus the
refused sections, held sections, held decisions and dropped fields. An operator
resume clears the failure; refused or held sections and dropped fields already
recorded on the row stay with it.

A commit conflict does not have to cost a whole new judgment. While it builds
the context, the host records an input manifest of what that attempt is shown:
the judgment type and lane, when it was built and until when it is valid,
digests of the prompt, schema, model parameters and projected context, policy
and persona versions, the latest owner input, the clock, the time-derived values
and the time boundaries shown, root evidence and every citable source as source,
hash and revision, and per class of input the identifiers, revisions and
enumerated states of what was shown — dimensions, wishes, concerns, sharing
decisions, rhythm, the recorded plan view, methods, preferences, graph, topic,
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
counters of objects whose shown content is identical, review times and a decay
curve re-anchored where it already was.

*Tier A* commits the stored proposal with no model call when nobody wrote what it
writes, nothing relevant moved, no owner input arrived, policy, persona,
prompt and configuration are the same and the manifest is still valid. Validity
ends after one ordinary attempt's bound or at the nearest time boundary the
model was shown, whichever comes first: a reuse never accepts more staleness
than a single attempt already accepts, and an open window with two minutes left
is not the window with two hours left that the model judged. *Tier B* asks the
lane's model — DeepSeek, or the main-session fork on the action lane when
`main_session_review` is on — one light question, `revalidate_appraisal`: the
stored proposal, the host's conflict list — object, before, after, the fragments
of the proposal resting on it, and the evidence that may currently be cited — the
latest four public turns and the clock. Each conflict gets one verdict: `keep`,
`adjust` with a patch confined to the listed fragment, `append`, `correct` or
`link` for an event route only, `wait` to withdraw the fragment, or `replan`. The
host refuses an answer that leaves a conflict unanswered, patches outside its
fragment, patches without `adjust`, uses a route verdict elsewhere, or does not
validate; a refused answer and `replan` mean the next attempt judges afresh,
with no repair call. Only an entry answered with a standing verdict has its
compare-and-swap expectation reset to the current value. A *full rerun* follows
a change of root evidence, policy, persona or configuration, a spent light
budget, and every conflict that is blocked or unknown: evidence out of bounds,
insufficient authority and a lost lease never reach a model.

Whatever is reused or revalidated goes through the same commit as any proposal,
where sources, revisions, the lease and idempotency are checked again; a verdict
cannot make superseded evidence valid. Sources the stored proposal was given and
that are still current are merged back into what may be cited, so evidence an
earlier expansion round recalled does not fall out of bounds. A light attempt
that meets a fresh reusable conflict waits about fifteen seconds and spends one
of the two light attempts above; anything else hands the row to a full rerun, so
the charged cap ends every row. The same predicate guards the commit itself:
when the mind revision moved during the model call, the rebase passes only if
nobody wrote the dimensions, wishes or concerns the proposal writes and nothing
this judgment type was shown of the mind state was written. The switches are
`manifest_rebase` for the rebase, `appraisal_reuse` for Tier A and
`appraisal_revalidation` for Tier B, each independent of the others.

Absorbed appraisals settle transitively. Nested `batch_ids` are flattened when a
candidate is absorbed, so a failed candidate's own children are absorbed with
it, and every terminal path settles the whole closure: a completed parent
completes them from its receipt, a quarantined or superseded parent returns them
to the queue, and a parent that goes back to pending keeps them for its next
attempt. A judgment that already holds a durable commit receipt absorbs nothing
that commit never saw, and an already-integrated batch completes each child with
the child's own data. A final update that matches no row means the worker lost
its attempt token; its receipt, proposal and error are discarded and recorded as
the `appraisal_attempt_discarded` metric, with the attempt's usage where it is
known and its `usage_status` otherwise.

Three properties hold across these paths. An advisory field cannot veto an
appraisal: a refused section costs that section, not the owner message it
arrived with, and a blocked memory item costs that item. Charged attempts,
provider outages, compression and preparation conflicts all have a cap, and the
end of a cap is quarantine rather than another charge; only an admission wait
has no end. Failure history and usage survive quarantine and resume, and usage
that is not known is recorded as unknown, never as zero.

Idle evaluation has its own persisted schedule. The action-lane model chooses the
next review between ten minutes and a day, and the host clamps it to that range;
an idle review that ends without a commit is rescheduled at the last interval, or
an hour. The initial recovery evaluates current context. A pending independent
idea does not block another verified intent. A pending review of that intent or
a stale source blocks it. Sending keeps the core's owner-epoch, source-freshness
and deduplication checks and the host's work and quiet-hour gates; the score
threshold applies only with `legacy_drive_thresholds`.

New exploration results join the due operational priority queue so their share,
defer or keep decisions do not wait behind an older interaction backlog. They
respect the active action lease and retry backoff; a failed urgent result does
not stop the next due batch.

## Operator recovery

Three host actions serve an operator here. All are host-only, call no model and
write no memory of their own.

`appraisal-attempts` reads the attempt ledger, `mind_appraisal_attempts`: one
append-only row per attempt, written when that attempt ends, so a process that
dies mid-attempt leaves no half-written record. A row holds the ordinal, attempt
token, lane, stimulus, start and finish, whether the attempt was charged, the
outcome, the tier, manifest digest and verdicts of a light attempt, the failure
with its classification, the waiting or repair reason, sha256 digests of the
proposal and of the rendered request, and every model call the attempt made —
purpose, model, request id, elapsed time and the usage the provider reported, or
an explicit `usage_status: "unknown"` where it reported none. The row's own
`usage_status` states whether every call, some or none of them reported usage. It
takes an optional `job_id` and a `limit` of 1–200. No proposal body, context body
or chat text is stored, so the ledger is safe to read in full.

The outcomes are `committed`; `reused` for a stored proposal committed with no
model call and `revalidated` for one committed after a light revalidation;
`failed`; `quarantined`; `discarded` for an attempt whose result was thrown
away, because its token was taken over or because the row had already been
completed from another attempt's receipt; and `abandoned` for the attempt that
left a row `running` past its lease, which the next claimer back-fills with
unknown usage. The ledger leaves the queue row's own `error`,
`failed_call_receipt`, `proposed_result` and `receipt` in place: it is history
beside them, never a replacement. Setting `attempt_ledger` to false stops the
rows without changing anything else.

`recover-appraisals` takes 1–50 unique `job_ids`, a `command_id`, a sourced
`source` and an optional `retire`. It acts on quarantined rows of any lane in one
write transaction, so it needs no worker shutdown: a quarantined row is held by
no worker, and the claim query takes it from there atomically. Without `retire`
each row returns to the queue and is judged afresh — attempts reset to zero, the
retry counters other than the preparation-conflict count are cleared, and a
stored proposal stays audit data instead of being replayed as a seed: whatever a
conflict had kept for reuse or revalidation (`data.reuse`) is dropped. With
`retire` each row ends as `superseded` instead, for a moment that has passed or
evidence that is gone, and releases whatever it had absorbed. Either way the
failure moves into `recovery_history` with its attempts, error, `error_detail`,
`repair_reason`, proposal, receipts and retry counters. The command is
idempotent and fenced to its own batch: a different job list under the same
`command_id` is a conflict, a row that is not quarantined is refused, and a row
that finished meanwhile is reported under `already_complete`.

`recover-batched` takes a `command_id` and the legacy `workers_stopped`, and is
one-shot and idempotent for that command. It settles the jobs an interrupted parent left
batched. A child is completed only when a really committed ancestor evaluated
its exact current source version: that ancestor's commit receipt and event must
exist, and every source must still be current, lie inside the ancestor's
evaluated manifest and have a committed event of its own. Everything else
returns to the queue for a new judgment, with its reason recorded per row —
`no-committed-ancestor`, `evidence-unresolved`, `source-no-longer-current`,
`outside-ancestor-manifest` or `commit-receipt-missing`. The separate
[`recover-history`](exploration-recovery-continuity.md) path resumes quarantined
historical-enrichment rows, or with `admission_only` only those that waited on
admission; it refuses replacement proposals, and each row is judged afresh.

## What proves the workers stopped

A recovery command takes the caller's `workers_stopped`, but while
`liveness_checks` is on the parameter decides nothing: the lease ledger and the
process table answer instead.

`recover-operational` releases every `running` appraisal and `recover-batched`
settles children that a parent still under lease might yet commit, so both
refuse — `worker-lease-fresh` — while any `running` appraisal holds an unexpired
lease. `plan-recover` returns only the executions whose `lease_until` has passed
and reports the rest under `still_leased` instead of interrupting them.
`recover-history` and `recover-appraisals` touch quarantined rows that no worker
can hold, and so ask for nothing at all.

A start-up `recover` interrupts a running exploration only when its worker is
provably gone. Each exploration records the pid that claimed it, that process's
start time and a deadline of its own budget plus an hour of settlement, inside
the row's `data`. The restart interrupts a row only when that pid is gone, when
the number belongs to a process that started at another time, or when the
deadline has passed; anything it cannot establish — no `ps` to ask, a row without
these records — stays running and is reported under `live_explorations`. Every
check fails closed in that same direction: unreadable evidence means the work is
alive.

Compaction and other whole-store operations ask for more than that. Quiescence
means all of: at least one pid file is configured, and each is absent, holds no
pid, or names a process that is gone or, where a command is configured, does not
run it; `status.json` is `stopped` or its heartbeat is over 45 s old; no
unexpired lease in any of the five lease tables (`mind_appraisals`,
`mind_plan_runs`, `mind_model_leases`, `mind_foreground_leases`, `jobs`); no live
exploration; and a `BEGIN EXCLUSIVE` probe that succeeds. The probe is attempted
only once every other clause is quiet, so a running host is recognised without
ever reaching for the write lock. The pid and status files belong to the host, so
their paths come from its configuration:

```json
"liveness": {
  "processes": [{"name": "host", "pid_file": "PRIVATE_STATE/bridge.pid", "command": "service.mjs"},
                {"name": "memory-service", "pid_file": "PRIVATE_ROOT/service.pid", "command": "memory_service.py"}],
  "status_file": "PRIVATE_STATE/status.json"
}
```

A path that is not configured, a missing status file and any file that cannot be
read are missing evidence, and missing evidence is never quiet; an absent pid
file claims no process. With `liveness_checks` set to false the boolean alone
decides for all four commands, and a start-up `recover` interrupts every running
exploration.

## Visibility and deployment

`operational-status` reports queue counts, action success and next review,
enrichment progress, exploration and accepted contact records without messages
or model reasoning, quarantined appraisals by count, oldest availability and
reason, and a `model_lanes` block with each
[lane](kin-mind.md#model-lanes-and-the-lease-interface), its limit, the source of
that limit and its current holders by label, age and time to expiry. The native
runtime separately reports execution, current model, open tasks with any
declared outcome, and notification receipts.

Queue counts are grouped by state and lane, so quarantined work is visible as
`needs-repair` for the lane it belongs to. The `read` action returns the most
recent appraisal rows, where a failed, reused or partly refused attempt is
legible without opening a proposal: `error`, `error_detail` with its `kind` and
`code`, `repair_reason`, `waiting_reason`, `last_wait_at`, the uncharged
counters `admission_waits`, `compression_waits`, `compression_stalls`,
`transient_failures`, `preparation_conflicts` and `light_attempts`, `tier` for a
row committed from a stored proposal and `completed_from` for one completed from
another attempt's receipt, together with the committed `result` — which carries
`rejected_sections` as section, code and host message, and a dropped memory item
as its kind and position, `held_sections` as the section, the part held, how
many items and the upstream refusal, `held_decisions`
as plan, step and one of `plan-inactive`, `view-missing`, `step-touched` or
`basis-changed`, and `follow_up_id` when one was armed — and the `receipt`, which
carries `dropped_fields` and any repair receipts. Quarantine emits the
`appraisal_quarantined` metric with the lane, stimulus, reason, error, charged
attempts and retry counters; a lost attempt token emits
`appraisal_attempt_discarded`; each attempt that finds a stored proposal emits
`appraisal_tier` with the tier it chose — `A`, `B` or `full` — and the reasons
for it, and a manifest the host could not record emits
`appraisal_manifest_failed` with the exception class alone, the attempt
continuing without one.

Queue availability, attempt start, lease expiry, last successful result and
collection time are separate fields. A fresh collection does not turn an old
failure into a new one, and an old queued job may have a newly started attempt.
An attempt time that was never recorded is reported as unknown. Action requests
omit unused graph schema definitions as well as graph background.

Mobile health review uses a 64K output ceiling and an eight-minute deadline. A
failed review retries after five and then fifteen minutes, then waits its
ordinary interval, four hours by default; a run no lane admitted retries in five
minutes and is not counted as a failure. A fault identity survives changing
timestamps. A control notice counts as delivered only on an accepted platform
receipt, and recording it clears no input's reply wait; the control input itself
is settled when the router accepts the control.

Deploy after synthetic tests and a private replay: back up the database and
mutable host state, verify restoration, stop only verified owned idle processes,
migrate once, then resume the same native conversation. Do not clear a live
task lock manually. Record the actual switch and send receipts. An exclusive
process file prevents duplicate starters from replacing the active PID; shutdown
removes only the process's own file. Retired standalone channel failover
services stay disabled when shared-session routing replaces them. Keep a
four-hour observation spanning at least two autonomous decisions. The daily
check is separate: the launchd job `local.kin.daily-check` runs kin-check and the
unattended runtime upgrade, and the read-only Codex automation `kin` writes the
improvements it finds as plans awaiting owner approval.

Rollback disables `operational_lanes` and restores host code.
`appraisal_section_isolation` rolls back separately: `false` makes the appraisal
all-or-nothing and offers none of the audited sections, without touching the
lanes, and the per-lane cap, the batched settlement and both recovery actions
stay in force either way. The conflict-handling switches on this page roll back
the same way, one part at a time and none of them touching another; the
[switch registry](memory-lifecycle.md#feature-switches-and-rollout) lists them
with the tables each one writes. The conflict taxonomy and the terminal endings
it decides have no switch and stay in force. Keep new records, tables and
deliveries; do not overwrite an active database with an older snapshot. Private
recordings, reconciliation evidence and credentials are never published.
