"""The seams between the four stage-1 work packages, driven end to end.

WP1 ends the plan review loop, WP2 isolates a refused section, WP3 bounds retries and quarantine,
WP4 settles absorbed batches. Each was tested alone; these replays cross the boundaries between them.

Synthetic replays only: an injected clock and scripted providers. No model or network call.
"""
import json
import time
from datetime import timedelta

import pytest
from test_appraisal_retry_policy import Failing
from test_batched_orphans import delivery, rows, schedule
from test_plan_review_loop import Env, Reviewer, later
from test_section_isolation import (
    SESSION,
    STALE_HABITS,
    UNKNOWN_ADVICE,
    children,
    crowd,
    run,
)

from kin_mind.appraisal import FOLLOW_UP, Appraisal, Appraisals
from kin_mind.autonomy_models import PlanChange, PlanStep
from kin_mind.habits import HabitProposal
from kin_mind.recovery import recover_quarantined
from kin_mind.session_advice import SessionAdvice

pytest_plugins = ("test_memory_continuity",)

REFUSED_DECISION = {"section": "action_decisions", "code": "conflict",
                    "message": "DeepSeek must explicitly account for each precondition"}
PRECONDITION = "A photo has been chosen"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


class Flaky(Reviewer):
    """A Reviewer that raises one scripted failure per attempt until the script runs out."""

    def __init__(self, script, errors):
        super().__init__(script)
        self.errors, self.attempts = list(errors), 0

    def appraise(self, context):
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        return super().appraise(context)


def requeue(env, job_id):
    with env.mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job_id,))


def metrics(env, name):
    return [json.loads(r["data"]) for r in env.rows("SELECT data FROM metrics WHERE name=?", name)]


def jobs(env):
    return Appraisals(env.mind, exploration_capabilities={"version": env.version})


def due_review(env):
    """A waiting plan whose step carries a precondition, woken by its own due time.

    The wake-up ledger has consumed that reason by the time the evaluation runs, so nothing but a
    follow-up can ask about this plan again until an unrelated reason appears.
    """
    plan = env.create(steps=[{"id": "make", "actor": "create", "goal": "Make the clock",
                              "completion": "A verified SVG is saved", "preconditions": [PRECONDITION]}])
    plan = env.decide(plan, "wait", next_review_at=later(env, hours=1))
    env.clock[0] += timedelta(hours=1, seconds=1)
    assert env.ticks() == 1 and [r[0] for r, _ in env.wakeups(plan)] == ["due"]
    return plan


def omits_the_precondition(env, plan):
    return lambda shown, context: Appraisal(reason="Time to make it", values={"mood": 58},
        action_decisions=[env.decision(shown[plan["id"]], "execute")])


def test_a_due_review_whose_decision_is_refused_is_asked_again_once_and_then_applied(env):
    crowd(env)
    plan = due_review(env)
    result, provider = env.evaluate(omits_the_precondition(env, plan))
    job = result["id"]
    assert result["state"] == "complete" and provider.calls == 1
    # Everything independent of the decision committed, and the refusal is a static host sentence.
    assert env.mind.read()["dimensions"]["mood"]["value"] == 58
    assert env.job(job)["rejected_sections"] == [REFUSED_DECISION]
    # The plan is where it was and its due reason is spent: alone, this strands it.
    assert env.step(plan)["state"] == "waiting" and env.ticks(6) == 1
    assert env.plans.claim("create", "worker")["state"] == "waiting"
    [follow] = children(env, job)
    assert follow["state"] == "pending" and follow["stimulus"] == FOLLOW_UP
    assert follow["section_review"] == {"rejected_sections": [REFUSED_DECISION], "held_sections": []}

    seen = {}
    def restated(shown, context):
        seen.update(stimulus=context["stimulus"], shown=list(shown),
                    notes=[json.loads(e["text"]) for e in context["new_evidence"]
                           if e["metadata"].get("host_event") == "internal-" + FOLLOW_UP])
        return Appraisal(reason="Accounted for now", action_decisions=[
            env.decision(shown[plan["id"]], "execute", conditions_met=[PRECONDITION])])
    second, provider = run(env, restated, job_id=follow["id"])
    assert second["state"] == "complete" and provider.calls == 1
    # The model saw the static refusal as evidence, and its own plan led the window of 40.
    assert seen["stimulus"] == FOLLOW_UP and seen["shown"][0] == plan["id"] and len(seen["shown"]) == 40
    assert seen["notes"] == [{"kind": FOLLOW_UP, "appraisal_id": job,
                              "rejected_sections": [REFUSED_DECISION], "held_sections": []}]
    step = env.step(plan)
    assert step["state"] == "ready" and step["decision"]["action"] == "execute"
    assert env.plans.claim("create", "worker")["state"] == "claimed"
    after = env.job(follow["id"])
    assert "rejected_sections" not in after and children(env, follow["id"]) == []


