"""Shared affect evaluates each plan-backed wish using its recorded recipient."""
from copy import deepcopy

import pytest

from eventmem.core.db import Missing
from kin_mind import recipient
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.autonomy_models import ActionDecision, PlanChange
from kin_mind.plans import AutonomousPlans
from test_unified_plan_appraisal import setup, arrange, Reviewer, X, Y, G


@pytest.mark.parametrize("targets", [(Y, G), (G, Y)])
def test_one_appraisal_two_contact_plans_sync_and_claim_without_crossing_owner(setup, targets):
    mind, source, clock = arrange(setup)
    sid = source("two-explicit-contact-requests")
    plans = AutonomousPlans(mind)
    untouched = plans.manage({"command_id": "x-untouched", "action": "create", "key": "x-standing",
        "goal": "X's separate request", "motivation": "X asked", "reason": "preserve X", "evidence_ids": [sid],
        "steps": [{"id": "work", "actor": "create", "goal": "Prepare X's work", "completion": "Verified"}]})
    jobs = Appraisals(mind)
    job = jobs.enqueue([sid], "synthetic-v1")
    result = jobs.run_one(Reviewer(Appraisal(reason="Execute both explicit contact requests",
        plan_changes=[PlanChange(action="create", key="shared-key", recipient_person_id=person,
            goal="Say hello", motivation="Explicit request", reason="Ready now", evidence_ids=[sid],
            steps=[{"id": "say", "actor": "contact", "goal": "Say hello", "completion": "Accepted receipt"}])
            for person in targets],
        action_decisions=[ActionDecision(plan_id="shared-key", recipient_person_id=person,
            step_id="say", expected_revision=1, action="execute", reason="Requested now", strength=90,
            evidence_ids=[sid]) for person in targets])), job_id=job["id"])
    assert result["state"] == "complete" and not result.get("rejected_sections"), result
    ids = {}
    for person in targets:
        with recipient.target_scope(mind, person):
            plans.tick(ActionEvents(mind))
            ids[person] = plans.sync_wishes()
            assert len(ids[person]) == 1
    assert recipient.current() == X
    with mind.engine.db.connect() as conn:
        before = deepcopy(mind._load(conn)["desires"])
        assert plans.get(conn, untouched["id"]) == untouched
        assert set(before) == {ids[Y][0], ids[G][0]}
        # Shared initiative still includes both wishes when evaluated from X.
        state = mind._load(conn)
        mind._retarget(conn, state, mind.clock())
        assert state["dimensions"]["initiative"]["target"] == 90
        assert plans.linked_ready(conn, before[ids[Y][0]], state)
        assert plans.linked_ready(conn, before[ids[G][0]], state)
    attempts = {}
    for person in targets:
        with recipient.target_scope(mind, person):
            candidate = mind.contact_candidate()
            assert candidate["eligible"] is True, candidate
            assert [d["id"] for d in candidate["desires"]] == ids[person]
            attempts[person] = mind.claim_contact(owner_epoch="shared-claim")
            assert attempts[person]["recipient_person_id"] == person
            assert attempts[person]["desire_ids"] == ids[person]
            assert plans.sync_wishes() == []
    assert attempts[Y]["id"] != attempts[G]["id"]
    assert recipient.current() == X
    with mind.engine.db.connect() as conn:
        assert mind._load(conn)["desires"] == before
        assert plans.get(conn, untouched["id"]) == untouched
    assert mind.contact_candidate()["eligible"] is False
    with recipient.target_scope(mind, Y):
        mind.settle_contact(attempt_id=attempts[Y]["id"], state="unconfirmed", reason="transport outcome unknown")
    # Shared retarget also needs the wish owner's uncertain-delivery ledger,
    # not only the right plan lookup. G's independent wish remains actionable.
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        assert mind._desire_ready(conn, state["desires"][ids[Y][0]], mind.clock(), state=state) is False
        assert mind._desire_ready(conn, state["desires"][ids[G][0]], mind.clock(), state=state) is True
        mind._retarget(conn, state, mind.clock())
        assert state["dimensions"]["initiative"]["target"] == 90
        assert state["desires"] == before
    assert recipient.current() == X


def test_linked_readiness_uses_wish_owner_but_does_not_hide_broken_plan_reference(setup):
    mind, source, clock = arrange(setup)
    plans = AutonomousPlans(mind)
    with mind.engine.db.connect() as conn:
        with pytest.raises(Missing):
            plans.linked_ready(conn, {"plan_id": "missing-plan", "recipient_person_id": Y})
    assert recipient.current() == X
