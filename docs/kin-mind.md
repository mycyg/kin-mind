# Kin Mind integration contract

Kin Mind adds tables to the existing MemoryPalace SQLite store. It does not copy
memories or create native agent conversations. A host chooses one database path,
`Scope`, and configuration version across channels. Persona-specific initialization
requires a retained explicit configuration source. All following examples are synthetic.

## Storage and tools

`Mind` persists the aggregate state, an append-only event snapshot history, and a
contact outbox reservation. `read()` projects independent decay trajectories without
writing. Every update freezes its source references and model/configuration version.
A source correction, supersession or deletion marks affected views `needs_review`.
Do not use those views to justify a contact. Expected revisions prevent stale model
results from overwriting newer updates. Scoped command IDs and source identity dedupe
replays. Desires are separate from the existing commitment/reminder task records.

Three tools are registered alongside the inherited MCP tools:

- `read_affective_state(scope, history=0)` returns scores, reasons, evidence IDs,
  configuration versions, desires and traits. History is limited to 100 snapshots.
- `record_affective_event(scope, event)` validates sourced partial updates. Generic
  library users can submit an appraisal directly. The private Kin host replaces this
  tool with an enqueue operation so DeepSeek owns evaluation.
- `manage_desire(scope, request)` creates or revises a wish. Terminal wishes remain in
  history; subsequent wishes require new identities. A model cannot confirm delivery.

Transport transitions (`claim`, `check`, `settle`) are host-only methods and are not
exposed as model-facing MCP tools. `python -m kin_mind.cli --root ROOT ACTION` accepts
one JSON request on stdin, including a mandatory scope. The host wrapper
`python -m kin_mind.host --config PRIVATE_CONFIG ACTION` reads credentials from an
existing private environment file and emits sanitized operation receipts.

## DeepSeek and memory

