"""Derivative recovery: failed embed/digest jobs requeued once, history linked.

Every test runs against a real store in tmp_path. Production-shaped failure rows
are written directly (state='failed', attempts exhausted), which is exactly the
shape the inspection reads; execution afterwards goes through the real Worker.
"""
import json
import threading
import time
from datetime import datetime, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing, digest
from eventmem.core.jobs import Worker
from eventmem.core.models import RevisionInput, Scope, SourceInput
from eventmem.core.providers import Providers
from kin_mind import derivative_recovery as dr
from kin_mind.lifecycle import (
    EventLifecycle,
    EventRoute,
    EventSummary,
    SummaryUnit,
    mark_dirty,
    schedule,
)
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 19, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "memory")
    mind = Mind(engine, Scope(persona="synthetic-w2"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)

    def source(key, text=None, authority="explicit", kind="observation"):
        return engine.receive(SourceInput(namespace="synthetic", key=key, text=text or key,
            scope=mind.scope, authority=authority, kind=kind,
            occurred_at=mind.clock(), extract=False))

    initial = source("initial", "Synthetic recovery fixture")
    mind.initialize(agent_version="fixture", evidence_ids=[initial["id"]])
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True,
                      "event_lifecycle": True})
    return mind, memory, EventLifecycle(mind, memory.graph), source, clock


class Summarizer:
    timeout = 300
    background = False

    def structured(self, name, schema, system, context, **options):
        assert name == "submit_event_digest"
        records = context["records"]
        return EventSummary(narrative=[SummaryUnit(text=r["text"], record_ids=[r["id"]]) for r in records]), \
            {"model": "synthetic", "reasoning": "high", "input_tokens": 12, "output_tokens": 7}


def make_event(system, key, texts=("星图报告最初版本还未发送。",)):
    mind, memory, lifecycle, source, _ = system
    srcs = [source(f"{key}-{n}", text) for n, text in enumerate(texts)]
    proposal = EventRoute(key=key, action="create", title="星图报告", evidence_ids=[s["id"] for s in srcs],
                          binding="semantic_candidate", reason="Sourced fictional event")
    with mind.engine.db.connect(write=True) as conn:
        proof = memory.graph.proof(conn, [s["id"] for s in srcs])
        result = lifecycle.apply_routes(conn, [proposal], proof, "appraisal-" + key)[0]
    return result["event_id"]


def publish(system, event_id):
    mind, _, lifecycle, _, _ = system
    apply = lifecycle.prepare_digest(event_id, Summarizer())
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
    return lifecycle.read(event_id)


def quiesce(engine):
    """Silence background queue entries the test does not drive."""
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET state='complete' WHERE state IN ('pending','retry')")


def fail_digest(system, event_id):
    """The production shape: digest row failed, its job failed, attempts spent."""
    mind, _, _, _, _ = system
    scope_key = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        mark_dirty(conn, scope_key, [event_id], mind.clock(), "source-revision")
        generation = conn.execute(
            "SELECT generation FROM mind_event_digests WHERE scope=? AND event_id=?",
            (scope_key, event_id)).fetchone()[0]
        jid = mind.engine.enqueue(
            "event_digest", {"scope": mind.scope.model_dump(), "event_id": event_id},
            f"event-digest:{digest(scope_key)}:{event_id}:{generation}", conn=conn)
        conn.execute("UPDATE jobs SET state='failed',attempts=5,error='FileNotFoundError' WHERE id=?", (jid,))
        conn.execute("UPDATE mind_event_digests SET state='failed',data=json_set(data,'$.last_error','FileNotFoundError') WHERE scope=? AND event_id=?",
                     (scope_key, event_id))
    return jid


def failed_embed_job(engine, record_id, revision):
    jid = "job_" + digest(f"embed:{record_id}:{revision}")[:32]
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET state='failed',attempts=5,error='FileNotFoundError' WHERE id=?", (jid,))
    return jid


def record_of(engine, source_result):
    return engine.source(source_result["id"])["record_ids"][0]


def stub_embed(monkeypatch, dims=4, fail=None):
    def embed(self, texts, role="embedding"):
        if fail:
            raise fail
        from eventmem.core.vectors import VectorIndex
        index_id = VectorIndex.register(self.engine, "synthetic-model", dims, "test-v1")
        return [[1.0] + [0.0] * (dims - 1) for _ in texts], index_id

    monkeypatch.setattr(Providers, "embed", embed)


def synthetic_index(engine, dims=4):
    from eventmem.core.vectors import VectorIndex
    return VectorIndex(engine, "vec_" + digest(["synthetic-model", dims, "test-v1"])[:24])


def vector_receipt(engine, record_ids):
    table = synthetic_index(engine).table()
    predicate = "id IN (" + ",".join(
        "'" + record_id.replace("'", "''") + "'" for record_id in record_ids
    ) + ")"
    rows = table.search().where(predicate).select(["id", "revision"]).limit(len(record_ids)).to_list()
    return {"probed": True, "contract_verified": True,
            "revisions": {row["id"]: row["revision"] for row in rows}}


def reviewed_manifest(engine, vector_revisions=None):
    with engine.db.connect() as conn:
        current = dr.plan(conn, {} if vector_revisions is None else vector_revisions)
    current["digests"] = dr.attach_digest_review_evidence(engine, current["digests"])
    vector_targets = [
        {"record_id": entry["record_id"], "vector_revision": entry["vector_revision"]}
        for entry in current["embeds"] if entry.get("action") == "recover"
    ]
    manifest = {
        "schema_version": 2,
        "kind": "w2-derivative-recovery-dry-run",
        "store": {"root": str(engine.db.root)},
        "vector_store": {
            "probed": True,
            "contract_verified": True,
            "table": "vec_synthetic",
            "version": 1,
            "dimensions": 4,
            "query": "targeted-id-filter",
            "targets": vector_targets,
            "probe_receipt": digest({"table": "vec_synthetic", "version": 1,
                                     "targets": vector_targets}),
            "contract": {
                "resolved": True,
                "index_id": "vec_synthetic",
                "model": "synthetic-model",
                "dimensions": 4,
                "preprocessing": "test-v1",
            },
        },
        "embeds": current["embeds"],
        "digests": current["digests"],
    }
    manifest["review_fingerprint"] = dr.manifest_review_fingerprint(manifest)
    return manifest, current


