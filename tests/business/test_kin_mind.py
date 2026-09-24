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



def test_contact_constraints_come_from_the_live_policy_not_the_install_copy(setup, tmp_path):
    """K1-03: the state kept quiet hours 0–9 and threshold 75 from the first install. Once the
    host names its live policy, Kin is shown what the host applies: no quiet window when the
    policy has none, no score threshold outside the legacy gates, and the owner's preference
    event still decides wait_for_reply."""
    from kin_mind import manifest
    mind, source, _ = setup
    installed = mind.read()["contact"]
    assert [installed["quiet_start"], installed["quiet_end"]] == [0, 9]
    policy = tmp_path / "proactive-policy.json"
    policy.write_text(json.dumps({"enabled": True, "timeZone": "Asia/Singapore", "quietStartHour": 0,
                                  "quietEndHour": 0, "minimumGapHours": 0, "waitForReply": True,
                                  "trigger": "semantic-decision", "initiativeThreshold": None}))
    assert mind.register_contact_policy(policy) is True
    assert mind.register_contact_policy(policy) is False
    contact = mind.read()["contact"]
    assert contact["quiet_start"] is None and contact["quiet_end"] is None
    assert "threshold" not in contact and contact["wait_for_reply"] is True
    assert contact["source"] == "live-policy"
    assert not [b for b in manifest._boundaries({}, {"contact": contact}, {}, "2026-09-24T10:00:00+00:00") if b[0] == "quiet-hours"]
    # A policy that does keep a window is shown with it, and read live.
    policy.write_text(json.dumps({"enabled": True, "timeZone": "Asia/Singapore", "quietStartHour": 23,
                                  "quietEndHour": 7, "waitForReply": False}))
    contact = mind.read()["contact"]
    assert [contact["quiet_start"], contact["quiet_end"], contact["wait_for_reply"]] == [23, 7, False]
    # The owner's own preference event decides, whatever the file says.
    mind.configure_contact({"command_id": "wait", "agent_version": "synthetic-v2",
        "expected_revision": mind.read()["revision"], "evidence_ids": [source("wait")],
        "reason": "The owner asked Kin to wait for a reply", "wait_for_reply": True})
    assert mind.read()["contact"]["wait_for_reply"] is True
    # An unreadable policy shows no window it cannot vouch for.
    policy.write_text("{broken")
    contact = mind.read()["contact"]
    assert contact["source"] == "live-policy-unreadable" and contact["quiet_start"] is None

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


def test_session_advice_goes_to_the_registry_carrier_not_the_mind_state(setup, monkeypatch):
    """DB1-03 (write side): a maintenance judgment is handed to session_advice.submit inside the
    commit; the versioned mind state no longer holds it, and reads go through the carrier."""
    from kin_mind import session_advice
    from kin_mind.session_advice import SessionAdvice
    mind, source, _ = setup
    carried = []
    monkeypatch.setattr(session_advice, "submit", lambda conn, scope, record, at: carried.append((scope, record)) or record, raising=False)
    monkeypatch.setattr(session_advice, "latest", lambda conn, scope, state=None: carried[-1][1] if carried else None, raising=False)
    context = {"id": "snapshot-1", "binding": {"generation": 3}, "evidence": [], "recent": []}
    jobs = Appraisals(mind, session_context=context)
    jobs.enqueue([source("pressure")], "synthetic-v1", origin="reflection", stimulus="session-maintenance")
    reviewer = FakeReviewer(Appraisal(reason="Keep the session", session_advice=SessionAdvice(action="keep", reason="Fine")))
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert carried and carried[0][1]["snapshotId"] == "snapshot-1" and carried[0][1]["decision"]["action"] == "keep"
    with mind.engine.db.connect() as conn:
        assert "session_advice" not in mind._load(conn)
    assert mind.read()["session_advice"]["snapshotId"] == "snapshot-1"


def test_a_short_term_drive_runs_its_course_and_asks_again(setup):
    """K1-02: a drive lasts four half-lives, then the value goes on from where it was toward the
    baseline; a later event does not carry the spent drive, and its end asks Kin to look again."""
    from kin_mind.actions import ActionEvents
    from kin_mind.state import Motivation, project
    mind, source, clock = setup
    baseline = mind.read()["dimensions"]["initiative"]["baseline"]
    mind.record(AffectiveEvent(command_id="night", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("night")], values={"initiative": 60}, reason="Winding down for the night",
        motivations={"initiative": Motivation(target=5, half_life_minutes=30, reason="Wants to rest")}))
    with mind.engine.db.connect() as conn:
        entry = mind._load(conn)["dimensions"]["initiative"]
    assert entry["motivation"]["expires_at"]
    clock[0] += timedelta(hours=2)
    at_end = project(entry, mind.clock())
    clock[0] += timedelta(hours=10)
    later = project(entry, mind.clock())
    assert later > at_end and abs(later - baseline) < abs(at_end - baseline)
    ActionEvents(mind).crossings()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_action_events WHERE kind='motivation-review'").fetchone()[0] == 1
    mind.record(event(mind, source, "morning", {"initiative": 40}))
    with mind.engine.db.connect() as conn:
        after = mind._load(conn)["dimensions"]["initiative"]
    assert after["target"] == baseline and "motivation" not in after


