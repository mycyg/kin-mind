"""Synthetic activity autobiography: original sources, bounded reads, no fact leakage."""
import asyncio
import json

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing, dumps
from eventmem.core.models import RecallRequest, RevisionInput, Scope, SourceInput
from eventmem.core.read_policy import ReadPolicy
from kin_mind import dreams
from kin_mind.activities import (
    canonical_activity_ref,
    context_item,
    read_activity_ref,
    select_activities,
    validate_activity_ref,
)
from kin_mind.context import Contexts
from kin_mind.continuity_manifest import ContinuityManifest
from kin_mind.evidence_classes import never_evidence
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

DREAM = "我在蓝色门后看见一只银色纸鹤，屋顶像海一样轻轻摇动。"


@pytest.fixture
def env(tmp_path):
    mind = Mind(Engine(tmp_path / "db"), Scope(persona="synthetic-activity-memory"),
                clock=lambda: "2026-10-12T12:00:00+00:00")

    def source(key, text, *, namespace="kin-dream", at="2026-08-01T00:00:00+00:00", version="1", derived_from=None):
        return mind.engine.receive(SourceInput(namespace=namespace, key=key, version=version, text=text,
            scope=mind.scope, occurred_at=at, extract=False,
            authority="explicit" if namespace == "synthetic-owner" else "model",
            kind="diary" if namespace == "kin-dream" else "observation",
            metadata={"role": "user"} if namespace == "synthetic-owner" else {}), derived_from=derived_from)["id"]

    mind.initialize(agent_version="synthetic-activity-v1",
                    evidence_ids=[source("init", "setup", namespace="synthetic-owner")])
    memory = MemoryContinuity(mind)
    memory.configure({"context": True, "graph_recall": False, "dreams": False})
    return mind, memory, source


def report(source, key="explore-private", **options):
    return source(key, dumps({"state": "complete", "partial": False, "result": {
        "summary": "自己探索过纸鹤的折法", "findings": ["沿对角线折叠"],
        "sources": [{"url": "https://example.invalid/origami"}], "open_questions": ["为什么折痕会变软？"]}}),
        namespace="kin-exploration", **options)


def test_old_dreams_remain_readable_after_generation_is_disabled_and_restart(env):
    mind, memory, source = env
    sid = source("old-dream", dreams.DREAM_PREFIX + DREAM)
    memory.configure({"dreams": True})
    memory.configure({"dreams": False})
    restarted = Mind(Engine(mind.engine.db.root), mind.scope, clock=mind.clock)
    result = dreams.read(restarted)
    assert result["generation_enabled"] is False and result["state"] == "enabled"
    assert result["dreams"][0]["text"] == DREAM
    assert result["dreams"][0]["activity_ref"] == canonical_activity_ref(restarted, "dream", sid)
    assert dreams.recent(restarted) == [], "older than the recent window, still readable"


def test_dream_reads_locate_topic_id_time_and_pages_with_legacy_source_keys(env):
    mind, _, source = env
    first = source("legacy-dream-key", dreams.DREAM_PREFIX + DREAM, at="2026-08-01T00:00:00+00:00")
    second = source("later-dream", "没有前缀的旧梦：我在月亮上弹琴。", at="2026-09-02T00:00:00+00:00")
    assert dreams.read(mind, identifier=first)["dreams"][0]["source_id"] == first
    assert dreams.read(mind, identifier="legacy-dream-key")["dreams"][0]["text"] == DREAM
    assert [d["source_id"] for d in dreams.read(mind, query="银色纸鹤")["dreams"]] == [first]
    assert [d["source_id"] for d in dreams.read(mind, since="2026-09-01T00:00:00Z", until="2026-09-03T00:00:00Z")["dreams"]] == [second]
    page = dreams.read(mind, limit=1)
    assert page["dreams"][0]["source_id"] == second and page["cursor"] == 1
    assert dreams.read(mind, cursor=page["cursor"], limit=1)["dreams"][0]["source_id"] == first
    assert dreams.read(mind, query="不存在的话题")["dreams"] == []
    index = dreams.read(mind, identifier=first, detail="index")
    assert index["dreams"][0]["text"] == "" and index["dreams"][0]["activity_ref"]["kind"] == "dream"
    bounded = dreams.read(mind, identifier=first, budget=250)
    assert DREAM not in dumps(bounded) and bounded["next_action"] == "increase_budget_or_read_by_id"


