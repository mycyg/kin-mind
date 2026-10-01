# Event memory lifecycle

Kin uses the existing MemoryPalace scope, original sources, versioned graph and
persistent worker queue. Event IDs remain stable as members arrive. A topic can
contain several distinct events; semantic similarity proposes candidates and
never establishes identity by itself.

## Feature switches and rollout

`MemoryContinuity.configure` accepts independent boolean switches:

| Switch | Effect |
| --- | --- |
| `event_lifecycle` | Appraisal routes, versioned membership and digest refresh |
| `adaptive_recall` | Automatic choice of local or deep retrieval |
| `auto_volumes` | Versioned topic volumes derived from graph membership |
| `temperature_shadow` | Usage provenance and simulated thermal ordering |
| `temperature_ranking` | Apply thermal ordering to optional automatic background |
| `deferred_routes` | Deferred event routes are shown to later memory assessments until placed (needs `event_lifecycle` and `graph`) |

Those switches default to false. Enable on a backed-up copy first. Automatic
volumes have `generated_by=kin-lifecycle`; manually maintained volumes retain
ownership of their own members. Disabling a lifecycle feature fences both new
job preparation and a pending result's commit. Disabled jobs park in
`waiting_config` and can resume after re-enabling the feature.

The same configuration, written through the host action `configure-memory`,
carries the switches that default to **on**:

