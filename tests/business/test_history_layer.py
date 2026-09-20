import json

from datetime import datetime, timedelta, timezone

import pytest

from test_autonomous_plans import create, decide

from eventmem.core.db import Conflict, dumps

from kin_mind import history

from kin_mind.state import AffectiveEvent, DesireChange, Evolution

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

CHECKPOINT_KINDS = ("initialize", "evolution")

def drive(mind, source, clock, *, rounds=50):
    """Revisions the way the host writes them: observations, wishes, and owner preferences.

    Driven through the public API with an injected clock, so what lands in `mind_events` is the
    real thing and not a fixture's idea of it."""
    revision, made = mind.read()["revision"], []
    for index in range(rounds):
        clock[0] += timedelta(minutes=7)
        revision = mind.record(AffectiveEvent(
            command_id="observation-" + str(index), agent_version="synthetic-v1",
            expected_revision=revision, evidence_ids=[source("observed-" + str(index))],
            values={"mood": 40 + index % 50, "curiosity": 30 + index % 60},
            reason="A sourced synthetic observation"))["revision"]
        wish = mind.manage_desire(DesireChange(
            command_id="wish-" + str(index), agent_version="synthetic-v1", expected_revision=revision,
            evidence_ids=[source("wish-source-" + str(index))], action="create",
            content="Share finding " + str(index), topic="synthetic", kind="contact", strength=50,
            expires_at=(datetime.fromisoformat(mind.clock()) + timedelta(days=3)).isoformat(),
            completion="The owner has it", reason="A finding worth discussing"))
        revision, made = wish["revision"], made + [wish["desire_id"]]
        if index % 3 == 0:
            revision = mind.manage_desire(DesireChange(
                command_id="abandon-" + str(index), agent_version="synthetic-v1", expected_revision=revision,
                evidence_ids=[source("reconsidered-" + str(index))], action="abandon",
                desire_id=wish["desire_id"], reason="No longer current"))["revision"]
        if index % 7 == 0:
            revision = mind.configure_contact({
                "command_id": "preference-" + str(index), "agent_version": "synthetic-v1",
                "expected_revision": revision, "evidence_ids": [source("owner-preference-" + str(index))],
                "wait_for_reply": bool(index % 2), "reason": "The owner said which they prefer"})["revision"]
    return made

def stored(mind):
    """revision -> the exact canonical text of the state that revision left behind."""
    with mind.engine.db.connect() as conn:
        return {row["revision"]: history.canonical(json.loads(row["data"])["snapshot"])
                for row in conn.execute("SELECT revision,data FROM mind_events WHERE scope=? ORDER BY revision",
                                        (mind.scope.key(),)).fetchall()}

def repack(mind, *, checkpoint_every=50):
    """Rewrite the whole history in the patch shape, the way the later writer will store it.

    Checkpoints where that writer will keep them: the first row, every `evolution`, and one every
    `checkpoint_every` rows. Everything between is a depth-2 keyed diff against the row before it,
    carrying both hashes and the two-key shim that keeps the previous release's queries working."""
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        rows = conn.execute("SELECT revision,kind,data FROM mind_events WHERE scope=? ORDER BY revision",
                            (scope,)).fetchall()
        previous, base, since = None, None, 0
        for row in rows:
            data = json.loads(row["data"])
            state = data["snapshot"]
            if previous is None or row["kind"] in CHECKPOINT_KINDS or since >= checkpoint_every:
                written, since = {"request": data["request"], "format": history.PATCH_FORMAT,
                                  "snapshot": state, "state_hash": history.row_hash(state)}, 0
            else:
                since += 1
                written = {"request": data["request"], "format": history.PATCH_FORMAT, "base": base,
                           "depth": since, "patch": history.diff(previous, state),
                           "state_hash": history.row_hash(state), "base_hash": history.row_hash(previous),
                           "snapshot": {"last_evidence_key": state.get("last_evidence_key"),
                                        "revision": state["revision"]}}
            conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                         (dumps(written), scope, row["revision"]))
            previous, base = state, row["revision"]

def corrupt(mind, revision):
    """Change a stored state without changing the hash the row claims for it."""
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                       (scope, revision)).fetchone()[0])
        target = data["snapshot"] if history.snapshot_of(data) else data.get("patch")
        if isinstance(target, dict):
            target["updated_at"] = "2001-01-01T00:00:00+00:00"
        else:
            target.append(["set", ["updated_at"], "2001-01-01T00:00:00+00:00"])
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (dumps(data), scope, revision))

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
    drive(mind, source, clock, rounds=14)
    before = mind.read(history=30)["history"]
    assert len(before) == 30
    assert {key for entry in before for key in entry} == {
        "id", "kind", "revision", "occurred_at", "request", "snapshot"}
    repack(mind, checkpoint_every=7)
    assert mind.read(history=30)["history"] == before

def test_the_previous_release_still_finds_the_evidence_key_in_both_formats(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=4)
    with mind.engine.db.connect() as conn:
        key = mind._load(conn)["last_evidence_key"]
    # The literal guard query the previous release ships, run unchanged against both shapes.
    guard = ("SELECT 1 FROM mind_events WHERE scope=? AND kind='affect' "
             "AND json_extract(data,'$.snapshot.last_evidence_key')=? LIMIT 1")
    with mind.engine.db.connect() as conn:
        assert conn.execute(guard, (mind.scope.key(), key)).fetchone()
    repack(mind, checkpoint_every=3)
    with mind.engine.db.connect() as conn:
        assert conn.execute(guard, (mind.scope.key(), key)).fetchone()

def test_an_unregistered_history_command_is_refused_by_name():
    from kin_mind import history_admin

    with pytest.raises(ValueError):
        history_admin.dispatch(None, "history-nothing-registered", {})