def test_a_follow_up_decision_refused_again_is_recorded_without_a_third_call(env):
    plan = due_review(env)
    result, _ = env.evaluate(omits_the_precondition(env, plan))
    [follow] = children(env, result["id"])
    second, provider = run(env, omits_the_precondition(env, plan), job_id=follow["id"])
    assert second["state"] == "complete" and provider.calls == 1
    assert env.job(follow["id"])["rejected_sections"] == [REFUSED_DECISION]
    assert env.step(plan)["state"] == "waiting"
    # Bounded: a follow-up never queues another, and the host finds nothing left to pay for.
    assert children(env, follow["id"]) == []
    assert len(env.rows("SELECT id FROM mind_appraisals WHERE json_extract(data,'$.stimulus')=?", FOLLOW_UP)) == 1
    assert run(env, lambda shown, context: pytest.fail("A third call was made"))[0]["state"] == "idle"
    # A later owner message is a reason of its own, so an ordinary review still asks.
    env.owner_message("owner-asks-about-the-clock")
    assert env.ticks() == 2
    third, _ = env.evaluate(lambda shown, context: Appraisal(reason="Accounted for now", action_decisions=[
        env.decision(shown[plan["id"]], "execute", conditions_met=[PRECONDITION])]))
    assert env.job(third["id"])["stimulus"] == "plan-review" and env.step(plan)["state"] == "ready"


def test_a_refused_plan_change_alone_is_asked_again_too(env):
    """plan_changes is already upstream of action_decisions; with nothing resting on it, it strands
    the plan it was arranging in the same way, so it asks again by the same rule."""
    owner = env.source("owner-chat")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    def script(shown, context):
        return Appraisal(reason="Arrange it", values={"mood": 52}, plan_changes=[PlanChange(
            action="create", key="window", goal="Send it", motivation="A whim", reason="Own idea",
            evidence_ids=[owner], steps=[PlanStep(id="send", actor="contact", goal="Send it",
                completion="Accepted", not_before=later(env, days=2), not_after=later(env, days=1))])])
    result, _ = run(env, script, job_id=job)
    assert result["state"] == "complete" and env.mind.read()["dimensions"]["mood"]["value"] == 52
    data = env.job(job)
    assert data["rejected_sections"] == [{"section": "plan_changes", "code": "invalid-value",
                                          "message": "Time window must end after it begins"}]
    assert "held_sections" not in data and [p["key"] for p in env.plans.read()["plans"]] == []
    [follow] = children(env, job)
    assert follow["stimulus"] == FOLLOW_UP and follow["section_review"]["held_sections"] == []


def test_a_retry_after_a_whole_commit_failure_tells_the_model_what_was_refused(env):
    """WP2 writes rejected_sections during apply(); a commit that then fails as a whole keeps them,
    and WP3 hands them to the next attempt beside its own static error code."""
    from kin_mind.exploration import Explorations
    from kin_mind.exploration_decisions import SharingDecision
    Explorations(env.mind)
    owner = env.source("owner-asks-to-pause-exploring")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    stale = HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner],
                          reason="The owner asked", expected_revision=7)
    result, _ = run(env, lambda shown, context: Appraisal(reason="A refused preference, then a fault in the event",
        values={"mood": 99}, habits=stale,
        sharing=[SharingDecision(exploration_id="explore_missing", decision="keep", reason="Keep")]), job_id=job)
    assert result["state"] == "pending" and result["error"] == "Conflict"
    failed = env.job(job)
    assert failed["rejected_sections"] == [STALE_HABITS] and "result" not in failed

    requeue(env, job)
    seen = {}
    def repaired(shown, context):
        seen["previous"] = context.get("previous_attempt")
        return Appraisal(reason="Restated", values={"mood": 60}, habits=stale.model_copy(update={"expected_revision": 0}))
    second, _ = run(env, repaired, job_id=job)
    assert second["state"] == "complete" and env.memory.habits.read()["preferences"]["exploration_paused"] is True
    assert seen["previous"]["rejected_sections"] == [STALE_HABITS]
    assert seen["previous"]["error_class"] == "Conflict"
    assert seen["previous"]["message"] == "Sharing decision needs an exploration in this scope"
    assert failed["error_detail"]["message"] == seen["previous"]["message"]
    # Static host facts only; no owner text and no payload reaches the next request.
    assert set(seen["previous"]) <= {"error_class", "code", "message", "rejected_sections", "held_sections",
                                     "held_decisions", "dropped_fields"}
    # A committed attempt keeps none of it, so a later request carries no stale feedback.
    assert not {"error", "error_detail", "rejected_sections"} & set(env.job(job))