def test_private_exploration_source_remains_canonical_without_graph_or_sharing(env):
    mind, _, source = env
    sid = report(source)
    ref = canonical_activity_ref(mind, "exploration", "explore-private")
    assert ref["source_id"] == sid and ref["content_class"] == "knowledge_report"
    found = read_activity_ref(mind, ref)
    assert found["result"]["sources"] and found["result"]["open_questions"]
    # No disclosure decision or graph projection is needed to remember doing it.
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_share_coverage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mind_graph_nodes WHERE kind='finding'").fetchone()[0] == 0
    assert select_activities(mind, query="折法")["activities"][0]["activity_ref"] == ref
    again = Mind(Engine(mind.engine.db.root), mind.scope, clock=mind.clock)
    assert read_activity_ref(again, ref)["result"] == found["result"]


def test_source_identity_survives_willingness_and_document_revision(env):
    mind, _, source = env
    sid = report(source)
    ref = canonical_activity_ref(mind, "exploration", "explore-private")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE mind_explorations(id TEXT PRIMARY KEY,scope TEXT,state TEXT,created_at TEXT,data TEXT)")
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                     ("explore-private", mind.scope.key(), "complete", mind.clock(), dumps({"source_id": sid, "sharing_decision": {"decision": "keep"}})))
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_explorations SET data=?", (dumps({"source_id": sid, "sharing_decision": {"decision": "share", "reason": ""}}),))
    assert validate_activity_ref(mind, ref) == ref
    replacement = report(source, version="2")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_explorations SET data=?", (dumps({"source_id": replacement}),))
    assert validate_activity_ref(mind, ref) == ref
    assert read_activity_ref(mind, ref)["superseded"] is True
    assert "content_hash" not in ref
    assert validate_activity_ref(mind, {**ref, "version": 999, "content_hash": "old-format-field"}) == ref
    latest = canonical_activity_ref(mind, "exploration", "explore-private")
    assert latest["version"] == 2 and latest["source_id"] == replacement


def test_dream_autobiography_never_enters_facts_trait_evidence_or_graph_projection(env):
    mind, _, source = env
    sid = source("old-dream", dreams.DREAM_PREFIX + DREAM)
    ref = canonical_activity_ref(mind, "dream", sid)
    item = context_item(read_activity_ref(mind, ref))
    assert item["basis"] == "imagined" and item["facts"]["evidence"] is False
    assert item["facts"]["use_as_current_fact"] is False
    with mind.engine.db.connect() as conn:
        evidence = mind._evidence(conn, [sid])[0]
        assert never_evidence(evidence)
        root = mind.engine._get(conn, evidence["record_id"])
        assert not ReadPolicy.load(mind.engine, mind.scope, "experience_recall", conn=conn).visible(root)
        assert conn.execute("SELECT COUNT(*) FROM search WHERE id=?", (root["id"],)).fetchone()[0] == 0
    result = mind.engine.recall(RecallRequest(scope=mind.scope, query="蓝色门 银色纸鹤"))
    assert sid not in dumps(result) and DREAM not in dumps(result)


def test_actual_context_build_event_thread_and_manifest_expand_activity_focus(env):
    mind, _, source = env
    sid = source("old-dream", dreams.DREAM_PREFIX + DREAM)
    ref = canonical_activity_ref(mind, "dream", sid)
    MemoryContinuity(mind).configure({"manifests":True})
    contexts = Contexts(mind)
    built = contexts.build(query="后来那个门呢", activity_focus=ref, purpose="read", budget=12000, mode="light")
    assert DREAM in built["text"] and '"basis":"imagined"' in built["text"]
    assert built["model_requests"] == 0
    assert next(i for i in built["index"] if i["id"].startswith("activity:"))["depth"] == "original"
    event = contexts.event_thread(sid, detail="original", budget=12000, expected_revision=999)
    assert DREAM in event["text"] and event["index"][0]["activity_ref"] == ref
    assert event["requested_revision"] == 999, "historical document metadata does not block a read"
    selected = ContinuityManifest(mind, contexts=contexts).select(activity_focus=ref)
    assert selected["items"][0]["facts"]["activity_ref"] == ref
    # Ordinary conversation receives the short autobiography lane, not a new fact.
    topical = contexts.build(query="银色纸鹤", budget=12000, purpose="chat", mode="light", activity_recall=True)
    assert '"content_class":"dream_imagination"' in topical["text"]


