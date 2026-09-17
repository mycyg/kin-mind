"""Plan review loop (A0): synthetic replays with an injected clock and a scripted provider.

No private conversations, no model or network calls, no waiting on real time.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import digest
from eventmem.core.models import RevisionInput, Scope, SourceInput
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.autonomy_models import ActionDecision, PlanChange, PlanStep
from kin_mind.memory import MemoryContinuity
from kin_mind.plans import AutonomousPlans
from kin_mind.state import AffectiveEvent, Mind

RECEIPT = {"provider": "deepseek", "model": "deepseek-flash", "reasoning": "high"}


class Env:
    def __init__(self, tmp_path):
        self.clock = [datetime.now(timezone.utc)]
        self.mind = Mind(Engine(tmp_path), Scope(persona="synthetic-plan-review"), clock=lambda: self.clock[0].isoformat())
        self.initial = self.source("owner-authorizes-autonomous-work")
        self.mind.initialize(agent_version="planning-v1", evidence_ids=[self.initial])
        self.memory = MemoryContinuity(self.mind)
        self.memory.configure({"records": True, "semantic_actions": True, "autonomous_plans": True, "creative_execution": True})
        self.plans, self.actions, self.version = AutonomousPlans(self.mind), ActionEvents(self.mind), "planning-v1"

    def source(self, key, authority="explicit"):
        return self.mind.engine.receive(SourceInput(namespace="plan-review-test", key=key, text=key, scope=self.mind.scope,
            authority=authority, occurred_at=self.mind.clock(), metadata={"role": "user" if authority == "explicit" else "assistant", "host_event": "message"}))["id"]

    def create(self, *, key="idea", steps=None, **extra):
        return self.plans.manage({"command_id": "create:" + key, "action": "create", "key": key, "goal": "Make a small illustrated clock",
            "motivation": "Continue a shared interest", "reason": "A sourced idea", "evidence_ids": [self.initial],
            "steps": steps or [{"id": "make", "actor": "create", "goal": "Make the clock", "completion": "A verified SVG is saved"}], **extra})

    def decide(self, plan, action="execute", step_id="make", evidence=None, command=None, **extra):
        """A commit by someone else: the direct host path, outside the evaluation under test."""
        with self.mind.engine.db.connect(write=True) as conn:
            refs = self.mind._evidence(conn, evidence or [self.initial])
            return self.plans.decide(conn, {"plan_id": plan["id"], "step_id": step_id, "expected_revision": plan["revision"], "action": action,
                "reason": "Context supports this decision", "evidence_ids": evidence or [self.initial], **extra},
                command or "decision:" + action + ":" + step_id + ":" + str(plan["revision"]), {**RECEIPT, "agent_version": self.version}, refs)

    def decision(self, shown, action="wait", step_id="make", reason="Judged from the plan view supplied", **extra):
        """What the model returns: always fenced by the revision it was shown."""
        return ActionDecision(plan_id=shown["id"], step_id=step_id, expected_revision=shown["revision"], action=action,
            reason=reason, evidence_ids=[self.initial], **extra)

    def upgrade(self, version):
        """A configuration upgrade reaches the state with the first mutation made under it."""
        self.version = version
        self.mind.record(AffectiveEvent(command_id="upgrade:" + version, agent_version=version, expected_revision=self.mind.read()["revision"],
            evidence_ids=[self.source("upgrade:" + version)], reason="First mutation under the new configuration"))

    def owner_message(self, key):
        self.memory.ingest({"id": key, "kind": "owner-message", "at": self.mind.clock(), "text": key})

    def current(self, plan):
        return self.plans.read(plan["id"])["plans"][0]

    def step(self, plan, step_id="make"):
        return next(s for s in self.current(plan)["steps"] if s["id"] == step_id)

    def rows(self, sql, *args):
        with self.mind.engine.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def events(self):
        return [{**json.loads(r["data"]), "id": r["id"], "state": r["state"]} for r in
                self.rows("SELECT id,state,data FROM mind_action_events WHERE kind='plan-review' ORDER BY created_at,id")]

    def reviews(self, kind):
        return [json.loads(r["data"]) for r in self.rows("SELECT data FROM mind_plan_reviews WHERE kind=? ORDER BY at,command_id", kind)]

    def history(self, plan):
        return self.rows("SELECT revision FROM mind_plan_history WHERE id=? ORDER BY revision", plan["id"])

    def job(self, identifier):
        return json.loads(self.rows("SELECT data FROM mind_appraisals WHERE id=?", identifier)[0]["data"])

    def ticks(self, count=4):
        for _ in range(count):
            self.plans.tick(self.actions)
        return len(self.events())

    def evaluate(self, script, *, job_id=None):
        """One appraisal as the host runs it; script(shown plans by id, context) -> Appraisal."""
        jobs, provider = Appraisals(self.mind, exploration_capabilities={"version": self.version}), Reviewer(script)
        self.actions.drain(jobs)
        result = jobs.run_one(provider, job_id=job_id)
        self.actions.drain(jobs)
        assert result["state"] == "complete", result
        return result, provider

    def enqueue(self, key):
        return Appraisals(self.mind).enqueue([self.source(key)], self.version)["id"]

    def grounded(self, key="grounded"):
        """A waiting plan resting on a source of its own, so that source can change without touching a decision's evidence."""
        own = self.source(key + "-own-evidence")
        plan = self.plans.manage({"command_id": "create:" + key, "action": "create", "key": key, "goal": "Make a small illustrated clock",
            "motivation": "Continue a shared interest", "reason": "A sourced idea", "evidence_ids": [own],
            "steps": [{"id": "make", "actor": "create", "goal": "Make the clock", "completion": "A verified SVG is saved"}]})
        return self.decide(plan, "wait", strength=40, next_review_at=(self.clock[0] + timedelta(days=30)).isoformat()), own

    def revise(self, source_id, action, command):
        """A real change of a source's version through the engine, not a row edit."""
        with self.mind.engine.db.connect() as conn:
            rid = "mem_" + digest([source_id, "root"])[:32]
            revision = self.mind.engine._get(conn, rid)["revision"]
        self.mind.engine.revise(rid, RevisionInput(expected_revision=revision, command_id=command, action=action, reason="Synthetic source change"))

    def host_minutes(self, count, script):
        """The host's minute review `count` times: tick, drain, at most one appraisal, drain. Returns the model calls."""
        calls = 0
        for _ in range(count):
            self.clock[0] += timedelta(minutes=1)
            self.plans.tick(self.actions)
            jobs, provider = Appraisals(self.mind, exploration_capabilities={"version": self.version}), Reviewer(script)
            self.actions.drain(jobs)
            assert jobs.run_one(provider)["state"] in {"complete", "idle"}
            self.actions.drain(jobs)
            calls += provider.calls
        return calls

    def wakeups(self, plan):
        return [(json.loads(r["reason"]), r["event_id"]) for r in
                self.rows("SELECT reason,event_id FROM mind_plan_wakeups WHERE plan_id=? ORDER BY at,key_digest", plan["id"])]


def waits(env, plan, *, days=30, strengths=None):
    """The model keeps answering `wait`, citing only what this evaluation supplied; strengths makes each answer differ."""
    def script(shown, context):
        extra = {"strength": next(strengths)} if strengths else {}
        return Appraisal(reason="Still waiting", action_decisions=[ActionDecision(plan_id=plan["id"], step_id="make",
            expected_revision=shown[plan["id"]]["revision"], action="wait", reason="Nothing new", evidence_ids=[context["new_evidence"][0]["id"]],
            next_review_at=(env.clock[0] + timedelta(days=days)).isoformat(), **extra)])
    return script


class Reviewer:
    def __init__(self, script):
        self.script, self.calls = script, 0

    def appraise(self, context):
        self.calls += 1
        return self.script({p["id"]: p for p in context["autonomy_context"]["plans"]["plans"]}, context), dict(RECEIPT)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def later(env, **delta):
    return (env.clock[0] + timedelta(**delta)).isoformat()


