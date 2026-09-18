"""The expression intent: what the appraisal that just ran means to do with the next few replies,
and what the chat model is shown of the trait ledger.

Synthetic replays only: an injected clock, sources built through the engine, and scripted providers
that read the projection the model is really shown. No model call and no network.
"""
import hashlib
import json
from datetime import timedelta

import pytest
from test_appraisal_sections import CONTEXT, Recorded
from test_trait_ledger import World as LedgerWorld
from test_trait_ledger import first_round, notice, refused, second_round

from eventmem.core.db import digest, dumps
from kin_mind.appraisal import (
    ASK_AGAIN_SECTIONS,
    AUDIT_SECTIONS,
    SECTIONS_WITHHELD,
    Appraisal,
    ConcernProposal,
    ExpressionIntent,
    IntentTopic,
    TraitDecision,
    appraisal_schema,
    blank_sections,
    last_refusal,
    offered_sections,
)
from kin_mind.context import Contexts
from kin_mind.continuity import ContinuityConfig, select_concerns
from kin_mind.expression import MIXES, TENDENCIES, compile_expression
from kin_mind.expression_intent import stale_reason, stored

# The request with the intent offered, so a change to its paragraph or its schema is deliberate.
# The all-off pins live with the seams and are not touched here.
# Re-pinned once when every package landed together: WP6's three switch-less changes to the
# shared prompt (the widened half-life range, the stated procedure premise, the owner named by
# role) move every request that offers anything, this one included.
# Re-pinned once more when the intent and the move were told, as the ledger's own paragraph
# already told it, that a host internal event is not evidence: production refused both
# sections on two of its first four appraisals for citing exactly that.
INTENT_REQUEST = "b07ac17a53e770ad41248938d0d6e1029c38012c5beab7742407fc9e6d682a34"
# Every sentence the lookup table can say, and the whole compiled answer for a grid of states.
# Re-pin only together with a deliberate change to the table itself.
TABLE_ANSWERS = "80cee70dd5970f835d843603df5717ac226f84fcfedca89f72256d204fab23c3"
PHASES = (None, "awake", "settling", "drowsy", "resting", "roused", "recovering")


