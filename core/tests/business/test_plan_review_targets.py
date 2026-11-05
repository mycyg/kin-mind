"""A plan review judged in an assessment keeps its plan-review targets apart from the exploration
targets (an interaction batch with one merged plan review failed twice with a bare KeyError after
the model had answered, and was quarantined as `repeated-failure:KeyError`).

run_one used one name, `targets`, for both: the exploration targets it read first, and then the
plan-review targets ({event_id, plan_id}) of the reviews merged into the assessment. With one plan
review merged in, the sharing check read `targets[0]["exploration_id"]` from a plan-review target;
with none, the list was empty and an exploration result lost its sharing check under semantic
actions."""
import json
from datetime import datetime, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput

from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.memory import MemoryContinuity
from kin_mind.plans import AutonomousPlans
from kin_mind.state import Mind


@pytest.fixture
def env(tmp_path):
    clock = [datetime.now(timezone.utc)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-plan-review"), clock=lambda: clock[0].isoformat())

    def source(key, authority="explicit", **metadata):
        return mind.engine.receive(SourceInput(namespace="plan-review-test", key=key, text=key, scope=mind.scope,
            authority=authority, occurred_at=mind.clock(),
            metadata={"role": "user" if authority == "explicit" else "assistant", "host_event": "message", **metadata}))["id"]
    initial = source("owner-authorizes-autonomous-work")
    mind.initialize(agent_version="planning-v1", evidence_ids=[initial])
    MemoryContinuity(mind).configure({"records": True, "semantic_actions": True, "autonomous_plans": True})
    return mind, source, initial


class Reviewer:
    """The model's answer: a plain judgment, with no plan decision and no sharing decision."""

    def __init__(self):
        self.calls = 0

    def appraise(self, context):
        self.calls += 1
        return Appraisal(reason="Looked at what the tick queued"), {"provider": "deepseek", "model": "synthetic"}


def queue_row(mind, job_id):
    with mind.engine.db.connect() as conn:
        found = conn.execute("SELECT state,data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()
    return found["state"], json.loads(found["data"])


def test_a_single_plan_review_commits_and_answers_its_plan(env):
    mind, source, initial = env
    plans, actions, jobs = AutonomousPlans(mind), ActionEvents(mind), Appraisals(mind)
    plan = plans.manage({"command_id": "create:clock", "action": "create", "key": "clock", "goal": "Make a small clock",
                         "motivation": "A shared interest", "reason": "A sourced idea", "evidence_ids": [initial],
                         "steps": [{"id": "make", "actor": "create", "goal": "Make the clock", "completion": "A verified SVG"}]})
    # A new plan is due for review at once: the tick wakes one review, the drain queues its appraisal.
    [event_id] = plans.tick(actions)
    actions.drain(jobs)
    with mind.engine.db.connect() as conn:
        job_id = json.loads(conn.execute("SELECT data FROM mind_action_events WHERE id=?", (event_id,)).fetchone()[0])["job_id"]
    reviewer = Reviewer()
    jobs.run_one(reviewer, job_id=job_id)
    state, data = queue_row(mind, job_id)
    # Before the fix: `pending`, error KeyError ('exploration_id'), after the model had answered.
    assert (state, data.get("error")) == ("complete", None)
    assert reviewer.calls == 1
    assert data["plan_review_targets"] == [{"event_id": event_id, "plan_id": plan["id"], "shown": True}]
    actions.drain(jobs)
    with mind.engine.db.connect() as conn:
        # The review registered the plan it was shown, and its event ends with its appraisal.
        assert [r[0] for r in conn.execute("SELECT kind FROM mind_plan_reviews WHERE plan_id=?", (plan["id"],))] == ["version-seen"]
        assert conn.execute("SELECT state FROM mind_action_events WHERE id=?", (event_id,)).fetchone()[0] == "complete"


def test_an_exploration_result_keeps_its_sharing_check_under_semantic_actions(env):
    mind, source, initial = env
    result = source("exploration-finished", authority="model", host_event="exploration-result", exploration_id="explore_synthetic")
    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([result], "planning-v1", origin="reflection", stimulus="exploration-result")
    jobs.run_one(Reviewer(), job_id=job["id"])
    state, data = queue_row(mind, job["id"])
    # The answer decided nothing about the result it was asked about: the host does not commit it.
    assert (state, data.get("error")) == ("pending", "deepseek-missing-sharing-decision")
