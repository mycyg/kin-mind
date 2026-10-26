"""Energy recovery, drives and 此刻的状态 on real SQLite; synthetic people and words only."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict
from eventmem.core.models import Scope, SourceInput
from kin_mind import drives, energy, moment
from kin_mind.companion_view import CompanionView, companion_scope
from kin_mind.companions import FAMILY as GROUP, PEOPLE, XIAOGUANG as OWNER
from kin_mind.context import Contexts, affect_projection
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind, DesireChange

OTHER = next(iter(PEOPLE - {OWNER}))  # the other bound person


@pytest.fixture
def env(tmp_path):
    clock = [datetime.now(timezone.utc) + timedelta(minutes=1)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-life"), clock=lambda: clock[0].isoformat())
    def source(key):
        return mind.engine.receive(SourceInput(namespace="kin-owner-input", key=key, scope=mind.scope, text=key,
            authority="explicit", occurred_at=mind.clock(), extract=False, metadata={"role": "user", "host_event": "message"}))["id"]
    mind.initialize(agent_version="life-test", evidence_ids=[source("initial")])
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True})
    return mind, memory, clock, source


def message(memory, mind, key, person=OWNER, conversation="owner-chat", kind="p2p"):
    memory.ingest({"id": key, "kind": "owner-message", "text": "synthetic " + key, "at": mind.clock(),
                   "recipient_person_id": person, "speaker_person_id": OTHER if person == GROUP else person,
                   "conversation_key": conversation, "conversation_kind": kind})
    return key


def test_boredom_rises_when_idle_faster_with_energy_and_not_at_rest_and_eases_once(env):
    mind, _, clock, _ = env
    drives.configure(mind, {"enabled": True})
    clock[0] += timedelta(hours=4)
    with mind.engine.db.connect() as conn:
        assert drives.view(conn, mind.scope.key(), mind.clock())["boredom"]["value"] == 40
    energy.configure(mind, {"enabled": True, "night_penalty": 0, "initial": 100})
    energy.configure(mind, {"enabled": False})
    for _ in range(2):
        energy.activity(mind, {"kind": "explore", "activity_id": "wish"})  # recorded even with energy off
    with mind.engine.db.connect() as conn:
        eased = drives.view(conn, mind.scope.key(), mind.clock())["boredom"]
    assert eased["value"] == 5 and eased["last_engaging_at"] == mind.clock() and not eased["bored"]
    energy.configure(mind, {"enabled": True})
    energy.activity(mind, {"kind": "play", "activity_id": "game"})  # settles with the high energy tier
    clock[0] += timedelta(hours=2)
    with mind.engine.db.connect(write=True) as conn:
        assert drives.view(conn, mind.scope.key(), mind.clock())["boredom"]["value"] == 15
        drives.phase_changed(conn, mind, "resting")
    clock[0] += timedelta(hours=5)
    with mind.engine.db.connect() as conn:
        assert drives.view(conn, mind.scope.key(), mind.clock())["boredom"]["value"] == 15


def test_intimacy_grows_faster_at_night_and_stays_out_of_every_other_view(env):
    mind, _, clock, _ = env
    clock[0] = (clock[0] + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)  # a local noon
    drives.configure(mind, {"enabled": True})
    clock[0] += timedelta(hours=10)  # noon -> 22:00 local
    with mind.engine.db.connect() as conn:
        assert "intimacy" not in drives.view(conn, mind.scope.key(), mind.clock())
        assert drives.view(conn, mind.scope.key(), mind.clock(), private=True)["intimacy"]["value"] == 45
    clock[0] += timedelta(hours=4)  # inside the night window
    with mind.engine.db.connect() as conn:
        assert drives.view(conn, mind.scope.key(), mind.clock(), private=True)["intimacy"]["value"] == 54
    with companion_scope(CompanionView(mind.engine, mind.scope, OWNER, "owner-chat")):
        assert drives.owner_private()
    for person, chat in ((OTHER, "other-chat"), (GROUP, "family")):
        with companion_scope(CompanionView(mind.engine, mind.scope, person, chat)):
            assert not drives.owner_private()
    assert not drives.owner_private()


def test_only_owner_private_report_lowers_intimacy_and_a_decline_cools_down(env):
    mind, memory, clock, _ = env
    drives.configure(mind, {"enabled": True, "intimacy_initial": 80})
    own = message(memory, mind, "own-1")
    other = message(memory, mind, "other-1", OTHER, "other-chat")
    group = message(memory, mind, "group-1", GROUP, "family", "group")
    elsewhere = message(memory, mind, "own-elsewhere", OWNER, "owner-other-chat")
    with pytest.raises(PermissionError):
        drives.report(mind, {"input_id": own, "outcome": "satisfied"})
    with companion_scope(CompanionView(mind.engine, mind.scope, OTHER, "other-chat")):
        with pytest.raises(PermissionError):
            drives.report(mind, {"input_id": other, "outcome": "satisfied"})
    with companion_scope(CompanionView(mind.engine, mind.scope, OWNER, "owner-chat")):
        for wrong in (other, group, elsewhere, "missing"):
            with pytest.raises(PermissionError):
                drives.report(mind, {"input_id": wrong, "outcome": "satisfied"})
        declined = drives.report(mind, {"input_id": own, "outcome": "declined"})
        assert declined["intimacy"]["value"] == 80 and declined["intimacy"]["cooldown_until"]
        clock[0] += timedelta(hours=6, minutes=1)
        later = message(memory, mind, "own-2")
        with mind.engine.db.connect() as conn:
            assert drives.view(conn, mind.scope.key(), mind.clock(), private=True)["intimacy"]["cooldown_until"] is None
        done = drives.report(mind, {"input_id": later, "outcome": "satisfied"})
        assert done["state"] == "recorded" and done["intimacy"]["value"] == 10
        assert drives.report(mind, {"input_id": later, "outcome": "satisfied"})["state"] == "duplicate"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_drive_events WHERE kind LIKE 'intimacy-%'").fetchone()[0] == 2


def test_moment_block_speaks_to_each_audience_and_intimacy_only_to_owner(env):
    mind, memory, clock, _ = env
    energy.configure(mind, {"enabled": True, "night_penalty": 0})
    drives.configure(mind, {"enabled": True, "intimacy_initial": 90, "boredom_initial": 70})
    with companion_scope(CompanionView(mind.engine, mind.scope, OWNER, "owner-chat")):
        owner = moment.current(mind, mind.read())
        dims = affect_projection(mind.read())["dimensions"]
    assert owner.startswith(moment.HEADER) and "色色值 90/100" in owner and moment.INVITE in owner
    assert "report_private_moment" in owner and moment.BORED.format(value=70) in owner
    assert "flirtation" in dims
    for person, chat in ((OTHER, "other-chat"), (GROUP, "family")):
        with companion_scope(CompanionView(mind.engine, mind.scope, person, chat)):
            shown = moment.current(mind, mind.read()) + json.dumps(affect_projection(mind.read()), ensure_ascii=False)
        assert "精力" in shown and "无聊" in shown
        for private in ("色色值", "亲密", "report_private_moment", "flirtation"):
            assert private not in shown
    unscoped = moment.current(mind, mind.read())
    assert "色色值" not in unscoped
    with companion_scope(CompanionView(mind.engine, mind.scope, OWNER, "owner-chat")):
        energy.configure(mind, {"low_threshold": 90, "high_threshold": 95})
        tired = moment.current(mind, mind.read())
    # Tired: no invitation, the low-energy guidance instead (the value is still shown).
    assert moment.INVITE not in tired and "精力很低" in tired and "紧急" in tired


def test_tendencies_follow_the_largest_rises_and_energy_colours_them():
    dims = {"grievance": {"value": 24, "baseline": 10}, "longing": {"value": 86, "baseline": 55},
            "joy": {"value": 80, "baseline": 68}, "irritability": {"value": 18, "baseline": 12},
            "flirtation": {"value": 99, "baseline": 58}}
    family = moment.tendencies(dims)
    assert family[0].startswith("有点委屈") and not any(t.startswith("很想念") for t in family)
    assert moment.tendencies(dims, private=True)[0].startswith("很想念")
    tired = moment.tendencies(dims, "low")
    assert any(t.startswith("有点烦") for t in tired) and not any(t.startswith("挺开心") for t in tired)
    assert moment.tendencies({"grievance": {"value": 24, "baseline": 10, "needs_review": True}}) == []


def test_tired_defer_is_allowed_kept_as_unreplied_and_offered_back_once(env):
    mind, memory, clock, _ = env
    energy.configure(mind, {"enabled": True, "night_penalty": 0, "initial": 10})
    message(memory, mind, "late-1")
    message(memory, mind, "other-late", OTHER, "other-chat")
    choice = memory.habits.choose_reply({"input_id": "late-1", "action": "defer", "reason": "tired"})
    assert choice["resting"] is True and choice["desire_id"]
    with pytest.raises(Conflict):
        memory.habits.choose_reply({"input_id": "other-late", "action": "silent"})
    memory.habits.choose_reply({"input_id": "other-late", "action": "defer"})
    with companion_scope(CompanionView(mind.engine, mind.scope, OWNER, "owner-chat")):
        assert "休息时没回的消息" not in moment.current(mind, mind.read())  # still tired
        energy.configure(mind, {"low_threshold": 5})
        assert "late-1" in moment.current(mind, mind.read(), record=False)
        first = moment.current(mind, mind.read())
        assert moment.RESTED.format(ids="late-1") in first and "other-late" not in first
        assert "休息时没回的消息" not in moment.current(mind, mind.read())
    with companion_scope(CompanionView(mind.engine, mind.scope, OTHER, "other-chat")):
        assert moment.RESTED.format(ids="other-late") in moment.current(mind, mind.read())
    message(memory, mind, "rested")
    with pytest.raises(Conflict):
        memory.habits.choose_reply({"input_id": "rested", "action": "defer"})


def test_high_energy_brings_a_timed_wait_round_sooner(env):
    mind, memory, clock, source = env
    made = mind.manage_desire(DesireChange(command_id="wish", agent_version="life-test", expected_revision=mind.read()["revision"],
        evidence_ids=[source("wish")], action="create", kind="contact", strength=50, topic="t", content="c", completion="done",
        expires_at=(clock[0] + timedelta(days=2)).isoformat(), reason="test"))
    mind.manage_desire(DesireChange(command_id="wait", agent_version="life-test", expected_revision=mind.read()["revision"],
        evidence_ids=[source("wait")], action="wait", desire_id=made["desire_id"], wait_condition="time", retry_after_seconds=7200, reason="later"))
    clock[0] += timedelta(hours=1, minutes=1)
    assert mind.reconsider_contacts(owner_epoch="e")["state"] == "unchanged"
    energy.configure(mind, {"enabled": True, "night_penalty": 0, "initial": 100})
    assert mind.reconsider_contacts(owner_epoch="e")["resumed"] == [made["desire_id"]]


def test_reply_context_leads_with_the_moment_block(env):
    mind, memory, _, _ = env
    memory.configure({"context": True})
    energy.configure(mind, {"enabled": True, "night_penalty": 0})
    with companion_scope(CompanionView(mind.engine, mind.scope, OWNER, "owner-chat")):
        built = Contexts(mind).build("", purpose="chat", session="life-main", event_id="turn-1")
    envelope, block = built["rendered_text"].split("\n")[:2]
    assert envelope.startswith("共享记忆资料") and block == moment.HEADER
    assert "精力" in built["rendered_text"].split("\n")[2]


def test_without_the_memory_context_the_fallback_still_carries_the_moment_block(env):
    mind, _, _, _ = env
    from kin_mind.host import dispatch
    energy.configure(mind, {"enabled": True, "night_penalty": 0})
    config = {"root": str(mind.engine.db.path.parent), "scope": mind.scope.model_dump(), "agent_version": "life-test", "session_id": "life-main"}
    result = dispatch(config, "ingest", {"id": "fallback-1", "text": "synthetic fallback",
                                          "at": datetime.now(timezone.utc).isoformat(), "channel": "feishu"})
    assert result["memory_context"]["state"] == "disabled"
    assert result["moment"].startswith(moment.HEADER) and "精力" in result["moment"]


def test_family_state_reads_drop_the_intimacy_dimension_and_its_style(env, monkeypatch):
    from kin_mind import host
    from kin_mind.companion_host import dispatch_companion
    mind, _, _, _ = env
    state = {"dimensions": {"flirtation": {"value": 90}, "mood": {"value": 60}}, "interaction_style": {"band": "direct"},
             "affect_layers": {"undertone": {"leaning": {"flirtation": 80, "joy": 70}}, "lingering": {"dimension": "flirtation", "text": "x"}}}
    monkeypatch.setattr(host, "_dispatch", lambda *args: state)
    read = lambda person: dispatch_companion(mind.engine, {"scope": mind.scope.model_dump()}, "companion-host-call",
                                             {"person_id": person, "conversation_key": "chat:" + person, "action": "read", "request": {}})
    assert "flirtation" in read(OWNER)["dimensions"] and "interaction_style" in read(OWNER)
    for person in (OTHER, GROUP):
        shown = read(person)
        assert set(shown["dimensions"]) == {"mood"} and "interaction_style" not in shown
        assert shown["affect_layers"] == {"undertone": {"leaning": {"joy": 70}}, "lingering": None}


def test_high_energy_halves_the_pause_before_an_exploration_retry(env, tmp_path):
    from kin_mind.exploration import Explorations, EXPLORATION_RETRY_SECONDS
    mind, _, clock, source = env
    energy.configure(mind, {"enabled": True, "night_penalty": 0, "initial": 100})
    mind.manage_desire(DesireChange(command_id="explore", agent_version="life-test", expected_revision=mind.read()["revision"],
        evidence_ids=[source("explore")], action="create", content="Study the clock", topic="clock", kind="explore", strength=80,
        expires_at=(clock[0] + timedelta(days=1)).isoformat(), completion="A sourced result", reason="test"))

    def runner(executable, brief, directory, **kwargs):
        kwargs["on_started"]()
        return {"state": "failed", "reason": "native-run-incomplete", "attempt": 1, "partial": True}
    runner.reports_start = True
    Explorations(mind).run("unused", str(tmp_path / "runs"), "life-test", runner=runner)
    retry = next(d for d in mind.read()["desires"] if d["kind"] == "explore")["exploration_retry"]
    assert retry["not_before"] == (clock[0] + timedelta(seconds=EXPLORATION_RETRY_SECONDS / 2)).isoformat()
