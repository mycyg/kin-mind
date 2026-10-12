"""Synthetic activity willingness, original pointers and real delivery receipts."""
# Imported pytest fixture names intentionally also appear as test arguments.
# ruff: noqa: F811
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_erasure import settle, texts_everywhere
from test_event_graph import findings, system  # noqa: F401

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import SourceInput
from kin_mind.activities import canonical_activity_ref, read_activity_ref
from kin_mind.dialogue import recent_dialogue
from kin_mind.exploration_decisions import SharingDecision, apply_decisions
from kin_mind.graph import GraphAssessment, GraphNode
from kin_mind.sharing import (
    ContentReference,
    CoverageAssessment,
    CoverageMapping,
)
from kin_mind.state import AffectiveEvent, DesireChange


def activity(system, kind="dream", identifier="imagined-door"):
    mind, _, _, _ = system
    text = "Kin 的梦（想象出来的，不是发生过的事）：\n梦里一道蓝门，门后是一片海。"
    result = {"summary": "看过合成海图", "findings": ["合成海图有一扇蓝门。"], "sources": [], "open_questions": ["蓝门是什么？"]}
    sid = mind.engine.receive(SourceInput(namespace="kin-dream" if kind == "dream" else "kin-exploration",
        key=identifier, scope=mind.scope, authority="model", text=text if kind == "dream" else dumps({"state": "complete", "result": result, "partial": False}),
        occurred_at=mind.clock(), extract=False, metadata={"host_event": "dream" if kind == "dream" else "exploration-result"}))['id']
    if kind == "exploration":
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)")
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (identifier, mind.scope.key(), "complete", mind.clock(), dumps({"source_id": sid, "result": result})))
    return canonical_activity_ref(mind, kind, sid if kind == "dream" else identifier)


def test_private_choice_withdraws_a_pending_contact_before_its_first_send(system):
    mind, _, _, _ = system
    ref = activity(system)
    decide(system, ref, "share", "initial-share")
    created = wish(system, ref, "initial-wish")
    attempt = mind.claim_contact(owner_epoch="synthetic-epoch")
    mind.settle_contact(attempt_id=attempt["id"], state="pending", text="梦里有一道蓝门。")
    assert mind.check_contact(attempt["id"], "synthetic-epoch", include_pending=True)["eligible"]
    decide(system, ref, "private", "changed-my-mind", evidence_ids=[])
    assert not mind.check_contact(attempt["id"], "synthetic-epoch", include_pending=True)["eligible"]
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", aborted_before_send=True, reason="contact-review-failed")
    assert next(d for d in mind.read()["desires"] if d["id"] == created["desire_id"])["status"] == "abandoned"


def decide(system, ref, decision, key, *, reason="", stimulus="wish-review", evidence_ids=None):
    mind, _, _, _ = system
    with mind.engine.db.connect() as conn:
        revision = mind._load(conn)["revision"]
    event = SimpleNamespace(evidence_ids=[ref["source_id"]] if evidence_ids is None else evidence_ids, command_id=key, agent_version="fixture-v1")
    proposal = SharingDecision(activity_ref=ref, decision=decision, reason=reason)
    mind._mutate({"command_id": key, "agent_version": "fixture-v1", "expected_revision": revision, "evidence_ids": event.evidence_ids},
        "synthetic-sharing", lambda conn, state, eid: apply_decisions(mind, conn, state, [proposal], event, {}, stimulus))


def wish(system, ref, key, action="create", desire_id=None):
    mind, _, _, clock = system
    with mind.engine.db.connect() as conn:
        revision = mind._load(conn)["revision"]
    return mind.manage_desire(DesireChange(command_id=key, agent_version="fixture-v1", expected_revision=revision,
        action=action, desire_id=desire_id, evidence_ids=[ref["source_id"]], activity_ref=ref,
        **({"kind": "contact", "content": "想聊蓝门", "topic": "梦里的门", "strength": 60,
            "expires_at": (clock[0] + timedelta(days=3)).isoformat(), "completion": "讲了想讲的那段"} if action == "create" else {})))