def drive_to_terminal(engine, job_ids, rounds=40):
    """Run the Worker until every named job is terminal. Retry backoff is elapsed
    by hand — the queue's own backoff window is not what a test waits for."""
    worker = Worker(engine)
    rows = []
    for _ in range(rounds):
        with engine.db.connect(write=True) as conn:
            rows = conn.execute(
                f"SELECT id,state FROM jobs WHERE id IN ({','.join('?' for _ in job_ids)})",
                tuple(job_ids)).fetchall()
            if all(r["state"] in ("complete", "failed", "canceled") for r in rows):
                break
            conn.execute("UPDATE jobs SET available=0 WHERE state='retry'")
        worker.run_once()
    else:
        raise AssertionError(f"jobs never settled: {rows}")
    with engine.db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT id,state,error,updated_at FROM jobs WHERE id IN ({','.join('?' for _ in job_ids)})",
            tuple(job_ids)).fetchall()]


def test_recover_bounds_and_failure_linkage(system):
    mind, _, _, source, _ = system
    engine = mind.engine
    rid = record_of(engine, source("one", "A record whose vector was lost"))
    quiesce(engine)
    jid = failed_embed_job(engine, rid, 1)
    worker = Worker(engine)
    with pytest.raises(ValueError):
        worker.recover([], command_id="c")
    with pytest.raises(ValueError):
        worker.recover([jid, jid], command_id="c")
    with pytest.raises(ValueError):
        worker.recover([jid], command_id="")
    with pytest.raises(ValueError):
        worker.recover([f"job_{n:032x}" for n in range(51)], command_id="c")
    with pytest.raises(Missing):
        worker.recover(["job_" + "0" * 32], command_id="c")

    before = engine.get(rid)
    result = worker.recover([jid], command_id="w2-test-1")
    assert result["recovered"] == [jid] and not result["skipped"]
    with engine.db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        assert job["state"] == "pending" and job["attempts"] == 0 and job["error"] is None
        assert job["kind"] == "embed" and json.loads(job["payload"])["record_id"] == rid
        audit = conn.execute("SELECT * FROM job_recovery WHERE job_id=?", (jid,)).fetchall()
    assert len(audit) == 1
    assert audit[0]["command_id"] == "w2-test-1"
    assert audit[0]["prev_state"] == "failed"
    assert audit[0]["prev_error"] == "FileNotFoundError"
    assert audit[0]["prev_attempts"] == 5
    # Recovery never flipped the row to complete, and the record was not touched.
    assert job["state"] == "pending"
    assert engine.get(rid) == before

    # The same command again: nothing is failed any more, so nothing moves and
    # the audit trail stays exactly one row.
    again = worker.recover([jid], command_id="w2-test-1")
    assert again["skipped"] == [{"id": jid, "state": "pending"}]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_recovery WHERE job_id=?", (jid,)).fetchone()[0] == 1


def test_embed_inspection_and_end_to_end(system, monkeypatch):
    pytest.importorskip("lancedb")
    mind, _, _, source, _ = system
    engine = mind.engine
    active = record_of(engine, source("active", "Active fact still missing its vector"))
    unverified = record_of(engine, source("unverified", "Model inference never confirmed",
                                          authority="model", kind="commitment"))
    superseded = record_of(engine, source("superseded", "Statement about to be replaced", kind="knowledge"))
    engine.revise(superseded, RevisionInput(expected_revision=1, command_id="replace-1",
        action="replace", replacement_id=active, reason="no longer current"))
    moved = record_of(engine, source("moved", "Content before correction"))
    engine.revise(moved, RevisionInput(expected_revision=1, command_id="correct-1",
        action="correct", content="Content after correction", reason="fix"))
    quiesce(engine)

    assert engine.get(unverified)["status"] == "unverified"
    jobs = {name: failed_embed_job(engine, rid, rev)
            for name, rid, rev in (("active", active, 1), ("unverified", unverified, 1),
                                   ("superseded", superseded, 2), ("moved", moved, 1))}
    with engine.db.connect() as conn:
        entries = {e["record_id"]: e for e in dr.inspect_embeds(conn, {})}
    assert entries[active]["action"] == "recover"
    assert entries[unverified]["action"] == "recover"
    assert entries[unverified]["confirmation"] == "inferred"
    assert entries[superseded]["action"] == "skip"
    assert entries[superseded]["skip_reason"] == "record-not-current"
    assert entries[moved]["action"] == "skip"
    assert entries[moved]["skip_reason"] == "target-moved"
    assert entries[moved]["current_version_job_state"] == "complete"

    # A vector already at the current revision heals the gap without the queue.
    with engine.db.connect() as conn:
        healed = {e["record_id"]: e for e in dr.inspect_embeds(conn, {active: 1})}
    assert healed[active]["skip_reason"] == "vector-present"

    # An older vector row is evidence, but not evidence at the failed target.
    with engine.db.connect() as conn:
        stale = {e["record_id"]: e for e in dr.inspect_embeds(conn, {active: 0})}
    assert stale[active]["action"] == "recover"
    assert stale[active]["vector_at_target"] is False
    assert stale[active]["vector_revision"] == 0

    stub_embed(monkeypatch)
    result = dr.recover_embeddings(engine, list(entries.values()), command_id="w2-test-2")
    assert set(result["recovered"]) == {jobs["active"], jobs["unverified"]}
    # Skip entries are never requeued; abandonment is for entries that move after inspection.
    assert not result["abandoned"] and not result["skipped"]
    outcomes = drive_to_terminal(engine, result["recovered"])
    assert all(o["state"] == "complete" for o in outcomes)

    found = {r["id"]: r["revision"] for r in synthetic_index(engine).search([1.0, 0.0, 0.0, 0.0], limit=10)}
    assert found[active] == 1 and found[unverified] == 1
    assert superseded not in found
    # Unverified stays unverified at the same revision; only the derivative appeared.
    after = engine.get(unverified)
    assert after["status"] == "unverified" and after["confirmation"] == "inferred" and after["revision"] == 1

    # Idempotent by target version: a second pass over the same entries moves nothing.
    repeat = dr.recover_embeddings(engine, list(entries.values()), command_id="w2-test-2")
    assert not repeat["recovered"] and not repeat["abandoned"]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_recovery").fetchone()[0] == 2
        # The skipped rows stay failed: history is not rewritten.
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='failed'").fetchone()[0] == 2


