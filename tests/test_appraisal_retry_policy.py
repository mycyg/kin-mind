"""Bounded appraisal retries (A4/A10/A5): every lane has a cap, preparation
waits are not failures, and a failure is described back to the model (WP3)."""

import asyncio
import json
import time

import pytest
from test_operational_recovery import CompressionProvider, Provider

from eventmem.core.db import Conflict, Deleted, dumps
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.state import AffectiveEvent

pytest_plugins = ("test_memory_continuity",)


class Failing(Provider):
    """Scripted failures; one entry per attempt, `None` commits."""

    def __init__(self, errors):
        super().__init__()
        self.errors = list(errors)

    def appraise(self, context):
        result = super().appraise(context)
        error = self.errors.pop(0) if self.errors else None
        if error:
            raise error
        return result


def requeue(mind, job_id):
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job_id,))


def saved(mind, job_id):
    with mind.engine.db.connect() as conn:
        return json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()[0])


def metrics(mind, name):
    with mind.engine.db.connect() as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT data FROM metrics WHERE name=?", (name,)).fetchall()]


def test_action_lane_quarantines_after_the_fifth_charged_attempt(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    # Distinct deterministic signatures: this exercises the cap, not the repeat rule.
    provider = Failing([RuntimeError("deepseek-http-4" + code) for code in ("00", "01", "03", "04", "22")])
    job = jobs.enqueue([source("current")], "fixture-v1")
    for attempt, expected in enumerate(["pending"] * 4 + ["needs-repair"], start=1):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="action")
        assert (result["state"], result["attempts"]) == (expected, attempt)
    assert len(provider.calls) == 5
    # A quarantined row is never claimed again, so nothing more is paid for.
    assert Appraisals(mind).run_one(provider, lane="action") == {"state": "idle"}
    assert len(provider.calls) == 5
    quarantine = metrics(mind, "appraisal_quarantined")
    assert len(quarantine) == 1 and quarantine[0]["appraisal"] == job["id"]
    assert quarantine[0]["lane"] == "action" and quarantine[0]["charged_attempts"] == 5
    assert saved(mind, job["id"])["repair_reason"] == "charged-attempts-exhausted:5"