def test_decide_stamps_the_current_agent_version(env):
    plan = env.create()
    env.upgrade("planning-v2")
    assert env.current(plan)["agent_version"] == "planning-v1"
    assert env.decide(plan, "wait")["agent_version"] == "planning-v2"


def test_configuration_upgrade_is_reviewed_exactly_once(env):
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, days=30))
    assert env.ticks() == 0
    env.upgrade("planning-v2")
    assert env.ticks() == 1
    before = env.current(plan)
    result, provider = env.evaluate(lambda shown, context: Appraisal(reason="Still waiting",
        action_decisions=[env.decision(shown[plan["id"]], next_review_at=later(env, hours=2))]))
    after = env.current(plan)
    assert provider.calls == 1 and "held_decisions" not in env.job(result["id"])
    assert after["agent_version"] == "planning-v2" == env.mind.read()["agent_version"]
    # waiting -> waiting with the same business fields no longer costs a revision.
    assert after["revision"] == before["revision"] and env.events()[0]["state"] == "complete"
    assert env.ticks(8) == 1
    # A new owner message, then the due time, each wake exactly one review.
    env.owner_message("owner-says-more")
    assert env.ticks() == 2
    env.evaluate(lambda shown, context: Appraisal(reason="Read it; still waiting",
        action_decisions=[env.decision(shown[plan["id"]], next_review_at=later(env, hours=2))]))
    assert env.ticks(8) == 2
    env.clock[0] += timedelta(hours=2, seconds=1)
    assert env.ticks(8) == 3
    # The version never again counts as a change for this plan.
    assert [e["agent_version"] for e in env.events()] == ["planning-v2"] * 3


def test_production_replay_twelve_host_minutes_cost_one_model_call(env):
    """Before the fix: twelve calls and revision 2 -> 14, although each answer asked for two more hours."""
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, days=30))
    env.upgrade("planning-v2")
    calls = 0
    for _ in range(12):
        env.plans.tick(env.actions)
        jobs = Appraisals(env.mind, exploration_capabilities={"version": "planning-v2"})
        env.actions.drain(jobs)
        provider = Reviewer(lambda shown, context: Appraisal(reason="Still waiting",
            action_decisions=[env.decision(shown[plan["id"]], next_review_at=later(env, hours=2))]))
        assert jobs.run_one(provider)["state"] in {"complete", "idle"}
        env.actions.drain(jobs)
        calls += provider.calls
        env.clock[0] += timedelta(minutes=1)
    current = env.current(plan)
    assert calls == 1 and current["revision"] == plan["revision"] and current["agent_version"] == "planning-v2"


def test_unchanged_wait_is_recorded_without_a_revision_and_due_reviews_still_fire_once(env):
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, hours=1))
    stored = env.step(plan)["decision"]
    for round_number in (1, 2, 3):
        env.clock[0] += timedelta(hours=1, seconds=1)
        # Due: exactly one event however often the minute review runs.
        assert env.ticks(6) == round_number
        target, reason = later(env, hours=1), "Nothing new in round " + str(round_number)
        env.evaluate(lambda shown, context: Appraisal(reason="Reviewed",
            action_decisions=[env.decision(shown[plan["id"]], reason=reason, next_review_at=target, **({"strength": 40} if round_number == 2 else {}))]))
        current = env.current(plan)
        assert current["revision"] == plan["revision"] and env.history(plan) == [{"revision": 1}, {"revision": 2}]
        assert current["steps"][0]["revision"] == 1 + 1 and current["steps"][0]["decision"] == stored
        assert datetime.fromisoformat(current["next_review_at"]) == datetime.fromisoformat(target)
        recorded = env.reviews("unchanged-wait")
        assert len(recorded) == round_number
        # This review's own reason, receipt and decision are kept, outside the plan history.
        assert recorded[-1]["decision"]["reason"] == reason and recorded[-1]["receipt"]["provider"] == "deepseek"
        assert recorded[-1]["decision"]["action"] == "wait" and recorded[-1]["evidence"][0]["source_id"] == env.initial
        assert recorded[-1]["decision_id"] == stored["id"] and recorded[-1]["step_id"] == "make"
        assert env.ticks(6) == round_number


def test_due_review_fires_after_a_recorded_review_that_kept_the_same_time(env):
    """A review woken by an owner message may keep the same review time; that time has not fired yet and still must.

    Each reason has a key of its own: the owner message consumed ["owner_epoch", n], never ["due", T].
    """
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, hours=2))
    target = env.current(plan)["next_review_at"]
    env.owner_message("owner-says-more")
    assert env.ticks() == 1
    env.evaluate(lambda shown, context: Appraisal(reason="Read it; same time", action_decisions=[env.decision(shown[plan["id"]], next_review_at=target)]))
    current = env.current(plan)
    assert current["revision"] == plan["revision"] and current["next_review_at"] == target and env.ticks(6) == 1
    env.clock[0] += timedelta(hours=2, seconds=1)
    assert env.ticks(8) == 2


def test_echoed_past_review_time_on_the_recorded_path_neither_loops_nor_strands_the_plan(env):
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, hours=1))
    echoed = env.current(plan)["next_review_at"]
    env.clock[0] += timedelta(hours=1)
    script = lambda shown, context: Appraisal(reason="Echo", action_decisions=[env.decision(shown[plan["id"]], next_review_at=echoed)])
    assert env.host_minutes(12, script) == 1 and len(env.events()) == 1 and env.current(plan)["revision"] == plan["revision"]
    [recorded] = env.reviews("unchanged-wait")
    # The model's own value stays in the audit row; the plan is reviewed again as if no time had been given.
    assert recorded["decision"]["next_review_at"] == echoed and recorded["next_review_at"] == env.current(plan)["next_review_at"]
    assert datetime.fromisoformat(recorded["next_review_at"]) == datetime.fromisoformat(echoed) + timedelta(minutes=1 + 20)
    # Not stranded: that time arrives and wakes exactly one review, which echoes again.
    assert env.host_minutes(12, script) == 1 and env.host_minutes(12, script) == 0 and len(env.reviews("unchanged-wait")) == 2


@pytest.mark.parametrize("strength", [55, 25])
def test_wait_with_a_new_strength_takes_the_normal_path(env, strength):
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, hours=1))
    env.clock[0] += timedelta(hours=1, seconds=1)
    assert env.ticks() == 1
    env.evaluate(lambda shown, context: Appraisal(reason="The wish changed", action_decisions=[env.decision(shown[plan["id"]], strength=strength)]))
    current = env.current(plan)
    assert current["revision"] == plan["revision"] + 1 and current["steps"][0]["strength"] == strength
    assert current["steps"][0]["decision"]["strength"] == strength and env.reviews("unchanged-wait") == []
    assert [r["revision"] for r in env.history(plan)] == [1, 2, 3]


@pytest.mark.parametrize("case,recorded_only", [("identical", True), ("conditions-added", False), ("conditions-removed", False),
    ("procedure-added", False), ("first-wait-after-result", False)])
def test_every_business_field_must_match_for_a_recorded_review(env, case, recorded_only):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, hours=1), conditions_met=["light is good"])
    if case == "first-wait-after-result":
        # An execution outcome removed the stored decision: the next wait is the first verdict on it.
        with env.mind.engine.db.connect(write=True) as conn:
            env.plans.settle_linked(conn, {"plan_id": plan["id"], "plan_step_id": "make"}, {"id": "partial-result", "complete": False})
        plan = env.current(plan)
    env.clock[0] += timedelta(hours=1, seconds=1)
    assert env.ticks() == 1
    extra = {"identical": {"conditions_met": ["light is good"]}, "conditions-added": {"conditions_met": ["light is good", "paper is ready"]},
             "conditions-removed": {}, "procedure-added": {"conditions_met": ["light is good"], "procedure_ids": ["procedure_under_consideration"]},
             "first-wait-after-result": {"conditions_met": ["light is good"]}}[case]
    env.evaluate(lambda shown, context: Appraisal(reason="Reviewed", action_decisions=[env.decision(shown[plan["id"]], **extra)]))
    current = env.current(plan)
    assert current["revision"] == plan["revision"] + (0 if recorded_only else 1)
    assert len(env.reviews("unchanged-wait")) == (1 if recorded_only else 0)
    if not recorded_only:
        # The normal path saved what this decision restated.
        assert current["steps"][0]["decision"]["conditions_met"] == extra.get("conditions_met", [])
        assert current["steps"][0]["decision"]["procedure_ids"] == extra.get("procedure_ids", [])


