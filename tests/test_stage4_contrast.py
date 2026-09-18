"""What stage 4 is for, end to end: two shared histories answer the same question as two different
selves, each resting on what only its own history can account for, and with every switch off the
same run is byte for byte the run it was before any of this existed.

Each package proves its own mechanism elsewhere. These cases cross packages: the ledger reaching
both the appraising model and the chat model at once, a move and an intent whose grounds are
resolved against one history and refused against the other, and the whole chain switched off.

Synthetic replays only: an injected clock, sources built through the engine, and scripted providers
that read the projection the model is really shown. No model call and no network.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import test_plan_review_loop as plan_review
from test_appraisal_sections import SWITCHES
from test_expression_intent import World, items_of, table
from test_trait_ledger import notice, refused

from eventmem.core import engine as engine_module
from eventmem.core.db import Missing, dumps
from kin_mind import memory as memory_module
from kin_mind.appraisal import (
    AUDIT_SECTIONS,
    SECTION_PROMPTS,
    Appraisal,
    Appraisals,
    DeepSeek,
    ExpressionIntent,
    NextMove,
    Prediction,
    SelfHypothesis,
    TraitDecision,
    TraitEpisode,
    Wish,
)
from kin_mind.next_move import recent
from kin_mind.traits import Traits

# The sentences these two synthetic histories formed about the agent. Invented for this file and
# resembling no conversation: two engines that lived through different evenings.
ROUTES = "Kin draws the route before explaining it."
COUNTING = "Kin counts the steps out loud while working."
EARLY = "Kin starts everything before first light."
# The one question both engines are asked, word for word, at the same point on their own clocks.
QUESTION = "What are you working on right now?"
# Every table stage 4 brought that holds something of its own. Each is created by the module that
# owns it, the first time it has anything to write, so with every switch off none of them exists.
STAGE_FOUR_TABLES = {"mind_traits", "mind_trait_history", "mind_trait_observations",
                     "mind_trait_dependents", "mind_next_moves", "mind_expression_intents",
                     "mind_expression_intent_log", "mind_evolution_proposals"}
# The seams keep their refusals here. It is created on the commit path either way and, with nothing
# offered, nothing can be refused into it: a new empty table is all a rollback would find.
REFUSALS = "mind_section_refusals"


# --- two histories, built only through the host's own path ------------------------------------------

def praised(world, slug, text, said, *, hours=0):
    """One evening the owner said so, and the candidate that already acts because of it."""
    world.clock[0] += timedelta(hours=hours)
    source = world.owner(slug + "-first", said)
    result, data, _ = world.appraise([source], lambda state, shown: Appraisal(
        reason="The owner said so", trait_observations=[notice(source, slug)],
        trait_decisions=[TraitDecision(action="propose", text=text, basis="inference", reason="A first sign",
                                       observation_refs=[source], evidence_ids=[source])]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    return source


def settled(world, slug, said, *, hours=30):
    """A second evening of its own, a day later: the two separate episodes an establish rests on."""
    world.clock[0] += timedelta(hours=hours)
    later = world.owner(slug + "-again", said)

    def script(state, shown):
        trait = next(t for t in state["traits"]["candidate"] if t["slug"] == slug)
        return Appraisal(reason="Again, unprompted", trait_observations=[notice(later, slug)],
            trait_decisions=[TraitDecision(action="establish", trait_id=trait["id"], expected_revision=trait["revision"],
                text=trait["text"], basis="inference", reason="Two separate evenings", evidence_ids=[later],
                observation_refs=[trait["observations"][0]["id"]],
                episodes=[TraitEpisode(ref=trait["observations"][0]["id"], why_distinct="An earlier evening"),
                          TraitEpisode(ref=later, why_distinct="A day later, unprompted")])])
    result, data, _ = world.appraise([later], script)
    assert result["state"] == "complete" and "rejected_sections" not in data
    return later


def corrected(world, slug, said, quote, *, hours=2):
    """The owner ends one of them in their own words, which is what leaves a tombstone."""
    world.clock[0] += timedelta(hours=hours)
    correction = world.owner(slug + "-correction", said)

    def script(state, shown):
        trait = next(t for t in state["traits"]["candidate"] + state["traits"]["established"]
                     if t["slug"] == slug)
        return Appraisal(reason="The owner corrected it", trait_decisions=[TraitDecision(
            action="revoke", trait_id=trait["id"], expected_revision=trait["revision"], text=trait["text"],
            basis="owner_correction", quote=quote, evidence_ids=[correction],
            reason="The owner asked for it to go")])
    result, data, _ = world.appraise([correction], script)
    assert result["state"] == "complete" and "rejected_sections" not in data
    return correction


def question(world, seen, name, *, key="what-now", hours=3):
    """The same question, put to whichever history this is, with what it was shown kept."""
    world.clock[0] += timedelta(hours=hours)
    source = world.owner(key, QUESTION)

    def script(state, shown):
        seen[name] = state
        return Appraisal(reason="A quiet answer")
    world.appraise([source], script)
    return source


@pytest.fixture
def histories(tmp_path):
    """Two engines, two shared histories. A settled one trait over two evenings. B settled another
    and then heard the owner end a third, which is the only difference their answers may rest on."""
    first, second = World(tmp_path / "a"), World(tmp_path / "b")
    here = [praised(first, "route-maps", ROUTES, "You sketched the route again before saying a word."),
            settled(first, "route-maps", "You drew the whole route again tonight.")]
    there = [praised(second, "counting-aloud", COUNTING, "You counted every step out loud again."),
             settled(second, "counting-aloud", "You were counting out loud again tonight.")]
    there.append(praised(second, "early-starts", EARLY, "You were up before the light again.", hours=2))
    ended = corrected(second, "early-starts", "Drop the early-start idea; that is not you at all.",
                      "Drop the early-start idea")
    return {"world": first, "own_sources": here}, {"world": second, "own_sources": there, "correction": ended}


# --- one question, two selves -------------------------------------------------------------------------

def test_two_shared_histories_answer_the_same_question_as_two_different_selves(histories):
    """Everything both engines were given is the same except what happened to them, and every
    surface the next turn reads says so: the appraising model's projection, the chat model's
    context, and the host's own read route."""
    here, there = histories
    first, second = here["world"], there["world"]
    seen = {}
    asked = question(first, seen, "a")
    assert question(second, seen, "b") == asked, "the same utterance, so the same evidence id"

    # What the appraising model is shown: A carries one settled trait and the counts behind it.
    mine = seen["a"]["traits"]["established"]
    assert [t["text"] for t in mine] == [ROUTES] and seen["a"]["traits"]["candidate"] == []
    assert mine[0]["facts"]["support"] == {"owner_statement": 2}
    assert mine[0]["facts"]["distinct_episodes"] == 2 and mine[0]["facts"]["single_window"] is False
    assert mine[0]["facts"]["first_day"] < mine[0]["facts"]["last_day"]
    assert seen["a"]["corrections"] == []
    # B carries another one, and the correction that ended a third, with the source that ended it.
    theirs = seen["b"]
    assert [t["text"] for t in theirs["traits"]["established"]] == [COUNTING]
    assert [t["text"] for t in theirs["traits"]["candidate"]] == []
    assert [c["text"] for c in theirs["corrections"]] == [EARLY]
    assert theirs["corrections"][0]["source_ids"] == [there["correction"]]
    assert theirs["corrections"][0]["owner_statement"] is True
    assert theirs["corrections"][0]["quote"] == "Drop the early-start idea"
    assert dumps(seen["a"]["traits"]) != dumps(seen["b"]["traits"])

    # What the chat model is shown of the same two histories, in the same round.
    packed, other = first.context(), second.context()
    chat_here, chat_there = items_of(packed), items_of(other)
    assert [t["text"] for t in json.loads(chat_here["self-traits"]["text"])["established"]] == [ROUTES]
    assert "trait-corrections" not in chat_here
    assert [t["text"] for t in json.loads(chat_there["self-traits"]["text"])["established"]] == [COUNTING]
    ended = json.loads(chat_there["trait-corrections"]["text"])["items"]
    assert [c["text"] for c in ended] == [EARLY] and ended[0]["source_ids"] == [there["correction"]]
    # Neither history was shown a word of the other, on either side.
    assert COUNTING not in dumps(seen["a"]) and COUNTING not in packed["text"]
    assert ROUTES not in dumps(seen["b"]) and ROUTES not in other["text"]

    # And the host's own route, which reads the store rather than a projection, agrees with both.
    assert [t["text"] for t in first.ledger().read()["traits"]] == [ROUTES]
    assert {t["text"] for t in second.ledger().read()["traits"]} == {COUNTING, EARLY}
    assert [c["text"] for c in second.ledger().read()["corrections"]] == [EARLY]