def test_deleted_record_derivative_is_never_rebuilt(system):
    mind, _, _, source, _ = system
    engine = mind.engine
    doomed = record_of(engine, source("doomed", "Erased on owner request"))
    quiesce(engine)
    # delete() cancels queued work for the record; a row that still ended up
    # failed afterwards (an older cancel race) must never be brought back.
    engine.delete(doomed)
    jid = "job_" + digest(f"embed:{doomed}:1")[:32]
    with engine.db.connect(write=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs(id,kind,unique_key,payload,state,attempts,max_attempts,available,created_at,updated_at) "
            "VALUES(?,?,?,?, 'failed', 5, 5, 0, ?, ?)",
            (jid, "embed", f"embed:{doomed}:1", json.dumps({"record_id": doomed, "revision": 1}),
             "2026-09-18T00:00:00+00:00", "2026-09-18T00:00:00+00:00"))
    with engine.db.connect() as conn:
        (entry,) = [e for e in dr.inspect_embeds(conn, {}) if e["record_id"] == doomed]
    assert entry["action"] == "skip" and entry["skip_reason"] == "record-deleted"
    result = dr.recover_embeddings(engine, [entry], command_id="w2-test-3")
    assert not result["recovered"]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM job_recovery").fetchone()[0] == 0


def test_digest_ready_substitute_is_not_redone(system):
    mind, _, lifecycle, _, _ = system
    engine = mind.engine
    event_id = make_event(system, "ready")
    before = publish(system, event_id)
    assert before["state"] == "ready"
    quiesce(engine)
    # An old failure from an earlier generation sits in the queue as history.
    with engine.db.connect(write=True) as conn:
        jid = engine.enqueue("event_digest", {"scope": mind.scope.model_dump(), "event_id": event_id},
                             "event-digest:old-generation:1", conn=conn)
        conn.execute("UPDATE jobs SET state='failed',attempts=5,error='FileNotFoundError' WHERE id=?", (jid,))
    with engine.db.connect() as conn:
        (entry,) = [e for e in dr.inspect_digests(conn) if e["event_id"] == event_id]
    assert entry["action"] == "skip" and entry["skip_reason"] == "ready-substitute"
    result = dr.recover_digests(engine, [entry], command_id="w2-test-4")
    assert not result["recovered"]
    after = lifecycle.read(event_id)
    assert after["state"] == "ready" and after["revision"] == before["revision"]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_recovery").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "failed"


def test_failed_digest_recovered_through_normal_path(system, monkeypatch):
    mind, _, lifecycle, _, _ = system
    engine = mind.engine
    event_id = make_event(system, "gap", ("星图报告最初版本还未发送。", "继续星图报告：第二版已经完成。"))
    publish(system, event_id)
    quiesce(engine)
    old_jid = fail_digest(system, event_id)

    with engine.db.connect() as conn:
        (entry,) = [e for e in dr.inspect_digests(conn) if e["event_id"] == event_id]
    assert entry["action"] == "recover"
    assert entry["failed_job_ids"] == [old_jid]
    assert entry["error_classes"] == ["FileNotFoundError"]
    before_gen = entry["generation"]

    result = dr.recover_digests(engine, [entry], command_id="w2-test-5")
    (rec,) = result["recovered"]
    assert rec["generation"] == before_gen + 1 and rec["event_id"] == event_id
    with engine.db.connect() as conn:
        digest_row = conn.execute("SELECT state,generation FROM mind_event_digests WHERE event_id=?", (event_id,)).fetchone()
        assert digest_row["state"] == "dirty" and digest_row["generation"] == before_gen + 1
        old = conn.execute("SELECT state FROM jobs WHERE id=?", (old_jid,)).fetchone()
        new = conn.execute("SELECT * FROM jobs WHERE id=?", (rec["job_id"],)).fetchone()
        audit = conn.execute("SELECT * FROM job_recovery WHERE job_id=?", (rec["job_id"],)).fetchone()
    assert old["state"] == "failed"  # history is never rewritten
    assert new["state"] == "pending" and new["priority"] == 200
    assert audit["command_id"] == "w2-test-5" and audit["prev_error"] == "FileNotFoundError"
    assert audit["target"] == f"{event_id}@g{before_gen + 1}"

    # The ordinary scheduler does not double-enqueue the recovered event.
    with engine.db.connect(write=True) as conn:
        schedule(engine, conn, mind.clock())
        active = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='event_digest' AND state IN ('pending','retry','running') AND json_extract(payload,'$.event_id')=?",
            (event_id,)).fetchone()[0]
    assert active == 1

    # Execution reuses the normal digest path: DeepSeek high on current members.
    monkeypatch.setattr("kin_mind.appraisal.DeepSeek.from_engine", classmethod(lambda cls, eng: Summarizer()))
    outcomes = drive_to_terminal(engine, [rec["job_id"]])
    assert outcomes[0]["state"] == "complete"
    final = lifecycle.read(event_id)
    assert final["state"] == "ready" and final["data"]["model_receipt"]["model"] == "synthetic"
    with engine.db.connect() as conn:
        again = [e for e in dr.inspect_digests(conn) if e["event_id"] == event_id]
    assert again[0]["action"] == "skip" and again[0]["skip_reason"] == "ready-substitute"