| Switch | Covers | Documented in |
| --- | --- | --- |
| `attempt_ledger` | `mind_appraisal_attempts`: one row per appraisal attempt with its model calls | [mobile recovery](mobile-recovery.md) |
| `idempotency_fingerprint` | `mind_command_fingerprints`: command id, effective payload and precondition kept apart | [architecture](architecture.md) |
| `manifest_rebase` | `mind_appraisal_manifests` and the commit rebase that reads one | [mobile recovery](mobile-recovery.md) |
| `appraisal_reuse` | Tier A: committing a stored proposal with no model call | [mobile recovery](mobile-recovery.md) |
| `appraisal_revalidation` | Tier B: one light `revalidate_appraisal` question | [mobile recovery](mobile-recovery.md) |
| `model_lanes` | `mind_model_leases` admission with foreground, user-work and background lanes | [Kin Mind](kin-mind.md#model-lanes-and-the-lease-interface) |
| `semantic_cache_v2` | `mind_judgment_cache` and its dependency index | [Kin Mind](kin-mind.md#deepseek-and-memory) |
| `memory_item_isolation` | Item-level isolation in the memory section, and `mind_memory_unorganized` | [mobile recovery](mobile-recovery.md) |
| `recall_purpose_policy` | `source_evidence_class`: the reading purpose, the evidence classes and the labels every read surface carries | [architecture](architecture.md#reading-purpose-and-evidence-classes) |

Each is independent, and an explicit `false` turns off that part alone. None of
them adds a column to a table that already existed, so code without these
features reads the columns it always read. `mind_model_leases` also serves
admission with `model_lanes` off; every other table above belongs to its switch
alone. A rollback leaves the tables in place, and a re-enabled switch finds its
history. `max_charged_attempts` is a number in the same configuration rather
than a switch. `chunked_reply_review` is retired: a configuration that still
carries it is not refused, and the key is ignored when read and dropped on the
next write.

Several mechanisms documented across these pages have no switch. The conflict
taxonomy that classifies a commit failure only adds fields to existing records.
The outbox's single cancel rule, its dispatch outside the write transaction and
`outbox_channel_contracts` keep a delivery whose request may be on the network
from being reported as canceled. The durable reply manifest with
its fragments and receipts, the words of an unsent reply that it owes Kin's next
turn, the proactive contact group's single local preflight, the durability of
the adapter state files and the ordering of a degraded rerank have none either.
The evidence isolation migration is not a switch: it is an operator action with
its own dry run and undo.

Cooling additionally requires seven full elapsed days, seven consecutive completed
daily observation receipts, and a fresh `temperature_validation` with critical
recall 1.0, recall@8 >= 0.9, and zero wrong merges, unsupported upgrades, stale
facts and background reinforcement. Receipts use the actual observation day;
catching up old scheduled slots cannot fabricate earlier observations. Re-enabling
shadow mode after a pause starts a new trial. Observation does not activate ranking.

## Event routing and revision

`MemoryAssessment.event_routes` is committed in the appraisal transaction after
source and graph validation. Each proposal includes:

- `key`, `action`, `evidence_ids`, `member_ids` and `reason`;
- `event_id` and `expected_revision` for existing events;
- `title` for a new event;
- `binding`: `same_task`, `same_artifact`, `sourced_continuation`, `explicit_reference`, or
  `semantic_candidate`;
- an exact original `quote` for explicit references;
- optional `thread_id` and `expected_thread_revision`.

Actions are create, append, link, correct and defer. Explicit continuation includes
an original quotation and a sourced model judgment of continuity.
`sourced_continuation` handles natural-language references and is checked by the
same rule as `explicit_reference`. The model's `identity` decision covers participants, concrete object, continuation
and time compatibility, and cites `prior_record_ids`. The host captures their versions before the model
request and verifies those original versions at commit. The host verifies that the
quotation exists in the new explicit source and that the prior records belong to
the current target event. It does not require a matching title or continuation
keyword. Uncertain/different-event decisions remain deferred even with the same
title. Same-task binding requires a canonical host task, rather than a shared broad
topic. Artifact binding requires an actual artifact identity. An append or a
correction whose binding does not hold is deferred; a link can rest on a semantic
candidate. Every member must belong to the authorized evidence scope.

With `deferred_routes` on, a deferred route -- an append or correction whose
binding did not hold, or a route the assessment deferred itself -- is not only
a receipt in `mind_event_routes`. It also leaves a row in
`mind_event_deferrals`: the target event, the members with the sources each
rests on, the route's evidence ids, a reason code (`binding-unverified`,
`target-missing`, `assessment-deferred`), how often it has been looked at
again and when it may be next; ids, codes and times only. A non-operational
memory context carries at most four that are due, oldest first, as
`pending_deferrals` (each member's current records as short excerpts of the
experience read), with the target event among the graph candidates; the
prompt then says what they are. A later route that places a member -- create,
append, correct or link -- uses it up, and a row with nothing left goes
(`event_deferrals_settled`). Each commit of an assessment that was shown a row
counts one look, an hour apart; after two looks without being placed it goes
(`event_deferrals_dropped`), as does one older than fourteen days. An erase
takes every row that names an erased source or record. The rows keep no
evidence references, so the slimming migration has nothing of them. Off, none
is written or shown and neither the memory context nor the prompt changes.

Membership is an active versioned `part_of` edge. Appending advances the event
revision. Corrections retain source records and create `corrects` edges. They do
not silently rewrite original speech or turn inferred records into explicit
facts. A route has a stable command ID and returns `undo_command_id`, consumable
by the graph's `undo` operation. Undo refuses to overwrite later edits.

`split_event` accepts an event ID, its expected revision, a proper subset of
`member_ids`, a new title, source evidence and command ID. It creates another
stable event and moves those membership edges. The graph also merges and undoes;
a bare `split` action is refused and names `split_event` for a division and
`undo` for a reversal. Merge aliases retain old event IDs.

## Versioned digests

Digests transition through `dirty`, `refreshing`, `ready` and `failed`. Changes
to members, record revisions, dependencies and graph receipts invalidate an
event and its ancestor topics. A 30-second debounce coalesces message bursts,
with a five-minute maximum postponement.

The digest contains sourced narrative, conclusions, pending work, corrections
and unresolved points. Occurrence, receipt and summary times stay distinct.
Its input hash includes graph nodes/edges, member revisions, source versions and
the digest-rule version. A model request runs outside the write transaction;
the commit compares the complete hash and dirty generation again. A concurrent
message therefore invalidates the result instead of being overwritten.

A single short source can be projected in full without a model. Multi-source
summaries use DeepSeek high with a 65,536-token output ceiling and truncation
checks. Large evidence sets are prepared in complete sourced batches. Invalid
citations, truncated output and incomplete preparation never replace the last
good digest. Every generated unit retains its sources' confirmation basis.

During refresh, a thread read returns current valid evidence and marks any old
derived summary as stale. Retracted, deleted and superseded evidence cannot
become current simply because an older summary still exists. A reverse source
index supports invalidation without scanning every graph object's JSON.

A summary is built from what happened, so the membership snapshot behind it is an
experience read. What the [reading purpose](architecture.md#reading-purpose-and-evidence-classes)
removes from that membership beyond the host envelopes enters the input hash
together with the rules version, so exactly those events become dirty and are
written again, while an event that loses only host envelopes keeps its signature
and is not rebuilt for nothing. A derived view is never corrected in place: before a
rebuild replaces it, the text it replaces is kept in `mind_isolation_archive`
under the rules version that retired it. While the [evidence isolation](operations.md#evidence-isolation)
migration is unfinished, a `ready` summary is reported as pending and never
served — not even as a stale one — because it was written under rules that are
still being applied.

## Retrieval interfaces

`read_continuity_context` / host `memory-context` accept optional
`mode=auto|light|deep`. Generic `RecallRequest.mode` passes through the Kin
adapter. Ordinary light retrieval uses the local path and prepared views. The
routing classifier asks for the deeper path when a message needs old events,
open promises, versions, what was shared or contradictory evidence, and `auto`
on a read tool is deep; an explicit light mode stays local, and `auto` is
otherwise light unless the request asks for history.

Deep retrieval combines lexical matches, original user statements, Qwen vectors
and graph neighbors. Every lane asks one policy loaded for the
request, so a legacy host envelope, a role configuration or a self-claim is kept
out of an experience read and is returned, labelled, to an audit read.
A graph read applies the same rule to its frontier: a node the purpose does not
admit is not a bridge, so its neighbours are not reached through it, and
candidates are filtered before the limit so a hidden projection cannot take a
visible node's place. Edges follow their own evidence. A node that projects a
record carries no text of its own, so a reader falls back to that record's
content; the fallback is taken only when this read may see the record, and the
node's basis then follows the record's class rather than the node's, which is
what stops a whole role agreement leaving under a node's own basis.
`graph-detail` is a read by id and therefore an audit read, carrying the node's
`evidence_class`.
Raw-message/event wrappers share a candidate rather than consuming two slots.
DeepSeek selects temporal neighbors through structured `followups`, including a
provided candidate ID and a before/after/both direction. Whole nearby original
turns are returned for its next judgment, without topical keyword scoring.
Temporal neighbors are retrieval candidates only, never evidence of event identity.

The merged candidate pool is capped at 40, the optional DeepSeek ranker receives
up to 24 per call, and the first page expands up to eight. Subsequent rounds
retain the best eight and review previously unseen candidates. Ranking uses complete bounded
source sentences, distinguishes an excerpt from the original, and only accepts
provided candidate keys. Explicit IDs retain priority. The model selects relevant
corrections and unfinished promises in `protected_ids`; word overlap cannot pin
an unrelated constraint above the answer's evidence.
There are at most three retrieval rounds and a 150-second total budget. Optional
reranking has a 30-second absolute deadline; embedding failure or invalid ranking
returns validated local evidence with a degradation reason. When no round's
ranking ever answered, the later rounds only reordered the same local evidence in
order to feed one, so their reshuffling may not demote what the first round led
with: the first round's leading eight stay in front, and whatever the later
rounds surfaced follows them. A degraded recall is therefore never ordered worse
than a recall that asked for no rerank at all. Every DeepSeek call on this path
uses high effort. No reasoning trace becomes a memory source.

Results expose mode used, rounds, candidate/evidence versions, pending IDs,
digest revisions, degradation reasons and separate retrieval/compression call
counts. `local_recall_ms` measures candidate lookup, excluding affect,
work-history and context rendering costs. Automatic chat background has an
800-token allowance, and the first chat context of a session window 2,000; with
`native_window_context` on, it takes what the selected material needs, up to
8,000 tokens and the room the native window reports.

`memory_context_read` counts digest/cache use, injection deduplication and missing
coverage. `event_digest_refreshed` records end-to-end refresh latency.
`structured_model_usage` records the provider request ID and token usage of each
structured DeepSeek call that returned a body and of the main appraisal call; the
memory service's own model roles record `model_tokens`, and summary-cache hits and
native cached-input tokens are separate measurements. A call the provider reported no
usage for writes `model_usage_unknown` and no token count at all: unreported usage
is never a zero. That covers timeouts, network errors, HTTP failures, a missing
tool call and an unverified model. A model role with no configured price reports
`cost_status: "unpriced"` through `model_cost_unknown` instead of a cost of 0.0.
The same rule governs the per-call records an appraisal attempt keeps in the
[attempt ledger](mobile-recovery.md#operator-recovery), and the usage row a Node
adapter reports for every call it makes.

`read_event_thread` accepts `detail=index|summary|original` and optional
`expected_revision`. Summary is the compatible default. Revision mismatch is an
explicit response. Complete originals paginate in sets of up to eight and retain
read URLs or continuation instructions when a source exceeds the page budget.
MCP reads accept `access_origin` and an idempotent `usage_id`; autonomous background
reads use `maintenance`. Reading only an index does not increase memory temperature.

## Maintenance and usage

The worker loop schedules jobs, with leases, renewal, retries and commit
fencing. Priority is source correction, live event digest, topic volumes, thermal
statistics, then historical backfill. While any host process holds an unexpired
shared foreground lease, a worker claims only the kinds that call no paid model —
embedding, parsing, vector builds, rebuilds and history erasure — and every other
job, source corrections included, waits for the lease to end. The phone host
renews its lease from actual session/task state. At most two event digests run
at once, never two for the same event, to limit optional model load.

The organizer's scheduling eligibility matches its executor: active,
nondeleted records at the indexed revision. Work deduplication uses eligible
input versions; unrelated metrics or configuration changes cannot cause empty
organization loops.

Daily thermal maintenance is due at 03:30 Asia/Singapore. Topic integration is
due on Wednesday/Sunday at 04:00. The most recent missed slot is picked up while
nobody is interacting. Unchanged
topic inputs have a completion receipt without another organization job. Leiden
families supply candidates; only independently evidenced topic membership may
publish automatic volumes. Relevant candidate families enter the existing
appraisal with up to four families and twelve sourced members each. DeepSeek
judges topic organization from that evidence; the host does not turn cluster
overlap into event identity. Each volume revision retains source/event identities.

Usage has an origin and stable idempotency key. Explicit queries, accepted reply
references and evidenced follow-ups can update last use. Index hits, automatic
injection, migration and maintenance do not heat memories. Thermal state is
independent of validity and completion: hot <=30 days, warm <=90 days, cold >90
days; unknown dates remain warm. Current preferences, constraints and unfinished
commitments retain priority. Explicit and deep retrieval search every tier.

Use the host `lifecycle-status` and `lifecycle-backfill` actions for rollout
inspection and bounded historical seeding. Backfill never emits an access event.
The worker also persists its newest-first cursor and advances 100 historical
events per batch while the digest backlog is below 100. Cursor and dirty-state
updates commit together; a retry cannot skip a batch. Completion stops new jobs.

## Validation and rollback

Run `pytest`, adapter tests and the frozen private replay script. Keep private
utterances, record IDs, model credentials and results outside the repository:

```sh
PYTHONPATH=src python scripts/lifecycle_replay.py \
  --root /private/snapshot --cases /private/frozen-cases.json \
  --output /private/results.json --scope '<scope JSON>' --mode deep --models
```

The frozen set requires at least 48 cases. Every required source group must be
reached within eight candidates; exact duplicate channel imports are equivalent,
including a frozen complete utterance quoted in an ingestion note; generated
paraphrases are not. The cases file also carries `frozen_before_retrieval`.
Recall metrics measure source retrieval, not whether an answer is correct.
Evaluate delivered text separately.

Rollback disables the lifecycle switches and uses versioned graph undo where
needed; a switch from either table above is set to `false` on its own, as above.
Setting `recall_purpose_policy` to false does not delete a classification row or
a stamp — it stops them being read, so re-enabling it finds the same classes; a
store whose rows were written by the migration is taken back with that action's
own `--undo`, and neither step rewrites a legacy record.
Do not restore an old production database over new conversations or receipts.
Retain the pre-rollout SQLite backup and original source/vector storage. Automatic
volumes and summaries are derived views; source speech and delivery evidence
remain canonical.

## Semantic decisions and host verification

Model judgments make the semantic decisions, each with source references and a
concise reason. The combined appraisal decides event identity, corrections,
affect, exploration direction, sharing intent and session-maintenance advice;
recall asks for priority and follow-up reading, and a digest for its summary
content. These are DeepSeek high calls, except that the action lane's appraisal
runs on the main-session model when the host is configured for it; see
[Kin Mind](kin-mind.md#deepseek-and-memory).

The host verifies citations, current versions, scope, command identity, actual
execution/delivery receipts, deadlines, leases, budgets and owner-configured
hard constraints. Keyword/vector/graph search generates candidates; a similarity
score does not decide that two experiences are the same event. The 30/90-day
thermal tiers are fixed in code and the contact and work-lock rules are the
owner's configured host policy, while semantic choices are model judgments with
an auditable source.
