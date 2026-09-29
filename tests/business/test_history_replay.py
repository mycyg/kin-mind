"""History replays and quarantined work have a way out: a replay moves past a job that ended
in any way (K3-01, K4-02), the two replays never evaluate the same event twice (K4-03), a
quarantined appraisal can be retired without a model call (K4-06, DB1-05), admission waits
are one list (E2-03), and a failed digest is tried again after a cooldown, a bounded number
of times, with its in-flight job recognised (K4-04, K4-05)."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import dumps
from eventmem.core.models import Scope, SourceInput

from kin_mind import lifecycle, recovery
from kin_mind.graph_migration import GraphMigration
from kin_mind.memory import MemoryContinuity
from kin_mind.operational_status import operational_status
from kin_mind.state import Mind

SCOPE = Scope(persona="synthetic-replay")


class Jobs:
    """The appraisal queue as the replays see it: a status per id, and what was enqueued."""

    def __init__(self):
        self.states, self.enqueued = {}, []

    def status(self, job_id=None):
        return {"id": job_id, "state": self.states[job_id]} if job_id in self.states else []

    def enqueue(self, sources, agent_version, **kwargs):
        job_id = f"job-{len(self.enqueued)}"
        self.enqueued.append(list(sources))
        self.states[job_id] = "pending"
        return {"id": job_id}


@pytest.fixture
def world(tmp_path):
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, SCOPE)
    memory = MemoryContinuity(mind)
    first = engine.receive(SourceInput(namespace="kin-owner-input", key="init", text="setup", scope=SCOPE,
                                       authority="explicit", extract=False,
                                       metadata={"role": "user", "host_event": "message"}))["id"]
    mind.initialize(agent_version="fixture-v1", evidence_ids=[first])
    return engine, mind, memory


def runtime_event(engine, n, *, historical):
    sid = engine.receive(SourceInput(namespace="kin-owner-input", key=f"event-{n}", text=f"event {n}", scope=SCOPE,
                                     authority="explicit", extract=False,
                                     metadata={"role": "user", "host_event": "message"}))["id"]
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_runtime_events(id,scope,kind,occurred_at,digest,data) VALUES(?,?,?,?,?,?)",
                     (f"rt-{n}", SCOPE.key(), "message", "2026-09-20T00:00:00+00:00", f"d{n}",
                      dumps({"source_id": sid, **({"historical": 1} if historical else {})})))
    return sid


def migration(engine, name):
    with engine.db.connect() as conn:
        row = conn.execute("SELECT cursor,data FROM mind_memory_migrations WHERE scope=? AND name=?",
                           (SCOPE.key(), name)).fetchone()
    return (row[0], json.loads(row[1])) if row else (None, None)


@pytest.mark.parametrize("ending", ["superseded", "needs-repair", "missing"])
def test_the_semantic_replay_moves_past_a_job_that_ended_any_way(world, ending):
    engine, mind, memory = world
    for n in range(20):
        runtime_event(engine, n, historical=True)
    jobs = Jobs()
    first = memory.queue_history(jobs, "fixture-v1")
    assert first["state"] == "pending" and len(jobs.enqueued) == 1
    if ending == "missing":
        del jobs.states[first["job_id"]]
    else:
        jobs.states[first["job_id"]] = ending
    second = memory.queue_history(jobs, "fixture-v1")
    assert len(jobs.enqueued) == 2 and second["job_id"] != first["job_id"]
    assert second["deferred_repairs"] == [first["job_id"]] and second["ended"] == {ending: 1}


def test_the_graph_replay_leaves_historical_events_to_the_semantic_one_and_then_rests(world):
    engine, mind, memory = world
    ours = [runtime_event(engine, n, historical=False) for n in range(3)]
    theirs = [runtime_event(engine, n, historical=True) for n in range(3, 6)]
    jobs = Jobs()
    graph = GraphMigration(mind)
    started = graph.queue_history(jobs, "fixture-v1")
    assert started["state"] == "pending" and set(jobs.enqueued[0]) == set(ours) and not set(theirs) & set(jobs.enqueued[0])
    jobs.states[started["job_id"]] = "superseded"
    finished = graph.queue_history(jobs, "fixture-v1")
    assert finished["state"] == "complete" and finished["ended"] == {"superseded": 1}
    # Finished: later calls return what is stored and write nothing.
    before = migration(engine, "event-graph-semantic-v1")
    generation = engine.db.generation()
    assert graph.queue_history(jobs, "fixture-v1")["state"] == "complete"
    assert migration(engine, "event-graph-semantic-v1") == before and len(jobs.enqueued) == 1
    assert engine.db.generation() == generation


def quarantined(engine, identifier, reason="repeated-failure:RuntimeError", **data):
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_appraisals VALUES(?,?,?,?,0,2,?)",
                     (identifier, SCOPE.key(), "needs-repair", time.time() - 7 * 86400,
                      dumps({"stimulus": "memory-enrichment", "repair_reason": reason, "error": reason, **data})))


def test_a_quarantined_appraisal_can_be_retired_without_a_model(world):
    engine, mind, memory = world
    from kin_mind.appraisal import Appraisals

    Appraisals(mind)
    quarantined(engine, "stale-1")
    quarantined(engine, "stale-2", reason="native-review-unconfirmed")
    status = operational_status(mind)["memory"]["quarantined"]
    assert status["count"] == 2 and status["reasons"] == {"repeated-failure:RuntimeError": 1, "native-review-unconfirmed": 1}
    result = recovery.recover_quarantined(mind, job_ids=["stale-1"], command_id="retire-1", source="operator", retire=True)
    assert result["state"] == "retired" and result["retired"] == ["stale-1"]
    with engine.db.connect() as conn:
        state, data = conn.execute("SELECT state,data FROM mind_appraisals WHERE id='stale-1'").fetchone()
    data = json.loads(data)
    assert state == "superseded" and data["recovery_history"][-1]["retired"] and data["retired_reason"]
    # The same command again is a replay, not a second change.
    assert recovery.recover_quarantined(mind, job_ids=["stale-1"], command_id="retire-1", source="operator",
                                        retire=True) == result
    assert operational_status(mind)["memory"]["quarantined"]["count"] == 1


def test_history_recovery_judges_afresh_and_knows_every_admission_wait(world):
    engine, mind, memory = world
    from kin_mind.appraisal import Appraisals
    from kin_mind.model_lanes import WAIT_LEDGER

    Appraisals(mind)
    quarantined(engine, "waited", reason=WAIT_LEDGER, proposed_result={"an": "older shape"},
                receipt=None)
    with pytest.raises(ValueError):
        recovery.recover_history(mind, job_ids=["waited"], command_id="c1", source="operator",
                                 workers_stopped=True, replacements={"waited": {"proposal": {}}})
    result = recovery.recover_history(mind, job_ids=["waited"], command_id="c2", source="operator",
                                      workers_stopped=True, admission_only=True)
    assert result["resumed"] == ["waited"]
    with engine.db.connect() as conn:
        state, data = conn.execute("SELECT state,data FROM mind_appraisals WHERE id='waited'").fetchone()
    assert state == "pending" and json.loads(data)["seed_rejected"] is True


def digest_row(engine, event_id, state, data):
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_event_digests(scope,event_id,state,generation,dirty_at,due_at,data) VALUES(?,?,?,?,?,?,?)",
                     (SCOPE.key(), event_id, state, 1, "2026-09-20T00:00:00+00:00", 0, dumps(data)))


def test_a_failed_digest_is_tried_again_after_a_cooldown_a_few_times(world):
    engine, mind, memory = world
    at = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    digest_row(engine, "old-failure", "failed", {"last_error": "ConnectError"})
    digest_row(engine, "fresh-failure", "failed", {"failed_at": (at - timedelta(hours=1)).isoformat()})
    digest_row(engine, "given-up", "failed", {"failed_retries": lifecycle.DIGEST_RETRY_LIMIT})
    with engine.db.connect(write=True) as conn:
        retried = lifecycle.retry_failed(conn, SCOPE.key(), at.isoformat())
        rows = {r[0]: (r[1], r[2], json.loads(r[3])) for r in conn.execute(
            "SELECT event_id,state,generation,data FROM mind_event_digests WHERE scope=?", (SCOPE.key(),))}
    assert retried == ["old-failure"]
    assert rows["old-failure"][:2] == ("dirty", 2) and rows["old-failure"][2]["dirty_reason"] == "failed-retry"
    assert rows["fresh-failure"][0] == "failed" and rows["given-up"][0] == "failed"
    assert lifecycle.DIGEST_PRIORITIES["failed-retry"] == lifecycle.DIGEST_PRIORITIES["derivative-recovery"] == 200


def test_a_digest_job_on_its_way_is_recognised_whatever_the_key_order(world):
    engine, mind, memory = world
    memory.configure({"event_lifecycle": True})
    at = datetime.now(timezone.utc)
    digest_row(engine, "event-a", "dirty", {"dirty_reason": "membership"})
    with engine.db.connect(write=True) as conn:
        lifecycle.schedule(engine, conn, at.isoformat())
        first = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='event_digest'").fetchone()[0]
        conn.execute("UPDATE mind_event_digests SET generation=generation+1 WHERE event_id='event-a'")
        lifecycle.schedule(engine, conn, at.isoformat())
        second = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='event_digest'").fetchone()[0]
    # A new generation while the first job is still pending does not queue a second job.
    assert first == 1 and second == 1


def test_policy_only_digest_finishes_without_model_and_rechecks_generation(world):
    from eventmem.core.db import Conflict
    engine, mind, memory = world
    memory.configure({"event_lifecycle": True, "recall_purpose_policy": True})
    sid = engine.receive(SourceInput(namespace="mind-internal-event", key="idle", text="Internal wake-up",
        scope=SCOPE, authority="model", extract=False, metadata={"host_event": "internal-idle-review"}))["id"]
    with engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [sid])
        event = memory.graph._put(conn, {"id": "policy-event", "kind": "event", "title": "Internal wake-up",
            "text": "", "source_ids": [sid], "evidence": refs, "basis": "inferred"})
    life = lifecycle.EventLifecycle(mind)
    with engine.db.connect() as conn:
        snapshot = life.snapshot(conn, event["id"])
    assert not snapshot["records"] and snapshot["filtered"]
    apply = life.prepare_digest(event["id"], provider=object())
    with engine.db.connect(write=True) as conn:
        apply(conn)
        row = conn.execute("SELECT state,data FROM mind_event_digests WHERE event_id=?", (event["id"],)).fetchone()
        assert row[0] == "excluded"
        assert life.read(event["id"], conn=conn)["pending"] is False
        assert json.loads(row[1])["excluded_reason"] == "experience-policy"
        assert lifecycle.retry_failed(conn, SCOPE.key(), mind.clock()) == []
        lifecycle.mark_dirty(conn, SCOPE.key(), [event["id"]], mind.clock())
        with pytest.raises(Conflict):
            apply(conn)
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM mind_event_digests WHERE event_id=?", (event["id"],)).fetchone()[0] == "dirty"
