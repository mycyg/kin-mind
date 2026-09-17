"""One registry for every static conflict message the host raises on the commit path.

`kind` says what moved. A **semantic** conflict is a property of the proposal itself, so the
same proposal fails the same way however often it is retried; a **runtime** conflict is a
version that changed under it; **unknown** is anything the host has not classified yet.

`handling` says what a retry may do with it:
  reuse     a later attempt may reuse the stored proposal (Tier A/B of WP4)
  block     the host decides alone; never sent back to the model for review
  terminal  no retry can succeed; the row finishes in an existing terminal state
  section   raised inside a stage-1 isolated section, so it is refused on its own

Classification order: the keywords at the raise site, then this registry keyed by the
**static message literal**, then `unknown` — conservatively counted and never reusable.
"""

from __future__ import annotations

from typing import NamedTuple

from eventmem.core.db import Conflict, Missing

KINDS = ("semantic", "runtime", "unknown")
HANDLINGS = ("reuse", "block", "terminal", "section")

# Modules the appraisal commit path runs through. A new Conflict/Missing literal in any of
# them must be registered below, or test_conflict_taxonomy fails.
COMMIT_PATH_MODULES = (
    "kin_mind.appraisal", "kin_mind.state", "kin_mind.graph", "kin_mind.memory",
    "kin_mind.lifecycle", "kin_mind.continuity", "kin_mind.plans", "kin_mind.habits",
    "kin_mind.sharing", "kin_mind.procedures", "kin_mind.exploration_decisions",
    "eventmem.core.engine", "kin_mind.model_lanes", "kin_mind.revalidation",
    "kin_mind.evidence_classes", "kin_mind.traits",
)


class Classification(NamedTuple):
    kind: str
    code: str | None
    handling: str


UNCLASSIFIED = Classification("unknown", None, "block")