def test_source_deletion_erases_activity_reads_and_invalidates_context_dependencies(env):
    mind, _, source = env
    material = source("material", "a clock", namespace="synthetic-owner")
    sid = source("derived-dream", dreams.DREAM_PREFIX + DREAM, derived_from=[material])
    ref = canonical_activity_ref(mind, "dream", sid)
    contexts = Contexts(mind)
    item = context_item(read_activity_ref(mind, ref))
    assert contexts._current(item)
    mind.engine.delete(material)
    assert not contexts._current(item)
    with pytest.raises(Missing):
        read_activity_ref(mind, ref)
    assert dreams.read(mind)["dreams"] == []
    assert contexts.activity_items(activity_focus=ref) == []


def test_scope_boundary_stays_closed_without_a_body_hash_gate(env):
    mind, _, source = env
    sid = source("old-dream", dreams.DREAM_PREFIX + DREAM)
    ref = canonical_activity_ref(mind, "dream", sid)
    other = Mind(mind.engine, Scope(persona="another-synthetic-scope"), clock=mind.clock)
    with pytest.raises(Missing):
        read_activity_ref(other, ref)
    mind.engine.source(sid, content=True).write_text("changed body")
    entry = read_activity_ref(mind, ref)
    assert entry["text"] == "changed body"
    assert entry["basis"] == "imagined" and entry["evidence"] is False


def test_mcp_old_signature_and_host_memory_context_reach_the_original(env):
    mind, _, source = env
    sid = source("old-dream", dreams.DREAM_PREFIX + DREAM)
    from eventmem.core.mcp import create_mcp
    answer = asyncio.run(create_mcp(mind.engine).call_tool("read_dreams", {"scope": mind.scope.model_dump()}))
    shown = answer[1] if isinstance(answer, tuple) else json.loads(answer[0].text)
    assert shown["dreams"][0]["text"] == DREAM and shown["generation_enabled"] is False
    from kin_mind.host import dispatch
    cfg = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "synthetic-activity-v1"}
    built = dispatch(cfg, "memory-context", {"query": "那个门", "purpose": "chat", "budget": 12000,
        "intent": {"kind": "contact", "activity_ref": canonical_activity_ref(mind, "dream", sid)}})
    assert DREAM in built["text"] and '"basis":"imagined"' in built["text"]


@pytest.mark.parametrize("reply_id,message_ids", [("platform-old", []), ("platform-tail", ["platform-old", "platform-tail"])])
def test_platform_reply_context_supplies_original_dream_and_the_accepted_fragment(env, reply_id, message_ids):
    mind, memory, source = env
    memory.configure({"records": True, "sharing": True})
    sid = source("old-dream", dreams.DREAM_PREFIX + DREAM)
    ref = canonical_activity_ref(mind, "dream", sid)
    memory.ingest({"id": "sent-fragment", "kind": "delivery", "at": "2026-08-02T00:00:00+00:00",
        "channel": "feishu", "conversation_id": "synthetic-chat", "delivery_id": "synthetic-delivery", "bubble_id": "bubble-1",
        "state": "accepted", "message_id": "platform-old", "message_ids": message_ids, "text": "梦里有一只银色纸鹤。",
        "references": [{"activity_ref": ref}]})
    route = {"channel": "feishu", "conversation_id": "synthetic-chat", "reply_to_message_id": reply_id}
    contexts = Contexts(mind)
    answer = contexts.build(query="那个门后来呢", reply_context=route, purpose="read", budget=12000, mode="light")
    assert answer["reply_context"]["state"] == "resolved"
    assert DREAM in answer["text"] and "梦里有一只银色纸鹤。" in answer["text"]
    assert answer["reply_context"]["candidates"][0]["activity_refs"] == [ref]
    other = contexts.build(reply_context={**route, "conversation_id": "another-chat"}, purpose="read", budget=12000, mode="light")
    assert other["reply_context"]["state"] == "unresolved" and DREAM not in other["text"]
    other_channel = contexts.build(reply_context={**route, "channel": "wechat"}, purpose="read", budget=12000, mode="light")
    assert other_channel["reply_context"]["state"] == "unresolved" and DREAM not in other_channel["text"]
    mind.engine.delete(sid)
    after = contexts.build(reply_context=route, purpose="read", budget=12000, mode="light")
    assert DREAM not in after["text"]
    assert all(not candidate["activity_refs"] for candidate in after["reply_context"]["candidates"])


