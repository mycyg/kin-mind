"""Kin starts things again (2026-09-27). From 9/24 she made almost no wishes, so for days nothing was
said to the owner and nothing was looked into or made, though everything after a wish worked. Each
cause is held here:

1. the invitation to start something at an idle review was in the main-session prompt only, and
   assessments run on DeepSeek;
2. an idle review re-rated two drives, was never asked whether to reach out, and was not shown how
   long it had been quiet;
3. one refused wish rolled back every wish of its proposal, a valid contact wish among them;
4. the owner's word on contact frequency reached an assessment only through one expression style,
   which changed on 9/21;
6. a failed exploration waited for new evidence that never came;
7. a decided share sat unsent until it expired, without a word.

The contact draft's schema and parser (5) are held by owner-host.test.mjs and the host's
contact-draft-schema.test.mjs."""
import json
from datetime import timedelta

import pytest

from kin_mind import appraisal as A
from kin_mind import initiative
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals, NextMove, Wish, WishUpdate
from kin_mind.continuity import ConcernChange, ContinuityConfig
from kin_mind.exploration import EXPLORATION_RETRY_SECONDS, Explorations
from kin_mind.memory import MemoryContinuity
from kin_mind.state import DesireChange
from test_kin_mind import FakeReviewer, wish
from test_main_session_review import native_provider

pytest_plugins = ('test_kin_mind',)

RECEIPT = {'native_turn_id': 'turn-1', 'native_session_id': 'same-main', 'model': 'gpt-6-astra', 'provider': 'custom',
           'reasoning': 'medium', 'usage': {}}


def world(setup):
    """Operational lanes, semantic actions and an action policy, as production runs; the bootstrap
    review is taken as done."""
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "idle": True, "operational_lanes": True, "semantic_actions": True})
    actions = ActionEvents(mind)
    actions.configure({"command_id": "policy", "agent_version": "synthetic-v1", "expected_revision": mind.read()["revision"],
                       "evidence_ids": [source("policy")], "reason": "The owner allowed autonomous review"})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_action_events SET state='complete' WHERE kind='bootstrap'")
    return memory, actions


def idle_review(mind, memory, actions, clock):
    """The next idle review, queued and handed to the appraisal queue."""
    clock[0] += timedelta(minutes=30)
    assert memory.queue_idle(actions)
    jobs = Appraisals(mind)
    actions.drain(jobs)
    return jobs


# --- 1. Every provider is invited ----------------------------------------------------------------

def test_every_provider_is_told_an_idle_review_may_start_something():
    """The invitation is in the contract DeepSeek is given, and the main session hears it once."""
    deepseek = A.DeepSeek.__new__(A.DeepSeek)
    system = A.DeepSeek._system(deepseek, {"stimulus": "idle-review", "operational_only": True}, None)
    assert "空闲评估本身就是自主起念的机会" in system and "想联系、探索或创作都可以直接提出" in system
    assert "每次idle-review都对主动联系作一个明确决定" in system
    native = A.NativeReview.__new__(A.NativeReview)
    assert A.NativeReview._system(native, {"stimulus": "idle-review"}, None).count("自主起念的机会") == 1


# --- 2. An idle review states a contact decision, and sees how long it has been quiet ---------------

