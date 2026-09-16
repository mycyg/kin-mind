"""Curiosity admission replaces elapsed-time windows; all data is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from test_kin_mind import FakeReviewer, event, wish
from test_kin_mind import setup as _setup

from eventmem.core.models import SourceInput
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals, Wish, WishUpdate
from kin_mind.exploration import Explorations
from kin_mind.exploration_cadence import ExplorationCadence
from kin_mind.state import Mind, Motivation

setup = _setup


def review(actions, jobs, proposal):
    actions.drain(jobs)
    provider = FakeReviewer(proposal)
    result = jobs.run_one(provider)
    assert result["state"] == "complete", result
    actions.drain(jobs)
    return provider


def activate(mind, source):
    actions, jobs = ActionEvents(mind), Appraisals(mind)
    actions.configure(
        {
            "command_id": "new-policy",
            "agent_version": "synthetic-actions-v2",
            "expected_revision": mind.read()["revision"],
            "evidence_ids": [source("policy-v2")],
            "reason": "Owner wants spontaneous thoughts and curiosity-led exploration",
        }
    )
    review(
        actions,
        jobs,
        Appraisal(
            values={"initiative": 60, "curiosity": 50},
            motivations={
                "initiative": Motivation(
                    target=90, half_life_minutes=20, reason="A growing playful impulse"
                ),
                "curiosity": Motivation(
                    target=30, half_life_minutes=60, reason="Resting"
                ),
            },
            reason="Initial assessment",
        ),
    )
    return actions, jobs


def test_clock_crossing_once_across_restart_and_no_idle_model_calls(setup):
    mind, source, clock = setup
    actions, jobs = activate(mind, source)
    idle = FakeReviewer(error=AssertionError("No idle model calls"))
    for _ in range(19):
        clock[0] += timedelta(minutes=1)
        assert actions.crossings() == []
        actions.drain(jobs)
        assert jobs.run_one(idle)["state"] == "idle"
    clock[0] += timedelta(minutes=1)
    key = actions.crossings()[0]
    restarted = ActionEvents(Mind(mind.engine, mind.scope, clock=mind.clock))
    assert restarted.crossings() == [key]
    provider = review(
        restarted,
        jobs,
        Appraisal(
            values={"initiative": 85},
            reason="A strange playful thought",
            wishes=[
                Wish(
                    content="I wonder whether clouds get hiccups",
                    topic="an idle thought",
                    kind="contact",
                    strength=85,
                    ttl_hours=12,
                    completion="Share the thought",
                )
            ],
        ),
    )
    assert mind.contact_candidate()["eligible"]
    for _ in range(5):
        clock[0] += timedelta(minutes=1)
        assert restarted.crossings() == []
        restarted.drain(jobs)
        assert jobs.run_one(idle)["state"] == "idle"
    assert idle.calls == 0 and provider.calls == 1
    assert mind.read()["dimensions"]["grievance"]["value"] == 10
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 75
    assert mind.read()["action_policy"]["reasoning"] == "high"


def test_concurrent_crossings_keep_one_internal_stimulus(setup):
    mind, source, clock = setup
    actions, jobs = activate(mind, source)
    clock[0] += timedelta(minutes=21)
    with ThreadPoolExecutor(max_workers=3) as pool:
        keys = list(pool.map(lambda _: actions.crossings()[0], range(3)))
    assert len(set(keys)) == 1
    actions.drain(jobs)
    assert len([j for j in jobs.status() if j["state"] == "pending"]) == 1
    with mind.engine.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sources WHERE namespace='mind-internal-event' AND json_extract(data,'$.metadata.host_event')='internal-drive-crossing'"
            ).fetchone()[0]
            == 1
        )


def test_delivery_reappraises_instead_of_reset_and_cannot_invent_next_thought(setup):
    mind, source, _ = setup
    actions, jobs = activate(mind, source)
    wish(mind, source, "first", content="A small joke")
    wish(mind, source, "second", content="Another existing thought")
    mind.record(event(mind, source, "ready", {"initiative": 90}))
    attempt = mind.claim_contact(owner_epoch="same")
    mind.settle_contact(
        attempt_id=attempt["id"],
        state="accepted",
        message_id="m1",
        message_ids=["m1", "m2"],
    )
    assert mind.read()["dimensions"]["initiative"]["value"] == 90
    assert mind.contact_candidate()["reason"] == "action-appraisal-pending"
    review(
        actions,
        jobs,
        Appraisal(
            values={"initiative": 82},
            reason="Still have another thought",
            wishes=[
                Wish(
                    content="Receipt alone invented this",
                    topic="bad",
                    kind="contact",
                    strength=95,
                    ttl_hours=1,
                    completion="bad",
                )
            ],
        ),
    )
    assert mind.read()["dimensions"]["initiative"]["value"] == 82
    assert len(mind.read()["desires"]) == 2
    assert mind.contact_candidate()["eligible"]
    assert actions.crossings() == []


def test_unanswered_timing_ignores_internal_configuration_and_uses_view_clock(setup):
    mind, source, clock = setup

    def receive(namespace, key):
        return mind.engine.receive(
            SourceInput(
                namespace=namespace,
                key=key,
                scope=mind.scope,
                text="Synthetic interaction",
                occurred_at=mind.clock(),
                extract=False,
            )
        )

    receive("kin-owner-input", "owner-one")
    clock[0] += timedelta(minutes=1)
    wish(mind, source, "affection", content="I feel like calling for attention")
    mind.record(event(mind, source, "ready", {"initiative": 90}))
    attempt = mind.claim_contact(owner_epoch="owner-one")
    mind.settle_contact(
        attempt_id=attempt["id"], state="accepted", message_id="synthetic-message"
    )
    clock[0] += timedelta(minutes=30)
    receive("mind-internal-event", "review")
    receive("kin-owner-configuration", "policy")
    timing = mind.read()["interaction_timing"]
    assert timing["awaiting_reply"]
    assert timing["owner_silence_seconds"] == 31 * 60
    assert timing["unanswered_contact_seconds"] == 30 * 60
    assert (
        mind.read(as_of=(clock[0] + timedelta(hours=1)).isoformat())[
            "interaction_timing"
        ]["unanswered_contact_seconds"]
        == 90 * 60
    )
    receive("kin-owner-input", "owner-two")
    assert not mind.read()["interaction_timing"]["awaiting_reply"]


def test_new_affection_episode_keeps_prior_completion_and_current_config_version(setup):
    mind, source, clock = setup
    actions, jobs = activate(mind, source)
    content = "I miss you and want a little attention"
    wish(mind, source, "first-affection", content=content)
    mind.record(event(mind, source, "ready", {"initiative": 90}))
    attempt = mind.claim_contact(owner_epoch="same")
    mind.settle_contact(attempt_id=attempt["id"], state="accepted", message_id="m1")
    review(
        actions, jobs, Appraisal(values={"initiative": 50}, reason="Contented for now")
    )
    clock[0] += timedelta(hours=2)
    job = jobs.enqueue(
        [source("new-episode")], "older-enqueued-version", stimulus="drive-crossing"
    )
    result = jobs.run_one(
        FakeReviewer(
            Appraisal(
                values={"initiative": 85},
                reason="A new affectionate impulse",
                wishes=[
                    Wish(
                        content=content,
                        topic="affection",
                        kind="contact",
                        strength=85,
                        ttl_hours=6,
                        completion="Express the new feeling",
                    )
                ],
            )
        )
    )
    assert result["id"] == job["id"] and result["state"] == "complete"
    assert result["receipt"]["agent_version"] == "synthetic-actions-v2"
    assert result["receipt"]["enqueued_agent_version"] == "older-enqueued-version"
    desires = [d for d in mind.read()["desires"] if d["content"] == content]
    assert len(desires) == 2
    assert {d["status"] for d in desires} == {"completed", "wanted"}
    assert len({d["id"] for d in desires}) == 2


def result(*args, **kwargs):
    assert kwargs["budget_seconds"] == 1200
    return {
        "state": "complete",
        "partial": False,
        "result": {
            "summary": "A question remains open",
            "findings": [],
            "sources": [],
            "open_questions": ["What would distinguish the explanations?"],
            "suggested_share": "A funny uncertainty",
        },
    }


def test_curiosity_not_elapsed_time_and_result_always_enters_appraisal(setup, tmp_path):
    mind, source, clock = setup
    actions, jobs = activate(mind, source)
    wish(mind, source, "question1", kind="explore", content="Research a first question")
    cadence = ExplorationCadence(mind)
    assert cadence.status()["state"] == "waiting"
    mind.record(event(mind, source, "curious", {"curiosity": 90}))
    actions.review_unselected()
    review(
        actions,
        jobs,
        Appraisal(
            reason="Select this question",
            wish_updates=[
                WishUpdate(
                    desire_id=mind.read()["desires"][0]["id"],
                    action="resume",
                    reason="I want to explore it",
                )
            ],
        ),
    )
    assert cadence.reserve("test-v2")["state"] == "ready"
    executor = Explorations(mind)
    assert (
        executor.run("fake", tmp_path / "work", "test-v2", runner=result)["state"]
        == "complete"
    )
    assert mind.read()["desires"][0]["status"] == "completed"
    assert cadence.status()["reason"] == "action-appraisal-pending"
    review(
        actions,
        jobs,
        Appraisal(
            values={"curiosity": 80, "initiative": 85},
            reason="Wondering about the open question",
            wishes=[
                Wish(
                    content="This result gave me a silly thought",
                    topic="loose conversation",
                    kind="contact",
                    strength=85,
                    ttl_hours=12,
                    completion="Share it",
                )
            ],
        ),
    )
    assert mind.contact_candidate()["eligible"]
    assert cadence.status()["reason"] == "no-exploration-intent"
    wish(
        mind,
        source,
        "question2",
        kind="explore",
        content="Research a different question",
    )
    actions.review_unselected()
    selected = next(
        d
        for d in mind.read()["desires"]
        if d["content"] == "Research a different question"
    )
    review(
        actions,
        jobs,
        Appraisal(
            reason="Select another question",
            wish_updates=[
                WishUpdate(
                    desire_id=selected["id"],
                    action="resume",
                    reason="A different question",
                )
            ],
        ),
    )
    clock[0] += timedelta(minutes=1)
    assert cadence.status()["state"] == "ready"
    assert (
        executor.run("fake", tmp_path / "work", "test-v2", runner=result)["state"]
        == "complete"
    )
    assert len(executor.recent()) == 2


def test_one_exploration_worker_and_preemption_restores_intent(setup, tmp_path):
    mind, source, _ = setup
    wish(mind, source, "question", kind="explore")
    executor = Explorations(mind)

    def preempt(*args, **kwargs):
        assert ExplorationCadence(mind).status()["reason"] == "exploration-in-progress"
        assert (
            executor.run("fake", tmp_path / "work", "v1", runner=result)["state"]
            == "waiting"
        )
        return {"state": "preempted", "partial": True, "result": None}

    assert (
        executor.run("fake", tmp_path / "work", "v1", runner=preempt)["state"]
        == "preempted"
    )
    assert mind.read()["desires"][0]["status"] == "wanted"


def test_corrected_evidence_stops_a_pending_crossing(setup):
    mind, source, clock = setup
    actions, _jobs = activate(mind, source)
    source("policy-v2", "Corrected owner preference", version="2")
    clock[0] += timedelta(minutes=30)
    assert actions.crossings() == []
    assert mind.read()["dimensions"]["initiative"]["needs_review"]


def test_concurrent_admission_cannot_replay_a_successful_worker_claim(
    setup, tmp_path, monkeypatch
):
    mind, source, _ = setup
    wish(mind, source, "concurrent-question", kind="explore")
    barrier = Barrier(2)
    original = ActionEvents.exploration_candidate

    def simultaneous(self):
        snapshot = original(self)
        barrier.wait(timeout=5)
        return snapshot

    monkeypatch.setattr(ActionEvents, "exploration_candidate", simultaneous)
    first, second = Explorations(mind), Explorations(mind)
    calls = []

    def run_once(*args, **kwargs):
        calls.append(1)
        return result(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda e: e.run("fake", tmp_path / "work", "v1", runner=run_once),
                [first, second],
            )
        )
    assert len(calls) == 1
    assert sorted(r["state"] for r in results) == ["complete", "waiting"]
