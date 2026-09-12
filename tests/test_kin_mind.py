import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict
from eventmem.core.models import Scope, SourceInput
from kin_mind.appraisal import Appraisal, Appraisals, DeepSeek, Wish
from kin_mind.state import AffectiveEvent, DesireChange, Mind


@pytest.fixture
def setup(tmp_path):
    clock = [datetime.now(timezone.utc)]
    engine = Engine(tmp_path)
    scope = Scope(persona="synthetic")
    mind = Mind(engine, scope, clock=lambda: clock[0].isoformat())

    def source(key, text=None, authority="explicit", version="1"):
        return engine.receive(
            SourceInput(
                namespace="test",
                key=key,
                version=version,
                scope=scope,
                text=text or key,
                authority=authority,
                occurred_at=clock[0].isoformat(),
                metadata={"role": "user", "host_event": "message"},
            )
        )["id"]

    init = source("configuration")
    mind.initialize(agent_version="synthetic-v1", evidence_ids=[init])
    return mind, source, clock


def event(mind, source, key, values):
    return AffectiveEvent(
        command_id=key,
        agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"],
        evidence_ids=[source(key)],
        values=values,
        reason="A sourced synthetic event",
    )


def wish(mind, source, key="wish", strength=95, **extra):
    args = {
        "command_id": key,
        "agent_version": "synthetic-v1",
        "expected_revision": mind.read()["revision"],
        "evidence_ids": [source(key)],
        "action": "create",
        "content": "Share a sourced finding",
        "topic": "synthetic",
        "kind": "contact",
        "strength": strength,
        "expires_at": (
            datetime.fromisoformat(mind.clock()) + timedelta(days=2)
        ).isoformat(),
        "completion": "platform accepts this share",
        "reason": "A useful finding to discuss",
    }
    args.update(extra)
    return mind.manage_desire(DesireChange(**args))


def test_independent_defaults_decay_restart_dedup(setup):
    mind, source, clock = setup
    initial = mind.read()
    assert len(initial["dimensions"]) == 20
    assert all(x["basis"] == "role_default" for x in initial["dimensions"].values())
    request = event(
        mind,
        source,
        "mixed",
        {"longing": 90, "mood": 20, "flirtation": 95, "focus": 98},
    )
    result = mind.record(request)
    assert mind.record(request) == result
    assert Mind(mind.engine, mind.scope, clock=mind.clock).read()["revision"] == 2
    v = mind.read()["dimensions"]
    assert v["curiosity"]["value"] == 75 and v["flirtation"]["value"] == 95
    clock[0] += timedelta(hours=2)
    v = mind.read()["dimensions"]
    assert v["mood"]["value"] == 42 and v["longing"]["value"] > 80
    assert v["flirtation"]["value"] == 68 and v["focus"]["value"] == 79
    with pytest.raises(Conflict):
        mind.record(
            request.model_copy(
                update={"command_id": "replay-as-new", "expected_revision": 2}
            )
        )
    with pytest.raises(Conflict):
        mind.record(
            event(mind, source, "new", {"mood": 60}).model_copy(
                update={"expected_revision": 1}
            )
        )


def test_threshold_before_four_hours_delivery_idempotency(setup):
    mind, source, _clock = setup
    wish(mind, source)
    # New experience can reach the threshold immediately, even one minute into a chat.
    mind.record(event(mind, source, "share-result", {"initiative": 80}))
    assert mind.contact_candidate()["eligible"]
    attempt = mind.claim_contact(owner_epoch="owner-1")
    assert not mind.contact_candidate()["eligible"]
    assert not mind.check_contact(attempt["id"], "owner-2")["eligible"]
    assert mind.check_contact(attempt["id"], "owner-1")["eligible"]
    mind.settle_contact(attempt_id=attempt["id"], state="pending")
    mind.settle_contact(attempt_id=attempt["id"], state="unconfirmed")
    with pytest.raises(Conflict):
        mind.claim_contact(owner_epoch="owner-2")
    with pytest.raises(ValueError):
        mind.settle_contact(attempt_id=attempt["id"], state="accepted")
    with pytest.raises(Conflict):
        mind.settle_contact(attempt_id=attempt["id"], state="canceled")
    receipt = mind.settle_contact(
        attempt_id=attempt["id"], state="accepted", message_id="synthetic-platform-id"
    )
    assert (
        mind.settle_contact(
            attempt_id=attempt["id"],
            state="accepted",
            message_id="synthetic-platform-id",
        )
        == receipt
    )
    assert mind.read()["dimensions"]["initiative"]["value"] == 20
    assert mind.read()["desires"][0]["delivery"]["visibility"] == "unverified"
    assert mind.read()["desires"][0]["status"] == "completed"
    assert not mind.contact_candidate()["eligible"]