The existing `settings.models` extraction, conflict and summary endpoints remain
responsible for memory processing. `DeepSeek.from_engine` reuses the configured
summary endpoint, model and environment key name for affect appraisal. The adapter
supports the official [Anthropic-compatible API](https://api-docs.deepseek.com/guides/anthropic_api/),
with a typed `submit_appraisal` tool call. Credentials are sent only to the official
HTTPS hostname. Embedding configuration remains independent.

`Appraisals.enqueue` persists original evidence IDs. `run_one` leases one review per
scope, makes one provider request, validates the response, and commits state and
wish proposals in one transaction. A changed revision requires reappraisal. A lost
queue acknowledgement after a committed mutation reuses the command receipt.
Failures retain previous scores and use bounded retries with backoff. A timer checks
queue readiness; no ready evidence means no model request. Source bodies and provider
reasoning are absent from diagnostic errors. User-facing state may show the last
valid revision while appraisal is pending.

The one-minute host timer is a local queue/threshold check, not a periodic model
request. Lengthening it to twenty minutes delays threshold detection without
reducing idle provider requests, which are already zero. Exploration retains its
independent four-hour cadence.

The optional [mobile routing host](mobile-routing.md) keeps conversation and work
models in one native thread, protects ongoing tasks during model changes, and
separates four-hour mobile health reviews from the daily desktop check.

A daily personality review is separate from short-term scoring. It requires three
independent original interactions and an existing prospective behavioral assessment
in the same agent version. `Mind` enforces parameter limits and preserves the claim's
hypothesis status. A later explicit correction can revert the latest personality
revision while retaining history; intervening personality revisions require review.

## Exploration

`Explorations` selects an unexpired, source-backed exploration wish. The four-hour
cadence and 1,200-second maximum are stored in the profile. `run_kimi` starts the
installed CLI with a dedicated [agent profile](https://moonshotai.github.io/kimi-code/en/customization/agents.html)
that allows Read, Grep, Glob, WebSearch and FetchURL. It excludes write, shell,
subagent and messaging tools and overrides automatic skill discovery. These are
application tool restrictions, not an operating-system filesystem sandbox. The
host should run it under the desired OS identity/sandbox and pass only authorized
project paths. Remote search depends on the installed CLI's configured services.

The wrapper consumes stream JSON and keeps only validated final reports. Tool
transcripts and thinking blocks are discarded. Citations remain model-reported and
reviewable; a citation is not automatic factual verification. A new owner task sets
the cancellation signal. The wrapper terminates only its own child process group.
Timeouts retain any already completed final report as partial. A missing final report
is recorded as incomplete, never fabricated. Crash-interrupted jobs stay inspectable.
The stock runner is Kimi CLI; `Explorations.run(..., runner=...)` is the extension
point for a host-provided Luna runner with the same result and cancellation contract.

## Shared-session contact host

Contact preferences are owner-editable configuration. `Mind.configure_contact`
(host action `configure-contact`) records an explicit source, expected revision,
configuration version and idempotent command. It can change `wait_for_reply`
without changing scores, quiet hours, exploration cadence or delivery records.
Corrected preference evidence pauses contact for review, including a draft already
in progress. A host projects the accepted preference into its live transport gate
and verifies both writes before reporting completion. Replaying an older command
must not restore an obsolete preference.

Hosts that provide model-side configuration tools should state that prior owner
preferences can be changed by a new explicit owner request; they are not immutable
tool permissions. The current preference must accompany the response/draft context.
When reply waiting is disabled, new thoughts, playful ideas and imagined scenarios
may be shared as such; repeating a previous share or inventing an experience is
not new content. A particular question can still wait for an answer.

`adapters/owner-host.mjs` supplies a dependency-injected `MindLoop`. The private host
must authenticate the sender, bind a single recipient and preserve its native session.
It performs these operations:

1. Ingest authenticated owner messages as real user sources. Internal wakes and
   exploration reports have independent event categories and do not change the
   owner's last-input timestamp or release unanswered-outreach waiting.
2. Check the score after updates and on a 60-second timer. Reserve one contact
   candidate only when initiative is at least 75 and an actionable wish exists.
3. Ask the **original** shared session for a draft, capturing output locally. Do not
   forward draft commentary, files or intermediate output to the phone.
4. Recheck desire revision/expiry/source validity, owner input epoch, active work,
   quiet hours and unanswered outreach immediately before sending.
5. Store pending before the platform call. Use the same stable attempt ID in the
   transport outbox. Accepted requires the platform's message ID. A timeout, missing
   ID, context race at the send boundary, or uncertain failure stays unconfirmed.
   Never replay it under a fresh ID. Reconcile with actual platform evidence.
6. On accepted delivery, complete that wish and reset initiative to 20. Track phone
   visibility separately. An internal wake does not count as a user reply.

Drafts use the public `contact-draft.mjs` contract: send text, abandon an obsolete
wish, or wait for a declared condition. Time waits include a bounded `retry_at`;
owner-reply waits require authenticated owner activity; evidence waits require a
new related source. The host calls `reconsider` before candidate selection. This
deterministic operation survives restart and never creates evidence or scores.
Expired or corrected sources cannot resume. Every attempted draft still passes
the existing send-boundary checks after a condition becomes ready.

Invalid JSON and generation errors are technical failures, distinct from a valid
decision to wait. Failed drafts retry after five and ten minutes; the third failure
waits for new evidence. An uncertain send remains held and is never converted into
a retryable draft error. Contact status exposes the last check and blocking reason.
Wishes already addressed in ordinary dialogue are retired without inventing a
proactive delivery receipt.

`configure-behavior` records an explicit expression preference and optional quiet
start hour under a new configuration version. It does not change scores, baselines,
or decay parameters. `interaction_style` turns a sourced affectionate preference
and the current flirtation band into short response guidance. Focus controls timing
and task quality without suppressing the independent flirtation state. Corrected
preference sources mark that guidance for review.

Owner activity, the contact state machine and the authenticated transport must be
checked together. The library alone does not know recipient identity, quiet-hour
preferences, phone read status, or whether a native session is still working. Do not
use an older scheduled-checkin sender to bypass this entry point. Explicit user
reminders keep their requested timing and task identity.

## Validation

Python tests cover independent trajectories, mixed states, restart/replay, revision
conflicts, source correction, scope isolation, early threshold crossing, held unknown
delivery, personality evidence and reversion, provider validation, and Kimi process
cancellation. Node tests cover quiet/wait gates, owner input races, concurrent ticks,
platform receipt requirements and uncertain sends. Real providers use private runtime
verification; CI uses synthetic sources and controlled provider/CLI stubs.