@pytest.mark.parametrize("kind", ["dream", "exploration"])
def test_private_and_defer_can_change_without_new_facts_or_reason(system, kind):
    mind, _, _, _ = system
    ref = activity(system, kind)
    decide(system, ref, "private", "first")
    before = read_activity_ref(mind, ref)["text"]
    decide(system, ref, "share", "now-share")
    did = wish(system, ref, "contact")["desire_id"]
    decide(system, ref, "share", "same-choice")
    with mind.engine.db.connect() as conn:
        same = mind._load(conn)
        assert same["desires"][did]["sharing_revision"] == 2
        assert mind._desire_ready(conn, same["desires"][did], mind.clock(), state=same)
    decide(system, ref, "defer", "later")
    with mind.engine.db.connect() as conn:
        saved = mind._load(conn)["desires"][did]
        assert saved["status"] == "waiting"
        assert saved["reason"] == ""
        assert saved["contact_wait"]["condition"] == "review"
        assert not saved["contact_wait"].get("retry_at")
    decide(system, ref, "share", "share-again", stimulus="delivery")
    wish(system, ref, "resume", action="resume", desire_id=did)
    assert read_activity_ref(mind, ref)["text"] == before
    decide(system, ref, "abandon", "let-go")
    assert read_activity_ref(mind, ref)["text"] == before
    decisions = mind.read()["activity_decisions" if kind == "dream" else "exploration_decisions"]
    assert decisions[0]["activity_ref"] == ref
    assert decisions[0]["reason"] == ""


def deliver(system, ref, state, key, *, channel="feishu", conversation="chat-a", message_id=None, draft_id=None):
    mind, memory, _, _ = system
    return memory.ingest({"id": key + ":" + state, "kind": "delivery", "at": mind.clock(), "channel": channel,
        "conversation_id": conversation, "delivery_id": key, "bubble_id": key + ":bubble", "text": "梦里有一道蓝门。",
        "state": state, "message_id": message_id, "draft_id": draft_id,
        "references": [{"activity_ref": ref, "mode": "new", "reason": ""}]})


def test_dream_coverage_waits_for_real_acceptance_and_never_becomes_a_finding(system):
    mind, memory, _, _ = system
    ref = activity(system)
    memory.sharing.register({"reply_id": "draft-a", "bubbles": [{"text": "梦里有一道蓝门。", "references": [{"activity_ref": ref}]}]})
    memory.sharing.prepare({"draft_id": "draft-a", "references": [{"activity_ref": ref}]})
    deliver(system, ref, "unconfirmed", "attempt-a", draft_id="draft-a")
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn, ref)["state"] == "unconfirmed"
        assert not conn.execute("SELECT 1 FROM mind_graph_nodes WHERE kind='finding'").fetchone()
    with pytest.raises(Conflict, match="original receipt"):
        memory.sharing.prepare({"draft_id": "draft-a", "references": [{"activity_ref": ref}]})
    assert memory.sharing.prepare({"draft_id": "other-channel-draft", "references": [{"activity_ref": ref}]})["state"] == "ready"
    deliver(system, ref, "accepted", "attempt-a", message_id="platform-original", draft_id="draft-a")
    with mind.engine.db.connect() as conn:
        coverage = memory.sharing.coverage(conn, ref)
        assert coverage["state"] == "shared"
        assert coverage["deliveries"][0]["message_id"] == "platform-original"
    assert memory.sharing.prepare({"draft_id": "cross-channel-new", "references": [{"activity_ref": ref}]})["state"] == "ready"
    retell = memory.sharing.register({"reply_id": "reflection", "bubbles": [{"text": "想起蓝门", "references": [{"activity_ref": ref, "mode": "reflection"}]}]})
    assert retell["bubbles"][0]["references"][0]["activity_ref"] == ref


def test_failed_delivery_keeps_the_original_and_does_not_mark_shared(system):
    mind, memory, _, _ = system
    ref = activity(system)
    deliver(system, ref, "canceled", "failed")
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn, ref)["state"] != "shared"
    assert "蓝门" in read_activity_ref(mind, ref)["text"]


def test_legacy_content_hash_and_version_do_not_gate_a_real_activity_pointer(system):
    _, memory, _, _ = system
    ref = activity(system)
    old = {**ref, "content_hash": "legacy-unused", "version": 987}
    prepared = memory.sharing.prepare({"draft_id": "stable-id", "references": [{"activity_ref": old, "version": 654}]})
    canonical = prepared["checked"][0]["references"][0]["activity_ref"]
    assert canonical == ref and "content_hash" not in canonical