def test_a_failing_follow_up_row_is_capped_quarantined_and_resumable(env):
    plan = due_review(env)
    result, _ = env.evaluate(omits_the_precondition(env, plan))
    [follow] = children(env, result["id"])
    provider = Flaky(lambda shown, context: Appraisal(reason="Accounted for now", action_decisions=[
        env.decision(shown[plan["id"]], "execute", conditions_met=[PRECONDITION])]),
        [RuntimeError("deepseek-http-4" + code) for code in ("00", "01", "03", "04", "22")])
    for attempt, expected in enumerate(["pending"] * 4 + ["needs-repair"], start=1):
        requeue(env, follow["id"])
        status = jobs(env).run_one(provider, job_id=follow["id"])
        assert (status["state"], status["attempts"]) == (expected, attempt)
    assert provider.attempts == 5 and env.job(follow["id"])["repair_reason"] == "charged-attempts-exhausted:5"
    quarantine = metrics(env, "appraisal_quarantined")
    assert [(m["appraisal"], m["stimulus"], m["lane"]) for m in quarantine] == [(follow["id"], FOLLOW_UP, "action")]
    # A quarantined row is never claimed again, whichever way the host asks for it.
    requeue(env, follow["id"])
    assert jobs(env).run_one(provider, job_id=follow["id"]) == {"state": "idle"}
    assert jobs(env).run_one(provider) == {"state": "idle"} and provider.attempts == 5

    resume = recover_quarantined(env.mind, job_ids=[follow["id"]], command_id="stage1-follow-up", source="owner approval")
    assert resume["resumed"] == [follow["id"]]
    status = jobs(env).run_one(provider, job_id=follow["id"])
    assert status["state"] == "complete" and provider.attempts == 6
    assert env.step(plan)["state"] == "ready" and env.step(plan)["decision"]["action"] == "execute"
    # The resumed row is still a follow-up: it restates, and queues none of its own.
    after = env.job(follow["id"])
    assert after["stimulus"] == FOLLOW_UP and after["recovery_history"][0]["command_id"] == "stage1-follow-up"
    assert after["recovery_history"][0]["repair_reason"] == "charged-attempts-exhausted:5"
    assert children(env, follow["id"]) == [] and not {"error", "repair_reason"} & set(after)


def test_a_resumed_batch_parent_does_not_apply_a_released_child_twice(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    queue = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-missing-structured-result")] * 2)
    child = delivery(queue, source, "child")
    parent = delivery(queue, source, "parent")
    schedule(mind, {parent: 0, child: 1})
    assert queue.run_one(provider, lane="action")["state"] == "pending"
    assert rows(mind)[child]["state"] == "batched"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (parent,))
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "needs-repair"
    # WP4: a quarantined parent returns what it had absorbed to the queue.
    released = rows(mind)[child]
    assert released["state"] == "pending" and released["available"] <= time.time()

    # The released child is judged on its own before anyone resumes the parent.
    first = Appraisals(mind).run_one(provider, lane="action")
    assert (first["id"], first["state"]) == (child, "complete")
    child_data = json.loads(rows(mind)[child]["data"])
    child_sources, child_event = child_data["evidence_ids"], child_data["result"]["event_id"]
    assert [e["id"] for e in provider.calls[-1]["new_evidence"]] == child_sources

    resume = recover_quarantined(mind, job_ids=[parent], command_id="stage1-batch", source="owner approval")
    assert resume["resumed"] == [parent]
    settled = Appraisals(mind).run_one(provider, lane="action")
    assert (settled["id"], settled["state"]) == (parent, "complete")
    # The child's evidence was appraised once and committed once; the resumed parent never sees it again.
    assert child_sources and set(json.loads(rows(mind)[parent]["data"])["evidence_ids"]).isdisjoint(child_sources)
    assert set(child_sources).isdisjoint(e["id"] for e in provider.calls[-1]["new_evidence"])
    with mind.engine.db.connect() as conn:
        indexed = conn.execute("SELECT event_id FROM mind_semantic_sources WHERE scope=? AND source_id=?",
                               (mind.scope.key(), child_sources[0])).fetchall()
    assert [r["event_id"] for r in indexed] == [child_event]
    # Its own commit stands: the parent's settlement never overwrote it.
    assert json.loads(rows(mind)[child]["data"])["result"]["event_id"] == child_event
    assert "batch_id" not in json.loads(rows(mind)[child]["data"])["result"]
    assert len(provider.calls) == 4


def test_a_held_decision_and_a_refused_advisory_section_in_one_appraisal(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    job = env.enqueue("owner-chat")
    def script(shown, context):
        # Someone else authorizes the step while the model thinks: this decision loses its fence.
        env.decide(env.current(plan), "execute")
        return Appraisal(reason="Judged on the older view", values={"mood": 57},
            action_decisions=[env.decision(shown[plan["id"]])],
            session_advice=SessionAdvice(action="recall", reason="Context looks thin", evidenceIds=["invented-observation"]))
    result, provider = run(env, script, job_id=job, session=SESSION)
    assert result["state"] == "complete" and provider.calls == 1
    data = env.job(job)
    # Each is recorded by its own mechanism: a held decision is not a refused section.
    assert [h["code"] for h in data["held_decisions"]] == ["step-touched"]
    assert data["rejected_sections"] == [UNKNOWN_ADVICE] and "held_sections" not in data
    # The independent sections committed and the newer decision stands.
    view = env.mind.read()
    assert view["dimensions"]["mood"]["value"] == 57 and view["session_advice"] is None
    assert env.step(plan)["state"] == "ready" and env.step(plan)["decision"]["action"] == "execute"
    # Exactly one coalesced review asks for the held decision again; the advisory section asks for nothing.
    assert [e["reason"] for e in env.events()] == ["held-decision"] and env.ticks(6) == 1
    assert children(env, job) == []