def test_an_idle_review_must_answer_with_a_contact_decision_and_is_shown_the_silence(setup):
    mind, source, clock = setup
    # Thirty hours ago: a contact wish that never went out. Six hours later a contact was sent, and
    # ten hours in an exploration failed with a code, and another with only words.
    wish(mind, source, "stale", content="讲讲那只猫", expires_at=(clock[0] + timedelta(days=5)).isoformat())
    made = mind.read()["desires"][0]["id"]
    Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        at = lambda hours: (clock[0] + timedelta(hours=hours)).isoformat()
        conn.execute("INSERT INTO mind_contacts VALUES(?,?,?,?)", ("kin-mind-sent", mind.scope.key(), "accepted",
                     json.dumps({"updated_at": at(6), "desire_id": "desire_earlier"})))
        for key, hours, data in (("explore_coded", 10, {"desire_id": "desire_x", "reason": "native-run-incomplete"}),
                                 ("explore_worded", 11, {"desire_id": "desire_y", "reason": "the run broke down halfway"})):
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (key, mind.scope.key(), "failed", at(hours), json.dumps(data)))
    clock[0] += timedelta(hours=30)
    memory, actions = world(setup)
    jobs = idle_review(mind, memory, actions, clock)
    frames = []

    def exchange(request):
        frames.append(request)
        return {'state': 'complete', 'receipt': RECEIPT, 'result': {
            'reason': '今天先安静地想想。',
            'next_move': {'move': 'rest', 'reason': '她在赶论文，晚点再说', 'grounds': [], 'wish_ref': None, 'step_ref': None, 'alternative': ''}}}
    result = jobs.run_one(native_provider(mind, exchange), lane="action")
    assert result["state"] == "complete", result
    schema = frames[0]["schema"]
    # The move is the one field it must answer, and a strict fork cannot answer it with null.
    assert "next_move" in schema["required"] and schema["properties"]["next_move"] == {"$ref": "#/$defs/NextMove"}
    assert "idle-review 时 next_move 必填" in frames[0]["contract"]
    facts = frames[0]["context"]["autonomy_context"]["initiative_facts"]
    assert facts["hours_since_last_wish"] == {"contact": 30.5, "explore": None, "create": None}
    assert facts["hours_since_last_contact_sent"] == 24.5
    assert facts["idle_reviews_since_last_wish"] == 1
    assert facts["last_exploration"] == {"state": "failed", "hours_ago": 19.5} and facts["last_creation"] is None
    # A failure is named by its code, never by its words.
    assert facts["recent_failed_explorations"] == [
        {"exploration_id": "explore_worded", "state": "failed", "hours_ago": 19.5, "desire_id": "desire_y"},
        {"exploration_id": "explore_coded", "state": "failed", "hours_ago": 20.5, "desire_id": "desire_x", "code": "native-run-incomplete"}]
    assert facts["contact_wishes_unsent_a_day"] == [{"desire_id": made, "status": "wanted", "hours_since_made": 30.5}]
    assert result["result"]["contact_decision"] == {"state": "not-now", "move": "rest"}
    # The same for the tool schema DeepSeek is given: the move is required and takes no null.
    tool = A.appraisal_schema(True, False, ("next_move",), required=("next_move",))
    assert "next_move" in tool["required"] and tool["properties"]["next_move"] == {"$ref": "#/$defs/NextMove"}
    assert "next_move" not in A.appraisal_schema(True, False, ("next_move",)).get("required", [])


@pytest.mark.parametrize("answer,expected", [
    ("contact", "contact"), ("nothing", "missing"), ("reply-without-wish", "missing")])
def test_what_an_idle_review_decided_about_contacting_is_kept_with_its_result(setup, answer, expected):
    mind, source, clock = setup
    memory, actions = world(setup)
    jobs = idle_review(mind, memory, actions, clock)
    proposal = {
        "contact": Appraisal(reason="想她了", next_move=NextMove(move="reply", reason="想问问她实验顺不顺利"),
                             wishes=[Wish(content="问问她今天的实验顺不顺利", topic="实验", kind="contact", strength=70,
                                          ttl_hours=12, completion="发出一条问候")]),
        "nothing": Appraisal(reason="没有新想法"),
        "reply-without-wish": Appraisal(reason="想说点什么", next_move=NextMove(move="reply", reason="想她")),
    }[answer]
    result = jobs.run_one(FakeReviewer(proposal), lane="action")
    decision = result["result"]["contact_decision"]
    assert decision["state"] == expected
    if expected == "contact":
        assert decision["desire_ids"] == [d["id"] for d in mind.read()["desires"] if d["kind"] == "contact"]
    with mind.engine.db.connect() as conn:
        counted = conn.execute("SELECT data FROM metrics WHERE name='idle_contact_decision'").fetchall()
    assert [json.loads(row[0])["state"] for row in counted] == [expected]


def test_an_assessment_without_an_idle_review_is_asked_for_no_contact_decision(setup):
    mind, source, _ = setup
    jobs = Appraisals(mind)
    jobs.enqueue([source("owner-says-hi")], "synthetic-v1")
    frames = []

    def exchange(request):
        frames.append(request)
        return {'state': 'complete', 'result': {'reason': '她来打招呼了'}, 'receipt': RECEIPT}
    result = jobs.run_one(native_provider(mind, exchange))
    assert result["state"] == "complete" and "contact_decision" not in result["result"]
    # Offered as before: a strict fork may still answer it with null.
    assert {"type": "null"} in frames[0]["schema"]["properties"]["next_move"]["anyOf"]


# --- 3. One refused wish is refused alone, and asked again -----------------------------------------