@pytest.mark.parametrize("hashes,recorded_only", [(["a"], True), (["a", "b"], False), ([], False), (["b"], False)])
def test_delivery_selection_is_compared_by_sha256_and_still_restated_in_full(env, hashes, recorded_only):
    plan = env.decide(env.create(steps=TWO_STEPS), "execute")
    run = env.plans.claim("create", "worker")["run"]
    files = {name: {"sha256": name * 64, "path": "/synthetic/" + name + ".svg", "bytes": 10} for name in ("a", "b")}
    env.plans.settle(run["id"], "worker", run["fence"], state="completed",
        result={"verified": True, "source_id": env.source("verified-result"), "artifacts": list(files.values())})
    plan = env.decide(env.current(plan), "wait", step_id="tell", artifact_hashes=[files["a"]["sha256"]], next_review_at=later(env, hours=1))
    assert [a["sha256"] for a in plan["steps"][1]["delivery_artifacts"]] == [files["a"]["sha256"]]
    env.clock[0] += timedelta(hours=1, seconds=1)
    assert env.ticks() == 1
    chosen = [files[name]["sha256"] for name in hashes]
    env.evaluate(lambda shown, context: Appraisal(reason="Reviewed",
        action_decisions=[env.decision(shown[plan["id"]], step_id="tell", artifact_hashes=chosen)]))
    current = env.current(plan)
    assert current["revision"] == plan["revision"] + (0 if recorded_only else 1)
    assert len(env.reviews("unchanged-wait")) == (1 if recorded_only else 0)
    # Each decision restates the whole selection; an omitted file is no longer selected.
    assert [a["sha256"] for a in current["steps"][1]["delivery_artifacts"]] == chosen


def test_switch_off_restores_revision_per_wait_but_keeps_the_fixes(env):
    env.memory.configure({"plan_review_record_only": False})
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, days=30))
    env.upgrade("planning-v2")
    assert env.ticks() == 1
    env.evaluate(lambda shown, context: Appraisal(reason="Still waiting",
        action_decisions=[env.decision(shown[plan["id"]], next_review_at=later(env, days=30))]))
    current = env.current(plan)
    assert current["revision"] == plan["revision"] + 1 and env.reviews("unchanged-wait") == []
    # The loop is still closed: the version was stamped, so the new revision wakes nothing.
    assert current["agent_version"] == "planning-v2" and env.ticks(8) == 1
    # Holding a stale decision does not depend on the switch.
    job = env.enqueue("owner-chat")
    def script(shown, context):
        env.decide(env.current(plan), "execute")
        return Appraisal(reason="Old view", action_decisions=[env.decision(shown[plan["id"]])])
    env.evaluate(script, job_id=job)
    assert env.job(job)["held_decisions"][0]["code"] == "step-touched" and env.step(plan)["state"] == "ready"


def test_stale_wait_cannot_overwrite_a_newer_execute(env):
    plan = env.create()
    env.decide(plan, "wait", next_review_at=later(env, days=30))
    plan = env.current(plan)
    job = env.enqueue("owner-chat")
    landed = {}
    def script(shown, context):
        # V0 has been shown. Another commit now authorizes the step; nothing has executed yet.
        landed["plan"] = env.decide(env.current(plan), "execute")
        return Appraisal(reason="Judged on V0", values={"curiosity": 64}, action_decisions=[env.decision(shown[plan["id"]])])
    result, provider = env.evaluate(script, job_id=job)
    execute = landed["plan"]["steps"][0]["decision"]
    step = env.step(plan)
    assert step["state"] == "ready" and step["decision"]["id"] == execute["id"] and step["decision"]["action"] == "execute"
    assert env.current(plan)["revision"] == landed["plan"]["revision"] and env.reviews("unchanged-wait") == []
    # The rest of the same evaluation committed.
    assert env.mind.read()["dimensions"]["curiosity"]["value"] == 64
    held = env.job(job)["held_decisions"]
    assert held == result["result"]["held_decisions"] and len(held) == 1
    assert {k: held[0][k] for k in ("plan_id", "step_id", "code")} == {"plan_id": plan["id"], "step_id": "make", "code": "step-touched"}
    assert held[0]["expected"]["step"] == {"revision": 2, "state": "waiting", "decision_id": plan["steps"][0]["decision"]["id"]}
    assert held[0]["actual"]["step"] == {"revision": 3, "state": "ready", "decision_id": execute["id"]}
    assert held[0]["expected"]["plan_revision"] == plan["revision"] and held[0]["actual"]["plan_revision"] == landed["plan"]["revision"]
    # One fresh review asks again (the host's drain has already queued it); the minute review does not multiply it.
    [event] = env.events()
    assert event["state"] == "queued" and event["reason"] == "held-decision" and event["held"] == [{"step_id": "make", "code": "step-touched"}]
    assert event["job_id"] != job and env.rows("SELECT state FROM mind_appraisals WHERE id=?", event["job_id"]) == [{"state": "pending"}]
    assert env.ticks(6) == 1
    # The authorization survived: the executor can still claim it.
    assert env.plans.claim("create", "worker")["state"] == "claimed"


def test_stale_execute_cannot_overwrite_a_newer_wait(env):
    """The mirror image, and the costlier mistake: acting on a judgment that has since been withdrawn."""
    plan = env.decide(env.create(), "execute")
    job = env.enqueue("owner-chat")
    landed = {}
    def script(shown, context):
        landed["plan"] = env.decide(env.current(plan), "wait", next_review_at=later(env, days=30))
        return Appraisal(reason="Judged on V0", action_decisions=[env.decision(shown[plan["id"]], "execute")])
    env.evaluate(script, job_id=job)
    step = env.step(plan)
    assert step["state"] == "waiting" and step["decision"]["id"] == landed["plan"]["steps"][0]["decision"]["id"]
    assert env.job(job)["held_decisions"][0]["code"] == "step-touched"
    assert env.plans.claim("create", "worker")["state"] == "waiting"


def test_decision_on_a_step_that_ran_meanwhile_is_held(env):
    plan = env.decide(env.create(), "execute")
    job = env.enqueue("owner-chat")
    def script(shown, context):
        run = env.plans.claim("create", "worker")["run"]
        env.plans.settle(run["id"], "worker", run["fence"], state="completed", result={"verified": True, "source_id": env.source("verified-result")})
        return Appraisal(reason="Judged on V0", action_decisions=[env.decision(shown[plan["id"]])])
    env.evaluate(script, job_id=job)
    [held] = env.job(job)["held_decisions"]
    # The whole plan finished meanwhile; nothing is left to review.
    assert held["code"] == "plan-inactive" and held["actual"]["status"] == "completed" and env.events() == []
    assert env.step(plan)["state"] == "completed"