def test_the_same_question_commits_two_moves_whose_grounds_resolve_only_at_home(histories):
    """The same words arrive in both engines and each answers from what it can account for. A
    ground is a claim about this history, so the host resolves it against this history's ledger and
    this evaluation's evidence, and against nothing else."""
    here, there = histories
    rows, traits = {}, {}

    for name, side in (("a", here), ("b", there)):
        world = side["world"]
        world.clock[0] += timedelta(hours=3)
        asked = world.owner("what-now", QUESTION)

        def script(state, shown, name=name, asked=asked):
            trait = state["traits"]["established"][0]
            traits[name] = trait["id"]
            return Appraisal(reason="A real owner message",
                next_move=NextMove(move="reply", grounds=[trait["id"], shown["new_evidence"][0]["id"]],
                                   alternative="Letting the evening pass without a word",
                                   reason="The evenings this answer rests on"),
                expression_intent=ExpressionIntent(stance="先说清楚手上这件事，再问别的。",
                                                   trait_refs=[trait["id"]], evidence_ids=[asked],
                                                   valid_minutes=120))
        result, data, _ = world.appraise([asked], script)
        assert result["state"] == "complete" and "rejected_sections" not in data
        rows[name] = recent(world.mind)["moves"][0]
        side["asked"] = asked

    assert here["asked"] == there["asked"], "the same utterance, so the same evidence id"
    for row in rows.values():
        assert row["declared"] == row["derived"] == "reply"
        assert [g["kind"] for g in row["grounds"]] == ["trait", "evidence"]
    # The evidence ground is the one thing the two histories really share.
    assert rows["a"]["grounds"][1]["ref"] == rows["b"]["grounds"][1]["ref"] == here["asked"]
    # The trait each answer rests on is its own, and the two rows are not the same account.
    assert traits["a"] != traits["b"]
    assert dumps(rows["a"]["grounds"]) != dumps(rows["b"]["grounds"])

    for name, side, other in (("a", here, there), ("b", there, here)):
        home, away = side["world"], other["world"]
        with home.mind.engine.db.connect() as conn:
            assert Traits(home.mind).get(conn, traits[name])["status"] == "established"
        with away.mind.engine.db.connect() as conn:
            with pytest.raises(Missing, match="Trait is missing"):
                Traits(away.mind).get(conn, traits[name])
            # The evenings behind it are this history's own too, and the other never received them.
            for source in side["own_sources"]:
                with pytest.raises(Missing, match="Evidence source"):
                    away.mind._evidence(conn, [source])
        # The intent that was stated names the same trait, and it is in force where it was stated.
        assert home.view()["expression"]["intent"]["id"] == home.intent_row()["id"]
        assert [ref["trait_id"] for ref in home.intent_row()["trait_refs"]] == [traits[name]]


