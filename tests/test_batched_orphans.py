"""Batched children must never outlive the parent that absorbed them (A7)."""

import json
import time
from datetime import timedelta

import pytest

from eventmem.core.db import dumps
from eventmem.core.models import SourceInput
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.memory import MemoryAssessment
from kin_mind.recovery import migrate_operational, recover_batched

pytest_plugins = ("test_memory_continuity",)


class Provider:
    """Scripted appraiser: no network, no model, one receipt per call."""

    def __init__(self, fail_times=0):
        self.calls, self.fail_times = [], fail_times

    def appraise(self, context):
        self.calls.append(context)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("synthetic-batch-failure")
        return Appraisal(reason="A synthetic batch judgment", values={}, next_review_minutes=25,
                         memory=MemoryAssessment()), {"provider": "deepseek", "model": "deepseek-flash",
                                                      "reasoning": "high", "request_id": "fixture",
                                                      "usage": {"input_tokens": 11}}


def delivery(jobs, source, key):
    """A delivery judgment: the production stimulus that batches into a batch."""
    return jobs.enqueue([source(key)], "fixture-v1", origin="reflection", stimulus="delivery")["id"]


def schedule(mind, order):
    """Queue order without waiting: `available` is wall clock, not the test clock."""
    with mind.engine.db.connect(write=True) as conn:
        for job_id, value in order.items():
            conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (value, job_id))


def rows(mind):
    with mind.engine.db.connect() as conn:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM mind_appraisals").fetchall()}


def nested(mind, jobs, source, provider):
    """The real nesting: P absorbs C and fails, G later absorbs P and commits."""
    child = delivery(jobs, source, "child")
    parent = delivery(jobs, source, "parent")
    schedule(mind, {parent: 0, child: 1})
    assert jobs.run_one(provider)["state"] == "pending"
    batched = rows(mind)[child]["data"]
    grand = delivery(jobs, source, "grand")
    schedule(mind, {grand: 0, parent: 1})
    assert jobs.run_one(provider)["state"] == "complete"
    # Restore what the old one-level settlement left behind: G knows only P.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET data=json_set(data,'$.batch_ids',json(?)) WHERE id=?", (dumps([parent]), grand))
        conn.execute("UPDATE mind_appraisals SET state='batched',data=? WHERE id=?", (batched, child))
    return child, parent, grand