def test_digest_concurrent_dirty_still_aborts_stale_commit(system, monkeypatch):
    """The pre-commit generation check stays in force for recovered digests."""
    pytest.importorskip("lancedb")
    stub_embed(monkeypatch)
    mind = system[0]
    engine = mind.engine
    event_id = make_event(system, "race")
    publish(system, event_id)
    quiesce(engine)
    fail_digest(system, event_id)
    with engine.db.connect() as conn:
        (entry,) = [e for e in dr.inspect_digests(conn) if e["event_id"] == event_id]
    (rec,) = dr.recover_digests(engine, [entry], command_id="w2-test-6")["recovered"]

    monkeypatch.setattr("kin_mind.appraisal.DeepSeek.from_engine", classmethod(lambda cls, eng: Summarizer()))
    worker = Worker(engine)
    job = worker.claim()
    assert job and job["id"] == rec["job_id"]
    apply = worker.prepare(job)
    # The event moves while the model answer is being prepared.
    _, _, _, source, _ = system
    source("race-continued", "星图报告第二版已经发出。")
    with engine.db.connect(write=True) as conn:
        mark_dirty(conn, mind.scope.key(), [event_id], mind.clock(), "source-revision")
        with pytest.raises(Conflict):
            apply(conn)


def test_runner_stops_on_environmental_failure_with_backoff(system, monkeypatch):
    mind, _, _, source, _ = system
    engine = mind.engine
    ids = [record_of(engine, source(f"env-{n}", f"Lost vector {n}")) for n in range(5)]
    quiesce(engine)
    jids = [failed_embed_job(engine, rid, 1) for rid in ids]
    stub_embed(monkeypatch, fail=FileNotFoundError("deleted venv"))
    sleeps, probes = [], []

    runner = dr.Runner(engine, embed_batch=2, digest_batch=1,
                       probe=lambda: probes.append(1) or True,
                       backoff=(10.0,), sleep=sleeps.append, poll=0)
    outcome = runner.run(command_id="w2-test-7", allow_unreviewed=True,
                         settle=lambda job_ids: drive_to_terminal(engine, job_ids))
    assert outcome["stopped"] == "environmental"
    assert sleeps == [10.0] and probes == [1]
    assert len(outcome["cycles"]) == 1
    assert set(outcome["cycles"][0]["environmental"]) == set(jids[:2])
    with engine.db.connect() as conn:
        audited = {r[0] for r in conn.execute("SELECT job_id FROM job_recovery")}
        states = {r["id"]: r["state"] for r in conn.execute(
            f"SELECT id,state FROM jobs WHERE id IN ({','.join('?' for _ in jids)})", jids)}
    # Only the first batch was ever requeued; the rest kept their failed history.
    assert audited == set(jids[:2])
    assert [states[j] for j in jids[2:]] == ["failed"] * 3
    assert set(outcome["blocked"]) >= set(jids[2:])


def test_runner_defers_to_foreground_leases(system, monkeypatch):
    pytest.importorskip("lancedb")
    mind, _, _, source, _ = system
    engine = mind.engine
    rid = record_of(engine, source("fg", "Vector lost while the owner works"))
    quiesce(engine)
    jid = failed_embed_job(engine, rid, 1)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_foreground_leases VALUES(?,?,?)",
                     (mind.scope.key(), "owner-session", time.time() + 60))
    sleeps = []
    runner = dr.Runner(engine, sleep=sleeps.append, poll=0)
    outcome = runner.run(command_id="w2-test-8", allow_unreviewed=True,
                         settle=lambda ids: [], max_cycles=2)
    assert outcome["stopped"] == "preempted" and not outcome["cycles"]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "failed"

    stub_embed(monkeypatch)
    with engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_foreground_leases")
    outcome = runner.run(command_id="w2-test-8", allow_unreviewed=True,
                         settle=lambda job_ids: drive_to_terminal(engine, job_ids))
    assert outcome["stopped"] is None
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "complete"


def test_runner_restart_mid_recovery_resumes(system, monkeypatch):
    pytest.importorskip("lancedb")
    mind, _, _, source, _ = system
    engine = mind.engine
    ids = [record_of(engine, source(f"rs-{n}", f"Restart witness {n}")) for n in range(4)]
    quiesce(engine)
    jids = [failed_embed_job(engine, rid, 1) for rid in ids]
    stub_embed(monkeypatch)

    first = dr.Runner(engine, embed_batch=2, sleep=lambda s: None)
    one = first.run(command_id="w2-test-9", allow_unreviewed=True,
                    settle=lambda job_ids: drive_to_terminal(engine, job_ids), max_cycles=1)
    assert len(one["cycles"]) == 1
    assert one["stopped"] == "cycle-limit"

    # The process dies here. A new runner with the same command id continues.
    second = dr.Runner(engine, embed_batch=2, sleep=lambda s: None)
    two = second.run(command_id="w2-test-9", allow_unreviewed=True,
                     settle=lambda job_ids: drive_to_terminal(engine, job_ids))
    assert two["stopped"] is None
    with engine.db.connect() as conn:
        audits = conn.execute("SELECT job_id,command_id FROM job_recovery").fetchall()
        states = {r["id"]: r["state"] for r in conn.execute(
            f"SELECT id,state FROM jobs WHERE id IN ({','.join('?' for _ in jids)})", jids)}
    assert len({a["job_id"] for a in audits}) == 4
    assert all(s == "complete" for s in states.values())
    costs = {(c["kind"], c["job_id"]): c for c in one["costs"] + two["costs"]}
    assert set(costs) == {("embed", j) for j in jids}
    assert all(c["state"] == "complete" and c["wall_seconds"] is not None and not c["priced"]
               and c["usage_status"] == "unknown" for c in costs.values())