def test_held_decisions_survive_a_replayed_commit_receipt(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    job = env.enqueue("owner-chat")
    def script(shown, context):
        env.decide(env.current(plan), "execute")
        return Appraisal(reason="Judged on V0", action_decisions=[env.decision(shown[plan["id"]])])
    env.evaluate(script, job_id=job)
    # The process died after the commit and before the queue acknowledgement.
    with env.mind.engine.db.connect(write=True) as conn:
        data = {k: v for k, v in env.job(job).items() if k not in {"held_decisions", "result"}}
        conn.execute("UPDATE mind_appraisals SET state='running',lease=0,data=? WHERE id=?", (json.dumps(data), job))
    result, provider = env.evaluate(lambda shown, context: pytest.fail("A committed evaluation must not call the model again"), job_id=job)
    assert provider.calls == 0 and env.job(job)["held_decisions"][0]["code"] == "step-touched" and len(env.events()) == 1


TWO_STEPS = [{"id": "make", "actor": "create", "goal": "Make the clock", "completion": "A verified SVG is saved"},
             {"id": "tell", "actor": "contact", "goal": "Mention the clock", "completion": "The message is accepted"}]
DEPENDENT = [{"id": "input", "actor": "owner", "goal": "Choose a photo", "completion": "Photo supplied"},
             {"id": "make", "actor": "create", "goal": "Make the clock", "completion": "A verified SVG is saved", "depends_on": ["input"]}]


def test_stale_revision_is_rebased_when_only_a_sibling_moved(env):
    plan = env.create(steps=TWO_STEPS)
    job = env.enqueue("owner-chat")
    sibling = {}
    def script(shown, context):
        sibling["plan"] = env.decide(env.current(plan), "wait", step_id="tell", next_review_at=later(env, minutes=10))
        return Appraisal(reason="Make it now", action_decisions=[env.decision(shown[plan["id"]], "execute", next_review_at=later(env, hours=2))])
    env.evaluate(script, job_id=job)
    current = env.current(plan)
    assert sibling["plan"]["revision"] == plan["revision"] + 1 and current["revision"] == plan["revision"] + 2
    make, tell = current["steps"]
    assert make["state"] == "ready" and make["decision"]["expected_revision"] == sibling["plan"]["revision"]
    assert tell["state"] == "waiting" and tell["decision"]["id"] == sibling["plan"]["steps"][1]["decision"]["id"]
    assert "held_decisions" not in env.job(job) and env.events() == []
    # It never saw the sibling's revision, so it cannot postpone the review that revision scheduled.
    assert current["next_review_at"] == sibling["plan"]["next_review_at"]
    assert env.plans.claim("create", "worker")["state"] == "claimed"


def test_two_decisions_of_one_evaluation_share_its_base(env):
    plan = env.create(steps=TWO_STEPS)
    job = env.enqueue("owner-chat")
    env.evaluate(lambda shown, context: Appraisal(reason="Arrange both", action_decisions=[
        env.decision(shown[plan["id"]], "execute"), env.decision(shown[plan["id"]], "wait", step_id="tell")]), job_id=job)
    make, tell = env.current(plan)["steps"]
    assert (make["state"], tell["state"]) == ("ready", "waiting") and env.current(plan)["revision"] == plan["revision"] + 2
    assert "held_decisions" not in env.job(job)


def test_decision_on_a_dependency_does_not_hold_its_dependent_in_the_same_evaluation(env):
    plan = env.create(steps=DEPENDENT)
    reply = env.source("owner-sends-the-photo")
    job = Appraisals(env.mind).enqueue([reply], env.version)["id"]
    def script(shown, context):
        done = ActionDecision(plan_id=plan["id"], step_id="input", expected_revision=shown[plan["id"]]["revision"], action="owner_completed",
            reason="The owner supplied it", evidence_ids=[reply])
        return Appraisal(reason="Photo arrived", action_decisions=[done, env.decision(shown[plan["id"]], "execute")])
    env.evaluate(script, job_id=job)
    chosen, make = env.current(plan)["steps"]
    assert chosen["state"] == "completed" and make["state"] == "ready" and "held_decisions" not in env.job(job)


@pytest.mark.parametrize("case", ["dependency-completed", "time-window-changed", "new-receipt", "owner-epoch-advanced", "goal-changed"])
def test_stale_revision_with_a_changed_basis_is_held_and_reviewed_once(env, case):
    plan = env.create(steps=DEPENDENT if case == "dependency-completed" else TWO_STEPS)
    job = env.enqueue("owner-chat")
    def script(shown, context):
        current = env.current(plan)
        if case == "dependency-completed":
            env.decide(current, "owner_completed", step_id="input", evidence=[env.source("owner-sends-the-photo")])
        elif case == "time-window-changed":
            steps = [{**s, "not_before": later(env, days=1)} if s["id"] == "make" else s for s in TWO_STEPS]
            env.plans.manage({"command_id": "window", "action": "update", "id": plan["id"], "expected_revision": current["revision"],
                "reason": "Owner prefers tomorrow", "evidence_ids": [env.initial], "steps": steps})
        elif case == "new-receipt":
            with env.mind.engine.db.connect(write=True) as conn:
                env.plans.settle_linked(conn, {"plan_id": plan["id"], "plan_step_id": "make"}, {"id": "partial-result", "complete": False})
        elif case == "owner-epoch-advanced":
            env.decide(current, "wait", step_id="tell")
            env.owner_message("owner-changes-her-mind")
        else:
            env.plans.manage({"command_id": "goal", "action": "update", "id": plan["id"], "expected_revision": current["revision"],
                "reason": "Owner wants a poem", "evidence_ids": [env.initial], "goal": "Make a poem instead"})
        return Appraisal(reason="Judged on V0", values={"curiosity": 61}, action_decisions=[env.decision(shown[plan["id"]], "execute")])
    before = env.step(plan)
    env.evaluate(script, job_id=job)
    [held] = env.job(job)["held_decisions"]
    # change() with steps and a receipt on the step itself also move the step's own revision.
    assert held["code"] == ("step-touched" if case in {"time-window-changed", "new-receipt"} else "basis-changed")
    assert held["expected"]["plan_revision"] == plan["revision"] and held["actual"]["plan_revision"] > plan["revision"]
    assert held["expected"]["basis"] != held["actual"]["basis"]
    step = env.step(plan)
    assert step["state"] != "ready" and "decision" not in step and before["state"] == "pending"
    assert env.mind.read()["dimensions"]["curiosity"]["value"] == 61
    assert [e["reason"] for e in env.events()] == ["held-decision"] and env.ticks(6) == 1


def test_owner_message_during_evaluation_holds_even_at_the_current_revision(env):
    """Stricter than the revision rule: the owner epoch is the one basis that moves without a plan revision."""
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    job = env.enqueue("owner-chat")
    def script(shown, context):
        env.owner_message("owner-writes-while-the-model-thinks")
        return Appraisal(reason="Judged before her message", action_decisions=[env.decision(shown[plan["id"]], "execute")])
    env.evaluate(script, job_id=job)
    [held] = env.job(job)["held_decisions"]
    assert held["code"] == "basis-changed" and held["expected"]["plan_revision"] == held["actual"]["plan_revision"]
    assert env.step(plan)["state"] == "waiting" and env.current(plan)["last_reviewed_owner_epoch"] == 0
    assert [e["reason"] for e in env.events()] == ["held-decision"]


def test_pending_or_queued_review_absorbs_later_wakeups(env):
    plan = env.create()
    first = env.plans.tick(env.actions)
    assert len(first) == 1 and env.events()[0]["state"] == "pending"
    assert [r for r, event in env.wakeups(plan)] == [["due", plan["next_review_at"]]]
    # The revision moves and the plan falls due at another time: the review that has yet to start covers it.
    plan = env.decide(plan, "wait", next_review_at=later(env, minutes=1))
    env.clock[0] += timedelta(minutes=2)
    assert env.plans.tick(env.actions) == first and len(env.events()) == 1
    env.actions.drain(Appraisals(env.mind))
    assert env.events()[0]["state"] == "queued"
    plan = env.decide(plan, "wait", next_review_at=later(env, minutes=1), strength=9)
    env.clock[0] += timedelta(minutes=2)
    env.owner_message("another-reason-to-look")
    assert env.plans.tick(env.actions) == first and env.ticks(6) == 1
    # Each absorbed reason is accounted to that one review, which will read the plan after all of them.
    assert {event for r, event in env.wakeups(plan)} == set(first)
    assert sorted(r[0] for r, event in env.wakeups(plan)) == ["due", "due", "due", "owner_epoch"]
    # Another plan is not absorbed by this one's review.
    env.create(key="second")
    assert env.ticks(6) == 2
    # The one review runs, reads everything, and nothing is left to wake.
    assert env.host_minutes(6, lambda shown, context: Appraisal(reason="Reviewed both", action_decisions=[
        env.decision(p, next_review_at=later(env, days=30)) for p in shown.values()])) == 2
    assert len(env.events()) == 2


def test_set_aside_review_neither_refires_its_reasons_nor_blocks_a_new_one(env):
    plan, own = env.grounded()
    env.revise(own, "retract", "source-retracted")
    assert env.ticks() == 1
    env.actions.drain(Appraisals(env.mind))
    [event] = env.events()
    # A later work package quarantines a job that keeps failing; drain() never closes its event.
    with env.mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE id=?", (event["job_id"],))
    # The same reason does not ask again: repeating a review that was set aside repeats its failure.
    assert env.host_minutes(12, waits(env, plan)) == 0 and len(env.events()) == 1
    # A new reason is not held hostage by the queued event of the set-aside job.
    env.owner_message("owner-writes-later")
    assert env.host_minutes(12, waits(env, plan)) == 1
    assert [e["state"] for e in env.events()] == ["queued", "complete"]
    assert sorted(r[0] for r in env.events()[1]["wakeups"]) == ["owner_epoch", "sources"]


@pytest.mark.parametrize("path", ["recorded", "normal"])
def test_condition_that_stays_true_wakes_one_review_and_each_real_change_one_more(env, path):
    """A stale plan source the model never re-grounds. Level-triggered, this cost one model call per host minute."""
    plan, own = env.grounded()
    assert env.ticks() == 0
    env.revise(own, "retract", "source-retracted")
    # On the normal path every answer differs, so every review makes a plan revision; that must not matter.
    script = waits(env, plan, strengths=iter(range(41, 99)) if path == "normal" else None)
    assert env.host_minutes(12, script) == 1
    assert env.current(plan)["needs_review"] and env.current(plan)["revision"] == plan["revision"] + (1 if path == "normal" else 0)
    assert len(env.reviews("unchanged-wait")) == (0 if path == "normal" else 1) and len(env.events()) == 1
    [(reason, event)] = env.wakeups(plan)
    assert reason[0] == "sources" and event == env.events()[0]["id"] and env.events()[0]["wakeups"] == [reason]
    # The source really changes again: exactly one more, however long the host keeps ticking.
    env.revise(own, "archive", "source-archived")
    assert env.host_minutes(12, script) == 1 and env.host_minutes(12, script) == 0
    assert len(env.events()) == 2 and [r[0] for r, event in env.wakeups(plan)] == ["sources", "sources"]
    assert env.current(plan)["revision"] == plan["revision"] + (2 if path == "normal" else 0)


def test_newer_version_of_a_plan_source_is_a_change_although_the_old_record_is_untouched(env):
    plan, own = env.grounded()
    def version(number):
        env.mind.engine.receive(SourceInput(namespace="plan-review-test", key="grounded-own-evidence", version=number, text="edited " + number,
            scope=env.mind.scope, authority="explicit", occurred_at=env.mind.clock(), metadata={"role": "user", "host_event": "message"}))
    version("2")
    assert env.host_minutes(6, waits(env, plan)) == 1
    # The old record's revision and deletion flag never moved; only the superseding source did.
    version("3")
    assert env.host_minutes(6, waits(env, plan)) == 1 and env.host_minutes(6, waits(env, plan)) == 0
    states = [reason[1][0] for reason, event in env.wakeups(plan)]
    assert [s[1] for s in states] == [[1, 0], [1, 0]] and states[0][2] != states[1][2] and [s[3] for s in states] == [False, False]


def test_due_time_that_coincides_with_a_consumed_reason_still_fires_once(env):
    plan, own = env.grounded()
    target = later(env, hours=1)
    plan = env.decide(plan, "wait", next_review_at=target)
    same_time = lambda shown, context: Appraisal(reason="Still waiting", action_decisions=[ActionDecision(plan_id=plan["id"], step_id="make",
        expected_revision=shown[plan["id"]]["revision"], action="wait", reason="Nothing new", evidence_ids=[context["new_evidence"][0]["id"]], next_review_at=target)])
    env.revise(own, "retract", "source-retracted")
    assert env.host_minutes(12, same_time) == 1 and env.current(plan)["next_review_at"] == target
    # The due time arrives while the stale source, already reviewed, is still stale.
    env.clock[0] += timedelta(hours=1)
    assert env.host_minutes(12, waits(env, plan)) == 1
    due = env.events()[1]
    assert sorted(r[0] for r in due["wakeups"]) == ["due", "sources"] and due["wakeups"][0] == ["due", target]
    assert env.host_minutes(12, waits(env, plan)) == 0 and len(env.events()) == 2


def test_echoed_past_review_time_on_the_normal_path_neither_loops_nor_strands_the_plan(env):
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, hours=1))
    echoed = env.current(plan)["next_review_at"]
    env.clock[0] += timedelta(hours=1)
    strengths = iter(range(41, 99))
    def script(shown, context):
        return Appraisal(reason="Echo", action_decisions=[env.decision(shown[plan["id"]], strength=next(strengths), next_review_at=echoed)])
    assert env.host_minutes(12, script) == 1
    current = env.current(plan)
    # The plan is looked at again as if no time had been given; the decision keeps the model's own value.
    assert current["revision"] == plan["revision"] + 1 and current["steps"][0]["decision"]["next_review_at"] == echoed
    assert datetime.fromisoformat(current["next_review_at"]) == env.clock[0] - timedelta(minutes=11) + timedelta(minutes=20)
    # Not stranded: that time arrives and wakes exactly one review, which echoes again.
    assert env.host_minutes(12, script) == 1 and env.host_minutes(12, script) == 0
    assert [r for r, event in env.wakeups(plan)] == [["due", echoed], ["due", current["next_review_at"]]]


