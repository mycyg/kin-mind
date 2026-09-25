# Persistent mobile model routing

The mobile host keeps one native conversation across channels and model providers.
DeepSeek Flash / high handles conversation and lightweight tool use. The default
work profile is GPT-6 Sol / medium with Fast preference, for substantive creative
work, research, documents, code and computer tasks. These are the router's built-in
profiles; the private host passes its own from the optional `routingProfiles` in
`mobile-router-config.json`, each field overriding the built-in one. Routing selects
a conversation mode, not an independent model for every message. Deployment-specific
identities, credentials, state files and personas remain private. The private host
loads these public adapters from the one core checkout its static imports name
(`coreAdapter` in its `kin-paths`); no configuration points it at another copy.

## Work owns the conversation

`MobileRouter` persists an input ledger, work tasks, mode requests, configuration
revisions and transition history. Input acceptance and provider replacement share
one mutex. The classifier is asked outside it; a dispatch is planned under it,
prepared outside it and marked submitted under it again only while nothing it was
planned on has changed, otherwise it is planned again. A planned dispatch reserves
the provider until it reaches the session. New work received during a DeepSeek turn
waits for that turn to finish. DeepSeek classifies natural language using the
current message, recent conversation, mode and task summary, including during work.
Its result describes intent; the host independently preserves the active work
profile unless the owner explicitly changes it. Runtime enquiries and notification
requests do not create work or touch a declared outcome. Literal commands — the
owner's stop, `/mode work`, `/mode auto` and `/compact` — have their own path;
natural phrasing is not matched by a collection of regular expressions.

A classification that fails decides nothing: no task is opened, no provider switch
happens and no execution is granted. The input waits as itself in the durable
`semantic-pending` state, with its version basis, the tasks and model at capture and
the failure class (`timeout`, `http`, `parse` or `unavailable`), and the same input
is asked of DeepSeek again: four attempts in all, each retry allowed twice the
classifier timeout (at most 45 seconds), with a growing wait between them. A late
answer routes against the state it finds. A stop it reads applies only to the tasks
it was read against, and late work never extends a task the owner is stopping; it
opens work of its own. An input whose session moved to a new generation meanwhile
is read again there. When the attempts are spent the message still reaches Kin,
unlabelled, as chat on the current profile, rather than ending unanswered.

A `work` label only proposes a task. The proposal keeps its own turn on the work
profile and lapses when that turn ends, unless Kin takes it on
(`request_mobile_mode` with `task_outcome=accepted`, the task ID and its input
version), declares an outcome for it or declines it; a lapsed proposal can still be
taken on while no other task is open. Kin's own handoffs and the host's internal
jobs are commitments from the start. An open task holds the work lock: later
messages ride on it, new work raising its input version and clearing an earlier
declaration, chat as context that changes neither. A mode request Kin makes takes
effect at an idle boundary.

Work closes on the outcome Kin declares, never on a model's verdict: `completed`,
`partial`, `declined` or `deferred`, requested with `mode=auto`, the task ID and its
current input version. A decline belongs to the task's current native turn. A
deferral names a `not_before` in the future and within 30 days; the task closes as
`deferred`, releasing the lock, and the mind's `plan-deferral` action turns it into
Kin's own plan for that time, or the deferral records `needs-kin` and why. Before
closing, the host checks only facts: the declaring turn ended, for whatever stop
reason or by an owner interruption; the task's tools are terminal; no delivery is in
flight; and the report reached the platform after the declaration, or
`reportWaitMinutes` passed, which is recorded as it is together with what was left
undelivered, unsent or unknown. While the native runtime is unknown nothing closes.
An owner stop cancels the task once execution has stopped. The exit request stays
pending while work is open, and the automatic profile returns once the last task
closes. A platform receipt is not a read receipt.

The host's MCP interface provides live runtime inspection, including the facts of
each open task, and asynchronous mode requests; a request is credited to the input
of the native turn it came from, or to nobody. A model requesting a handoff ends its
turn; the host resumes the same native thread and continues the retained task. It
must not synchronously wait for the provider restart inside the requesting tool
call.

