import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict
from eventmem.core.models import RevisionInput, Scope, SourceInput
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.context import CompressedEntry, Compression, Contexts
from kin_mind.memory import (
    Disclosure,
    MemoryAssessment,
    MemoryContinuity,
    fingerprint_file,
)
from kin_mind.state import Mind


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 14, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    scope = Scope(persona="synthetic-memory")
    mind = Mind(engine, scope, clock=lambda: clock[0].isoformat(timespec="microseconds"))
    def source(key, text=None):
        return engine.receive(SourceInput(namespace="synthetic", key=key, text=text or key, scope=scope,
            occurred_at=clock[0].isoformat(), metadata={"role": "user", "host_event": "message"}))["id"]
    initial = source("initial")
    mind.initialize(agent_version="fixture-v1", evidence_ids=[initial])
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "context": True, "idle": True})
    actions = ActionEvents(mind)
    actions.configure({"command_id": "action-config", "agent_version": "fixture-v1", "expected_revision": mind.read()["revision"], "evidence_ids": [initial], "reason": "Synthetic permission"})
    return mind, memory, source, clock


def test_zip_identity_survives_repack_and_renaming(system, tmp_path):
    mind, memory, _, clock = system
    first, second = tmp_path / "Kin-original.zip", tmp_path / "uploaded-again.zip"
    with zipfile.ZipFile(first, "w") as out:
        out.writestr("original/report.md", "Synthetic report written by Kin.")
    with zipfile.ZipFile(second, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("new-folder/renamed.md", "Synthetic report written by Kin.")
    assert fingerprint_file(first)["sha256"] != fingerprint_file(second)["sha256"]
    a = memory.ingest({"id": "creation", "kind": "artifact-created", "at": mind.clock(), "task_id": "task-fixture", "artifact": {"path": str(first)}})
    clock[0] += timedelta(hours=1)
    b = memory.ingest({"id": "upload", "kind": "artifact-observed", "at": mind.clock(), "artifact": {"path": str(second)}})
    assert a["work_id"] == b["work_id"]
    work = memory.history("work", identifier=a["work_id"])["items"][0]
    assert work["created_by"] == "Kin"
    assert len(work["versions"]) == 2
    assert work["task_ids"] == ["task-fixture"]


def test_receipt_states_are_durable_and_do_not_invent_read(system):
    mind, memory, _, _ = system
    base = {"kind": "delivery", "at": mind.clock(), "channel": "synthetic", "delivery_id": "batch", "expected_bubbles": 2}
    one = {**base, "id": "one-accepted", "bubble_id": "one", "text": "A new thought", "state": "accepted", "message_id": "platform-one"}
    a = memory.ingest(one)
    assert memory.ingest(one) == a
    memory.ingest({**base, "id": "two-unknown", "bubble_id": "two", "text": "Its follow-up", "state": "unconfirmed"})
    share = memory.history("share")["items"][0]
    assert share["state"] == "partial"
    assert share["visibility"] == "unverified"
    memory.ingest({**base, "id": "two-reconciled", "bubble_id": "two", "text": "Its follow-up", "state": "accepted", "message_id": "platform-two"})
    assert memory.history("share")["items"][0]["state"] == "accepted"
    with pytest.raises(Conflict):
        memory.ingest({**one, "text": "Changed contents"})
    with pytest.raises(ValueError):
        memory.ingest({**base, "id": "missing-id", "state": "accepted"})


def test_idle_reassessment_without_a_drive_crossing(system):
    mind, memory, _, clock = system
    actions = ActionEvents(mind)
    assert memory.queue_idle(actions) is None
    clock[0] += timedelta(minutes=21)
    a = memory.queue_idle(actions)
    assert a and memory.queue_idle(actions) == a
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM mind_action_events WHERE kind='idle-review'").fetchone()[0] == 1
    MemoryContinuity(mind).queue_idle(actions)
    assert mind.read()["dimensions"]["initiative"]["value"] < 75


class Compressor:
    def __init__(self):
        self.calls = 0
    def structured(self, name, schema, system, context, **kwargs):
        self.calls += 1
        return Compression(entries=[CompressedEntry(item_ids=[i["id"] for i in context["items"]], summary="Kin created the file; delivery failed. The user has not confirmed reading it.")]), {"provider": "deepseek", "model": "deepseek-flash", "reasoning": "max"}


def test_compression_cache_sources_correction_and_no_text_slicing(system):
    mind, _, source, _ = system
    sid = source("long", "Kin created the file. Delivery failed.\n\n" + "Supporting detail about the task.\n\n" * 70)
    with mind.engine.db.connect() as conn:
        rid = mind._evidence(conn, [sid])[0]["record_id"]
    contexts, provider = Contexts(mind), Compressor()
    item = contexts.record_item(mind.engine.get(rid))
    first = contexts.pack([item], "Did you send it?", 150, provider=provider)
    assert first["state"] == "compressed"
    assert first["tokens"] <= 150
    assert "failed" in first["text"]
    assert contexts.pack([item], "Did you send it?", 150, provider=provider)["cache_hit"]
    assert provider.calls == 1
    mind.engine.revise(rid, RevisionInput(expected_revision=1, command_id="correct", action="correct", content="A later receipt confirms platform acceptance."))
    assert not contexts.pack([item], "Did you send it?", 150, provider=provider)["covered_ids"]
    assert mind.engine.get(rid)["content"] == "A later receipt confirms platform acceptance."


def test_tiny_budget_defers_complete_items_and_explicit_read_bypasses_seen(system):
    mind, _, source, _ = system
    sid = source("recall", "A unique nebula finding " * 100)
    with mind.engine.db.connect() as conn:
        rid = mind._evidence(conn, [sid])[0]["record_id"]
    contexts = Contexts(mind)
    item = contexts.record_item(mind.engine.get(rid))
    packed = contexts.pack([item], "nebula", 30, allow_model=False)
    assert packed["text"] == ""
    assert packed["omitted_ids"] == [rid]
    a = contexts.build("nebula", session="same-thread", event_id="turn-one", budget=3000)
    assert a["tokens"] <= 3000
    assert contexts.build("nebula", session="same-thread", event_id="turn-one", budget=3000) == a
    b = contexts.build("nebula", purpose="read", session="same-thread", budget=3000)
    assert rid in b["covered_ids"]
    with pytest.raises(Conflict):
        contexts.compact_ack("same-thread", "next", actual_session="other", completed=True)
    contexts.compact_ack("same-thread", "next", actual_session="same-thread", completed=True)
    assert contexts.window("same-thread")["used"] == 0


def test_disclosure_semantics_do_not_change_receipt_and_transaction_rolls_back(system):
    mind, memory, _, _ = system
    result = memory.ingest({"id": "sent", "kind": "delivery", "at": mind.clock(), "channel": "fixture", "text": "Our original thought", "state": "accepted", "message_id": "p1"})
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [result["source_id"]])
        memory.apply_assessment(conn, MemoryAssessment(disclosures=[Disclosure(share_id=result["share_id"], topic="old thought", summary="Already shared", mode="reminiscence")]), refs, "evaluation-one", result["seq"], 60, {"model": "deepseek-flash"})
    assert memory.history("share")["items"][0]["state"] == "accepted"
    assert memory.history("share")["items"][0]["mode"] == "reminiscence"
    with pytest.raises(RuntimeError), mind.engine.db.connect(write=True) as conn:
        memory.apply_assessment(conn, MemoryAssessment(), refs, "never-commit", 100, 20, {})
        raise RuntimeError("Simulated interruption")
    assert memory.semantic_context()["cursor"] == result["seq"]