def test_registered_reply_words_can_change_without_changing_its_relationship(system):
    mind, memory, _, _ = system
    ref = activity(system)
    for words in ("梦里是蓝门。", "又想起那扇门了。"):
        result = memory.sharing.register({"reply_id": "one-reply", "bubbles": [{"text": words, "references": [{"activity_ref": ref}]}]})
    assert result["bubbles"][0]["text"] == "又想起那扇门了。"
    assert "text_hash" not in result["bubbles"][0]
    with mind.engine.db.connect() as conn:
        assert memory.sharing.references(conn, "完全不同的正文", "one-reply")[0]["activity_ref"] == ref
        assert conn.execute("SELECT digest FROM mind_reply_references WHERE reply_id='one-reply'").fetchone()[0] == ""


def test_multi_bubble_delivery_uses_the_registered_index_not_text_matching(system):
    mind, memory, _, _ = system
    dream, report = activity(system), activity(system, "exploration", "paper-report")
    memory.sharing.register({"reply_id": "original-input", "bubbles": [
        {"text": "同样的正文", "references": [{"activity_ref": dream}]},
        {"text": "同样的正文", "references": [{"activity_ref": report}]}]})
    with mind.engine.db.connect() as conn:
        assert memory.sharing.references(conn, "同样的正文", "original-input") == []
        assert memory.sharing.references(conn, reply_id="original-input", bubble_index=1)[0]["activity_ref"] == report
    accepted = memory.ingest({"id": "indexed-receipt", "kind": "delivery", "at": mind.clock(), "channel": "feishu",
        "conversation_id": "chat-index", "delivery_id": "indexed-send", "bubble_id": "actual-bubble-id",
        "reply_input_id": "original-input", "reply_bubble_index": 1, "text": "发送时的正文可以不同",
        "state": "accepted", "message_id": "actual-platform-id"})
    with mind.engine.db.connect() as conn:
        share = memory._get(conn, accepted["share_id"])
    assert share["bubbles"]["actual-bubble-id"]["references"][0]["activity_ref"] == report


def test_repeated_report_projection_keeps_its_existing_database_unit_ids(system):
    first = findings(system)
    assert [unit["id"] for unit in findings(system)] == [unit["id"] for unit in first]


@pytest.mark.parametrize("scrubbed", [False, True])
def test_late_registered_reply_cannot_restore_a_deleted_activity(system, scrubbed):
    mind, memory, _, _ = system
    ref = activity(system)
    secret = "蓝门 deleted-source reply"
    memory.sharing.register({"reply_id": "late-source-input", "bubbles": [{"text": secret, "references": [{"activity_ref": ref}]}]})
    mind.engine.delete(ref["source_id"])
    if scrubbed:
        settle(mind.engine)
    with mind.engine.db.connect() as conn:
        with pytest.raises((Missing, Conflict, ValueError)):
            memory.sharing.references(conn, reply_id="late-source-input", bubble_index=0)
    receipt = memory.ingest({"id": "late-source-delivery", "kind": "delivery", "at": mind.clock(), "channel": "feishu",
        "conversation_id": "chat-a", "delivery_id": "late-source-send", "bubble_id": "late-source-bubble",
        "reply_input_id": "late-source-input", "reply_bubble_index": 0,
        "state": "accepted", "message_id": "late-source-platform-id", "text": secret})
    assert receipt.get("references_withheld") is True
    assert texts_everywhere(mind.engine, secret) == set()


def test_reply_to_an_old_message_uses_channel_chat_and_original_activity(system):
    mind, memory, _, clock = system
    ref = activity(system)
    delivered = deliver(system, ref, "accepted", "old", message_id="same-platform-id")
    clock[0] += timedelta(days=40)
    incoming = {"id": "question", "kind": "owner-message", "at": mind.clock(), "text": "那个门后来呢？",
                "channel": "feishu", "conversation_id": "chat-a", "platform_message_id": "user-current",
                "reply_to_message_id": "same-platform-id", "thread_id": "thread-a"}
    with mind.engine.db.connect() as conn:
        resolution = memory.sharing.resolve_reply_context(conn, incoming)
        wrong_chat = memory.sharing.resolve_reply_context(conn, {**incoming, "conversation_id": "chat-b"})
        wrong_channel = memory.sharing.resolve_reply_context(conn, {**incoming, "channel": "wechat"})
    assert resolution["state"] == "resolved"
    assert resolution["candidates"][0]["activity_refs"] == [ref]
    assert wrong_chat["state"] == wrong_channel["state"] == "unresolved"
    # Root's ingest integration consumes this same deterministic resolution.
    receipt = memory.ingest({**incoming, "reply_resolution": resolution})
    graph = memory.graph.read(focus=receipt["event_id"], hops=1)
    assert any(e["predicate"] == "responds_to" and e["object"] == delivered["share_id"] for e in graph["edges"])
    user = [d for d in recent_dialogue(mind) if d["role"] == "user"][-1]
    assert user["reply_share_id"] == delivered["share_id"]
    assert user["activity_refs"] == [ref]
    assert user["conversation_id"] == "chat-a"