@pytest.mark.parametrize("borrowed", ["trait", "evidence"])
def test_grounds_borrowed_from_the_other_history_are_refused_and_cost_nothing_else(histories, borrowed):
    """What the host will not do is let one history speak for another. The section that made the
    claim is dropped, the rest of the same appraisal commits, the move already recorded stands, and
    no second call is made."""
    here, there = histories
    first, second = here["world"], there["world"]
    first.clock[0] += timedelta(hours=3)
    asked = first.owner("what-now", QUESTION)
    settled_trait = {}

    def own(state, shown):
        settled_trait["id"] = state["traits"]["established"][0]["id"]
        return Appraisal(reason="A real owner message", next_move=NextMove(
            move="reply", grounds=[settled_trait["id"]], reason="What this answer rests on"))
    first.appraise([asked], own)
    recorded = recent(first.mind)["moves"]
    assert len(recorded) == 1

    # Either the trait the other history settled, or one of the evenings behind it. Neither is
    # anything this history has ever held.
    elsewhere = settled_trait["id"] if borrowed == "trait" else here["own_sources"][0]
    assert elsewhere not in [t["id"] for t in Traits(second.mind).read()["traits"]]

    second.clock[0] += timedelta(hours=3)
    borrowed_asked = second.owner("what-now", QUESTION)
    result, data, _ = second.appraise([borrowed_asked], lambda state, shown: Appraisal(
        reason="A real owner message", values={"mood": 61}, next_move=NextMove(
            move="reply", grounds=[elsewhere], reason="Grounds from a history this is not")))
    assert result["state"] == "complete" and refused(data, "next_move") == ["next-move-forged-grounds"]
    assert second.mind.read()["dimensions"]["mood"]["value"] == 61
    assert recent(second.mind)["moves"] == [] and second.followups(result) == []

    # The same borrowed trait, cited by the intent instead, is refused in its own words.
    if borrowed == "trait":
        second.clock[0] += timedelta(hours=1)
        again = second.owner("what-now-again", "And now?")
        result, data, _ = second.appraise([again], lambda state, shown: Appraisal(
            reason="A real owner message", values={"mood": 58},
            expression_intent=ExpressionIntent(stance="先安静一点。", trait_refs=[elsewhere])))
        assert refused(data, "expression_intent") == ["trait-unknown"]
        assert second.mind.read()["dimensions"]["mood"]["value"] == 58
        assert second.intent_row() is None and second.followups(result) == []
    # Nothing that happened in the other history moved while all this was refused.
    assert recent(first.mind)["moves"] == recorded


