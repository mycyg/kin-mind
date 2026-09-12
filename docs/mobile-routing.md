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

Entering work mode takes effect at an idle boundary. An exit request remains
pending until the task is delivered. A completion proposal alone does not release
work: the host requires `completedTaskId` and `completedInputVersion`, then checks native turn completion, tool states,
background terminals and confirmed delivery receipts. New input invalidates an
earlier completion proposal. Unknown runtime, failed work and uncertain delivery
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

A mode request can set `notify: true`. After native model verification, the host
creates a durable notification with a stable output ID. The owner-bound sender
and its durable outbox are injected through `flushNotices({send, lookup})`.
Successful delivery requires a platform message ID. A missing ID or timeout is
reconciled against that same outbox record; it does not trigger another model
turn or a fresh send. Only a confirmed pre-send failure (`not-started`) retries,
with the same ID. Notification receipts are separate from task delivery receipts.
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

## Provider compatibility

`codex-models.mjs` changes the ACP provider and model, then verifies native thread
identity, model, provider and reasoning settings. `codex-runtime-patch.mjs` adds a
bounded runtime query and rejects provider changes while native execution or
background terminals remain active. An unsupported adapter version fails closed.

The mobile process uses its own combined model catalogue. Global desktop model
configuration and account credentials are not rewritten. GPT-6 retains the host's
configured Fast setting. DeepSeek conversation, classification and health review
use `max` thinking; private reasoning never enters the channel output.

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
summary. A bounded timeout (15 seconds by default) or invalid result selects the work model before
submission. Once native acceptance is uncertain, no model fallback may replay the
message. Interrupted provider replacement requires reconciliation rather than a
new conversation.

## Review cadence

`MobileAudit` performs a durable four-hour review of structured health evidence.
The DeepSeek reviewer has no repair or messaging tools. It returns findings with
source fields; the host schedules a repair in the shared GPT-6 conversation when
user work permits. Repeated findings do not create duplicate repair jobs. A failed
review waits for the next review period rather than retrying every minute.

The desktop monitor runs every twenty-four hours as an external check. Local
transport recovery, contact eligibility checks and the four-hour exploration
schedule remain independent. Exploration retains its twenty-minute budget.
Routine healthy reviews do not send a message.

The independent reviewer uses DeepSeek's [Anthropic-compatible endpoint](https://api-docs.deepseek.com/guides/anthropic_api/)
with `max` thinking and a structured tool result. The conversation gateway
uses the [Responses endpoint](https://api-docs.deepseek.com/guides/responses_api/).

## Validation

Run `node --test adapters/*.test.mjs` for synthetic routing, delivery, concurrency,
gateway and cadence checks. Before enabling a host, additionally verify a live
DeepSeek → GPT-6 → DeepSeek round trip in an isolated native conversation, tool
calls on both providers, model-requested handoff, task completion and restoration
of automatic routing. The live check must produce zero owner messages.

Verify an active turn and a background terminal both reject provider changes.
Preserve the existing work provider if native continuation or tool compatibility
cannot be established. Host rollout must wait for active user work and delivery
to settle before replacing the process.
