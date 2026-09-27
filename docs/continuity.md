# Expression, concerns and interaction-led rhythm

Kin Mind connects source-backed state to response style and preserves the issues
behind contact wishes. The four optional features are `interpretation`,
`concerns`, `expression` and `rhythm`. Existing installations have them disabled
until a sourced configuration change enables them.

## One assessment, one transaction

The existing DeepSeek Flash/high request can return `understanding`, `concerns`
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

## Derived layers

Emotion v2b (`affect_layers.py`, `affect-layers-v1`) adds four layers that are
computed locally from the committed scores and the rhythm. None calls a model,
none is evidence, and none feeds a score or the expression compiler; they are
shown beside `expression`.

- **Undertone (心境).** Each dimension has a slow value `m` that follows its
  instant curve `x(t)` (the projection above) as `dm/dt = (x − m)/τ`, `τ = 24 h`.
  Between two changes of a curve it has a closed form,
  `m(t) = T + (m₀ − T)e^{−t/τ} + (x₀ − T)(e^{−λt} − e^{−t/τ})/(1 − λτ)`, taken in
  pieces at a motivation's end and through its limit where `λτ = 1`. It is stored
  only where a curve changes: `Mind._save`, which every writer calls, re-anchors a
  dimension whose curve moved at the value the old curve had brought it to, as
  `{m, x, at, fp}` under the top-level `affect_layers` key rather than in the
  dimension entries. Reads never write, a save that moved no curve anchors
  nothing, and a replayed or record-only command never saves. An hour's spike
  barely stirs the undertone; a mood held all day moves it.
- **Feeling (心绪).** At most two words from a fixed table, for the strongest
  weighted leanings of the instant values away from their baselines (雀跃,
  踏实, 有点闷, 心烦, 有点酸, 不安, 想念, 好奇 and a few more); 平静 when none
  reaches the threshold. The undertone is named from the same table with gentler
  thresholds.
- **Lingering (余韵).** An event that moves a dimension by 12 or more against the
  value projected just before it leaves an echo: which dimensions moved, which
  way, how far and when, with no words or IDs. The largest move is named from a
  table by dimension and direction while it fades (1.5-hour half-life, at most six
  hours).
- **Vitals (心跳/呼吸).** A virtual heart rate from 70 bpm plus weighted
  deviations of arousal dimensions (fear, irritability, flirtation, expressive
  energy, anticipation, joy, wonder) less contentment and security, alertness,
  the rhythm phase (resting −12 … roused +4) and a small local-hour table,
  clamped to 50–130; breathing follows it within 8–26. A rhythm that is disabled,
  forming or awaiting review contributes nothing, and `status` says so.

A state kept before these layers gains them once at host start-up
(`ensure_affect_layers`, one `affect-layers-added` revision); until then each
undertone reads `forming` at its instant value. The main session's `affect`
item carries the words and a pulse rounded to 5 bpm (breathing to 2), so its
revision does not move with every minute of a decaying curve. No source or
record ID enters the block.

## Interfaces and rollout

| Interface | Addition |
|---|---|
| `read_affective_state(scope, history=0, query="")` | `continuity`, `appraisal_summary`, `concerns`, `selected_concerns`, `rhythm`, `expression`, `affect_layers` and each dimension's `undertone` |
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
