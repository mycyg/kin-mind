"""The behavior verification chain: what may be claimed, what may settle it, and what a day merges.

Synthetic replays only: an injected clock and scripted providers. No model or network call. The
scripted provider that must not be called fails the test if it is.
"""
import hashlib
import json
import subprocess
import sys
from datetime import timedelta

import pytest
from test_appraisal_sections import CONTEXT, Recorded
from test_plan_review_loop import Env, Reviewer
from test_section_isolation import children, run
from test_trait_ledger import World, first_round, world  # noqa: F401 - `world` is a fixture

from eventmem.core.db import Conflict, digest
from eventmem.core.models import ModelRole, SourceInput
from eventmem.core.self_knowledge import (
    AssessmentInput,
    ClaimInput,
    PredictionInput,
    SelfKnowledge,
    metadata,
)
from kin_mind import appraisal as appraisal_module
from kin_mind import compat
from kin_mind.appraisal import (
    ASK_AGAIN_SECTIONS,
    AUDIT_SECTION_SWITCH,
    SECTIONS_WITHHELD,
    Appraisal,
    Appraisals,
    DailyReview,
    Prediction,
    PredictionOutcome,
    SelfHypothesis,
    appraisal_context,
    last_refusal,
    offered_sections,
)
from kin_mind.behavior_chain import manifest_entries, open_predictions, owner_episodes, pending
from kin_mind.state import AffectiveEvent, Evolution

pytest_plugins = ("test_memory_continuity",)

# What the chain's switch adds to one fixed request: two properties and two paragraphs, and nothing
# else. Re-pin only together with a deliberate change to those.
CHAIN_REQUEST = "907a33d6f729519b1ba46d2b943ab95455aa1e023efd42ac9fcbd5371314a32c"
# Every behavior-relevant instruction, digested (compat.behavior_prompts: the shared appraisal
# prompt and the paragraph of each audited section). A paragraph that moves fails the test below,
# and its author chooses one of two things — see the message there.
#
# behavior-1 covers: the seams' placeholders replaced by the trait ledger's two real paragraphs.
PINNED_PROMPTS = {"behavior-1": "078abd5881aad060ff5738888af6a37662835f210735692c3787e3635deca6ab"}
# How to read the digest a change produced, in one command from the repository root:
REPIN_COMMAND = ("uv run --extra dev python -c "
                 "'from kin_mind.compat import prompt_digest; print(prompt_digest())'")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def record_of(env, source_id):
    return env.mind.engine.source(source_id)["record_ids"][0]


def stamp(env):
    with env.mind.engine.db.connect() as conn:
        return compat.stamp(env.mind, conn)


def hypothesis(hours=24, **extra):
    return SelfHypothesis(statement="Kin brings back a source when one was promised",
                          reason="Two afternoons where that happened", predictions=[Prediction(
                              statement="The next answer cites a source", test_window_hours=hours)], **extra)


def record_hypothesis(env, key="owner-asks-about-clocks", **extra):
    def script(shown, context):
        return Appraisal(reason="A real owner message",
                         self_hypothesis=hypothesis(evidence_ids=[context["new_evidence"][0]["id"]], **extra))
    result, _ = run(env, script, job_id=env.enqueue(key))
    assert result["state"] == "complete", result
    return result


def settle(env, prediction_id, key="owner-says-what-happened", outcome="confirmed", **cited):
    def script(shown, context):
        citation = cited or {"evidence_ids": [context["new_evidence"][0]["id"]]}
        return Appraisal(reason="A real owner message", prediction_outcomes=[PredictionOutcome(
            prediction_id=prediction_id, outcome=outcome, reason="What the host can check", **citation)])
    return run(env, script, job_id=env.enqueue(key))[0]


def propose(env, prediction, key="owner-chats-again", **changes):
    """An ordinary appraisal that also proposes a personality change."""
    def script(shown, context):
        return Appraisal(reason="Two afternoons of the same habit",
                         evolution=Evolution(claim_id=prediction["claim_id"],
                                             assessment_id=assessment_of(env, prediction["id"]), **changes))
    result, _ = run(env, script, job_id=env.enqueue(key))
    assert result["state"] == "complete", result
    return result


def openings(env):
    with env.mind.engine.db.connect() as conn:
        return open_predictions(conn, env.mind)


def proposals(env):
    with env.mind.engine.db.connect() as conn:
        return pending(conn, env.mind)


def assessment_of(env, prediction_id):
    with env.mind.engine.db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM records WHERE scope=? AND deleted=0"
            " AND json_extract(data,'$.attributes.self_knowledge.entry')='assessment'"
            " AND json_extract(data,'$.attributes.self_knowledge.prediction_id')=?",
            (env.mind.scope.key(), prediction_id)).fetchone()
    return row["id"] if row else None


