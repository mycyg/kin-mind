import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

from eventmem.core.db import Conflict
from eventmem.core.models import RevisionInput
from kin_mind.memory import MemoryContinuity
from kin_mind.model_runtime import model_slot
from kin_mind.procedures import Procedures, prepare_replay
from kin_mind.reinforcement import record, strengths
from test_autonomous_plans import env


def test_effective_use_is_deduplicated_across_channels_and_origin(env):
    mind, _, _, clock, initial = env
    with mind.engine.db.connect(write=True) as conn:
        rid = mind._evidence(conn, [initial])[0]["record_id"]
        assert record(conn, mind.scope.key(), rid, "input-1", "user_query", mind.clock())
        assert not record(conn, mind.scope.key(), rid, "input-1", "reply_reference", mind.clock())
        for origin in ("maintenance", "automatic_injection", "candidate", "summary", "planning"):
            assert not record(conn, mind.scope.key(), rid, "background-" + origin, origin, mind.clock())
        first = strengths(conn, mind.scope.key(), [rid], mind.clock())[rid]
        assert first["uses"] == 1 and first["strength"] == math.log(2)
    clock[0] += timedelta(days=30)
    with mind.engine.db.connect(write=True) as conn:
        decayed = strengths(conn, mind.scope.key(), [rid], mind.clock())[rid]
        assert decayed["decayed_uses"] == pytest.approx(.5)
        record(conn, mind.scope.key(), rid, "input-2", "user_query", mind.clock())
        assert strengths(conn, mind.scope.key(), [rid], mind.clock())[rid]["strength"] > first["strength"]


def test_one_plan_cannot_heat_every_step_and_old_trial_cannot_enable_new_weights(env):
    mind, _, _, clock, initial = env
    with mind.engine.db.connect(write=True) as conn:
        rid = mind._evidence(conn, [initial])[0]["record_id"]
        for step in range(6):
            record(conn, mind.scope.key(), rid, f"step-{step}", "verified_task", mind.clock(), {"verified": True, "result_id": str(step), "plan_id": "one-plan"})
        assert strengths(conn, mind.scope.key(), [rid], mind.clock())[rid]["uses"] == 1
        with pytest.raises(Conflict):
            record(conn, mind.scope.key(), rid, "fictional-result", "verified_task", mind.clock())
    with pytest.raises(Conflict):
        MemoryContinuity(mind).configure({"reinforcement_ranking": True, "temperature_shadow_started_at": "2020-01-01T00:00:00+00:00"})


def propose(env):
    mind, plans, source, clock, initial = env
    memory = MemoryContinuity(mind)
    memory.configure({"procedure_learning": True})
    outcomes = [memory.ingest({"id": "task-outcome-" + str(i), "kind": "task-result", "task_id": "independent-task-" + str(i),
        "at": mind.clock(), "text": "Host verified deterministic computation", "verified": True}) for i in (1, 2)]
    ids = [o["id"] for o in outcomes]
    mind.engine.settings("execution_environment", {"python": "fixture"})
    methods = Procedures(mind)
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [initial])
        p = methods.propose(conn, {"key": "repeatable-computation", "title": "Compute a total", "applicable_when": "Inputs are finite numeric rows",
            "steps": ["Validate inputs", "Compute and compare an independent total"], "tools": ["python"], "environment": {"python": "fixture"},
            "success_criteria": "Computed total matches the independent result", "evidence_ids": [initial], "result_ids": ids,
            "reason": "Actual outcomes support a candidate"}, "method-1", {"provider": "deepseek", "reasoning": "high"}, refs)
    return methods, p, ids


def test_methods_need_two_independent_cases_and_failure_revokes_current(env):
    mind, _, source, _, _ = env
    methods, p, ids = propose(env)
    assert methods.read(identifier=p["id"])["procedures"][0]["executable"] is False
    def trial(key, result, passed=True):
        return methods.record_trial(identifier=p["id"], revision=p["revision"], trial_id=key, result_id=result,
            passed=passed, isolated=True, environment=p["environment"], verification="Host replay of a real result")
    trial("one", ids[0])
    trial("same-case-again", ids[0])
    assert not methods.read(identifier=p["id"])["procedures"][0]["executable"]
    trial("independent", ids[1])
    assert methods.read(identifier=p["id"])["procedures"][0]["executable"]
    assert not methods.read(identifier=p["id"], environment={"python": "changed"})["procedures"][0]["executable"]
    trial("counterexample", ids[1], False)
    assert not methods.read(identifier=p["id"])["procedures"][0]["executable"]


def test_queued_method_replay_and_source_correction_are_fenced(env):
    mind, _, _, _, initial = env
    methods, p, ids = propose(env)
    class Reviewer:
        def structured(self, name, schema, system, context, **kwargs):
            assert name == "replay_procedure" and len(context["cases"]) == 2
            return schema.model_validate({"cases": [{"result_id": c["result_id"], "passed": True, "reason": "Actual receipt meets criteria"} for c in context["cases"]]}), {"provider": "deepseek", "reasoning": "high"}
    apply = prepare_replay(mind.engine, {"scope": mind.scope.model_dump(), "id": p["id"], "revision": p["revision"]}, Reviewer())
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
        rid = mind._evidence(conn, [initial])[0]["record_id"]
    assert methods.read(identifier=p["id"])["procedures"][0]["executable"]
    mind.engine.revise(rid, RevisionInput(command_id="correct-source", expected_revision=1, action="correct", content="The original method had a failed assumption", reason="Sourced correction"))
    assert not methods.read(identifier=p["id"])["procedures"][0]["executable"]
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict):
        apply(conn)


def test_background_model_slots_are_shared_and_foreground_bypasses(env):
    mind, _, _, _, _ = env
    provider = SimpleNamespace(engine=mind.engine, background=True)
    barrier, release = threading.Barrier(3), threading.Event()
    def hold():
        with model_slot(provider, "background-test"):
            barrier.wait(timeout=5)
            release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(hold) for _ in range(2)]
        barrier.wait(timeout=5)
        try:
            with pytest.raises(RuntimeError, match="capacity"):
                with model_slot(provider, "third-background"):
                    pass
            with model_slot(SimpleNamespace(engine=mind.engine, background=False), "foreground"):
                pass
        finally:
            release.set()
        for f in futures:
            f.result()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_model_leases").fetchone()[0] == 0