class World(LedgerWorld):
    """The ledger world plus the continuity features the wording actually runs on."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.memory.configure({"context": True})
        self.enable()

    def enable(self, key="on", activation="active", **features):
        wanted = {name: True for name in ("interpretation", "concerns", "expression", "rhythm")}
        wanted.update(features)
        self.mind.configure_continuity(ContinuityConfig(
            command_id="continuity:" + key, agent_version=self.version,
            expected_revision=self.mind.read()["revision"],
            evidence_ids=[self.source("owner-enables-continuity:" + key)],
            features=wanted, activation=activation, reason="The owner enables continuity"))

    def view(self, query=""):
        return self.mind.read(query=query)

    def context(self, **options):
        return Contexts(self.mind).build(**options)

    def intent_row(self):
        with self.mind.engine.db.connect() as conn:
            return stored(conn, self.mind.scope.key())

    def persona(self, version):
        """The approved contract this scope runs under. Its texts never move here; only which
        version is approved, which is what an intent was stated under."""
        policy = {"schema": 1, "version": version, "scope": self.mind.scope.model_dump(),
                  "approved_source": "owner-request", "requires_owner_confirmation": True,
                  "core": "【MY_PERSONA_LOAD】 SYNTHETIC\n【/MY_PERSONA_LOAD】",
                  "voice": "Use complete sentences.", "maintenance": "Preserve source quotes.",
                  "mutable_trait_keys": ["interests", "habits"]}
        for key in ("core", "voice", "maintenance"):
            policy[key + "_sha256"] = hashlib.sha256(policy[key].encode()).hexdigest()
        (self.mind.engine.db.root / "persona-policy.json").write_text(json.dumps(policy))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def table(view):
    """What the lookup table alone says about exactly this state, assembled the way the view
    assembles it. Anything an intent did not touch has to come out of here byte for byte."""
    answer = compile_expression(view["dimensions"], rhythm=view["rhythm"],
                                config_version=view["continuity"]["version"],
                                persona=view.get("persona_contract"))
    answer["concern_ids"] = [c["id"] for c in view["selected_concerns"]]
    answer["fingerprint"] = digest([answer, view["selected_concerns"]])
    return answer


def state_intent(world, key="an-evening", text="I am still thinking about the lanterns.", **fields):
    """One appraisal that says nothing but how it means to be present."""
    source = world.owner(key, text)
    fields.setdefault("stance", "慢一点接话，把自己真正在意的说清楚。")
    result, data, _ = world.appraise([source], lambda state, shown: Appraisal(
        reason="What this evening asks for", expression_intent=ExpressionIntent(
            evidence_ids=[source], **fields)))
    return source, result, data


def concerns(world, key="evening"):
    """Two concerns the chat side may stay with, the second one the newer."""
    made = []
    for name, topic, content in (("lantern-paper", "灯笼纸", "对方还在挑灯笼纸"),
                                 ("late-train", "夜班车", "对方这周都在赶夜班车")):
        world.clock[0] += timedelta(minutes=20)
        source = world.owner(key + ":" + name, content)
        world.appraise([source], lambda state, shown, topic=topic, content=content, name=name, cited=source: Appraisal(
            reason="Something to keep in mind", concerns=[ConcernProposal(
                action="create", key=name, kind="care", content=content, topic=topic, intensity=60,
                basis="explicit", confidence=0.9, reason="The owner said so", evidence_ids=[cited])]))
        made.append(next(c for c in world.view()["concerns"] if c["key"] == name))
    return made


# --- an intent that holds -------------------------------------------------------------------------

def test_an_intent_shapes_the_wording_and_what_it_stays_with(world):
    older, newer = concerns(world)
    bigram = [c["id"] for c in select_concerns(world.view()["concerns"])]
    assert bigram[0] == newer["id"], "without an intent the newer concern leads"
    state_intent(world, stance="今晚慢一点，先把灯笼的事说完。",
                 continue_topics=[IntentTopic(topic="灯笼纸", concern_id=older["id"]),
                                  IntentTopic(topic="夜班车")],
                 avoid=["再问一遍工作进度"], valid_minutes=120)
    view = world.view()
    guidance = view["expression"]["guidance"]
    assert [hint["id"] for hint in guidance] == ["intent-stance", "intent-continue", "intent-avoid"]
    assert guidance[0]["text"] == "今晚慢一点，先把灯笼的事说完。"
    assert guidance[1]["text"] == "接着聊：灯笼纸、夜班车" and guidance[2]["text"] == "这段时间先不提：再问一遍工作进度"
    assert all(hint["basis"] == "appraised_intent" for hint in guidance)
    assert view["expression"]["intent"]["id"] == world.intent_row()["id"]
    # The concern it named leads, the one its other topic matches follows, and the rest is unchanged.
    assert [c["id"] for c in view["selected_concerns"]] == [older["id"], newer["id"]]
    assert view["continuity"]["expression_intent"]["stale_reason"] is None


def test_a_topic_without_an_id_still_finds_the_concern_it_overlaps(world):
    older, newer = concerns(world)
    state_intent(world, continue_topics=[IntentTopic(topic="灯笼纸还没挑好")])
    assert [c["id"] for c in world.view()["selected_concerns"]] == [older["id"], newer["id"]]
    # A topic that matches nothing current leaves the order the overlap ranking gave it.
    state_intent(world, key="another-evening", continue_topics=[IntentTopic(topic="完全无关的事情")])
    assert [c["id"] for c in world.view()["selected_concerns"]] == [newer["id"], older["id"]]


def test_the_stance_reaches_the_chat_model_and_the_proactive_draft(world):
    state_intent(world, stance="带着点想念接话，不追问。")
    for purpose in ("chat", "proactive"):
        packed = world.context(purpose=purpose)
        affect = json.loads(next(json.loads(line)["text"] for line in packed["text"].splitlines()
                                 if json.loads(line)["id"] == "affect"))
        assert affect["expression"][0] == "带着点想念接话，不追问。"


def test_a_new_owner_message_alone_does_not_end_an_intent(world):
    """The reply to a message is written before the appraisal of it: an intent that ended with the
    next message could never be used at all."""
    state_intent(world, stance="今晚慢一点。", valid_minutes=120)
    before = dumps(world.view()["expression"])
    world.clock[0] += timedelta(minutes=5)
    later = world.owner("owner-writes-again", "Are you still awake?")
    world.memory.ingest({"id": "owner-writes-again", "kind": "owner-message",
                         "at": world.mind.clock(), "source_id": later})
    view = world.view()
    assert view["expression"]["guidance"][0]["text"] == "今晚慢一点。"
    assert dumps(view["expression"]) == before
    assert view["continuity"]["expression_intent"]["stale_reason"] is None


# --- every way it ends ------------------------------------------------------------------------------

def revoke(world, trait, key="correction"):
    correction = world.owner(key, "That is not you at all; drop the lantern idea.")
    return world.appraise([correction], lambda state, shown: Appraisal(reason="The owner corrected it",
        trait_decisions=[TraitDecision(action="revoke", trait_id=trait["id"],
            expected_revision=state["traits"]["candidate"][0]["revision"], text=trait["text"],
            basis="owner_correction", quote="drop the lantern idea", evidence_ids=[correction],
            reason="The owner said to drop it")]))


def ended(world, how):
    """Put a stated intent out of use in one of the ways the host knows. Returns the reason the
    next evaluation is shown, which is nothing at all when there is no intent to speak of."""
    if how == "never-stated":
        return None
    cites = how.startswith("trait")
    trait = None
    if how == "persona-changed":
        world.persona("persona-v1")
    if cites:
        first_round(world)
        trait = world.ledger().read()["traits"][0]
    # A trait can only be the reason while the minutes have not run out on their own.
    source, _, data = state_intent(world, valid_minutes=720 if cites else 10,
                                   **({"trait_refs": [trait["id"]]} if cites else {}))
    assert "rejected_sections" not in data
    assert "intent" in world.view()["expression"], "it was in force before this ended it"
    if how == "expired":
        world.clock[0] += timedelta(minutes=11)
    elif how == "evidence-retracted":
        world.revise(source, "retract", "owner-retracted-own-words")
    elif how == "trait-revised":
        second_round(world, hours=2, action="revise")
    elif how == "trait-revoked":
        world.clock[0] += timedelta(hours=2)
        revoke(world, trait)
    elif how == "persona-changed":
        world.persona("persona-v2")
    elif how == "switched-off":
        world.memory.configure({"expression_intent": False})
    elif how == "continuity-paused":
        world.enable(key="paused", activation="shadow")
    return {"expired": "intent-expired", "evidence-retracted": "intent-evidence-stale",
            "trait-revised": "intent-trait-moved", "trait-revoked": "intent-trait-moved",
            "persona-changed": "persona-changed",
            "continuity-paused": "continuity-inactive"}.get(how)


@pytest.mark.parametrize("how", ["never-stated", "expired", "evidence-retracted", "trait-revised",
                                 "trait-revoked", "persona-changed", "switched-off", "continuity-paused"])
def test_an_intent_that_does_not_hold_leaves_the_table_answer_byte_for_byte(tmp_path, how):
    world = World(tmp_path)
    older, newer = concerns(world)
    reason = ended(world, how)
    view = world.view()
    if how == "continuity-paused":
        # Nothing is compiled at all while continuity is not speaking, with or without an intent.
        assert view["expression"] is None
    else:
        assert view["expression"] == table(view) and "intent" not in view["expression"]
    # And what it would have stayed with is the overlap ranking's own answer again.
    assert [c["id"] for c in view["selected_concerns"]] == [c["id"] for c in select_concerns(view["concerns"])]
    assert [c["id"] for c in view["selected_concerns"]][:2] == [newer["id"], older["id"]]
    status = view["continuity"].get("expression_intent")
    assert status["stale_reason"] == reason if reason else status is None


def test_the_lookup_table_is_the_same_table(world):
    """The fallback, pinned: every sentence it can say and the whole answer for a grid of states."""
    grid, phrases = [], set()
    for name in sorted(TENDENCIES):
        for value in (0, 35, 36, 50, 69, 70, 100):
            for basis in ("event_inferred", "role_configuration"):
                dimensions = {name: {"value": value, "basis": basis, "baseline": 50,
                                     "evidence_ids": ["src_" + name]}}
                grid += [compile_expression(dimensions, rhythm={"phase": phase} if phase else None)
                         for phase in PHASES]
    for key, conditions, _text in MIXES:
        dimensions = {name: {"value": (low + high) // 2, "basis": "event_inferred", "baseline": 50,
                             "evidence_ids": ["src_" + key]} for name, (low, high) in conditions.items()}
        grid += [compile_expression(dimensions, rhythm={"phase": phase} if phase else None) for phase in PHASES]
    for answer in grid:
        phrases.update(hint["text"] for hint in answer["guidance"])
    assert phrases >= {text for pair in TENDENCIES.values() for text in pair}
    assert phrases >= {text for _key, _conditions, text in MIXES}
    assert len(phrases) == 50, "the 49 sentences and the one used when nothing is selected"
    assert digest(grid) == TABLE_ANSWERS
    # An intent that does not hold is never handed to the compiler, and None changes nothing.
    assert digest([compile_expression(dict(dimensions), rhythm={"phase": "drowsy"}, intent=None)
                   for dimensions in ({}, {"mood": {"value": 80, "basis": "event_inferred"}})]) == digest(
        [compile_expression(dict(dimensions), rhythm={"phase": "drowsy"})
         for dimensions in ({}, {"mood": {"value": 80, "basis": "event_inferred"}})])


def test_the_freshness_rule_is_one_pure_function_with_the_moment_given():
    intent = {"valid_until": "2026-01-01T01:00:00+00:00", "persona": "none"}
    early, late = "2026-01-01T00:59:59+00:00", "2026-01-01T01:00:01+00:00"
    assert stale_reason(intent, early) is None and stale_reason(intent, late) == "intent-expired"
    assert stale_reason(None, early) == "no-intent"
    assert stale_reason(intent, early, continuity_active=False) == "continuity-inactive"
    assert stale_reason(intent, early, persona={"version": "2"}) == "persona-changed"
    assert stale_reason(intent, early, evidence_fresh=False) == "intent-evidence-stale"
    assert stale_reason(intent, early, traits_current=False) == "intent-trait-moved"


# --- what the host refuses ---------------------------------------------------------------------------

def forged(world, **fields):
    """An intent whose citation the host cannot resolve, beside an ordinary affect update."""
    source = world.owner("forged-intent", "What are you thinking about?")
    result, data, _ = world.appraise([source], lambda state, shown: Appraisal(
        reason="A real owner message", values={"mood": 72},
        expression_intent=ExpressionIntent(stance="先安静一点。", **fields)))
    assert result["state"] == "complete"
    return data, result


@pytest.mark.parametrize("fields,code", [
    ({"trait_refs": ["trait_" + "0" * 32]}, "trait-unknown"),
    ({"continue_topics": [{"topic": "灯笼纸", "concern_id": "concern_" + "0" * 32}]}, "intent-concern-unknown"),
])
def test_a_citation_the_host_cannot_resolve_refuses_the_intent_alone(world, fields, code):
    if "continue_topics" in fields:
        fields = {"continue_topics": [IntentTopic(**entry) for entry in fields["continue_topics"]]}
    data, result = forged(world, **fields)
    assert refused(data, "expression_intent") == [code]
    assert [r["section"] for r in data["rejected_sections"]] == ["expression_intent"]
    # The rest of the appraisal committed, nothing was held, and nothing was asked again.
    assert world.mind.read()["dimensions"]["mood"]["value"] == 72
    assert "held_sections" not in data and world.followups(result) == []
    assert ASK_AGAIN_SECTIONS == {"habits", "plan_changes", "concerns", "action_decisions"}
    assert world.intent_row() is None
    with world.mind.engine.db.connect() as conn:
        assert last_refusal(conn, world.mind.scope.key(), "expression_intent")["expression_intent"]["code"] == code


def test_evidence_this_evaluation_never_saw_refuses_the_intent_alone(world):
    outside = world.owner("another-evening", "A turn this evaluation was never given.")
    data, result = forged(world, evidence_ids=[outside])
    assert refused(data, "expression_intent") == ["intent-evidence-unknown"]
    assert world.mind.read()["dimensions"]["mood"]["value"] == 72 and world.intent_row() is None
    assert world.followups(result) == []


def test_the_refusal_reaches_the_next_evaluation_without_a_second_call(world):
    forged(world, trait_refs=["trait_" + "0" * 32])
    world.clock[0] += timedelta(minutes=30)
    source = world.owner("what-now", "Still there?")
    _result, _data, provider = world.appraise([source], lambda state, shown: Appraisal(reason="Still here"))
    refusal = provider.seen[-1]["state"]["continuity"]["expression_intent"]["last_refusal"]
    assert refusal["code"] == "trait-unknown" and provider.calls == 1


def test_no_withheld_lane_offers_the_intent():
    for stimulus in SECTIONS_WITHHELD:
        assert "expression_intent" not in offered_sections(stimulus, set(AUDIT_SECTIONS))
    carried = Appraisal(reason="A stored proposal", expression_intent=ExpressionIntent(stance="先安静一点。"))
    assert blank_sections(carried, offered_sections("session-maintenance", set(AUDIT_SECTIONS))).expression_intent is None


# --- what the chat model is shown of the ledger ---------------------------------------------------------

def items_of(packed):
    return {json.loads(line)["id"]: json.loads(line) for line in packed["text"].splitlines()}


def test_the_chat_context_carries_the_ledger_only_once_it_holds_something(world):
    assert not {"self-traits", "trait-corrections"} & set(items_of(world.context()))
    source = first_round(world)
    shown = items_of(world.context())
    assert "trait-corrections" not in shown
    traits = shown["self-traits"]
    assert traits["basis"] == "self_knowledge" and traits["revision"]
    payload = json.loads(traits["text"])
    assert payload["ledger"] == "traits-of-this-history" and payload["established"] == []
    assert [t["text"] for t in payload["candidate"]] == ["Kin keeps coming back to lantern light."]
    facts = payload["candidate"][0]["facts"]
    assert set(facts) == {"episodes", "counter_examples", "first_day", "last_day", "support_strength"}
    assert facts["episodes"] == {"support": 1, "counter": 0, "non_self_support": 1}
    assert facts["counter_examples"] == 0 and facts["support_strength"] == 1.0
    # A correction brings its own item, with the source that ended the trait.
    world.clock[0] += timedelta(hours=2)
    trait = world.ledger().read()["traits"][0]
    correction = world.owner("correction", "That is not you at all; drop the lantern idea.")
    world.appraise([correction], lambda state, shown: Appraisal(reason="The owner corrected it",
        trait_decisions=[TraitDecision(action="revoke", trait_id=trait["id"], expected_revision=trait["revision"],
            text=trait["text"], basis="owner_correction", quote="drop the lantern idea",
            evidence_ids=[correction], reason="The owner said to drop it")]))
    shown = items_of(world.context())
    dropped = json.loads(shown["trait-corrections"]["text"])
    assert shown["trait-corrections"]["basis"] == "self_knowledge" and dropped["ledger"] == "owner-corrections"
    assert dropped["items"][0]["quote"] == "drop the lantern idea" and dropped["items"][0]["owner_statement"] is True
    assert dropped["items"][0]["source_ids"] == [correction] and source not in dropped["items"][0]["source_ids"]
    assert "self-traits" not in shown, "a revoked trait leaves the ledger with nothing to show"


def test_the_ledger_items_are_deduplicated_by_the_window_like_every_other_item(world):
    first_round(world)
    first = world.context(session="chat-1")
    assert "self-traits" in {entry["id"] for entry in first["index"]}
    again = world.context(session="chat-1")
    assert "self-traits" not in {entry["id"] for entry in again["index"]}


def test_two_histories_hand_the_chat_model_different_ledgers(tmp_path):
    first, second = World(tmp_path / "a"), World(tmp_path / "b")
    first_round(first)
    second_round(first)
    first_round(second, slug="night-walks", text="Kin would rather walk than sit still.")
    second_round(second, slug="night-walks")
    established = second.ledger().read()["traits"][0]
    second.clock[0] += timedelta(hours=2)
    other = second.owner("tea-praise", "You always make the tea sound like an occasion.")
    second.appraise([other], lambda state, shown: Appraisal(reason="A second thread",
        trait_observations=[notice(other, "tea")],
        trait_decisions=[TraitDecision(action="propose", text="Kin makes small rituals of things.",
            basis="inference", reason="Another sign", observation_refs=[other], evidence_ids=[other])]))
    second.clock[0] += timedelta(hours=2)
    correction = second.owner("correction", "That is not you at all; drop the walking idea.")
    second.appraise([correction], lambda state, shown: Appraisal(reason="The owner corrected it",
        trait_decisions=[TraitDecision(action="revoke", trait_id=established["id"],
            expected_revision=established["revision"], text=established["text"], basis="owner_correction",
            quote="drop the walking idea", evidence_ids=[correction], reason="The owner said to drop it")]))
    here, there = items_of(first.context()), items_of(second.context())
    mine = json.loads(here["self-traits"]["text"])
    assert [t["text"] for t in mine["established"]] == ["Kin keeps coming back to lantern light."]
    assert "trait-corrections" not in here
    theirs = json.loads(there["self-traits"]["text"])
    assert [t["text"] for t in theirs["candidate"]] == ["Kin makes small rituals of things."]
    assert theirs["established"] == []
    assert json.loads(there["trait-corrections"]["text"])["items"][0]["text"] == established["text"]


def test_with_the_ledger_off_the_chat_context_is_what_it_was(world):
    first_round(world)
    assert "self-traits" in items_of(world.context())
    world.memory.configure({"trait_ledger": False})
    assert not {"self-traits", "trait-corrections"} & set(items_of(world.context()))


# --- the switch ------------------------------------------------------------------------------------------

def test_with_the_switch_off_the_intent_changes_nothing(world):
    concerns(world)
    state_intent(world, stance="今晚慢一点。", valid_minutes=120)
    assert "intent" in world.view()["expression"]
    world.memory.configure({"expression_intent": False})
    view = world.view()
    assert view["expression"] == table(view) and "expression_intent" not in view["continuity"]
    assert [c["id"] for c in view["selected_concerns"]] == [c["id"] for c in select_concerns(view["concerns"])]
    # The row stays where it is; switching back on is enough to use it again.
    world.memory.configure({"expression_intent": True})
    assert "intent" in world.view()["expression"]


def test_the_request_that_offers_the_intent_is_pinned(monkeypatch):
    from kin_mind.appraisal import EXPRESSION_INTENT_PROMPT
    api = Recorded(monkeypatch, ("expression_intent",))
    body, fingerprint = api.request(CONTEXT)
    schema = body["tools"][0]["input_schema"]
    assert set(schema["properties"]) == set(appraisal_schema()["properties"]) | {"expression_intent"}
    assert "ExpressionIntent" in schema["$defs"] and "IntentTopic" in schema["$defs"]
    assert EXPRESSION_INTENT_PROMPT in body["system"]
    assert fingerprint == INTENT_REQUEST