# Static message literal -> (kind, code, handling). Several messages may share one code:
# the kind is then the same for all of them, while the handling follows the raise site
# (the same fault is refused alone inside an isolated section and blocks outside one).
REGISTRY = {
    # --- appraisal.py: the evaluation's own preparation and commit guards ---
    "Migration requires current owner evidence": ("semantic", "migration-needs-owner-evidence", "block"),
    "Migration source set needs a bounded batch": ("semantic", "migration-batch-unbounded", "block"),
    "Exploration result changed after enqueue": ("runtime", "exploration-target-changed", "terminal"),
    "source-needs-review": ("runtime", "source-needs-review", "block"),
    "Appraisal lease no longer owns this proposal": ("runtime", "lease-lost", "block"),
    "Evaluated sources changed before commit": ("runtime", "root-evidence-changed", "block"),
    "Referenced semantic evidence changed before commit": ("runtime", "cited-evidence-changed", "reuse"),
    "Referenced interaction changed during evaluation": ("runtime", "cited-evidence-changed", "reuse"),
    "Recent interaction source needs review": ("runtime", "reference-needs-review", "reuse"),
    "Referenced graph identity changed during evaluation": ("runtime", "graph-node-changed", "reuse"),
    "Event identity evidence changed during preparation": ("runtime", "context-changed-in-preparation", "reuse"),
    "Topic candidate evidence changed during preparation": ("runtime", "context-changed-in-preparation", "reuse"),
    # An audited section whose module is not installed: refused alone, recorded, never asked again.
    "No module has claimed this section yet": ("semantic", "section-unavailable", "section"),
    # --- state.py: evidence, mind revision, policy authority, desires ---
    "Initialize the role profile before reading or changing state": ("semantic", "profile-uninitialized", "block"),
    "Evidence source is unavailable": ("runtime", "evidence-source-unavailable", "reuse"),
    "Evidence source belongs to another scope": ("semantic", "reference-cross-scope", "block"),
    "Evidence belongs to another scope": ("semantic", "reference-cross-scope", "block"),
    "Evidence must retain its source": ("semantic", "evidence-unsourced", "block"),
    "Future evidence cannot describe an observed state": ("semantic", "evidence-in-future", "block"),
    "Evidence is not current": ("runtime", "evidence-not-current", "reuse"),
    "This evidence was already appraised; use a source correction or new evidence": ("runtime", "evidence-already-appraised", "terminal"),
    "A profile already exists; initialization cannot reset it": ("semantic", "profile-exists", "block"),
    "Role initialization requires explicit source evidence": ("semantic", "insufficient-authority", "block"),
    "Mind revision changed; read current state before updating": ("runtime", "mind-revision-changed", "reuse"),
    "Autonomy policy requires explicit user evidence": ("semantic", "insufficient-authority", "block"),
    "Contact preferences require explicit user evidence": ("semantic", "insufficient-authority", "block"),
    "Behavior policy requires explicit user evidence": ("semantic", "insufficient-authority", "block"),
    "Personality was already evaluated today": ("runtime", "evolution-already-today", "block"),
    "Personality changes require three independent user interactions": ("semantic", "evolution-needs-interactions", "block"),
    "A current, version-matched prospective behavioral check is required": ("semantic", "evolution-needs-behavioral-check", "block"),
    "A personality reversion requires explicit user correction evidence": ("semantic", "reversion-needs-owner-evidence", "block"),
    "Evolution event is outside this scope or missing": ("semantic", "reference-cross-scope", "block"),
    "Reversion evidence must follow the change": ("semantic", "reversion-evidence-too-early", "block"),
    "A later personality revision exists; review it before reverting": ("runtime", "later-revision-exists", "block"),
    "This sharing decision already has a contact intent": ("semantic", "share-intent-exists", "section"),
    "This sharing decision already has another contact intent": ("semantic", "share-intent-exists", "section"),
    "Desire is outside this scope or missing": ("semantic", "desire-unknown", "section"),
    "Only exploration wishes have an exploration target": ("semantic", "wish-kind-mismatch", "section"),
    "Only contact wishes link a communication decision": ("semantic", "wish-kind-mismatch", "section"),
    "A finished desire stays in history; create a new desire": ("semantic", "desire-finished", "section"),
    "An unresolved contact attempt already exists": ("runtime", "contact-attempt-open", "block"),
    "Action appraisal is pending": ("runtime", "action-appraisal-pending", "block"),
    "The contact threshold or desire is no longer current": ("runtime", "contact-threshold-changed", "block"),
    "Contact attempt is outside this scope or missing": ("semantic", "reference-cross-scope", "block"),
    "Accepted delivery cannot be rewritten": ("semantic", "delivery-accepted", "block"),
    "A possible send requires reconciliation, not cancellation": ("semantic", "send-needs-reconciliation", "block"),
    # --- graph.py ---
    "Graph evidence is missing or needs review": ("runtime", "cited-evidence-not-current", "reuse"),
    "Graph evidence was not part of this evaluation": ("semantic", "evidence-out-of-bounds", "block"),
    "Graph reference crossed memory scopes": ("semantic", "reference-cross-scope", "block"),
    "An event cannot provide its own relationship evidence": ("semantic", "self-reference", "block"),
    "Causal claims need a stated evidence basis": ("semantic", "causal-basis-required", "block"),
    "Graph keys must be unique": ("semantic", "duplicate-keys", "block"),
    "Graph node changed after evaluation": ("runtime", "graph-node-changed", "reuse"),
    "Graph kind is immutable": ("semantic", "graph-kind-immutable", "block"),
    "A graph node cannot own itself": ("semantic", "self-reference", "block"),
    "Graph command ID reused with other data": ("runtime", "payload-changed", "block"),
    "Graph revision changed": ("runtime", "graph-revision-changed", "reuse"),
    "Invalid graph merge": ("semantic", "graph-merge-invalid", "block"),
    "Merge target changed": ("runtime", "graph-merge-target-changed", "reuse"),
    "Only event membership can be split": ("semantic", "graph-split-unsupported", "block"),
    "Split members must be a proper subset": ("semantic", "graph-split-invalid", "block"),
    "Split identity already exists": ("semantic", "graph-split-exists", "block"),
    "Affected graph objects changed since the command": ("runtime", "graph-undo-changed", "reuse"),
    "Explicit claims need owner evidence": ("semantic", "insufficient-authority", "block"),
    # --- memory.py ---
    "Cooling needs seven full days of shadow observation": ("semantic", "cooling-needs-observation", "block"),
    "Cooling needs a current successful replay validation": ("semantic", "cooling-needs-validation", "block"),
    "Runtime event id belongs to different data": ("runtime", "payload-changed", "block"),
    "Conflicting runtime event": ("runtime", "runtime-event-conflict", "block"),
    "Observed artifact version changed before ingestion": ("runtime", "artifact-version-changed", "block"),
    "Runtime evidence needs review": ("runtime", "reference-needs-review", "reuse"),
    "Creator provenance requires a host operation receipt": ("semantic", "insufficient-authority", "block"),
    "Bubble content changed under the same ID": ("runtime", "payload-changed", "block"),
    "Accepted bubble cannot change platform ID": ("semantic", "delivery-accepted", "block"),
    "Semantic evidence is outside the evaluated source set": ("semantic", "evidence-out-of-bounds", "block"),
    "Cited semantic evidence is no longer current": ("runtime", "cited-evidence-changed", "reuse"),
    "Memory note keys must be unique in an assessment": ("semantic", "duplicate-keys", "block"),
    "Memory and graph keys must be unique in an assessment": ("semantic", "duplicate-keys", "block"),
    "Disclosure needs current delivery evidence": ("runtime", "reference-needs-review", "reuse"),
    "Graph reference needs review": ("runtime", "reference-needs-review", "reuse"),
    "Linked source needs review": ("runtime", "reference-needs-review", "reuse"),
    "Cross-scope reference": ("semantic", "reference-cross-scope", "block"),
    "Linked memory needs review": ("runtime", "reference-needs-review", "reuse"),
    # --- lifecycle.py ---
    "Membership closure exceeds bounded graph": ("unknown", "host-limit", "block"),
    "Event route keys must be unique": ("semantic", "duplicate-keys", "block"),
    "Event route command changed": ("runtime", "payload-changed", "block"),
    "Event route target changed": ("runtime", "event-target-changed", "reuse"),
    "An event cannot contain itself": ("semantic", "self-reference", "block"),
    "Event membership would create a cycle": ("semantic", "membership-cycle", "block"),
    "Event thread changed": ("runtime", "event-thread-changed", "reuse"),
    "Event member crossed scope": ("semantic", "reference-cross-scope", "block"),
    "Event summary cited unavailable evidence": ("semantic", "evidence-out-of-bounds", "block"),
    "Event changed during digest generation": ("runtime", "event-digest-changed", "reuse"),
    "Topic membership changed during organization": ("runtime", "topic-membership-changed", "reuse"),
    # --- continuity.py ---
    "Continuity configuration requires current owner evidence": ("semantic", "insufficient-authority", "block"),
    "Continuity proposal requires current source evidence": ("runtime", "cited-evidence-not-current", "reuse"),
    "Proposal cites evidence outside this appraisal": ("semantic", "evidence-out-of-bounds", "block"),
    "Concern feature is disabled": ("runtime", "feature-disabled", "block"),
    "Concern belongs to another scope or is missing": ("semantic", "reference-cross-scope", "section"),
    "A closed concern requires an explicit reopen with new evidence": ("semantic", "concern-closed", "section"),
    "Only a closed concern can reopen": ("semantic", "concern-not-closed", "section"),
    "Resolution requires a new outcome or correction source": ("semantic", "resolution-needs-new-evidence", "section"),
    "Owner participation needs a new explicit owner response": ("semantic", "insufficient-authority", "section"),
    "Explicit interpretation requires an explicit source": ("semantic", "insufficient-authority", "section"),
    "Sending a request does not resolve the awaited owner outcome": ("semantic", "request-not-outcome", "section"),
    "Unverified interpretation cannot resolve a concern": ("semantic", "unverified-cannot-resolve", "section"),
    "Linked concern is missing from this scope": ("semantic", "concern-unknown", "section"),
    "Linked concern requires review": ("runtime", "reference-needs-review", "reuse"),
    # --- plans.py ---
    "Plan is missing or outside this scope": ("semantic", "plan-unknown", "section"),
    "Plan evidence needs review": ("runtime", "cited-evidence-not-current", "reuse"),
    "Plan uses evidence not supplied to this evaluation": ("semantic", "evidence-out-of-bounds", "block"),
    "Plan evidence changed after it was supplied": ("runtime", "cited-evidence-changed", "reuse"),
    "Plan changed during evaluation": ("runtime", "plan-revision-changed", "reuse"),
    "Terminal plans preserve their history; create a new goal": ("semantic", "plan-terminal", "section"),
    "Executed or reserved steps cannot be overwritten": ("semantic", "step-executed", "section"),
    "Keep executed steps in plan history": ("semantic", "step-executed", "section"),
    "A command ID cannot be reused for a different change": ("runtime", "payload-changed", "block"),
    "Autonomous action requires a verified DeepSeek high decision": ("semantic", "insufficient-authority", "block"),
    "Decision plan revision is no longer active": ("runtime", "plan-revision-changed", "reuse"),
    "Step is not available for this decision": ("runtime", "step-unavailable", "reuse"),
    "Delivery selection requires host-verified artifacts from completed steps": ("semantic", "insufficient-authority", "block"),
    "Owner participation requires actual owner evidence": ("semantic", "insufficient-authority", "block"),
    "The host cannot execute on the owner's behalf": ("semantic", "insufficient-authority", "block"),
    "DeepSeek must explicitly account for each precondition": ("semantic", "preconditions-unmet", "section"),
    "Inconsistent plan decision base revision": ("runtime", "plan-decision-base-inconsistent", "section"),
    "Execution lease expired or was replaced": ("runtime", "execution-lease-lost", "block"),
    "Execution fence mismatch": ("runtime", "execution-fence-mismatch", "block"),
    "An executor outcome cannot be overwritten": ("semantic", "execution-outcome-final", "block"),
    "Late executor result is isolated": ("runtime", "execution-result-late", "block"),
    "Completion requires a verified host result source": ("semantic", "insufficient-authority", "block"),
    "Step has another execution": ("runtime", "step-has-execution", "block"),
    "Completion lost its current decision or evidence": ("runtime", "execution-decision-changed", "block"),
    "Recovery requires verified termination of previous executors": ("semantic", "insufficient-authority", "block"),
    # --- habits.py ---
    "Habit changes require current explicit owner evidence": ("semantic", "insufficient-authority", "block"),
    "Habit change cites evidence outside this evaluation": ("semantic", "evidence-out-of-bounds", "block"),
    "Habit command changed": ("runtime", "payload-changed", "block"),
    "Conversation preferences changed": ("runtime", "habits-revision-changed", "reuse"),
    "Reply choice must refer to a received owner input": ("semantic", "reply-input-unknown", "block"),
    "Autonomous silence has not been enabled by the owner": ("semantic", "insufficient-authority", "block"),
    "Merge target must be another received input": ("semantic", "reply-merge-invalid", "block"),
    "Reply choice already recorded for this input": ("runtime", "reply-choice-exists", "block"),
    # --- sharing.py ---
    "Share reference version is unavailable": ("runtime", "share-reference-unavailable", "reuse"),
    "Share reference needs review": ("runtime", "reference-needs-review", "reuse"),
    "Coverage mapping is outside evaluated delivery evidence": ("semantic", "evidence-out-of-bounds", "block"),
    "Coverage mapping refers to an unknown bubble": ("semantic", "bubble-unknown", "block"),
    # A reply body is frozen content, not a command: the same code covers the identical
    # freeze in reply_review.preflight, which raises it with explicit keywords.
    "Registered reply body changed": ("runtime", "reply-content-changed", "block"),
    # --- procedures.py ---
    "Procedure is missing or outside this scope": ("semantic", "procedure-unknown", "section"),
    "Artifact presence alone does not verify a method outcome": ("semantic", "insufficient-authority", "section"),
    "Method learning requires an actual verified result": ("semantic", "insufficient-authority", "section"),
    "Procedure command changed": ("runtime", "payload-changed", "block"),
    "Procedure changed during evaluation": ("runtime", "procedure-revision-changed", "reuse"),
    "Procedure learning is disabled": ("runtime", "feature-disabled", "block"),
    "Procedure needs review": ("runtime", "procedure-needs-review", "reuse"),
    "Procedure environment needs review": ("runtime", "procedure-needs-review", "reuse"),
    "Procedure dependency version changed": ("runtime", "procedure-dependency-changed", "reuse"),
    "Procedure has a failed counterexample": ("semantic", "procedure-counterexample", "block"),
    "Trial ran a different method revision": ("runtime", "trial-revision-mismatch", "block"),
    "External effects must use isolated validation and existing receipts": ("semantic", "insufficient-authority", "block"),
    "Trial dependencies are no longer current": ("runtime", "trial-dependencies-changed", "block"),
    "Trial identity cannot be reused": ("runtime", "payload-changed", "block"),
    "Procedure evidence changed before replay": ("runtime", "procedure-evidence-changed", "block"),
    "Need independent result cases": ("semantic", "replay-needs-cases", "block"),
    "Procedure replay omitted or duplicated an outcome": ("semantic", "replay-incomplete", "block"),
    "Procedure changed during replay": ("runtime", "procedure-changed-during-replay", "block"),
    # --- exploration_decisions.py ---
    "Sharing decision needs an exploration in this scope": ("semantic", "exploration-unknown", "block"),
    "A clock or delivery event cannot reopen a result decision": ("semantic", "decision-reopen-not-allowed", "block"),
    "Reconsideration requires a new sourced thought or observation": ("semantic", "reconsideration-needs-new-evidence", "block"),
    "This result has no current decision to communicate": ("runtime", "reference-needs-review", "reuse"),
    # --- core/engine.py ---
    "Idempotency key reused with different content": ("runtime", "payload-changed", "block"),
    "This source was explicitly deleted": ("runtime", "tombstoned", "block"),
    "Source changed: provide a new version": ("runtime", "source-changed", "block"),
    "Record id already belongs to different content": ("runtime", "payload-changed", "block"),
    "Source and record scopes differ": ("semantic", "reference-cross-scope", "block"),
    "Evidence and record scopes differ": ("semantic", "reference-cross-scope", "block"),
    "Revision": ("semantic", "revision-unknown", "block"),
    "Replacement must be another active record in the same scope": ("semantic", "replacement-invalid", "block"),
    "Cross-scope relations are not allowed": ("semantic", "reference-cross-scope", "block"),
    "Job key reused with a different task": ("runtime", "payload-changed", "block"),
    # --- traits.py: the ledger checks the material and the times, never the trait itself.
    # All of them are raised inside an audited section, so each is refused on its own, recorded
    # as a static code the next projection shows, and never asked again with a paid call. ---
    "Trait is missing or outside this scope": ("semantic", "trait-unknown", "section"),
    "Trait changed during evaluation": ("runtime", "trait-revision-changed", "section"),
    "Cited material cannot stand for the evidence class it was given": ("semantic", "trait-evidence-class", "section"),
    "A trait observation needs evidence of its own": ("semantic", "trait-evidence-missing", "section"),
    "Establishing on inference needs separate episodes and support that is not Kin's own": ("semantic", "trait-single-episode", "section"),
    "A trait decision names an observation the ledger does not hold": ("semantic", "trait-observation-unknown", "section"),
    "An owner instruction or correction must quote the owner's own words": ("semantic", "trait-quote-unverified", "section"),
    "A revoked trait needs an owner statement newer than its tombstone": ("semantic", "trait-revoked", "section"),
    "A faded trait needs new support before it stands again": ("semantic", "trait-needs-support", "section"),
    # --- model_lanes.py: a lease is the host's to check, never a question for the model ---
    "Model lease was lost before the result returned": ("runtime", "model-lease-lost", "block"),
    "Model lease was lost during the evaluation": ("runtime", "model-lease-lost", "block"),
    "Model lease was lost before commit": ("runtime", "model-lease-lost", "block"),
    "Appraisal row was taken over during the evaluation": ("runtime", "lease-lost", "block"),
}