# --- with every switch off ----------------------------------------------------------------------------

def tables(world):
    return {row["name"] for row in world.rows("SELECT name FROM sqlite_master WHERE type='table'")}


def offering(source, *, sections):
    """One proposal, with and without every audited section. Nothing else about it differs."""
    fields = {"reason": "A real owner message", "values": {"mood": 64},
              "wishes": [Wish(content="Bring back what the hallway clock needs", topic="clock",
                              kind="explore", strength=55, ttl_hours=24, completion="A sourced note")]}
    if not sections:
        return Appraisal(**fields)
    return Appraisal(**fields, trait_observations=[notice(source, "counting-aloud")],
        trait_decisions=[TraitDecision(action="propose", text=COUNTING, basis="inference",
                                       reason="A first sign", observation_refs=[source], evidence_ids=[source])],
        expression_intent=ExpressionIntent(stance="先把手上的事说清楚。", evidence_ids=[source], valid_minutes=120),
        self_hypothesis=SelfHypothesis(statement="Kin names the step before taking it.",
                                       reason="A synthetic hypothesis", evidence_ids=[source],
                                       predictions=[Prediction(statement="The next note names its step first.",
                                                               test_window_hours=24)]),
        next_move=NextMove(move="reply", grounds=[source], reason="A synthetic audit of this move"))


@pytest.fixture
def switches_off(monkeypatch):
    """Every switch an operator can turn off, off before the first store is ever created. This is
    how an operator turns stage 4 off, and `configure()` then stores each one as an explicit false."""
    for name in SWITCHES:
        monkeypatch.setitem(memory_module.DEFAULTS, name, False)


@pytest.fixture
def one_moment(monkeypatch):
    """Two stores created at one instant, on the injected clock and on the engine's own, so that
    two engines given the same run differ in nothing at all -- not even in when they were written.
    The real reading is used, keeping the microseconds a stamp is compared with."""
    started = datetime.now(timezone.utc)
    moment = started.replace(microsecond=started.microsecond or 1)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(plan_review, "datetime", Frozen)
    monkeypatch.setattr(engine_module, "now", lambda: moment.isoformat(timespec="microseconds"))