def test_ambiguous_platform_reference_keeps_candidates_without_selecting_an_activity(env):
    mind, memory, source = env
    memory.configure({"records": True, "sharing": True})
    ids = [source("one", dreams.DREAM_PREFIX + "我梦见蓝色门。"), source("two", dreams.DREAM_PREFIX + "我梦见绿色窗。")]
    for n, sid in enumerate(ids):
        memory.ingest({"id": "ambiguous-" + str(n), "kind": "delivery", "at": "2026-08-02T00:00:00+00:00",
            "channel": "feishu", "conversation_id": "synthetic-chat", "delivery_id": "delivery-" + str(n), "bubble_id": "bubble-" + str(n),
            "state": "accepted", "message_id": "same-platform-id", "text": "讲过一段梦。",
            "references": [{"activity_ref": canonical_activity_ref(mind, "dream", sid)}]})
    contexts = Contexts(mind)
    route = {"channel": "feishu", "conversation_id": "synthetic-chat", "reply_to_message_id": "same-platform-id"}
    resolved, shared, focused = contexts.reply_activity_context(route)
    assert resolved["state"] == "ambiguous" and len(resolved["candidates"]) == 2
    assert shared == focused == []
    built = contexts.build(reply_context=route, purpose="read", budget=12000, mode="light")
    assert "我梦见蓝色门。" not in built["text"] and "我梦见绿色窗。" not in built["text"]


def test_interrupted_exploration_reads_only_its_actual_checkpoint(env):
    mind, _, source = env
    sid = source("interrupted", dumps({"state": "preempted", "partial": True, "result": None}), namespace="kin-exploration")
    checkpoint = {"partial_findings": ["只看了纸鹤的一条折痕"], "gaps": ["其余步骤没有检查"], "attempt": 1}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE mind_explorations(id TEXT PRIMARY KEY,scope TEXT,state TEXT,created_at TEXT,data TEXT)")
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                     ("interrupted", mind.scope.key(), "preempted", mind.clock(), dumps({"source_id": sid, "checkpoint": checkpoint, "selected_brief": "研究纸鹤的折法"})))
    assert select_activities(mind, query="纸鹤的折法")["activities"][0]["activity_ref"]["source_id"] == sid
    ref = canonical_activity_ref(mind, "exploration", "interrupted")
    entry = read_activity_ref(mind, ref)
    assert entry["result"] == {} and entry["partial"] is True and entry["checkpoint"] == checkpoint
    item = context_item(entry)
    assert "其余步骤没有检查" in item["text"] and Contexts(mind)._current(item)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_explorations SET data=?", (dumps({"source_id": sid, "checkpoint": {"gaps": ["修订后的实际缺口"]}}),))
    assert Contexts(mind)._current(item), "an earlier shown checkpoint remains its historical account"
    assert "digest" not in item["checkpoint_dependency"]
    assert validate_activity_ref(mind, ref) == ref
    mind.engine.delete(sid)
    with pytest.raises(Missing):
        read_activity_ref(mind, ref)
    assert Contexts(mind).activity_items(activity_focus=ref) == []


def test_host_sharing_preflight_reserves_only_at_the_send_boundary(env):
    mind, _, source = env
    sid = source("old-dream", dreams.DREAM_PREFIX + DREAM)
    ref = canonical_activity_ref(mind, "dream", sid)
    from kin_mind.host import RESIDENT_ACTIONS, dispatch
    cfg = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "synthetic-activity-v1"}
    request = {"draft_id": "synthetic-draft", "references": [{"activity_ref": ref}]}
    assert "sharing-preflight" in RESIDENT_ACTIONS and "attention" in RESIDENT_ACTIONS
    assert dispatch(cfg, "sharing-preflight", {**request, "dry_run": True})["state"] == "ready"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_share_reservations").fetchone()[0] == 0
    assert dispatch(cfg, "sharing-preflight", request)["state"] == "ready"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_share_reservations").fetchone()[0] == 1
    assert dispatch(cfg, "sharing-preflight", {**request, "draft_id": "other-channel-draft"})["state"] == "ready"