def test_silence_growth_and_expiry(setup):
    mind, source, clock = setup
    wish(
        mind,
        source,
        strength=100,
        expires_at=(clock[0] + timedelta(hours=5)).isoformat(),
    )
    clock[0] += timedelta(hours=3)
    assert mind.contact_candidate()["eligible"]
    v = mind.read()["dimensions"]
    assert [v[k]["value"] for k in ("grievance", "possessiveness", "reassurance")] == [
        10,
        25,
        25,
    ]
    clock[0] += timedelta(hours=3)
    assert not mind.contact_candidate()["eligible"]
    assert mind.read()["desires"][0]["expired"]
    assert 65 < mind.read()["dimensions"]["initiative"]["value"] < 80


def test_source_correction_cancels_candidate_and_marks_state(setup):
    mind, source, clock = setup
    sid = source("evidence", "Initial evidence")
    wish(mind, source, evidence_ids=[sid])
    mind.record(event(mind, source, "rise", {"initiative": 90}))
    attempt = mind.claim_contact(owner_epoch="same")
    clock[0] += timedelta(seconds=1)
    source("evidence", "Corrected evidence", version="2")
    assert mind.read()["desires"][0]["needs_review"]
    assert not mind.check_contact(attempt["id"], "same")["eligible"]
    mind.settle_contact(attempt_id=attempt["id"], state="canceled")


def test_scope_isolation_and_tasks_rejected(setup):
    mind, source, _clock = setup
    foreign = mind.engine.receive(
        SourceInput(
            namespace="test", key="alien", text="outside", scope=Scope(persona="other")
        )
    )["id"]
    with pytest.raises(Conflict):
        mind.record(
            event(mind, source, "attempt", {"mood": 50}).model_copy(
                update={"evidence_ids": [foreign]}
            )
        )
    with pytest.raises(ValueError):
        wish(mind, source, kind="task")
    with pytest.raises(ValueError):
        mind.record(event(mind, source, "unknown", {"imaginary": 70}))
    assert mind.read()["revision"] == 1


class FakeReviewer:
    def __init__(self, proposal=None, error=None):
        self.proposal, self.error, self.calls = proposal, error, 0

    def appraise(self, context):
        self.calls += 1
        if self.error:
            raise self.error
        assert context["new_evidence"]
        return self.proposal, {"provider": "deepseek", "model": "synthetic"}


def test_durable_appraisal_and_wish_atomic_replay(setup):
    mind, source, _clock = setup
    jobs = Appraisals(mind)
    sid = source("new-idea")
    first = jobs.enqueue([sid], "synthetic-v1")
    assert jobs.enqueue([sid], "synthetic-v1")["id"] == first["id"]
    reviewer = FakeReviewer(
        Appraisal(
            values={"curiosity": 85},
            reason="A new question",
            wishes=[
                Wish(
                    content="Research the question",
                    topic="synthetic",
                    kind="explore",
                    strength=80,
                    ttl_hours=24,
                    completion="cited findings",
                )
            ],
        )
    )
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert mind.read()["dimensions"]["curiosity"]["value"] == 85
    assert len(mind.read()["desires"]) == 1
    assert mind.read()["revision"] == 2
    assert jobs.run_one(reviewer)["state"] == "idle"
    # Lost queue acknowledgement after a committed review must not run the provider again.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=0")
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert reviewer.calls == 1 and mind.read()["revision"] == 2


def test_failed_appraisal_keeps_state(setup):
    mind, source, _clock = setup
    jobs = Appraisals(mind)
    jobs.enqueue([source("input")], "synthetic-v1")
    out = jobs.run_one(FakeReviewer(error=ValueError("sensitive payload")))
    assert out["state"] == "pending" and "sensitive" not in json.dumps(out)
    assert mind.read()["revision"] == 1