The phone session runs no Codex hooks. The host records the owner's words itself
when it takes a message in; internal prompts, answers and tool events stay in the
native transcript and host journal without becoming new interpersonal evidence.

## The input ledger

Every input keeps its facts under its original ID: classification, preparation, the
native queue, submission, the native turn that carried it, what of its reply reached
the platform and any notice about it. `adapters/input-ledger.mjs` is the one reading
of those facts. It sorts owner inputs into eight summary states: `received` (in the
inbox, or proven never to have reached the native session), `classifying`, `queued`
(taken by the router, not yet in the native session), `submitted`, `answered`,
`failed-notified`, `superseded` and `canceled-by-owner`. Records that predate the
ledger count apart as `historical` and are never run again, and the host's own turns
are summarised on their own. An owner input is answered when its whole reply
reached the platform, when Kin chose not to reply or merged it into another answer,
or when a reply group names it among the inputs its reply answered when it was formed
(`answeredInputIds`) — sharing a native turn with a reply is not enough; a control or
maintenance command is answered by being applied. An answer outranks a notice. An
internal input settles when it leaves the native session, unless its submission is
still to be looked up by its own ID.

Every owner input ends in one of three ways: a reply, a retry under its original ID,
or one system notice to the owner. The router's `watch()`, which the host runs every
minute, decides from evidence alone:

- An input proven never submitted — its preparation failed or was withdrawn, it
  reached only the host's in-memory queue, or reconciliation proved it absent — goes
  back to the inbox under the same ID after 30 seconds, 2 minutes and 10 minutes, at
  most three times across all its attempts — those of its intake and of its routing
  counted together — never while dispatch is frozen; after
  that it is told as `stopped`. A failed model control of the owner's is reported by
  its own mode notice instead. What the owner's stop withdrew before it was submitted
  is canceled by her, never retried.
- A submission whose outcome is uncertain is looked up by its original ID through
  the owned ACP's `_kin/input-status`, on the thread the ledger recorded it was
  submitted to. `found` accepts it. `not-found` proves it never arrived, and it is
  retried, only when all three hold: the runtime it was submitted on declared
  `inputCorrelation` (recorded with the submission), the answer is `complete: true`,
  and the thread the answer read is the one it was submitted to. Any other answer —
  a bare `not-found` from an older runtime, an input with no recorded submission, a
  thread a migration replaced — leaves it `unknown`: looked up again, told as
  `unknown`, never submitted again. Until it is found or told, it holds the session's
  boundary: native maintenance waits, and so do the mind's own contacts. One the
  owner's stop has settled holds it no more, since the stop is its outcome, whether
  it was stopped live or came back canceled from the journal; its submission stays on
  record, and a native turn, a tool or a send of it still counts on its own.
- An accepted input with no reply, no running turn and no progress for
  `inputStuckMinutes` while the session is idle is told as `partial` when part of its
  reply reached the platform, and as `unknown` otherwise.

Notices go out one at a time, the oldest input first, and never two within
`noticeGapMinutes`. `bridge.notifyOwner(kind, inputId)` sends each as the host's own
labelled system notice, with fixed words chosen by the facts, never in Kin's voice
and never remembered as her words, under a transport ID derived from the input and
the kind: an attempt already begun is only looked up, never sent twice, except one
the platform refused outright, which goes again under the same ID after 10 minutes,
1 hour and 6 hours. The input becomes `failed-notified` only once that notice has a
platform receipt; sends and lookups are bounded. A dispatch waits for the
coordinator, a switch or a classification at most `dispatchWaitMinutes`; past that
nothing has been submitted, and the same ID is retried or reported like any other
unsubmitted input. The watchdog holds a dispatch whose preparation hangs to the same
deadline and refuses its late submission, and an input still waiting before
submission past `inputStuckMinutes` is told as `stopped`. An input selected but never
handed to the session within `workReviewIntervalMinutes` is unsubmitted too.

