import json
import math

import threading

from concurrent.futures import ThreadPoolExecutor

from datetime import timedelta

from types import SimpleNamespace

import pytest

from eventmem.core.db import Conflict

from eventmem.core.jobs import Worker

from eventmem.core.models import RevisionInput, Scope, SourceInput

from eventmem.core.providers import Providers

from kin_mind.graph import GraphAssessment, GraphEdge

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

def test_conflict_job_rechecks_model_seen_target_at_commit(tmp_path, monkeypatch):
    from eventmem.core import Engine
    engine = Engine(tmp_path / "conflict")
    scope = Scope(project="synthetic-conflict")
    def source(key, text):
        sid = engine.receive(SourceInput(namespace="conflict-test", key=key, text=text, scope=scope))["id"]
        return engine.source(sid)["record_ids"][0]
    target = source("target", "Launch status pending")
    candidate = source("candidate", "Launch status complete")
    def model(_provider, role, _instruction, payload):
        assert role == "conflict" and target in {item["id"] for item in payload["existing"]}
        return {"relations": [{"id": target, "relation": "refutes", "reason": "Model saw pending"}]}
    monkeypatch.setattr(Providers, "json", model)
    job = {"kind": "conflict", "payload": json.dumps({"record_id": candidate})}
    stale_apply = Worker(engine).prepare(job)
    engine.revise(target, RevisionInput(expected_revision=1, command_id="correct-target", action="correct", content="Launch status complete"))
    with engine.db.connect(write=True) as conn:
        with pytest.raises(Conflict, match="evidence changed"):
            stale_apply(conn)
        assert not conn.execute("SELECT 1 FROM relations WHERE subject=? AND object=?", (candidate, target)).fetchone()
    current_apply = Worker(engine).prepare(job)
    with engine.db.connect(write=True) as conn:
        current_apply(conn)
        assert conn.execute("SELECT predicate FROM relations WHERE subject=? AND object=?", (candidate, target)).fetchone()[0] == "refutes"

def test_inferred_graph_refutation_reviews_method_once(env):
    mind, _, source, _, _ = env
    methods, method, outcomes = propose(env)
    graph = MemoryContinuity(mind).graph
    class Reviewer:
        def structured(self, name, schema, system, context, **kwargs):
            return schema.model_validate({"cases": [{"result_id": case["result_id"], "passed": True,
                "reason": "Earlier outcome passed"} for case in context["cases"]]}), {"provider": "synthetic"}
    for index, result_id in enumerate(outcomes):
        methods.record_trial(identifier=method["id"], revision=method["revision"], trial_id=f"initial-{index}",
            result_id=result_id, passed=True, isolated=True, environment=method["environment"], verification="Host verified independent case")
    assert methods.read(identifier=method["id"])["procedures"][0]["executable"]
    queued_apply = prepare_replay(mind.engine, {"scope": mind.scope.model_dump(), "id": method["id"],
        "revision": method["revision"]}, Reviewer())
    contrary_source = source("new-method-counterexample")
    with mind.engine.db.connect(write=True) as conn:
        rid = conn.execute("SELECT id FROM records WHERE json_extract(data,'$.attributes.procedure_id')=?", (method["id"],)).fetchone()[0]
        refuter = mind._evidence(conn, [contrary_source])[0]["record_id"]
        refs = mind._evidence(conn, [contrary_source])
        proposal = GraphAssessment(edges=[GraphEdge(subject=refuter, object=rid, relation="refutes",
            evidence_ids=[contrary_source], reason="Model proposes a possible counterexample")])
        thought = GraphAssessment(edges=[proposal.edges[0].model_copy(update={"basis": "internal_thought"})])
        graph.apply(conn, thought, refs, "unverified-thought", {})
        assert methods.get(conn, method["id"])["status"] == "active"
        graph.apply(conn, proposal, refs, "assessment-1", {})
        first = mind.engine._get(conn, rid)
        assert first["status"] == "unverified"
        with pytest.raises(Conflict, match="needs review"):
            queued_apply(conn)
    pending = methods.read(identifier=method["id"])["procedures"][0]
    assert pending["status"] == "needs_review" and not pending["executable"]
    reviewed = MemoryContinuity(mind).ingest({"id": "reviewed-case", "kind": "task-result", "task_id": "reviewed-case",
        "at": mind.clock(), "text": "Host replayed the counterexample conditions", "verified": True})
    methods.record_trial(identifier=method["id"], revision=method["revision"], trial_id="reviewed-counterexample",
        result_id=reviewed["id"], passed=True, isolated=True, environment=method["environment"],
        verification="Host checked the new conditions")
    with mind.engine.db.connect(write=True) as conn:
        with pytest.raises(Conflict, match="Procedure changed during replay"):
            queued_apply(conn)
        before = mind.engine._get(conn, rid)["revision"]
        repeated = GraphAssessment(edges=[proposal.edges[0].model_copy(update={"reason": "Same source, paraphrased model claim"})])
        graph.apply(conn, repeated, refs, "assessment-2", {})
        assert mind.engine._get(conn, rid)["revision"] == before
    assert methods.read(identifier=method["id"])["procedures"][0]["executable"]

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
