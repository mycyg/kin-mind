"""Recipient contracts and the actual execution facts shown at the next review."""
from datetime import timedelta

import pytest

from eventmem.core.db import Conflict, Missing, dumps
from kin_mind import recipient
from kin_mind.autonomy_models import ActionDecision, PlanChange
from kin_mind.companion_view import CompanionView, companion_scope
from kin_mind.decision_context import appraisal_plan, compact_plan
from kin_mind.plans import AutonomousPlans
from kin_mind.state import Mind
from test_autonomous_plans import env, create, decide


def view(mind, person):
    return companion_scope(CompanionView(mind.engine, mind.scope, person, "synthetic:" + person))


@pytest.mark.parametrize("person", ["person:xiaoguang", "person:yiyi", "group:family"])
def test_explicit_recipient_creation_is_idempotent_in_its_own_context(env, person):
    mind, plans, source, clock, initial = env
    original = recipient.current()
    plan = create(env, recipient_person_id=person)
    assert plan["recipient_person_id"] == person
    assert recipient.current() == original
    with view(mind, person):
        assert create(env) == plan
        assert plans.read(plan["id"])["plans"][0]["recipient_person_id"] == person
    if person != original:
        with pytest.raises(Missing):
            plans.read(plan["id"])
        other = create(env)
        assert other["id"] != plan["id"]


def test_stable_plan_change_follows_owner_without_moving_it(env):
    mind, plans, source, clock, initial = env
    with view(mind, "person:yiyi"):
        plan = create(env)
    request = {"command_id": "update-y", "action": "update", "id": plan["id"],
               "expected_revision": plan["revision"], "reason": "shared review updates the request",
               "evidence_ids": [initial], "goal": "Continue Y's request"}
    updated = plans.manage(request)
    assert updated["recipient_person_id"] == "person:yiyi"
    assert updated["goal"] == request["goal"]
    assert recipient.current() == "person:xiaoguang"
    with view(mind, "person:yiyi"):
        assert plans.manage(request) == updated
    with pytest.raises(Conflict, match="recipient cannot be changed"):
        plans.manage({**request, "command_id": "move-y", "expected_revision": updated["revision"],
                      "recipient_person_id": "person:xiaoguang"})
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_plans").fetchone()[0] == 1


def test_legacy_stable_id_keeps_x_owner_from_other_context(env):
    mind, plans, source, clock, initial = env
    plan = create(env)
    plan.pop("recipient_person_id")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plans SET data=? WHERE id=?", (dumps(plan), plan["id"]))
    with view(mind, "person:yiyi"):
        updated = plans.manage({"command_id": "pause-legacy", "action": "pause", "id": plan["id"],
            "expected_revision": plan["revision"], "reason": "pause shared plan", "evidence_ids": [initial]})
        assert recipient.current() == "person:yiyi"
    assert updated.get("recipient_person_id", "person:xiaoguang") == "person:xiaoguang"
    assert plans.read(plan["id"])["plans"][0]["status"] == "paused"


def test_plan_and_decision_schema_accept_only_known_optional_recipients():
    decision = {"plan_id": "new-key", "step_id": "one", "expected_revision": 1,
                "action": "wait", "reason": "review", "evidence_ids": ["src_test"]}
    assert ActionDecision(**decision).recipient_person_id is None
    for person in ("person:xiaoguang", "person:yiyi", "group:family"):
        assert ActionDecision(**decision, recipient_person_id=person).recipient_person_id == person
    for model, raw in ((ActionDecision, decision), (PlanChange, {
        "action": "pause", "id": "plan-test", "expected_revision": 1, "reason": "review", "evidence_ids": ["src_test"]})):
        with pytest.raises(ValueError):
            model(**raw, recipient_person_id="person:unknown")


def test_pending_work_and_expired_wait_are_visible_without_rewriting_window(env):
    mind, plans, source, clock, initial = env
    expired = (clock[0] - timedelta(hours=12)).isoformat()
    plan = create(env, steps=[
        {"id": "make", "actor": "create", "goal": "Do requested work", "completion": "Verified result"},
        {"id": "report", "actor": "contact", "goal": "Report progress", "completion": "Accepted",
         "depends_on": ["make"], "not_after": expired}])
    review_at = (clock[0] + timedelta(hours=2)).isoformat()
    decide(env, plan, "wait", step_id="report", next_review_at=review_at)
    result = plans.read(plan["id"])
    shown = compact_plan(result["plans"][0])
    pending, waiting = shown["steps"]
    assert pending["state"] == "pending"
    assert pending["execution"] == {"plan_run": None, "linked_wish": None,
                                    "receipt_count": 0, "latest_outcome": None}
    assert waiting["state"] == "waiting"
    assert waiting["execution_window"] == {"state": "expired", "review_required": True}
    assert waiting["not_after"] == expired and shown["next_review_at"] == review_at
    assert "not proof of execution" in result["state_meanings"]["ready"]
    assert plans.claim("create", "worker")["state"] == "waiting"