def test_revised_dream_keeps_original_ids_and_marks_history_without_a_content_gate(env):
    mind, memory, source = env
    original = source("revised-dream", dreams.DREAM_PREFIX + DREAM)
    old = canonical_activity_ref(mind, "dream", original)
    revised_text = "我梦见纸鹤停在绿色窗前，后来变成了一盏灯。"
    revised = source("revised-dream", dreams.DREAM_PREFIX + revised_text, version="2")
    current = canonical_activity_ref(mind, "dream", "revised-dream")
    assert current["source_id"] == current["id"] == revised and current["version"] == 2
    assert "content_hash" not in current and "content_hash" not in old
    assert canonical_activity_ref(mind, "dream", original) == old, "the original ID keeps its identity"
    assert validate_activity_ref(mind, old) == old
    assert read_activity_ref(mind, old)["text"] == DREAM
    historical = read_activity_ref(mind, old, historical=True)
    assert historical["text"] == DREAM and historical["superseded"] is True
    assert Contexts(mind)._current(context_item(historical))
    from kin_mind.exploration_decisions import _decision_fresh
    with mind.engine.db.connect() as conn:
        assert _decision_fresh(mind, conn, {"activity_ref": old, "evidence": []})
        assert _decision_fresh(mind, conn, {"activity_ref": current, "evidence": []})
    kept = dreams.read(mind, identifier=original)["dreams"]
    assert len(kept) == 1 and kept[0]["text"] == DREAM and kept[0]["superseded"] is True
    for method in (memory.sharing.preview, memory.sharing.prepare):
        assert method({"draft_id": "new-share", "references": [{"activity_ref": old}]})["state"] == "ready"
    assert memory.sharing.preview({"draft_id": "current-share", "references": [{"activity_ref": current}]})["state"] == "ready"
    assert validate_activity_ref(mind, current) == current


def test_source_id_cannot_be_rebound_by_a_new_dream_using_it_as_a_source_key(env):
    mind, _, source = env
    original = source("immutable-dream-id", dreams.DREAM_PREFIX + DREAM)
    ref = canonical_activity_ref(mind, "dream", original)
    collision = source(original, dreams.DREAM_PREFIX + "另一个梦的正文。")
    assert collision != original
    assert canonical_activity_ref(mind, "dream", original) == ref
    assert read_activity_ref(mind, ref)["text"] == DREAM
    assert [entry["source_id"] for entry in dreams.read(mind, identifier=original)["dreams"]] == [original]
    mind.engine.delete(original)
    with pytest.raises(Missing):
        read_activity_ref(mind, ref, historical=True)
    with pytest.raises(Missing):
        canonical_activity_ref(mind, "dream", original)
    assert dreams.read(mind, identifier=original)["dreams"] == []


def test_subjective_history_survives_source_correction_while_fact_reads_use_current_records(env):
    mind, _, source = env
    sid = source("material-for-curiosity", "纸鹤的折法是一段新的话题。", namespace="synthetic-owner")
    contexts = Contexts(mind)
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [sid])
        state = mind._load(conn)
        state.setdefault('concerns', {})['synthetic-curiosity'] = {'id': 'synthetic-curiosity', 'status': 'active', 'kind': 'care',
            'content': '我仍然好奇纸鹤的折法。', 'topic': '纸鹤', 'basis': 'inferred', 'evidence': refs, 'intensity': 50}
        mind._save(conn, state)
    original = mind.engine.get(refs[0]['record_id'])
    fact_item = contexts.record_item(original)
    subjective = {'id': 'synthetic-curiosity', 'revision': 1, 'text': '我仍然好奇纸鹤的折法。', 'basis': 'inferred',
        'state_dependency': {'kind': 'concerns', 'id': 'synthetic-curiosity'},
        'dependencies': [{'id': original['id'], 'revision': original['revision']}], 'source_ids': [sid]}
    mind.engine.revise(original['id'], RevisionInput(expected_revision=original['revision'], action='correct',
        command_id='synthetic-material-correction', content='话题其实是纸灯的折法。', reason='更正记录'))
    assert contexts._current(subjective), 'source revision is not a psychological consistency gate'
    assert not contexts._current(fact_item), 'an old factual body must not be presented as a current fact'
    selected = ContinuityManifest(mind, contexts=contexts).select('纸鹤')
    assert any(item['id'] == 'synthetic-curiosity' for item in selected['items'])
    mind.engine.delete(sid)
    assert not contexts._current(subjective)
    assert not any(item['id'] == 'synthetic-curiosity' for item in ContinuityManifest(mind, contexts=contexts).select('纸鹤')['items'])