def test_legacy_finding_references_and_subjective_state_remain_compatible(system):
    mind, memory, _, _ = system
    unit = findings(system)[0]
    legacy = {"unit_id": unit["id"], "version": 1}
    assert ContentReference.model_validate(legacy).activity_ref is None
    memory.sharing.register({"reply_id": "legacy", "bubbles": [{"text": unit["text"], "references": [legacy]}]})
    for key, score in (("mood-a", 70), ("mood-b", 45)):
        with mind.engine.db.connect() as conn:
            revision = mind._load(conn)["revision"]
        mind.record(AffectiveEvent(command_id=key, agent_version="fixture-v1", expected_revision=revision,
            subjective=True, values={"curiosity": score}))
    assert mind.read()["dimensions"]["curiosity"]["basis"] == "subjective"
    assert mind.read()["dimensions"]["curiosity"]["reason"] == ""
    with pytest.raises(ValueError, match="source evidence"):
        AffectiveEvent(command_id="observed", agent_version="fixture-v1", expected_revision=1, values={"curiosity": 60})


def test_independent_wish_can_be_created_and_wait_without_material_or_cause(system):
    mind, _, _, clock = system
    created = mind.manage_desire(DesireChange(command_id="independent", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
        action="create", subjective=True, kind="explore", content="想看看颜色", topic="颜色", strength=40,
        expires_at=(clock[0] + timedelta(days=2)).isoformat(), completion="看看就好"))
    did = created["desire_id"]
    mind.manage_desire(DesireChange(command_id="independent-later", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
        action="wait", desire_id=did, subjective=True))
    with mind.engine.db.connect() as conn:
        desire = mind._load(conn)["desires"][did]
    assert desire["basis"] == "subjective" and desire["evidence"] == [] and desire["reason"] == ""
    assert desire["contact_wait"]["condition"] == "review"
    with pytest.raises(ValueError, match="requires evidence"):
        DesireChange(command_id="observed-wish", agent_version="fixture-v1", expected_revision=1, action="wait", desire_id=did)


def test_batch_reservation_tracks_real_bubble_ids_and_preview_does_not_reserve(system):
    mind, memory, _, _ = system
    ref = activity(system)
    request = {"batch_id": "batch", "entries": [{"draft_id": "bubble-a", "text": "蓝门", "references": [{"activity_ref": ref}]},
                                                 {"draft_id": "bubble-b", "text": "门后的海", "references": [{"activity_ref": ref}]}]}
    assert memory.sharing.preview(request)["state"] == "ready"
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM mind_share_reservations").fetchone()
    memory.sharing.prepare(request)
    memory.sharing.cancel("bubble-a")
    assert memory.sharing.prepare({"draft_id": "new-channel", "references": [{"activity_ref": ref}]})["state"] == "ready"
    memory.sharing.cancel("bubble-b")
    memory.sharing.prepare({"draft_id": "new-channel", "references": [{"activity_ref": ref}]})
    deliver(system, ref, "accepted", "real-attempt", draft_id="new-channel", message_id="real-platform-id")
    # A real accepted draft cannot submit again. A new choice can still discuss the activity.
    with pytest.raises(Conflict, match="accepted receipt"):
        memory.sharing.prepare({"draft_id": "new-channel", "references": [{"activity_ref": ref}]})
    assert memory.sharing.preview({"draft_id": "another", "references": [{"activity_ref": ref}]})["state"] == "ready"