def concern_under_review(mind, source):
    """A concern Kin inferred with low confidence: nothing may rest on it until it is reviewed."""
    mind.configure_continuity(ContinuityConfig(command_id="continuity", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("continuity")], features={"concerns": True}, reason="test"))
    return mind.manage_concern(ConcernChange(command_id="guess", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("hint")], action="create", key="tired", kind="care", content="她可能有点累", topic="作息",
        intensity=40, basis="inferred", confidence=0.5, reason="只是猜测"))["concern_id"]


def test_one_refused_wish_leaves_its_sibling_standing_and_is_asked_again_with_its_code(setup):
    """2026-09-27 06:24: a valid contact wish went because a sibling explore wish linked a concern under review."""
    mind, source, _ = setup
    concern = concern_under_review(mind, source)
    contact = Wish(content="跟她说今天看到的晚霞", topic="晚霞", kind="contact", strength=62, ttl_hours=12, completion="发出晚霞的消息")
    explore = Wish(content="查查晚霞为什么这么红", topic="晚霞", kind="explore", strength=55, ttl_hours=24,
                   completion="带回有来源的解释", concern_ids=[concern])
    jobs = Appraisals(mind)
    jobs.enqueue([source("sunset")], "synthetic-v1")
    result = jobs.run_one(FakeReviewer(Appraisal(reason="看到晚霞", wishes=[contact, explore])))
    assert result["state"] == "complete", result
    assert [(d["kind"], d["status"]) for d in mind.read()["desires"]] == [("contact", "wanted")]
    refused = [(r["section"], r["index"], r["code"]) for r in result["result"]["rejected_sections"]]
    assert refused == [("wishes", 1, "reference-needs-review")]
    follow_up = result["result"]["follow_up_id"]
    with mind.engine.db.connect() as conn:
        queued = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (follow_up,)).fetchone()[0])
    assert queued["stimulus"] == A.FOLLOW_UP
    assert [(r["section"], r["index"], r["code"]) for r in queued["section_review"]["rejected_sections"]] == refused
    # The follow-up restates the refused wish alone, without the link still under review, and it stands.
    again = jobs.run_one(FakeReviewer(Appraisal(reason="先不关联那件心事", wishes=[explore.model_copy(update={"concern_ids": []})])),
                         job_id=follow_up)
    assert again["state"] == "complete" and "follow_up_id" not in again["result"]
    assert sorted((d["kind"], d["status"]) for d in mind.read()["desires"]) == [("contact", "wanted"), ("explore", "wanted")]


def test_one_refused_wish_update_leaves_the_others_applied(setup):
    mind, source, _ = setup
    concern = concern_under_review(mind, source)
    wish(mind, source, "first", content="Share the photo")
    wish(mind, source, "second", content="Ask about the trip")
    first, second = (d["id"] for d in mind.read()["desires"])
    jobs = Appraisals(mind)
    jobs.enqueue([source("later")], "synthetic-v1")
    result = jobs.run_one(FakeReviewer(Appraisal(reason="晚点再说", wish_updates=[
        WishUpdate(desire_id=first, action="link", concern_ids=[concern], reason="和她累不累有关"),
        WishUpdate(desire_id=second, action="wait", wait_condition="owner_reply", reason="等她回来")])))
    assert result["state"] == "complete", result
    statuses = {d["id"]: d["status"] for d in mind.read()["desires"]}
    assert statuses == {first: "wanted", second: "waiting"}
    assert [(r["section"], r["index"]) for r in result["result"]["rejected_sections"]] == [("wish_updates", 0)]
    assert result["result"]["follow_up_id"]


# --- 4. The owner's word on contact frequency, whatever the style -----------------------------------

