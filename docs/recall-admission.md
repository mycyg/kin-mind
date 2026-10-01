# Relevance admission for the automatic context

What the host injects on its own before a chat reply or a proactive draft (host action
`memory-context`, `Contexts.build(automatic=True)`) can be held to relevance. Explicit reads --
`read_continuity_context`, `recall_memory`, the history tools, the console's recall lab -- are never
gated, and neither is any other purpose (startup, work).

All of it is off by default; unset, a build is exactly what it was.

## Settings (`configure-memory`)

| Setting | Default | Effect |
| --- | --- | --- |
| `recall_admission` | `"off"` | `"shadow"` decides and records, and injects what it always did. `"on"` injects only admitted recall items. |
| `recall_admission_threshold` | `0.55` | Cosine (0–1) a light candidate needs. |
| `recall_admission_quota` | `4` | Admitted recall items per build, 1–16, apart from the state items. |
| `recall_admission_timeout_ms` | `3000` | How long scoring may take, 200–20000; past it nothing is admitted. |
| `recall_quiet_marks` | `false` | Automatic contexts skip what the owner marked "不主动提起"; the marks can be set. |
| `context_usage_hint` | `false` | Adds `context.CONTEXT_USAGE_HINT` to the automatic context's envelope. |

## What is gated

Only recall items: lexical records, graph nodes and their one neighbour, works, shares and the deep
pool (archive-memory entries come only in explicit reads). Never gated: host runtime, intent, habits, affect, the trait ledger,
continuity manifests, constraint records, and an item named by its exact id (or the exploration a
draft is about). They pass as before.

## How an item is admitted

- **Light.** The resolved recall query and an excerpt of the item's own text near the query's words
  (at most 200 tokens, 600 characters) are embedded by the local embedding service
  ([local embeddings](local-embedding.md)); the item passes at the threshold. A remote embedding role
  is never asked on the reply's path (`embedding-not-local`), and a look asks for nothing
  (`session-required`).
- **Deep.** The RecallRanking selection (protected ids, then ids, of the last ranking that answered)
  is the admission. The ranked tail, which the explicit read keeps, is not admitted.
- **Pool.** At most 10 lexical records, 6 graph hits in lexical order (not findings first), one
  neighbour one hop from the best-ranked hit that has one, 3 works and 5 shares are scored; past that
  they are dropped as `pool`, unscored.
- **Order and quota.** Passing items are taken best first -- the ranking's order, then the score --
  at most the quota.
- **No refill.** What this window has already seen goes after the quota is taken, and nothing weaker
  takes its place.
- **Coverage.** A note (the appraisal's generated record) goes when a ready event digest whose sources
  include all of the note's sources is admitted beside it (the lower-ranked of the two goes) or was
  seen in this window.
- **Fail-closed.** No score is no admission: a service that is off, slow (`deadline`), busy
  (`capacity`), failing, remote, or a ranking that did not answer leaves only the exempt items, and the
  context adds `recall_admission.ADMISSION_MORE_NOTE`, one line saying more can be read with the
  tools.

Admitted items stand where the first recall item stood; the state items keep their page of 16.

## What is kept

- `mind_recall_observations`: one row per gated build -- purpose, mode, setting, state and reason
  code, each candidate's id, revision, route and score, the admitted and exempt ids, the dropped ids
  with their reason, timings, and in shadow the recall ids actually injected. Ids and numbers only:
  no text, no query, no metadata. The newest 500 per scope.
- `mind_recall_vectors`: an item's vector, keyed by the hash of the model and the scored text, beside
  the item's id, so an unchanged item is not embedded again (each memory-context is its own process).
  The newest 4,000 per scope. Query vectors and scores are never stored.
- `mind_recall_quiet`: the marks. A mark set by the main session keeps 小光's message as its evidence
  (`evidence_refs`, group `records`) and a reason; one set in the console keeps neither.

An erase deletes every observation naming anything it reached, every vector kept for it, and a mark
on an erased item. A mark whose evidence is erased keeps the item quiet and loses the evidence's words
(the plain scrub: a tombstone reference, `reason` blanked).

## "不主动提起"

With `recall_quiet_marks` on, an automatic context leaves out a marked record, graph node, work or
share, and the records a marked graph node holds; an explicit read still returns them. A mark is set
or cleared only

- by the main session's tool `set_memory_quiet` (description `recall_admission.QUIET_TOOL_DESCRIPTION`),
  which needs 小光's own current message as `evidence_ids` -- checked as a conversation habit's
  evidence is (`evidence_classes.owner_statement`, fresh) -- and a reason; idempotent by `command_id`;
- or by 小光 in the console (`POST /v1/recall/quiet`).

Both refuse while the setting is off. The host offers the tool only once its name is in
`chat-permissions.json`.

## Console

"自动带入" lists the newest builds: when, purpose, mode, setting, state, scoring time, the titles of
what was admitted (as the store titles them now) with the "不主动提起" toggle, and what was dropped,
counted by reason (`GET /v1/recall/admissions`). A second panel lists the current marks.

## Calibration

`echo '{}' | python -m kin_mind.host --config <mind-config.json> recall-admission-calibrate`
(optionally `{"thresholds": [0.5, 0.55, 0.6], "quota": 4}`) replays the kept observations under other thresholds and reports numbers
only: score percentiles overall and by route, scoring time, and per threshold the mean passing and
admitted items, the builds with none and the builds over the quota. Run shadow for some days, pick the
threshold, then switch `on`.