def test_plan_change_with_a_past_review_time_is_reviewed_now(env):
    plan = env.create()
    assert env.host_minutes(3, lambda shown, context: Appraisal(reason="Wait", action_decisions=[env.decision(shown[plan["id"]], next_review_at=later(env, days=30))])) == 1
    consumed = plan["next_review_at"]
    current = env.current(plan)
    changed = env.plans.manage({"command_id": "reschedule", "action": "reschedule", "id": plan["id"], "expected_revision": current["revision"],
        "reason": "Echoes a time that has already fired", "evidence_ids": [env.initial], "next_review_at": consumed})
    assert changed["next_review_at"] == env.mind.clock() != consumed
    assert env.host_minutes(6, lambda shown, context: Appraisal(reason="Wait", action_decisions=[env.decision(shown[plan["id"]], next_review_at=later(env, days=30))])) == 1


def soon_due(env):
    """A waiting plan that falls due in ten minutes, and an owner message that wakes its review first."""
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, minutes=10))
    env.owner_message("owner-writes")
    [first] = env.plans.tick(env.actions)
    return plan, first


NOTHING = lambda shown, context: Appraisal(reason="Read it; nothing to arrange")


def test_reason_that_appears_while_its_review_is_running_wakes_one_review_afterwards(env):
    plan, first = soon_due(env)
    seen = {}
    def script(shown, context):
        # This evaluation has read the plan. It now falls due, and the minute review runs meanwhile.
        env.clock[0] += timedelta(minutes=11)
        seen.update(tick=env.plans.tick(env.actions), events=len(env.events()), ledger=[r[0] for r, event in env.wakeups(plan)])
        return NOTHING(shown, context)
    env.evaluate(script)
    # The running review absorbed the wake-up, but is not credited with a fact it may not have read.
    assert seen == {"tick": [first], "events": 1, "ledger": ["owner_epoch"]}
    assert env.host_minutes(12, waits(env, plan)) == 1 and len(env.events()) == 2
    assert env.events()[1]["wakeups"][0] == ["due", plan["next_review_at"]] and env.host_minutes(12, waits(env, plan)) == 0