def events(env, kind="evolution"):
    return env.rows("SELECT id,occurred_at FROM mind_events WHERE kind=? ORDER BY revision", kind)


def merge(env):
    """The host's daily action, with a provider that fails the test if anything calls it."""
    return DailyReview(env.mind).run(Reviewer(lambda *_: pytest.fail("the merge made a model call")), env.version)


def confirmed_chain(env, hours=6):
    """A hypothesis, its prediction and a confirmed outcome, written as the host writes them.

    All of it happens before the injected now, so that a replay with an injected clock and the wall
    clock the previous release used agree about which moment came first."""
    now, current = env.clock[0], stamp(env)
    knowledge = SelfKnowledge(env.mind.engine, env.mind.scope, clock=env.mind.clock)
    env.clock[0] = now - timedelta(hours=hours)
    promise = env.source("promised-a-source")
    with env.mind.engine.db.connect(write=True) as conn:
        claim = knowledge.claim_in(conn, ClaimInput(
            command_id="hypothesis", aspect="promised sources", context="ordinary chat",
            agent_version=env.version, claim="Kin brings back a source when one was promised",
            basis="hypothesis", evidence_ids=[record_of(env, promise)]), compat=current)
        prediction = knowledge.predict_in(conn, PredictionInput(
            command_id="forecast", claim_id=claim["id"], expected_revision=1, case_id="one-case",
            behavior="The next answer cites a source", information="The promise has just been made",
            probability=0.5), compat=current, window_hours=24)
    env.clock[0] = now - timedelta(hours=hours / 2)
    observed = env.source("brought-the-source-back")
    with env.mind.engine.db.connect(write=True) as conn:
        assessment = knowledge.assess_in(conn, AssessmentInput(
            command_id="assess", prediction_id=prediction["id"], expected_revision=1, outcome=True,
            evidence_ids=[record_of(env, observed)], note="The host resolved the answer"), compat=current)
    env.clock[0] = now
    return claim, prediction, assessment


def evolution(env, claim, assessment, command="evolve", evidence=None, **changes):
    return AffectiveEvent(command_id=command, agent_version=env.version,
                          expected_revision=env.mind.read()["revision"],
                          evidence_ids=evidence or [env.initial],
                          reason="A prospective trial with independent interactions",
                          evolution=Evolution(claim_id=claim["id"], assessment_id=assessment["id"], **changes))


def owner_message(env, key, at):
    """One owner message in the namespace the interaction windows are drawn from."""
    return env.mind.engine.receive(SourceInput(
        namespace="kin-owner-input", key=key, scope=env.mind.scope, text="An owner message " + key,
        authority="explicit", occurred_at=at.isoformat(),
        metadata={"role": "user", "host_event": "message"}))["id"]


def owner_window(env, count=10, minutes=5, key="window"):
    """`count` owner messages inside one interaction window, all before the injected now."""
    return [owner_message(env, key + "-" + str(index), env.clock[0] - timedelta(minutes=minutes * (count - index)))
            for index in range(count)]


def separate_windows(env, count=3, key="apart"):
    """`count` owner messages far enough apart to be separate windows, all before the injected now."""
    return [owner_message(env, key + "-" + str(index), env.clock[0] - timedelta(hours=2 * (count - index)))
            for index in range(count)]


# --- what is recorded, and under what ----------------------------------------------------------

def test_a_hypothesis_is_recorded_with_its_predictions_and_what_it_was_made_under(env):
    record_hypothesis(env)
    open_ones = openings(env)
    assert len(open_ones) == 1
    item = open_ones[0]
    assert item["statement"] == "The next answer cites a source" and item["test_window_hours"] == 24
    assert item["compat"] == "current" and item["stale_reason"] is None and not item["expired"]
    assert item["window_ends_at"] > item["made_at"] and item["claim_id"]
    # What the manifest owner needs: the same ids, their revision, and whether they still hold.
    assert manifest_entries_for(env) == {item["id"]: {"revision": 1, "needs_review": False, "compat": "current"}}


def manifest_entries_for(env):
    with env.mind.engine.db.connect() as conn:
        return manifest_entries(conn, env.mind)