def test_runner_reports_digest_receipt_per_event(system, monkeypatch):
    pytest.importorskip("lancedb")
    stub_embed(monkeypatch)
    mind, _, _, _, _ = system
    engine = mind.engine
    event_id = make_event(system, "costed", ("星图报告还未发送。", "星图报告第二版完成了。"))
    publish(system, event_id)
    quiesce(engine)
    fail_digest(system, event_id)
    monkeypatch.setattr("kin_mind.appraisal.DeepSeek.from_engine", classmethod(lambda cls, eng: Summarizer()))

    runner = dr.Runner(engine, sleep=lambda s: None)
    outcome = runner.run(command_id="w2-test-12", allow_unreviewed=True,
                         settle=lambda job_ids: drive_to_terminal(engine, job_ids))
    assert outcome["stopped"] is None
    (cost,) = [c for c in outcome["costs"] if c["kind"] == "event_digest"]
    assert cost["event_id"] == event_id and cost["state"] == "ready"
    assert cost["model"] == "synthetic" and cost["reasoning"] == "high"
    assert cost["input_tokens"] == 12 and cost["output_tokens"] == 7


def test_target_moved_between_plan_and_apply_abandons_cleanly(system, monkeypatch):
    pytest.importorskip("lancedb")
    mind, _, _, source, _ = system
    engine = mind.engine
    rid = record_of(engine, source("stale", "Version one of a moving record"))
    quiesce(engine)
    jid = failed_embed_job(engine, rid, 1)
    with engine.db.connect() as conn:
        (entry,) = [e for e in dr.inspect_embeds(conn, {}) if e["record_id"] == rid]
    assert entry["action"] == "recover"
    # The owner corrects the record before the reviewed batch runs.
    engine.revise(rid, RevisionInput(expected_revision=1, command_id="late-correction",
                                   action="correct", content="Version two", reason="moved"))
    result = dr.recover_embeddings(engine, [entry], command_id="w2-test-10")
    assert result["abandoned"] == [{"job_id": jid, "record_id": rid, "reason": "target-moved-or-not-current"}]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM job_recovery").fetchone()[0] == 0
    # The new version's own embed job proceeds normally; no stale vector is written.
    stub_embed(monkeypatch)
    current_jid = "job_" + digest(f"embed:{rid}:2")[:32]
    outcomes = drive_to_terminal(engine, [current_jid])
    assert outcomes[0]["state"] == "complete"
    found = {r["id"]: r["revision"] for r in synthetic_index(engine).search([1.0, 0.0, 0.0, 0.0], limit=10)}
    assert found[rid] == 2


def test_digest_no_longer_failed_at_apply_is_skipped(system):
    mind = system[0]
    engine = mind.engine
    event_id = make_event(system, "healed")
    publish(system, event_id)
    quiesce(engine)
    fail_digest(system, event_id)
    with engine.db.connect() as conn:
        (entry,) = [e for e in dr.inspect_digests(conn) if e["event_id"] == event_id]
    # Someone else's dirty pass lands before the reviewed batch.
    with engine.db.connect(write=True) as conn:
        mark_dirty(conn, mind.scope.key(), [event_id], mind.clock(), "source-revision")
    result = dr.recover_digests(engine, [entry], command_id="w2-test-11")
    assert result["skipped"] == [{"event_id": event_id, "state": "dirty", "reason": "no-longer-failed"}]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_recovery").fetchone()[0] == 0


def test_plan_counts_and_out_of_scope_listing(system):
    mind, _, _, source, _ = system
    engine = mind.engine
    from kin_mind.appraisal import Appraisals
    Appraisals(mind)  # creates the queue tables
    rid = record_of(engine, source("counted", "One lost vector"))
    quiesce(engine)
    failed_embed_job(engine, rid, 1)
    event_id = make_event(system, "counted-event")
    publish(system, event_id)
    fail_digest(system, event_id)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_appraisals VALUES(?,?,?,?,?,?,?)",
                     ("appraise_quarantined", mind.scope.key(), "needs-repair", 0.0, 0.0, 2,
                      json.dumps({"stimulus": "memory-enrichment"})))
    with engine.db.connect() as conn:
        current = dr.plan(conn, {})
    assert current["counts"]["embed_failed"] == 1 and current["counts"]["embed_recover"] == 1
    assert current["counts"]["digest_recover"] == 1
    repairs = current["out_of_scope"]["needs_repair_appraisals"]
    assert [r["id"] for r in repairs] == ["appraise_quarantined"]
    assert repairs[0]["stimulus"] == "memory-enrichment"


def test_missing_vector_evidence_blocks_recovery_and_runner_requires_review(system):
    mind, _, _, source, _ = system
    engine = mind.engine
    rid = record_of(engine, source("no-vector-proof", "Do not recompute without current Lance evidence"))
    quiesce(engine)
    failed_embed_job(engine, rid, 1)
    with engine.db.connect() as conn:
        (entry,) = [e for e in dr.inspect_embeds(conn) if e["record_id"] == rid]
    assert entry["action"] == "blocked"
    assert entry["block_reason"] == "vector-evidence-unavailable"
    with pytest.raises(ValueError, match="reviewed selection"):
        dr.Runner(engine).run(command_id="unsafe-enumeration")


