import json
from datetime import datetime, timedelta, timezone

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from kin_mind.context import Contexts
from kin_mind.adaptive_recall import AdaptiveRecall
from kin_mind.graph import GraphAssessment, GraphNode
from kin_mind.memory import MemoryContinuity
from kin_mind.recall_facts import elapsed_hours
from kin_mind.state import Mind


def system(tmp_path):
    clock = [datetime(2026, 6, 12, 2, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, Scope(persona="synthetic-time"), clock=lambda: clock[0].isoformat())
    memory = MemoryContinuity(mind)
    sid = engine.receive(SourceInput(namespace="synthetic", key="old-request", text="A requested a fictional diagram; completion is unknown.",
        scope=mind.scope, authority="explicit", occurred_at="2026-06-09T10:00:00+08:00", extract=False,
        metadata={"speaker_person_id": "person:a", "recipient_person_id": "agent:kin", "subject_ids": ["person:b"],
                  "platform_message_id": "private-platform-id", "conversation_key": "private-routing"}))["id"]
    mind.initialize(agent_version="fixture", evidence_ids=[sid])
    memory.configure({"records": True, "graph": True, "graph_recall": True, "context": True})
    record = engine.get(engine.source(sid)["record_ids"][0])
    return mind, memory, Contexts(mind), sid, record, clock


def test_recall_distinguishes_old_source_time_people_and_recording_time(tmp_path):
    mind, memory, contexts, sid, record, clock = system(tmp_path)
    item = contexts.record_item(record)
    facts = json.loads(contexts._line(item))["facts"]
    origin = facts["sources"][0]
    assert origin["source_age_hours"] == 72
    assert datetime.fromisoformat(origin["source_at"]) == datetime.fromisoformat("2026-06-09T10:00:00+08:00")
    assert origin["recorded_at"] == mind.engine.source(sid)["received_at"]
    assert [origin[k] for k in ("speaker_person_id", "recipient_person_id", "subject_ids")] == ["person:a", "agent:kin", ["person:b"]]
    assert facts["state_basis"] == "memory-record-validity"
    assert "event_at" not in facts, "a source timestamp does not date the story inside it"
    assert "private-platform-id" not in json.dumps(facts) and "private-routing" not in json.dumps(facts)
    clock[0] += timedelta(hours=24)
    assert contexts.record_item(record)["facts"]["sources"][0]["source_age_hours"] == 96
    assert mind.engine.get(record["id"])["revision"] == record["revision"], "read-time age does not rewrite history"


def test_compact_recall_keeps_later_correction_and_resolution(tmp_path):
    mind, memory, contexts, sid, record, clock = system(tmp_path)
    graph = memory.graph
    with mind.engine.db.connect(write=True) as conn:
        refs = graph.proof(conn, [sid])
        old = graph._put(conn, {"id": "old", "kind": "event", "title": "Original diagram request", "text": "Diagram requested", "source_ids": [sid], "evidence": refs})
        for i in range(10):
            other = graph._put(conn, {"id": f"other-{i}", "kind": "event", "title": "Related event", "source_ids": [sid], "evidence": refs})
            graph.link(conn, old["id"], "related", other["id"], refs)
        correction = graph._put(conn, {"id": "correction", "kind": "event", "title": "Later correction", "text": "The request belongs to B", "source_ids": [sid], "evidence": refs})
        graph.link(conn, correction["id"], "corrects", old["id"], refs, reason="Later evidence assigns this request to B", valid_from=mind.clock())
        resolution = graph._put(conn, {"id": "resolution", "kind": "event", "title": "Completion receipt", "source_ids": [sid], "evidence": refs})
        graph.link(conn, resolution["id"], "resolves", old["id"], refs, reason="The requested diagram was accepted", valid_from=mind.clock())
    facts = contexts.graph_item(old, compact=True)["facts"]
    assert {e["predicate"] for e in facts["corrections_and_resolutions"]} == {"corrects", "resolves"}
    assert next(e for e in facts["corrections_and_resolutions"] if e["predicate"] == "corrects")["subject"] == "correction"
    assert facts["state"] == "active" and facts["state_basis"] == "memory-node-validity"
    assert facts["time"]["event_at"] is None, "legacy graph insertion time is not a known event date"


def test_explicit_event_date_survives_later_summary_and_unknown_stays_unknown(tmp_path):
    mind, memory, contexts, sid, record, clock = system(tmp_path)
    graph = memory.graph
    with mind.engine.db.connect(write=True) as conn:
        receipt = graph.apply(conn, GraphAssessment(nodes=[
            GraphNode(key="dated", kind="event", title="Dated fictional request", evidence_ids=[sid], occurred_at="2026-06-09T02:00:00+00:00"),
            GraphNode(key="undated", kind="event", title="Undated fictional story", evidence_ids=[sid])]),
            graph.proof(conn, [sid]), "synthetic-assessment", {"model": "synthetic"})
        nodes = [graph.get(conn, nid) for nid in receipt["node_ids"]]
    times = {n["title"]: contexts.graph_item(n, compact=True)["facts"]["time"] for n in nodes}
    assert times["Dated fictional request"]["elapsed_hours"] == 72
    assert times["Undated fictional story"]["event_at"] is None
    assert elapsed_hours(None, mind.clock()) is None
    assert elapsed_hours("2026-06-13T02:00:00Z", mind.clock()) == -24, "a future date is not silently made old"


def test_runtime_event_retains_event_time_and_person_role(tmp_path):
    mind, memory, contexts, sid, record, clock = system(tmp_path)
    result = memory.ingest({"id": "old-host-event", "kind": "owner-message", "at": "2026-06-09T02:00:00+00:00", "text": "Fictional diagram request", "task_id": "diagram"})
    with mind.engine.db.connect() as conn:
        node = memory.graph.get(conn, result["event_id"])
    facts = contexts.graph_item(node, compact=True)["facts"]
    assert facts["time"]["basis"] == "host-event" and facts["time"]["elapsed_hours"] == 72
    assert any(r["role"] == "requester" for r in facts["roles"])
    before_revision = contexts.graph_item(node, compact=True)["revision"]
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [sid])
        later = memory.graph._put(conn, {"id": "later-correction", "kind": "event", "title": "Diagram correction", "source_ids": [sid], "evidence": refs})
        memory.graph.link(conn, later["id"], "corrects", node["id"], refs, reason="The fictional diagram is for B")
    assert contexts.graph_item(node, compact=True)["revision"] != before_revision, "a previously seen original must surface its new correction"
    recalled, _ = AdaptiveRecall(contexts).collect("Fictional diagram request", mode="light", allow_model=False)
    assert any(i["id"] == node["id"] and i["facts"].get("corrections_and_resolutions") for i in recalled), "original-message dedup must not discard later corrections"
