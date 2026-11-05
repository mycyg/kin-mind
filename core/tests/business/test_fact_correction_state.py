"""A factual correction reaches the live inner state and the next contact candidate.

The transport answers a fixed synthetic proposal: this checks the effective contract and
real persistence/projections, not whether a paid model understands an ambiguous sentence.
"""
import json
from datetime import timedelta

import httpx
import pytest

from kin_mind import appraisal as A
from kin_mind.continuity import ContinuityConfig
from kin_mind.interaction_projection import interaction_projection
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind
from test_kin_mind import FakeReviewer

pytest_plugins = ("test_kin_mind",)

WRONG = "The owner carried the telescope for the visitor."
CORRECT = "Kin carried the telescope for the visitor; the owner did not."
CORRECTION = "Haha, you carried the telescope, not me. I was only watching!"


def assert_correction_contract(system):
    # The instructions must reach the actual request, including the operational-only lane.
    assert "事实更正必须优先于先前的推测" in system
    assert "避免只做 `ease` 却保留错误前提" in system
    assert '在 `wish_updates` 中使用 `action: "abandon"`' in system
    assert "将 `action` 标记为 `resolve`" in system
    assert "在同一轮中需要将相关状态一起修正" in system


@pytest.mark.parametrize("provider_class", [A.DeepSeek, A.NativeReview])
def test_both_assessment_providers_receive_the_correction_contract(provider_class):
    provider = provider_class.__new__(provider_class)
    for stimulus in ("interaction-batch", "idle-review"):
        assert_correction_contract(provider._system({"stimulus": stimulus, "operational_only": True}, None))
    # A history-only pass cannot change current inner state.
    assert '在 `wish_updates` 中使用 `action: "abandon"`' not in provider._system({"stimulus": "memory-enrichment"}, None)


@pytest.mark.parametrize("transition", ["update", "resolve"])
def test_correction_replaces_live_premises_and_reaches_next_contact(setup, monkeypatch, transition):
    mind, source, clock = setup
    mind.configure_continuity(ContinuityConfig(command_id="continuity", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("configuration-change")],
        features={"interpretation": True, "concerns": True, "expression": True}, reason="synthetic"))
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "operational_lanes": True})
    jobs = A.Appraisals(mind)
    first = source("ambiguous", "You will probably help the visitor with the telescope later.")
    old = A.Appraisal(reason="The mistaken interpretation", understanding={
        "meaning": WRONG, "topic": "telescope", "importance": 60, "confidence": .8,
        "basis": "inferred", "evidence_ids": [first]}, concerns=[{
        "action": "create", "key": "telescope-helper", "kind": "care", "content": WRONG,
        "topic": WRONG, "intensity": 65, "basis": "explicit", "confidence": .8,
        "evidence_ids": [first]}], wishes=[{
        "content": "Ask whether the owner is tired from carrying the telescope.",
        "topic": WRONG, "kind": "contact", "strength": 90, "ttl_hours": 24,
        "completion": "A chat", "concern_ids": ["telescope-helper"]}])
    initial = jobs.enqueue([first], "synthetic-v1")
    assert jobs.run_one(FakeReviewer(old), job_id=initial["id"])["state"] == "complete"
    before = mind.read()
    concern = next(c for c in before["concerns"] if c["key"] == "telescope-helper")
    old_wish = before["desires"][0]
    clock[0] += timedelta(minutes=2)
    correction = source("correction", CORRECTION)
    corrected = A.Appraisal(reason="Correct the actor while keeping the playful exchange", understanding={
        "meaning": CORRECT, "topic": "telescope", "importance": 60, "confidence": .95,
        "basis": "explicit", "evidence_ids": [correction]}, concerns=[{
        "action": transition, "concern_id": concern["id"], "content": CORRECT, "topic": CORRECT,
        "basis": "explicit", "confidence": .95, "intensity": 20,
        "evidence_ids": [correction], "reason": "The owner corrected the actor"}],
        wish_updates=[{"desire_id": old_wish["id"], "action": "abandon", "reason": "The premise was corrected"}],
        wishes=[{"content": "Tell the owner about Kin carrying the telescope.", "topic": CORRECT,
                 "kind": "contact", "strength": 90, "ttl_hours": 24, "completion": "A chat",
                 "evidence_ids": [correction]}])
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        # Intentionally independent of the request: no prompt-driven fake reasoning.
        return httpx.Response(200, json={"id": "synthetic-correction", "model": A.APPRAISAL_MODEL,
            "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 100},
            "content": [{"type": "tool_use", "name": "submit_appraisal", "input": corrected.model_dump()}]})

    monkeypatch.setenv("KIN_TEST_DS_KEY", "synthetic")
    provider = A.DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "KIN_TEST_DS_KEY",
                         timeout=60, transport=httpx.MockTransport(respond))
    provider.engine = mind.engine
    # Version-one prompt materializations (as on the host) must follow the new default.
    prompt = mind.engine.db.root / "prompts" / "assessment" / "submit_appraisal.json"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text(json.dumps({"name": "assessment/submit_appraisal", "version": 1, "text": "Old default"}))
    queued = jobs.enqueue([correction], "synthetic-v1")
    result = jobs.run_one(provider, job_id=queued["id"])
    assert result["state"] == "complete", result
    assert not result.get("rejected_sections"), result
    assert len(requests) == 1
    sent = json.loads(requests[0]["messages"][0]["content"])
    assert any(e["text"] == CORRECTION and e["role"] == "user" for e in sent["new_evidence"])
    assert any(c["id"] == concern["id"] and c["content"] == WRONG for c in sent["state"]["concerns"])

    # Reload durable state, then use the real foreground and next-appraisal projections.
    reloaded = Mind(mind.engine, mind.scope, clock=mind.clock)
    state = reloaded.read()
    saved = next(c for c in state["concerns"] if c["id"] == concern["id"])
    assert saved["revision"] == concern["revision"] + 1
    assert saved["content"] == saved["topic"] == CORRECT
    assert saved["status"] == ("active" if transition == "update" else "resolved")
    assert state["appraisal_summary"]["understanding"]["meaning"] == CORRECT
    assert next(d for d in state["desires"] if d["id"] == old_wish["id"])["status"] == "abandoned"
    foreground = interaction_projection(state)
    assert WRONG not in json.dumps(foreground)
    background = A.appraisal_context({"state": state, "stimulus": "idle-review", "new_evidence": []})
    assert background["state"]["appraisal_summary"]["understanding"]["meaning"] == CORRECT
    assert next(c for c in background["state"]["concerns"] if c["id"] == concern["id"])["content"] == CORRECT
    active = [d for d in background["state"]["desires"] if d["status"] == "wanted"]
    assert len(active) == 1 and active[0]["topic"] == CORRECT
    candidate = reloaded.contact_candidate()
    assert candidate["eligible"], candidate
    assert [d["id"] for d in candidate["desires"]] == [active[0]["id"]]
    assert candidate["desire"]["content"] == "Tell the owner about Kin carrying the telescope."
    # The earlier mistaken evaluation remains historical, not silently rewritten.
    with mind.engine.db.connect() as conn:
        row = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (initial["id"],)).fetchone()[0])
    assert row["result"]["proposal"]["understanding"]["meaning"] == WRONG
    assert_correction_contract(requests[0]["system"])
