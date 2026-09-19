"""Rules that used to be literals in the code: the fixed score gates, the review ceiling, the
motivation range, the words a recall looks for, the envelope openings, and the places a wish used
to be dropped or left waiting for ever.

Synthetic replays only: an injected clock and scripted providers. No model or network call.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from test_appraisal_sections import CONTEXT, UNCHANGED_REQUEST, Recorded
from test_exploration_cadence import activate, legacy_gates
from test_kin_mind import event, setup, wish  # noqa: F401 - the fixture and its helpers
from test_memory_continuity import system  # noqa: F401 - a mind with records and semantics
from test_plan_review_loop import RECEIPT, Env, Reviewer

from eventmem.core.db import Conflict
from eventmem.core.read_policy import (
    HOST_PREFIXES,
    configure_envelopes,
    envelope_prefixes,
    host_envelope,
)
from kin_mind.actions import ActionEvents
from kin_mind.adaptive_recall import GENERIC_TERMS, owner_names, owner_question
from kin_mind.appraisal import (
    REVIEW_MAX_MINUTES,
    REVIEW_REST_MAX_MINUTES,
    REVIEW_WINDOW_PROMPT,
    SYSTEM,
    Appraisal,
    Appraisals,
    Wish,
    appraisal_schema,
)
from kin_mind.dialogue import is_public_dialogue
from kin_mind.memory import MemoryContinuity
from kin_mind.procedures import Procedures
from kin_mind.profile import DIMENSIONS
from kin_mind.rhythm import at_rest, quiet_hours
from kin_mind.state import Mind, Motivation


def saved_profile(mind):
    with mind.engine.db.connect() as conn:
        return json.loads(conn.execute("SELECT data FROM mind_state WHERE scope=?", (mind.scope.key(),)).fetchone()[0])["profile"]


def action_events(mind, kind):
    with mind.engine.db.connect() as conn:
        return [{"id": r["id"], **json.loads(r["data"])} for r in conn.execute(
            "SELECT id,data FROM mind_action_events WHERE scope=? AND kind=? ORDER BY created_at,id", (mind.scope.key(), kind))]


# --- the fixed score gates ------------------------------------------------------------------

def test_a_ready_wish_is_no_longer_held_back_by_a_score(setup):
    mind, source, _ = setup
    wish(mind, source)
    mind.record(event(mind, source, "quiet", {"initiative": 30}))
    candidate = mind.contact_candidate()
    assert candidate["eligible"] and candidate["reason"] == "draft-required"
    assert mind.claim_contact(owner_epoch="owner-1")["state"] == "drafting"


def test_the_score_gates_come_back_exactly_as_they_were(setup):
    mind, source, _ = setup
    legacy_gates(mind)
    wish(mind, source)
    mind.record(event(mind, source, "quiet", {"initiative": 30}))
    candidate = mind.contact_candidate()
    assert not candidate["eligible"] and candidate["reason"] == "below-threshold"
    assert candidate["initiative"] == 30
    with pytest.raises(Conflict):
        mind.claim_contact(owner_epoch="owner-1")
    mind.record(event(mind, source, "ready", {"initiative": 90}))
    assert mind.contact_candidate()["eligible"]


def test_the_stored_threshold_stays_a_number_either_way(setup):
    mind, source, _ = setup
    assert saved_profile(mind)["contact"]["threshold"] == 75
    legacy_gates(mind)
    ActionEvents(mind).configure({"command_id": "policy", "agent_version": "synthetic-v1",
        "expected_revision": mind.read()["revision"], "evidence_ids": [source("policy")],
        "reason": "The owner asks for semantic decisions"})
    stored = saved_profile(mind)["contact"]["threshold"]
    assert type(stored) is int and stored == 75
    assert mind.read()["contact"]["threshold"] == 75


def test_the_model_is_told_scores_are_context_and_legacy_thresholds_are_explicit(monkeypatch):
    guidance = DIMENSIONS["initiative"]["expression"]
    assert "当前语义决策" in guidance and "既有授权" in guidance and "显式旧版模式" in guidance
    assert "达到阈值" not in guidance
    api = Recorded(monkeypatch)
    body, _ = api.request({**CONTEXT, "definitions": DIMENSIONS})
    rendered = json.dumps(body, ensure_ascii=False)
    assert guidance in rendered and "达到阈值后请求有依据的联系" not in rendered


def test_a_crossing_needs_the_gates_that_define_it(setup):
    mind, source, clock = setup
    actions, _ = activate(mind, source)
    clock[0] += timedelta(minutes=21)
    assert actions.crossings() == []
    legacy_gates(mind)
    assert len(actions.crossings()) == 1


def test_a_low_curiosity_no_longer_answers_for_exploration(setup):
    mind, source, _ = setup
    actions, _ = activate(mind, source)
    mind.record(event(mind, source, "incurious", {"curiosity": 20}))
    assert actions.exploration_candidate()["reason"] == "no-exploration-intent"
    legacy_gates(mind)
    waiting = actions.exploration_candidate()
    assert waiting["reason"] == "curiosity-below-threshold-or-stale" and waiting["curiosity"] == 20


# --- a wish whose decision was made under an earlier version ---------------------------------

def decided_wish(env, *, version=None):
    """One contact wish carrying a decision receipt, committed the way an appraisal commits it."""
    jobs = Appraisals(env.mind, exploration_capabilities={"version": version or env.version})
    jobs.enqueue([env.source("something-happened")], env.version)
    result = jobs.run_one(Reviewer(lambda shown, context: Appraisal(reason="A thought worth sharing",
        wishes=[Wish(content="Tell the owner about the clock", topic="the clock", kind="contact",
                     strength=70, ttl_hours=48, completion="The owner has heard it")])))
    assert result["state"] == "complete", result
    return env.mind.read()["desires"][0]


def allow_actions(env):
    env.actions.configure({"command_id": "policy", "agent_version": env.version,
        "expected_revision": env.mind.read()["revision"], "evidence_ids": [env.initial],
        "reason": "The owner authorises autonomous contact"})


def test_a_wish_decided_under_an_earlier_version_is_asked_about_once(tmp_path):
    env = Env(tmp_path)
    allow_actions(env)
    desire = decided_wish(env)
    assert desire["decision_receipt"]["agent_version"] == "planning-v1"
    assert env.actions.review_unselected() is None and action_events(env.mind, "wish-review") == []
    env.upgrade("planning-v2")
    env.actions.review_unselected()
    asked = action_events(env.mind, "wish-review")
    assert [a["desire_id"] for a in asked] == [desire["id"]]
    assert asked[0]["agent_version"] == "planning-v2"
    # Once per wish and version: a host tick every minute does not ask again.
    env.actions.review_unselected()
    env.actions.review_unselected()
    assert len(action_events(env.mind, "wish-review")) == 1
    # A further version is a further question, not a loop inside one.
    env.upgrade("planning-v3")
    env.actions.review_unselected()
    assert len(action_events(env.mind, "wish-review")) == 2


def test_the_lagging_wish_is_dropped_silently_with_the_switch_off(tmp_path):
    env = Env(tmp_path)
    allow_actions(env)
    env.memory.configure({"wish_version_review": False})
    decided_wish(env)
    env.upgrade("planning-v2")
    env.actions.review_unselected()
    assert action_events(env.mind, "wish-review") == []


# --- the motivation half-life ------------------------------------------------------------------

@pytest.mark.parametrize("minutes", [10, 20, 45, 180, 720])
def test_a_motivation_may_last_from_ten_minutes_to_twelve_hours(minutes):
    assert Motivation(target=60, half_life_minutes=minutes, reason="A sourced impulse").half_life_minutes == minutes


@pytest.mark.parametrize("minutes", [9, 721, 0, -20])
def test_a_motivation_outside_the_range_is_refused_by_the_schema(minutes):
    with pytest.raises(ValidationError):
        Motivation(target=60, half_life_minutes=minutes, reason="A sourced impulse")


def test_a_stored_half_life_is_still_a_plain_number(setup):
    mind, source, _ = setup
    mind.record(event(mind, source, "impulse", {"initiative": 40}).model_copy(
        update={"motivations": {"initiative": Motivation(target=70, half_life_minutes=10, reason="A passing impulse")}}))
    stored = mind.read()["dimensions"]["initiative"]
    assert stored["half_life_hours"] == 10 / 60
    assert stored["motivation"]["half_life_minutes"] == 10


# --- the review ceiling while the owner is asleep -----------------------------------------------

NIGHT = datetime(2026, 9, 14, 18, tzinfo=timezone.utc)   # 02:00 in the owner's own timezone
DAY = datetime(2026, 9, 14, 4, tzinfo=timezone.utc)      # 12:00 in the owner's own timezone
CONTACT = {"quiet_start": 0, "quiet_end": 9, "timezone": "Asia/Singapore"}


def test_the_rest_window_is_the_quiet_hours_or_the_resting_phase():
    assert quiet_hours(CONTACT, NIGHT.isoformat()) and not quiet_hours(CONTACT, DAY.isoformat())
    assert not quiet_hours({}, NIGHT.isoformat())
    assert not quiet_hours({"quiet_start": 3, "quiet_end": 3}, NIGHT.isoformat())
    # A window that wraps midnight is the same window.
    assert quiet_hours({**CONTACT, "quiet_start": 23, "quiet_end": 7}, NIGHT.isoformat())
    day = {"contact": CONTACT, "rhythm": {"phase": "awake"}}
    assert not at_rest(day, DAY.isoformat()) and at_rest(day, NIGHT.isoformat())
    assert at_rest({"contact": CONTACT, "rhythm": {"phase": "resting"}}, DAY.isoformat())
    # Every other phase is the role being up, whatever its alertness says.
    for phase in ("awake", "settling", "drowsy", "roused", "recovering", "forming"):
        assert not at_rest({"contact": CONTACT, "rhythm": {"phase": phase}}, DAY.isoformat())


def test_the_request_states_the_ceiling_it_allows(monkeypatch):
    api = Recorded(monkeypatch)
    body, fingerprint = api.request(CONTEXT)
    assert fingerprint == UNCHANGED_REQUEST["interaction"]
    assert body["tools"][0]["input_schema"]["properties"]["next_review_minutes"]["maximum"] == REVIEW_MAX_MINUTES
    assert REVIEW_WINDOW_PROMPT.format(cap=REVIEW_MAX_MINUTES) in body["system"]
    api.provider.review_max_minutes = REVIEW_REST_MAX_MINUTES
    resting, resting_fingerprint = api.request(CONTEXT)
    assert resting_fingerprint != fingerprint
    assert resting["tools"][0]["input_schema"]["properties"]["next_review_minutes"]["maximum"] == REVIEW_REST_MAX_MINUTES
    assert REVIEW_WINDOW_PROMPT.format(cap=REVIEW_REST_MAX_MINUTES) in resting["system"]
    assert REVIEW_WINDOW_PROMPT.format(cap=REVIEW_MAX_MINUTES) not in resting["system"]
    # The ordinary ceiling put back is the request as it was, byte for byte.
    api.provider.review_max_minutes = REVIEW_MAX_MINUTES
    assert api.request(CONTEXT)[1] == fingerprint
    assert appraisal_schema() == appraisal_schema(review_max=REVIEW_MAX_MINUTES)


def review_at(system, clock_at, minutes, **settings):
    mind, memory, source, clock = system
    clock[0] = clock_at
    if settings:
        memory.configure(settings)
    jobs = Appraisals(mind)
    sid = source("turn", "Synthetic message")
    memory.ingest({"id": "turn", "kind": "owner-message", "at": mind.clock(), "text": "Synthetic message", "source_id": sid})
    jobs.enqueue([sid], "fixture-v1")
    seen = {}
    class Provider:
        def appraise(self, context):
            seen["cap"] = self.review_max_minutes
            return Appraisal(reason="Nothing needs answering tonight.", next_review_minutes=minutes), {"model": "deepseek-flash"}
    assert jobs.run_one(Provider())["state"] == "complete"
    scheduled = datetime.fromisoformat(memory.semantic_context()["next_review"]) - clock[0]
    return seen["cap"], round(scheduled.total_seconds() / 60)


def test_a_night_costs_one_review_instead_of_one_every_two_hours(system):
    assert review_at(system, NIGHT, 300) == (REVIEW_REST_MAX_MINUTES, 300)


def test_the_same_proposal_in_the_daytime_keeps_the_ordinary_ceiling(system):
    assert review_at(system, DAY, 300) == (REVIEW_MAX_MINUTES, 120)


def test_the_rest_ceiling_is_off_with_its_switch(system):
    assert review_at(system, NIGHT, 300, rest_review_window=False) == (REVIEW_MAX_MINUTES, 120)


def test_the_resting_ceiling_is_a_bounded_setting(system):
    _, memory, _, _ = system
    assert memory.settings()["review_rest_max_minutes"] == REVIEW_REST_MAX_MINUTES
    memory.configure({"review_rest_max_minutes": 240})
    assert memory.settings()["review_rest_max_minutes"] == 240
    for bad in (60, 721, "480", 480.0):
        with pytest.raises(ValueError, match="resting review ceiling"):
            memory.configure({"review_rest_max_minutes": bad})


# --- what a recall calls the owner ----------------------------------------------------------------

def test_a_question_about_the_owner_needs_no_name_in_the_code():
    assert owner_question("我当时说过什么")
    assert owner_question("你确认过这个吗")
    assert owner_question("她的偏好是什么")
    assert not owner_question("明天天气怎么样")
    # A configured name reaches the same lane without appearing in this module.
    assert not owner_question("阿狸要求过什么颜色")
    assert owner_question("阿狸要求过什么颜色", ("阿狸",))


def test_the_classifier_words_win_over_the_configured_aliases():
    assert owner_names(["阿狸"], ()) == ("阿狸",)
    assert owner_names(["阿狸"], ["小鹿", "小鹿"]) == ("小鹿",)
    assert owner_names([], ()) == ()
    assert owner_names([" 阿狸 ", "", None, 7], ()) == ("阿狸",)
    assert len(owner_names([], [str(i) for i in range(20)])) == 8
    # The generic words a question always carries are not names and never were.
    assert "小" not in GENERIC_TERMS and set(GENERIC_TERMS) == {"kin", "说", "什么", "时候", "怎样"}


def test_the_owner_aliases_are_a_bounded_setting(system):
    _, memory, _, _ = system
    assert memory.settings()["recall_owner_aliases"] == []
    memory.configure({"recall_owner_aliases": ["阿狸"]})
    assert memory.settings()["recall_owner_aliases"] == ["阿狸"]
    for bad in ("阿狸", [""], ["x" * 41], [1], [str(i) for i in range(9)]):
        with pytest.raises(ValueError, match="Owner aliases"):
            memory.configure({"recall_owner_aliases": bad})


def test_a_recall_uses_the_words_the_classifier_supplied(system, monkeypatch):
    import kin_mind.adaptive_recall as module
    mind, memory, source, _ = system
    from kin_mind.context import Contexts
    memory.configure({"adaptive_recall": True, "recall_owner_aliases": ["阿狸"]})
    seen = []
    original = module.owner_question
    monkeypatch.setattr(module, "owner_question", lambda query, names=(): seen.append(names) or original(query, names))
    source("turn", "阿狸要求过蓝色")
    module.AdaptiveRecall(Contexts(mind)).collect("阿狸要求过什么", mode="light")
    module.AdaptiveRecall(Contexts(mind)).collect("阿狸要求过什么", mode="light", owner_words=["小鹿"])
    assert seen == [("阿狸",), ("小鹿",)]


# --- the envelope openings ------------------------------------------------------------------------

def test_a_configured_opening_is_added_to_the_built_in_ones(system):
    mind, _, _, _ = system
    engine = mind.engine
    assert envelope_prefixes(engine) == HOST_PREFIXES
    assert configure_envelopes(engine, ["### Synthetic host block"]) == (*HOST_PREFIXES, "### Synthetic host block")
    # Configuration only ever adds: the built-in list is part of the read policy.
    assert set(HOST_PREFIXES) <= set(envelope_prefixes(engine))
    assert host_envelope("### Synthetic host block\nanything", envelope_prefixes(engine))
    assert not host_envelope("### Synthetic host block\nanything")
    assert configure_envelopes(engine, []) == HOST_PREFIXES
    for bad in ("a string", [""], [7], ["x" * 201], ["p"] * 33):
        with pytest.raises(ValueError, match="Envelope prefixes"):
            configure_envelopes(engine, bad)


def test_the_public_dialogue_window_reads_the_same_list():
    ordinary = {"kind": "owner-message", "text": "an ordinary turn"}
    assert is_public_dialogue(ordinary)
    for prefix in HOST_PREFIXES:
        assert not is_public_dialogue({**ordinary, "text": prefix + " and the rest"})
    assert is_public_dialogue({**ordinary, "text": "### Synthetic host block"})
    assert not is_public_dialogue({**ordinary, "text": "### Synthetic host block"},
                                  (*HOST_PREFIXES, "### Synthetic host block"))


# --- a quotation is a candidate, not a decision ------------------------------------------------------

def test_a_bubble_that_quotes_nothing_is_still_released(system):
    mind, memory, _, _ = system
    memory.ingest({"id": "task", "kind": "task-result", "at": mind.clock(), "task_id": "task", "verified": True,
                   "text": "A synthetic verified result"})
    assert memory.sharing.preflight({"draft_id": "small-talk", "text": "今天天气不错呀"})["state"] == "ready"


# --- a method candidate and the result it rests on -----------------------------------------------------

def test_the_prompt_states_what_a_method_candidate_may_rest_on():
    assert "state=completed" in SYSTEM and "verified=true" in SYSTEM
    assert "procedure_candidates 留空" in SYSTEM


def test_a_verified_result_resolves_by_the_id_the_model_was_shown(system):
    mind, memory, _, _ = system
    memory.ingest({"id": "host-task-7", "kind": "task-result", "at": mind.clock(), "task_id": "task-7",
                   "verified": True, "text": "A synthetic verified result"})
    shown = [e["id"] for e in memory.semantic_context()["pending_events"]]
    assert "host-task-7" in shown
    procedures = Procedures(mind)
    with mind.engine.db.connect() as conn:
        by_host_id = procedures.outcome(conn, "host-task-7")
        stored = conn.execute("SELECT id FROM mind_runtime_events WHERE scope=?", (mind.scope.key(),)).fetchone()[0]
        assert stored != "host-task-7"
        assert procedures.outcome(conn, stored) == by_host_id
        assert by_host_id["case_id"] == "task-7"
        with pytest.raises(Conflict, match="actual verified result"):
            procedures.outcome(conn, "never-happened")


# --- a wish whose draft keeps failing --------------------------------------------------------------

def test_the_third_failed_draft_asks_the_model_about_the_wish(setup):
    mind, source, clock = setup
    wish(mind, source)
    mind.record(event(mind, source, "ready", {"initiative": 95}))
    for index in range(1, 4):
        attempt = mind.claim_contact(owner_epoch="owner-1")
        receipt = mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-failed")
        # The backoff is unchanged; what used to be the end of the road now asks a question.
        assert receipt["decision"]["condition"] == ("time" if index < 3 else "new_evidence")
        assert len(action_events(mind, "wish-review")) == (1 if index == 3 else 0)
        clock[0] += timedelta(seconds=300 * index)
        mind.reconsider_contacts(owner_epoch="owner-1")
    asked = action_events(mind, "wish-review")
    assert asked[0]["desire_id"] == mind.read()["desires"][0]["id"]
    assert asked[0]["evidence_ids"] and asked[0]["agent_version"] == "synthetic-v1"