def test_repeated_signature_quarantines_on_its_second_attempt(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-missing-structured-result")] * 5)
    job = jobs.enqueue([source("current")], "fixture-v1")
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    requeue(mind, job["id"])
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "needs-repair"
    assert len(provider.calls) == 2
    stored = saved(mind, job["id"])
    assert stored["repair_reason"] == "repeated-failure:deepseek-missing-structured-result"
    assert stored["error"] == "deepseek-missing-structured-result" and stored["error_repeats"] == 2
    assert metrics(mind, "appraisal_quarantined")[0]["error_repeats"] == 2


def test_a_different_signature_restarts_the_repeat_counter(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-missing-structured-result"), RuntimeError("deepseek-model-unverified"), None])
    job = jobs.enqueue([source("current")], "fixture-v1")
    for expected in ("pending", "pending", "complete"):
        requeue(mind, job["id"])
        assert Appraisals(mind).run_one(provider, lane="action")["state"] == expected
    assert len(provider.calls) == 3
    assert not metrics(mind, "appraisal_quarantined")
    # A committed judgment keeps no failure record to send to the next model.
    assert {"error", "error_detail", "error_repeats"} & set(saved(mind, job["id"])) == set()


def test_configured_cap_replaces_the_default(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True, "max_charged_attempts": 2})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-http-400"), RuntimeError("deepseek-http-422")])
    job = jobs.enqueue([source("current")], "fixture-v1")
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    requeue(mind, job["id"])
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "needs-repair"
    assert saved(mind, job["id"])["repair_reason"] == "charged-attempts-exhausted:2"
    with pytest.raises(ValueError, match="Charged appraisal attempts"):
        memory.configure({"max_charged_attempts": 0})


def test_transient_provider_failures_spend_no_repair_budget(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-network-error"), RuntimeError("deepseek-http-503"), None])
    job = jobs.enqueue([source("current")], "fixture-v1")
    for failures in (1, 2):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="action")
        # No model output was produced: neither the cap nor the repeat rule applies.
        assert (result["state"], result["attempts"]) == ("pending", 0)
        assert result["transient_failures"] == failures
    assert not metrics(mind, "appraisal_quarantined")
    requeue(mind, job["id"])
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    assert "transient_failures" not in saved(mind, job["id"])
    # A real failure also ends the outage: its own budget starts from zero again.
    other = Appraisals(mind)
    second = other.enqueue([source("second")], "fixture-v1")
    charged = Failing([RuntimeError("deepseek-network-error"), RuntimeError("deepseek-model-unverified")])
    assert other.run_one(charged, lane="action")["attempts"] == 0
    requeue(mind, second["id"])
    result = Appraisals(mind).run_one(charged, lane="action")
    assert (result["state"], result["attempts"]) == ("pending", 1)
    assert "transient_failures" not in saved(mind, second["id"])


def test_a_transient_failure_rebuilds_a_context_no_compression_paid_for(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-network-error"), None])
    job = jobs.enqueue([source("current")], "fixture-v1")
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    first = json.loads(dumps(provider.calls[0]["memory_context"]))
    assert "frozen_memory_context" not in saved(mind, job["id"])
    memory.ingest({"id": "later", "kind": "owner-message", "text": "A later unrelated message", "at": mind.clock()})
    requeue(mind, job["id"])
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    # The attempt after the outage sees the world as it is now.
    assert provider.calls[1]["memory_context"]["latest_owner_seq"] > first["latest_owner_seq"]


def test_a_transient_failure_keeps_a_context_compression_was_paid_for(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = CompressionProvider(mind)
    job = jobs.enqueue([source("heavy")], "fixture-v1", stimulus="memory-enrichment")
    assert jobs.run_one(provider, lane="enrichment")["state"] == "pending"
    frozen = json.loads(dumps(provider.calls[-1]["memory_context"]))
    provider.failure = "deepseek-http-503"
    memory.ingest({"id": "later", "kind": "owner-message", "text": "A later unrelated message", "at": mind.clock()})
    requeue(mind, job["id"])
    result = Appraisals(mind).run_one(provider, lane="enrichment")
    assert (result["state"], result["attempts"], result["transient_failures"]) == ("pending", 0, 1)
    # Cached compression parts are keyed on these inputs, so they stay.
    assert saved(mind, job["id"])["frozen_memory_context"]
    requeue(mind, job["id"])
    assert Appraisals(mind).run_one(provider, lane="enrichment")["state"] == "pending"
    assert provider.calls[-1]["memory_context"] == frozen


def test_a_provider_outage_is_bounded_by_its_own_counter(system):
    from kin_mind.appraisal import MAX_TRANSIENT_FAILURES
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-http-502")] * (MAX_TRANSIENT_FAILURES + 1))
    job = jobs.enqueue([source("current")], "fixture-v1")
    for failure in range(1, MAX_TRANSIENT_FAILURES + 2):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="action")
        assert result["state"] == ("needs-repair" if failure > MAX_TRANSIENT_FAILURES else "pending")
        assert result["attempts"] == 0
    assert len(provider.calls) == 25
    assert saved(mind, job["id"])["repair_reason"] == "transient-failures-exhausted:25"
    assert metrics(mind, "appraisal_quarantined")[0]["transient_failures"] == 25


def test_timeouts_are_charged_but_never_quarantine_as_a_repeat(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-timeout")] * 5)
    job = jobs.enqueue([source("current")], "fixture-v1")
    for attempt, expected in enumerate(["pending"] * 4 + ["needs-repair"], start=1):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="action")
        assert (result["state"], result["attempts"]) == (expected, attempt)
    stored = saved(mind, job["id"])
    # The same signature five times over: capped, never quarantined as a repeat.
    assert stored["error_repeats"] == 5
    assert stored["repair_reason"] == "charged-attempts-exhausted:5"


def test_compression_waits_are_free_while_they_cache_new_parts(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = CompressionProvider(mind, parts=2)
    job = jobs.enqueue([source("heavy")], "fixture-v1", stimulus="memory-enrichment")
    for _ in range(4):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="enrichment")
        assert (result["state"], result["attempts"]) == ("pending", 0)
    assert len(provider.calls) == 4
    stored = saved(mind, job["id"])
    assert stored["compression_waits"] == 4 and stored["compression_stalls"] == 0
    assert stored["frozen_memory_context"] and "error_detail" not in stored
    with mind.engine.db.connect() as conn:
        available = conn.execute("SELECT available FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0]
    assert time.time() < available <= time.time() + 30
    frozen = json.loads(dumps(provider.calls[-1]["memory_context"]))
    # Preparation that stops producing parts is quarantined, not retried for ever.
    # A stall still keeps the frozen inputs: the cached parts are keyed on them.
    provider.parts = 0
    memory.ingest({"id": "later", "kind": "owner-message", "text": "A later unrelated message", "at": mind.clock()})
    for expected in ("pending", "pending", "needs-repair"):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="enrichment")
        assert (result["state"], result["attempts"]) == (expected, 0)
        assert provider.calls[-1]["memory_context"] == frozen
    assert len(provider.calls) == 7
    stored = saved(mind, job["id"])
    assert stored["repair_reason"] == "compression-stalled:3"
    # Quarantine releases them: an operator resume rebuilds the context.
    assert "frozen_memory_context" not in stored
    assert metrics(mind, "appraisal_quarantined")[0]["compression_waits"] == 7


def test_compression_waits_are_bounded_even_while_they_progress(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = CompressionProvider(mind)
    job = jobs.enqueue([source("heavy")], "fixture-v1", stimulus="memory-enrichment")
    from kin_mind.appraisal import MAX_COMPRESSION_WAITS
    for pass_number in range(1, MAX_COMPRESSION_WAITS + 2):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="enrichment")
        assert result["state"] == ("needs-repair" if pass_number > MAX_COMPRESSION_WAITS else "pending")
        assert result["attempts"] == 0
    assert saved(mind, job["id"])["repair_reason"] == "compression-passes-exhausted:13"


def test_a_free_compression_wait_still_records_its_model_call(system, monkeypatch):
    """An uncharged wait is not free of accounting: structured() books every call."""
    import httpx

    from kin_mind.appraisal import DeepSeek
    from kin_mind.context import Compression
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    monkeypatch.setenv("KIN_TEST_API_KEY", "synthetic")
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "model": "deepseek-flash", "id": "fixture", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "submit_compression",
                     "input": {"entries": [{"item_ids": ["a"], "summary": "A compressed part"}], "omitted_ids": []}}]}))
    compressor = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "KIN_TEST_API_KEY", transport=transport)
    compressor.engine = mind.engine

    class Preparing(Provider):
        def appraise(self, context):
            self.calls.append(context)
            compressor.structured("submit_compression", Compression, "Compress", {"items": ["a"]})
            raise RuntimeError("deepseek-evidence-compression-pending:needs-compression")

    jobs, provider = Appraisals(mind), Preparing()
    jobs.enqueue([source("heavy")], "fixture-v1", stimulus="memory-enrichment")
    result = jobs.run_one(provider, lane="enrichment")
    assert (result["state"], result["attempts"]) == ("pending", 0)
    usage = metrics(mind, "structured_model_usage")
    assert len(usage) == 1 and usage[0]["tool"] == "submit_compression"