def test_deepseek_contract_and_redaction(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_KEY", "test-only-key")

    def handler(request):
        assert request.headers["x-api-key"] == "test-only-key"
        body = json.loads(request.content)
        assert body["tool_choice"]["name"] == "submit_appraisal"
        assert body["thinking"]["type"] == "disabled"
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "usage": {"input_tokens": 1},
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "private reasoning must not escape",
                    },
                    {
                        "type": "tool_use",
                        "name": "submit_appraisal",
                        "input": {"values": {}, "reason": "No new evidence"},
                    },
                ],
            },
        )

    proposal, receipt = DeepSeek(
        "https://api.deepseek.com/anthropic",
        "deepseek-flash",
        "SYNTHETIC_KEY",
        transport=httpx.MockTransport(handler),
    ).appraise({})
    assert not proposal.values and "thinking" not in json.dumps(receipt)
    with pytest.raises(ValueError):
        DeepSeek("https://example.com", "model")
    bad = DeepSeek(
        "https://api.deepseek.com/anthropic",
        "deepseek-flash",
        "SYNTHETIC_KEY",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(401, text="test-only-key")
        ),
    )
    with pytest.raises(RuntimeError, match="^deepseek-http-401$"):
        bad.appraise({})


def test_personality_prospective_limits_history_and_reversion(setup):
    from eventmem.core.self_knowledge import (
        AssessmentInput,
        ClaimInput,
        PredictionInput,
        SelfKnowledge,
    )
    from kin_mind.state import Evolution

    mind, source, clock = setup
    sk = SelfKnowledge(mind.engine, mind.scope)

    # The self-knowledge layer has an independent wall clock; evidence must precede calls.
    def src(key):
        clock[0] = datetime.now(timezone.utc)
        sid = source(key)
        return mind.engine.source(sid)["record_ids"][0]

    refs = [src("interaction-1"), src("interaction-2"), src("interaction-3")]
    claim = sk.claim(
        ClaimInput(
            command_id="hypothesis",
            aspect="curiosity",
            context="source-checking",
            agent_version="synthetic-v1",
            claim="I prefer checking a primary source",
            evidence_ids=refs,
        )
    )
    prediction = sk.predict(
        PredictionInput(
            command_id="prospective",
            claim_id=claim["id"],
            expected_revision=1,
            case_id="future-case",
            behavior="Check a primary source",
            information="The next query has not been answered",
            probability=0.8,
        )
    )
    outcome = src("later-observed-behavior")
    assessment = sk.assess(
        AssessmentInput(
            command_id="assess",
            prediction_id=prediction["id"],
            expected_revision=1,
            outcome=True,
            evidence_ids=[outcome],
            note="Observed source check",
        )
    )
    clock[0] = datetime.now(timezone.utc)
    proposal = Evolution(
        claim_id=claim["id"],
        assessment_id=assessment["id"],
        baseline_changes={"curiosity": 77},
        half_life_changes={"curiosity": 52.8},
        traits={"interest": "Check sources"},
    )
    request = AffectiveEvent(
        command_id="evolve",
        agent_version="synthetic-v1",
        expected_revision=1,
        evidence_ids=refs,
        reason="A prospective trial with independent interactions",
        evolution=proposal,
    )
    result = mind.record(request)
    view = mind.read(history=5)
    assert view["dimensions"]["curiosity"]["baseline"] == 77
    assert view["traits"]["interest"]["basis"] == "hypothesis_trial"
    assert mind.engine.get(claim["id"])["status"] == "unverified"
    with pytest.raises(Conflict):
        mind.record(
            request.model_copy(update={"command_id": "again", "expected_revision": 2})
        )
    correction = src("please-revert-this-change")
    mind.record(
        AffectiveEvent(
            command_id="revert",
            agent_version="synthetic-v1",
            expected_revision=2,
            evidence_ids=[correction],
            reason="Explicit user correction",
            evolution=Evolution(revert_event_id=result["event_id"]),
        )
    )
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 75
    assert not mind.read()["traits"]
    assert len(mind.read(history=10)["history"]) == 3


