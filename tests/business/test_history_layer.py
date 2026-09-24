import json

from datetime import datetime, timedelta, timezone

import pytest

from kin_mind import history

from kin_mind.memory import MemoryContinuity

from kin_mind.state import AffectiveEvent, DesireChange, Evolution

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

def drive(mind, source, clock, *, rounds=50, start=0):
    """Revisions the way the host writes them: observations, wishes, and owner preferences.

    Driven through the public API with an injected clock, so what lands in `mind_events` is the
    real thing and not a fixture's idea of it. Returns the exact state text each revision left."""
    revision, made = mind.read()["revision"], {}

    def note(value):
        with mind.engine.db.connect() as conn:
            made[value] = conn.execute("SELECT data FROM mind_state WHERE scope=?",
                                       (mind.scope.key(),)).fetchone()[0]
        return value

    for index in range(start, start + rounds):
        clock[0] += timedelta(minutes=7)
        revision = note(mind.record(AffectiveEvent(
            command_id="observation-" + str(index), agent_version="synthetic-v1",
            expected_revision=revision, evidence_ids=[source("observed-" + str(index))],
            values={"mood": 40 + index % 50, "curiosity": 30 + index % 60},
            reason="A sourced synthetic observation"))["revision"])
        wish = mind.manage_desire(DesireChange(
            command_id="wish-" + str(index), agent_version="synthetic-v1", expected_revision=revision,
            evidence_ids=[source("wish-source-" + str(index))], action="create",
            content="Share finding " + str(index), topic="synthetic", kind="contact", strength=50,
            expires_at=(datetime.fromisoformat(mind.clock()) + timedelta(days=3)).isoformat(),
            completion="The owner has it", reason="A finding worth discussing"))
        revision = note(wish["revision"])
        if index % 3 == 0:
            revision = note(mind.manage_desire(DesireChange(
                command_id="abandon-" + str(index), agent_version="synthetic-v1", expected_revision=revision,
                evidence_ids=[source("reconsidered-" + str(index))], action="abandon",
                desire_id=wish["desire_id"], reason="No longer current"))["revision"])
        if index % 7 == 0:
            revision = note(mind.configure_contact({
                "command_id": "preference-" + str(index), "agent_version": "synthetic-v1",
                "expected_revision": revision, "evidence_ids": [source("owner-preference-" + str(index))],
                "wait_for_reply": bool(index % 2), "reason": "The owner said which they prefer"})["revision"])
    return made

def both_formats(mind, source, clock, rounds):
    """Whole-state rows first, then patch rows from the production writer once its switch is on."""
    texts = drive(mind, source, clock, rounds=rounds)
    MemoryContinuity(mind).configure({"history_patches": True})
    texts |= drive(mind, source, clock, rounds=rounds, start=rounds)
    with mind.engine.db.connect() as conn:
        shapes = {history.is_patch(json.loads(row[0])) for row in conn.execute(
            "SELECT data FROM mind_events WHERE scope=?", (mind.scope.key(),))}
    assert shapes == {True, False}
    return texts

def evolve(mind, source, clock):
    """One personality evolution, with the prospective check the commit requires. Returns its
    event id, which is what a later reversion names."""
    from eventmem.core.self_knowledge import (
        AssessmentInput,
        ClaimInput,
        PredictionInput,
        SelfKnowledge,
    )
    from kin_mind.compat import stamp as compatibility
    from kin_mind.memory import MemoryContinuity

    MemoryContinuity(mind).configure({"trait_ledger": False})
    knowledge = SelfKnowledge(mind.engine, mind.scope)
    with mind.engine.db.connect() as conn:
        compat = compatibility(mind, conn)

    def record_id(key):
        # The self-knowledge layer keeps its own wall clock, so its evidence has to precede it.
        clock[0] = datetime.now(timezone.utc)
        return mind.engine.source(source(key))["record_ids"][0]

    refs = [record_id("interaction-" + str(n)) for n in range(1, 4)]
    claim = knowledge.claim(ClaimInput(command_id="hypothesis", aspect="curiosity", context="source-checking",
        agent_version="synthetic-v1", claim="I prefer checking a primary source", evidence_ids=refs), compat=compat)
    prediction = knowledge.predict(PredictionInput(command_id="prospective", claim_id=claim["id"],
        expected_revision=1, case_id="future-case", behavior="Check a primary source",
        information="The next query has not been answered", probability=0.8), compat=compat)
    assessment = knowledge.assess(AssessmentInput(command_id="assess", prediction_id=prediction["id"],
        expected_revision=1, outcome=True, evidence_ids=[record_id("later-observed-behavior")],
        note="Observed source check"), compat=compat)
    clock[0] = datetime.now(timezone.utc)
    return mind.record(AffectiveEvent(command_id="evolve", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=refs,
        reason="A prospective trial with independent interactions",
        evolution=Evolution(claim_id=claim["id"], assessment_id=assessment["id"],
                            baseline_changes={"curiosity": 77}, half_life_changes={"curiosity": 52.8})))

def revert(mind, source, clock, event_id, *, command="revert"):
    clock[0] = datetime.now(timezone.utc)
    return mind.record(AffectiveEvent(command_id=command, agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source(command + "-correction")],
        reason="Explicit user correction", evolution=Evolution(revert_event_id=event_id)))

def test_read_history_keeps_its_keys_and_its_answer_across_the_two_formats(setup):
    mind, source, clock = setup
    texts = both_formats(mind, source, clock, rounds=7)
    view = mind.read(history=30)["history"]
    assert len(view) == 30
    for entry in view:
        assert set(entry) == {"id", "kind", "revision", "occurred_at", "request", "snapshot"}
        assert history.canonical(entry["snapshot"]) == texts[entry["revision"]]

def test_the_previous_release_still_finds_the_evidence_key_in_both_formats(setup):
    mind, source, clock = setup
    # The literal guard query the previous release ships, run unchanged against both shapes.
    guard = ("SELECT 1 FROM mind_events WHERE scope=? AND kind='affect' "
             "AND json_extract(data,'$.snapshot.last_evidence_key')=? LIMIT 1")
    drive(mind, source, clock, rounds=2)
    MemoryContinuity(mind).configure({"history_patches": True})
    for turn in range(2):
        with mind.engine.db.connect() as conn:
            key = mind._load(conn)["last_evidence_key"]
            assert key and conn.execute(guard, (mind.scope.key(), key)).fetchone()
        drive(mind, source, clock, rounds=2, start=2 + 2 * turn)
    with mind.engine.db.connect() as conn:
        latest = conn.execute("SELECT data FROM mind_events WHERE scope=? AND kind='affect' "
                              "ORDER BY revision DESC LIMIT 1", (mind.scope.key(),)).fetchone()[0]
        assert history.is_patch(json.loads(latest))
        assert conn.execute(guard, (mind.scope.key(), mind._load(conn)["last_evidence_key"])).fetchone()

def test_an_unregistered_history_command_is_refused_by_name():
    from kin_mind import history_admin

    with pytest.raises(ValueError):
        history_admin.dispatch(None, "history-nothing-registered", {})
