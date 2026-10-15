"""A later wish to share uses the existing clock-reviewed plan, never a catch-up send."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from eventmem.core.db import Conflict
from kin_mind.actions import ActionEvents
from kin_mind.exploration_decisions import SharingDecision, apply_decisions
from test_activity_sharing import activity
from test_autonomous_plans import env, create, decide  # noqa: F401


def sharing(mind, ref, initial, choice, key):
    event = SimpleNamespace(evidence_ids=[initial], command_id=key, agent_version="planning-v1")
    mind._mutate({"command_id": key, "agent_version": "planning-v1", "expected_revision": mind.read()["revision"],
                  "evidence_ids": [initial]}, "synthetic-sharing",
                 lambda conn, state, eid: apply_decisions(mind, conn, state,
                    [SharingDecision(activity_ref=ref, decision=choice)], event, {}, "plan-review"))


@pytest.mark.parametrize("kind", ["dream", "exploration"])
@pytest.mark.parametrize("final_choice", ["execute", "abandon"])
def test_future_activity_plan_reviews_reschedules_and_rebinds_current_choice(env, kind, final_choice):
    mind, plans, source, clock, initial = env
    ref = activity((mind, None, None, clock), kind)
    sharing(mind, ref, initial, "defer", "later")
    due = clock[0] + timedelta(hours=12)
    step = {"id": "make", "actor": "contact", "goal": "Share this remembered activity if still wanted",
            "completion": "Actual delivery confirmed", "activity_ref": ref, "not_before": due.isoformat()}
    plan = create(env, steps=[step], next_review_at=due.isoformat())
    # Dream content is a pointer, not the evidence that authorizes the plan.
    assert all(r['source_id'] != ref['source_id'] for r in plan['evidence'])
    assert plans.tick(ActionEvents(mind)) == [] and plans.sync_wishes() == []
    clock[0] = due
    first = plans.tick(ActionEvents(mind))
    assert len(first) == 1 and plans.tick(ActionEvents(mind)) == first
    assert plans.sync_wishes() == [], "due means review, not permission to send"
    later = clock[0] + timedelta(hours=2)
    plan = decide(env, plan, "wait", next_review_at=later.isoformat())
    assert plans.sync_wishes() == []
    assert plan['next_review_at'] == later.isoformat()
    clock[0] = later
    if final_choice == 'abandon':
        sharing(mind, ref, initial, 'private', 'keep-to-myself')
        plan = decide(env, plan, 'abandon')
        assert plans.sync_wishes() == [] and plan['status'] == 'completed'
        return
    sharing(mind, ref, initial, 'share', 'now')
    plan = decide(env, plan)
    did, = plans.sync_wishes()
    before = next(d for d in mind.read()['desires'] if d['id'] == did)
    sharing(mind, ref, initial, 'defer', 'changed-window')
    plan = decide(env, plan, 'wait', next_review_at=(clock[0] + timedelta(hours=1)).isoformat())
    # The defer intentionally disables the old sharing revision.
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        assert not mind._desire_ready(conn, state['desires'][did], mind.clock(), state=state)
    clock[0] += timedelta(hours=1)
    sharing(mind, ref, initial, 'share', 'reconsidered')
    plan = decide(env, plan)
    assert plans.sync_wishes() == [did], "reuse the same wish at the new sharing and plan revisions"
    after = next(d for d in mind.read()['desires'] if d['id'] == did)
    assert after['sharing_revision'] > before['sharing_revision']
    assert after['status'] == 'wanted' and after['plan_decision_id'] == plan['steps'][0]['decision']['id']
    with pytest.raises(Conflict):
        decide(env, {**plan, 'revision': plan['revision'] - 1})