def test_multiple_conversation_events_share_one_appraisal(system):
    mind, memory, source, clock = system
    jobs = Appraisals(mind)
    ids = []
    for index in range(3):
        sid = source(f"turn-{index}", f"Synthetic message {index}")
        memory.ingest({"id": f"turn-{index}", "kind": "owner-message", "at": mind.clock(), "text": f"Synthetic message {index}", "source_id": sid})
        ids.append(jobs.enqueue([sid], "fixture-v1")["id"])
    class Provider:
        calls = 0
        def appraise(self, context):
            self.calls += 1
            assert len(context["new_evidence"]) == 3
            assert len(context["memory_context"]["recent_interaction"]) == 3
            return Appraisal(reason="The latest conversation already addresses these messages.", next_review_minutes=90), {"model": "deepseek-flash"}
    provider = Provider()
    result = jobs.run_one(provider)
    assert result["state"] == "complete", result
    assert provider.calls == 1
    assert all(jobs.status(i)["state"] == "complete" for i in ids)
    assert timestamp_for_test(memory.semantic_context()["next_review"]) == clock[0] + timedelta(minutes=90)


def timestamp_for_test(value):
    return datetime.fromisoformat(value)


def test_late_appraisal_cannot_recreate_a_response_after_a_new_message(system):
    from kin_mind.appraisal import Wish
    mind, memory, source, clock = system
    sid = source("earlier", "Please tell me later")
    memory.ingest({"id": "earlier", "kind": "owner-message", "at": mind.clock(), "source_id": sid})
    jobs = Appraisals(mind)
    jobs.enqueue([sid], "fixture-v1")
    class Provider:
        def appraise(self, context):
            clock[0] += timedelta(minutes=1)
            sid = source("latest", "We already discussed that")
            memory.ingest({"id": "latest", "kind": "owner-message", "at": mind.clock(), "source_id": sid})
            return Appraisal(reason="Late interpretation", values={"initiative": 95}, wishes=[Wish(content="Reply to the older message", topic="earlier", kind="contact", strength=95, ttl_hours=1, completion="Send once")]), {"model": "deepseek-flash"}
    result = jobs.run_one(Provider())
    assert result["state"] == "complete", result
    assert result["result"]["new_interaction_pending"]
    assert not mind.read()["desires"]
    assert mind.read()["dimensions"]["initiative"]["value"] < 75