def test_a_hypothesis_without_a_prediction_or_without_shown_evidence_is_refused(env):
    def bare(shown, context):
        return Appraisal(reason="A real owner message", values={"mood": 61}, self_hypothesis=SelfHypothesis(
            statement="Kin is curious", reason="No prediction", evidence_ids=[context["new_evidence"][0]["id"]]))
    job = env.enqueue("owner-chat")
    result, _ = run(env, bare, job_id=job)
    assert [r["code"] for r in env.job(job)["rejected_sections"]] == ["hypothesis-without-prediction"]
    # The rest of the appraisal committed, and nothing was asked again.
    assert env.mind.read()["dimensions"]["mood"]["value"] == 61 and children(env, job) == []
    invented = env.enqueue("owner-chat-again")
    run(env, lambda shown, context: Appraisal(reason="A real owner message",
        self_hypothesis=hypothesis(evidence_ids=[record_of(env, env.initial)])), job_id=invented)
    assert [r["code"] for r in env.job(invented)["rejected_sections"]] == ["chain-evidence-unknown"]
    assert openings(env) == []


# --- what may settle a prediction ---------------------------------------------------------------

def test_a_prediction_must_be_made_before_the_evidence_that_tests_it(env):
    record_hypothesis(env)
    prediction = openings(env)[0]
    # The same moment is not later: this evidence was available to whoever made the prediction.
    job = env.enqueue("owner-says-what-happened")
    same_moment, _ = run(env, lambda shown, context: Appraisal(reason="A real owner message",
        prediction_outcomes=[PredictionOutcome(prediction_id=prediction["id"], outcome="confirmed",
        evidence_ids=[context["new_evidence"][0]["id"]], reason="Too early to test anything")]), job_id=job)
    assert [r["code"] for r in env.job(job)["rejected_sections"]] == ["outcome-not-later"]
    assert assessment_of(env, prediction["id"]) is None and openings(env)[0]["id"] == prediction["id"]
    env.clock[0] += timedelta(hours=2)
    assert settle(env, prediction["id"], key="owner-says-it-later")["state"] == "complete"
    assert assessment_of(env, prediction["id"]) and openings(env) == []


def test_kins_own_report_settles_nothing(env):
    record_hypothesis(env)
    prediction = openings(env)[0]
    env.clock[0] += timedelta(hours=2)
    spoken = env.source("kin-says-it-was-done", authority="model")
    job = Appraisals(env.mind).enqueue([spoken], env.version)["id"]
    result, _ = run(env, lambda shown, context: Appraisal(reason="A synthetic proposal",
        prediction_outcomes=[PredictionOutcome(prediction_id=prediction["id"], outcome="confirmed",
        evidence_ids=[record_of(env, spoken)], reason="Kin says it happened")]), job_id=job)
    assert result["state"] == "complete"
    assert [r["code"] for r in env.job(job)["rejected_sections"]] == ["outcome-not-verifiable"]
    assert assessment_of(env, prediction["id"]) is None


def test_an_execution_receipt_settles_a_prediction_and_an_unverified_one_does_not(env):
    record_hypothesis(env)
    prediction = openings(env)[0]
    env.clock[0] += timedelta(hours=2)
    env.memory.ingest({"id": "unverified-task", "kind": "task-result", "task_id": "task-a",
                       "at": env.mind.clock(), "text": "A result nobody checked", "verified": False})
    assert settle(env, prediction["id"], key="a-result-arrives", result_ids=["unverified-task"])["state"] == "complete"
    assert last_code(env) == "result-unverified" and assessment_of(env, prediction["id"]) is None
    env.memory.ingest({"id": "verified-task", "kind": "task-result", "task_id": "task-b",
                       "at": env.mind.clock(), "text": "A result the host verified", "verified": True})
    # The host's own event id resolves as well as the stored one: it is what the model is shown.
    assert settle(env, prediction["id"], key="a-checked-result-arrives", result_ids=["verified-task"])["state"] == "complete"
    assert assessment_of(env, prediction["id"])
    assert settle(env, prediction["id"], key="an-unknown-result", result_ids=["never-happened"])["state"] == "complete"
    assert last_code(env) == "result-unknown"


def last_code(env, section="prediction_outcomes"):
    with env.mind.engine.db.connect() as conn:
        return last_refusal(conn, env.mind.scope.key(), section).get(section, {}).get("code")


# --- what the key keeps and what it voids --------------------------------------------------------

def test_an_unrelated_version_bump_keeps_the_chain(env):
    record_hypothesis(env)
    prediction = openings(env)[0]
    before = stamp(env)["key"]
    env.upgrade("planning-v2")
    assert stamp(env)["key"] == before and openings(env)[0]["compat"] == "current"
    env.clock[0] += timedelta(hours=2)
    assert settle(env, prediction["id"])["state"] == "complete"
    assert assessment_of(env, prediction["id"])