def test_reason_that_appears_before_its_review_starts_does_not_wake_a_second(env):
    plan, first = soon_due(env)
    # Not drained yet: the review will read the plan later.
    env.clock[0] += timedelta(minutes=11)
    assert env.plans.tick(env.actions) == [first]
    # Queued with its job still pending: the same holds.
    env.actions.drain(Appraisals(env.mind))
    env.upgrade("planning-v2")
    assert env.plans.tick(env.actions) == [first] and len(env.events()) == 1
    assert sorted((r[0], event) for r, event in env.wakeups(plan)) == [("agent_version", first), ("due", first), ("owner_epoch", first)]
    # It reads the plan after all three and leaves them as they are: none of them wakes a second review.
    assert env.host_minutes(12, NOTHING) == 1 and env.host_minutes(12, NOTHING) == 0 and len(env.events()) == 1
    assert env.current(plan)["next_review_at"] == plan["next_review_at"]


def test_failed_attempt_reads_the_plan_again_so_its_retry_covers_what_appeared_meanwhile(env):
    plan, first = soon_due(env)
    jobs = Appraisals(env.mind, exploration_capabilities={"version": env.version})
    env.actions.drain(jobs)
    class TimesOut:
        def appraise(self, context):
            env.clock[0] += timedelta(minutes=11)
            assert env.plans.tick(env.actions) == [first] and [r[0] for r, event in env.wakeups(plan)] == ["owner_epoch"]
            raise RuntimeError("deepseek-timeout")
    assert jobs.run_one(TimesOut())["state"] == "pending"
    # Back in the queue, the retry has yet to read the plan: it now answers for the due time too.
    assert env.plans.tick(env.actions) == [first]
    assert sorted(r[0] for r, event in env.wakeups(plan)) == ["due", "owner_epoch"]
    with env.mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0")
    assert env.host_minutes(12, NOTHING) == 1 and env.host_minutes(12, NOTHING) == 0 and len(env.events()) == 1


def test_retry_that_keeps_its_frozen_context_is_not_credited_with_a_later_owner_message(env):
    """With operational lanes a retry that keeps its first attempt's memory context commits no plan decision
    over an owner message newer than that context. Since WP3 only a preparation pass that cached new evidence
    parts keeps it (every other failure rebuilds), so that is the retry driven here."""
    env.memory.configure({"semantic": True, "operational_lanes": True})
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, days=30))
    env.owner_message("first-message")
    [first] = env.plans.tick(env.actions)
    jobs = Appraisals(env.mind, exploration_capabilities={"version": env.version})
    env.actions.drain(jobs)
    class PreparationPending:
        def appraise(self, context):
            from kin_mind.context import SCHEMA
            with env.mind.engine.db.connect(write=True) as conn:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO mind_context_cache VALUES(?,?,?,?)", ("part", env.mind.scope.key(),
                             json.dumps({"value": {"entries": [], "omitted_ids": []}}), env.mind.clock()))
            raise RuntimeError("deepseek-evidence-compression-pending:needs-compression")
    assert jobs.run_one(PreparationPending(), lane="action")["state"] == "pending"
    assert "frozen_memory_context" in env.job(env.events()[0]["job_id"])
    env.owner_message("second-message-while-the-retry-waits")
    assert env.plans.tick(env.actions) == [first] and [r for r, event in env.wakeups(plan)] == [["owner_epoch", 1]]
    with env.mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0")
    retry = jobs.run_one(Reviewer(waits(env, plan)), lane="action")
    assert retry["state"] == "complete" and retry["result"]["new_interaction_pending"] and env.current(plan)["last_reviewed_owner_epoch"] == 0
    env.actions.drain(jobs)
    # The message it could not answer for wakes exactly one review, which does.
    calls = 0
    for _ in range(12):
        env.clock[0] += timedelta(minutes=1)
        env.plans.tick(env.actions)
        jobs, provider = Appraisals(env.mind, exploration_capabilities={"version": env.version}), Reviewer(waits(env, plan))
        env.actions.drain(jobs)
        jobs.run_one(provider, lane="action")
        env.actions.drain(jobs)
        calls += provider.calls
    assert calls == 1 and env.current(plan)["last_reviewed_owner_epoch"] == 2 and len(env.events()) == 2


@pytest.mark.parametrize("drained", [False, True])
def test_review_set_aside_for_stale_evidence_hands_its_reasons_to_a_new_one(env, drained):
    """drain() drops an event whose evidence went stale. What it answered for must not be lost with it."""
    plan, own = env.grounded()
    env.owner_message("owner-writes")
    [first] = env.plans.tick(env.actions)
    assert env.events()[0]["evidence_ids"] and [event for r, event in env.wakeups(plan)] == [first]
    if drained:
        env.actions.drain(Appraisals(env.mind))
    # The plan's own source changes before that review reads anything; it can no longer load its evidence.
    env.revise(own, "retract", "source-retracted")
    [second] = env.plans.tick(env.actions)
    assert second != first and sorted((r[0], event) for r, event in env.wakeups(plan)) == [("owner_epoch", second), ("sources", second)]
    seen = {}
    def script(shown, context):
        seen.update(needs_review=shown[plan["id"]]["needs_review"], evidence=[e["id"] for e in context["new_evidence"]])
        return waits(env, plan)(shown, context)
    if drained:
        # The first job can never load its evidence again; bounding its retries is another work package's.
        with env.mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE id=?", (next(e for e in env.events() if e["id"] == first)["job_id"],))
    assert env.host_minutes(12, script) == 1 and env.host_minutes(12, script) == 0
    # The new review ran on evidence that is still current, and saw that the plan's grounding is not.
    assert seen["needs_review"] is True and own not in seen["evidence"] and len(seen["evidence"]) == 1
    assert {e["id"]: e["state"] for e in env.events()} == {first: "needs-review", second: "complete"}


def test_review_that_finished_is_not_mistaken_for_one_that_never_read_the_plan(env):
    """drain() checks evidence before it closes an event, so a finished review can end up set aside too."""
    plan, own = env.grounded()
    env.owner_message("owner-writes")
    [first] = env.plans.tick(env.actions)
    jobs = Appraisals(env.mind, exploration_capabilities={"version": env.version})
    env.actions.drain(jobs)
    assert jobs.run_one(Reviewer(NOTHING))["state"] == "complete"
    env.revise(own, "retract", "source-retracted")
    env.actions.drain(jobs)
    assert env.events()[0]["state"] == "needs-review"
    # Only the source change is new; the owner message stays answered by the review that did read it.
    [second] = env.plans.tick(env.actions)
    assert sorted((r[0], event) for r, event in env.wakeups(plan)) == [("owner_epoch", first), ("sources", second)]
    assert env.host_minutes(12, NOTHING) == 1 and env.host_minutes(12, NOTHING) == 0


def test_rebased_decision_asks_again_at_once_when_the_revision_it_missed_had_asked_at_once(env):
    plan = env.create(steps=TWO_STEPS)
    plan = env.decide(plan, "wait", step_id="tell", next_review_at=later(env, days=30))
    job = env.enqueue("owner-chat")
    moments = {}
    def script(shown, context):
        env.clock[0] += timedelta(minutes=1)
        with env.mind.engine.db.connect(write=True) as conn:
            env.plans.settle_linked(conn, {"plan_id": plan["id"], "plan_step_id": "tell"}, {"id": "partial-result", "complete": False})
        moments["settled"] = env.current(plan)["next_review_at"]
        env.clock[0] += timedelta(minutes=1)
        return Appraisal(reason="Make it", action_decisions=[env.decision(shown[plan["id"]], "execute", next_review_at=later(env, days=30))])
    env.evaluate(script, job_id=job)
    current = env.current(plan)
    assert "held_decisions" not in env.job(job) and current["steps"][0]["state"] == "ready"
    # Not the thirty days it asked for, nor the settlement's moment, which a review may already have
    # answered for: a time of its own, now, so exactly one review reads the result it never saw.
    assert current["next_review_at"] == env.mind.clock() != moments["settled"]
    assert env.host_minutes(12, lambda shown, context: Appraisal(reason="Seen the result", action_decisions=[
        env.decision(shown[plan["id"]], step_id="tell", next_review_at=later(env, days=30))])) == 1


