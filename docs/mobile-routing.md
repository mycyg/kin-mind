# Persistent mobile model routing

The mobile host keeps one native conversation across channels and model providers.
DeepSeek Flash handles conversation; GPT-6 handles creative work, research,
documents, code and computer tasks. Routing selects a conversation mode, not an
independent model for every message. Deployment-specific identities, credentials,
state files and personas remain private.

## Work owns the conversation

`MobileRouter` persists input receipts, work tasks, mode requests, configuration
revisions and transition history. Input acceptance and provider replacement share
one mutex. New work received during a DeepSeek turn waits for that turn to finish.
DeepSeek classifies natural language using the current message, recent conversation,
mode and task summary, including during work. Its result describes intent; the
host independently keeps active work on GPT-6. Runtime enquiries and notification
requests do not create work or invalidate a completion proposal. Literal native
commands have their own protocol path; natural phrasing is not matched by a
collection of regular expressions.

A classification that fails decides nothing (KIN-ITER-20260918-02, revising the
old "classification anomaly becomes work" tradeoff): no task is manufactured, no
provider switch happens and no execution grant is made. The input waits in the
durable `semantic-pending` state — input id and version basis, the real tasks and
actual model at capture, the failure class (`timeout`, `http`, `parse` or
`unavailable`) and the next bounded review condition — and the same input is asked
of DeepSeek again under a bounded, persistent budget. A late answer is applied to
the state of now: it checks the task's input version, cancel state and generation
before routing, and a work answer that arrives after a stop is retired rather than
resurrected. When the budget is exhausted the input becomes `semantic-failed`,
visible to the ops and reply chains — never silently swallowed, never routed by
the failure itself.

Entering work mode takes effect at an idle boundary. An exit request remains
pending until the task is delivered. A completion proposal alone does not release
work: the host requires `completedTaskId` and `completedInputVersion`, then checks native turn completion, tool states,
background terminals and confirmed delivery receipts. New input invalidates an
earlier completion proposal. When `WorkLockReview` is installed, owner-task completion also requires a DeepSeek judgment; the assistant proposal remains supporting evidence. Unknown runtime, failed work and uncertain delivery
retain the task. An explicit owner cancellation releases it only after execution
has stopped. A platform receipt is not a read receipt.

The host's MCP interface provides live runtime inspection and asynchronous mode
requests. A model requesting a handoff ends its turn; the host resumes the same
native thread and continues the retained task. It must not synchronously wait for
the provider restart inside the requesting tool call.

`mobile-memory-hook.py` uses private host event receipts to separate owner input
from internal continuations and reviews. It saves the original owner text instead
of transport metadata. Internal prompts, answers and tool events stay in the
native transcript and host journal without becoming new interpersonal evidence.

## Runtime controls and native maintenance

The classifier returns `chat`, `work`, or `control`. Control intentions are
`status`, `watch`, `work` (enter work mode) and `auto` (request automatic routing).
The host answers status requests from verified runtime metadata. Asking for a
switch notification attaches to the pending request; it does not start a GPT-6
task. A mixed message that also requests code, a document or a repair remains work.

Historical correction is a separate receipt, not a rewrite of the input record. An
authenticated private host can call `reclassifyAcceptedControl` only for an already
accepted owner input, with its semantic hash, the exact router revision, a bounded
current-classifier result and hashed acceptance/owner/source evidence tied to the
same session, conversation and generation. Only manual, auto or work control is
accepted. The receipt leaves `record.route` untouched and authorizes one exact mode
request; it never submits the source input or replays its task effects. An identical
retry returns that receipt, while a changed command ID or newer explicit control
makes the evidence stale.

A mode request can set `notify: true`. After native model verification, the host
creates a durable notification with a stable output ID. The owner-bound sender
and its durable outbox are injected through `flushNotices({send, lookup})`.
Successful delivery requires a platform message ID; an `accepted` answer without
one is not believed. Every receipt reads exactly one of three ways
(KIN-ITER-20260918-03): `accepted`; `rejected`, which is terminal; or
proven-not-submitted — no outbox record, or one that proves nothing reached the
platform — which retries the SAME ID under a visible bounded budget, resumable
across restarts. Anything else means the submission is unknown: the original ID
is only ever looked up, never resent, and the re-checks are themselves bounded
before the notice stops polling as a visible `unresolved`. Retry exhaustion is a
state with a reason, never permanent polling. A settle that would change nothing
writes nothing. Notification receipts are separate from task delivery receipts.
Host integrations must guard provider changes for the duration of the send and
must bypass task-delivery accounting for these non-task messages.

`actual` is the fresh native model result. `lastTransition` is a historical record
with `matchesCurrentModel`; `transition` remains a compatibility alias. Every
host switch, including verification probes, records its origin and verified
result. An independently observed model change gets its own transition record.