def test_a_changed_contract_or_persona_voids_the_chain(env, monkeypatch):
    record_hypothesis(env)
    prediction = openings(env)[0]
    env.clock[0] += timedelta(hours=2)
    monkeypatch.setattr(compat, "BEHAVIOR_CONTRACT", "behavior-synthetic")
    stale = openings(env)[0]
    assert (stale["compat"], stale["stale_reason"]) == ("stale", "compat-changed:contract")
    job = env.enqueue("owner-says-what-happened")
    run(env, lambda shown, context: Appraisal(reason="A real owner message",
        prediction_outcomes=[PredictionOutcome(prediction_id=prediction["id"], outcome="confirmed",
        evidence_ids=[context["new_evidence"][0]["id"]], reason="Under another configuration")]), job_id=job)
    assert [r["code"] for r in env.job(job)["rejected_sections"]] == ["prediction-incompatible"]
    monkeypatch.undo()
    approve_persona(env, voice="A different approved voice.")
    assert openings(env)[0]["stale_reason"] == "compat-changed:persona"


def test_the_key_moves_for_what_decides_behavior_and_for_nothing_else(env, monkeypatch):
    first = stamp(env)
    env.mind.record(AffectiveEvent(command_id="an-ordinary-event", agent_version=env.version,
                                   expected_revision=env.mind.read()["revision"], evidence_ids=[env.initial],
                                   values={"mood": 71}, reason="An ordinary afternoon"))
    assert stamp(env)["key"] == first["key"], "a score is not a configuration"
    with env.mind.engine.db.connect() as conn:
        moved = env.mind._load(conn)
        moved["profile"]["dimensions"]["curiosity"]["definition"] = "A different question entirely"
        assert compat.stale_reason(first, compat.stamp(env.mind, conn, moved)) == "compat-changed:definitions"
        # What an evolution itself moves is deliberately not in the key: the first committed change
        # would otherwise void every prediction still open.
        evolved = env.mind._load(conn)
        evolved["profile"]["dimensions"]["curiosity"].update(baseline=90, half_life_hours=99)
        assert compat.stamp(env.mind, conn, evolved)["key"] == first["key"]
    # The memory model roles are not what answers the owner, and moving one is not a configuration
    # change of the agent under test.
    env.mind.engine.settings("models", {"summary": ModelRole(endpoint="https://api.deepseek.com",
                                                             model="another-model").model_dump()})
    assert stamp(env)["key"] == first["key"]
    # An execution fact nothing of this rests on is not one either; the ones named are.
    env.mind.engine.settings("execution_environment", {"jq": "1.7"})
    assert stamp(env)["key"] == first["key"]
    env.mind.engine.settings("execution_environment", {"jq": "1.7", "python": "3.13"})
    assert compat.stale_reason(first, stamp(env)) == "compat-changed:environment"
    env.mind.engine.settings("execution_environment", {})
    assert stamp(env)["key"] == first["key"], "an environment nobody registered is not a change"
    # The model this evaluator pins itself is: it decides what every structured answer was.
    monkeypatch.setattr(appraisal_module, "APPRAISAL_MODEL", "another-appraiser")
    assert compat.stale_reason(first, stamp(env)) == "compat-changed:models"


def host_action(env, action, **request):
    from kin_mind.host import dispatch
    return dispatch({"root": str(env.mind.engine.db.root), "scope": env.mind.scope.model_dump(),
                     "agent_version": env.version, "session_id": "synthetic-session"}, action, request)


def test_the_chat_model_is_registered_by_the_host_and_moves_the_key_once(env, monkeypatch):
    """Nothing goes stale for never having registered one; the first registration moves it once."""
    unregistered = stamp(env)
    assert stamp(env)["key"] == unregistered["key"]
    registered = host_action(env, "configure-behavior-models", chat=" a-chat-model ", chat_effort="high")
    assert registered["state"] == "registered" and registered["behavior_models"] == {
        "chat": "a-chat-model", "chat_effort": "high"}
    once = stamp(env)
    assert compat.stale_reason(unregistered, once) == "compat-changed:models"
    assert stamp(env)["key"] == once["key"], "reading it again is not a change"
    # The same registration again is the same configuration; a different effort is not.
    host_action(env, "configure-behavior-models", chat="a-chat-model", chat_effort="high")
    assert stamp(env)["key"] == once["key"]
    host_action(env, "configure-behavior-models", chat="a-chat-model", chat_effort="low")
    assert compat.stale_reason(once, stamp(env)) == "compat-changed:models"
    for invalid in ({"chat": "", "chat_effort": "high"}, {"chat": "a-chat-model"}, {"chat": 3, "chat_effort": "high"}):
        with pytest.raises(ValueError, match="Behavior models need"):
            host_action(env, "configure-behavior-models", **invalid)