def test_coverage_cannot_attach_an_unseen_dream_to_an_unrelated_accepted_bubble(system):
    mind, memory, _, _ = system
    ref = activity(system)
    accepted = memory.ingest({"id": "unrelated-delivery", "kind": "delivery", "at": mind.clock(), "channel": "feishu",
        "conversation_id": "chat-a", "delivery_id": "unrelated", "bubble_id": "unrelated-bubble", "text": "晚安。",
        "state": "accepted", "message_id": "unrelated-platform-id"})
    assessment = CoverageAssessment(mappings=[CoverageMapping(share_id=accepted["share_id"], bubble_id="unrelated-bubble",
        references=[ContentReference(activity_ref=ref)], confidence=1, reason="synthetic semantic claim")])
    with mind.engine.db.connect(write=True) as conn:
        with pytest.raises(Conflict, match="neither supplied"):
            memory.sharing.apply(conn, assessment, {accepted["share_id"]}, allowed_activity_sources=set())
        assert memory.sharing.coverage(conn, ref)["state"] == "unshared"
    registered = deliver(system, ref, "accepted", "registered", message_id="registered-platform-id")
    known = CoverageAssessment(mappings=[CoverageMapping(share_id=registered["share_id"], bubble_id="registered:bubble",
        references=[ContentReference(activity_ref=ref)], confidence=1, reason="registered source")])
    with mind.engine.db.connect(write=True) as conn:
        memory.sharing.apply(conn, known, {registered["share_id"]}, allowed_activity_sources=set())
        assert memory.sharing.coverage(conn, ref)["state"] == "shared"


def test_sourcefree_dream_choice_still_depends_on_the_actual_original(system):
    mind, _, _, _ = system
    ref = activity(system)
    decide(system, ref, "private", "independent-private", evidence_ids=[])
    decide(system, ref, "share", "independent-share", evidence_ids=[])
    assert mind.read()["activity_decisions"][0]["needs_review"] is False
    mind.engine.delete(ref["source_id"])
    assert mind.read()["activity_decisions"][0]["needs_review"] is True


def test_legacy_reference_registration_upgrades_dependencies_without_changing_the_reply(system):
    mind, memory, _, _ = system
    unit = findings(system)[0]
    refs = [{"unit_id": unit["id"], "version": 1, "mode": "new", "reason": ""}]
    bubbles = [{"text": "旧回复", "text_hash": "legacy-unused-body-hash", "references": refs}]
    old = {"reply_id": "old-format", "bubbles": bubbles, "state": "registered", "at": mind.clock()}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_reply_references VALUES(?,?,?,?)", (mind.scope.key(), "old-format", digest(bubbles), dumps(old)))
    upgraded = memory.sharing.register({"reply_id": "old-format", "bubbles": [{"text": "旧回复", "references": refs}]})
    assert upgraded["reply_id"] == old["reply_id"] and upgraded["at"] == old["at"]
    assert upgraded["bubbles"][0]["text"] == "旧回复"
    assert upgraded["bubbles"][0]["source_ids"] == unit["source_ids"]
    assert "text_hash" not in upgraded["bubbles"][0]


def test_deleting_the_original_takes_bound_desire_and_registered_share_words(system):
    mind, memory, _, clock = system
    ref = activity(system)
    decide(system, ref, "share", "bound-share", evidence_ids=[])
    mind.manage_desire(DesireChange(command_id="bound-wish", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
        action="create", subjective=True, activity_ref=ref, kind="contact", content="我想讲蓝门", topic="蓝门的梦", strength=50,
        expires_at=(clock[0] + timedelta(days=2)).isoformat(), completion="讲完蓝门那段"))
    memory.sharing.register({"reply_id": "bound-reply", "bubbles": [{"text": "梦里有一道蓝门。", "references": [{"activity_ref": ref}]}]})
    deliver(system, ref, "accepted", "bound-send", message_id="bound-platform-id")
    reply = {"channel": "feishu", "conversation_id": "chat-a", "reply_to_message_id": "bound-platform-id"}
    with mind.engine.db.connect() as conn:
        assert memory.sharing.resolve_reply_context(conn, reply)["state"] == "resolved"
    mind.engine.delete(ref["source_id"])
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        result = memory.sharing.resolve_reply_context(conn, reply)
        registered = json.loads(conn.execute("SELECT data FROM mind_reply_references WHERE scope=? AND reply_id='bound-reply'", (mind.scope.key(),)).fetchone()[0])
    assert result["state"] == "unresolved" and result["candidates"] == []
    assert "蓝门" not in registered["bubbles"][0]["text"]
    assert texts_everywhere(mind.engine, "蓝门") == set()