# Codes that have no static message to look up: the caller substitutes them because only it
# knows which meaning a shared message has, the raise site formats its message, or the raise
# site is outside the commit-path modules and carries its code as a keyword.
CALLER_CODES = {
    # core/self_knowledge.py: repairing a supersede chain is the host's own operation, so it
    # is never sent back for review; the migration reports the refusal and retries by itself.
    "supersession-invalid": ("semantic", "block"),
    "supersession-revision-changed": ("runtime", "block"),
    # The evidence this evaluation exists to judge is gone: nothing is left to appraise.
    "root-evidence-unavailable": ("runtime", "terminal"),
    # `engine.command` refusing an appraisal's own key: another attempt already committed it.
    "already-committed": ("runtime", "terminal"),
    # engine.revise(), whose message carries the current revision.
    "record-revision-changed": ("runtime", "reuse"),
}

# code -> (kind, handling) for the callers that know only the code. Where one code is
# raised with two handlings, the code alone resolves to the stricter of them.
CODES = dict(CALLER_CODES)
for _message, (_kind, _code, _handling) in REGISTRY.items():
    if _kind not in KINDS or _handling not in HANDLINGS:
        raise RuntimeError("A registered conflict needs a known kind and handling")
    _known = CODES.get(_code)
    if _known and _known[0] != _kind:
        raise RuntimeError("One conflict code keeps one kind")
    CODES[_code] = (_kind, _handling if _known is None or _known[1] == _handling else "block")