def approve_persona(env, voice="Use complete sentences.", version="persona-v1"):
    policy = dict(schema=1, version=version, scope=env.mind.scope.model_dump(), approved_source="owner-request",
                  requires_owner_confirmation=True, core="【MY_PERSONA_LOAD】 SYNTHETIC\n【/MY_PERSONA_LOAD】",
                  voice=voice, maintenance="Preserve source quotes.", mutable_trait_keys=["interests"])
    for key in ("core", "voice", "maintenance"):
        policy[key + "_sha256"] = hashlib.sha256(policy[key].encode()).hexdigest()
    (env.mind.engine.db.root / "persona-policy.json").write_text(json.dumps(policy))


# --- the day's merge -------------------------------------------------------------------------

def test_the_day_merges_what_an_appraisal_proposed_with_no_model_call(env):
    record_hypothesis(env)
    prediction = openings(env)[0]
    env.clock[0] += timedelta(hours=2)
    settle(env, prediction["id"])
    propose(env, prediction, baseline_changes={"curiosity": 77})
    assert [(p["state"], p["stale_reason"]) for p in proposals(env)] == [("pending", None)]
    result = merge(env)
    assert result["state"] == "complete" and result["model_calls"] == 0
    assert len(events(env)) == 1 and env.mind.read()["dimensions"]["curiosity"]["baseline"] == 77
    assert env.mind.read()["dimensions"]["curiosity"]["basis"] == "hypothesis_trial"
    # The proposal is spent, and the day is done whatever else happens.
    assert proposals(env) == [] and merge(env)["state"] == "already-evaluated"
    assert env.rows("SELECT state FROM mind_evolution_proposals")[0]["state"] == "applied"


def test_without_a_confirmed_compatible_check_nothing_evolves_and_the_proposal_stays_readable(env, monkeypatch):
    record_hypothesis(env)
    prediction = openings(env)[0]
    env.clock[0] += timedelta(hours=2)
    settle(env, prediction["id"], outcome="inconclusive")
    propose(env, prediction, baseline_changes={"curiosity": 77})
    assert merge(env)["reason"] == "need-prospective-behavioral-check"
    assert events(env) == [] and [p["state"] for p in proposals(env)] == ["pending"]
    monkeypatch.setattr(compat, "BEHAVIOR_CONTRACT", "behavior-synthetic")
    answer = merge(env)
    assert answer["reason"] == "proposal-incompatible" and events(env) == []
    # Stale, with the ingredient that moved, and still readable.
    assert [(p["state"], p["stale_reason"]) for p in proposals(env)] == [("stale", "compat-changed:contract")]
    assert proposals(env)[0]["evolution"]["baseline_changes"] == {"curiosity": 77}


def test_a_day_with_nothing_proposed_waits(env):
    answer = merge(env)
    assert (answer["state"], answer["reason"], answer["proposals"]) == ("waiting", "need-personality-proposal", [])
    # A day that waited is not a day that was used.
    assert env.rows("SELECT day FROM mind_daily_reviews") == [] and merge(env)["state"] == "waiting"


# --- the limits, unchanged ----------------------------------------------------------------------

def test_the_daily_limits_and_the_reversion_are_unchanged(env):
    claim, _prediction, assessment = confirmed_chain(env)
    apart = separate_windows(env)
    with pytest.raises(ValueError, match="Baseline change exceeds"):
        env.mind.record(evolution(env, claim, assessment, command="too-far", evidence=apart,
                                  baseline_changes={"curiosity": 78}))
    with pytest.raises(ValueError, match="Half-life change exceeds"):
        env.mind.record(evolution(env, claim, assessment, command="too-fast", evidence=apart,
                                  half_life_changes={"curiosity": 54}))
    committed = env.mind.record(evolution(env, claim, assessment, evidence=apart,
                                          baseline_changes={"curiosity": 77}, half_life_changes={"curiosity": 52.8}))
    assert env.mind.read()["dimensions"]["curiosity"]["baseline"] == 77
    with pytest.raises(Conflict, match="already evaluated today"):
        env.mind.record(evolution(env, claim, assessment, command="again", evidence=apart))
    correction = env.source("please-put-that-back")
    env.mind.record(AffectiveEvent(command_id="revert", agent_version=env.version,
                                   expected_revision=env.mind.read()["revision"], evidence_ids=[correction],
                                   reason="An explicit owner correction",
                                   evolution=Evolution(revert_event_id=committed["event_id"])))
    assert env.mind.read()["dimensions"]["curiosity"]["baseline"] == 75


def test_a_reversion_is_not_something_an_appraisal_proposes(env):
    claim, _prediction, assessment = confirmed_chain(env)
    committed = env.mind.record(evolution(env, claim, assessment, evidence=separate_windows(env),
                                          baseline_changes={"curiosity": 77}))
    job = env.enqueue("owner-chat")
    run(env, lambda shown, context: Appraisal(reason="A synthetic proposal",
        evolution=Evolution(revert_event_id=committed["event_id"])), job_id=job)
    assert [r["code"] for r in env.job(job)["rejected_sections"]] == ["evolution-revert-not-proposed"]
    assert proposals(env) == []


