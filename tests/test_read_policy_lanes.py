"""The context builder and the adaptive recall lanes obey the same read policy as core recall:
what is not experience never reaches a chat read or the ranking model, and an audit read gets
it labelled with its class, never as `explicit`. Synthetic data only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import kin_mind.adaptive_recall as recall_module
from eventmem.core import Engine
from eventmem.core.mcp import create_mcp
from eventmem.core.models import Scope, SourceInput
from eventmem.core.providers import Providers
from eventmem.core.read_policy import ReadPolicy
from eventmem.core.self_knowledge import ClaimInput, SelfKnowledge
from kin_mind.adaptive_recall import AdaptiveRecall, RecallFollowup, RecallRanking
from kin_mind.context import Contexts
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

ENVELOPE = "内部探索选题事件，宿主要求挑选灯笼集市选题，不发送消息。"
EXAMPLE = "示例对话：去年冬天我们一起看过灯笼集市的灯船。"
CONFIGURED = "角色设定原文：谈到灯笼集市时语气要温暖。"
LIVED = "今天我们把灯笼集市的骨架修好了。"
# The owner-word lanes only ever return owner-role turns, and the one thing that keeps such a
# turn out of an experience read is the envelope text. A configuration flag only labels it.
CLASS_OF = {ENVELOPE: "host_envelope"}
LABELLED = (EXAMPLE, CONFIGURED)


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 16, 5, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "memory")
    mind = Mind(engine, Scope(persona="synthetic-lanes"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)

    def source(key, text, authority="explicit", **metadata):
        received = engine.receive(SourceInput(namespace="synthetic", key=key, text=text, scope=mind.scope,
            authority=authority, occurred_at=mind.clock(), extract=False, metadata=metadata))
        clock[0] += timedelta(minutes=1)
        return next(rid for rid in engine.source(received["id"])["record_ids"] if not engine.get(rid)["evidence_ids"])

    initial = engine.receive(SourceInput(namespace="synthetic", key="initial", text="Synthetic lanes fixture",
        scope=mind.scope, occurred_at=mind.clock(), extract=False))
    mind.initialize(agent_version="fixture", evidence_ids=[initial["id"]])
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True, "graph_recall": True,
                      "sharing": True, "event_lifecycle": True, "adaptive_recall": True})
    return mind, source


def owner_turns(source):
    """Owner-role turns of one afternoon: one lived, one a host envelope stored as an owner
    turn, and two the owner said about a configuration, which a flag only labels."""
    ids = {LIVED: source("lived", LIVED, role="user", host_event="message")}
    ids[ENVELOPE] = source("envelope", ENVELOPE, role="user", host_event="message")
    ids[EXAMPLE] = source("example", EXAMPLE, role="user", host_event="message", examples_are_synthetic=True)
    ids[CONFIGURED] = source("configured", CONFIGURED, role="user", host_event="message", configuration_only=True)
    return ids


def make_legacy(engine):
    with engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM source_evidence_class")
        conn.execute("UPDATE records SET data=json_remove(data,'$.attributes.origin_kind')")
        conn.execute("UPDATE revisions SET data=json_remove(data,'$.attributes.origin_kind')")
        engine.db.bump(conn)


def assert_lane(collect, ids):
    """`collect(**options)` runs one lane in isolation and returns its items."""
    for history in (False, True):
        items = {i["id"]: i for i in collect(history=history)}
        assert ids[LIVED] in items and items[ids[LIVED]]["basis"] == "explicit"
        assert all(ids[text] not in items for text in CLASS_OF), history
        # What the owner said about a configuration is still an owner turn, and says so.
        for text in LABELLED:
            assert items[ids[text]]["basis"] == "explicit", (text, history)
            assert items[ids[text]]["facts"]["evidence_label"] == "configuration_request"
    audit = {i["id"]: i for i in collect(history=True, recall_purpose="audit")}
    for text, kind in CLASS_OF.items():
        assert audit[ids[text]]["basis"] == kind and audit[ids[text]]["text"] == text
    assert audit[ids[LIVED]]["basis"] == "explicit"


def test_dated_lane_obeys_the_policy(system, monkeypatch):
    mind, source = system
    ids = owner_turns(source)
    make_legacy(mind.engine)
    # No lexical candidates: whatever arrives came through the date window.
    monkeypatch.setattr(recall_module, "candidates", lambda *a, **k: ([], {}, 0))
    assert_lane(lambda **options: AdaptiveRecall(Contexts(mind)).collect("2026年9月16日发生了什么", mode="light", **options)[0], ids)


def test_lexical_candidates_are_rechecked_before_they_become_items(system, monkeypatch):
    mind, source = system
    ids = owner_turns(source)
    make_legacy(mind.engine)
    # A lexical result that slipped through (a warm cache, another host's write) is judged again.
    monkeypatch.setattr(recall_module, "candidates", lambda *a, **k: ([mind.engine.get(i) for i in ids.values()], {}, 0))
    assert_lane(lambda **options: AdaptiveRecall(Contexts(mind)).collect("骨架", mode="light", **options)[0], ids)


def test_owner_originals_lane_obeys_the_policy(system, monkeypatch):
    mind, source = system
    ids = owner_turns(source)
    make_legacy(mind.engine)
    monkeypatch.setattr(recall_module, "candidates", lambda *a, **k: ([], {}, 0))
    assert_lane(lambda **options: AdaptiveRecall(Contexts(mind)).collect("我说过灯笼集市的事吗", mode="light", **options)[0], ids)


def test_neighbour_lane_obeys_the_policy(system, monkeypatch):
    mind, source = system
    anchor = source("anchor", "色卡先保留蓝色。", role="user", host_event="message")
    ids = owner_turns(source)
    make_legacy(mind.engine)
    monkeypatch.setattr(recall_module, "candidates", lambda *a, **k: ([mind.engine.get(anchor)], {}, 0))
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    rounds = []

    class Ranker:
        timeout = 30

        def structured(self, name, schema, prompt, payload, **kwargs):
            rounds.append([c["text"] for c in payload["candidates"]])
            ordered = sorted(payload["candidates"], key=lambda c: "色卡" not in c["text"])
            return RecallRanking(ids=[c["id"] for c in ordered[:8]],
                                 followups=[RecallFollowup(candidate_id=ordered[0]["id"], direction="after")]), {}

    def collect(**options):
        del rounds[:]
        # The question shares no word with the later turns: only the follow-up can reach them.
        return AdaptiveRecall(Contexts(mind)).collect("色卡那句后续找完整", mode="deep", allow_model=True,
                                                     provider=Ranker(), **options)[0]

    for history in (False, True):
        items = {i["id"] for i in collect(history=history)}
        assert LIVED not in rounds[0] and LIVED in rounds[1]
        assert {anchor, ids[LIVED], *(ids[text] for text in LABELLED)} <= items
        assert all(ids[text] not in items for text in CLASS_OF)
        # The ranking model is a reader too: it is never shown what is not experience.
        assert not any(text in shown for shown in rounds for text in CLASS_OF)
    audit = {i["id"]: i for i in collect(history=True, recall_purpose="audit")}
    assert not any(text in rounds[0] for text in CLASS_OF) and all(text in rounds[1] for text in CLASS_OF)
    assert all(audit[ids[text]]["basis"] == kind for text, kind in CLASS_OF.items())


def test_vector_lane_obeys_the_policy(system, monkeypatch):
    pytest.importorskip("lancedb")
    from eventmem.core.vectors import VectorIndex

    mind, source = system
    ids = owner_turns(source)
    make_legacy(mind.engine)
    index_id = VectorIndex.register(mind.engine, "synthetic-vector-model", 4, "test-v1")
    VectorIndex(mind.engine, index_id).upsert([
        {"id": rid, "scope": mind.scope.key(), "revision": 1, "vector": [1.0, 0.0, 0.0, n / 10]}
        for n, rid in enumerate(ids.values())])
    monkeypatch.setattr(recall_module, "candidates", lambda *a, **k: ([], {}, 0))
    monkeypatch.setattr(Providers, "embed", lambda self, texts: ([[1.0, 0.0, 0.0, 0.0]], index_id))
    # No word of the question is in any turn: only the vector lane can reach them.
    assert_lane(lambda **options: AdaptiveRecall(Contexts(mind)).collect("zeppelin", mode="deep", **options)[0], ids)


def test_light_context_path_obeys_the_policy(system):
    mind, source = system
    owner_turns(source)
    make_legacy(mind.engine)
    contexts = Contexts(mind)
    for history in (False, True):
        built = contexts.build("灯笼集市", purpose="read", mode="light", history=history, budget=8000)
        shown = {line["text"]: line for line in map(json.loads, built["text"].splitlines())}
        assert LIVED in shown and "recall_purpose" not in built
        assert not any(text in shown for text in CLASS_OF)
        assert all(shown[text]["basis"] == "explicit" for text in LABELLED)
    audit = contexts.build("灯笼集市", purpose="read", mode="light", history=True, budget=8000, recall_purpose="audit")
    lines = {line["text"]: line for line in map(json.loads, audit["text"].splitlines())}
    assert all(lines[text]["basis"] == kind for text, kind in CLASS_OF.items())
    assert lines[LIVED]["basis"] == "explicit" and audit["recall_purpose"] == "audit"
    with pytest.raises(ValueError):
        contexts.build("灯笼集市", purpose="read", mode="light", recall_purpose="everything")


def test_generic_recall_forwards_the_purpose_to_the_context_builder(system):
    from eventmem.core.models import RecallRequest

    mind, source = system
    ids = owner_turns(source)
    request = {"scope": mind.scope, "query": "灯笼集市", "mode": "fast", "phase": "read", "history": True, "budget": 8000}
    assert not any(text in mind.engine.recall(RecallRequest(**request))["text"] for text in CLASS_OF)
    audit = mind.engine.recall(RecallRequest(**request, recall_purpose="audit"))
    assert all(text in audit["text"] for text in CLASS_OF) and ids[LIVED] in json.dumps(audit["items"])


def test_host_memory_context_is_where_an_audit_read_is_asked(system):
    from kin_mind.host import dispatch

    mind, source = system
    owner_turns(source)
    make_legacy(mind.engine)
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "fixture",
              "session_id": "synthetic-session"}
    request = {"query": "灯笼集市", "purpose": "read", "mode": "light", "history": True, "budget": 8000}
    default = dispatch(config, "memory-context", dict(request))
    assert LIVED in default["text"] and not any(text in default["text"] for text in CLASS_OF)
    audit = dispatch(config, "memory-context", {**request, "recall_purpose": "audit"})
    assert all(text in audit["text"] for text in CLASS_OF) and audit["recall_purpose"] == "audit"


def claim(mind, evidence):
    return SelfKnowledge(mind.engine, mind.scope).claim(ClaimInput(
        command_id="role-claim", aspect="voice", context="chat", agent_version="fixture", basis="role",
        claim="谈到灯笼集市时我的语气是温暖的。", evidence_ids=[evidence]))


@pytest.mark.asyncio
async def test_mcp_read_continuity_context_with_history_stays_experience(system, monkeypatch):
    from kin_mind.appraisal import DeepSeek

    mind, source = system
    ids = owner_turns(source)
    role = claim(mind, ids[LIVED])
    make_legacy(mind.engine)
    shown = []

    class Ranker:
        timeout = 30

        def structured(self, name, schema, prompt, payload, **kwargs):
            shown.extend(c["text"] for c in payload["candidates"])
            return RecallRanking(ids=[c["id"] for c in payload["candidates"][:8]]), {}

    monkeypatch.setattr(DeepSeek, "from_engine", classmethod(lambda cls, engine: Ranker()))
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    server = create_mcp(mind.engine)
    result = await server.call_tool("read_continuity_context", {"scope": mind.scope.model_dump(), "query": "灯笼集市", "history": True, "budget": 8000})
    built = json.loads(result[0].text)
    assert built["mode_used"] == "deep" and LIVED in built["text"] and LIVED in shown
    for text in [*CLASS_OF, role["content"]]:
        assert text not in built["text"] and text not in shown
    assert role["id"] not in json.dumps(built, ensure_ascii=False)
    # The host, not a chat tool, can ask for the audit view; the claim then carries its class.
    audit = Contexts(mind).build("灯笼集市", purpose="read", history=True, mode="light", budget=8000, recall_purpose="audit")
    lines = {line["id"]: line for line in map(json.loads, audit["text"].splitlines())}
    assert lines[role["id"]]["basis"] == "self_knowledge"
    view = Contexts(mind).build("灯笼集市", purpose="read", history=True, mode="light", budget=8000, recall_purpose="self_knowledge_view")
    lines = {line["id"]: line for line in map(json.loads, view["text"].splitlines())}
    assert lines[role["id"]]["basis"] == "self_knowledge" and ids[ENVELOPE] not in lines


@pytest.mark.asyncio
async def test_read_memory_and_record_items_label_what_is_not_experience(system):
    mind, source = system
    ids = owner_turns(source)
    role = claim(mind, ids[LIVED])
    make_legacy(mind.engine)
    contexts = Contexts(mind)
    assert contexts.record_item(mind.engine.get(role["id"]))["basis"] == "self_knowledge"
    assert contexts.record_item(mind.engine.get(ids[LIVED]))["basis"] == "explicit"
    server = create_mcp(mind.engine)
    for rid, kind in [(role["id"], "self_knowledge"), *[(ids[text], kind) for text, kind in CLASS_OF.items()]]:
        read = json.loads((await server.call_tool("read_memory", {"record_id": rid}))[0].text)
        assert read["confirmation"] == read["evidence_class"] == kind
        assert json.loads(read["content"])["basis"] == kind and "explicit" not in read["content"]
    lived = json.loads((await server.call_tool("read_memory", {"record_id": ids[LIVED]}))[0].text)
    assert lived["confirmation"] == "explicit" and "evidence_class" not in lived
    for text in LABELLED:
        asked = json.loads((await server.call_tool("read_memory", {"record_id": ids[text]}))[0].text)
        assert asked["confirmation"] == "explicit" and "evidence_class" not in asked
        assert asked["evidence_label"] == "configuration_request"
        assert contexts.record_item(mind.engine.get(ids[text]))["facts"]["evidence_label"] == "configuration_request"


def test_configuration_request_keeps_its_basis_and_gains_a_label(system):
    from eventmem.core.read_policy import configure_registry

    mind, source = system
    configure_registry(mind.engine, {"synthetic": "role_configuration"})
    asked = source("asked", "请在谈到灯笼集市时语气温暖一些。", role="user", host_event="configuration-request")
    installed = source("installed", "已写入角色设定：谈到灯笼集市时语气温暖。", authority="operation", role="host")
    items = {i["id"]: i for i in AdaptiveRecall(Contexts(mind)).collect("灯笼集市", mode="light", history=True)[0]}
    assert items[asked]["basis"] == "explicit" and items[asked]["facts"]["evidence_label"] == "configuration_request"
    assert installed not in items
    policy = ReadPolicy.load(mind.engine, mind.scope)
    assert policy.label(mind.engine.get(installed)) == "role_configuration"


def test_switch_off_keeps_the_prefix_filter_and_the_old_labels(system, monkeypatch):
    mind, source = system
    ids = owner_turns(source)
    role = claim(mind, ids[LIVED])
    memory = MemoryContinuity(mind)
    assert memory.settings()["recall_purpose_policy"] is True  # registered, default on
    with pytest.raises(ValueError):
        memory.configure({"recall_purpose_policy": "off"})
    memory.configure({"recall_purpose_policy": False})
    monkeypatch.setattr(recall_module, "candidates", lambda *a, **k: ([], {}, 0))
    for purpose in ("experience_recall", "audit"):
        items = {i["id"]: i for i in AdaptiveRecall(Contexts(mind)).collect(
            "我说过灯笼集市的事吗", mode="light", history=True, recall_purpose=purpose)[0]}
        # Before: envelopes were dropped by prefix, everything else was an owner turn.
        assert ids[ENVELOPE] not in items and {ids[LIVED], ids[EXAMPLE], ids[CONFIGURED]} <= set(items)
        assert all(i["basis"] == "explicit" for i in items.values())
    assert Contexts(mind).record_item(mind.engine.get(role["id"]))["basis"] == "explicit"
