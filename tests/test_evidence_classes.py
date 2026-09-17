"""The shared authenticity predicates: what may stand for the owner's own words, what proves a
behaviour, what is only Kin's own statement, what is evidence of nothing, and what one episode is.

Synthetic records only, built through the engine. Nothing here calls a model or the network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_read_policy import (  # noqa: F401 - the specimen store and its scope
    SCOPE,
    receive,
    root,
    world,
)

from eventmem.core import Engine
from eventmem.core.db import Conflict
from eventmem.core.models import SourceInput
from eventmem.core.read_policy import ReadPolicy, configure_registry
from kin_mind.evidence_classes import (
    episode_key,
    never_evidence,
    owner_statement,
    root_key,
    self_statement,
    verified_behavior,
    window_of,
)
from kin_mind.habits import ConversationHabits, HabitProposal
from kin_mind.rhythm import interaction_windows
from kin_mind.state import Mind

AT = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
# Everything the specimen store holds that is not the owner speaking about what happened.
NOT_HER_WORDS = ("request", "configuration", "example", "envelope", "claim")


def test_only_the_owners_own_turn_is_her_statement(world):
    engine, ids = world
    policy = ReadPolicy.load(engine, SCOPE)
    assert owner_statement(engine.get(ids["lived"]), policy)
    for name in NOT_HER_WORDS:
        assert not owner_statement(engine.get(ids[name]), policy), name
    # A configuration request is still an experience, and still recalled; it is not her saying
    # what Kin has become.
    assert policy.classify(engine.get(ids["request"])).kind == "experience"
    assert policy.label(engine.get(ids["request"])) == "configuration_request"


def test_the_local_checks_hold_where_the_classification_is_switched_off(world):
    engine, ids = world
    assert owner_statement(engine.get(ids["lived"]), None)
    for name in NOT_HER_WORDS:
        assert not owner_statement(engine.get(ids[name]), None), name


def kins_own(engine, key="exploration-result"):
    """What Kin itself produced, written back as a source of its own: model authority."""
    return receive(engine, "kin-exploration", key, "I found three lantern makers.",
                   authority="model", role="assistant", host_event="exploration-result")


def test_an_internal_event_is_evidence_of_nothing(world):
    engine, ids = world
    policy = ReadPolicy.load(engine, SCOPE)
    internal = {"namespace": "mind-internal-event", "authority": "model"}
    assert never_evidence(internal) and never_evidence({"namespace": "mind-internal-event:drafts"})
    assert not never_evidence({"namespace": "kin-owner-input"})
    assert not owner_statement(engine.get(ids["lived"]), policy, sources=[internal])
    assert not self_statement(engine.get(kins_own(engine)), sources=[internal])
    assert not verified_behavior(internal)


def test_model_authored_material_is_only_ever_kins_own_statement(world):
    engine, ids = world
    policy = ReadPolicy.load(engine, SCOPE)
    mine = engine.get(kins_own(engine))
    assert self_statement(mine) and not owner_statement(mine, policy)
    assert not self_statement(engine.get(ids["lived"]))
    assert self_statement({"attributes": {"role": "assistant"}, "generated": True})
    assert self_statement({"attributes": {}}, sources=[{"authority": "model"}])
    # An exploration result is model authority, so it is this and nothing else.
    assert not verified_behavior({"kind": "exploration-result", "text": mine["content"]})


@pytest.mark.parametrize("receipt,proved", [
    ({"kind": "exploration", "state": "complete"}, True),
    ({"kind": "exploration", "state": "running"}, False),
    ({"kind": "plan-run", "state": "completed", "result": {"verified": True}}, True),
    ({"kind": "plan-run", "state": "interrupted", "result": {"verified": True}}, False),
    ({"kind": "plan-run", "state": "completed", "result": {"verified": False}}, False),
    ({"kind": "task-result", "verified": True}, True),
    ({"kind": "task-result", "verified": "yes"}, False),
    ({"kind": "delivery", "state": "accepted", "message_id": "m1"}, True),
    ({"kind": "delivery", "state": "accepted"}, False),
    ({"kind": "artifact-created", "artifact": {"sha256": "a" * 64}}, False),
    ({"authority": "explicit", "text": "I did the thing"}, False),
])
def test_only_a_resolved_execution_proves_a_behaviour(receipt, proved):
    assert verified_behavior(receipt) is proved


# --- episodes -------------------------------------------------------------------------------

def owner_source(engine, key, minutes, namespace="kin-owner-input", text="Same words"):
    return engine.receive(SourceInput(namespace=namespace, key=key, text=text, scope=SCOPE, authority="explicit",
        occurred_at=(AT + timedelta(minutes=minutes)).isoformat(), extract=False,
        metadata={"role": "user", "host_event": "message"}))


def test_an_episode_is_the_interaction_window_rhythm_already_draws(tmp_path):
    engine = Engine(tmp_path / "memory")
    for key, minutes in (("first", 0), ("still-talking", 20), ("much-later", 200)):
        owner_source(engine, key, minutes)
    with engine.db.connect() as conn:
        windows = interaction_windows(conn, SCOPE.key(), (AT + timedelta(hours=6)).isoformat())
    # Two windows: the 30-minute join is the one rhythm makes, not a second threshold.
    assert windows["window_count"] == 2 and windows["join_minutes"] == 30
    early, late = windows["recent_windows"]
    assert window_of(windows["recent_windows"], (AT + timedelta(minutes=20)).isoformat()) == early
    assert window_of(windows["recent_windows"], (AT + timedelta(minutes=200)).isoformat()) == late
    assert window_of(windows["recent_windows"], (AT + timedelta(minutes=100)).isoformat()) is None
    # A window that grows as the conversation continues stays one episode.
    assert episode_key(window=early) == episode_key(window={**early, "end": late["end"]})
    assert episode_key(window=early) != episode_key(window=late)
    # The execution the host resolved wins over both.
    assert episode_key(execution_id="run_1", window=early) == episode_key(execution_id="run_1")
    assert episode_key(root="root_a") not in {episode_key(window=early), episode_key(execution_id="run_1")}
    with pytest.raises(ValueError):
        episode_key()


def test_one_utterance_carried_by_two_namespaces_is_one_root_and_one_episode(tmp_path):
    engine = Engine(tmp_path / "memory")
    phone = owner_source(engine, "phone", 0)
    transcript = owner_source(engine, "transcript", 1, namespace="host:codex", text="same  words")
    with engine.db.connect() as conn:
        windows = interaction_windows(conn, SCOPE.key(), (AT + timedelta(hours=6)).isoformat())["recent_windows"]
    window = window_of(windows, (AT + timedelta(minutes=1)).isoformat())
    assert root_key(text="Same words", window=window) == root_key(text="same  words", window=window)
    assert episode_key(root=root_key(text="Same words", window=window)) == \
        episode_key(root=root_key(text="same  words", window=window))
    # Without a window a source is its own root, and two ingestions are then two of them.
    assert root_key(source_id=phone["id"]) != root_key(source_id=transcript["id"])
    with pytest.raises(ValueError):
        root_key(text="no window")


# --- the one caller this package moves over ---------------------------------------------------

@pytest.fixture
def preferences(tmp_path):
    """A mind whose conversational preferences the owner may set, and the specimens that may not."""
    engine = Engine(tmp_path / "memory")
    configure_registry(engine, {"synthetic-persona-store": "role_configuration"})
    mind = Mind(engine, SCOPE)
    owner = owner_source(engine, "owner-authorizes", 0, text="You may keep preferences for me.")
    mind.initialize(agent_version="fixture-v1", evidence_ids=[owner["id"]])
    return mind, ConversationHabits(mind), engine


def pause(evidence, revision=0):
    return HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[evidence],
                         reason="She asked to pause", expected_revision=revision)


def test_a_preference_needs_her_own_words_not_merely_explicit_authority(preferences):
    mind, habits, engine = preferences
    asked = owner_source(engine, "owner-asks-to-pause", 5, text="Please pause exploring for a while.")
    with mind.engine.db.connect(write=True) as conn:
        applied = habits.apply(conn, pause(asked["id"]), "owner-asked")
    assert applied["revision"] == 1 and habits.read()["preferences"]["exploration_paused"] is True
    # A configuration request carries the same authority and the same role, and is not a preference.
    request = engine.receive(SourceInput(namespace="synthetic-persona-store", key="configure", scope=SCOPE,
        text="Please speak warmly about lantern evenings.", authority="explicit", extract=False,
        occurred_at=(AT + timedelta(minutes=6)).isoformat(), metadata={"role": "user", "host_event": "message"}))
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict) as refused:
        habits.apply(conn, pause(request["id"], revision=1), "configuration-request")
    assert str(refused.value) == "Habit changes require current explicit owner evidence"
    assert habits.read()["revision"] == 1


def test_a_role_claim_about_kin_is_not_a_preference_she_stated(preferences):
    mind, habits, engine = preferences
    from eventmem.core.self_knowledge import ClaimInput, SelfKnowledge
    owner = owner_source(engine, "owner-mentions-exploring", 7, text="I like hearing what you explored.")
    claim = SelfKnowledge(engine, SCOPE).claim(ClaimInput(command_id="role-claim", aspect="voice", context="chat",
        agent_version="fixture-v1", basis="role", claim="I explore rarely.", evidence_ids=[root(engine, owner)]))
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict) as refused:
        habits.apply(conn, pause(claim["id"]), "persona-claim")
    assert str(refused.value) == "Habit changes require current explicit owner evidence"
    assert habits.read()["revision"] == 0
