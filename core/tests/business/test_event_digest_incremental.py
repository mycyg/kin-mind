"""Growing events reuse source compression without reusing stale conclusions."""
import json

from eventmem.core.models import RevisionInput
from eventmem.core.retrieval import tokens
from kin_mind.context import Compression
from kin_mind.lifecycle import EventLifecycle, EventSummary, mark_dirty
from test_event_graph import system  # noqa: F401


def test_digest_compresses_old_material_once_and_rechecks_corrected_or_deleted_sources(system):
    mind, memory, source, _ = system
    memory.configure({"event_lifecycle": True})
    long_text = "Fictional lighthouse observations: the beam is blue and the inspection remains pending.\n" * 5000
    assert tokens(long_text) > 64000
    old = source("long-observation", long_text)
    old_record = mind.engine.source(old)["record_ids"][0]
    sources = [old]
    calls = []

    class Provider:
        timeout = 300

        def structured(self, tool, schema, system, payload, **kwargs):
            calls.append((tool, payload))
            if tool == "submit_compression":
                return Compression(entries=[{"item_ids": payload["allowed_item_ids"], "summary": "Fictional observations retained; inspection status awaits the later evidence."}], omitted_ids=[]), {"model": "synthetic", "requests": 1}
            assert tool == "submit_event_digest"
            ids = payload.get("allowed_record_ids") or [r["id"] for r in payload["records"]]
            return EventSummary(narrative=[{"text": "A sourced fictional event with current evidence.", "record_ids": ids}]), {"model": "synthetic"}

    life = EventLifecycle(mind)

    def update_members():
        with mind.engine.db.connect(write=True) as conn:
            memory.graph._put(conn, {"id": "growing-fictional-event", "kind": "thread", "title": "Fictional lighthouse", "text": "",
                "source_ids": sources[:], "evidence": memory.graph.proof(conn, sources), "basis": "documented"})
            mark_dirty(conn, mind.scope.key(), ["growing-fictional-event"], mind.clock())

    def refresh():
        calls.clear()
        apply = life.prepare_digest("growing-fictional-event", provider=Provider())
        with mind.engine.db.connect(write=True) as conn:
            apply(conn)
        return life.read("growing-fictional-event")["data"]

    update_members()
    first = refresh()
    first_calls = sum(t == "submit_compression" for t, _ in calls)
    assert first_calls > 1 and not first["preparation"]["cache_hit"]
    assert any("source_at" in json.dumps(p) for t, p in calls if t == "submit_compression")
    sources.append(source("new-result", "Later evidence: the fictional inspection succeeded; this supersedes the pending state."))
    new_record = mind.engine.source(sources[-1])["record_ids"][0]
    update_members()
    second = refresh()
    assert second["preparation"]["cache_hit"] and second["preparation"]["model_requests"] == 0
    assert [t for t, _ in calls] == ["submit_event_digest"]
    assert new_record in calls[0][1]["allowed_record_ids"] and "inspection succeeded" in calls[0][1]["sourced_context"]
    assert len(second["source_versions"]) == 2, "the final conclusion is regenerated from old and new evidence"

    mind.engine.revise(old_record, RevisionInput(expected_revision=1, command_id="correct-lighthouse", action="correct", content=long_text.replace("blue", "green"), reason="Corrected observation"))
    update_members()
    corrected = refresh()
    assert not corrected["preparation"]["cache_hit"]
    assert any(t == "submit_compression" and "green" in json.dumps(p) for t, p in calls)
    assert corrected["source_versions"][old_record] == 2

    mind.engine.delete(old)
    sources.remove(old)
    update_members()
    after_delete = refresh()
    assert old_record not in after_delete["source_versions"]
    assert all(old_record not in json.dumps(p) for _, p in calls)