def test_real_run_lease_acknowledgement_recovery_and_completion_are_projected(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env))
    ready = plans.read(plan["id"])["plans"][0]["steps"][0]
    assert ready["state"] == "ready" and ready["execution"]["plan_run"] is None
    run = plans.claim("create", "worker")["run"]
    def shown():
        return compact_plan(plans.read(plan["id"])["plans"][0])["steps"][0]
    claimed = shown()["execution"]["plan_run"]
    assert claimed["id"] == run["id"] and claimed["state"] == "running"
    assert claimed["lease_status"] == "active" and claimed["execution_started_at"] is None
    plans.started(run["id"], "worker", run["fence"])
    assert shown()["execution"]["plan_run"]["execution_started_at"] == mind.clock()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plan_runs SET lease_until=0 WHERE id=?", (run["id"],))
    assert shown()["execution"]["plan_run"]["lease_status"] == "expired"
    restarted = AutonomousPlans(Mind(mind.engine, mind.scope, clock=mind.clock))
    assert restarted.recover(workers_stopped=True)["recovered"] == [run["id"]]
    recovered = shown()
    assert recovered["state"] == "waiting"
    assert recovered["execution"]["plan_run"]["state"] == "interrupted"
    assert recovered["execution"]["plan_run"]["lease_status"] == "released"
    assert recovered["execution"]["plan_run"]["recovery_reason"] == "host-restart"
    assert recovered["execution"]["latest_outcome"]["checkpoint_retained"] is True
    assert recovered["receipts"][-1]["checkpoint_retained"] is True
    with pytest.raises(Conflict):
        plans.settle(run["id"], "worker", run["fence"], state="failed", result={})
    current = plans.read(plan["id"])["plans"][0]
    decide(env, current)
    run2 = plans.claim("create", "worker-2")["run"]
    assert run2["id"] != run["id"]
    plans.settle(run2["id"], "worker-2", run2["fence"], state="completed",
                 result={"verified": True, "source_id": source("verified-result")})
    complete = appraisal_plan(plans.read(plan["id"])["plans"][0])
    assert complete["status"] == "completed"
    assert complete["steps"][0]["execution"]["plan_run"]["state"] == "completed"
    assert complete["steps"][0]["execution"]["latest_outcome"]["verified"] is True
    assert complete["steps"][0]["execution"]["receipt_count"] == 2


def test_failed_host_receipt_remains_visible_and_is_not_running(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env))
    run = plans.claim("create", "worker")["run"]
    plans.settle(run["id"], "worker", run["fence"], state="failed", result={"reason": "executor failed"})
    shown = compact_plan(plans.read(plan["id"])["plans"][0])["steps"][0]
    assert shown["state"] == "waiting"
    assert shown["execution"]["plan_run"]["state"] == "failed"
    assert shown["execution"]["latest_outcome"]["reason"] == "executor failed"


def test_expired_claim_cannot_acknowledge_first_execution(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env))
    run = plans.claim("create", "late-worker")["run"]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plan_runs SET lease_until=0 WHERE id=?", (run["id"],))
    with pytest.raises(Conflict, match="lease expired"):
        plans.started(run["id"], "late-worker", run["fence"])
    with mind.engine.db.connect() as conn:
        import json
        stored = json.loads(conn.execute("SELECT data FROM mind_plan_runs WHERE id=?", (run["id"],)).fetchone()[0])
    assert "execution_started_at" not in stored


def test_linked_contact_ledger_is_distinguished_from_plan_run(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env, actor="contact"), strength=90)
    linked = plans.sync_wishes()
    assert len(linked) == 1
    shown = compact_plan(plans.read(plan["id"])["plans"][0])["steps"][0]
    assert shown["execution"]["plan_run"] is None
    assert shown["execution"]["linked_wish"] == {"id": linked[0], "status": "wanted"}
