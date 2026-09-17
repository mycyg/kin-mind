# Kin Mind integration contract

Kin Mind adds tables to the existing MemoryPalace SQLite store. It does not copy
memories or create native agent conversations. A host chooses one database path,
`Scope`, and configuration version across channels. Persona-specific initialization
requires a retained explicit configuration source. All following examples are synthetic.

The optional [continuity layer](continuity.md) adds event understanding, grounded
concerns, local expression compilation and interaction-led rhythm to the same
transaction and asynchronous assessment. It introduces no separate expression
model request or native conversation.

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
summary endpoint and environment key name, with `deepseek-flash` for affect appraisal. The adapter
supports the official [Anthropic-compatible API](https://api-docs.deepseek.com/guides/anthropic_api/),
with a typed `submit_appraisal` tool call. Credentials are sent only to the official
HTTPS hostname. Embedding configuration remains independent.

`Appraisals.enqueue` persists original evidence IDs. `run_one` leases one review per
scope, makes one provider request, validates the response, and commits state and
wish proposals in one transaction. Eight optional sections of that commit are
applied in savepoints of their own, so a section the host refuses is undone alone
and the rest of the appraisal still commits; the refusal is recorded with static
codes and host text only, and one bounded follow-up asks again for it and for
whatever rested on it. A changed revision requires reappraisal. A lost queue
acknowledgement after a committed mutation reuses the command receipt.
Failures retain previous scores and use bounded retries with backoff: every lane
shares one cap on charged attempts, and an exhausted budget or the same error
signature twice in a row quarantines the row in the existing `needs-repair` state
instead of paying for it again. A sourced host action resumes a quarantined row,
which is then judged afresh with its failure history preserved. A timer checks
queue readiness; no ready evidence means no model request. Source bodies and provider
reasoning are absent from diagnostic errors. User-facing state may show the last
valid revision while appraisal is pending. The request includes projected scores,
active wishes with source IDs and compact completed-wish summaries. Full receipts
and evidence history remain in the database. Max reasoning uses a 131,072-token
output ceiling, matching the [official max-effort default](https://api-docs.deepseek.com/api/create-chat-completion/).
The background request timeout is 600 seconds. A private host must enforce a
660-second absolute review-subprocess deadline; the durable lease lasts 690 seconds,
so another worker cannot reclaim it before the original subprocess exits.
This does not block the chat session. Exhaustion remains a pending appraisal,
never an empty successful decision. Output ceilings do not require that many tokens
to be generated.

### Model lanes and the lease interface

Every DeepSeek call is admitted through one ledger, `mind_model_leases`, shared by the
Python host, the API service and the Node adapters. The lane follows the purpose the
caller declares, never the name of the function that ends up calling the model:
compression serves a waiting reader and an idle queue alike.

| Lane | Admission | Declared by |
|---|---|---|
| `foreground` | always admitted; the row only records who is calling | `memory-context` and the read tools for a chat, work, read or start-up context the user asked for; `session-checkpoint`; `share-preflight` and `share-preflight-group`; core recall; the Node classifier and chat turns |
| `user-work` | one reserved slot, **exempt from the foreground yield** | the review that decides whether a held work task is finished |
| `background` | `meta.kin_background_model_limit`, and only while no foreground session holds a lease anywhere on the machine | appraisals and everything nested in them, the daily review, event digests, procedure replay, completion review, prewarming, coverage backfill, core jobs, a context whose `access_origin` is `maintenance`, proactive drafts, the Node health audit |

The exemption is what prevents a deadlock. A held work task keeps a foreground lease,
every background caller waits on it with `deepseek-foreground-priority`, and the one
call that can release the task is that review. The foreground yield is machine-wide:
a scope isolates data, not the model's attention, and a plan executor's claim follows
the same rule. A caller that declares nothing keeps the behaviour it had before lanes,
no lease at all, and leaves a `model_lane_undeclared` metric with its purpose label.

Capacity comes from configuration. The host's `mind-config.json` states
`background_model_limit` (1–8); the `recover` action, which the host already runs at
start-up, writes it to `meta`, and any other action fills it in only when `meta` has
none. `configure-model-capacity` changes it while the host runs and holds until the
next restart, so change the file as well. A missing limit is never a silent default:
admission still uses 2, writes `model_capacity_unconfigured`, and `operational-status`
reports `source: "default-unconfigured"`. `operational-status` lists each lane with its
limit, the source of that limit and what is held, and every current holder by label,
age and time to expiry. Purposes and holders are short labels, never text.

A row lives for 90 seconds and Python renews it every 20. Expired rows, and expired
`mind_foreground_leases` rows such as the `read:` leases nobody returns, are swept at
every admission; that sweep is the whole of crash recovery. A renewal checks that it
still updated a row. When it did not, the lease is lost: the late result is
quarantined. A single call raises a `model-lease-lost` conflict instead of returning,
and writes `model_lease_lost` with the usage its caller reported or `unknown`, never
zero. An appraisal keeps the proposal and its receipt on the queue row and refuses
the commit, so the judgment is auditable and never applied; once the lease is gone
no further nested call is paid for. While an evaluation runs, a heartbeat extends the
queue row's own lease in 180-second steps and never shortens it, so a long call cannot
be reclaimed by another worker. No heartbeat outlives one hour: a worker that hangs
without dying loses its row and its slot like one that crashed, only later. A thread started on a caller's behalf, such as the
bounded rerank, is handed the caller's lease and declared lane and reuses them instead
of taking a second slot; a thread abandoned at its deadline is no longer renewed, so
it cannot keep a slot beyond the 90 seconds. The metrics are `model_capacity_unconfigured`,
`model_capacity_invalid`, `model_lane_undeclared`, `model_lane_unrecorded` (a foreground
or user-work call that went ahead while the ledger was busy), `model_lease_lost` and
`model_call_abandoned`; each carries labels and counters only.

Node never opens SQLite. The adapters use internal routes of the local API, which are
deliberately absent from the published OpenAPI contract, with the same bearer
credential as every `/v1` route:

| Route | Request | Answers (HTTP 200) |
|---|---|---|
| `POST /v1/model-leases/acquire` | `{lane, purpose, holder?, id?, ttl_seconds?}` | `{state:"admitted", lease:{id, lane, purpose, expires_at, ttl_seconds, renew_after_seconds}, capacity}` · `{state:"wait", reason, retry_after_seconds, capacity}` · `{state:"disabled"}` |
| `POST /v1/model-leases/renew` | `{id, ttl_seconds?}` | `{state:"renewed", lease}` · `{state:"lost", id}` |
| `POST /v1/model-leases/release` | `{id}` | `{state:"released", id}` · `{state:"lost", id}` |
| `GET /v1/model-leases` | — | the `model_lanes` block of `operational-status` |

`purpose`, `holder` and a caller-chosen `id` match `[A-Za-z0-9][A-Za-z0-9:._-]*`;
anything else is a 422. Choosing the `id` makes `acquire` repeatable after a lost
answer and lets the caller release a lease it never saw confirmed. `ttl_seconds` is
15–300 and defaults to 90; renew every `renew_after_seconds`. A lease is lost once its
row is gone: the next admission of any lane deletes every row past its expiry before it
counts, so a late renewal that still finds its row keeps it, and one that does not has
lost a slot that may already be somebody else's. The wait reasons are
`deepseek-background-capacity`, `deepseek-foreground-priority` and
`deepseek-user-work-capacity`. Lease operations use a two-second `busy_timeout`: a busy
or missing ledger answers 503 with `{state:"busy"|"unavailable", reason}` instead of
queuing behind a writer. The fallback when the service is down is the host action
`model-lease` with `{op:"acquire"|"renew"|"release"|"status", …}` and the same fields
and answers; it is handled before any engine opens, and reports a busy ledger as
`{state:"busy"}`. On `lost`, abort the request and record its usage as unknown. When
neither path answers, foreground and user work go ahead and note it, and background
work skips that run. `disabled` means `model_lanes` is off: carry on without a lease,
as before. A rolled-back service answers 404, which is the same degraded mode.

`model_lanes` is on by default in the memory settings and the ledger is shared, so the
switch is machine-wide: set to `false` in any scope it restores the previous admission
everywhere, with background capacity only, no quarantine, no heartbeat, and a plan
claim that looks at its own scope.

The one-minute host timer is a local queue/threshold check, not a periodic model
request. Lengthening it to twenty minutes delays threshold detection without
reducing idle provider requests, which are already zero. Exploration retains its
independent curiosity threshold and consumed wish identities.

The optional [mobile routing host](mobile-routing.md) keeps conversation and work
models in one native thread, protects ongoing tasks during model changes, and
separates four-hour mobile health reviews from the daily desktop check.

A daily personality review is separate from short-term scoring. It requires three
independent original interactions and an existing prospective behavioral assessment
in the same agent version. `Mind` enforces parameter limits and preserves the claim's
hypothesis status. A later explicit correction can revert the latest personality
revision while retaining history; intervening personality revisions require review.

## Exploration

`Explorations` claims an unexpired question selected by DeepSeek. With `semantic_actions` enabled, a current DS decision replaces the legacy curiosity threshold; scores remain dynamic context. The worker claim and desire transition are atomic. There is no elapsed-time admission gate; the 1,200-second maximum remains in the profile. `run_kimi` starts the
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
2. Project dynamic scores after updates and on a 60-second timer. With
   `semantic_actions` enabled, reserve a contact only with a current DS decision
   and an actionable wish; the disabled legacy path retains its score gate.
3. Ask the **original** shared session for a draft, capturing output locally. Do not
   forward draft commentary, files or intermediate output to the phone.
4. Recheck desire revision/expiry/source validity, owner input epoch, active work,
   quiet hours and unanswered outreach immediately before sending.
5. Store pending before the platform call. Use the same stable attempt ID in the
   transport outbox. Accepted requires the platform's message ID. A timeout, missing
   ID, context race at the send boundary, or uncertain failure stays unconfirmed.
   Never replay it under a fresh ID. Reconcile with actual platform evidence.
6. On accepted delivery, complete that wish and atomically queue a DeepSeek reassessment. Hold further contact until that review finishes; transport does not assign a score. Track phone visibility separately. An internal wake does not count as a user reply.

Drafts use the public `contact-draft.mjs` contract: send `bubbles` (legacy `text` remains valid), abandon an obsolete
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


## Affect-driven action episodes

An owner can permit affectionate calls for attention after a long silence. The
reviewer receives `interaction_timing` from authenticated owner inputs and accepted
proactive receipts, and combines it with current affect. It need not invent a new
topic. Internal reviews and configuration changes do not count as an owner reply;
silence does not itself increase grievance or possessiveness. A new episode uses a
new wish identity while delivery retries keep the existing message identities.

`AffectiveEvent.motivations` and `Appraisal.motivations` accept `initiative` and
`curiosity`, each with a target (0–100), a half-life in minutes (20, 60 or 180),
and a sourced reason. These are short-term episode parameters; long-term trait
calibration remains separate. A persisted crossing key prevents a high plateau
or restart from calling the model repeatedly. Internal thoughts are model-origin
sources and do not count as new owner interactions or personality evidence.

`ActionEvents` is the transactional outbox for bootstrap, threshold crossings,
exploration findings and accepted contact. It materializes idempotent sources and
appraisal jobs. The owner host also ingests public assistant results. Failed model
requests remain pending with bounded backoff; corrected evidence requires review.
A delivery receipt can update satisfaction and remaining motivation but cannot
create a new wish on its own. Existing useful, playful or affectionate intentions
can continue without imposing a fixed reset or a fixed sending interval.

`configure-actions` requires explicit source evidence, configuration version and
revision. It installs the policy and queues a one-time migration review without
resetting scores or reopening completed, expired or abandoned wishes. `read`
returns episode metadata, action-event state and a verified `decision_runtime`.
Exploration wishes supplied through another tool are reviewed by DeepSeek before
execution. The original shared conversation generates outreach only after its
actual DeepSeek model is verified; active work defers it without changing GPT.

`createContactBatch` freezes text and per-bubble IDs before sending. A restart
reuses accepted receipts, reconciles uncertain bubbles, and only then sends the
unsent remainder. The same paragraph splitter serves ordinary and proactive chat;
it preserves fenced code, words and links rather than truncating them to fit.

DeepSeek requests enable thinking with `output_config.effort=high`, using the
[official effort controls](https://api-docs.deepseek.com/guides/thinking_mode/).
Only the validated structured tool result enters the state store. Public copies
contain synthetic examples; persona contracts and actual state remain private.