CHANGES = ["owner", "revise", "newer", "upgrade", "result"]
MOMENTS = ["plain", "pending", "queued", "backlog", "running"]


@pytest.mark.parametrize("moment", MOMENTS)
def test_every_kind_of_change_at_every_moment_is_read_by_a_later_review_and_never_loops(env, moment):
    """Seeded replay of the host's minute review: one outside change per quiet window, five kinds at five moments.

    plain: before the tick. pending: after the tick emitted a review, before drain. queued: after drain,
    before the evaluation starts. backlog: the review job waits a minute behind other work and the change
    lands before the next tick. running: while the model thinks, with another host process ticking meanwhile.
    Read: a model call STARTED after the change, inside its window. Never loops: a window costs no more than
    the change, a held follow-up, a follow-up of a running review and one carried due time; a quiet tail costs nothing.
    Checked against deliberately broken variants: it fails without the release of set-aside reviews (backlog),
    when a running review is credited with new reasons (running), and with the plan revision and a review
    count back in the wake-up key (plain, queued, backlog, running).
    """
    import random
    rng = random.Random(MOMENTS.index(moment))
    plan, own = env.grounded()
    Appraisals(env.mind)
    calls, state, numbers = [], {"inject": None, "quiet": False, "after": None}, iter(range(2, 999))
    revisions = iter(["retract", "restore", "archive", "restore"] * 9)

    def outside(kind):
        number = next(numbers)
        if kind == "owner":
            env.owner_message("owner-" + str(number))
        elif kind == "revise":
            env.revise(own, next(revisions), "revision-" + str(number))
        elif kind == "newer":
            env.mind.engine.receive(SourceInput(namespace="plan-review-test", key="grounded-own-evidence", version=str(number), text="edited " + str(number),
                scope=env.mind.scope, authority="explicit", occurred_at=env.mind.clock(), metadata={"role": "user", "host_event": "message"}))
        elif kind == "upgrade":
            env.upgrade("planning-v" + str(number))
        else:
            with env.mind.engine.db.connect(write=True) as conn:
                env.plans.settle_linked(conn, {"plan_id": plan["id"], "plan_step_id": "make"}, {"id": "result-" + str(number), "complete": False})
        state["after"] = len(calls)

    class Model:
        def appraise(self, context):
            calls.append(None)
            shown = context["autonomy_context"]["plans"]["plans"][0]
            if state["inject"]:
                kind, state["inject"] = state["inject"], None
                outside(kind)
                env.plans.tick(env.actions)
            answer = "same" if state["quiet"] else rng.choice(["same", "same", "strength", "nothing", "echo", "omit"])
            if answer == "nothing":
                return Appraisal(reason="Nothing to arrange"), dict(RECEIPT)
            extra = {"same": {"next_review_at": later(env, days=30)}, "strength": {"next_review_at": later(env, days=30), "strength": rng.randrange(100)},
                     "echo": {"next_review_at": later(env, hours=-1)}, "omit": {}}[answer]
            return Appraisal(reason="Wait", action_decisions=[ActionDecision(plan_id=plan["id"], step_id="make", expected_revision=shown["revision"],
                action="wait", reason="Judged", evidence_ids=[context["new_evidence"][0]["id"]], **extra)]), dict(RECEIPT)

    def minute(*, after_tick=None, after_drain=None, slot_taken=False):
        env.clock[0] += timedelta(minutes=1)
        with env.mind.engine.db.connect(write=True) as conn:
            # Real time passes, so a retry's back-off has elapsed; a later work package bounds a hopeless job.
            conn.execute("UPDATE mind_appraisals SET available=0 WHERE state='pending'")
            conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE state='pending' AND attempts>=5")
        env.plans.tick(env.actions)
        after_tick and after_tick()
        jobs = Appraisals(env.mind, exploration_capabilities={"version": env.version})
        env.actions.drain(jobs)
        after_drain and after_drain()
        if not slot_taken:
            jobs.run_one(Model())
        env.actions.drain(jobs)

    problems = []
    for kind in CHANGES:
        before, state["after"] = len(calls), None
        if moment == "plain":
            outside(kind)
            minute()
        else:
            env.owner_message("wake-before-" + kind)
            if moment == "pending":
                minute(after_tick=lambda: outside(kind))
            elif moment == "queued":
                minute(after_drain=lambda: outside(kind))
            elif moment == "backlog":
                minute(slot_taken=True)
                outside(kind)
                minute()
            else:
                state["inject"] = kind
                minute()
        for _ in range(11):
            minute()
        if state["after"] is None or len(calls) <= state["after"]:
            problems.append(("never read", kind))
        if len(calls) - before > 5:
            problems.append(("loop", kind, len(calls) - before))
    state["quiet"] = True
    for _ in range(25):
        minute()
    settled = len(calls)
    for _ in range(25):
        minute()
    assert problems == [] and len(calls) == settled