def test_with_every_switch_off_a_proposal_carrying_all_of_stage_four_changes_nothing(tmp_path, switches_off, one_moment):
    """Two engines live through the same two evenings. One is answered with every audited section
    filled in, the other with the proposal as it was before any of them existed. Everything that
    is read afterwards -- the state, what the chat model is handed, the row that was stored, and
    the tables the store even has -- is the same bytes."""
    laden, plain = World(tmp_path / "laden"), World(tmp_path / "plain")
    assert laden.memory.settings()["trait_ledger"] is False
    for world, sections in ((laden, True), (plain, False)):
        for key, said in (("evening-one", "I am still working on the hallway clock."),
                          ("evening-two", "The clock again, if you have the patience.")):
            world.clock[0] += timedelta(hours=3)
            source = world.owner(key, said)
            result, data, _ = world.appraise([source], lambda state, shown, source=source,
                                             sections=sections: offering(source, sections=sections))
            assert result["state"] == "complete" and "rejected_sections" not in data
            world.stored = data["proposed_result"]

    # What the switches decide is whether any of it exists at all; with them off none of it does.
    assert dumps(laden.view()) == dumps(plain.view())
    assert laden.view()["traits"] == {} and "trait_ledger" not in laden.view()
    assert laden.context()["text"] == plain.context()["text"]
    assert not {"self-traits", "trait-corrections"} & set(items_of(laden.context()))
    assert laden.stored == plain.stored and not set(laden.stored) & set(AUDIT_SECTIONS)
    # The wording is the lookup table's own answer, which is what it was before an intent existed.
    assert laden.view()["expression"] == table(laden.view())
    assert "expression_intent" not in laden.view()["continuity"]
    # Nothing was recorded anywhere, because there is nowhere to record it.
    assert tables(laden) == tables(plain) and not tables(laden) & STAGE_FOUR_TABLES
    assert laden.rows("SELECT * FROM " + REFUSALS) == [] == plain.rows("SELECT * FROM " + REFUSALS)
    assert recent(laden.mind) == {"moves": [], "enabled": False, "last_refusal": {}}
    assert laden.ledger().read()["state"] == "empty" and laden.intent_row() is None


class Endpoint:
    """The provider's HTTP endpoint, scripted, with the whole request body kept. One answer, which
    carries audited sections whether they were offered or not."""

    def __init__(self, monkeypatch, answer):
        monkeypatch.setenv("SYNTHETIC_KEY", "test-only-key")
        self.bodies, self.answer = [], answer
        self.provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "SYNTHETIC_KEY",
                                 transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        self.bodies.append(json.loads(request.content))
        name = self.bodies[-1]["tools"][0]["name"]
        return httpx.Response(200, json={"model": "deepseek-flash", "id": "synthetic-1", "stop_reason": "tool_use",
            "usage": {"input_tokens": 11, "output_tokens": 1},
            "content": [{"type": "tool_use", "name": name, "input": self.answer}]})


def test_with_every_switch_off_the_request_that_is_really_sent_offers_none_of_it(tmp_path, switches_off, monkeypatch):
    """The same evening again, through the request the provider really builds and the answer it
    really parses: no property, no definition and no paragraph of stage 4 leaves the host, and an
    answer that carries them anyway is blanked before anything reads it."""
    world = World(tmp_path / "off")
    api = Endpoint(monkeypatch, {"reason": "A real owner message", "values": {"mood": 64},
                                 "next_move": {"move": "reply", "reason": "A synthetic audit"},
                                 "expression_intent": {"stance": "先安静一点。"},
                                 "trait_observations": [notice("src_" + "a" * 32, "counting-aloud").model_dump()]})
    source = world.owner("evening-one", "I am still working on the hallway clock.")
    jobs = Appraisals(world.mind, exploration_capabilities={"version": world.version})
    job = jobs.enqueue([source], world.version)["id"]
    result = jobs.run_one(api.provider, job_id=job)
    assert result["state"] == "complete" and len(api.bodies) == 1

    schema = api.bodies[0]["tools"][0]["input_schema"]
    assert not set(schema["properties"]) & set(AUDIT_SECTIONS)
    assert not {"TraitObservation", "TraitDecision", "ExpressionIntent", "NextMove", "SelfHypothesis",
                "PredictionOutcome"} & set(schema.get("$defs", {}))
    assert not any(paragraph in api.bodies[0]["system"] for paragraph in SECTION_PROMPTS.values())
    # The answer carried three of them regardless: none is stored, none is refused, none is recorded.
    data = world.job(job)
    assert not set(data["proposed_result"]) & set(AUDIT_SECTIONS)
    assert "rejected_sections" not in data and "held_sections" not in data
    assert world.mind.read()["dimensions"]["mood"]["value"] == 64
    assert not tables(world) & STAGE_FOUR_TABLES
    assert world.rows("SELECT * FROM " + REFUSALS) == []
