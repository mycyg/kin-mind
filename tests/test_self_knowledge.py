from __future__ import annotations

import json

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing
from eventmem.core.mcp import create_mcp
from eventmem.core.models import RecallRequest, RevisionInput, Scope, SourceInput
from eventmem.core.retrieval import tokens
from eventmem.core.self_knowledge import (
    AssessmentInput,
    ClaimInput,
    PredictionInput,
    SelfKnowledge,
)


@pytest.fixture
def memory(tmp_path):
    return SelfKnowledge(Engine(tmp_path), Scope(persona="researcher"))


def evidence(memory, key="initial", authority="explicit", **changes):
    source = memory.engine.receive(
        SourceInput(
            namespace="self-test",
            key=key,
            scope=memory.scope,
            text=f"Reported evidence {key}",
            authority=authority,
            **changes,
        )
    )
    return next(
        rid
        for rid in memory.engine.source(source["id"])["record_ids"]
        if not memory.engine.get(rid)["evidence_ids"]
    )


def claim(memory, **changes):
    return ClaimInput.model_validate(
        {
            "command_id": "claim",
            "aspect": "uncertainty",
            "context": "research",
            "agent_version": "config-1",
            "claim": "I seek evidence before answering.",
            "evidence_ids": [evidence(memory)],
            **changes,
        }
    )


def forecast(memory, row, case="case-1", **changes):
    return PredictionInput.model_validate(
        {
            "command_id": f"forecast-{case}",
            "claim_id": row["id"],
            "expected_revision": row["revision"],
            "case_id": case,
            "behavior": "The answer cites a primary source.",
            "information": "A research question that has not yet been answered.",
            "probability": 0.8,
            "generic_probability": 0.5,
            **changes,
        }
    )


def assessment(memory, prediction, **changes):
    return AssessmentInput.model_validate(
        {
            "command_id": "assess-" + prediction["id"],
            "prediction_id": prediction["id"],
            "expected_revision": prediction["revision"],
            "outcome": True,
            "evidence_ids": [evidence(memory, prediction["id"], "operation")],
            "note": "The host reports a primary-source citation in the answer.",
            **changes,
        }
    )


def score(memory, version="config-1", **changes):
    return memory.view(agent_version=version, **changes)["calibration"][
        "by_agent_version"
    ][version]


def correct(memory, row, **changes):
    return memory.engine.revise(
        row["id"],
        RevisionInput(
            expected_revision=row["revision"],
            command_id="correct-" + row["id"],
            action="correct",
            content="Corrected evidence",
            **changes,
        ),
    )


def test_roles_and_hypotheses_keep_their_epistemic_status(memory):
    role = memory.claim(claim(memory, basis="role", command_id="role"))
    hypothesis = memory.claim(claim(memory))
    assert (role["generated"], role["confirmation"], role["status"]) == (
        False,
        "explicit",
        "active",
    )
    assert (
        hypothesis["generated"],
        hypothesis["confirmation"],
        hypothesis["status"],
    ) == (True, "inferred", "unverified")
    view = memory.view(agent_version="config-1")
    assert "hypothesis unverified inferred" in view["text"]
    assert "role active explicit" in view["text"]
    # Unqualified recall cannot inject claims about another agent configuration.
    recalled = memory.engine.recall(
        RecallRequest(scope=memory.scope, query="seek evidence")
    )
    assert (
        hypothesis["id"] not in recalled["text"] and role["id"] not in recalled["text"]
    )
    # `history` reaches earlier states of experience. It no longer admits self-knowledge:
    # that takes the purpose of the read, and the line then carries its class.
    historical = memory.engine.recall(
        RecallRequest(scope=memory.scope, query="seek evidence", history=True)
    )
    assert (
        hypothesis["id"] not in historical["text"]
        and role["id"] not in historical["text"]
    )
    view = memory.engine.recall(
        RecallRequest(
            scope=memory.scope,
            query="seek evidence",
            history=True,
            recall_purpose="self_knowledge_view",
        )
    )
    assert "[self_knowledge hypothesis config-1]" in view["text"]
    assert "[self_knowledge role config-1]" in view["text"]
    assert "explicit" not in view["text"]


