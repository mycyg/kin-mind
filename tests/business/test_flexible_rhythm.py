"""Synthetic continuity: a changing inner state, independent rhythm, and reliable attention.

No live host, private conversation, model call, real send, or outside database is involved.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing
from eventmem.core.models import Scope, SourceInput
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import ExpressionIntent, TraitDecision, TraitObservation
from kin_mind.continuity import (
    ConcernChange,
    ContinuityConfig,
    OwnerRequest,
    RhythmProposal,
    Understanding,
)
from kin_mind.expression import compile_expression
from kin_mind.expression_intent import commit_intent, current
from kin_mind.memory import MemoryContinuity
from kin_mind.rhythm import rhythm_view
from kin_mind.state import AffectiveEvent, Mind
from kin_mind.traits import Traits


@pytest.fixture
def flexible(tmp_path):
    clock = [datetime.now(timezone.utc) + timedelta(minutes=1)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-flexible"), clock=lambda: clock[0].isoformat())

    def source(key, text=None, authority="explicit"):
        return mind.engine.receive(SourceInput(namespace="flexible-test", key=key, scope=mind.scope,
            text=text or key, authority=authority, occurred_at=mind.clock(), extract=False,
            metadata={"role": "user" if authority == "explicit" else "assistant", "host_event": "message"}))["id"]

    mind.initialize(agent_version="flexible-v1", evidence_ids=[source("initialize")])
    mind.configure_continuity(ContinuityConfig(command_id="continuity-on", agent_version="flexible-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("configure")],
        features={"rhythm": True, "interpretation": True, "expression": True}, reason="Synthetic configuration"))
    memory = MemoryContinuity(mind)
    memory.configure({"records": True})
    return mind, memory, source, clock


def subjective(mind, key, **extra):
    return mind.record(AffectiveEvent(command_id=key, agent_version="flexible-v1",
        expected_revision=mind.read()["revision"], subjective=True, **extra))


def rest(mind):
    subjective(mind, "rest", values={"joy": 82, "sadness": 65},
        rhythm=RhythmProposal(phase="resting", alertness=20, target=10, half_life_minutes=60,
                             current_activity="自己想一会儿颜色和音乐"))


def received(memory, clock, key, text="Just saying hello", **extra):
    return memory.ingest({"id": key, "kind": "owner-message", "at": clock[0].isoformat(), "text": text, **extra})


def raw(mind):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)


def decide(mind, decision, command):
    with mind.engine.db.connect(write=True) as conn:
        return Traits(mind).decide(conn, [decision], command, {}, None, command)[0]


def test_subjective_change_needs_no_cause_and_remembers_previous_state(flexible):
    mind, _, _, clock = flexible
    rest(mind)
    clock[0] += timedelta(minutes=2)
    subjective(mind, "drift", values={"joy": 25, "sadness": 88},
        understanding=Understanding(meaning="今天想换一种颜色", topic="颜色", importance=30,
                                   confidence=0.8, basis="internal_thought"),
        rhythm=RhythmProposal(phase="awake", alertness=75, target=65, half_life_minutes=180))
    state = Mind(mind.engine, mind.scope, clock=mind.clock).read(history=10)
    assert state["dimensions"]["joy"]["value"] == 25
    assert state["dimensions"]["sadness"]["value"] == 88
    assert all(not state["dimensions"][name]["needs_review"] for name in ("joy", "sadness"))
    assert state["rhythm"]["reason"] == "" and state["rhythm"]["evidence_ids"] == []
    assert not state["rhythm"]["needs_review"]
    assert state["rhythm"]["previous"]["base_phase"] == "resting"
    assert state["rhythm"]["current_activity"] == "自己想一会儿颜色和音乐"
    assert raw(mind)["last_assessment"]["understanding"]["evidence"] == []
    assert state["history"], "The existing revision history keeps both states; no causal explanation is needed"


def test_message_draws_attention_without_resetting_phase_activity_or_mood(flexible):
    mind, memory, _, clock = flexible
    rest(mind)
    before = raw(mind)["dimensions"]
    clock[0] += timedelta(minutes=1)
    received(memory, clock, "hello")
    # The public seam works for older hosts too; the ingest seam is connected by memory.py.
    mind.attend("hello")
    after = Mind(mind.engine, mind.scope, clock=mind.clock)
    rhythm = after.read()["rhythm"]
    assert rhythm["phase"] == rhythm["base_phase"] == "resting"
    assert rhythm["attention_for"] == "owner_message" and rhythm["return_to"] == "resting"
    assert rhythm["current_activity"] == "自己想一会儿颜色和音乐"
    assert raw(after)["dimensions"] == before
    assert after.finish_attention("hello")["changed"]
    assert after.read()["rhythm"]["attention_for"] is None
    assert after.read()["rhythm"]["phase"] == "resting"


def test_task_and_urgent_attention_persist_return_and_cannot_be_lost_to_chitchat(flexible):
    mind, memory, _, clock = flexible
    rest(mind)
    received(memory, clock, "permission", "You can choose silence during ordinary chat")
    with mind.engine.db.connect() as conn:
        source_id = conn.execute("SELECT source_id FROM mind_reply_inputs WHERE id='permission'").fetchone()[0]
    memory.habits.update({"command_id": "allow-quiet", "expected_revision": 0, "evidence_ids": [source_id],
                         "reason": "Owner permits ordinary chat silence", "preferences": {"reply_choice": "autonomous"}})
    clock[0] += timedelta(minutes=1)
    received(memory, clock, "task", "Please finish the report")
    mind.attend("task", "task")
    with pytest.raises(Conflict, match="Task or urgent"):
        memory.habits.choose_reply({"input_id": "task", "action": "silent"})
    assert mind.read()["rhythm"]["base_phase"] == "resting"
    assert "完整处理" in compile_expression({}, rhythm=mind.read()["rhythm"])["guidance"][-1]["text"]
    received(memory, clock, "urgent", "Urgent, help now")
    mind.attend("urgent", "urgent")
    received(memory, clock, "chat")
    mind.attend("chat")
    restarted = Mind(mind.engine, mind.scope, clock=mind.clock)
    assert restarted.read()["rhythm"]["attention_for"] == "urgent"
    assert restarted.finish_attention("task")["changed"], "The old input's marker is finished independently"
    assert restarted.read()["rhythm"]["attention_for"] == "urgent", "An old task result cannot clear the newer urgent input"
    for action in ("silent", "defer"):
        with pytest.raises(Conflict) as refused:
            memory.habits.choose_reply({"input_id": "urgent", "action": action})
        assert refused.value.code == "reply-attention-required"
    assert restarted.finish_attention("urgent")["changed"]
    assert restarted.read()["rhythm"]["phase"] == "resting"
    assert restarted.read()["rhythm"]["current_activity"] == "自己想一会儿颜色和音乐"


def test_attention_requires_a_received_input_and_is_idempotent(flexible):
    mind, memory, source, clock = flexible
    rest(mind)
    with pytest.raises(Missing):
        mind.attend("made-up-task", "task")
    received(memory, clock, "real-input")
    mind.attend("real-input", "task")
    revision = mind.read()["revision"]
    assert not mind.attend("real-input", "task")["changed"]
    assert mind.read()["revision"] == revision
    assert mind.finish_attention("real-input")["changed"]
    assert mind.attend("real-input", "task")["changed"], "A later intentional reassessment has its own history entry"
    fake = source("model-only", authority="model")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_reply_inputs VALUES(?,?,?,?)", (mind.scope.key(), "forged", fake, mind.clock()))
    with pytest.raises(Conflict, match="owner input"):
        mind.attend("forged", "urgent")


def test_long_offline_phase_stays_and_existing_review_queue_reconsiders_it(flexible):
    mind, _, _, clock = flexible
    rest(mind)
    clock[0] += timedelta(days=5)
    view = mind.read()["rhythm"]
    assert view["mode"] == "independent" and view["base_phase"] == "resting" and view["review_due"]
    assert view["interactions"]["window_count"] == 0
    ActionEvents(mind).crossings()
    ActionEvents(mind).crossings()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_action_events WHERE kind='rhythm-review'").fetchone()[0] == 1
    subjective(mind, "own-awakening", rhythm=RhythmProposal(phase="awake", alertness=70, target=65, half_life_minutes=180))
    assert mind.read()["rhythm"]["phase"] == "awake"


def test_legacy_rhythm_is_adapted_without_mechanical_wakeup():
    entry = {"phase": "resting", "alertness": 20, "target": 10, "half_life_minutes": 60,
             "at": "2026-01-01T00:00:00+00:00", "reason": "Legacy rest", "evidence": [], "event_id": "old", "config_version": "old"}
    view = rhythm_view(entry, "2026-01-01T00:10:00+00:00",
                       {"sample_status": "forming", "last_owner_at": "2026-01-01T00:09:00+00:00"})
    assert view["phase"] == view["base_phase"] == "resting"
    assert view["attention_for"] == "owner_message" and view["return_to"] == "resting"
    assert entry["phase"] == "resting", "Reads do not rewrite the persistent assessment"


def test_self_preference_drifts_without_new_observations_or_reason_and_keeps_history(flexible):
    mind, _, _, clock = flexible
    first = decide(mind, TraitDecision(action="propose", basis="self_preference", category="aesthetics",
                                      slug="colors", key="颜色", text="最近偏爱浓一点的颜色"), "pref-1")
    clock[0] += timedelta(days=1)
    second = decide(mind, TraitDecision(action="revise", basis="self_preference", trait_id=first["id"],
        expected_revision=first["revision"], text="今天更偏爱淡一点的颜色"), "pref-2")
    third = decide(mind, TraitDecision(action="establish", basis="self_preference", trait_id=second["id"],
        expected_revision=second["revision"], text=second["text"]), "pref-3")
    assert third["subjective"] and third["reason"] == "" and third["evidence"] == []
    assert third["facts"]["distinct_episodes"] == 0, "No fictional behaviour or experience was manufactured"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_trait_history WHERE id=?", (first["id"],)).fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM mind_trait_observations").fetchone()[0] == 0
    faded = decide(mind, TraitDecision(action="fade", basis="self_preference", trait_id=third["id"],
        expected_revision=third["revision"], text="这阵子不太在意这种颜色了"), "pref-4")
    restored = decide(mind, TraitDecision(action="restore", basis="self_preference", trait_id=faded["id"],
        expected_revision=faded["revision"], text="又想看淡一点的颜色"), "pref-5")
    assert faded["status"] == "fading" and restored["status"] == "candidate"
    with mind.engine.db.connect() as conn:
        saved = Traits(Mind(mind.engine, mind.scope, clock=mind.clock)).get(conn, restored["id"])
    assert saved["text"] == "又想看淡一点的颜色" and saved["revision"] == 5 and saved["evidence"] == []


@pytest.mark.parametrize("category", ["values", "permissions", "habits"])
def test_self_preference_cannot_change_core_authority_categories(flexible, category):
    mind, *_ = flexible
    with pytest.raises(Conflict) as refused:
        decide(mind, TraitDecision(action="propose", basis="self_preference", category=category,
                                  slug="unapproved", text="change a core rule"), "invalid-" + category)
    assert refused.value.code == "trait-preference-category"


def test_self_preference_still_obeys_persona_permissions(flexible, monkeypatch):
    mind, *_ = flexible
    monkeypatch.setattr("kin_mind.traits.load_persona", lambda *_: {"mutable_trait_keys": ["interests"]})
    with pytest.raises(ValueError, match="owner approval"):
        decide(mind, TraitDecision(action="propose", basis="self_preference", category="aesthetics",
                                  slug="forbidden", text="A new aesthetic preference"), "forbidden")


def test_subjective_drift_cannot_overwrite_owner_instruction_or_invent_a_quote(flexible):
    mind, memory, _, clock = flexible
    first = decide(mind, TraitDecision(action="propose", basis="self_preference", category="expression_habits",
                                      slug="style", text="想多说几句"), "own-style")
    words = "Please keep replies concise when we work"
    source = received(memory, clock, "owner-style", words)["source_id"]
    with pytest.raises(Conflict) as refused:
        decide(mind, TraitDecision(action="revise", trait_id=first["id"], expected_revision=first["revision"],
            basis="owner_instruction", text="工作时简短些", quote="The owner said something else", evidence_ids=[source]), "fake-quote")
    assert refused.value.code == "trait-quote-unverified"
    with pytest.raises(Conflict):
        decide(mind, TraitDecision(action="revise", trait_id=first["id"], expected_revision=first["revision"],
            basis="owner_instruction", text="工作时简短些", evidence_ids=[source]), "missing-quote")
    owner = decide(mind, TraitDecision(action="revise", trait_id=first["id"], expected_revision=first["revision"],
        basis="owner_instruction", text="工作时简短些", quote=words, evidence_ids=[source]), "verified-owner-style")
    with pytest.raises(Conflict) as refused:
        decide(mind, TraitDecision(action="revise", trait_id=owner["id"], expected_revision=owner["revision"],
            basis="self_preference", text="今天就想无视工作偏好"), "overwrite-owner")
    assert refused.value.code == "trait-preference-authority"
    with pytest.raises(Conflict):
        decide(mind, TraitDecision(action="revoke", trait_id=owner["id"], expected_revision=owner["revision"],
            basis="self_preference", text=owner["text"], evidence_ids=[source]), "revoke-with-mood")


def test_autonomous_reply_choice_has_no_required_reason_and_no_phantom_choice_on_failure(flexible):
    _mind, memory, _, clock = flexible
    received(memory, clock, "ordinary")
    assert memory.habits.reply_status("ordinary")["state"] == "default"
    with pytest.raises(Conflict, match="enabled"):
        memory.habits.choose_reply({"input_id": "ordinary", "action": "silent"})
    assert memory.habits.reply_status("ordinary")["state"] == "default"
    chosen = memory.habits.choose_reply({"input_id": "ordinary", "action": "reply"})
    assert chosen["state"] == "decided" and chosen["reason"] == ""
    permission = received(memory, clock, "permission-for-quiet", "Ordinary chat can be quiet")["source_id"]
    memory.habits.update({"command_id": "quiet-is-okay", "expected_revision": 0, "evidence_ids": [permission],
                         "reason": "Owner permits silence", "preferences": {"reply_choice": "autonomous"}})
    received(memory, clock, "quiet-choice")
    quiet = memory.habits.choose_reply({"input_id": "quiet-choice", "action": "silent"})
    assert quiet["state"] == "decided" and quiet["reason"] == ""
    received(memory, clock, "defer-choice")
    deferred = memory.habits.choose_reply({"input_id": "defer-choice", "action": "defer"})
    assert deferred["state"] == "decided" and deferred["reason"] == "" and deferred["retry_at"]


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("attention_for", ["task", "urgent"])
def test_actual_task_and_urgent_input_are_protected_even_when_rhythm_is_forming_or_off(flexible, enabled, attention_for):
    mind, memory, source, clock = flexible
    assert mind.read()["rhythm"]["phase"] == "forming"
    if not enabled:
        mind.configure_continuity(ContinuityConfig(command_id="rhythm-off", agent_version="flexible-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[source("off")], features={"rhythm": False}, reason="Test off"))
    permission = received(memory, clock, "permission", "Ordinary chat can be quiet")["source_id"]
    memory.habits.update({"command_id": "allow", "expected_revision": 0, "evidence_ids": [permission],
                         "reason": "Owner permits silence", "preferences": {"reply_choice": "autonomous"}})
    received(memory, clock, "required", "Please handle this input", attention_for=attention_for)
    with pytest.raises(Conflict) as refused:
        memory.habits.choose_reply({"input_id": "required", "action": "silent"})
    assert refused.value.code == "reply-attention-required"
    assert memory.habits.reply_status("required")["state"] == "default", "A refusal never fabricates a successful quiet choice"
    if attention_for == "urgent":
        with pytest.raises(Conflict):
            memory.habits.choose_reply({"input_id": "required", "action": "defer"})
    assert memory.habits.choose_reply({"input_id": "required", "action": "reply"})["state"] == "decided"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("attention_for", ["task", "urgent"])
def test_routing_after_plain_ingest_protects_input_without_a_rhythm_and_receipt_finish_releases_it(flexible, enabled, attention_for):
    """The actual host path ingests before it knows the route: attend must work after that."""
    mind, memory, source, clock = flexible
    if not enabled:
        mind.configure_continuity(ContinuityConfig(command_id="rhythm-off-routed", agent_version="flexible-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[source("off-routed")],
            features={"rhythm": False}, reason="Test off"))
    permission = received(memory, clock, "routed-permission", "Ordinary chat can be quiet")["source_id"]
    memory.habits.update({"command_id": "allow-routed", "expected_revision": 0, "evidence_ids": [permission],
                         "reason": "Owner permits silence", "preferences": {"reply_choice": "autonomous"}})
    request = {"id": "routed-required", "kind": "owner-message", "at": clock[0].isoformat(), "text": "Please handle this"}
    receipt = memory.ingest(request)

    def stored_input():
        with mind.engine.db.connect() as conn:
            row = conn.execute("SELECT * FROM mind_runtime_events WHERE id=?", (receipt["id"],)).fetchone()
            return dict(row), json.loads(row["data"])

    before, original = stored_input()
    assert "attention_for" not in original and "attention_marker" not in original
    assert mind.attend(request["id"], attention_for)["changed"]
    after, classified = stored_input()
    assert classified["attention_marker"]["state"] == "active"
    assert classified["attention_marker"]["attention_for"] == attention_for
    assert {key: value for key, value in classified.items() if key != "attention_marker"} == original
    assert {key: value for key, value in after.items() if key != "data"} == {key: value for key, value in before.items() if key != "data"}
    assert memory.ingest(request) == receipt, "Adding a computed marker preserves retry identity and original receipt"
    assert raw(mind).get("rhythm") is None, "Input classification does not fabricate an independent phase"
    with pytest.raises(Conflict) as refused:
        memory.habits.choose_reply({"input_id": request["id"], "action": "silent"})
    assert refused.value.code == "reply-attention-required"
    restarted = Mind(mind.engine, mind.scope, clock=mind.clock)
    assert restarted.finish_attention(request["id"])["changed"]
    assert stored_input()[1]["attention_marker"]["state"] == "finished"
    assert not restarted.finish_attention(request["id"])["changed"]
    assert memory.habits.choose_reply({"input_id": request["id"], "action": "silent"})["state"] == "decided"


def test_finished_computed_marker_overrides_an_original_task_input_marker(flexible):
    mind, memory, _, clock = flexible
    item = received(memory, clock, "original-task", "Please do this", attention_for="task")
    assert mind.finish_attention("original-task")["changed"]
    with mind.engine.db.connect() as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_runtime_events WHERE id=?", (item["id"],)).fetchone()[0])
        assert data["attention_for"] == "task", "The original observation is retained"
        assert data["attention_marker"]["state"] == "finished"
        assert memory.habits._input_attention(conn, "original-task", item["source_id"]) is None


def test_runtime_payload_cannot_spoof_a_finished_computed_marker(flexible):
    mind, memory, _, clock = flexible
    permission = received(memory, clock, "marker-permission", "Ordinary chat can be quiet")["source_id"]
    memory.habits.update({"command_id": "marker-permission", "expected_revision": 0, "evidence_ids": [permission],
                         "reason": "Owner permits silence", "preferences": {"reply_choice": "autonomous"}})
    item = received(memory, clock, "spoof-marker", "Please handle this", attention_for="task",
                    attention_marker={"attention_for": "task", "state": "finished"})
    with mind.engine.db.connect() as conn:
        stored = json.loads(conn.execute("SELECT data FROM mind_runtime_events WHERE id=?", (item["id"],)).fetchone()[0])
    assert "attention_marker" not in stored, "Only the host attention operation writes computed markers"
    with pytest.raises(Conflict) as refused:
        memory.habits.choose_reply({"input_id": "spoof-marker", "action": "silent"})
    assert refused.value.code == "reply-attention-required"


def test_expression_intent_without_cause_survives_new_input_and_cites_no_fabricated_source(flexible):
    mind, memory, _, clock = flexible
    with mind.engine.db.connect(write=True) as conn:
        commit_intent(SimpleNamespace(mind=mind, conn=conn, state=mind._load(conn), sources=[],
            value=ExpressionIntent(stance="今天想说得轻一点"), event_id="intent-1", job_id="synthetic-job",
            receipt={}, version="flexible-v1", stimulus="idle-review"))
    clock[0] += timedelta(minutes=1)
    received(memory, clock, "new-message")
    with mind.engine.db.connect() as conn:
        intent = current(conn, mind, mind.clock(), persona=None)
    assert intent["stance"] == "今天想说得轻一点" and intent["stale_reason"] is None
    assert intent["evidence_ids"] == []


def test_mood_fluctuation_does_not_autopromote_to_permanent_traits(flexible):
    mind, *_ = flexible
    subjective(mind, "passing-mood", values={"joy": 80, "sadness": 80})
    assert mind.read()["dimensions"]["joy"]["value"] == mind.read()["dimensions"]["sadness"]["value"] == 80
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_traits'").fetchone()


def test_verified_behavior_still_requires_real_completed_receipts(flexible):
    mind, *_ = flexible
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict):
        Traits(mind).observe(conn, [TraitObservation(key="reliable", category="interests", slug="real-behavior",
            evidence_class="verified_behavior", polarity="support", result_ids=["invented-run"])],
            "fake-behavior", {}, None, "fake-behavior")


@pytest.mark.parametrize("kind", ["care", "anticipation", "distress"])
def test_own_concern_changes_and_reopens_without_causal_reason_or_new_evidence(flexible, kind):
    mind, _, source, clock = flexible
    mind.configure_continuity(ContinuityConfig(command_id="concerns-on", agent_version="flexible-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("concerns-allowed")],
        features={"concerns": True}, reason="Synthetic configuration"))
    created = mind.manage_concern(ConcernChange(command_id="own-concern", agent_version="flexible-v1",
        expected_revision=mind.read()["revision"], action="create", key="own-" + kind, kind=kind,
        content="心里记挂着这个念头", topic="自己的念头", intensity=70, basis="internal_thought", confidence=0.8))
    old_revision = created["revision"]
    subjective(mind, "mood-in-between", values={"joy": 65})
    clock[0] += timedelta(minutes=3)
    updated = mind.manage_concern(ConcernChange(command_id="own-concern-drift", agent_version="flexible-v1",
        expected_revision=old_revision, action="update", concern_id=created["concern_id"], intensity=28,
        content="这会儿又想淡一点"))
    assert updated["concern_revision"] == 2 and not updated.get("replayed")
    assert raw(mind)["dimensions"]["joy"]["score"] == 65
    for action in ("resolve", "reopen", "archive", "reopen"):
        mind.manage_concern(ConcernChange(command_id="own-" + action + "-" + str(mind.read()["revision"]),
            agent_version="flexible-v1", expected_revision=mind.read()["revision"], action=action,
            concern_id=created["concern_id"]))
    concern = raw(mind)["concerns"][created["concern_id"]]
    assert concern["reason"] == "" and concern["evidence"] == [] and concern["status"] == "active"
    assert concern["recurrence_count"] == 2 and concern["revision"] == 6
    assert concern["updated_at"] == clock[0].isoformat()
    projected = next(c for c in mind.read()["concerns"] if c["id"] == created["concern_id"])
    assert not projected["needs_review"]
    assert any(h["snapshot"]["concerns"].get(created["concern_id"], {}).get("intensity") == 70
               for h in mind.read(history=10)["history"])


def test_subjective_concern_does_not_authorize_shared_plan_or_owner_request(flexible):
    from pydantic import ValidationError
    mind, _, source, _ = flexible
    mind.configure_continuity(ContinuityConfig(command_id="bounded-concerns", agent_version="flexible-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("bounded-config")],
        features={"concerns": True}, reason="Synthetic configuration"))
    common = {"agent_version": "flexible-v1", "expected_revision": mind.read()["revision"], "action": "create",
              "content": "一起做一件事", "topic": "实际参与", "intensity": 50, "basis": "internal_thought", "confidence": 0.8}
    with pytest.raises(Conflict, match="current source evidence"):
        mind.manage_concern(ConcernChange(**common, command_id="invented-plan", key="plan", kind="shared_plan"))
    with pytest.raises(ValidationError):
        OwnerRequest(kind="help", action="帮忙", reason="", completion="做完")
    request = OwnerRequest(kind="help", action="帮忙", reason="请求协助", completion="做完", status="accepted")
    with pytest.raises(Conflict, match="current source evidence"):
        mind.manage_concern(ConcernChange(**common, command_id="invented-acceptance", key="owner", kind="care", owner_request=request))
    own = mind.manage_concern(ConcernChange(**common, command_id="bounded-own", key="own", kind="care"))
    with pytest.raises(Conflict, match="current source evidence"):
        mind.manage_concern(ConcernChange(command_id="upgrade-to-plan", agent_version="flexible-v1",
            expected_revision=mind.read()["revision"], action="update", concern_id=own["concern_id"], kind="shared_plan"))
    assert raw(mind)["concerns"][own["concern_id"]]["kind"] == "care"


def test_subjective_change_uses_current_state_but_actual_configuration_still_requires_revision(flexible):
    mind, _, source, _ = flexible
    earlier = mind.read()["revision"]
    subjective(mind, "new-own-state", values={"joy": 77})
    mind.record(AffectiveEvent(command_id="another-own-state", agent_version="flexible-v1", expected_revision=earlier,
                              subjective=True, values={"sadness": 62}))
    assert mind.read()["dimensions"]["joy"]["value"] == 77
    with pytest.raises(Conflict) as refused:
        mind.configure_continuity(ContinuityConfig(command_id="stale-actual-config", agent_version="flexible-v1",
            expected_revision=earlier, evidence_ids=[source("actual-config")], features={"rhythm": False}, reason="Owner setting"))
    assert refused.value.code == "mind-revision-changed"


def test_legacy_content_keys_never_refuse_a_new_appraisal_or_create_new_keys(flexible):
    from kin_mind import evidence_keys
    mind, *_ = flexible
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_evidence_keys VALUES(?,?,?,?)", (mind.scope.key(), "legacy-content-key", "old-event", 1))
        deferred = []
        assert not evidence_keys.already_scored(conn, mind.scope.key(), "legacy-content-key", at=mind.clock(), deferred=deferred)
        assert deferred == []
        assert evidence_keys.record(conn, mind.scope.key(), {"revision": 2, "last_evidence_key": "new-key"}, "affect", "new-event") == 0
        assert not conn.execute("SELECT 1 FROM mind_evidence_keys WHERE evidence_key='new-key'").fetchone()


def test_expression_outputs_and_persona_context_need_no_content_fingerprint(flexible):
    from kin_mind.expression_intent import stale_reason
    mind, memory, _, clock = flexible
    rest(mind)
    received(memory, clock, "fingerprint-free-input")
    view = mind.read()
    assert "fingerprint" not in view["rhythm"]["interactions"]
    assert "fingerprint" not in view["expression"]
    intent = {"stance": "自己的想法还在", "persona": "legacy-fulltext-hash",
              "valid_until": (clock[0] + timedelta(minutes=10)).isoformat()}
    assert stale_reason(intent, mind.clock(), persona={"version": "changed-approved-context", "text": "changed text"}) is None


def test_revised_source_does_not_hide_psychology_while_factual_authorization_and_deletion_still_apply(flexible):
    mind, _, source, clock = flexible
    mind.configure_continuity(ContinuityConfig(command_id="origins-concerns", agent_version="flexible-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("origins-config")],
        features={"concerns": True}, reason="Synthetic configuration"))
    origin = source("inner-origin", "旧的原话")
    subjective(mind, "cited-own-state", evidence_ids=[origin], values={"joy": 81},
        understanding=Understanding(meaning="想自己待一会儿", topic="自己的念头", importance=25,
                                   basis="internal_thought", confidence=0.8, evidence_ids=[origin]),
        rhythm=RhythmProposal(phase="resting", alertness=20, target=10, half_life_minutes=60, evidence_ids=[origin]))
    concern = mind.manage_concern(ConcernChange(command_id="cited-own-concern", agent_version="flexible-v1",
        expected_revision=mind.read()["revision"], action="create", key="cited-own", kind="care",
        content="还记着这个念头", topic="自己的念头", intensity=60, basis="internal_thought", confidence=0.8,
        evidence_ids=[origin]))
    clock[0] += timedelta(minutes=1)
    mind.engine.receive(SourceInput(namespace="flexible-test", key="inner-origin", version="2", scope=mind.scope,
        text="后来的原话已修订", authority="explicit", occurred_at=mind.clock(), extract=False,
        metadata={"role": "user", "host_event": "message"}))
    view = mind.read()
    assert not view["dimensions"]["joy"]["needs_review"]
    assert not view["rhythm"]["needs_review"] and view["rhythm"]["base_phase"] == "resting"
    assert not view["appraisal_summary"]["needs_review"]
    assert not view["appraisal_summary"]["understanding"]["needs_review"]
    assert not next(c for c in view["concerns"] if c["id"] == concern["concern_id"])["needs_review"]
    with mind.engine.db.connect() as conn:
        old_refs = raw(mind)["dimensions"]["joy"]["evidence"]
        assert not mind._fresh(conn, old_refs), "A revised citation is still stale for actual facts and authorization"
        assert mind._subjective_available(conn, old_refs), "The historical origin of a feeling still exists"
    with pytest.raises(Conflict):
        mind.configure_continuity(ContinuityConfig(command_id="cannot-use-old-owner-words", agent_version="flexible-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[origin], features={"rhythm": False}, reason="A factual setting"))
    mind.engine.delete(origin)
    assert mind.read()["dimensions"]["joy"]["needs_review"], "Actual deletion keeps its privacy boundary"
