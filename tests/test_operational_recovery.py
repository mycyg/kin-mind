import io
import json
import zipfile
from datetime import timedelta

import pytest

from eventmem.core.db import dumps
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.delivery_proof import verify_replacement
from kin_mind.memory import MemoryAssessment
from kin_mind.operational_status import operational_status

pytest_plugins = ("test_memory_continuity",)


def test_action_schema_fault_cannot_leak_thinking_or_block_core(system, monkeypatch):
    import httpx

    from kin_mind.appraisal import DeepSeek
    mind, _memory, source, _clock = system
    monkeypatch.setenv("KIN_TEST_API_KEY", "synthetic")
    payload = {"reason": "Current decision", "values": {"initiative": 80},
               "memory": {"graph": {"nodes": [{"basis": "invalid-provider-enum"}]}}}
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "model": "deepseek-flash", "id": "fixture", "stop_reason": "tool_use",
        "content": [{"type": "thinking", "thinking": "PRIVATE_FIXTURE_DO_NOT_COPY"},
                    {"type": "tool_use", "name": "submit_appraisal", "input": payload}]}))
    provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "KIN_TEST_API_KEY", transport=transport)
    proposal, receipt = provider.appraise({"state": mind.read(), "operational_only": True,
                                          "new_evidence": [{"id": source("current"), "text": "Current context"}]})
    assert proposal.values["initiative"] == 80
    assert proposal.memory == MemoryAssessment()
    assert "PRIVATE_FIXTURE" not in dumps([proposal.model_dump(), receipt])


def test_enrichment_lease_does_not_hold_an_action_review(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    history = jobs.enqueue([source("heavy")], "fixture-v1", stimulus="memory-enrichment")
    import time
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=? WHERE id=?", (time.time() + 600, history["id"]))
    current = jobs.enqueue([source("now")], "fixture-v1")
    assert jobs.run_one(Provider(), lane="action")["state"] == "complete"
    assert jobs.status(current["id"])["state"] == "complete"
    assert jobs.status(history["id"])["state"] == "running"


def test_recovery_coalesces_clock_wakeups_once_and_preserves_state(system):
    from kin_mind.recovery import migrate_operational
    mind, memory, _source, clock = system
    before = mind.read()["dimensions"]
    clock[0] += timedelta(hours=1)
    actions = ActionEvents(mind)
    memory.queue_idle(actions)
    # The caller's word for a stopped worker is no longer the check; an unheld
    # lane migrates whatever the boolean says (stage 5: liveness_checks).
    receipt = migrate_operational(mind, workers_stopped=False)
    assert receipt["superseded_idle_events"]
    assert migrate_operational(mind, workers_stopped=True) == receipt
    assert mind.read()["dimensions"] == before
    assert memory.due()["next_review"] == mind.clock()


class Provider:
    def __init__(self, fail_history=False):
        self.calls = []
        self.fail_history = fail_history

    def appraise(self, context):
        self.calls.append(context)
        if self.fail_history and context["stimulus"] == "memory-enrichment":
            raise RuntimeError("deepseek-evidence-compression-pending")
        return Appraisal(reason="Synthetic current decision", values={"initiative": 77} if context.get("operational_only") else {},
                         next_review_minutes=25, memory=MemoryAssessment()), {
                             "provider": "deepseek", "model": "deepseek-flash", "reasoning": "high", "request_id": "fixture"}


class CompressionProvider(Provider):
    """Stands in for Contexts.pack: a pass caches the evidence parts it finished
    and reports preparation as still pending. `parts=0` is a stalled pass."""

    def __init__(self, mind, parts=1):
        super().__init__()
        self.mind, self.parts, self.failure = mind, parts, None

    def appraise(self, context):
        import uuid

        from kin_mind.context import SCHEMA
        self.calls.append(context)
        if self.failure:
            raise RuntimeError(self.failure)
        with self.mind.engine.db.connect(write=True) as conn:
            conn.executescript(SCHEMA)
            for _ in range(self.parts):
                conn.execute("INSERT INTO mind_context_cache VALUES(?,?,?,?)",
                             (uuid.uuid4().hex, self.mind.scope.key(),
                              dumps({"value": {"entries": [{"item_ids": ["src"], "summary": "A compressed part"}],
                                               "omitted_ids": []}, "receipt": {"model": "deepseek-flash"}}),
                              self.mind.clock()))
        raise RuntimeError("deepseek-evidence-compression-pending:needs-compression")


def test_action_contract_excludes_unused_graph_schema():
    from kin_mind.appraisal import appraisal_schema
    full, action = appraisal_schema(), appraisal_schema(True)
    assert action["properties"]["memory"]["additionalProperties"] is False
    assert "MemoryAssessment" not in action["$defs"]
    assert len(dumps(action)) < len(dumps(full))
    assert {"values", "wishes", "next_review_minutes"} <= action["properties"].keys()


def test_exploration_result_precedes_backlog_without_defeating_retry_backoff(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    old = jobs.enqueue([source("old-interaction")], "fixture-v1")
    current = jobs.enqueue([source("new-exploration")], "fixture-v1", stimulus="exploration-result")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (old["id"],))

    class PendingResult(Provider):
        def appraise(self, context):
            if context["stimulus"] == "exploration-result":
                self.calls.append(context)
                raise RuntimeError("synthetic-result-retry")
            return super().appraise(context)

    provider = PendingResult()
    result = jobs.run_one(provider, lane="action")
    assert result["id"] == current["id"]
    assert result["state"] == "pending"
    assert provider.calls[0]["stimulus"] == "exploration-result"
    assert jobs.status(old["id"])["attempts"] == 0
    # A failed urgent result keeps its backoff; it cannot starve due old work.
    assert jobs.run_one(provider, lane="action")["id"] == old["id"]
    assert jobs.status(old["id"])["state"] == "complete"


def test_action_commits_and_enrichment_is_atomic_durable_separate(system):
    mind, memory, source, clock = system
    memory.configure({"operational_lanes": True})
    jobs, provider = Appraisals(mind), Provider(fail_history=True)
    job = jobs.enqueue([source("current")], "fixture-v1")
    assert jobs.run_one(provider)["state"] == "complete"
    assert mind.read()["dimensions"]["initiative"]["value"] == 77
    with mind.engine.db.connect() as conn:
        child = conn.execute("SELECT * FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=?", (job["id"],)).fetchone()
    assert child["state"] == "pending"
    status = operational_status(mind)
    assert status["action"]["next_review"] == (clock[0] + timedelta(minutes=25)).isoformat()
    assert jobs.run_one(provider)["state"] == "pending"
    # A failed heavy batch cannot stop a newly due idle decision.
    clock[0] += timedelta(minutes=26)
    actions = ActionEvents(mind)
    memory.queue_idle(actions)
    actions.drain(jobs)
    assert jobs.run_one(provider)["state"] == "complete"
    assert provider.calls[-1]["stimulus"] in {"idle-review", "bootstrap"}
    assert not provider.calls[-1]["memory_context"]["graph_candidates"]


def test_compression_progress_freezes_evidence_and_heavy_failure_quarantines(system):
    """WP3: only a preparation pass that cached new parts keeps the frozen
    context and stays uncharged; a real heavy failure still quarantines."""
    mind, memory, source, _ = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = CompressionProvider(mind)
    first = source("first")
    job = jobs.enqueue([first], "fixture-v1", stimulus="memory-enrichment")
    result = jobs.run_one(provider)
    assert result["state"] == "pending" and result["attempts"] == 0
    original = provider.calls[-1]["new_evidence"]
    original_context = json.loads(dumps(provider.calls[-1]["memory_context"]))
    memory.ingest({"id": "unrelated", "kind": "owner-message", "text": "A later unrelated message", "at": mind.clock()})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    assert Appraisals(mind).run_one(provider)["state"] == "pending"
    assert provider.calls[-1]["new_evidence"] == original
    assert provider.calls[-1]["memory_context"] == original_context
    provider.failure = "deepseek-output-budget-exhausted"
    for expected in ("pending", "needs-repair"):
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
        assert Appraisals(mind).run_one(provider)["state"] == expected
        with mind.engine.db.connect() as conn:
            saved = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0])
        # A charged failure rebuilds the context instead of resending a stale one.
        assert "frozen_memory_context" not in saved
    assert jobs.status(job["id"])["repair_reason"] == "deepseek-output-budget-exhausted"