def test_review_registers_the_version_even_when_its_decision_is_held(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    env.upgrade("planning-v2")
    assert env.ticks() == 1
    def script(shown, context):
        # A linked execution result lands meanwhile; unlike decide() it stamps no version.
        with env.mind.engine.db.connect(write=True) as conn:
            env.plans.settle_linked(conn, {"plan_id": plan["id"], "plan_step_id": "make"}, {"id": "partial-result", "complete": False})
        return Appraisal(reason="Judged on V0", action_decisions=[env.decision(shown[plan["id"]])])
    result, _ = env.evaluate(script)
    assert env.job(result["id"])["held_decisions"][0]["code"] == "step-touched"
    current = env.current(plan)
    assert current["agent_version"] == "planning-v2" and current["revision"] == plan["revision"] + 1
    [seen] = env.reviews("version-seen")
    assert (seen["previous_agent_version"], seen["agent_version"]) == ("planning-v1", "planning-v2")
    assert [r["revision"] for r in env.history(plan)] == [1, 2, 3]
    # The held decision asked once; the settlement made the plan due, and that is absorbed too.
    # (Both events carry the same injected time, so their order is not asserted.)
    assert sorted(e["reason"] for e in env.events()) == ["due-or-changed-evidence", "held-decision"] and env.ticks(6) == 2
    env.evaluate(lambda shown, context: Appraisal(reason="Seen the partial result", action_decisions=[env.decision(shown[plan["id"]])]))
    assert env.step(plan)["decision"]["action"] == "wait" and env.ticks(8) == 2


def test_review_without_any_decision_still_registers_the_version(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    other = env.decide(env.create(key="second"), "wait", next_review_at=later(env, days=30))
    env.upgrade("planning-v2")
    assert env.ticks() == 2
    env.evaluate(lambda shown, context: Appraisal(reason="Nothing to arrange"))
    # Every plan this review was shown is registered, without a revision.
    assert [env.current(p)["agent_version"] for p in (plan, other)] == ["planning-v2"] * 2
    assert [env.current(p)["revision"] for p in (plan, other)] == [plan["revision"], other["revision"]]
    assert len(env.reviews("version-seen")) == 2
    env.evaluate(lambda shown, context: Appraisal(reason="Nothing to arrange"))
    assert env.ticks(8) == 2 and len(env.reviews("version-seen")) == 2


def test_ordinary_evaluation_does_not_register_versions(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    env.upgrade("planning-v2")
    env.evaluate(lambda shown, context: Appraisal(reason="Just chatting"), job_id=env.enqueue("owner-chat"))
    assert env.current(plan)["agent_version"] == "planning-v1" and env.ticks() == 1


def test_plan_view_shown_to_the_model_is_persisted_with_the_attempt(env):
    plan = env.decide(env.create(steps=DEPENDENT), "wait", strength=33)
    job = env.enqueue("owner-chat")
    seen, supplied = {}, {}
    def script(shown, context):
        seen.update(shown)
        supplied.update(context["autonomy_context"]["plans"])
        return Appraisal(reason="Looked")
    env.evaluate(script, job_id=job)
    view = env.job(job)["plan_view"]
    assert view["owner_epoch"] == 0 and set(view["plans"]) == set(seen) == {plan["id"]}
    entry = view["plans"][plan["id"]]
    assert entry["revision"] == seen[plan["id"]]["revision"] == plan["revision"]
    make = entry["steps"]["make"]
    assert set(make) == {"revision", "state", "decision_id", "strength", "delivery_artifacts", "basis"}
    assert (make["revision"], make["state"], make["strength"], make["delivery_artifacts"]) == (2, "waiting", 33, [])
    assert make["decision_id"] == plan["steps"][1]["decision"]["id"] and entry["steps"]["input"]["decision_id"] is None
    # The manifest itself is host bookkeeping and is not part of the model's input.
    assert set(supplied) == {"plans", "next_cursor", "timezone"} and make["basis"] not in json.dumps(supplied)
    assert len(make["basis"]) == 64
    # The same read without the option is unchanged for every other caller.
    assert set(env.plans.read()) == {"plans", "next_cursor", "timezone"}


def test_plan_view_is_persisted_with_a_failed_attempt_too(env):
    plan = env.create()
    job = env.enqueue("owner-chat")
    class Failing:
        def appraise(self, context):
            raise RuntimeError("deepseek-timeout")
    assert Appraisals(env.mind).run_one(Failing(), job_id=job)["state"] == "pending"
    data = env.job(job)
    assert data["error"] == "deepseek-timeout" and data["plan_view"]["plans"][plan["id"]]["revision"] == plan["revision"]
    assert "held_decisions" not in data


@pytest.mark.parametrize("stale", [False, True])
def test_without_a_recorded_view_only_an_exact_revision_is_accepted(env, stale):
    """Plans are on but the model is shown none: there is no manifest to prove an unrelated revision."""
    env.memory.configure({"semantic_actions": False})
    plan = env.create(steps=TWO_STEPS)
    supplied = env.source("owner-chat")
    job = Appraisals(env.mind).enqueue([supplied], env.version)["id"]
    class Blind:
        def appraise(self, context):
            assert "autonomy_context" not in context
            if stale:
                env.decide(env.current(plan), "wait", step_id="tell")
            # Without a plan view only this evaluation's own sources may be cited.
            decision = ActionDecision(plan_id=plan["id"], step_id="make", expected_revision=plan["revision"], action="execute",
                reason="Decided without a view", evidence_ids=[supplied])
            return Appraisal(reason="Decided without a view", action_decisions=[decision]), dict(RECEIPT)
    assert Appraisals(env.mind).run_one(Blind(), job_id=job)["state"] == "complete"
    data = env.job(job)
    assert "plan_view" not in data
    if stale:
        [held] = data["held_decisions"]
        assert held["code"] == "view-missing" and held["expected"]["step"] is None and env.step(plan)["state"] == "pending"
    else:
        assert "held_decisions" not in data and env.step(plan)["state"] == "ready"


def test_decisions_after_the_evaluations_own_plan_change_are_not_held(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    job = env.enqueue("owner-chat")
    def script(shown, context):
        step = PlanStep(id="make", actor="create", goal="Make the clock at dusk", completion="A verified SVG is saved")
        update = PlanChange(action="update", id=plan["id"], expected_revision=shown[plan["id"]]["revision"], reason="Better light", evidence_ids=[env.initial], steps=[step])
        fresh = PlanChange(action="create", key="fresh", goal="Write a poem", motivation="A new whim", reason="Own idea", evidence_ids=[env.initial],
            steps=[PlanStep(id="write", actor="create", goal="Write it", completion="A saved text")])
        return Appraisal(reason="Rearranged", plan_changes=[update, fresh], action_decisions=[
            env.decision({"id": plan["id"], "revision": shown[plan["id"]]["revision"] + 1}, "execute"),
            env.decision({"id": "fresh", "revision": 1}, "wait", step_id="write")])
    env.evaluate(script, job_id=job)
    assert "held_decisions" not in env.job(job) and env.step(plan)["state"] == "ready"
    fresh = next(p for p in env.plans.read()["plans"] if p["key"] == "fresh")
    assert fresh["steps"][0]["state"] == "waiting" and fresh["revision"] == 2


def test_idempotent_create_cannot_vouch_for_a_view_the_model_never_saw(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    job = env.enqueue("owner-chat")
    def script(shown, context):
        env.decide(env.current(plan), "execute")
        again = PlanChange(action="create", key="idea", goal="Make a small illustrated clock", motivation="Continue a shared interest",
            reason="Repeat", evidence_ids=[env.initial], steps=[PlanStep(id="make", actor="create", goal="Make the clock", completion="A verified SVG is saved")])
        return Appraisal(reason="Judged on V0", plan_changes=[again], action_decisions=[env.decision(shown[plan["id"]])])
    env.evaluate(script, job_id=job)
    assert env.job(job)["held_decisions"][0]["code"] == "step-touched" and env.step(plan)["state"] == "ready"


def test_decision_on_a_plan_that_stopped_being_active_is_held_without_a_review(env):
    plan = env.decide(env.create(), "wait", next_review_at=later(env, days=30))
    job = env.enqueue("owner-chat")
    def script(shown, context):
        env.plans.manage({"command_id": "pause", "action": "pause", "id": plan["id"], "expected_revision": plan["revision"],
            "reason": "Owner needs the computer", "evidence_ids": [env.initial]})
        return Appraisal(reason="Judged on V0", values={"curiosity": 58}, action_decisions=[env.decision(shown[plan["id"]], "execute")])
    env.evaluate(script, job_id=job)
    [held] = env.job(job)["held_decisions"]
    assert held["code"] == "plan-inactive" and held["actual"]["status"] == "paused" and env.events() == []
    assert env.mind.read()["dimensions"]["curiosity"]["value"] == 58


def test_basis_covers_every_listed_input_and_never_normalizes_text(env):
    plan = env.decide(env.create(steps=DEPENDENT), "owner_completed", step_id="input", evidence=[env.source("owner-sends-the-photo")])
    make = plan["steps"][1]
    original = env.plans.basis(plan, make, 3)
    assert original == env.plans.basis(json.loads(json.dumps(plan)), json.loads(json.dumps(make)), 3)
    def varied(path, value, epoch=3):
        changed = json.loads(json.dumps(plan))
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return env.plans.basis(changed, changed["steps"][1], epoch)
    variants = {"actor": (["steps", 1, "actor"], "explore"), "preconditions": (["steps", 1, "preconditions"], ["dry weather"]),
        "depends_on": (["steps", 1, "depends_on"], []), "not_before": (["steps", 1, "not_before"], "2027-01-01T08:00:00+08:00"),
        "not_after": (["steps", 1, "not_after"], "2027-01-02T08:00:00+08:00"), "completion": (["steps", 1, "completion"], "Another criterion"),
        "owner_request_id": (["steps", 1, "owner_request_id"], "concern_1"), "dependency revision": (["steps", 0, "revision"], 9),
        "dependency state": (["steps", 0, "state"], "waiting"), "dependency receipts": (["steps", 0, "receipts"], []),
        "own receipts": (["steps", 1, "receipts"], [{"id": "partial-result"}]), "goal": (["goal"], "Another goal"),
        "motivation": (["motivation"], "Another motive"), "evidence revision": (["evidence", 0, "revision"], 99),
        "evidence record": (["evidence", 0, "record_id"], "mem_other")}
    results = {name: varied(*change) for name, change in variants.items()}
    results["owner_epoch"] = env.plans.basis(plan, make, 4)
    assert all(value != original for value in results.values()), [k for k, v in results.items() if v == original]
    assert len(set(results.values())) == len(results)
    # Compatibility-equivalent and whitespace-variant strings stay different facts.
    assert len({varied(["goal"], text) for text in ["カフェ", "ｶﾌｪ", "カフェ ", "カ フェ", "Café", "Café"]}) == 6
    # Fields outside the listed basis do not disturb it.
    assert varied(["steps", 1, "goal"], "Reworded step goal") == original and varied(["reason"], "Another reason") == original
    assert varied(["steps", 1, "strength"], 77) == original and varied(["next_review_at"], "2030-01-01T00:00:00+08:00") == original
