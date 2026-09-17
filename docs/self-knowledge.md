# Evidence-backed self-knowledge

MemoryPalace can retain an agent's role agreements, behavioral hypotheses and prospective checks as distinct records. A hypothesis remains an inference even when several reported outcomes support it. A role records an explicit agreement; it does not establish an observed trait or a conscious experience.

## Records and evidence

| Entry | Evidence | Meaning |
|---|---|---|
| Role claim | Explicit user source, through an active evidence record | A declared role or agreement |
| Behavioral hypothesis | Active, source-backed evidence records; model interpretations are allowed and remain labeled | A claim awaiting evaluation |
| Prediction | A current claim and its revision | A probability recorded before an observable outcome |
| Assessment | Later explicit user reports or operation records | A reported outcome, including an unresolved outcome |

All entries share the existing source, scope, revision and deletion mechanisms. Evidence must belong to the same project, persona, collection and world. Claims retain evidence revision numbers. Corrected, expired or retracted evidence prevents the current self-view from presenting its dependent claim as current. The historical view retains the record and its evidence status.

The caller supplies an `agent_version` identifying the configuration under assessment. Include changes to the model, instructions and relevant memory policy in that version. MemoryPalace cannot discover or attest the runtime configuration. A persona label alone should not imply that two different runtimes have the same behavior.

## MCP workflow

1. Retain the actual source with `receive_source`, unless a host hook has already recorded it. Use `source_evidence` to obtain its `record_ids` and inspect the original report with `read_memory`; that list can also contain derived claims. Each `evidence_ids` value below is a **record ID**, not a source ID.
2. Call `record_self_claim` with a stable command ID, aspect, context, configuration version and evidence. A `basis` of `hypothesis` creates an inferred, unverified record. A `basis` of `role` requires explicit user evidence.
3. Before an outcome exists, call `predict_self_behavior`. Define an observable behavior, identify the case, record the information available to the predictor, and give a probability between zero and one. An optional generic-agent estimate must concern the same case and information.
4. After the event, retain the outcome source and call `assess_self_prediction`. Supply `outcome: true`, `false` or `null` when the outcome is unresolved. A model's interpretation alone cannot serve as assessment evidence. The assessment itself remains an inferred interpretation of its sources.
5. Use `read_self_knowledge` with the current version and the relevant `aspect` or `context`. Inspect sources and counterexamples before deciding whether to revise the claim.

Example claim arguments, after the evidence record exists:

```json
{
  "scope": {"project": "personal", "persona": "researcher"},
  "claim": {
    "command_id": "source-checking-hypothesis-1",
    "aspect": "source checking",
    "context": "research answers",
    "agent_version": "research-config-1",
    "claim": "I tend to check a primary source before answering a research question.",
    "basis": "hypothesis",
    "evidence_ids": ["<retained-evidence-record-id>"]
  }
}
```

`predict_self_behavior` accepts `scope` and a `prediction` object containing `command_id`, `claim_id`, `expected_revision`, `case_id`, `behavior`, `information`, `probability` and optional `generic_probability`. Case IDs are unique within a scope and agent version. Changing the forecast after creation excludes it from prospective scoring, including after a rollback.

`assess_self_prediction` accepts `scope` and an `assessment` object containing `command_id`, `prediction_id`, `expected_revision`, `outcome`, `evidence_ids` and `note`. Outcome sources must have occurred after the forecast was recorded and before assessment. These timestamp checks cannot prove that a caller did not already know the answer, nor authenticate a caller's authority labels. Operational evidence should be ingested by a trusted host.

The Python entry point is `SelfKnowledge(engine, scope)` from `eventmem.core.self_knowledge`. Its `claim`, `predict`, `assess` and `view` methods back the MCP tools. This feature adds no HTTP REST routes or database migration.

## Current views and retained history

Current reads require `agent_version`. A history read can omit the version to inspect previous configurations. `aspect` and `context` filters select the relevant claims; records from unrelated contexts remain in the archive. A claim replacement specifies `supersedes` and `expected_revision`, and preserves the same aspect, context and basis. Other claims remain intact. Earlier revisions remain available through `memory_history` and `read_memory` with `known_at`.

Ordinary `recall_memory` does not inject these structured entries into the current context: it has no configuration-version selector, and its `history` option reaches earlier states of experience only. Use `read_self_knowledge` to request them. A host can also recall them over HTTP with `recall_purpose: "self_knowledge_view"` (or `"audit"`); each line and item then carries the class `self_knowledge` with the entry's basis and version, never the confirmation it was stored with. Setting the memory switch `recall_purpose_policy` to false restores the previous behaviour, in which `history` also admitted these entries. This does not change the treatment of legacy, unstructured self-narrative records.

`limit` bounds the most recent matching records inspected (default 50, maximum 100). `budget` bounds only the returned `text`, in tokens; it does not bound the JSON metadata envelope, source storage or total processing. Long entries become labeled read hints. `has_more` signals additional database matches; `omitted_for_budget` counts entries dropped after hint packing. A historical view shows current record states, including supersession; `known_at` reads reconstruct an earlier revision.

## Interpreting reported calibration

The response groups scores by `agent_version`. Scores use only the assessments returned by that read, including entries represented by read hints. Filters, the record limit and the text budget can therefore change the sample. This is a browsing summary, not a complete benchmark result.

For binary outcomes, the Brier score is the mean of `(probability - outcome)²`; lower values indicate better probability predictions. Paired scores use only cases with a generic-agent estimate. `paired_self_advantage` is generic Brier minus self Brier on those same cases. A positive number indicates lower reported error for the self estimate on that sample. It does not establish a statistically reliable advantage.

Unresolved outcomes, revised forecasts and stale evidence are excluded. Assessments that share an outcome-source hash are counted once per version, with the newest eligible assessment taking precedence. This avoids counting copies of a report as separate evidence; it does not establish statistical independence between different reports. Correct an existing assessment through the revision mechanism instead of creating a duplicate. Reported calibration never automatically verifies a hypothesis.

## Research basis and limits

[What Should an Agent Forget? Separating What Is Stored from What Is Used](https://arxiv.org/abs/2609.10263) motivates separating retained history from the evidence selected for a current question. MemoryPalace's versioned views and scoped replacement apply that distinction; they do not reproduce the paper's selector or its reported benchmark results.

[Strangers to Themselves: What Language Models Say About Themselves Is Generic](https://arxiv.org/abs/2609.09899) evaluates behavioral self-prediction and questions whether first-person reports convey agent-specific information. Prospective records, matched generic estimates and configuration-specific scores make that question inspectable here. They do not reproduce its experimental protocol or settle questions about consciousness.

These tools store inspectable claims and observations. They provide no detector for subjective experience, no guarantee of truthful self-report and no automatic personality learning. A host chooses when a behavioral check is useful; ordinary conversation need not become an evaluation session.
