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
During GPT-6 work, incoming chat is steered to the existing task without another
classification request.

Entering work mode takes effect at an idle boundary. An exit request remains
pending until the task is delivered. A completion proposal alone does not release
work: the host checks the task input version, native turn completion, tool states,
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

## Provider compatibility

`codex-models.mjs` changes the ACP provider and model, then verifies native thread
identity, model, provider and reasoning settings. `codex-runtime-patch.mjs` adds a
bounded runtime query and rejects provider changes while native execution or
background terminals remain active. An unsupported adapter version fails closed.

The mobile process uses its own combined model catalogue. Global desktop model
configuration and account credentials are not rewritten. GPT-6 retains the host's
configured Fast setting. DeepSeek requests explicitly disable thinking.

`deepseek-gateway.mjs` binds an authenticated loopback endpoint and forwards only
DeepSeek Flash Responses requests to the official HTTPS endpoint. It excludes
provider-specific reasoning payloads from cross-provider input and output while
preserving user messages, assistant answers, function calls and tool results.
This addresses reasoning content accepted by one provider but rejected by another;
it does not rewrite an existing native transcript. Provider keys never appear in
the loopback client token or diagnostic errors.

Classification uses the current message, a bounded recent conversation and task
summary. A five-second timeout or invalid result selects the work model before
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
with thinking disabled and a forced structured result. The conversation gateway
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