def test_dream_and_internal_imagination_cannot_become_factual_graph_findings(system):
    mind, memory, source, _ = system
    ref = activity(system)
    dream_fact = GraphAssessment(nodes=[GraphNode(key="dream-fact", kind="finding", title="蓝门是真实观察",
        text="门后真的有一片海", evidence_ids=[ref["source_id"]], basis="documented")])
    sid = source("real-observation", "The observed fictional fixture contains blue paint.")
    imagined_fact = GraphAssessment(nodes=[GraphNode(key="imagined-fact", kind="finding", title="我想象的门",
        evidence_ids=[sid], basis="internal_thought")])
    with mind.engine.db.connect(write=True) as conn:
        with pytest.raises(Conflict, match="factual graph evidence"):
            memory.graph.apply(conn, dream_fact, memory.graph.proof(conn, [ref["source_id"]]), "dream-eval", {})
        with pytest.raises(Conflict, match="factual finding"):
            memory.graph.apply(conn, imagined_fact, memory.graph.proof(conn, [sid]), "imagined-eval", {})
        assert not conn.execute("SELECT 1 FROM mind_graph_nodes WHERE kind='finding'").fetchone()
        actual = GraphAssessment(nodes=[GraphNode(key="actual", kind="finding", title="实测蓝色",
            evidence_ids=[sid], basis="documented")])
        result = memory.graph.apply(conn, actual, memory.graph.proof(conn, [sid]), "actual-eval", {})
        assert memory.graph.get(conn, result["node_ids"][0])["basis"] == "documented"
    # Sharing and its host-observed receipt remain real operations.
    accepted = deliver(system, ref, "accepted", "dream-operation", message_id="dream-operation-platform-id")
    assert memory.graph.read(focus=accepted["event_id"])["nodes"]


def test_old_dream_stays_readable_and_shareable_after_correction_without_content_gate(system):
    mind, memory, _, _ = system
    ref = activity(system)
    with mind.engine.db.connect() as conn:
        key = conn.execute("SELECT source_key FROM sources WHERE id=?", (ref["source_id"],)).fetchone()[0]
    mind.engine.receive(SourceInput(namespace="kin-dream", key=key, version="2", scope=mind.scope, authority="model",
        text="Kin 的梦（想象出来的，不是发生过的事）：\n改正后的梦只有紫门。", occurred_at=mind.clock(), extract=False))
    old = read_activity_ref(mind, ref, historical=True)
    assert old["superseded"] and "蓝门" in old["text"]
    assert memory.sharing.preview({"draft_id": "old-as-new", "references": [{"activity_ref": ref}]})["state"] == "ready"
    assert memory.sharing.preview({"draft_id": "old-retelling", "references": [{"activity_ref": ref, "mode": "reminiscence"}]})["state"] == "ready"


@pytest.mark.parametrize("change", ["correction", "delete"])
def test_late_real_receipt_closes_original_unknown_delivery_without_restoring_content(system, change):
    mind, memory, _, _ = system
    ref = activity(system)
    memory.sharing.prepare({"draft_id": "original-draft", "references": [{"activity_ref": ref}]})
    original = deliver(system, ref, "unconfirmed", "original-attempt", draft_id="original-draft")
    if change == "delete":
        mind.engine.delete(ref["source_id"])
        settle(mind.engine)
    else:
        with mind.engine.db.connect() as conn:
            key = conn.execute("SELECT source_key FROM sources WHERE id=?", (ref["source_id"],)).fetchone()[0]
        mind.engine.receive(SourceInput(namespace="kin-dream", key=key, version="2", scope=mind.scope, authority="model",
            text="Kin 的梦（想象出来的，不是发生过的事）：\n改正后的梦只有紫门。", occurred_at=mind.clock(), extract=False))
    accepted = deliver(system, ref, "accepted", "original-attempt", draft_id="original-draft", message_id="original-platform-receipt")
    assert accepted["share_id"] == original["share_id"]
    with mind.engine.db.connect() as conn:
        share = memory._get(conn, original["share_id"])
        coverage = json.loads(conn.execute("SELECT data FROM mind_share_coverage WHERE scope=? AND share_id=? AND bubble_id=?",
            (mind.scope.key(), original["share_id"], "original-attempt:bubble")).fetchone()[0])
    assert share["bubbles"]["original-attempt:bubble"]["state"] == "accepted"
    assert coverage["state"] == "accepted" and coverage["message_id"] == "original-platform-receipt"
    if change == "delete":
        assert coverage["needs_review"] is True
        with pytest.raises((Conflict, Missing)):
            memory.sharing.preview({"draft_id": "new-send-id", "references": [{"activity_ref": ref}]})
        assert "蓝门" not in share["bubbles"]["original-attempt:bubble"]["text"]
        assert texts_everywhere(mind.engine, "蓝门") == set()
    else:
        assert memory.sharing.preview({"draft_id": "new-send-id", "references": [{"activity_ref": ref}]})["state"] == "ready"
