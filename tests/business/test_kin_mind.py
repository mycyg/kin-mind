import json

from datetime import datetime, timedelta, timezone

import httpx

import pytest

from eventmem.core import Engine

from eventmem.core.db import Conflict

from eventmem.core.models import Scope, SourceInput

from kin_mind.appraisal import Appraisal, Appraisals, DeepSeek, Wish, appraisal_context

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

def defer(mind, condition="time", **extra):
    attempt = mind.claim_contact(owner_epoch="owner-1")
    return mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision",
        decision={"action": "wait", "condition": condition, "reason": "A temporary condition", **extra})

def test_owner_contact_preference_is_reversible_and_does_not_change_scores(setup):
    mind, source, _ = setup
    before = mind.read()
    request = {"command_id": "allow-new-content", "agent_version": "synthetic-v2",
        "expected_revision": before["revision"], "evidence_ids": [source("allow")],
        "reason": "New content may be shared without waiting", "wait_for_reply": False}
    result = mind.configure_contact(request)
    assert mind.configure_contact(request) == result
    view = mind.read()
    assert view["contact"]["wait_for_reply"] is False
    assert view["contact"]["preference"]["event_id"] == result["event_id"]
    assert view["contact"]["quiet_start"] == before["contact"]["quiet_start"]
    assert view["dimensions"] == before["dimensions"]
    assert view["exploration"] == before["exploration"]
    mind.configure_contact({**request, "command_id": "restore-wait",
        "expected_revision": view["revision"], "evidence_ids": [source("restore")],
        "reason": "The owner restored waiting", "wait_for_reply": True})
    assert mind.read()["contact"]["wait_for_reply"] is True
    assert mind.read()["dimensions"] == before["dimensions"]


def test_every_ready_wish_is_offered_and_kin_picks(setup):
    """N11: the draft is offered every ready wish; a send completes only the ones Kin chose."""
    mind, source, _ = setup
    wish(mind, source, "first", strength=40, content="Tell her about the tea")
    wish(mind, source, "second", strength=90, content="Ask about the trip")
    candidate = mind.contact_candidate()
    assert candidate["eligible"] and len(candidate["desires"]) == 2
    attempt = mind.claim_contact(owner_epoch="owner-1")
    assert len(attempt["desire_ids"]) == 2
    tea = next(d["id"] for d in attempt["desires"] if d["content"] == "Tell her about the tea")
    with pytest.raises(ValueError):
        mind.settle_contact(attempt_id=attempt["id"], state="pending", desire_ids=["not-offered"])
    assert mind.check_contact(attempt["id"], "owner-1", desire_ids=[tea])["eligible"]
    mind.settle_contact(attempt_id=attempt["id"], state="pending", desire_ids=[tea], text="The tea was lovely")
    mind.settle_contact(attempt_id=attempt["id"], state="accepted", message_id="m-1")
    status = {d["content"]: d["status"] for d in mind.read()["desires"]}
    assert status == {"Tell her about the tea": "completed", "Ask about the trip": "wanted"}


def test_an_unknown_send_holds_only_its_own_wish(setup):
    """AD2-14: an unknown outcome is reconciled under its own id with a growing interval. Only its
    wish is held, Kin is told, and the same words are never said again under another id."""
    mind, source, clock = setup
    wish(mind, source, "moon", content="Share the moon photo")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    held = attempt["desire_ids"][0]
    mind.settle_contact(attempt_id=attempt["id"], state="pending", text="Did you see the moon?")
    mind.settle_contact(attempt_id=attempt["id"], state="unconfirmed", reason="timeout")
    assert mind.read()["contact_unconfirmed"][0]["attempt_id"] == attempt["id"]
    assert mind.contact_candidate()["reason"] == "no-actionable-desire"
    wish(mind, source, "other", content="Ask how the day went")
    candidate = mind.contact_candidate()
    assert candidate["eligible"] and held not in [d["id"] for d in candidate["desires"]]
    assert candidate["reconcile"] == []
    clock[0] += timedelta(minutes=6)
    assert [u["attempt_id"] for u in mind.contact_candidate()["reconcile"]] == [attempt["id"]]
    mind.settle_contact(attempt_id=attempt["id"], state="unconfirmed", reason="receipt-still-unknown")
    assert mind.contact_candidate()["reconcile"] == []
    second = mind.claim_contact(owner_epoch="owner-1")
    assert mind.check_contact(second["id"], "owner-1", text="Did you  see the moon?")["reason"] == "repeats-unconfirmed-send"
    assert mind.check_contact(second["id"], "owner-1", text="How was your day?")["eligible"]


def test_a_transient_cancel_lets_the_same_wish_be_tried_again(setup):
    """K1-17: nothing changed, yet the next attempt is a new one, not the finished old one."""
    mind, source, _ = setup
    wish(mind, source, "again")
    first = mind.claim_contact(owner_epoch="owner-1")
    mind.settle_contact(attempt_id=first["id"], state="canceled", reason="Draft or delivery conditions changed")
    second = mind.claim_contact(owner_epoch="owner-1")
    assert second["id"] != first["id"] and second["state"] == "drafting"


def test_the_reviews_of_one_tick_are_one_assessment(setup):
    """K1-06: internal reviews queued in the same tick are judged in a single assessment."""
    mind, source, _ = setup
    jobs = Appraisals(mind)
    idle = jobs.enqueue([source("idle-timer")], "synthetic-v1", origin="reflection", stimulus="idle-review")
    look = jobs.enqueue([source("wish-look")], "synthetic-v1", origin="reflection", stimulus="wish-review")
    reviewer = FakeReviewer(Appraisal(reason="Looked at both"))
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert reviewer.calls == 1
    assert jobs.status(idle["id"])["state"] == "complete" and jobs.status(look["id"])["state"] == "complete"
    assert jobs.run_one(reviewer)["state"] == "idle"


def test_a_decision_outlives_a_deployment_but_not_a_change_of_what_decides_behaviour(setup):
    """K1-13, MAIN-RUA-02: the compat stamp, not agent_version, says whether a decision holds."""
    from kin_mind import compat
    mind, _, _ = setup
    with mind.engine.db.connect() as conn:
        stamped = compat.stamp(mind, conn)
        assert mind.decision_current(conn, {"compat": stamped}) and mind.decision_current(conn, {})
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state["agent_version"] = "synthetic-v2"
        mind._save(conn, state)
    with mind.engine.db.connect() as conn:
        assert mind.decision_current(conn, {"compat": stamped})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,data) VALUES('behavior_models',?)",
                     (json.dumps({"chat": "another-model", "chat_effort": "high"}),))
    with mind.engine.db.connect() as conn:
        assert not mind.decision_current(conn, {"compat": stamped})