def test_timeout_retry_rebuilds_the_memory_context(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-timeout"), None])
    job = jobs.enqueue([source("current")], "fixture-v1")
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    first = json.loads(dumps(provider.calls[0]["memory_context"]))
    assert "frozen_memory_context" not in saved(mind, job["id"])
    memory.ingest({"id": "later", "kind": "owner-message", "text": "A later unrelated message", "at": mind.clock()})
    requeue(mind, job["id"])
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    assert provider.calls[1]["memory_context"] != first
    # The rebuilt snapshot sees the message that arrived after the timeout.
    assert provider.calls[1]["memory_context"]["latest_owner_seq"] > first["latest_owner_seq"]


def test_commit_conflict_is_reported_back_as_a_static_code(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    private = source("private", "小光说的私密原文 PRIVATE_FIXTURE_DO_NOT_COPY")
    interference = source("interference")

    class Interfering(Provider):
        def appraise(self, context):
            if not self.calls:
                # A parallel affect judgment moves the state under this proposal.
                mind.record(AffectiveEvent(command_id="interference", agent_version="fixture-v1",
                                           expected_revision=mind.read()["revision"], evidence_ids=[interference],
                                           values={"initiative": 61}, reason="A synthetic parallel judgment"))
            return super().appraise(context)

    provider = Interfering()
    job = jobs.enqueue([private], "fixture-v1")
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    stored = saved(mind, job["id"])
    assert stored["error"] == "Conflict"
    assert stored["error_detail"] == {"class": "Conflict", "code": "mind-revision-changed",
                                      "message": "Mind revision changed; read current state before updating",
                                      "target": mind.scope.key(), "expected": 2, "actual": 3}
    assert "PRIVATE_FIXTURE" not in dumps(stored["error_detail"])
    requeue(mind, job["id"])
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    assert provider.calls[1]["previous_attempt"] == {
        "error_class": "Conflict", "code": "mind-revision-changed",
        "message": "Mind revision changed; read current state before updating"}
    assert "previous_attempt" not in provider.calls[0]


def test_held_sections_of_the_previous_attempt_reach_the_model(system):
    """WP2 writes these keys; WP3 only forwards them, and a missing key is empty."""
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-timeout"), None])
    job = jobs.enqueue([source("current")], "fixture-v1")
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    rejected = [{"section": "session_advice", "code": "unknown-observation", "message": "Unknown observation"}]
    held = [{"section": "wishes", "code": "habits-rejected"}]
    with mind.engine.db.connect(write=True) as conn:
        data = saved(mind, job["id"]) | {"rejected_sections": rejected, "held_sections": held,
                                         "dropped_fields": [["procedures"]]}
        conn.execute("UPDATE mind_appraisals SET available=0,data=? WHERE id=?", (dumps(data), job["id"]))
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    assert provider.calls[1]["previous_attempt"] == {
        "error_class": "RuntimeError", "code": "deepseek-timeout", "rejected_sections": rejected,
        "held_sections": held, "dropped_fields": [["procedures"]]}


def test_a_committed_input_is_never_sent_to_the_model_again(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs, provider = Appraisals(mind), Provider()
    job = jobs.enqueue([source("current")], "fixture-v1")
    assert jobs.run_one(provider, lane="action")["state"] == "complete"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='pending',available=0 WHERE id=?", (job["id"],))
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    assert len(provider.calls) == 1


def test_a_quarantined_action_parent_still_releases_its_children(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-missing-structured-result")] * 2)
    child = jobs.enqueue([source("child")], "fixture-v1", origin="reflection", stimulus="delivery")["id"]
    parent = jobs.enqueue([source("parent")], "fixture-v1", origin="reflection", stimulus="delivery")["id"]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (0, parent))
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (1, child))
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM mind_appraisals WHERE id=?", (child,)).fetchone()[0] == "batched"
    requeue(mind, parent)
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "needs-repair"
    with mind.engine.db.connect() as conn:
        released = conn.execute("SELECT state,available FROM mind_appraisals WHERE id=?", (child,)).fetchone()
    assert released["state"] == "pending" and released["available"] <= time.time()


def quarantine(mind, jobs, provider, evidence, **extra):
    """Drive one action-lane row to needs-repair through the charged-attempt cap."""
    job = jobs.enqueue([evidence], "fixture-v1", **extra)
    for _ in range(5):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="action")
    assert result["state"] == "needs-repair"
    return job["id"]


def test_an_operator_resume_returns_a_quarantined_action_row_to_the_queue(system):
    from kin_mind.recovery import recover_quarantined
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-http-4" + code) for code in ("00", "01", "03", "04", "22")])
    job = quarantine(mind, jobs, provider, source("current"))
    failed = saved(mind, job)
    args = {"job_ids": [job], "command_id": "wp3-resume", "source": "owner approval"}
    result = recover_quarantined(mind, **args)
    assert result["resumed"] == [job] and result["already_complete"] == []
    assert recover_quarantined(mind, **args) == result
    resumed = saved(mind, job)
    # The failure stays readable, and none of it is sent to the next model.
    history = resumed["recovery_history"][0]
    assert history["command_id"] == "wp3-resume" and history["source"] == "owner approval"
    assert history["attempts"] == 5 and history["error"] == "deepseek-http-422"
    assert history["error_detail"] == failed["error_detail"] and history["error_repeats"] == 1
    assert history["repair_reason"] == "charged-attempts-exhausted:5"
    assert not {"error", "error_detail", "repair_reason", "error_signature", "error_repeats"} & set(resumed)
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT state,attempts,available,lease FROM mind_appraisals WHERE id=?", (job,)).fetchone()
    assert (row["state"], row["attempts"], row["lease"]) == ("pending", 0, 0) and row["available"] <= time.time()
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    assert len(provider.calls) == 6 and "previous_attempt" not in provider.calls[-1]


def test_a_resume_command_is_fenced_to_its_own_batch_and_to_quarantined_rows(system):
    from eventmem.core.db import Missing
    from kin_mind.recovery import recover_quarantined
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-http-4" + code) for code in ("00", "01", "03", "04", "22")])
    job = quarantine(mind, jobs, provider, source("current"))
    pending = jobs.enqueue([source("other")], "fixture-v1")["id"]
    recover_quarantined(mind, job_ids=[job], command_id="wp3-resume", source="owner approval")
    with pytest.raises(Conflict, match="another batch"):
        recover_quarantined(mind, job_ids=[job], command_id="wp3-resume", source="a different approval")
    with pytest.raises(Conflict, match="quarantined"):
        recover_quarantined(mind, job_ids=[pending], command_id="wp3-pending", source="owner approval")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=? WHERE id=?", (time.time() + 600, pending))
    with pytest.raises(Conflict, match="quarantined"):
        recover_quarantined(mind, job_ids=[pending], command_id="wp3-running", source="owner approval")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='pending',lease=0,available=? WHERE id=?", (time.time() + 600, pending))
    with pytest.raises(Missing):
        recover_quarantined(mind, job_ids=["appraise_unknown"], command_id="wp3-unknown", source="owner approval")
    with pytest.raises(ValueError, match="sourced command"):
        recover_quarantined(mind, job_ids=[job, job], command_id="wp3-duplicate", source="owner approval")
    with pytest.raises(ValueError, match="sourced command"):
        recover_quarantined(mind, job_ids=[job], command_id="wp3-unsourced", source="")
    # A row that finished meanwhile is reported, never resumed a second time.
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    done = recover_quarantined(mind, job_ids=[job], command_id="wp3-after", source="owner approval")
    assert done == {**done, "resumed": [], "already_complete": [job]}


def test_the_host_action_resumes_a_quarantined_batch_parent_with_its_children(system):
    from kin_mind.host import dispatch
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-missing-structured-result")] * 2)
    child = jobs.enqueue([source("child")], "fixture-v1", origin="reflection", stimulus="delivery")["id"]
    parent = jobs.enqueue([source("parent")], "fixture-v1", origin="reflection", stimulus="delivery")["id"]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (0, parent))
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (1, child))
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    requeue(mind, parent)
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "needs-repair"
    with mind.engine.db.connect() as conn:
        # WP4 releases the absorbed child the moment the parent is quarantined.
        assert conn.execute("SELECT state FROM mind_appraisals WHERE id=?", (child,)).fetchone()[0] == "pending"
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
              "agent_version": "fixture-v1", "session_id": "fixture"}
    request = {"job_ids": [parent], "command_id": "wp3-host", "source": "owner approval"}
    result = dispatch(config, "recover-appraisals", request)
    assert result["resumed"] == [parent]
    assert dispatch(config, "recover-appraisals", request) == result
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (0, parent))
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (1, child))
    assert Appraisals(mind).run_one(provider, lane="action")["state"] == "complete"
    # The resumed parent still carries the child's evidence, so the released
    # child settles from that commit instead of paying for a model call.
    settled = Appraisals(mind).run_one(provider, lane="action")
    assert settled["id"] == child and settled["state"] == "complete"
    assert settled["result"] == {"already_integrated": True} and len(provider.calls) == 3


