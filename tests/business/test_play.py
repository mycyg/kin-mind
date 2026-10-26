"""Kin's interests and boredom-driven play (play.py), with synthetic data only.

Interests fade, grow and stay bounded, and an erase takes the ones grown from what it erases.
Boredom wakes the usual idle review -- once, and only while Kin is bored, rested, awake, idle and
under today's limits -- where Kin is shown the interests and may choose play, a lighter exploration
whose own word on how it went moves the interests. The migration lists interests once."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from kin_mind import energy, play
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisals
from kin_mind.codex_executor import codex_prompt
from kin_mind.continuity import ContinuityConfig, RhythmProposal
from kin_mind.erasure import ERASED
from kin_mind.exploration import Explorations
from kin_mind.memory import MemoryContinuity
from kin_mind.state import AffectiveEvent, Mind
from test_autonomous_plans import env  # noqa: F401 - the creation test's plan fixture
from test_main_session_review import native_provider

RECEIPT = {'native_turn_id': 'turn-1', 'native_session_id': 'same-main', 'model': 'gpt-6-astra', 'provider': 'custom',
           'reasoning': 'medium', 'usage': {}}


@pytest.fixture
def kin(tmp_path):
    clock = [datetime.now(timezone.utc)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-play"), clock=lambda: clock[0].isoformat())

    def source(key, namespace="kin-owner-input"):
        return mind.engine.receive(SourceInput(namespace=namespace, key=key, scope=mind.scope, text=key, authority="explicit",
                                               occurred_at=mind.clock(), extract=False,
                                               metadata={"role": "user", "host_event": "message"}))["id"]
    initial = source("initial")
    mind.initialize(agent_version="play-test", evidence_ids=[initial])
    mind.configure_continuity(ContinuityConfig(command_id="rhythm", agent_version="play-test", expected_revision=mind.read()["revision"],
                                               evidence_ids=[initial], features={"rhythm": True}, reason="test"))
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "idle": True, "operational_lanes": True, "semantic_actions": True})
    actions = ActionEvents(mind)
    actions.configure({"command_id": "policy", "agent_version": "play-test", "expected_revision": mind.read()["revision"],
                       "evidence_ids": [initial], "reason": "The owner allowed autonomous review"})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_action_events SET state='complete' WHERE kind='bootstrap'")
    # Local noon in the play timezone, so a day's count does not cross midnight; then a fresh message.
    clock[0] = (clock[0] + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=500000)
    source("hello")
    return mind, memory, actions, clock, source


def live(mind):
    with mind.engine.db.connect() as conn:
        return {e["topic"]: e for e in play.interests(conn, mind.scope.key(), mind.clock(), play.config(conn))}


def boredom_wakes(mind):
    with mind.engine.db.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM mind_action_events WHERE kind='idle-review' AND json_extract(data,'$.boredom') IS NOT NULL")]


def phase(mind, value, key):
    mind.record(AffectiveEvent(command_id=key, agent_version="play-test", expected_revision=mind.read()["revision"], subjective=True,
                               rhythm=RhythmProposal(phase=value, alertness=70, target=70, half_life_minutes=180)))


def test_interests_fade_grow_shrink_and_stay_bounded(kin):
    mind, _, _, clock, _ = kin
    scope, at = mind.scope.key(), mind.clock()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute(play.SCHEMA)
        play._add(conn, scope, at, play.config(conn), "天文", 50, "persona")
        play.after_activity(conn, mind, "天文", {"enjoyment": "fun"})
    assert live(mind)["天文"]["strength"] == 65 and live(mind)["天文"]["last_engaged_at"] == at
    # Two weeks untouched: half of it.
    clock[0] += timedelta(days=14)
    assert round(live(mind)["天文"]["strength"], 1) == 32.5
    with mind.engine.db.connect(write=True) as conn:
        # Named inside a longer topic it is the same interest; a dull run takes some off.
        play.after_activity(conn, mind, "天文和星座的来历", {"enjoyment": "meh"})
        # Fun on a new short topic makes one; a run may spark two more at most.
        play.after_activity(conn, mind, "折纸", {"enjoyment": "fun", "new_interests": ["苔藓", "旧地图", "火山"]})
        # Without a word from Kin an activity only touches what it names.
        play.after_activity(conn, mind, "纸雕灯", None)
    found = live(mind)
    assert round(found["天文"]["strength"], 1) == 22.5
    assert set(found) == {"天文", "折纸", "苔藓", "旧地图"} and found["折纸"]["source"] == "exploration"
    play.configure(mind, {"max_interests": 4})
    with mind.engine.db.connect(write=True) as conn:
        cfg = play.config(conn)
        # A full list keeps its stronger interests and lets a stronger one in for the weakest.
        assert play._add(conn, scope, mind.clock(), cfg, "星空摄影", 10, "exploration") is None
        assert play._add(conn, scope, mind.clock(), cfg, "星空摄影", 40, "exploration")
    assert set(live(mind)) == {"星空摄影", "折纸", "苔藓", "旧地图"}
    clock[0] += timedelta(days=120)
    assert live(mind) == {}


def test_an_erase_takes_an_interest_grown_from_what_it_erases(kin):
    mind, _, _, _, source = kin
    report = source("an exploration report", namespace="kin-exploration")
    with mind.engine.db.connect(write=True) as conn:
        play.after_activity(conn, mind, "苔藓", {"enjoyment": "fun", "new_interests": ["盆景"]}, [report])
    assert set(live(mind)) == {"苔藓", "盆景"}
    mind.engine.delete(report)
    assert live(mind) == {}
    with mind.engine.db.connect() as conn:
        assert {json.loads(row[0])["topic"] for row in conn.execute("SELECT data FROM mind_interests")} == {ERASED}


def bored(monkeypatch, value):
    """The drives module's boredom, as the host's own clock reads it."""
    from kin_mind import drives
    monkeypatch.setattr(drives, "view", lambda conn, scope, at, **_: {"enabled": True, "boredom": {"value": value, "last_engaging_at": None}})
    return drives


def test_boredom_wakes_the_idle_review_once_and_only_when_kin_is_up_for_it(kin, monkeypatch):
    mind, memory, actions, clock, _ = kin
    from kin_mind.host import dispatch
    config = {"root": str(mind.engine.db.path.parent), "scope": mind.scope.model_dump(), "agent_version": "play-test"}
    clock[0] += timedelta(hours=1)
    # An hour after the last message: 25, not bored.
    assert play.wake(mind, actions, memory) is None and play.configure(mind, {})["boredom"] == 25
    clock[0] += timedelta(hours=8)
    assert play.configure(mind, {})["ready"] is True
    phase(mind, "resting", "asleep")
    assert play.wake(mind, actions, memory) is None
    phase(mind, "awake", "up")
    energy.configure(mind, {"enabled": True, "initial": 30, "night_penalty": 0})
    assert play.wake(mind, actions, memory) is None
    play.configure(mind, {"energy_floor": 20})
    # Runs started with the last message, so boredom stays where it was.
    earlier = (clock[0] - timedelta(hours=9)).isoformat()
    Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES('explore_running',?,'running',?,'{}')", (mind.scope.key(), earlier))
    assert play.wake(mind, actions, memory) is None
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_explorations SET state='complete' WHERE id='explore_running'")
        for index in range(5):
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,'failed',?,'{}')", ("explore_" + str(index), mind.scope.key(), earlier))
    play.configure(mind, {"create_per_day": 0})
    assert play.configure(mind, {})["left_today"] == {"explore_or_play": 0, "create": 0}
    assert play.wake(mind, actions, memory) is None
    play.configure(mind, {"create_per_day": 3})
    # Up for something: the minute's review queues the usual idle review, once. The host's dispatch
    # reads the real clock, so boredom comes from the drives module here.
    bored(monkeypatch, 65.0)
    dispatch(config, "review-due", {})
    dispatch(config, "review-due", {})
    woken = boredom_wakes(mind)
    assert len(woken) == 1 and woken[0]["boredom"] == 65 and woken[0]["job_id"]
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT json_extract(data,'$.stimulus') FROM mind_appraisals WHERE id=?", (woken[0]["job_id"],)).fetchone()[0] == "idle-review"
    # After a committed review the next wake waits its interval.
    with mind.engine.db.connect(write=True) as conn:
        memory.commit_action(conn, [], "review-done", 600, {})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_action_events SET state='complete'")
    clock[0] += timedelta(minutes=119)
    assert play.wake(mind, actions, memory) is None
    clock[0] += timedelta(minutes=2)
    assert play.wake(mind, actions, memory) and len(boredom_wakes(mind)) == 2


def test_the_drives_module_boredom_is_used_when_it_is_on(kin, monkeypatch):
    mind, memory, actions, clock, _ = kin
    drives = bored(monkeypatch, 10.0)
    clock[0] += timedelta(hours=9)
    assert play.wake(mind, actions, memory) is None
    drives.view = lambda conn, scope, at: {"enabled": False}
    assert play.wake(mind, actions, memory)


def test_an_idle_review_is_shown_the_interests_and_may_choose_play(kin, tmp_path):
    mind, memory, actions, clock, _ = kin
    with mind.engine.db.connect(write=True) as conn:
        conn.execute(play.SCHEMA)
        play._add(conn, mind.scope.key(), mind.clock(), play.config(conn), "折纸", 50, "persona")
    clock[0] += timedelta(hours=9)
    assert play.wake(mind, actions, memory)
    jobs = Appraisals(mind)
    actions.drain(jobs)
    frames = []

    def exchange(request):
        frames.append(request)
        return {'state': 'complete', 'receipt': RECEIPT, 'result': {
            'reason': '好闲，想出去逛逛。',
            'wishes': [{'content': '去看看别人折的纸艺作品', 'topic': '折纸', 'kind': 'explore', 'exploration_target': 'play',
                        'strength': 70, 'ttl_hours': 12, 'completion': '逛完写几句感受'}],
            'next_move': {'move': 'quiet', 'reason': '', 'grounds': [], 'wish_ref': None, 'step_ref': None, 'alternative': ''}}}
    assert jobs.run_one(native_provider(mind, exchange), lane="action")["state"] == "complete"
    shown = frames[0]["context"]["autonomy_context"]["play"]
    assert shown["bored"] is True and shown["boredom"] == 65 and shown["left_today"] == {"explore_or_play": 6, "create": 3}
    assert shown["interests"] == [{"topic": "折纸", "strength": 49, "source": "persona", "days_since": None}]
    assert "exploration_target 写 play" in frames[0]["contract"]
    wish = next(d for d in mind.read()["desires"] if d["kind"] == "explore")
    assert wish["exploration_target"] == "play"
    seen = {}

    def runner(executable, brief, directory, **kwargs):
        seen.update(brief=brief, budget=kwargs["budget_seconds"])
        return {"state": "complete", "partial": False, "attempt": 1, "result": {
            "summary": "看了好多纸鹤，很开心", "findings": ["有人用一张纸折出整条龙"], "sources": [], "open_questions": [],
            "suggested_share": None, "enjoyment": "fun", "new_interests": ["纸雕"]}}
    # The synthetic native receipt is no verified decision; without a policy the wish runs as it is.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_state SET data=json_set(data,'$.action_policy',NULL)")
    result = Explorations(mind).run("codex", str(tmp_path / "explore"), "v1", runner=runner)
    assert result["state"] == "complete"
    # A lighter run: its own budget and the play guide.
    assert seen["budget"] == 480 and seen["brief"]["mode"] == "play"
    assert play.PLAY_GUIDE in codex_prompt(seen["brief"], budget_seconds=480)
    assert play.PLAY_GUIDE not in codex_prompt({**seen["brief"], "mode": None}, budget_seconds=480)
    found = live(mind)
    assert round(found["折纸"]["strength"]) == 64 and found["纸雕"]["source"] == "exploration"


def test_play_waits_for_the_next_day_once_todays_explorations_are_used(kin):
    mind, memory, actions, clock, source = kin
    from test_kin_mind import wish
    wish(mind, source, "play", kind="explore", exploration_target="play", content="随便逛逛", topic="手工")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_state SET data=json_set(data,'$.action_policy',NULL)")
    Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        for index in range(6):
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,'failed',?,'{}')", ("explore_" + str(index), mind.scope.key(), mind.clock()))
    assert actions.exploration_candidate()["reason"] == "no-exploration-intent"
    clock[0] += timedelta(days=1)
    assert actions.exploration_candidate()["state"] == "ready"


def test_the_migration_lists_interests_once(kin):
    mind, _, _, clock, source = kin
    scope = mind.scope.model_dump()
    (mind.engine.db.root / "persona-policy.json").write_text(json.dumps(
        {"scope": scope, "core": "【MY_PERSONA_LOAD】合成角色，喜欢折纸和看云。【/MY_PERSONA_LOAD】", "voice": "", "maintenance": ""}))
    report = source("a report", namespace="kin-exploration")
    Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        for index, tags in enumerate((["云", "气象"], ["云"])):
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,'complete',?,?)", ("explore_" + str(index), mind.scope.key(), mind.clock(),
                         json.dumps({"source_id": report, "result": {"topic_tags": tags}})))
    asked = []

    class Lister:
        def structured(self, name, schema, system, context, **kwargs):
            asked.append(context)
            return schema.model_validate({"interests": [{"topic": "折纸", "source": "persona"}, {"topic": "看云", "source": "persona"},
                                                        {"topic": "某个人", "source": "relation"}]}), {}
    assert play.seed(mind, Lister()) == {"state": "seeded", "counts": {"persona": 2, "chat": 0, "exploration": 2}}
    assert "折纸" in asked[0]["persona"] and asked[0]["own_preferences"] == []
    found = live(mind)
    assert set(found) == {"折纸", "看云", "云", "气象"} and found["云"]["strength"] == 40 and found["云"]["source_ids"] == [report]
    assert play.seed(mind, Lister()) == {"state": "seeded-already"} and len(asked) == 1


def test_a_completed_creation_touches_the_interests_it_was_about(env, tmp_path):
    from kin_mind.creation import accept_result
    from test_creation import Review, ready
    mind, _, run, result, config = ready(env, tmp_path)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute(play.SCHEMA)
        play._add(conn, mind.scope.key(), mind.clock(), play.config(conn), "clock", 30, "persona")
    assert accept_result(mind, config, {"run_id": run["id"], "owner": "worker", "fence": 1, "result": result}, Review())["state"] == "completed"
    assert live(mind)["clock"]["strength"] == 35 and live(mind)["clock"]["last_engaged_at"] == mind.clock()
