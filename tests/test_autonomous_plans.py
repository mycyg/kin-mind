"""Synthetic planning/fault replays; no private conversations or channel sends."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict
from eventmem.core.models import Scope, SourceInput
from kin_mind.actions import ActionEvents
from kin_mind.memory import MemoryContinuity
from kin_mind.plans import AutonomousPlans
from kin_mind.state import Mind


@pytest.fixture
def env(tmp_path):
    clock = [datetime.now(timezone.utc)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-planning"), clock=lambda: clock[0].isoformat())
    def source(key, authority="explicit"):
        return mind.engine.receive(SourceInput(namespace="planning-test", key=key, text=key, scope=mind.scope,
            authority=authority, occurred_at=mind.clock(), metadata={"role": "user" if authority == "explicit" else "assistant", "host_event": "message"}))["id"]
    initial = source("owner-authorizes-autonomous-work")
    mind.initialize(agent_version="planning-v1", evidence_ids=[initial])
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic_actions": True, "autonomous_plans": True, "creative_execution": True, "usage_reinforcement": True})
    return mind, AutonomousPlans(mind), source, clock, initial


def create(env, *, actor="create", steps=None, key="idea", **extra):
    mind, plans, source, clock, initial = env
    return plans.manage({"command_id": "create:" + key, "action": "create", "key": key, "goal": "Make a small illustrated clock",
        "motivation": "Continue a shared interest", "reason": "A sourced idea", "evidence_ids": [initial],
        "steps": steps or [{"id": "make", "actor": actor, "goal": "Make the clock", "completion": "A verified SVG is saved"}], **extra})


def decide(env, plan, action="execute", step_id="make", evidence=None, **extra):
    mind, plans, source, clock, initial = env
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, evidence or [initial])
        return plans.decide(conn, {"plan_id": plan["id"], "step_id": step_id, "expected_revision": plan["revision"], "action": action,
            "reason": "Context supports this decision", "evidence_ids": evidence or [initial], **extra},
            "decision:" + action + ":" + str(plan["revision"]), {"provider": "deepseek", "reasoning": "high", "agent_version": "planning-v1"}, refs)


@pytest.mark.parametrize("case", [
    "implicit-goal", "long-term", "singapore-time", "before-window", "missed-window", "dependency-wait", "dependency-cycle", "dependency-missing",
    "owner-proposed", "owner-accepted", "owner-completed", "owner-declined", "owner-fabricated", "defer", "abandon", "pause", "resume", "reschedule",
    "cancel", "goal-change", "new-chat", "duplicate-command", "conflicting-command", "model-unavailable",
])
def test_planning_replay_24(env, case):
    mind, plans, source, clock, initial = env
    owner_case = case.startswith("owner-")
    steps = [{"id": "make", "actor": "owner" if owner_case else "create", "goal": "Bring a photograph" if owner_case else "Make a clock", "completion": "Actual result exists"}]
    if case == "before-window":
        steps[0]["not_before"] = (clock[0] + timedelta(days=1)).isoformat()
    if case == "missed-window":
        steps[0]["not_after"] = (clock[0] - timedelta(minutes=1)).isoformat()
    if case in {"dependency-wait", "dependency-cycle", "dependency-missing"}:
        steps[0]["depends_on"] = ["input"]
        if case != "dependency-missing":
            steps.append({"id": "input", "actor": "owner", "goal": "Choose a photo", "completion": "Photo supplied", "depends_on": ["make"] if case == "dependency-cycle" else []})
    if case in {"dependency-cycle", "dependency-missing"}:
        with pytest.raises(ValueError):
            create(env, steps=steps)
        return
    plan = create(env, steps=steps, next_review_at="2028-02-01T12:00:00" if case in {"long-term", "singapore-time"} else None)
    if case == "implicit-goal":
        assert plan["motivation"] and plan["steps"][0]["state"] == "pending"
    elif case == "long-term":
        assert plan["next_review_at"].startswith("2028") and "expires_at" not in plan
    elif case == "singapore-time":
        assert plan["next_review_at"].endswith("+08:00")
    elif case == "owner-proposed":
        assert plan["steps"][0]["owner_status"] == "proposed"
        assert plans.claim("create", "worker")["state"] == "waiting"
    elif case in {"owner-accepted", "owner-completed", "owner-declined", "owner-fabricated"}:
        evidence = [source("actual-owner-response", "model" if case == "owner-fabricated" else "explicit")]
        if case == "owner-fabricated":
            with pytest.raises(Conflict):
                decide(env, plan, "owner_accepted", evidence=evidence)
        else:
            updated = decide(env, plan, case.replace("-", "_"), evidence=evidence)
            assert updated["steps"][0]["owner_status"] == case.split("-")[1]
            assert updated["steps"][0]["state"] == {"owner-accepted": "waiting", "owner-completed": "completed", "owner-declined": "abandoned"}[case]
    elif case in {"defer", "abandon"}:
        updated = decide(env, plan, "wait" if case == "defer" else "abandon")
        assert updated["steps"][0]["state"] in {"waiting", "abandoned"}
        assert plans.claim("create", "worker")["state"] == "waiting"
    elif case in {"pause", "resume", "reschedule", "cancel", "goal-change"}:
        plan = decide(env, plan)
        action = "update" if case == "goal-change" else "pause" if case == "resume" else case
        request = {"command_id": case, "id": plan["id"], "expected_revision": plan["revision"], "action": action,
                   "reason": "Owner changes the arrangement", "evidence_ids": [source(case)], "next_review_at": "2027-01-01T08:00:00"}
        if case == "goal-change":
            request["goal"] = "Make a poem instead"
        updated = plans.manage(request)
        if case == "resume":
            updated = plans.manage({**request, "command_id": "resume-now", "action": "resume", "expected_revision": updated["revision"]})
        assert updated["id"] == plan["id"] and not updated["steps"][0].get("decision")
        assert plans.claim("create", "worker")["state"] == "waiting"
    elif case == "duplicate-command":
        assert create(env, steps=steps) == plan
    elif case == "conflicting-command":
        with pytest.raises(Conflict):
            create(env, key="idea", goal="Different content")
    elif case == "model-unavailable":
        assert plans.claim("create", "worker")["state"] == "waiting"
    else:
        plan = decide(env, plan)
        if case == "new-chat":
            MemoryContinuity(mind).ingest({"id": "new-input", "kind": "owner-message", "at": mind.clock(), "text": "I changed my mind"})
        reason = plans.read(plan["id"])["plans"][0]["steps"][0]["waiting_reason"]
        assert reason == {"before-window": "time-window-not-open", "missed-window": "missed-window-review-required", "dependency-wait": "dependency-unfinished", "new-chat": "new-owner-evidence"}[case]
        assert plans.claim("create", "worker")["state"] == "waiting"


def test_concurrent_claim_restart_and_late_result(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner: plans.claim("create", owner), ["one", "two"]))
    assert [r["state"] for r in results].count("claimed") == 1
    claimed = next(r for r in results if r["state"] == "claimed")
    run = claimed["run"]
    with pytest.raises(Conflict):
        plans.settle(run["id"], run["owner"], run["fence"], state="completed", result={"verified": True})
    with pytest.raises(Conflict):
        plans.recover()
    assert plans.recover(workers_stopped=True)["recovered"] == [run["id"]]
    assert plans.read(plan["id"])["plans"][0]["steps"][0]["state"] == "waiting"
    with pytest.raises(Conflict):
        plans.settle(run["id"], run["owner"], run["fence"], state="failed", result={})


def test_due_review_is_deduplicated_not_an_action(env):
    mind, plans, source, clock, initial = env
    plan = create(env)
    actions = ActionEvents(mind)
    assert plans.tick(actions) == plans.tick(actions)
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_action_events").fetchone()[0] == 1
    assert plans.claim("create", "worker")["state"] == "waiting"
    updated = decide(env, plan, "wait", next_review_at=(clock[0] + timedelta(days=30)).isoformat())
    assert plans.tick(actions) == []
    assert updated["next_review_at"]


@pytest.mark.parametrize("score,action,eligible", [(12, "execute", True), (99, "wait", False)])
def test_scores_are_context_not_action_gates(env, score, action, eligible):
    mind, plans, source, clock, initial = env
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state["dimensions"]["initiative"].update(score=score, baseline=score, at=mind.clock())
        mind._save(conn, state)
    plan = decide(env, create(env, actor="contact"), action)
    plans.sync_wishes()
    assert mind.contact_candidate()["eligible"] is eligible
    if eligible:
        attempt = mind.claim_contact(owner_epoch="same-input")
        assert mind.check_contact(attempt["id"], "same-input")["eligible"]
        mind.settle_contact(attempt_id=attempt["id"], state="pending")
        mind.settle_contact(attempt_id=attempt["id"], state="accepted", message_id="synthetic-receipt")
        assert plans.read(plan["id"])["plans"][0]["steps"][0]["state"] == "completed"
        assert not mind.contact_candidate()["eligible"]


def test_partial_contact_does_not_complete_plan(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env, actor="contact"))
    plans.sync_wishes()
    attempt = mind.claim_contact(owner_epoch="same-input")
    mind.settle_contact(attempt_id=attempt["id"], state="pending")
    mind.settle_contact(attempt_id=attempt["id"], state="accepted", message_id="synthetic-first-only", partial=True, canceled_bubbles=1)
    step = plans.read(plan["id"])["plans"][0]["steps"][0]
    assert step["state"] == "waiting" and step["receipts"][0]["partial"]


def test_source_change_requires_review(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env))
    with mind.engine.db.connect(write=True) as conn:
        rid = mind._evidence(conn, [initial])[0]["record_id"]
        row = mind.engine._get(conn, rid)
        row["status"] = "superseded"
        conn.execute("UPDATE records SET status='superseded',data=? WHERE id=?", (json.dumps(row), rid))
    assert plans.claim("create", "worker")["state"] == "waiting"


def test_pause_invalidates_running_worker(env):
    mind, plans, source, clock, initial = env
    plan = decide(env, create(env))
    claimed = plans.claim("create", "worker")
    current = plans.read(plan["id"])["plans"][0]
    plans.manage({"command_id": "pause", "action": "pause", "id": current["id"], "expected_revision": current["revision"], "reason": "Owner needs the computer", "evidence_ids": [initial]})
    assert plans.renew(claimed["run"]["id"], "worker", 1)["state"] == "interrupt"

def test_waiting_plan_rechecks_new_evidence_before_its_distant_review(env):
    mind, plans, source, clock, initial = env
    p=decide(env, create(env), 'wait', next_review_at='2028-01-01T09:00:00+08:00')
    actions=ActionEvents(mind)
    assert plans.tick(actions)==[]
    MemoryContinuity(mind).ingest({'id':'new-owner-photo','kind':'owner-message','text':'Here is the requested photo','at':mind.clock()})
    assert len(plans.tick(actions))==1
    assert plans.claim('create','worker')['state']=='waiting'

def test_disabling_plans_revokes_linked_contact_without_losing_history(env):
    mind, plans, *_ = env
    p=decide(env, create(env,actor='contact'))
    plans.sync_wishes()
    MemoryContinuity(mind).configure({'autonomous_plans':False})
    with mind.engine.db.connect() as conn:
        desire=next(d for d in mind._load(conn)['desires'].values() if d.get('plan_id')==p['id'])
        assert not plans.linked_ready(conn,desire)
    assert plans.read(identifier=p['id'],history=True)['plans'][0]['history']