def test_the_owner_word_on_contact_frequency_reaches_the_assessment_whatever_the_style(setup):
    mind, source, _ = setup
    said = source("frequency", text="主动联系可以更频繁一点")
    # The expression style the owner approved later is the contextual one, which carries no preference.
    mind.configure_behavior({"command_id": "style", "agent_version": "synthetic-v1", "expected_revision": mind.read()["revision"],
                             "evidence_ids": [source("style")], "style": "contextual", "reason": "按语境表达"})
    assert mind.read().get("interaction_style") is None
    assert mind.adopt_contact_frequency([said], agent_version="synthetic-v2")["state"] == "adopted"
    frequency = mind.read()["contact"]["frequency"]
    assert frequency["text"] == "主动联系可以更频繁一点" and frequency["needs_review"] is False
    assert mind.adopt_contact_frequency([said], agent_version="synthetic-v2") == {"state": "unchanged"}
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_events WHERE kind='contact-frequency'").fetchone()[0] == 1
    # The assessment is shown it, and told how to read it.
    shown = A.appraisal_context({"state": mind.read(), "new_evidence": [], "stimulus": "idle-review"})
    assert shown["state"]["contact"]["frequency"]["text"] == "主动联系可以更频繁一点"
    assert "state.contact.frequency" in A.SYSTEM and "无论表达风格是什么" in A.SYSTEM
    # A newer word from the same place: the old one is not shown as current.
    source("frequency", text="最近少联系一点", version="2")
    assert "text" not in mind.read()["contact"]["frequency"] and mind.read()["contact"]["frequency"]["needs_review"]


def test_contact_frequency_is_only_ever_the_owner_word(setup):
    mind, source, _ = setup
    revision = mind.read()["revision"]
    guessed = source("guess", text="她应该想要更频繁", authority="model")
    assert mind.adopt_contact_frequency([guessed], agent_version="v")["state"] == "refused"
    assert mind.adopt_contact_frequency([], agent_version="v") == {"state": "unconfigured"}
    assert mind.adopt_contact_frequency(["src_" + "0" * 32], agent_version="v")["state"] == "refused"
    assert mind.read()["revision"] == revision and "frequency" not in mind.read()["contact"]


def test_the_host_start_up_adopts_the_frequency_the_config_names_once(tmp_path):
    from eventmem.core import Engine
    from eventmem.core.models import Scope, SourceInput
    from kin_mind.host import dispatch
    from kin_mind.state import Mind
    engine, scope = Engine(tmp_path / "memory"), Scope(persona="synthetic-frequency")
    mind = Mind(engine, scope)
    receive = lambda key, text: engine.receive(SourceInput(namespace="test", key=key, scope=scope, text=text, authority="explicit"))["id"]
    mind.initialize(agent_version="test-v1", evidence_ids=[receive("configuration", "configuration")])
    said = receive("frequency", "主动联系可以更频繁")
    config = {"root": str(tmp_path / "memory"), "scope": scope.model_dump(), "agent_version": "test-v2",
              "contact_frequency_evidence_ids": [said]}
    assert dispatch(config, "recover", {})["state"] == "recovered"
    dispatch(config, "recover", {})
    assert mind.read()["contact"]["frequency"]["text"] == "主动联系可以更频繁"
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_events WHERE kind='contact-frequency'").fetchone()[0] == 1
    # A config that names nothing changes nothing.
    assert dispatch({**config, "contact_frequency_evidence_ids": None}, "recover", {})["state"] == "recovered"
    # Her later word, set through the operator's route, stands over what the config still names.
    later = receive("frequency-later", "每天主动找我一两次就好")
    dispatch(config, "configure-contact-frequency", {"command_id": "later", "agent_version": "test-v2",
             "expected_revision": mind.read()["revision"], "evidence_ids": [later], "reason": "她又说了一次"})
    dispatch(config, "recover", {})
    assert mind.read()["contact"]["frequency"]["text"] == "每天主动找我一两次就好"
    assert mind.adopt_contact_frequency([said], agent_version="test-v2") == {"state": "kept"}
    # What is deleted takes its words out of the state with it.
    engine.delete(later)
    assert "text" not in mind.read()["contact"]["frequency"]
    with engine.db.connect() as conn:
        assert "每天主动找我" not in json.dumps(mind._load(conn), ensure_ascii=False)


# --- 6. A failed exploration is tried once more, then settled --------------------------------------

def explore_wish(mind, source, clock):
    """An explore wish on the owner's word, with operational lanes as production runs them: an
    exploration's own result review holds no wish."""
    MemoryContinuity(mind).configure({"records": True, "semantic": True, "operational_lanes": True})
    said = source("sunset", text="晚霞为什么这么红？")
    wish(mind, source, "explore", kind="explore", content="查查晚霞为什么这么红", completion="带回有来源的解释",
         evidence_ids=[said], expires_at=(clock[0] + timedelta(days=3)).isoformat())
    return mind.read()["desires"][0]["id"], said


def failing(executable, brief, directory, **kwargs):
    return {"state": "failed", "reason": "native-run-incomplete", "attempt": 1, "partial": True}