def static_message(error):
    """The exception's text only when it is a literal of the code object that raised it.

    An identifier the model cited, a parsed value or a formatted message is never such a literal, so
    nothing of a proposal or its evidence can reach the queue row this way. One rule for every place a
    failure is written down: a refused section, a failed attempt and what the next attempt is told.
    """
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    text = str(error)
    return text if trace is not None and text in trace.tb_frame.f_code.co_consts else ""


def classify(error):
    """Keywords at the raise site first, then the registry, then a conservative unknown."""
    kind, code = getattr(error, "kind", None), getattr(error, "code", None)
    entry = UNCLASSIFIED
    if isinstance(error, (Conflict, Missing)):
        found = REGISTRY.get(static_message(error))
        if found:
            entry = Classification(found[0], found[1], found[2])
        elif code in CODES:
            entry = Classification(CODES[code][0], code, CODES[code][1])
    if code and code != entry.code:
        # An explicit keyword wins, including a code the caller substituted for the raise site's.
        known = CODES.get(code)
        entry = Classification(known[0] if known else entry.kind, code, known[1] if known else entry.handling)
    if kind in KINDS:
        entry = entry._replace(kind=kind)
    return entry


def reusable(error):
    """Only a classified conflict whose handling allows reuse may seed a later attempt."""
    found = classify(error)
    return found.kind != "unknown" and found.handling == "reuse"