@pytest.mark.parametrize("authority", ["model", "operation", "document"])
def test_role_needs_an_explicit_user_source(memory, authority):
    rid = evidence(memory, authority, authority)
    with pytest.raises(Conflict):
        memory.claim(claim(memory, basis="role", evidence_ids=[rid]))


def test_replacement_is_scoped_and_history_remains_readable(memory):
    old = memory.claim(claim(memory))
    unrelated = memory.claim(claim(memory, command_id="other", context="conversation"))
    role = memory.claim(claim(memory, command_id="role", basis="role"))
    replacement = claim(
        memory,
        command_id="revised",
        supersedes=old["id"],
        expected_revision=1,
        claim="I sometimes omit source checks.",
    )
    for changes in (
        {"aspect": "different"},
        {"context": "other"},
        {"basis": "role"},
        {"expected_revision": 2},
    ):
        with pytest.raises(Conflict):
            memory.claim(replacement.model_copy(update=changes))
    new = memory.claim(replacement)
    assert memory.claim(replacement)["id"] == new["id"]
    current = {r["id"] for r in memory.view(agent_version="config-1")["items"]}
    assert old["id"] not in current
    assert {new["id"], unrelated["id"], role["id"]} <= current
    assert memory.engine.history(old["id"])[-1]["data"]["content"] == old["content"]
    history = memory.view(history=True)
    assert any(
        r["id"] == old["id"] and r["status"] == "superseded" for r in history["items"]
    )
    assert memory.claim(claim(memory))["status"] == "superseded"
    with pytest.raises(Conflict):
        memory.claim(claim(memory, claim="Changed intent using the old command ID"))


def test_source_deletion_removes_entries_and_retry_cannot_resurrect(memory):
    request = claim(memory)
    row = memory.claim(request)
    memory.engine.delete(row["source_ids"][0])
    assert memory.view(history=True)["items"] == []
    with pytest.raises(Missing):
        memory.claim(request)


def test_paired_prospective_scores_do_not_promote_a_hypothesis(memory):
    row = memory.claim(claim(memory))
    prediction = memory.predict(forecast(memory, row))
    request = assessment(memory, prediction)
    result = memory.assess(request)
    assert memory.assess(request)["id"] == result["id"]
    measured = score(memory)
    assert measured["assessed_cases"] == measured["paired_cases"] == 1
    assert measured["reported_brier"] == pytest.approx(0.04)
    assert measured["paired_generic_brier"] == pytest.approx(0.25)
    assert measured["paired_self_advantage"] == pytest.approx(0.21)
    assert memory.engine.get(row["id"])["status"] == "unverified"
    assert result["confirmation"] == "inferred"
    with pytest.raises(Conflict):
        memory.predict(forecast(memory, row, command_id="duplicate-case"))
    with pytest.raises(Conflict):
        memory.assess(request.model_copy(update={"command_id": "duplicate-assessment"}))


def test_unknown_outcomes_and_missing_generic_baseline(memory):
    row = memory.claim(claim(memory))
    first = memory.predict(forecast(memory, row, generic_probability=None))
    memory.assess(assessment(memory, first))
    second = memory.predict(forecast(memory, row, case="unknown"))
    memory.assess(assessment(memory, second, outcome=None))
    measured = score(memory)
    assert measured["assessed_cases"] == 1
    assert measured["paired_cases"] == 0 and measured["paired_self_advantage"] is None


@pytest.mark.parametrize("bad_evidence", ["model", "past", "future", "scope"])
def test_assessment_rejects_unusable_evidence(memory, bad_evidence):
    row = memory.claim(claim(memory))
    prediction = memory.predict(forecast(memory, row))
    if bad_evidence == "past":
        rid = evidence(memory, "past", "operation", occurred_at="2000-01-01T00:00:00Z")
    elif bad_evidence == "future":
        rid = evidence(
            memory, "future", "operation", occurred_at="2099-01-01T00:00:00Z"
        )
    elif bad_evidence == "scope":
        rid = evidence(SelfKnowledge(memory.engine, Scope(persona="other")))
    else:
        rid = evidence(memory, "model", "model")
    with pytest.raises(Conflict):
        memory.assess(assessment(memory, prediction, evidence_ids=[rid]))
    assert score(memory)["assessed_cases"] == 0