# --- episodes, not messages ---------------------------------------------------------------------

def test_ten_messages_in_one_window_are_one_episode(env):
    claim, _prediction, assessment = confirmed_chain(env)
    ids = owner_window(env, 10)
    with env.mind.engine.db.connect() as conn:
        refs = env.mind._evidence(conn, ids)
        assert len(owner_episodes(env.mind, conn, refs)) == 1
    request = evolution(env, claim, assessment, baseline_changes={"curiosity": 77})
    request = request.model_copy(update={"evidence_ids": ids})
    with pytest.raises(Conflict, match="three independent user interactions"):
        env.mind.record(request)
    # The same ten messages were ten independent interactions while they were counted as hashes.
    env.memory.configure({"behavior_chain": False})
    env.mind.record(request.model_copy(update={"expected_revision": env.mind.read()["revision"]}))
    assert env.mind.read()["dimensions"]["curiosity"]["baseline"] == 77


def test_messages_in_separate_windows_are_separate_episodes(env):
    claim, _prediction, assessment = confirmed_chain(env)
    env.mind.record(evolution(env, claim, assessment, evidence=separate_windows(env),
                              baseline_changes={"curiosity": 77}))
    assert env.mind.read()["dimensions"]["curiosity"]["baseline"] == 77


# --- isolation, and the lanes that carry nothing --------------------------------------------------

def test_a_refused_section_costs_nothing_and_asks_nothing_again(env):
    assert ASK_AGAIN_SECTIONS == {"habits", "plan_changes", "concerns", "action_decisions"}
    job = env.enqueue("owner-chat")
    run(env, lambda shown, context: Appraisal(reason="A real owner message", values={"mood": 58},
        prediction_outcomes=[PredictionOutcome(prediction_id="mem_invented", outcome="confirmed",
        reason="Nothing the host can resolve")]), job_id=job)
    assert children(env, job) == [] and "held_sections" not in env.job(job)
    assert env.mind.read()["dimensions"]["mood"]["value"] == 58
    with env.mind.engine.db.connect() as conn:
        assert set(last_refusal(conn, env.mind.scope.key())) == {"prediction_outcomes"}


@pytest.mark.parametrize("stimulus", sorted(SECTIONS_WITHHELD))
def test_the_lanes_that_carry_nothing_never_offer_the_chain(stimulus):
    assert offered_sections(stimulus, {"self_hypothesis", "prediction_outcomes"}) == ()


# --- with the switch off --------------------------------------------------------------------------

def test_the_switch_on_adds_two_properties_and_two_paragraphs_and_nothing_else(monkeypatch):
    from kin_mind.appraisal import PREDICTION_OUTCOMES_PROMPT, SELF_HYPOTHESIS_PROMPT, appraisal_schema
    api = Recorded(monkeypatch, ("self_hypothesis", "prediction_outcomes"))
    body, fingerprint = api.request(CONTEXT)
    assert fingerprint == CHAIN_REQUEST
    schema = body["tools"][0]["input_schema"]
    assert set(schema["properties"]) - set(appraisal_schema()["properties"]) == {"self_hypothesis", "prediction_outcomes"}
    assert SELF_HYPOTHESIS_PROMPT in body["system"] and PREDICTION_OUTCOMES_PROMPT in body["system"]


def test_the_switch_off_still_drops_what_an_ordinary_appraisal_proposes(env):
    """Off, a proposal goes nowhere exactly as it did: no row, no refusal, no change."""
    env.memory.configure({"behavior_chain": False})
    claim, _prediction, assessment = confirmed_chain(env)
    job = env.enqueue("owner-chat")
    run(env, lambda shown, context: Appraisal(reason="A synthetic proposal", values={"mood": 60},
        evolution=Evolution(claim_id=claim["id"], assessment_id=assessment["id"],
                            baseline_changes={"curiosity": 77})), job_id=job)
    assert "rejected_sections" not in env.job(job) and proposals(env) == []
    assert env.mind.read()["dimensions"]["curiosity"]["baseline"] == 75
    assert env.mind.read()["dimensions"]["mood"]["value"] == 60