def test_a_resting_phase_is_not_moved_by_a_timer(setup):
    """K1-04: past its half-life Kin's phase stays as she gave it; the view only says a review is due."""
    from kin_mind.rhythm import rhythm_view
    entry = {"phase": "resting", "alertness": 10, "target": 10, "half_life_minutes": 60, "at": "2026-01-01T00:00:00+00:00",
             "reason": "Sleeping", "event_id": "e", "config_version": "v", "evidence": []}
    interactions = {"sample_status": "observing", "last_owner_at": None}
    view = rhythm_view(entry, "2026-01-01T09:00:00+00:00", interactions)
    assert view["phase"] == "resting" and view["review_due"] is True
    awake = rhythm_view({**entry, "phase": "awake", "alertness": 80, "target": 5}, "2026-01-01T09:00:00+00:00", interactions)
    assert awake["phase"] == "awake"


def test_an_expired_wish_is_shown_for_settlement_and_can_be_settled(setup):
    """K1-15, K4-18: past its window a wish is neither hidden nor archived by expiry; Kin settles it."""
    from kin_mind.appraisal import WishUpdate
    mind, source, clock = setup
    wish(mind, source, "old-wish", content="Share the old photo")
    desire = mind.read()["desires"][0]
    clock[0] += timedelta(days=3)
    context = appraisal_context({"state": mind.read(), "new_evidence": []})
    assert [w["id"] for w in context["state"]["expired_unsettled_wishes"]] == [desire["id"]]
    jobs = Appraisals(mind)
    jobs.enqueue([source("settle")], "synthetic-v1")
    reviewer = FakeReviewer(Appraisal(reason="That moment passed", wish_updates=[
        WishUpdate(desire_id=desire["id"], action="abandon", reason="The moment for it passed")]))
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert mind.read()["desires"][0]["status"] == "abandoned"


def test_easing_a_concern_takes_effect_without_a_new_source(setup):
    """K3-18: easing or archiving is Kin's own judgment and needs no new evidence; a change that
    alters nothing is a replay."""
    from kin_mind.continuity import ConcernChange, ContinuityConfig
    mind, source, _ = setup
    mind.configure_continuity(ContinuityConfig(command_id="continuity", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("continuity")], features={"concerns": True}, reason="test"))
    worry = source("worry")
    created = mind.manage_concern(ConcernChange(command_id="c1", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[worry], action="create", key="exam", kind="care", content="Her exam", topic="exam", intensity=60,
        basis="explicit", confidence=0.9, reason="She is worried"))
    cid = created["concern_id"]
    eased = mind.manage_concern(ConcernChange(command_id="c2", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[worry], action="ease", concern_id=cid, reason="It has faded for me"))
    assert not eased.get("replayed")
    again = mind.manage_concern(ConcernChange(command_id="c3", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[worry], action="ease", concern_id=cid, reason="Still faded"))
    assert again.get("replayed")


def test_a_wish_on_a_moved_trait_asks_again_and_a_confirmation_readies_it(setup):
    """K1-19: a trait revision no longer strands the wish that rested on it: Kin is asked, and
    looking at it again rests it on the traits as they stand now."""
    from kin_mind.actions import ActionEvents
    from kin_mind.trait_refs import links_fresh
    mind, source, _ = setup
    wish(mind, source, "trait-wish", content="Tease her about the tea")
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        did = next(iter(state["desires"]))
        state["desires"][did]["trait_revisions"] = {"trait_playful": 1}
        mind._save(conn, state)
    ActionEvents(mind).crossings()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_action_events WHERE kind='wish-review'").fetchone()[0] == 1
    mind.manage_desire(DesireChange(command_id="confirm", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
                                    evidence_ids=[source("confirm")], action="resume", desire_id=did, reason="Still want to"))
    with mind.engine.db.connect() as conn:
        assert links_fresh(conn, mind, mind._load(conn)["desires"][did])