def test_authorized_time_growth_and_open_discovery(setup, tmp_path):
    from kin_mind.exploration import Explorations
    mind, source, clock = setup
    mind.configure_autonomy({
        "command_id": "autonomy", "agent_version": "synthetic-v2",
        "expected_revision": mind.read()["revision"],
        "evidence_ids": [source("allow-new-topics")], "reason": "User requested discovery and time growth",
    })
    wish(mind, source, strength=58)
    mind.record(event(mind, source, "reset-like", {"initiative": 20}))
    initial = mind.read()
    clock[0] += timedelta(hours=6)
    projected = mind.read()
    assert projected["dimensions"]["initiative"]["value"] >= 75
    assert projected["dimensions"]["grievance"]["value"] == initial["dimensions"]["grievance"]["value"]
    assert projected["contact"] == initial["contact"]
    assert Mind(mind.engine, mind.scope, clock=mind.clock).read()["dimensions"]["initiative"] == projected["dimensions"]["initiative"]

    def runner(executable, brief, directory, **kwargs):
        assert brief["topic"] == "Kin 选定的探索题目" and brief["source_ids"]
        assert kwargs["budget_seconds"] == 1200
        return {"state": "complete", "partial": False, "result": {
            "summary": "A synthetic discovery", "findings": ["Synthetic"],
            "sources": [{"url": "https://example.com", "title": "Fixture"}],
            "open_questions": [], "suggested_share": "A discovery",
        }}
    explorer = Explorations(mind)
    assert explorer.run("fake", tmp_path / "jobs", "synthetic-v2", runner=runner)["reason"] == "kin-topic-selection-required"
    assert explorer.run("fake", tmp_path / "jobs", "synthetic-v2", runner=runner, brief="Kin chooses a synthetic question")["state"] == "complete"
    assert explorer.run("fake", tmp_path / "jobs", "synthetic-v2", runner=runner)["state"] == "waiting"


def test_autonomy_requires_explicit_evidence(setup):
    mind, source, _ = setup
    with pytest.raises(Conflict):
        mind.configure_autonomy({
            "command_id": "bad-policy", "agent_version": "synthetic-v2",
            "expected_revision": mind.read()["revision"],
            "evidence_ids": [source("inferred", authority="model")], "reason": "Not user permission",
        })


def test_minute_review_queues_authorized_exploration_once(setup, tmp_path, monkeypatch):
    from kin_mind import host
    mind, source, _ = setup
    mind.configure_autonomy({
        "command_id": "autonomy", "agent_version": "synthetic-v2",
        "expected_revision": mind.read()["revision"],
        "evidence_ids": [source("open")], "reason": "User authorization",
    })
    monkeypatch.setattr(host, "Engine", lambda root: mind.engine)
    monkeypatch.setattr(host, "Mind", lambda engine, scope: mind)
    monkeypatch.setattr(host.DeepSeek, "from_engine", lambda engine: None)
    monkeypatch.setattr(host.Appraisals, "run_one", lambda self, provider: {"state": "idle"})
    config = {"root": str(tmp_path), "scope": mind.scope.model_dump(),
              "exploration_stop_file": str(tmp_path / "stop")}
    assert host.dispatch(config, "review", {})["state"] == "idle"
    wake = tmp_path / "mind-exploration-request.json"
    before = wake.stat().st_mtime_ns
    host.dispatch(config, "review", {})
    assert wake.stat().st_mtime_ns == before
    assert json.loads(wake.read_text())["kind"] == "internal-exploration-wakeup"


def test_empty_draft_waits_and_survives_restart(setup):
    mind, source, clock = setup
    wish(mind, source)
    mind.record(event(mind, source, "ready-empty", {"initiative": 90}))
    attempt = mind.claim_contact(owner_epoch="owner-1")
    before = mind.read()["dimensions"]["mood"]["value"]
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-empty")
    restarted = Mind(mind.engine, mind.scope, clock=mind.clock)
    assert not restarted.contact_candidate()["eligible"]
    assert restarted.read()["desires"][0]["status"] == "waiting"
    assert restarted.read()["dimensions"]["mood"]["value"] == before


def test_empty_draft_does_not_override_changed_wish(setup):
    mind, source, clock = setup
    wish(mind, source)
    mind.record(event(mind, source, "ready-changed", {"initiative": 90}))
    attempt = mind.claim_contact(owner_epoch="owner-1")
    mind.manage_desire(DesireChange(command_id="change-wish", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("change-wish")],
        action="update", desire_id=attempt["desire_id"], content="A different finding", reason="New source"))
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-empty")
    assert mind.read()["desires"][0]["status"] == "wanted"