def test_the_switch_off_keeps_the_daily_review_as_it_was(env):
    """Off, the day is what it was: one model call, and the old version-equality rule."""
    env.memory.configure({"behavior_chain": False})
    claim, _prediction, assessment = confirmed_chain(env)
    separate_windows(env)
    provider = Reviewer(None)

    def appraise(context):
        provider.calls += 1
        assert context["mode"] == "daily-personality-review"
        return Appraisal(reason="A synthetic daily review", evolution=Evolution(
            claim_id=claim["id"], assessment_id=assessment["id"], baseline_changes={"curiosity": 77})), {"model": "deepseek-flash"}

    provider.appraise = appraise
    result = DailyReview(env.mind).run(provider, env.version)
    assert result["state"] == "complete" and provider.calls == 1
    assert env.mind.read()["dimensions"]["curiosity"]["baseline"] == 77
    assert len(events(env)) == 1


# --- what the model is shown, end to end -----------------------------------------------------------

def projected(world):
    """The state an appraisal would really be handed, through the projection that builds it."""
    return appraisal_context({"state": world.mind.read(), "stimulus": None})["state"]


def test_the_model_is_shown_an_open_prediction_and_settles_it(world):
    """One appraisal makes a check; the next is shown it, and settles it with what the host can see."""
    promise = world.owner("owner-asks-for-a-source", "Will you bring me a source next time?")
    result, data, _ = world.appraise([promise], lambda state, shown: Appraisal(
        reason="A real owner message", self_hypothesis=hypothesis(evidence_ids=[promise])))
    assert result["state"] == "complete" and "rejected_sections" not in data
    world.clock[0] += timedelta(hours=2)
    later = world.owner("owner-confirms", "You did bring the source this time.")
    seen = {}

    def script(state, shown):
        seen["open"] = state.get("open_predictions")
        return Appraisal(reason="A real owner message", prediction_outcomes=[PredictionOutcome(
            prediction_id=state["open_predictions"][0]["id"], outcome="confirmed",
            evidence_ids=[later], reason="What the owner reported, which the host holds")])

    result, data, _ = world.appraise([later], script)
    assert result["state"] == "complete" and "rejected_sections" not in data
    shown = seen["open"][0]
    # Everything the chain knows about an open check except the revision, which is the manifest's.
    assert set(shown) == {"id", "statement", "claim_id", "made_at", "test_window_hours",
                          "window_ends_at", "expired", "compat", "stale_reason"}
    assert shown["compat"] == "current" and not shown["expired"]
    assert assessment_of(world, shown["id"]) and projected(world)["open_predictions"] == []


def test_each_projection_answers_to_its_own_switch(world):
    first_round(world)
    promise = world.owner("owner-asks-for-a-source", "Will you bring me a source next time?")
    world.appraise([promise], lambda state, shown: Appraisal(
        reason="A real owner message", self_hypothesis=hypothesis(evidence_ids=[promise])))
    open_one = openings(world)[0]["id"]
    assert [p["id"] for p in projected(world)["open_predictions"]] == [open_one]
    # The ledger on, the chain off: the ledger's own projection, and nothing left open to settle.
    world.memory.configure({"behavior_chain": False})
    state = projected(world)
    assert state["open_predictions"] == [] and state["traits"]["candidate"]
    # The chain on, the ledger off: its own projection, and nothing of the ledger's.
    world.memory.configure({"behavior_chain": True, "trait_ledger": False})
    state = projected(world)
    assert [p["id"] for p in state["open_predictions"]] == [open_one]
    assert not {"traits", "corrections"} & set(state)


# --- one writer for a trait ---------------------------------------------------------------------

def test_the_ledger_is_the_only_writer_of_traits(world):
    """With the ledger on, a trait in an evolution is refused where it is proposed and where it
    would be applied. With it off, the older snapshot still works."""
    claim, _prediction, assessment = confirmed_chain(world)
    request = evolution(world, claim, assessment, evidence=separate_windows(world),
                        traits={"interests": "Lantern light"})
    with pytest.raises(Conflict, match="only writer of traits"):
        world.mind.record(request)
    job = world.enqueue("owner-chat")
    run(world, lambda shown, context: Appraisal(reason="A synthetic proposal", evolution=Evolution(
        claim_id=claim["id"], assessment_id=assessment["id"], traits={"interests": "Lantern light"})), job_id=job)
    assert [r["code"] for r in world.job(job)["rejected_sections"]] == ["traits-owned-by-ledger"]
    assert proposals(world) == []
    world.memory.configure({"trait_ledger": False})
    world.mind.record(request.model_copy(update={"expected_revision": world.mind.read()["revision"]}))
    assert world.mind.read()["traits"]["interests"]["text"] == "Lantern light"


# --- what a hypothesis rests on ------------------------------------------------------------------

def claim_of(world, prediction_id):
    return world.mind.engine.get([p for p in openings(world) if p["id"] == prediction_id][0]["claim_id"])


