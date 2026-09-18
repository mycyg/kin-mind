"""Stage 5 WP1: one reader for the state history, whichever shape a row was written in.

The writer that stores patches instead of whole snapshots lands later, so these tests bring it
forward by hand: `repack()` rewrites an already-written history into the patch shape, and the
reader is then held to rebuilding every revision of it byte for byte and to returning exactly the
`read(history=N)` it returned before. That is the contract the later writer has to meet, pinned
before it exists, rather than after.

The other half is what happens when the history cannot be rebuilt. A reversion that cannot show
the profile it is restoring restores nothing and fails the whole commit; a read of a hundred
revisions loses the one it cannot rebuild and returns the other ninety-nine. Both are asserted
here against a deliberately broken row, because a fallback that quietly guessed would be
indistinguishable from a correct answer right up to the moment it mattered.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from test_autonomous_plans import create, decide

from eventmem.core.db import Conflict, dumps
from kin_mind import history
from kin_mind.state import AffectiveEvent, DesireChange, Evolution

# `setup` drives one mind through the public API; `env` adds the planning side, which is where
# the wish sync takes a revision. Loaded as plugins rather than imported, so the fixture names
# stay the fixtures' and not this module's.
pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

# Where the writer will always keep a whole snapshot, whatever the chain depth says.
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


# --- the format itself ---------------------------------------------------------------------

def test_a_keyed_diff_of_depth_two_round_trips_every_shape_it_meets():
    base = {"same": 1, "dropped": [1, 2], "scalar": "before", "listed": [1, 2, 3],
            "entity": {"one": {"deep": 1}, "two": 2, "gone": 3}}
    target = {"same": 1, "scalar": 7, "listed": [3], "new": {"x": 1},
              "entity": {"one": {"deep": 2}, "two": 2, "added": 4}}
    patch = history.diff(base, target)
    assert history.apply(base, patch) == target
    assert history.canonical(history.apply(base, patch)) == history.canonical(target)
    # The source document is left alone, and nothing addresses more than two keys in.
    assert base["entity"]["one"] == {"deep": 1} and base["dropped"] == [1, 2]
    assert all(1 <= len(op[1]) <= history.MAX_DEPTH for op in patch)
    # A change below depth two replaces that value whole rather than reaching further in, and a
    # list is replaced whole however small the change to it was.
    assert ["set", ["entity", "one"], {"deep": 2}] in patch
    assert ["set", ["listed"], [3]] in patch
    assert ["del", ["entity", "gone"]] in patch and ["del", ["dropped"]] in patch
    # A key that did not move contributes nothing at all.
    assert not [op for op in patch if op[1][:1] == ["same"]]


def test_the_same_pair_always_produces_the_same_patch():
    base, target = {"b": 1, "a": {"y": 1, "x": 2}}, {"a": {"x": 3, "z": 4}, "c": 5}
    assert history.diff(base, target) == history.diff(base, target)
    assert [op[1] for op in history.diff(base, target)] == [["a", "x"], ["a", "y"], ["a", "z"], ["b"], ["c"]]


@pytest.mark.parametrize("patch", [
    [["set", ["a", "b", "c"], 1]],
    [["del", ["not-here"]]],
    [["del", ["a", "not-here"]]],
    [["set", ["scalar", "inner"], 1]],
    [["move", ["a"], 1]],
    [["set", ["a"]]],
    [["del", ["a"], 1]],
    [["set", "a", 1]],
    ["set"],
    "not a patch at all",
])
def test_a_patch_that_does_not_fit_is_refused_rather_than_half_applied(patch):
    base = {"a": {"b": 1}, "scalar": "text"}
    with pytest.raises(Conflict) as raised:
        history.apply(base, patch)
    assert raised.value.code == history.REBUILD_FAILED
    assert base == {"a": {"b": 1}, "scalar": "text"}


# --- rebuilding a real history ---------------------------------------------------------------

def test_every_revision_rebuilds_byte_identical_as_written(setup):
    mind, source, clock = setup
    drive(mind, source, clock)
    texts = stored(mind)
    assert len(texts) >= 100
    with mind.engine.db.connect() as conn:
        for revision, text in texts.items():
            assert history.canonical(history.materialize(conn, mind.scope.key(), revision)) == text


def test_every_revision_rebuilds_byte_identical_once_it_is_stored_as_patches(setup):
    mind, source, clock = setup
    drive(mind, source, clock)
    texts = stored(mind)
    repack(mind, checkpoint_every=50)
    with mind.engine.db.connect() as conn:
        for revision, text in texts.items():
            assert history.canonical(history.materialize(conn, mind.scope.key(), revision)) == text
        # It really is a chain and not a hundred disguised snapshots: the deepest rebuild above
        # walked forty-odd patches from its checkpoint before it was allowed to answer.
        rows = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_events WHERE scope=?",
                                                       (mind.scope.key(),))]
    assert max(row.get("depth", 0) for row in rows) >= 40
    assert len([row for row in rows if history.is_patch(row)]) >= len(texts) - 5


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


# --- the revision that has no row of its own --------------------------------------------------

def test_sync_wishes_writes_the_revision_it_takes(env):
    mind, plans, _source, _clock, _initial = env
    plan = decide(env, create(env, actor="contact"))
    before = mind.read()["revision"]
    created = plans.sync_wishes()
    assert created and mind.read()["revision"] == before + 1
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT kind,data FROM mind_events WHERE scope=? AND revision=?",
                           (mind.scope.key(), before + 1)).fetchone()
        revisions = [r[0] for r in conn.execute(
            "SELECT revision FROM mind_events WHERE scope=? ORDER BY revision", (mind.scope.key(),))]
    assert row["kind"] == "plan-wish-sync"
    assert json.loads(row["data"])["request"] == {"desire_ids": created}
    # And no hole anywhere: every revision the mind has taken owns a row.
    assert revisions == list(range(1, before + 2))
    assert plan["id"]


def test_the_reversion_reads_the_row_before_it_not_the_revision_before_it(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    # A host operation that moves the revision without writing a row, which is what the wish sync
    # used to do. The reversion's parent is then two revisions back, and arithmetic finds nothing.
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state["revision"] += 1
        mind._save(conn, state)
    evolution = evolve(mind, source, clock)
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM mind_events WHERE scope=? AND revision=?",
                                (mind.scope.key(), evolution["revision"] - 1)).fetchone()
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 77
    revert(mind, source, clock, evolution["event_id"])
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 75


@pytest.mark.parametrize("parent", ["v1", "patch", "checkpoint", "evolution-too"])
def test_a_reversion_crosses_the_format_boundary_in_either_direction(setup, parent):
    """What the reversion restores is the row *before* the evolution, so that row's shape is what
    the boundary is about — a whole snapshot as written today, a patch deep in a chain, or a
    checkpoint in the new format. The last case adds the evolution row itself in the new format,
    which the reversion never reads and must therefore not care about."""
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    if parent == "patch":
        repack(mind, checkpoint_every=50)
    if parent == "checkpoint":
        repack(mind, checkpoint_every=0)
    evolution = evolve(mind, source, clock)
    if parent == "evolution-too":
        repack(mind, checkpoint_every=50)
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 77
    revert(mind, source, clock, evolution["event_id"])
    current = mind.read()
    assert current["dimensions"]["curiosity"]["baseline"] == 75 and not current["traits"]


# --- what happens when it cannot be rebuilt ---------------------------------------------------

@pytest.mark.parametrize("packed", [False, True])
def test_a_revision_that_does_not_hash_fails_closed_without_taking_the_list_with_it(setup, packed):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=6)
    if packed:
        repack(mind, checkpoint_every=4)
    view = mind.read(history=6)["history"]
    broken_at = view[3]["revision"]
    corrupt(mind, broken_at)
    after = mind.read(history=6)["history"]
    spoiled = {entry["revision"] for entry in after if entry["snapshot"] is None}
    intact = [entry for entry in after if entry["snapshot"] is not None]
    assert all(entry["history_error"] == history.REBUILD_FAILED
               for entry in after if entry["snapshot"] is None)
    # A patch is answered for by the rows under it, so a break in one is found the moment it is
    # walked, and everything resting on it goes with it. A whole snapshot answers for itself, and
    # a list of a hundred of them is not where a hundred documents get hashed: that sweep is what
    # `history-verify` is for, and not what a path a reply is waiting behind should be doing. So
    # the break is still caught — by whatever rebuilds through it, a few lines below — but it is
    # no longer caught by the listing merely mentioning the row.
    with mind.engine.db.connect() as conn:
        shape = history.shape_of(json.loads(conn.execute(
            "SELECT data FROM mind_events WHERE scope=? AND revision=?",
            (mind.scope.key(), broken_at)).fetchone()[0]))
    assert (broken_at in spoiled) == (shape == "patch")
    # And nothing older than the break goes with it, in either shape.
    assert spoiled <= {broken_at} or packed
    assert all(revision >= broken_at for revision in spoiled)
    # What survived is unchanged, keys and all.
    assert intact and all(set(entry) == {"id", "kind", "revision", "occurred_at", "request", "snapshot"}
                          for entry in intact)
    assert [entry for entry in intact if entry["revision"] < broken_at]
    with mind.engine.db.connect() as conn, pytest.raises(Conflict) as raised:
        history.materialize(conn, mind.scope.key(), broken_at)
    assert raised.value.code == history.REBUILD_FAILED


def test_a_reversion_onto_an_unverifiable_profile_refuses_and_changes_nothing(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    evolution = evolve(mind, source, clock)
    corrupt(mind, evolution["revision"] - 1)
    before = mind.read()
    with pytest.raises(Conflict) as raised:
        revert(mind, source, clock, evolution["event_id"])
    assert raised.value.code == history.REBUILD_FAILED
    after = mind.read()
    assert after["revision"] == before["revision"]
    # The evolved personality is still the one in force: nothing was restored from what could not
    # be verified, and nothing was half restored either.
    assert after["dimensions"]["curiosity"]["baseline"] == 77
    assert after["profile_version"] == before["profile_version"]


def test_a_row_that_will_not_even_parse_still_says_a_revision_happened(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    view = mind.read(history=4)["history"]
    broken_at = view[1]["revision"]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     ("{not json at all", mind.scope.key(), broken_at))
    after = mind.read(history=4)["history"]
    broken = next(entry for entry in after if entry["revision"] == broken_at)
    assert broken["snapshot"] is None and broken["request"] is None
    assert broken["history_error"] == history.REBUILD_FAILED
    assert broken["kind"] and broken["occurred_at"] and broken["id"]
    assert len([entry for entry in after if entry["snapshot"] is not None]) == 3


def test_a_row_written_before_the_hash_existed_is_still_held_to_its_own_revision(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        revision = conn.execute("SELECT MAX(revision) FROM mind_events WHERE scope=?", (scope,)).fetchone()[0]
        data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                       (scope, revision)).fetchone()[0])
        # Exactly the shape of every row the database already holds: no hash to check it against.
        data.pop("state_hash")
        data["snapshot"]["revision"] = revision - 1
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (dumps(data), scope, revision))
    with mind.engine.db.connect() as conn, pytest.raises(Conflict) as raised:
        history.materialize(conn, scope, revision)
    assert raised.value.code == history.REBUILD_FAILED


def test_the_parent_is_named_and_verified_in_one_answer(setup):
    """What the later writer needs to compute a patch: the number it records as `base`, and the
    document that number stands for, agreed on by one query."""
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        revision = conn.execute("SELECT MAX(revision) FROM mind_events WHERE scope=?", (scope,)).fetchone()[0]
        at, state = history.parent(conn, scope, revision)
        assert at == revision - 1 and state["revision"] == at
        assert history.before(conn, scope, revision) == state
        current = history.materialize(conn, scope, revision)
        # And the patch the writer would store really carries one to the other.
        assert history.apply(state, history.diff(state, current)) == current
        # A whole snapshot stands on nothing; the brief's other "depth" is the path length.
        assert history.chain_depth(json.loads(conn.execute(
            "SELECT data FROM mind_events WHERE scope=? AND revision=?", (scope, revision)).fetchone()[0])) == 0
    repack(mind, checkpoint_every=50)
    with mind.engine.db.connect() as conn:
        assert history.chain_depth(json.loads(conn.execute(
            "SELECT data FROM mind_events WHERE scope=? AND revision=?", (scope, revision)).fetchone()[0])) > 0


def test_a_missing_parent_is_a_refusal_and_never_a_silent_fallback(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=2)
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        first = conn.execute("SELECT MIN(revision) FROM mind_events WHERE scope=?", (scope,)).fetchone()[0]
        with pytest.raises(Conflict) as raised:
            history.before(conn, scope, first)
        assert raised.value.code == history.REBUILD_FAILED
        with pytest.raises(Conflict):
            history.materialize(conn, scope, 10_000)


@pytest.mark.parametrize("break_it", ["base_forward", "base_missing", "base_hash", "no_snapshot"])
def test_a_chain_that_does_not_hold_together_is_refused(setup, break_it):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    repack(mind, checkpoint_every=50)
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        revision = conn.execute("SELECT MAX(revision) FROM mind_events WHERE scope=?", (scope,)).fetchone()[0]
        data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                       (scope, revision)).fetchone()[0])
        if break_it == "base_forward":
            data["base"] = revision + 1
        elif break_it == "base_missing":
            data["base"] = revision - 100
        elif break_it == "base_hash":
            data["base_hash"] = "0" * 64
        else:
            data.pop("patch"), data.pop("format")
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (dumps(data), scope, revision))
    with mind.engine.db.connect() as conn, pytest.raises(Conflict) as raised:
        history.materialize(conn, scope, revision)
    assert raised.value.code == history.REBUILD_FAILED


# --- the seam the later packages register on ---------------------------------------------------

def test_an_unregistered_history_command_is_refused_by_name():
    from kin_mind import history_admin

    with pytest.raises(ValueError):
        history_admin.dispatch(None, "history-nothing-registered", {})
    with pytest.raises(RuntimeError):
        history_admin.command("compact")(lambda mind: None)
