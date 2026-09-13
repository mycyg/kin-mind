# Expression, concerns and interaction-led rhythm

Kin Mind connects source-backed state to response style and preserves the issues
behind contact wishes. The four optional features are `interpretation`,
`concerns`, `expression` and `rhythm`. Existing installations have them disabled
until a sourced configuration change enables them.

## One assessment, one transaction

The existing DeepSeek Flash/max request can return `understanding`, `concerns`
and `rhythm` alongside scores, motivations and wishes. Understanding records a
short meaning, topic, importance, confidence, basis and source IDs. The bases
are `explicit`, `inferred` and `internal_thought`; an explicit interpretation
requires an explicit source. Inferred confidence below 0.65 is marked for review.
It cannot supply a confirmed relationship fact or an actionable linked concern.

`appraisal_summary` records the proposed and applied scores, projected prior
values and actual changes. Scores remain 0–100 with no uniform per-event step
limit. The original 20 dimensions and long-term personality validation remain.
Public structured results are parsed from the tool result; provider thinking
blocks and incidental text are not persisted as assessment output.

Scores, concern changes, wish links and audit snapshots commit in the existing
SQLite transaction. A bad link or an interrupted audit write rolls back all of
them. The durable appraisal queue continues to own retries and leases. Chat
enrichment uses the latest committed state and does not await this request.

## Concerns and wishes

A concern records what remains on the agent's mind: `care`, `anticipation`,
`curiosity`, `distress`, or `shared_plan`. It has a stable scoped ID, semantic
key, content, topic, target, intensity, basis, confidence and versioned evidence.

The lifecycle is `active`, `easing`, `resolved`, `archived`. Updates and reopening
use source evidence; resolution requires a new outcome or correction. Replaying
the activation source cannot resolve the concern. A fresh source may explicitly
reopen a closed concern and increment its recurrence count.

The first projection defaults are a 12-hour easing delay and a 48-hour intensity
half-life. These are engineering parameters, frozen in each concern revision,
not observations of psychological timing. Reads never move the assessment clock.
Silence can ease intensity but cannot mark the issue resolved.

Wishes contain `concern_ids` and the concern revisions they were assessed against.
Sending a wish completes that wish, not the concern. A changed or invalidated
concern makes its old linked wish require reassessment before contact. Reassessing
a wish acknowledges the current concern revision. Closing a stale wish remains
possible. Source-based evidence deduplication has its own durable table; removing
old items from a display does not make old summaries new evidence.

## Expression and rhythm

The local expression compiler covers all 20 dimensions and selects at most three
positive, short tendencies. It includes mixed longing/playfulness,
closeness/low-mood, flirtation/focus and curiosity/solitude. Each hint identifies
its source dimensions, evidence IDs and whether its basis is a role configuration
or event inference. Its fingerprint includes selected concern revisions and
changes when contributing sources need review.

The approved persona remains separate. `interactionView` is the common bounded
projection for ordinary chat and proactive drafts; it includes at most three
selected concerns. `read_affective_state(query=...)` can prioritize a topic and
returns the full current state when details are needed. Source material remains
data; operational expression instructions belong to the host.

Rhythm has no fixed bedtime or wake time. Real owner messages from the mobile
input namespace and authenticated host message receipts form 14-day activity
statistics. Consecutive messages no more than 30 minutes apart form one window;
each window contributes one start to the hourly distribution. Background events,
assistant messages and maintenance do not contribute. Fewer than three active
days or five windows is labeled `forming`, otherwise `observing`.
The interaction query uses a partial time index and a source-version index. It
reads timestamps and IDs rather than loading historical message metadata, and
uses the latest live source revision. Corrections and deletion update this view
through the database; there is no stale process-local activity cache.

DeepSeek can propose `awake`, `settling`, `drowsy`, `resting`, `roused`, or
`recovering`, with current alertness, a target and a 20/60/180-minute half-life.
The host projects that trajectory locally. A new owner message can rouse a resting
state; this is a role runtime inference, not a claim of biological sleep.
Rhythm influences cadence, never work completion or contact permissions.
The minute tick does not make a new model request merely to advance rhythm.

## Interfaces and rollout

| Interface | Addition |
|---|---|
| `read_affective_state(scope, history=0, query="")` | `continuity`, `appraisal_summary`, `concerns`, `selected_concerns`, `rhythm`, `expression` |
| `manage_concern(scope, request)` | Create/update/ease/resolve/reopen/archive with command ID, agent version, expected revision and source evidence |
| `manage_desire` | Optional concern IDs; missing field preserves existing links |
| DeepSeek `Appraisal` | Optional understanding, concern proposals and rhythm; `wish_updates` supports `link` |
| Trusted host `configure-continuity` | Independent feature flags plus `shadow`/`active` activation |
| Trusted host `migrate-continuity` | One durable bootstrap from current owner authorization and original sources of live wishes |

The bootstrap preserves prior scores, motivations and wish content/status. It
only links supported concerns and adds interpretation/rhythm data. Finished,
expired and abandoned wishes remain historical. A source set above the existing
50-source request bound requires an explicitly bounded migration batch.

`shadow` hides new expression and concern context from response projection and
does not enforce new concern revision dependencies at the contact boundary. It
allows migration results to be inspected before active rendering. Each feature
can subsequently be disabled without deleting its records. The shared native
conversation, work locks, quiet hours and original delivery identities remain.

Acceptance tests cover mixed expression, unresolved concerns after delivery,
source correction, replay beyond the display window, interaction grouping,
restart recovery, atomic failure, concurrent revision checks, migration,
reasoning isolation and asynchronous enrichment. Context size, model usage,
cache usage when returned by the provider and duration are measured separately;
smaller projected context is not by itself proof of better provider cache hits.
The assessment concern window holds at most 32 records, prioritizing the current
topic and linked wishes. Full concerns and durable evidence remain in storage.

## Inspiration

[AnimaFlux](https://github.com/baiyan09110/animaflux/tree/6d2d028a4c68d8c3ced86c04f0e975a7f6460825)
informed the separation of expression guidance, grounded concerns and rhythm.
Kin's implementation is written for its own transactional memory and delivery
architecture and does not depend on AnimaFlux. That project is distributed under
the PolyForm Noncommercial 1.0.0 license.