`/compact` is native maintenance, with an independent operation receipt. The host
uses `compactPrompt` to put the command in the first prompt block before attaching
host metadata: ACP only recognizes commands in that position. It reports operation
start and completion through `observeOperation`; these events do not complete an
unrelated user task. An interrupted operation remains unconfirmed rather than
being replayed. Native compaction completion follows the app server's
[`contextCompaction` lifecycle](https://learn.chatgpt.com/docs/app-server).

## The unsent rest of an interrupted reply

A new owner message does not cancel the reply that is still going out. The
group is interrupted at its next bubble boundary — a bubble that has begun is
always finished first — and its unsent remainder waits for a decision about what
should become of it: `continue` to deliver it exactly as written,
`rewrite_remainder` when what it says still matters but belongs in the next
reply, or `supersede` when the new message makes it obsolete.

That decision is DeepSeek's, and it rides on a call the host makes anyway. The
first carrier is the routing call for the new message: when the host has an
interrupted reply to offer, it travels with that request and the decision comes
back beside the route. Without one, every byte of the request is what it was
before tails existed, and an unsolicited answer is never handed on. The route
stands on its own — a tail the host cannot use is dropped and the remainder
simply waits for its next carrier, which the classifier's own timeout, an
attachment and a literal command all become, since those are never classified.
The second carrier is the review of the next reply, which is given the older
group's unsent items and reports which of them the new reply covers. Only when
neither ran does the remainder get a bounded call of its own, and a literal owner
stop needs no model at all. A group with a fragment whose outcome is unknown is
offered to nobody: it blocks itself and nothing else.

The intent is written to the group's manifest before anything is done about it,
and recovery rolls it forward rather than starting again:

1. **recorded** — the decision is on disk, with its carrier, its reason and the items it covers.
2. **applied** — every named bubble is retired, its content reservation released and its canceled event emitted. A `continue` whose bubbles are all still unsent skips this: the group simply goes on, same bubbles, same transport IDs, and settles at once.
3. **linked** — the group that answers for the remainder is named, and the old bubbles' supersession moves from the intent to that group. A `continue` whose bubbles can no longer go on in place becomes a group of its own carrying the same words, reviewed afresh.
4. **settled** — by what that group really delivered.

"Into the next reply" is an intention, not a delivery. What the next reply's
receipts do not show as covered comes back and waits for a decision again, at
most twice; after that the only answers left are continue and supersede. What
DeepSeek superseded, and what the owner stopped, never comes back. A remainder
that was withdrawn but not promised to a next reply does not wait indefinitely
to be pointed at a successor either: after a bounded wait it settles as
superseded.

The owner's literal stop is made durable before anything else, so a restart
cannot lose it, and it outranks whatever was decided before: a remainder promised
to the next reply is no longer owed, and one that was to go on does not. It is
dated when the owner said it rather than when the host got round to it, so the
reply written after the stop is its successor. A dedicated call is asked only for
a remainder that has waited without any carrier deciding, only while the owner's
turn is not running, one group per pass and a bounded number of times with a
growing wait; a new owner message is a new reason to ask. It declares the
foreground lane under a purpose of its own, so the usage rows show every time
ordinary chat paid for an extra call.

The reply-tail port the router calls (offer, decided, missed, stopped) is
optional and never decides routing: a port that is absent, slow to answer or
failing changes nothing about the route. Tail facts reported for status carry
states and identifiers only, never message text, and what was decided stays
readable after later events that do not repeat it. The switch is the host's
`reply_tail_decision`; with it off an interrupted group is handled exactly as the
manifest alone handles it. The manifest, its fragments and the operator commands
are described in [mobile recovery](mobile-recovery.md#complete-phone-replies).

## Provider compatibility

`codex-models.mjs` changes the ACP provider and model, then verifies native thread
identity, model, provider and reasoning settings. `codex-runtime-patch.mjs` adds a
bounded runtime query and rejects provider changes while native execution or
background terminals remain active. An unsupported adapter version fails closed.

The mobile process uses its own combined model catalogue. Global desktop model
configuration and account credentials are not rewritten. GPT-6 retains the host's
configured Fast setting. DeepSeek conversation uses the reasoning effort the host
configures for the gateway; classification, the tail decision, work review and
health review enable thinking at high effort, and their receipts record it.
Private reasoning never enters the channel output.

`deepseek-gateway.mjs` binds an authenticated loopback endpoint and forwards only
DeepSeek Flash Responses requests to the official HTTPS endpoint. It excludes
provider-specific reasoning payloads from cross-provider input and output while
preserving user messages, assistant answers, function calls and tool results.
Trusted developer instructions map to the provider's supported system role.
The gateway adds a stable instruction to address the user without narrating
response planning. This is a generation constraint: prose mislabeled by the
provider as a final answer cannot be identified by a channel filter alone.
This addresses reasoning content accepted by one provider but rejected by another;
it does not rewrite an existing native transcript. Provider keys never appear in
the loopback client token or diagnostic errors.

Classification uses the current message, a bounded recent conversation and task
summary. If the evidence does not establish that the owner wants substantive work,
the classifier keeps the message in chat so the conversation can clarify. A bounded
timeout (15 seconds by default) or invalid result decides no route: the input remains
`semantic-pending` for its bounded DeepSeek review. Once native acceptance is
uncertain, no model fallback may replay the message. Interrupted provider replacement
requires reconciliation rather than a new conversation.

## Review cadence

`WorkLockReview` checks the work lifecycle locally every minute and after new
input, a completed native turn or a delivery update. At a verified idle boundary,
DeepSeek Flash / high assesses the original authenticated inputs, their follow-ups,
public answers, tool status, actual delivery receipts and linked background
exploration wishes. It returns `keep`, `complete` or `not_a_task`. A held task is
reviewed again after twenty minutes even if its inputs have not changed. Failed
reviews use the same interval; busy native work only incurs a local check.

[Mobile recovery](mobile-recovery.md#work-and-delivery) states the output ceiling
and request deadline this review is given, which stay within the provider's
[documented output limit](https://api-docs.deepseek.com/quick_start/pricing/).
An output-limit stop is rejected even if a tool result appears syntactically
complete. The receipt records stop reason and token usage without retaining
reasoning text. A review-protocol version change invalidates the earlier review
key, allowing corrected reviewers to reassess a previously failed attempt.

The host rechecks the exact task/input version, actual runtime and evidence
after the model returns, under the same mutex used for new input and model
switches. A changed input, unfinished tool, background terminal, missing evidence
or uncertain send preserves GPT-6. Deferred share approval is recorded as
`deferred`, separately from a transport request in progress. DeepSeek may discard
an older ordinary acknowledgement only when it judges the task `not_a_task`, a
later authenticated input supersedes it and the host proves transport never
started. Files and work deliveries cannot use that exception. Closing an accidental
lock preserves its inputs and the independent exploration wishes.

Assessment receipts, evidence hashes, decisions and recheck times survive restart.
They remain available in the private runtime's `workReview` view and audit log.
The reviewer does not send owner messages or create a second native conversation.
The original thread resumes its verified conversation model after the host closes
the task. Normal assistant completion cannot bypass an installed work reviewer;
explicit owner cancellation and host-verified internal repairs retain their own
lifecycle paths.

`MobileAudit` performs a durable four-hour review of structured health evidence.
The DeepSeek reviewer has no repair or messaging tools. It returns findings with
source fields; the host schedules a repair in the shared GPT-6 conversation when
user work permits. Repeated findings do not create duplicate repair jobs. A failed
review waits for the next review period rather than retrying every minute.

The desktop monitor runs every twenty-four hours as an external check. Local
transport recovery, affect-driven contact checks and curiosity-led exploration
schedule remain independent. Exploration retains its twenty-minute budget.
Routine healthy reviews do not send a message.

The independent reviewer uses DeepSeek's [Anthropic-compatible endpoint](https://api-docs.deepseek.com/guides/anthropic_api/)
with `max` thinking and a structured tool result. The conversation gateway
uses the [Responses endpoint](https://api-docs.deepseek.com/guides/responses_api/).

## Validation

Every verified model change creates one durable owner notification, including
automatic routing, work completion and restart reconciliation. Every transition
records what became of it — `queued`, `suppressed` with its cause, or
`not-generated` (a rebind with no owner-visible change) — and a suppressed notice
is never counted as delivered. A same-model
profile refresh creates no switch notice, and neither does a maintenance restart
that ends on the model the owner was last told about: that notice is settled as
suppressed rather than sent, as [mobile recovery](mobile-recovery.md#durable-adapter-state)
describes. Explicit subscriptions share the
transition notice; uncertain sends reconcile the same outbox ID. Delayed notices
distinguish the earlier transition from the current verified model. These control
notices stay outside the recent public dialogue used for recall and appraisal.

The classifier, the tail decision, work review, health review and memory
evaluation enable thinking at high effort; the gateway forwards the effort the
host configures for conversation. Output ceilings are 16K for routing and for a
tail decision asked on its own, at least 64K for native chat and structured
memory requests, and 128K for appraisal and work review. Output truncation
remains a failed result. Latency limits are independent of token ceilings;
a classification that cannot be obtained waits as itself, bounded, as described
above.

Run `node --test adapters/*.test.mjs` for synthetic routing, delivery, concurrency,
gateway and cadence checks. Before enabling a host, additionally verify a live
DeepSeek → GPT-6 → DeepSeek round trip in an isolated native conversation, tool
calls on both providers, model-requested handoff, task completion and restoration
of automatic routing. The live check must produce zero owner messages.

Verify an active turn and a background terminal both reject provider changes.
Preserve the existing work provider if native continuation or tool compatibility
cannot be established. Host rollout must wait for active user work and delivery
to settle before replacing the process.
