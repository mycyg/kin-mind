# Memory-backed autonomous planning

Kin forms goals and reviews actions with DeepSeek Flash / high. Memories, four recent timestamped public turns, unresolved matters, artifacts, exploration outcomes, sharing receipts, plans and available capabilities enter the existing appraisal. A score is context, never an execution threshold. A missing or stale decision waits for review.

## Decisions, plans and execution

The `semantic_actions` flag replaces the legacy 75-point contact/exploration gates at candidate selection, claim and pre-send validation. Quiet hours, current contact preferences, user work priority, channel permissions and source/version validation remain authoritative. Normal chat reuses the existing input classifier's `recall` decision; it does not add a fixed model call. Optional memory expansion is capped at three rounds and 150 seconds, with each reranker capped at 30 seconds. Optional failures return verified local evidence and explicit gaps.

Plans have stable IDs, revisions, goal, motivation, source references, next review time and a dependency graph of steps. Actors are `explore`, `create`, `contact` and `owner`. Each step records its goal, completion criterion, Singapore time window, prerequisites, inputs and optional `owner_request_id`. Long-term plans have no seven-day expiration. A time window opening or being missed triggers another review. Restart never authorizes accumulated catch-up messages.

DS proposes plan changes and `execute`, `wait`, `abandon`, `owner_accepted`, `owner_completed` or `owner_declined`. The host checks the supplied source manifest and expected revision in the appraisal transaction. Owner participation requires an actual owner source. A proposal, delivered invitation, acceptance and completion remain distinct. A model cannot settle Kin execution or manufacture a channel receipt.

Kimi receives a question plus actual memory excerpts, known findings, gaps and recent dialogue. The creator uses the configured work model in an isolated Codex CLI workspace, with a separate native execution receipt. It does not resume the phone session, load its MCP tools, send messages or write shared state. One creator and one explorer may run initially; owner work preempts them and preserves checkpoints. The host verifies artifact containment, size, hashes, inspectable content and a separate DS completion review. Delivery requires a subsequent DS decision selecting verified artifact hashes, followed by the existing durable channel outbox. An uncertain send is reconciled with the same ID.

## Storage, APIs and feature flags

Additive SQLite tables store plans/history, fenced runs, background model leases, procedural memory/history/trials, effective-use events and independent strength observations. Model calls run outside write transactions; source, revision and lease checks fence late results. Existing desires migrate by stable identity without granting execution.

| Interface | Contract |
| --- | --- |
| `read_autonomous_plans` / `GET /v1/autonomy/plans` | Scope, optional ID/status, pagination and history; returns revisions and waiting reasons. |
| `manage_autonomous_plan` / `POST /v1/autonomy/plans` | Idempotent `command_id`; create/update/pause/resume/reschedule/cancel. Changes require expected revision and sourced reason. |
| `read_procedure_memory` / `GET /v1/autonomy/procedures` | Sourced method candidates, validity and dependency review state. DS judges applicability. |
| Private `plan-claim/renew/result/interrupt/recover` | Host executor receipts; unavailable as model completion tools. Recovery requires stopped workers. |

Independent flags: `semantic_actions`, `autonomous_plans`, `creative_execution`, `usage_reinforcement`, `procedure_learning`, `reinforcement_ranking`. They default off. Existing lifecycle and temperature flags retain their meanings. Turn off the affected path to roll back; retain new chats, plan history, source records, files and delivery receipts.

## Effective use and learned methods

Effective use is counted for explicit queries, actual reply references and verified task results. Deduplication spans channels and origins by authenticated input ID; all steps of a plan share one use key. Candidate hits, automatic background injection, maintenance, summaries and planning do not heat memory. Decayed frequency has a 30-day half-life and `log1p` strength. It does not alter confidence or revive corrected facts.

Frequency collection and ranking are separate. The new weight version starts its own seven-day observation. Ranking cannot be enabled using an older temperature trial: seven consecutive actual observation days, seven full elapsed days, version-matched replay evidence and an owner decision are required. Shadow metrics record proposed ranking differences while production order remains unchanged.

A procedure preserves applicability, steps, tool/environment versions, success criteria, counterexamples and actual outcome sources. It remains a candidate until two distinct recorded cases pass isolated replay. External effects use existing genuine receipts; tests never resend phone content. Source changes, counterexamples, agent configuration changes or unavailable environment versions prevent current execution. Methods cannot edit persona or permissions.

## Concurrency, accounting and validation

Keyword, vector and graph candidates are read concurrently. Different event digests can run in parallel, with one valid generation per event. Shared database leases cap background DS concurrency at two; foreground requests bypass this background queue. User work prevents claiming new low-priority jobs. Structured decision caches include input/schema/prompt identity and database generation, expire after five minutes, and are invalidated by new state or sources. Reused decisions do not claim new token consumption. Timed-out requests retain unknown usage; late usage receipts remain separate from effective results.

Run `pytest -q` and `node --test adapters/*.test.mjs`. Synthetic planning cases cover long horizons, time windows, dependency cycles, owner participation, changes, restart, concurrency, partial delivery and unavailable models. Private release validation uses a frozen 48-case history set and a separate real-model planning replay. Required history gates remain 21/21 critical cases and at least 44/48 top-eight hits. Measure warm local candidate P95 separately from network/model latency and total token cost. Keep private chats, replay inputs, credentials and plans outside Git.

The engineering references inform design, not a claim of measured Kin savings: [CoALA](https://arxiv.org/abs/2309.02427), [Mem0](https://arxiv.org/abs/2504.19413), and [Snowflake's agent context layer](https://www.snowflake.com/en/blog/agent-context-layer-trustworthy-data-agents/). Original evidence, versioned views and reversible state remain the system's authority.