def test_a_hypothesis_records_the_traits_it_rests_on(world):
    first_round(world)
    trait = world.mind.read()["trait_ledger"]["traits"]["candidate"][0]
    source = world.owner("owner-asks-about-it", "Is that a habit of yours now?")
    result, data, _ = world.appraise([source], lambda state, shown: Appraisal(
        reason="A real owner message", self_hypothesis=hypothesis(evidence_ids=[source], trait_refs=[trait["id"]])))
    assert result["state"] == "complete" and "rejected_sections" not in data
    prediction = openings(world)[0]
    assert metadata(claim_of(world, prediction["id"]))["rests_on"] == [trait["id"]]
    # A trait that is not there, and one an owner correction ended, are both refused.
    invented = world.owner("owner-asks-again", "And this one?")
    _result, data, _ = world.appraise([invented], lambda state, shown: Appraisal(
        reason="A real owner message", self_hypothesis=hypothesis(evidence_ids=[invented], trait_refs=["trait_invented"])))
    assert [r["code"] for r in data["rejected_sections"]] == ["trait-unknown"]
    world.ledger().revoke({"trait_id": trait["id"], "reason": "An owner correction",
                           "source_id": world.owner("owner-corrects", "That is not me at all.")})
    ended = world.owner("owner-asks-once-more", "And now?")
    _result, data, _ = world.appraise([ended], lambda state, shown: Appraisal(
        reason="A real owner message", self_hypothesis=hypothesis(evidence_ids=[ended], trait_refs=[trait["id"]])))
    assert [r["code"] for r in data["rejected_sections"]] == ["trait-not-effective"]


def test_with_the_ledger_off_a_trait_reference_is_accepted_and_ignored(world):
    world.memory.configure({"trait_ledger": False})
    source = world.owner("owner-asks", "Is that a habit of yours?")
    result, data, _ = world.appraise([source], lambda state, shown: Appraisal(
        reason="A real owner message", self_hypothesis=hypothesis(evidence_ids=[source], trait_refs=["trait_invented"])))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert "rests_on" not in metadata(claim_of(world, openings(world)[0]["id"]))


# --- every section claimed, whatever a path read first --------------------------------------------

def test_building_the_queue_is_enough_to_claim_every_section(tmp_path):
    """A fresh process that only builds the queue: no projection has been read, and no audited
    section is still the no-op that refuses whatever it is given."""
    code = """import json,sys
from eventmem.core import Engine
from eventmem.core.models import Scope
from kin_mind.appraisal import AUDIT_HANDLERS, Appraisals, unavailable_section
from kin_mind.state import Mind
Appraisals(Mind(Engine(sys.argv[1]), Scope(persona='synthetic-claims')))
print(json.dumps([n for n, h in AUDIT_HANDLERS.items() if h is not unavailable_section]))
"""
    answer = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=True)
    assert json.loads(answer.stdout.splitlines()[-1]) == [
        "trait_observations", "trait_decisions", "self_hypothesis", "prediction_outcomes"]


def test_a_section_owner_that_has_not_landed_is_simply_not_there(monkeypatch):
    """A release without one of the owning modules claims the others and says nothing about it."""
    monkeypatch.setattr(appraisal_module, "SECTION_OWNERS",
                        (*appraisal_module.SECTION_OWNERS, "a_module_that_has_not_landed"))
    appraisal_module.claim_audit_sections()
    appraisal_module.claim_audit_sections()  # importing is idempotent, so calling this is too
    assert appraisal_module.AUDIT_HANDLERS["self_hypothesis"] is not appraisal_module.unavailable_section


def test_a_behavior_relevant_paragraph_forces_a_decision(env):
    assert compat.prompt_digest() == PINNED_PROMPTS[compat.BEHAVIOR_CONTRACT], (
        "A behavior-relevant paragraph moved, so this is a decision, not a failure.\n"
        "Read the new digest with:\n  " + REPIN_COMMAND + "\n"
        "Then either (a) it says something different about how the agent behaves, so bump "
        "compat.BEHAVIOR_CONTRACT and add the new digest under the new name in PINNED_PROMPTS "
        "(tests/test_behavior_chain.py) — every open prediction and pending proposal made under "
        "the old contract then reads as stale, which is what you want; or (b) it is a rewording "
        "that leaves what an already-made check meant intact, so replace the digest of the current "
        "contract in PINNED_PROMPTS and note what you changed in the comment above it.")
    # The key is these five ingredients, and a stamp holds no text of any of them.
    current = stamp(env)
    assert set(current["parts"]) == {"persona", "contract", "definitions", "models", "environment"}
    assert current["key"] == "compat_" + digest(current["parts"])[:32]
    assert current["parts"]["contract"] == compat.BEHAVIOR_CONTRACT