def test_nested_batch_is_flattened_and_settled_end_to_end(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    first = delivery(jobs, source, "first")
    second = delivery(jobs, source, "second")
    parent = delivery(jobs, source, "parent")
    schedule(mind, {parent: 0, first: 1, second: 2})
    assert jobs.run_one(provider)["state"] == "pending"
    state = rows(mind)
    assert [state[i]["state"] for i in (first, second)] == ["batched", "batched"]
    assert json.loads(state[parent]["data"])["batch_ids"] == [first, second]

    grand = delivery(jobs, source, "grand")
    schedule(mind, {grand: 0, parent: 1})
    assert jobs.run_one(provider)["state"] == "complete"
    state = rows(mind)
    assert json.loads(state[grand]["data"])["batch_ids"] == [parent, first, second]
    assert [state[i]["state"] for i in (parent, first, second)] == ["complete"] * 3
    event_id = json.loads(state[grand]["data"])["result"]["event_id"]
    for identifier in (parent, first, second):
        settled = json.loads(state[identifier]["data"])
        assert settled["result"] == {"batch_id": grand, "event_id": event_id}
        assert settled["receipt"]["request_id"] == "fixture"
    # The absorbed subtree keeps its own evidence; G evaluated all four sources.
    assert json.loads(state[first]["data"])["evidence_ids"] != json.loads(state[grand]["data"])["evidence_ids"]
    assert len(json.loads(state[grand]["data"])["evidence_ids"]) == 4


def test_transitive_settlement_reaches_a_legacy_nested_child(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child = delivery(jobs, source, "child")
    parent = delivery(jobs, source, "parent")
    schedule(mind, {parent: 0, child: 1})
    assert jobs.run_one(provider)["state"] == "pending"
    grand = delivery(jobs, source, "grand")
    schedule(mind, {grand: 0, parent: 1})
    assert jobs.run_one(provider)["state"] == "complete"
    # A row written by the previous release carries only its direct child, and
    # its own child is nested one level below an already complete parent.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET data=json_set(data,'$.batch_ids',json(?)) WHERE id=?", (dumps([parent]), grand))
        conn.execute("UPDATE mind_appraisals SET state='batched' WHERE id=?", (child,))
        conn.execute("UPDATE mind_appraisals SET state='running',lease=0 WHERE id=?", (grand,))
    schedule(mind, {grand: 0})
    assert jobs.run_one(provider)["state"] == "complete"
    assert rows(mind)[child]["state"] == "complete"


def test_committed_parent_does_not_absorb_unevaluated_evidence(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider()
    parent = delivery(jobs, source, "parent")
    assert jobs.run_one(provider)["state"] == "complete"
    later = delivery(jobs, source, "later")
    # Replay the committed parent as if its queue acknowledgement had been lost.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='pending' WHERE id=?", (parent,))
    schedule(mind, {parent: 0, later: 1})
    assert jobs.run_one(provider)["state"] == "complete"
    assert len(provider.calls) == 1
    state = rows(mind)
    assert not json.loads(state[parent]["data"])["batch_ids"]
    assert state[later]["state"] == "pending"


def test_already_integrated_batch_completes_children_with_their_own_data(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider()
    child = delivery(jobs, source, "child")
    parent = delivery(jobs, source, "parent")
    schedule(mind, {parent: 0, child: 1})
    assert jobs.run_one(provider)["state"] == "complete"
    # Requeue both without their durable receipt: the evidence stays integrated.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM commands WHERE id=?", (mind._key(parent),))
        conn.execute("UPDATE mind_appraisals SET state='pending',data=json_remove(data,'$.batch_ids') WHERE id IN (?,?)", (parent, child))
    schedule(mind, {parent: 0, child: 1})
    assert jobs.run_one(provider)["state"] == "complete"
    assert len(provider.calls) == 1
    state = rows(mind)
    assert json.loads(state[parent]["data"])["result"] == {"already_integrated": True}
    assert state[child]["state"] == "complete"
    assert json.loads(state[child]["data"])["result"]["batch_id"] == parent
    assert len(json.loads(state[child]["data"])["evidence_ids"]) == 1


def test_quarantined_parent_returns_its_children_to_the_queue(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs, provider = Appraisals(mind), Provider(fail_times=2)
    child = delivery(jobs, source, "child")
    parent = delivery(jobs, source, "parent")
    schedule(mind, {parent: 0, child: 1})
    assert jobs.run_one(provider)["state"] == "pending"
    assert rows(mind)[child]["state"] == "batched"
    # Only the historical lane quarantines here (the cap is generalized elsewhere), so
    # the carried batch is driven through that existing quarantine branch.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0,data=json_set(data,'$.stimulus','memory-enrichment') WHERE id=?", (parent,))
    assert jobs.run_one(provider)["state"] == "needs-repair"
    state = rows(mind)
    assert state[child]["state"] == "pending" and state[child]["available"] <= time.time()
    assert jobs.run_one(provider)["id"] == child
    assert jobs.status(child)["state"] == "complete"


def test_superseded_parent_releases_its_children(system):
    mind, memory, source, clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child = delivery(jobs, source, "child")
    parent = delivery(jobs, source, "parent")
    schedule(mind, {parent: 0, child: 1})
    assert jobs.run_one(provider)["state"] == "pending"
    clock[0] += timedelta(hours=1)
    actions = ActionEvents(mind)
    memory.queue_idle(actions)
    with mind.engine.db.connect(write=True) as conn:
        event = conn.execute("SELECT id,data FROM mind_action_events WHERE kind='idle-review'").fetchone()
        conn.execute("UPDATE mind_action_events SET data=? WHERE id=?",
                     (dumps({**json.loads(event["data"]), "job_id": parent}), event["id"]))
    assert migrate_operational(mind, workers_stopped=True)["superseded_idle_events"]
    state = rows(mind)
    assert state[parent]["state"] == "superseded"
    assert state[child]["state"] == "pending" and state[child]["available"] <= time.time()


def test_lost_attempt_token_is_recorded_and_leaves_children_alone(system):
    mind, _memory, source, _clock = system
    jobs = Appraisals(mind)

    class Thief(Provider):
        def appraise(self, context):
            with mind.engine.db.connect(write=True) as conn:
                conn.execute("UPDATE mind_appraisals SET data=json_set(data,'$.attempt_token','another-worker') WHERE state='running'")
            return super().appraise(context)

    child = delivery(jobs, source, "child")
    parent = delivery(jobs, source, "parent")
    schedule(mind, {parent: 0, child: 1})
    jobs.run_one(Thief())
    state = rows(mind)
    assert state[parent]["state"] == "running" and state[child]["state"] == "batched"
    with mind.engine.db.connect() as conn:
        metrics = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM metrics WHERE name='appraisal_attempt_discarded'").fetchall()]
    assert len(metrics) == 1
    assert metrics[0]["appraisal"] == parent
    assert metrics[0]["reason"] == "attempt-token-no-longer-owns-the-row"
    assert metrics[0]["usage"] == {"input_tokens": 11}
    assert "synthetic" not in dumps(metrics)

    # A failure before any receipt still has to report the discarded attempt.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='pending',data=json_remove(data,'$.receipt') WHERE id=?", (parent,))
    schedule(mind, {parent: 0})
    jobs.run_one(Thief(fail_times=1))
    with mind.engine.db.connect() as conn:
        metrics = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM metrics WHERE name='appraisal_attempt_discarded'").fetchall()]
    assert len(metrics) == 2
    assert metrics[1]["usage_status"] == "unknown" and "usage" not in metrics[1]
    assert rows(mind)[child]["state"] == "batched"


def test_recovery_completes_a_provably_committed_orphan_once(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child, parent, grand = nested(mind, jobs, source, provider)
    # Nothing holds a lease here, so the boolean settles nothing either way.
    result = recover_batched(mind, command_id="wp4-orphans", workers_stopped=False)
    assert result["completed"] == [child] and result["requeued"] == []
    assert result["rows"] == [{"id": child, "state": "complete", "reason": "settled-by-committed-ancestor",
                               "ancestor": grand, "event_id": jobs.status(grand)["result"]["event_id"]}]
    assert jobs.status(child)["state"] == "complete"
    assert jobs.status(child)["result"]["batch_id"] == grand
    assert recover_batched(mind, command_id="wp4-orphans", workers_stopped=True) == result
    assert jobs.status(child)["state"] == "complete"
    assert jobs.status(parent)["state"] == "complete"


def test_host_action_runs_the_batched_recovery_once(system):
    from kin_mind.host import dispatch
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child, _parent, _grand = nested(mind, jobs, source, provider)
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
              "agent_version": "fixture-v1", "session_id": "fixture"}
    result = dispatch(config, "recover-batched", {"command_id": "wp4-host", "workers_stopped": False})
    assert result["completed"] == [child]
    assert dispatch(config, "recover-batched", {"command_id": "wp4-host", "workers_stopped": True}) == result


def test_recovery_requeues_a_child_whose_source_has_a_newer_version(system):
    mind, _memory, source, clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child, _parent, _grand = nested(mind, jobs, source, provider)
    clock[0] += timedelta(minutes=5)
    mind.engine.receive(SourceInput(namespace="synthetic", key="child", version="2", scope=mind.scope,
                                    text="A corrected observation", occurred_at=clock[0].isoformat(),
                                    metadata={"role": "user", "host_event": "message"}))
    result = recover_batched(mind, command_id="wp4-newer", workers_stopped=True)
    assert result["requeued"] == [child] and result["completed"] == []
    assert result["rows"][0]["reason"] == "source-no-longer-current"
    assert result["rows"][0]["event_id"] is None
    state = rows(mind)[child]
    assert state["state"] == "pending" and state["available"] <= time.time()


def test_recovery_requeues_without_a_real_commit_receipt(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child, _parent, grand = nested(mind, jobs, source, provider)
    # The source index still points at the commit; the receipt no longer exists.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM commands WHERE id=?", (mind._key(grand),))
        assert conn.execute("SELECT 1 FROM mind_semantic_sources WHERE source_id=?",
                            (json.loads(rows(mind)[child]["data"])["evidence_ids"][0],)).fetchone()
    result = recover_batched(mind, command_id="wp4-no-receipt", workers_stopped=True)
    assert result["requeued"] == [child]
    assert result["rows"][0] == {"id": child, "state": "pending", "reason": "no-committed-ancestor",
                                 "ancestor": None, "event_id": None}
    assert rows(mind)[child]["state"] == "pending"


def test_recovery_requeues_a_source_with_no_committed_event(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child, _parent, _grand = nested(mind, jobs, source, provider)
    # Listed in the ancestor manifest, but its own commit was never recorded.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_semantic_sources WHERE source_id=?",
                     (json.loads(rows(mind)[child]["data"])["evidence_ids"][0],))
    result = recover_batched(mind, command_id="wp4-no-event", workers_stopped=True)
    assert result["rows"][0]["reason"] == "commit-receipt-missing"
    assert rows(mind)[child]["state"] == "pending"


def test_recovery_requeues_a_child_outside_the_ancestor_manifest(system):
    mind, _memory, source, _clock = system
    jobs, provider = Appraisals(mind), Provider(fail_times=1)
    child, parent, _grand = nested(mind, jobs, source, provider)
    stranger = delivery(jobs, source, "stranger")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='batched' WHERE id=?", (stranger,))
        conn.execute("UPDATE mind_appraisals SET data=json_set(data,'$.batch_ids',json(?)) WHERE id=?",
                     (dumps([child, stranger]), parent))
    result = recover_batched(mind, command_id="wp4-manifest", workers_stopped=True)
    assert result["completed"] == [child]
    assert [r["reason"] for r in result["rows"] if r["id"] == stranger] == ["outside-ancestor-manifest"]
    assert rows(mind)[stranger]["state"] == "pending"
