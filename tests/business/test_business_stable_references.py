"""Business relations use database IDs and receipts, without content signatures."""
import json

import pytest

from eventmem.core.db import Missing
from kin_mind.lifecycle import EventLifecycle, EventRoute
from test_event_graph import system  # noqa: F401


def event(system):
    mind, memory, source, _ = system
    memory.configure({"event_lifecycle": True})
    sid = source("business-source", "A synthetic observed lamp is blue.")
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [sid])
        value = memory.graph._put(conn, {"id": "observed-business-event", "kind": "event", "title": "The synthetic lamp",
            "text": "", "source_ids": [sid], "evidence": refs, "basis": "documented"})
    return EventLifecycle(mind), value, sid


def test_event_projection_has_no_content_signature_and_ignores_legacy_hash_metadata(system):
    mind, _, _, _ = system
    life, value, sid = event(system)
    with mind.engine.db.connect() as conn:
        assert "input_hash" not in life.snapshot(conn, value["id"])
    apply = life.prepare_digest(value["id"])
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET hash='legacy-unused-value' WHERE id=?", (sid,))
        apply(conn)
        assert conn.execute("SELECT input_hash FROM mind_event_digests WHERE event_id=?", (value["id"],)).fetchone()[0] == ""
    result = life.read(value["id"])
    assert result["state"] == "ready" and "input_hash" not in result
    assert result["data"]["narrative"][0]["text"] == "A synthetic observed lamp is blue."


def test_prepared_summary_cannot_resurrect_a_deleted_source(system):
    mind, _, _, _ = system
    life, value, sid = event(system)
    apply = life.prepare_digest(value["id"])
    mind.engine.delete(sid)
    with mind.engine.db.connect(write=True) as conn:
        with pytest.raises(Missing, match="deleted"):
            apply(conn)


def test_route_replay_returns_its_original_command_receipt(system):
    mind, memory, source, _ = system
    memory.configure({"event_lifecycle": True})
    sid = source("route-original", "An observed synthetic event.")
    life = EventLifecycle(mind)
    original = EventRoute(key="stable-route", action="create", title="Original title", evidence_ids=[sid], reason="Original observation")
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [sid])
        first = life.apply_routes(conn, [original], refs, "same-actual-command")
        second = life.apply_routes(conn, [original.model_copy(update={"title": "Different title", "reason": ""})], refs, "same-actual-command")
        assert second == first
        assert conn.execute("SELECT digest FROM mind_event_routes").fetchone()[0] == ""
        saved = memory.graph.get(conn, first[0]["event_id"])
        assert saved["title"] == "Original title"


def test_graph_retry_uses_its_command_id_and_actual_receipt(system):
    mind, memory, _, _ = system
    _, value, sid = event(system)
    request = {"command_id": "actual-correction", "action": "correct", "id": value["id"], "expected_revision": value["revision"],
               "evidence_ids": [sid], "changes": {"title": "Corrected title"}, "reason": "Actual sourced correction"}
    first = memory.graph.revise(request)
    assert memory.graph.revise({**request, "expected_revision": 987, "changes": {"title": "New words"}}) == first
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT digest FROM mind_graph_commands WHERE id=?", (request["command_id"],)).fetchone()[0] == ""
        assert json.loads(conn.execute("SELECT data FROM mind_graph_nodes WHERE id=?", (value["id"],)).fetchone()[0])["title"] == "Corrected title"