def test_a_resumed_historical_row_is_judged_again_instead_of_reusing_its_seed(system):
    from kin_mind.recovery import recover_quarantined
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-model-unverified")] * 2)
    job = jobs.enqueue([source("heavy")], "fixture-v1", stimulus="memory-enrichment")["id"]
    for expected in ("pending", "needs-repair"):
        requeue(mind, job)
        assert Appraisals(mind).run_one(provider, lane="enrichment")["state"] == expected
    with mind.engine.db.connect(write=True) as conn:
        data = saved(mind, job) | {"seed_memory": {"notes": []}, "seed_receipt": {"model": "deepseek-flash"}}
        conn.execute("UPDATE mind_appraisals SET data=? WHERE id=?", (dumps(data), job))
    recover_quarantined(mind, job_ids=[job], command_id="wp3-history", source="owner approval")
    resumed = saved(mind, job)
    assert resumed["seed_rejected"] is True and resumed["seed_memory"] == {"notes": []}
    assert Appraisals(mind).run_one(provider, lane="enrichment")["state"] == "complete"
    assert len(provider.calls) == 3


def test_error_detail_keeps_only_static_host_facts(system):

    from eventmem.core.db import Missing
    from kin_mind.appraisal import error_detail
    _mind, memory, *_ = system

    def raised(call):
        try:
            call()
        except Exception as error:  # noqa: BLE001 - the raise site is the subject
            return error

    host = raised(lambda: memory.configure({"max_charged_attempts": 0}))
    assert error_detail(host, "ValueError") == {"class": "ValueError", "message": "Charged appraisal attempts must be between 1 and 20"}
    # A payload repr from outside the host is never kept, only its class.
    assert error_detail(raised(lambda: int("小光说的原文")), "ValueError") == {"class": "ValueError"}
    assert error_detail(raised(lambda: Appraisal.model_validate({"reason": ""})), "ValidationError") == {"class": "ValidationError"}
    assert error_detail(Missing("Evidence source is unavailable"), "Missing") == {
        "class": "Missing", "message": "Evidence source is unavailable"}
    assert error_detail(Deleted("mem_" + "a" * 32), "Deleted") == {"class": "Deleted", "target": "mem_" + "a" * 32}
    assert error_detail(Conflict("Evidence is not current", code="evidence-not-current", target="mem_x"), "Conflict") == {
        "class": "Conflict", "message": "Evidence is not current", "code": "evidence-not-current", "target": "mem_x"}
    assert error_detail(RuntimeError("deepseek-timeout"), "deepseek-timeout") == {
        "class": "RuntimeError", "code": "deepseek-timeout"}


def test_conflict_keeps_its_message_and_the_api_still_maps_409(system):
    mind, *_ = system
    from eventmem.core.api import create_app
    plain, detailed = Conflict("Evidence is not current"), Conflict(
        "Evidence is not current", code="evidence-not-current", target="mem_x", expected=1, actual=2)
    assert str(plain) == str(detailed) == "Evidence is not current"
    assert (plain.code, plain.target, plain.expected, plain.actual) == (None, None, None, None)
    assert (detailed.code, detailed.target, detailed.expected, detailed.actual) == ("evidence-not-current", "mem_x", 1, 2)
    assert str(Deleted("This source was explicitly deleted")) == "This source was explicitly deleted"
    assert isinstance(detailed, Conflict) and detailed.args == ("Evidence is not current",)
    app = create_app(engine=mind.engine, token="test-credential", workers=False, mcp_enabled=False)
    response = asyncio.run(app.exception_handlers[Conflict](None, detailed))
    assert response.status_code == 409
    assert json.loads(response.body) == {"detail": "Evidence is not current"}