At shutdown the host stops taking input first: WeChat polling ends and the inbox
takes nothing new. It waits up to 10 seconds for inputs the inbox already took, then
closes routing, the mind, the exchange and the session. An input still running then
stays in the inbox's `processing` directory, and the next start decides it by the
router's facts: back to the inbox only on evidence it never reached the native
session, filed when it was accepted, and otherwise held for reconciliation by its own
ID. A batch's members follow its first input.

A release or migration freezes dispatch through the host's authenticated loopback
control endpoint. `POST /freeze` with a `reason` and an optional `migrationId` and
`ttlMs` stops new dispatch — both channels' inboxes, the mind's own turns (which
check `router.frozen()`), handoff continuations and mode changes nobody forced —
while the owner's literal stop and her `/mode work` and `/mode auto` still act. The
freeze survives a restart and lifts itself after `ttlMs`, two hours by default and
between one minute and twelve hours, so a release lost half way cannot leave the
owner unanswered for good. Asking again with the same reason and migration ID changes
nothing, so a caller may poll it; every answer says whether the host is `idle` and,
if not, why. `POST /thaw` lifts it. `GET /busy` answers the same drain: inputs still
moving through the host, a native turn or an unreadable native runtime, a send, a
native command or a switch hold it, and so do the host's own background model calls —
a summary of open work, a health reading, a classification asked again — until the
call has ended and let go of its lane; a freeze refuses a new one before anything is
counted for it, and none of them holds the owner's conversation. A classification's
deadline covers the lane's admission as well as the request: once it passes, the
request is canceled, a lease that comes late starts nothing, and the classification
answers only after the call has let go of everything it held. Open work, a desktop
handoff, history and inputs waiting for a notice or a reconciliation do not, because
they do not change by waiting. `GET /unsettled` returns the eight-state summary and
every unsettled input by its original ID, naming inbox jobs the router has not taken
by ID and file time only. The same summary is `inputLedger` in `/runtime`.
`POST /handoff-source` answers from the same ledger whether an owner input may
authorize a desktop handoff; see [task exchange](task-exchange.md#host-integration).

The router's state file keeps what is unsettled and a bounded recent tail. Settled
inputs, closed tasks, finished requests and settled notices beyond it move, oldest
first, into `archive/router-YYYY-MM.jsonl` beside the state file (the private host's
`state/archive/`), filed by month and never deleted; whatever is open, pending or
still referenced stays. An ID index answers a replay of an archived input as a
duplicate instead of running it again, and the append-only event journal moves to
the same directory past 8 MiB.

When neither the state file nor its previous revision can be read, the ledger is
rebuilt from the tail of that journal: each input comes back by its ID and kind as
`unconfirmed`, and nothing is done with it until the watchdog has looked it up by its
ID. The owner's stop is the exception, because it always stands. Every event that
records it names the stop and how far the input had got — `preparation`,
`host-queue`, `prompt-start` or `native-session` — and an input the journal says she
stopped comes back canceled by that same stop under its own ID. It is never looked up,
requeued, routed again or told about; a replay of it is answered as canceled (or, if
it had reached the native session, as a duplicate).

## Runtime controls and native maintenance

The classifier returns `chat`, `work` or `control`. Control intentions are `status`,
`watch`, `work` (enter work mode), `auto` (request automatic routing) and `manual` (a
named model, effort and tier). The host answers status requests from verified
runtime metadata. Asking for a switch notification attaches to the pending request,
or is answered as a status request when none is pending; it does not start a
work-model task. A mixed message that also requests code, a document or a repair
remains work.

The owner's stop needs no model when it is the literal command: it is read before
anything else, answers when no classifier can and applies to every open task. A stop
read from natural language counts only when the classifier actually answered, and
applies only to the tasks it was read against. Either way it is recorded on the stop
input itself, cancels the tasks it names once execution has stopped, and never opens
or extends work. What of the stopped work waits in the host's in-memory queue, its
prompt not begun, leaves that queue at once by its original input IDs and is canceled
by her under those IDs; it is never requeued or sent under another ID. Nothing else in
the queue moves, whatever arrived after the stop included. The stop is read again where
any prompt would begin, in the step that records its start: a prompt that carries
stopped work never begins, and a message merged into it after the stop is not
submitted and goes again under its own ID.

In manual mode, ordinary messages retain the selected model, effort and Fast
preference. The classifier still reads content, recall and stop/file intentions, but
does not select a model for those messages. Only a fresh control in the current owner
text changes the profile or routing mode; an old control in the task summary or
recent conversation is context, not renewed authority. A manual profile comes only
from the owner's own control or a verified reclassification, never from a model or
maintenance request. Partial effort/tier changes retain the other current fields. An
already verified, identical manual profile settles without canceling the native turn,
fencing tools or notifying another switch. An uncertain prior switch still reconciles
through its original receipt. Closing a task retains a manual profile; returning to
automatic routing requires the owner's explicit request.

Historical correction is a separate receipt, not a rewrite of the input record. An
authenticated private host can call `reclassifyAcceptedControl` only for an accepted
owner input no older than `reclassifyMaxHours`, with its semantic hash, the basis it
read, a bounded current-classifier result and hashed acceptance/owner/source evidence
tied to the same session, conversation and generation. The basis is the configuration
revision and the owner inputs after the source, not every bookkeeping save. Only
manual, auto or work control is accepted. The receipt leaves `record.route` untouched
and authorizes one exact mode request; it never submits the source input or replays
its task effects. An identical retry returns that receipt; a changed command ID
conflicts, and a newer explicit control or reclassification makes the evidence stale.

A mode request can set `notify: true`. After native model verification, the host
creates a durable notification with a stable output ID. The owner-bound sender and
its durable outbox are injected through `flushNotices({send, lookup})`. Successful
delivery requires a platform message ID; an `accepted` answer without one is not
believed. A receipt proves one of three things: `accepted`; `rejected`, which is
terminal; or not submitted — no outbox record, or one that proves nothing reached the
platform — which sends the same ID again under a bounded budget that survives
restarts. Anything else leaves the submission unknown: the original ID is only looked
up, never resent, and after a bounded number of lookups the notice stops polling as
`unresolved`. Exhaustion is a state with a reason, never permanent polling. A send
starts only past the activity gate; if the ledger cannot be written after that, the
permit is let go and the notice waits, not submitted, under its own ID. A settle
that would change nothing writes nothing. Notification receipts are separate from
task delivery receipts. Host integrations must guard provider changes for the
duration of the send and must bypass task-delivery accounting for these non-task
messages.

`actual` is the fresh native model result. `lastTransition` is a historical record
with `matchesCurrentModel`; `transition` remains a compatibility alias. Every host
switch, including verification probes, records its origin and verified result. An
independently observed model change gets its own transition record.

The owner's literal `/compact` is native maintenance with an operation receipt of its
own. `compactPrompt` replaces the prompt with the bare command; the operation's start
and end reach the router through `observeOperation` and never complete an unrelated
task. An operation that ends its turn other than with `end_turn`, whose submission
outcome is unknown or that a restart cut off is closed as failed or interrupted:
never replayed, and never left counting as running work.

`POST /configure` changes the settings below with a command ID, a reason and the
configuration revision it read. A command repeated with the same content returns its
receipt, which is credited to the native turn the call came from, or to maintenance.
The model-side `configure_mobile_routing` tool reaches the first three.

| Setting | Default | Range | What it bounds |
| --- | --- | --- | --- |
| `classifierTimeoutMs` | 15000 | 1000–15000 | One classification attempt; a retry may wait twice as long, at most 45 s |
| `auditIntervalHours` | 4 | 1–168 | The interval between health readings |
| `workReviewIntervalMinutes` | 20 | 1–1440 | The retry after a failed work summary; how long a selected input may wait with no live dispatch |
| `workSummaryIdleMinutes` | 60 | 10–1440 | How long a task Kin took on stays quiet, undeclared, before its facts are summarized |
| `inputStuckMinutes` | 10 | 1–240 | How long an input may show no progress before the watchdog acts on it |
| `noticeGapMinutes` | 10 | 1–240 | The least time between two owner notices |
| `dispatchWaitMinutes` | 20 | 1–120 | How long a dispatch waits for the coordinator, a switch or a classification |
| `reportWaitMinutes` | 30 | 1–1440 | How long a declared outcome waits for its report to reach the platform |
| `reclassifyMaxHours` | 24 | 1–168 | The oldest accepted owner message that may be reclassified as a control |
| `handoffSourceMaxHours` | 24 | 1–168 | The oldest owner input that may authorize a desktop handoff |

## The unsent rest of an interrupted reply

When the owner writes again, or says stop, while a reply is still going out, the
group stops at its next bubble boundary: a bubble that has begun always goes out as
written, and what has not begun is withdrawn and never sent by the host. A new
message does not stop a reply written in work mode; the owner's stop does. Before an
owner message is submitted, the host waits a bounded time for groups that are
sending to reach such a boundary.

Those words, and the words of any bubble the transport could not deliver, are owed to
Kin. They ride on her next owner turn as a host note — whole groups, oldest first,
within a bounded size, the rest waiting for the turn after — and she decides whether
and how to say them. No other model is asked, the host rewrites nothing, nothing is
dropped unseen and there is no count of tries. A reply that could not go out as
written (a private marker, a broken envelope, an unfinished turn) is owed as a fact,
without its words. Kin's own choice to stay silent or merge owes nothing, and neither
does a group that settles more than a day after it was written. The obligation ends
once a turn that carried the words was taken into the native session — as a new turn,
a merge before it began or a steer — and the groups are then filed away. A group with
a fragment whose outcome is unknown is never withdrawn: it blocks itself and nothing
else.

The owner's literal stop is written down before anything else, so a restart cannot
lose it, and needs no model: it withdraws what was written before it. The router's
reply-tail port carries only that stop (`stopped`); a port that is absent, slow to
answer or failing changes nothing about the route. Tail facts reported for status
carry states and identifiers only, never message text. The manifest, its fragments
and the operator commands are described in
[mobile recovery](mobile-recovery.md#complete-phone-replies).

## Provider compatibility

`codex-models.mjs` changes the ACP provider and model, then verifies native thread
identity, model, provider and reasoning settings. `codex-runtime-patch.mjs` adds a
bounded runtime query and rejects provider changes while native execution or
background terminals remain active. An unsupported adapter version fails closed. The
pinned runtime bundle carries `codex`, `codex-code-mode-host` and, when the vendor
ships them, its shell resources from the same release; a build without a required
executable fails at once, and rolling back to an older bundle does not give it a
component it never had.

At startup the private host retrieves the current provider catalogue through Codex
`model/list` and its native metadata cache, then combines it with the explicit
DeepSeek gateway contract. The last successful mobile catalogue remains usable when
discovery is unavailable. `read_mobile_runtime` returns the available models and
default profiles; a refresh does not change the current manual profile. The pinned
executable and companion instructions stay independent of model catalogue updates.

Global desktop model configuration and account credentials are not rewritten. Work
uses the selected profile's Fast preference; an actual service tier counts as
verified only when the native runtime says so. DeepSeek conversation turns carry the
effort the session asks for — the chat profile's, or the owner's manual choice — and
the gateway's configured effort fills in only when a request names none it knows.
Classification, the work summary and the health reading enable thinking at high
effort, and their receipts record it. Private reasoning never enters the channel
output.

`deepseek-gateway.mjs` binds an authenticated loopback endpoint and forwards only
DeepSeek Flash Responses requests to the official HTTPS endpoint. It excludes
provider-specific reasoning payloads from cross-provider input and output while
preserving user messages, assistant answers, function calls and tool results.
Trusted developer instructions map to the provider's supported system role. The
gateway adds a stable instruction to address the user without narrating response
planning. This is a generation constraint: prose mislabeled by the provider as a
final answer cannot be identified by a channel filter alone. This addresses
reasoning content accepted by one provider but rejected by another; it does not
rewrite an existing native transcript. Provider keys never appear in the loopback
client token or diagnostic errors.

Classification uses the current message, a bounded recent conversation and task
summary. If the evidence does not establish that the owner wants substantive work,
the classifier keeps the message in chat so the conversation can clarify. With
intents on (`classifyIntents`, the private host's default) the same call reads
whether the owner wants the running task stopped, whether she asks for a file and the
words she uses for herself and for Kin, and an owner message with attachments is
classified like any other, seeing only each attachment's kind, type, name and size;
with intents off, attachments make a message work. A bounded timeout (15 seconds by
default) or invalid result decides no route: the input remains `semantic-pending`
for its bounded DeepSeek review. Once native acceptance is uncertain, no model
fallback may replay the message. Interrupted provider replacement requires
reconciliation rather than a new conversation.

## Idle work and health readings

Every minute, and a few seconds after a route, a native turn's end or a settled
delivery, `WorkLockReview` looks at the newest open task that owes the owner a
delivery. When Kin has taken the task on and it has been quiet for
`workSummaryIdleMinutes` without a declared outcome, DeepSeek Flash / high writes its
facts down for her from the authenticated inputs, the delivered and undelivered
outputs, the drafts never sent and the last public reply: what happened, what was
delivered, what the inputs asked for that the evidence does not show delivered, and
which drafts are unsent, citing only the IDs it was given. It judges nothing: no verdict, no lock, no
withdrawn draft. The summary is kept on the task, where the open-work facts of every
routed turn include it, and the host states it to Kin in a turn of its own that is
context, never a new requirement. Evidence is read by ID outside the router mutex,
and the summary is recorded only if the task's fingerprint has not changed meanwhile.
An unchanged task is summarized again only after an interval that doubles each time,
up to a day. A failed summary is retried after `workReviewIntervalMinutes`, and one
refused a background model slot after five minutes. Attempts, receipts and retry
times survive restart and show in the runtime's `workReview` view. The summarizer
sends no owner message and creates no second native conversation.

`MobileAudit` takes a durable reading of structured health evidence every
`auditIntervalHours`: channel and bridge health, memory queues, the runtime view,
unconfirmed and still-classifying inputs, the ledger summary, the work summaries'
state, appraisal progress and the contact and exploration configuration in effect.
DeepSeek Flash answers `healthy` or `needs_attention` with at most eight findings,
each naming one fault class from a fixed set — `delivery-uncertain`,
`session-mismatch`, `model-mismatch`, `task-stuck`, `input-unanswered`,
`memory-stalled`, `schedule-fault` or `other` — and the fields and values it rests
on. A reading never changes the provider, never starts work in the conversation and
never messages the owner. A reading that is not healthy is one incident keyed by its
fault classes: the same classes seen again refresh its evidence, and a cleared
incident reopens when they return within a day. Open incidents are facts for Kin.
Those she has not been told about ride once on her next routed turn that is not a
proactive draft or a command, marked as facts rather than instructions, and count as
told once that turn is submitted; when and whether to look at them is hers. A failed
reading is retried after 5 and then 15 minutes, then waits for its interval; a
reading skipped for lack of a background model slot is tried again within minutes
and is not a failure. Its state and open incidents show as `selfCheck` in
`/runtime`.

A read-only desktop review runs every twenty-four hours as an external check.
Transport recovery, affect-driven contact checks and curiosity-led exploration run on
their own schedules. Whatever triggers a contact, Kin's own decision included, the
owner's configured quiet hours, wait for a reply and minimum gap hold; with none
configured nothing holds it back. Exploration keeps its twenty-minute budget.

The reviewer calls DeepSeek's [Anthropic-compatible endpoint](https://api-docs.deepseek.com/guides/anthropic_api/)
with high-effort thinking and one structured tool result. The conversation gateway
uses the [Responses endpoint](https://api-docs.deepseek.com/guides/responses_api/).

## Validation

Every verified profile change creates one durable owner notification, including
automatic routing, the return after work closes and restart reconciliation. Every
transition records what became of it — `queued`, `suppressed` with its cause, or
`not-generated` (a rebind with no owner-visible change) — and a suppressed notice is
never counted as delivered. A switch that leaves the verified profile as it was
creates no switch notice, and neither does a maintenance restart that ends on the
model the owner was last told about: that notice is settled as suppressed rather than
sent, as [mobile recovery](mobile-recovery.md#durable-adapter-state) describes.
Explicit subscriptions share the transition notice; uncertain sends reconcile the
same outbox ID. Delayed notices distinguish the earlier transition from the current
verified model. These control notices stay outside the recent public dialogue used
for recall and appraisal.

Output ceilings are 16K for routing, 32K for a work summary, 64K for a health reading
and at least 64K for a native chat turn. A reviewer call that stops at its output
limit fails, even when its tool result looks syntactically complete; the receipt
records the stop reason and token usage without reasoning text. Request deadlines
are independent of token ceilings: the classifier timeout for routing, five minutes
for a work summary and eight for a health reading. A classification that cannot be
obtained waits as itself, bounded, as described above.

Run `node --test tests/business/*.test.mjs` for synthetic routing, ledger, delivery,
concurrency, gateway and cadence checks. Before enabling a host, additionally verify
a live conversation → configured work → exact prior profile round trip in an isolated
native conversation, tool calls on both providers, model-requested handoff, a
declared task outcome and restoration of automatic routing. The live check must
produce zero owner messages.

Verify an active turn and a background terminal both reject provider changes.
Preserve the existing work provider if native continuation or tool compatibility
cannot be established. A host release freezes dispatch and waits for `GET /busy` to
report idle before replacing the process, and thaws it once the new host is verified.

## Owner controls and message bursts

The owner's explicit model, effort or tier selection is independent of the content
route. A mixed request retains `route=work` and its `control=manual` profile
together. The router verifies the selected profile before submitting that work;
failure does not run it under the old profile. An owner control is immediate unless
she asks it to wait: the host fences the running turn's output, interrupts it and
confirms the native session idle before switching. A force whose interruption fails
is reported and releases its fences; one not yet confirmed waits with a pending
notice. A confirmed interruption preserves the original task, its delivery receipts
and any outcome Kin already declared; nothing continues the task automatically, and
its facts reach Kin in her next turn. Tools of the interrupted turn move into the
task's history under their original execution epoch and the saved cancel receipt,
keeping their status and ID: whether an external action took effect is still settled
by its own receipts, and a historical `in_progress` never counts as current activity.

The private mobile host groups consecutive messages from the same bound owner and
channel when their reception gap is less than twenty seconds. It waits twenty seconds
after the last message, then routes the combined text and attachments once. Each
original input and platform receipt stays identifiable. Batch membership is fixed on
the first existing inbox job before submission, so restart or late arrivals cannot
change a submitted request. An explicit stop bypasses the wait. A waiting dispatch
does not prevent later model controls from reaching the router; the existing
coordinator still serializes native execution. While dispatch is frozen the inbox
holds its jobs, letting only the owner's literal stop and mode commands through.

## Conversation during work

Owner chat keeps its semantic `intent=chat` while the work profile stays fixed, and
the host tells Kin in that turn that it is an interjection during work, not a new
requirement, and that the original task is still open. The existing live steering
path admits it to the same native session after the normal message-burst window.
Kin can respond between tools and continue; ordinary conversation never cancels an
in-flight tool, adds a requirement or closes the task, and nothing resumes the task
on her behalf afterwards. Public message segments pass the existing source/format
checks and delivery receipts at their ACP boundaries, without waiting for the whole
native turn. Private assessments stay private. A segment that ends in four or more
characters that could begin a private marker holds only that suffix until the next
segment; a shorter ending goes out as written.