def test_historical_backfill_does_not_raise_scores_or_reset_idle_schedule(system):
    from kin_mind.appraisal import Wish
    mind, memory, _, _ = system
    receipt = memory.ingest({"id": "old", "kind": "delivery", "at": mind.clock(), "text": "Already shared", "state": "accepted", "message_id": "old-message", "historical": True})
    jobs = Appraisals(mind)
    before = memory.semantic_context()["next_review"]
    memory.queue_history(jobs, "fixture-v1")
    class Provider:
        def appraise(self, context):
            assert context["stimulus"] == "memory-backfill"
            assert not context["memory_context"]["pending_events"]
            return Appraisal(reason="Historical import", values={"initiative": 100}, wishes=[Wish(content="An obsolete reply", topic="old", kind="contact", strength=100, ttl_hours=1, completion="Send")], memory=MemoryAssessment(disclosures=[Disclosure(share_id=receipt["share_id"], topic="earlier topic", summary="Already shared on this date.")])), {"model": "deepseek-flash"}
    assert jobs.run_one(Provider())["state"] == "complete"
    assert memory.history("share")["items"][0]["semantic_state"] == "assessed"
    assert not mind.read()["desires"]
    assert memory.semantic_context()["next_review"] == before
    assert memory.queue_history(jobs, "fixture-v1")["state"] == "complete"


def test_unverified_and_archived_records_can_be_read_without_promotion(system):
    from eventmem.core.models import RecordInput
    mind, _, source, _ = system
    sid = source("hypothesis-source", "An uncertain observation")
    with mind.engine.db.connect() as conn:
        rid = mind._evidence(conn, [sid])[0]["record_id"]
    record = mind.engine.add_record(RecordInput(scope=mind.scope, kind="fact", title="Unverified hypothesis", content="It might have happened; this is not confirmed.", source_ids=[sid], evidence_ids=[rid], confirmation="inferred", generated=True), "hypothesis-test")
    result = Contexts(mind).read_record(record)
    assert "not confirmed" in result["content"]
    assert result["confirmation"] == "inferred"
    assert mind.engine.get(record["id"])["status"] == "unverified"