def test_pending_idle_only_blocks_its_actual_dependencies(system):
    mind, memory, _source, _ = system
    memory.configure({"operational_lanes": True})
    actions = ActionEvents(mind)
    with mind.engine.db.connect(write=True) as conn:
        actions.emit(conn, "idle-review", "old-clock", {})
        assert not mind._action_review_pending(conn, {"id": "wish-a", "evidence": []})
        actions.emit(conn, "wish-review", "wish-a", {"desire_id": "wish-a"})
        assert mind._action_review_pending(conn, {"id": "wish-a", "evidence": []})
        assert not mind._action_review_pending(conn, {"id": "wish-b", "evidence": []})


def test_failed_upload_verified_by_received_split_archive(tmp_path):
    import hashlib
    raw = b"synthetic-audio" * 50
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("sound.wav", raw)
    zipped = archive.getvalue()
    def record(id, data, accepted=False):
        p = tmp_path / (id + ".bin"); p.write_bytes(data)
        obj = {"id": id, "stage": "upload-failed", "submissionStarted": False,
               "artifact": {"path": str(p), "sha256": hashlib.sha256(data).hexdigest()},
               "state": "accepted" if accepted else "not-submitted"}
        if accepted:
            obj["messageId"] = "platform-" + id
        (tmp_path / (id + ".json")).write_text(dumps(obj))
    record("attempt", raw)
    record("part1", zipped[:150], True)
    record("part2", zipped[150:], True)
    proposal = {"attemptId": "attempt", "replacementIds": ["part1", "part2"]}
    proof = verify_replacement(tmp_path, proposal)
    assert proof["state"] == "not-submitted"
    assert len(proof["fulfilledBy"]) == 2
    assert proof["method"] == "archive-member-identical-bytes"
    bad = json.loads((tmp_path / "part2.json").read_text())
    bad["state"] = "unconfirmed"
    (tmp_path / "part2.json").write_text(dumps(bad))
    with pytest.raises(ValueError, match="not accepted"):
        verify_replacement(tmp_path, proposal)


def test_uncertain_submitted_upload_cannot_be_certified(tmp_path):
    (tmp_path / "attempt.json").write_text(dumps({"id": "attempt", "stage": "message-unconfirmed", "submissionStarted": True}))
    with pytest.raises(ValueError, match="boundary"):
        verify_replacement(tmp_path, {"attemptId": "attempt", "replacementIds": []})