@pytest.mark.parametrize("target", ["forecast", "outcome"])
def test_corrections_exclude_stale_scores(memory, target):
    row = memory.claim(claim(memory))
    prediction = memory.predict(forecast(memory, row))
    request = assessment(memory, prediction)
    memory.assess(request)
    corrected = (
        prediction
        if target == "forecast"
        else memory.engine.get(request.evidence_ids[0])
    )
    correct(memory, corrected)
    assert score(memory)["assessed_cases"] == 0
    if target == "forecast":
        with pytest.raises(Conflict):
            memory.assess(
                request.model_copy(update={"command_id": "new", "expected_revision": 2})
            )


def test_changed_claim_evidence_requires_review_before_new_predictions(memory):
    row = memory.claim(claim(memory))
    rid = next(iter(row["attributes"]["self_knowledge"]["evidence_revisions"]))
    correct(memory, memory.engine.get(rid))
    assert memory.view(agent_version="config-1")["items"] == []
    with pytest.raises(Conflict):
        memory.predict(forecast(memory, row))


def test_reused_sources_do_not_inflate_case_count(memory):
    row = memory.claim(claim(memory))
    first = memory.predict(forecast(memory, row))
    second = memory.predict(forecast(memory, row, case="case-2"))
    rid = evidence(memory, "shared", "operation")
    memory.assess(assessment(memory, first, evidence_ids=[rid]))
    memory.assess(assessment(memory, second, evidence_ids=[rid]))
    assert score(memory)["assessed_cases"] == 1


def test_versions_and_context_filters_prevent_mixed_calibration(memory):
    for version, probability in [("config-1", 0.8), ("config-2", 0.2)]:
        row = memory.claim(claim(memory, command_id=version, agent_version=version))
        prediction = memory.predict(
            forecast(memory, row, case=version, probability=probability)
        )
        memory.assess(assessment(memory, prediction))
    with pytest.raises(ValueError):
        memory.view()
    current = memory.view(agent_version="config-2")
    assert all(
        r["self_knowledge"]["agent_version"] == "config-2" for r in current["items"]
    )
    groups = memory.view(history=True)["calibration"]["by_agent_version"]
    assert groups["config-1"]["reported_brier"] == pytest.approx(0.04)
    assert groups["config-2"]["reported_brier"] == pytest.approx(0.64)
    assert memory.view(agent_version="config-1", context="other")["items"] == []
    assert memory.view(agent_version="config-1", aspect="other")["items"] == []


def test_view_budget_keeps_typed_read_hints_and_retained_content(memory):
    row = memory.claim(claim(memory, claim="Evidence " * 400))
    view = memory.view(agent_version="config-1", budget=128)
    assert "hypothesis unverified inferred" in view["text"]
    assert "hint; read" in view["text"]
    assert view["tokens"] == tokens(view["text"]) <= 128
    assert memory.engine.get(row["id"])["content"] == row["content"]


@pytest.mark.asyncio
async def test_mcp_executes_the_self_knowledge_cycle(memory):
    server = create_mcp(memory.engine)
    names = {tool.name for tool in await server.list_tools()}
    assert {
        "record_self_claim",
        "predict_self_behavior",
        "assess_self_prediction",
        "read_self_knowledge",
    } <= names

    async def call(name, **payload):
        result = await server.call_tool(
            name, {"scope": memory.scope.model_dump(), **payload}
        )
        return json.loads(result[0].text)

    row = await call("record_self_claim", claim=claim(memory).model_dump())
    prediction = await call(
        "predict_self_behavior", prediction=forecast(memory, row).model_dump()
    )
    await call(
        "assess_self_prediction", assessment=assessment(memory, prediction).model_dump()
    )
    view = await call("read_self_knowledge", agent_version="config-1")
    assert view["calibration"]["by_agent_version"]["config-1"]["assessed_cases"] == 1