def test_a_failed_exploration_is_tried_once_more_at_the_next_idle_window_then_settled(setup, tmp_path):
    mind, source, clock = setup
    desire, _ = explore_wish(mind, source, clock)
    explorer, actions = Explorations(mind), ActionEvents(mind)
    assert explorer.run("codex", str(tmp_path / "explore"), "v1", runner=failing)["state"] == "failed"
    after_one = {d["id"]: d for d in mind.read()["desires"]}[desire]
    assert after_one["status"] == "wanted" and after_one["exploration_retry"]["failures"] == 1
    assert after_one["exploration_retry"]["code"] == "native-run-incomplete"
    pending = actions.exploration_candidate()
    assert pending["state"] == "waiting" and pending["reason"] == "exploration-retry-pending"
    clock[0] += timedelta(seconds=EXPLORATION_RETRY_SECONDS + 60)
    assert actions.exploration_candidate()["state"] == "ready"
    assert explorer.run("codex", str(tmp_path / "explore"), "v1", runner=failing)["state"] == "failed"
    settled = {d["id"]: d for d in mind.read()["desires"]}[desire]
    assert settled["status"] == "abandoned" and "after its retry" in settled["reason"] and "native-run-incomplete" in settled["reason"]
    assert actions.exploration_candidate()["reason"] == "no-exploration-intent"


def test_a_retried_exploration_that_finds_something_completes(setup, tmp_path):
    mind, source, clock = setup
    desire, said = explore_wish(mind, source, clock)
    Explorations(mind).run("codex", str(tmp_path / "explore"), "v1", runner=failing)
    clock[0] += timedelta(seconds=EXPLORATION_RETRY_SECONDS + 60)

    def found(executable, brief, directory, **kwargs):
        return {"state": "complete", "partial": False, "attempt": 1,
                "result": {"summary": "晚霞偏红是因为散射", "findings": ["瑞利散射让短波长先散掉"],
                           "sources": [{"url": "memory://" + said, "title": "Owner note"}], "open_questions": [], "suggested_share": None}}
    assert Explorations(mind).run("codex", str(tmp_path / "explore"), "v1", runner=found)["state"] == "complete"
    assert {d["id"]: d for d in mind.read()["desires"]}[desire]["status"] == "completed"


# --- 7. A contact wish unsent for a day is put to Kin again -----------------------------------------

def reviews(mind):
    with mind.engine.db.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM mind_action_events WHERE kind='wish-review' ORDER BY created_at,id").fetchall()]


def test_a_contact_wish_unsent_a_day_is_put_to_kin_again_once_a_day(setup):
    mind, source, clock = setup
    wish(mind, source, "share", content="把晚霞的发现讲给她听", expires_at=(clock[0] + timedelta(days=4)).isoformat())
    desire = mind.read()["desires"][0]["id"]
    actions = ActionEvents(mind)
    actions.crossings()
    assert reviews(mind) == []
    clock[0] += timedelta(hours=25)
    actions.crossings()
    actions.crossings()
    asked = reviews(mind)
    assert [r["desire_id"] for r in asked] == [desire] and "without being sent" in asked[0]["reason"]
    assert initiative.facts(mind)["contact_wishes_unsent_a_day"][0]["desire_id"] == desire
    clock[0] += timedelta(hours=24)
    actions.crossings()
    assert [r["desire_id"] for r in reviews(mind)] == [desire, desire]


def test_her_own_wait_until_later_stands_and_a_sent_wish_is_never_asked_about(setup):
    mind, source, clock = setup
    wish(mind, source, "later", content="晚点再讲", expires_at=(clock[0] + timedelta(days=4)).isoformat())
    desire = mind.read()["desires"][0]["id"]
    clock[0] += timedelta(hours=20)
    mind.manage_desire(DesireChange(command_id="wait", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
                                    evidence_ids=[source("wait")], action="wait", desire_id=desire, wait_condition="time",
                                    retry_after_seconds=3 * 86400, reason="等周末再说"))
    clock[0] += timedelta(hours=10)
    ActionEvents(mind).crossings()
    assert reviews(mind) == []
    wish(mind, source, "sent", content="已经发了的", expires_at=(clock[0] + timedelta(days=4)).isoformat())
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        sent = next(d for d in state["desires"].values() if d["content"] == "已经发了的")
        sent.update(status="completed", delivery={"state": "accepted"})
        mind._save(conn, state)
    clock[0] += timedelta(hours=30)
    ActionEvents(mind).crossings()
    assert all(r["desire_id"] != sent["id"] for r in reviews(mind))