def test_reviewed_batch_is_fixed_and_same_command_cannot_take_next_targets(system, monkeypatch):
    pytest.importorskip("lancedb")
    mind, _, _, source, _ = system
    engine = mind.engine
    ids = [record_of(engine, source(f"fixed-{n}", f"Reviewed vector gap {n}")) for n in range(5)]
    quiesce(engine)
    jids = [failed_embed_job(engine, rid, 1) for rid in ids]
    from eventmem.core.vectors import VectorIndex
    VectorIndex.register(engine, "synthetic-model", 4, "test-v1")
    synthetic_index(engine).table()
    manifest, current = reviewed_manifest(engine)
    reviewed_order = [
        entry["job_id"] for entry in manifest["embeds"] if entry["action"] == "recover"
    ]
    selection, review = dr.select_reviewed_targets(
        manifest, current, embed_limit=2, digest_limit=0
    )
    assert [e["job_id"] for e in selection["embeds"]] == reviewed_order[:2]
    assert review["remaining_reviewed"]["embeds"] == 3
    stub_embed(monkeypatch)

    runner = dr.Runner(
        engine, embed_batch=2, digest_batch=0, sleep=lambda _: None,
        vector_probe=lambda record_ids: vector_receipt(engine, record_ids),
    )
    first = runner.run(
        command_id="fixed-command", selection=selection,
        manifest_sha256="a" * 64, vector_revisions={},
        vector_evidence={"probed": True, "version": 1},
        settle=lambda job_ids: drive_to_terminal(engine, job_ids),
    )
    assert first["stopped"] is None
    assert [item["job_id"] for item in first["selected"]["embeds"]] == reviewed_order[:2]
    assert all(cost["vector_verified"] for cost in first["costs"])

    current_vectors = vector_receipt(engine, ids)["revisions"]
    with engine.db.connect() as conn:
        later = dr.plan(conn, current_vectors)
        later["reviewed_target_states"] = dr.reviewed_target_states(
            conn, manifest, current_vectors
        )
    later["digests"] = dr.attach_digest_review_evidence(engine, later["digests"])
    next_selection, _ = dr.select_reviewed_targets(
        manifest, later, embed_limit=2, digest_limit=0
    )
    assert [e["job_id"] for e in next_selection["embeds"]] == reviewed_order[2:4]

    replay = runner.run(
        command_id="fixed-command", selection=next_selection,
        manifest_sha256="a" * 64, vector_revisions=current_vectors,
        vector_evidence={"probed": True, "version": 2},
        settle=lambda job_ids: drive_to_terminal(engine, job_ids),
    )
    assert replay["idempotent_replay"] is True
    assert [item["job_id"] for item in replay["selected"]["embeds"]] == reviewed_order[:2]
    with engine.db.connect() as conn:
        states = {row["id"]: row["state"] for row in conn.execute(
            f"SELECT id,state FROM jobs WHERE id IN ({','.join('?' for _ in jids)})", jids
        )}
    assert [states[jid] for jid in reviewed_order[2:]] == ["failed", "failed", "failed"]

    second = runner.run(
        command_id="next-command", selection=next_selection,
        manifest_sha256="a" * 64, vector_revisions=current_vectors,
        vector_evidence={"probed": True, "version": 2},
        settle=lambda job_ids: drive_to_terminal(engine, job_ids),
    )
    assert second["stopped"] is None
    with engine.db.connect() as conn:
        assert [conn.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0]
                for jid in reviewed_order] == ["complete", "complete", "complete", "complete", "failed"]