def test_malformed_compression_preserves_whole_evidence_and_reports_omissions(system):
    mind, _, _, _ = system
    class BadCompressor:
        def structured(self, *args, **kwargs):
            return Compression(entries=[CompressedEntry(item_ids=["invented"], summary="A fabricated confirmation")]), {}
    item = {"id": "real", "text": "A whole sentence. " * 100, "basis": "inferred"}
    result = Contexts(mind).pack([item], "example", 150, provider=BadCompressor())
    assert not result["covered_ids"] and result["omitted_ids"] == ["real"]
    assert not result["text"]


def test_record_redaction_and_state_read_fit_without_a_model(system):
    from eventmem.core.retrieval import tokens
    mind, _, _, _ = system
    secret = "sk-" + "syntheticsecret" * 3
    result = Contexts(mind).pack([{"id": "secret-fixture", "text": "api_key=" + secret}], "context", 200, allow_model=False)
    assert secret not in result["text"]
    assert tokens(__import__("json").dumps(Contexts(mind).affective(), ensure_ascii=False)) <= 2000


def test_file_version_mismatch_is_deferred_instead_of_misattributed(system, tmp_path):
    mind, memory, _, _ = system
    file = tmp_path / "changing.txt"
    file.write_text("earlier")
    fingerprint = fingerprint_file(file)
    file.write_text("later")
    with pytest.raises(Conflict):
        memory.ingest({"id": "changed", "kind": "artifact-created", "at": mind.clock(), "artifact": fingerprint})


def test_history_projection_retains_evidence_without_reappraising_current_mood(system):
    from kin_mind.appraisal import appraisal_context
    mind, _, _, _ = system
    source = {"id": "old-source", "text": "This was delivered yesterday, not today."}
    context = appraisal_context({"stimulus": "memory-backfill", "state": mind.read(),
                                 "new_evidence": [source], "definitions": {"mood": "current mood"}})
    assert context["new_evidence"] == [source]
    assert context["state"]["scope"] == mind.scope.model_dump()
    assert "dimensions" not in context["state"]
    assert not context["definitions"]


def test_compression_reports_safe_provider_failure_and_restores_timeout(system):
    mind, _, _, _ = system
    class TimeoutProvider:
        timeout = 600
        def structured(self, *args, **kwargs):
            assert self.timeout <= 150
            assert kwargs["max_tokens"] == 65536
            raise RuntimeError("deepseek-timeout")
    provider = TimeoutProvider()
    result = Contexts(mind).pack([{"id": "long", "text": "Whole evidence sentence. " * 300}], "recall", 200, provider=provider)
    assert result["reason"] == "deepseek-timeout"
    assert result["omitted_ids"] == ["long"]
    assert provider.timeout == 600


def test_semantic_links_accept_original_source_identifiers(system):
    from kin_mind.memory import MemoryNote
    mind, memory, source, _ = system
    sid = source("source-linked", "The original request and its meaning.")
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [sid])
        memory.apply_assessment(conn, MemoryAssessment(notes=[MemoryNote(key="original", title="Original account", content="A source-linked interpretation.", evidence_ids=[sid], about_ids=[sid])]), refs, "event-source-alias", 0, 20, {"model":"deepseek-flash"})
        assert memory._record_ids(conn, sid) == [refs[0]["record_id"]]


def test_explicit_history_budget_counts_the_mcp_json_envelope(system):
    import json

    from eventmem.core.retrieval import tokens
    mind, memory, _, _ = system
    for i in range(20):
        memory.ingest({"id":f"page-{i}", "kind":"delivery", "at":mind.clock(), "text":"A complete synthetic note with a stable delivery record.", "state":"accepted", "message_id":f"receipt-{i}"})
    result=Contexts(mind).read_history("share", budget=2000)
    assert tokens(json.dumps(result,ensure_ascii=False,indent=2)) <= 2000
    assert result["cursor"] is not None