def test_command_lease_blocks_same_command_and_fences_other_command_receipt(system, monkeypatch):
    pytest.importorskip("lancedb")
    mind, _, _, source, _ = system
    engine = mind.engine
    rid = record_of(engine, source("leased", "One command owns this recovery batch"))
    quiesce(engine)
    jid = failed_embed_job(engine, rid, 1)
    from eventmem.core.vectors import VectorIndex
    VectorIndex.register(engine, "synthetic-model", 4, "test-v1")
    synthetic_index(engine).table()
    manifest, current = reviewed_manifest(engine)
    selection, _ = dr.select_reviewed_targets(
        manifest, current, embed_limit=1, digest_limit=0
    )
    stub_embed(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    first_result = {}

    def blocking_settle(job_ids):
        entered.set()
        assert release.wait(10)
        return drive_to_terminal(engine, job_ids)

    first_runner = dr.Runner(
        engine, embed_batch=1, digest_batch=0,
        vector_probe=lambda ids: vector_receipt(engine, ids),
        command_lease_seconds=15,
    )

    def run_first():
        first_result.update(first_runner.run(
            command_id="leased-command", selection=selection,
            manifest_sha256="e" * 64, vector_revisions={},
            settle=blocking_settle,
        ))

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(10)

    second = dr.Runner(
        engine, embed_batch=1, digest_batch=0,
        vector_probe=lambda ids: vector_receipt(engine, ids),
        command_lease_seconds=15,
    ).run(
        command_id="leased-command", selection=selection,
        manifest_sha256="e" * 64, vector_revisions={},
        settle=lambda _: pytest.fail("a second owner must not settle the command"),
    )
    assert second["stopped"] == "command-running" and second["state"] == "running"

    other = dr.Runner(
        engine, embed_batch=1, digest_batch=0,
        vector_probe=lambda ids: vector_receipt(engine, ids),
        command_lease_seconds=15,
    ).run(
        command_id="other-command", selection=selection,
        manifest_sha256="e" * 64, vector_revisions={},
        settle=lambda _: pytest.fail("another command must not settle the first command's job"),
    )
    assert other["stopped"] == "preflight-drift"
    with engine.db.connect() as conn:
        first_row = conn.execute(
            "SELECT state,owner,fence,receipt FROM mind_derivative_recovery_runs "
            "WHERE command_id='leased-command'"
        ).fetchone()
        other_row = conn.execute(
            "SELECT state,receipt FROM mind_derivative_recovery_runs "
            "WHERE command_id='other-command'"
        ).fetchone()
        audits = conn.execute(
            "SELECT command_id FROM job_recovery WHERE job_id=?", (jid,)
        ).fetchall()
    assert first_row["state"] == "running" and first_row["owner"] == first_runner.owner
    assert first_row["fence"] == 1 and first_row["receipt"] is None
    assert other_row["state"] == "stopped" and other_row["receipt"] is not None
    assert [row[0] for row in audits] == ["leased-command"]

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert first_result["stopped"] is None
    with engine.db.connect() as conn:
        row = conn.execute(
            "SELECT state,owner,lease_until,fence,receipt FROM mind_derivative_recovery_runs "
            "WHERE command_id='leased-command'"
        ).fetchone()
    assert row["state"] == "complete" and row["owner"] is None and row["lease_until"] == 0
    assert row["fence"] == 1 and row["receipt"] is not None


def test_expired_command_lease_resumes_original_selection(system, monkeypatch):
    pytest.importorskip("lancedb")
    mind, _, _, source, _ = system
    engine = mind.engine
    ids = [record_of(engine, source(f"resume-{n}", f"Crash recovery target {n}")) for n in range(2)]
    quiesce(engine)
    jids = [failed_embed_job(engine, rid, 1) for rid in ids]
    from eventmem.core.vectors import VectorIndex
    VectorIndex.register(engine, "synthetic-model", 4, "test-v1")
    synthetic_index(engine).table()
    manifest, current = reviewed_manifest(engine)
    all_selection, _ = dr.select_reviewed_targets(
        manifest, current, embed_limit=2, digest_limit=0
    )
    original = all_selection | {"embeds": all_selection["embeds"][:1]}
    proposed_after_crash = all_selection | {"embeds": all_selection["embeds"][1:]}
    claimed = dr.bind_recovery_selection(
        engine, command_id="crashed-command", manifest_sha256="f" * 64,
        selection=original, embed_limit=1, digest_limit=0,
        owner="crashed-owner", lease_seconds=15,
    )
    assert claimed["claimed"] is True and claimed["fence"] == 1
    with engine.db.connect(write=True) as conn:
        conn.execute(
            "UPDATE mind_derivative_recovery_runs SET lease_until=? "
            "WHERE command_id='crashed-command'",
            (time.time() - 1,),
        )
    stub_embed(monkeypatch)

    resumed = dr.Runner(
        engine, embed_batch=1, digest_batch=0,
        vector_probe=lambda record_ids: vector_receipt(engine, record_ids),
        command_lease_seconds=15,
    ).run(
        command_id="crashed-command", selection=proposed_after_crash,
        manifest_sha256="f" * 64, vector_revisions={},
        settle=lambda job_ids: drive_to_terminal(engine, job_ids),
    )
    original_job = original["embeds"][0]["job_id"]
    proposed_job = proposed_after_crash["embeds"][0]["job_id"]
    assert resumed["stopped"] is None
    assert [entry["job_id"] for entry in resumed["selected"]["embeds"]] == [original_job]
    with engine.db.connect() as conn:
        states = dict(conn.execute(
            f"SELECT id,state FROM jobs WHERE id IN ({','.join('?' for _ in jids)})", jids
        ).fetchall())
        run = conn.execute(
            "SELECT state,fence,receipt FROM mind_derivative_recovery_runs "
            "WHERE command_id='crashed-command'"
        ).fetchone()
    assert states[original_job] == "complete" and states[proposed_job] == "failed"
    assert run["state"] == "complete" and run["fence"] == 2 and run["receipt"] is not None

    stale_finish = dr._finish_recovery_run(
        engine, "crashed-command", {"stopped": "stale-owner-overwrite"},
        owner="crashed-owner", fence=1,
    )
    assert stale_finish["stopped"] is None
    assert stale_finish["idempotent_replay"] is True
    with engine.db.connect() as conn:
        unchanged = conn.execute(
            "SELECT state,fence,receipt FROM mind_derivative_recovery_runs "
            "WHERE command_id='crashed-command'"
        ).fetchone()
    assert unchanged["state"] == "complete" and unchanged["fence"] == 2
    assert unchanged["receipt"] == run["receipt"]


def test_new_failure_outside_reviewed_manifest_is_never_selected(system):
    mind, _, _, source, _ = system
    engine = mind.engine
    reviewed_id = record_of(engine, source("reviewed-only", "Reviewed missing vector"))
    quiesce(engine)
    reviewed_job = failed_embed_job(engine, reviewed_id, 1)
    manifest, _ = reviewed_manifest(engine)

    new_id = record_of(engine, source("after-review", "New failure after review"))
    quiesce(engine)
    new_job = failed_embed_job(engine, new_id, 1)
    with engine.db.connect() as conn:
        current = dr.plan(conn, {})
    current["digests"] = dr.attach_digest_review_evidence(engine, current["digests"])
    selection, review = dr.select_reviewed_targets(
        manifest, current, embed_limit=10, digest_limit=0
    )
    assert [entry["job_id"] for entry in selection["embeds"]] == [reviewed_job]
    assert [entry["job_id"] for entry in review["unreviewed"]["embeds"]] == [new_job]


def test_completed_job_without_target_vector_is_drift_not_success(system):
    mind, _, _, source, _ = system
    engine = mind.engine
    rid = record_of(engine, source("false-complete", "Queue state alone is not vector evidence"))
    quiesce(engine)
    jid = failed_embed_job(engine, rid, 1)
    manifest, _ = reviewed_manifest(engine)
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET state='complete',error=NULL WHERE id=?", (jid,))
    with engine.db.connect() as conn:
        current = dr.plan(conn, {})
        current["reviewed_target_states"] = dr.reviewed_target_states(conn, manifest, {})
    current["digests"] = dr.attach_digest_review_evidence(engine, current["digests"])
    selection, review = dr.select_reviewed_targets(
        manifest, current, embed_limit=1, digest_limit=0
    )
    assert not selection["embeds"]
    assert review["drifted"] == [{
        "kind": "embed", "job_id": jid, "record_id": rid,
        "target_revision": 1, "reason": "failed-job-no-longer-proven",
    }]


def test_reviewed_digest_membership_drift_abandons_without_generation_bump(system):
    mind = system[0]
    engine = mind.engine
    event_id = make_event(system, "reviewed-members")
    publish(system, event_id)
    quiesce(engine)
    fail_digest(system, event_id)
    with engine.db.connect() as conn:
        entries = dr.inspect_digests(conn)
    (entry,) = [
        item for item in dr.attach_digest_review_evidence(engine, entries)
        if item["event_id"] == event_id
    ]
    generation = entry["generation"]
    moved = entry | {"membership_input_hash": "not-the-reviewed-membership"}
    result = dr.recover_digests(
        engine, [moved], command_id="digest-drift", require_reviewed=True
    )
    assert not result["recovered"]
    assert result["abandoned"][0]["reason"] == "reviewed-generation-or-membership-moved"
    with engine.db.connect() as conn:
        row = conn.execute(
            "SELECT state,generation FROM mind_event_digests WHERE scope=? AND event_id=?",
            (mind.scope.key(), event_id),
        ).fetchone()
        assert row["state"] == "failed" and row["generation"] == generation
        assert conn.execute(
            "SELECT COUNT(*) FROM job_recovery WHERE command_id='digest-drift'"
        ).fetchone()[0] == 0


def test_reviewed_digest_runner_verifies_ready_receipt_and_replays(system, monkeypatch):
    mind = system[0]
    engine = mind.engine
    event_id = make_event(system, "strict-digest", ("第一条事实。", "第二条事实。"))
    publish(system, event_id)
    quiesce(engine)
    fail_digest(system, event_id)
    manifest, current = reviewed_manifest(engine)
    selection, _ = dr.select_reviewed_targets(
        manifest, current, embed_limit=0, digest_limit=1
    )
    assert [entry["event_id"] for entry in selection["digests"]] == [event_id]
    monkeypatch.setattr(
        "kin_mind.appraisal.DeepSeek.from_engine",
        classmethod(lambda cls, eng: Summarizer()),
    )
    runner = dr.Runner(engine, embed_batch=0, digest_batch=1, sleep=lambda _: None)
    first = runner.run(
        command_id="strict-digest-command", selection=selection,
        manifest_sha256="d" * 64,
        settle=lambda job_ids: drive_to_terminal(engine, job_ids),
    )
    assert first["stopped"] is None
    assert first["cycles"][0]["digest_verification"][0]["verified"] is True
    (cost,) = first["costs"]
    assert cost["scope"] == mind.scope.model_dump()
    assert cost["state"] == "ready" and cost["model"] == "synthetic"

    replay = runner.run(
        command_id="strict-digest-command", selection={
            "review_fingerprint": "different-current-view", "embeds": [], "digests": []
        },
        manifest_sha256="d" * 64,
    )
    assert replay["idempotent_replay"] is True
    assert replay["selection_digest"] == first["selection_digest"]


def test_runner_stops_on_nonterminal_batch_without_expanding(system):
    mind, _, _, source, _ = system
    engine = mind.engine
    ids = [record_of(engine, source(f"timeout-{n}", f"Timeout gap {n}")) for n in range(4)]
    quiesce(engine)
    jids = [failed_embed_job(engine, rid, 1) for rid in ids]
    ordered = sorted(jids)
    runner = dr.Runner(engine, embed_batch=2, digest_batch=0, sleep=lambda _: None)

    outcome = runner.run(
        command_id="timeout-command", allow_unreviewed=True,
        settle=lambda job_ids: [
            {"id": job_id, "state": "running", "error": None,
             "updated_at": "2026-09-19T00:00:00+00:00"}
            for job_id in job_ids
        ],
    )
    assert outcome["stopped"] == "settle-timeout"
    assert len(outcome["cycles"]) == 1
    with engine.db.connect() as conn:
        audited = {row[0] for row in conn.execute("SELECT job_id FROM job_recovery")}
        states = {row["id"]: row["state"] for row in conn.execute(
            f"SELECT id,state FROM jobs WHERE id IN ({','.join('?' for _ in jids)})", jids
        )}
    assert audited == set(ordered[:2])
    assert [states[jid] for jid in ordered[2:]] == ["failed", "failed"]


def test_digest_cost_receipt_is_scoped(system):
    engine = system[0].engine
    event_id = "evt_same_across_scopes"
    scope_a = Scope(persona="scope-a").key()
    scope_b_obj = Scope(persona="scope-b")
    scope_b = scope_b_obj.key()
    with engine.db.connect(write=True) as conn:
        for scope, model in ((scope_a, "wrong-scope"), (scope_b, "right-scope")):
            conn.execute(
                "INSERT INTO mind_event_digests(scope,event_id,state,generation,revision,input_hash,dirty_at,due_at,data) "
                "VALUES(?,?, 'ready', 4, 2, 'input', '2026-09-19T00:00:00+00:00', 0, ?)",
                (scope, event_id, json.dumps({
                    "model_receipt": {"model": model, "input_tokens": 3, "output_tokens": 2},
                    "source_versions": {},
                })),
            )
    result = {
        "command_id": "scope-cost",
        "cycles": [{
            "embeds": {"targets": []},
            "digests": {"recovered": [{
                "scope": scope_b_obj.model_dump(), "event_id": event_id,
                "job_id": "job_scope", "generation": 4,
            }], "replayed": []},
        }],
    }
    (cost,) = dr.collect_costs(engine, result)
    assert cost["scope"] == scope_b_obj.model_dump()
    assert cost["model"] == "right-scope"
    (summary,) = dr.costs_by_scope([cost])
    assert summary["scope"] == scope_b_obj.model_dump()
    assert summary["input_tokens"] == 3 and summary["output_tokens"] == 2
