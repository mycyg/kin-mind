"""Stage 5 WP3: the writer that stores what changed, behind a flag that is off until it is not.

WP1 pinned the reader against a history rewritten into the patch shape by hand. This is the same
contract asked of the real writer, and of the one thing the hand-rewritten fixture could not
answer for: what a revision was *actually* worth storing, at the moment it was stored, with only
the row before it to go on.

Every rebuild here is checked against the state the mind really held at that revision, captured
from `mind_state` as each command committed rather than read back out of the row being tested.
That is the whole claim of this package — that four hundred and eighty megabytes of repeated
document can be replaced by what changed and nothing is lost — and it is not a claim that can be
tested against the thing under test.

The other half is the flag. Off, this writes the row the store already writes, and that is
asserted key for key, because the release that deploys this code has to remain a release the host
can go back to. On is a decision taken later, once `history-verify` reads clean.
"""

import json
from datetime import datetime, timedelta

import pytest
from test_history_layer import evolve, revert

from eventmem.core.db import Conflict, dumps
from kin_mind import history
from kin_mind.history_admin import dispatch
from kin_mind.memory import MemoryContinuity
from kin_mind.state import AffectiveEvent, DesireChange

pytest_plugins = ("test_kin_mind",)

# The keys of the row this store writes today. With the flag off not one of them may move.
UNCHANGED_ROW = {"request", "snapshot", "state_hash"}


def patches(mind, on=True):
    MemoryContinuity(mind).configure({"history_patches": on})


def live(mind):
    """The exact text the mind's state is stored as right now."""
    with mind.engine.db.connect() as conn:
        return conn.execute("SELECT data FROM mind_state WHERE scope=?",
                            (mind.scope.key(),)).fetchone()[0]


def drive(mind, source, clock, *, rounds=8, start=0):
    """Revisions the way the host writes them, and the exact state each one left behind.

    The ground truth is taken from `mind_state` as each command commits, because a rebuild
    compared against the row it was rebuilt from proves nothing at all."""
    texts, revision = {}, mind.read()["revision"]
    texts[revision] = live(mind)

    def note(revision):
        texts[revision] = live(mind)
        return revision

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
                command_id="abandon-" + str(index), agent_version="synthetic-v1",
                expected_revision=revision, evidence_ids=[source("reconsidered-" + str(index))],
                action="abandon", desire_id=wish["desire_id"], reason="No longer current"))["revision"])
        if index % 7 == 0:
            revision = note(mind.configure_contact({
                "command_id": "preference-" + str(index), "agent_version": "synthetic-v1",
                "expected_revision": revision, "evidence_ids": [source("owner-preference-" + str(index))],
                "wait_for_reply": bool(index % 2), "reason": "The owner said which they prefer"})["revision"])
    return texts


def rows(mind):
    """revision -> the row's parsed contents, or None where a test has broken one on purpose."""
    def parsed(text):
        try:
            return json.loads(text)
        except ValueError:
            return None

    with mind.engine.db.connect() as conn:
        return {row["revision"]: parsed(row["data"]) for row in conn.execute(
            "SELECT revision,data FROM mind_events WHERE scope=? ORDER BY revision",
            (mind.scope.key(),)).fetchall()}


def rebuilds(mind, texts):
    """Every captured revision, rebuilt from whatever its row turned out to be, byte for byte."""
    with mind.engine.db.connect() as conn:
        for revision, text in texts.items():
            assert history.canonical(history.materialize(conn, mind.scope.key(), revision)) == text


def run(mind, action, request=None):
    return dispatch(mind, action, request or {})


# --- the flag ---------------------------------------------------------------------------------

def test_the_flag_is_off_until_a_store_says_otherwise(setup):
    mind, _source, _clock = setup
    with mind.engine.db.connect() as conn:
        assert not history.writes_patches(conn, mind.scope.key())
    # Every other stage-5 flag is on by default and stays on; this one is not carried along by
    # them, and configuring the rest does not switch the write format.
    MemoryContinuity(mind).configure({"evidence_key_index": True, "history_legacy_guard": True})
    with mind.engine.db.connect() as conn:
        assert not history.writes_patches(conn, mind.scope.key())
    assert MemoryContinuity(mind).settings()["history_patches"] is False


def test_with_the_flag_off_the_row_does_not_move_by_one_byte(setup):
    mind, source, clock = setup
    texts = drive(mind, source, clock, rounds=4)
    for revision, data in rows(mind).items():
        assert set(data) == UNCHANGED_ROW
        assert history.canonical(data["snapshot"]) == texts[revision]
        # And byte for byte the row that layer would write from the same two pieces.
        assert dumps(data) == dumps(history.snapshot_row(data["request"], data["snapshot"]))
    rebuilds(mind, texts)


def test_the_flag_is_a_boolean_and_nothing_else(setup):
    mind, _source, _clock = setup
    with pytest.raises(ValueError):
        MemoryContinuity(mind).configure({"history_patches": "yes"})


# --- what the writer stores -------------------------------------------------------------------

def test_every_revision_rebuilds_byte_identical_with_the_flag_on_throughout(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=45)
    stored = rows(mind)
    assert len(texts) >= 100
    rebuilds(mind, texts)
    # It really is storing what changed: most rows are patches, and a patch is a fraction of the
    # document it stands in for.
    written = [data for data in stored.values() if history.is_patch(data)]
    assert len(written) > len(stored) * 0.8
    assert sum(len(dumps(data)) for data in written) < sum(len(text) for text in texts.values()) / 4


def test_every_revision_rebuilds_byte_identical_when_the_flag_is_turned_on_midway(setup):
    mind, source, clock = setup
    texts = drive(mind, source, clock, rounds=6)
    first_new = max(texts) + 1
    patches(mind)
    texts |= drive(mind, source, clock, rounds=6, start=6)
    stored = rows(mind)
    # The boundary is real: whole documents below it, patches above, and the first patch is
    # computed against a row the previous release wrote.
    assert not any(history.is_patch(data) for revision, data in stored.items() if revision < first_new)
    crossing = min(revision for revision, data in stored.items() if history.is_patch(data))
    assert stored[crossing]["base"] < first_new
    assert set(stored[stored[crossing]["base"]]) == UNCHANGED_ROW
    rebuilds(mind, texts)


def test_the_view_a_caller_reads_is_unchanged_across_the_boundary(setup):
    mind, source, clock = setup
    texts = drive(mind, source, clock, rounds=3)
    patches(mind)
    texts |= drive(mind, source, clock, rounds=3, start=3)
    view = mind.read(history=100)["history"]
    assert len(view) == len(texts)
    assert {key for entry in view for key in entry} == {
        "id", "kind", "revision", "occurred_at", "request", "snapshot"}
    for entry in view:
        assert history.canonical(entry["snapshot"]) == texts[entry["revision"]]


def test_a_value_that_python_calls_equal_but_json_does_not_still_rebuilds(setup):
    """The hazard the writer's own round-trip check exists for, in isolation.

    Python reads 1, 1.0 and True as one value. JSON writes three different things. A revision
    that stores the integer where the document held the float changes the bytes without changing
    the value, and a diff that trusted `==` would not mention it."""
    base = {"a": 1.0, "b": {"c": 2.0}, "d": [1.0], "e": True}
    target = {"a": 1, "b": {"c": 2}, "d": [1], "e": 1}
    patch = history.diff(base, target)
    assert history.canonical(history.apply(base, patch)) == history.canonical(target)
    # And the other way, which is the direction a decayed score actually travels.
    assert history.canonical(history.apply(target, history.diff(target, base))) == history.canonical(base)


# --- where a whole state is kept ----------------------------------------------------------------

def test_the_kinds_that_must_answer_for_themselves_carry_a_whole_state(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=3)
    evolution = evolve(mind, source, clock)
    reversion = revert(mind, source, clock, evolution["event_id"])
    stored = rows(mind)
    # The first revision is written before any of this exists, so it is the one kind whose rule is
    # only visible in the list; the personality changes are written under it and are whole states
    # in the new format, each carrying its own `format` marker and hash.
    assert "initialize" in history.CHECKPOINT_KINDS and history.snapshot_of(stored[1])
    for revision in (evolution["revision"], reversion["revision"]):
        assert history.snapshot_of(stored[revision])
        assert stored[revision]["format"] == history.PATCH_FORMAT and stored[revision]["state_hash"]
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 75
    rebuilds(mind, texts)


@pytest.mark.parametrize("parent", ["v1", "patch", "checkpoint"])
def test_a_reversion_crosses_the_boundary_the_writer_really_made(setup, parent, monkeypatch):
    """What the reversion restores is the row *before* the evolution, so that row's shape is what
    the boundary is about: a whole snapshot as the previous release wrote it, a patch in a chain,
    or a whole state in the new format."""
    mind, source, clock = setup
    if parent == "checkpoint":
        # Nothing is ever worth storing as a patch, so every row is a whole state in the new
        # format — the shape a chain leaves behind wherever the policy checkpoints it.
        monkeypatch.setattr(history, "CHECKPOINT_SHARE", 10_000)
    if parent != "v1":
        patches(mind)
    drive(mind, source, clock, rounds=3)
    with mind.engine.db.connect() as conn:
        last = json.loads(conn.execute(
            "SELECT data FROM mind_events WHERE scope=? ORDER BY revision DESC LIMIT 1",
            (mind.scope.key(),)).fetchone()[0])
    assert history.shape_of(last) == {"v1": "checkpoint", "patch": "patch",
                                      "checkpoint": "checkpoint"}[parent]
    assert history.is_patch(last) is (parent == "patch")
    patches(mind)
    evolution = evolve(mind, source, clock)
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 77
    revert(mind, source, clock, evolution["event_id"])
    current = mind.read()
    assert current["dimensions"]["curiosity"]["baseline"] == 75 and not current["traits"]


def test_the_chain_never_grows_past_the_depth_the_policy_allows(setup, monkeypatch):
    mind, source, clock = setup
    monkeypatch.setattr(history, "CHECKPOINT_DEPTH", 5)
    patches(mind)
    texts = drive(mind, source, clock, rounds=8)
    stored = rows(mind)
    assert max(history.chain_depth(data) for data in stored.values()) < 5
    # Which means a whole state is never far away: the deepest read walks four rows.
    whole = sorted(revision for revision, data in stored.items() if not history.is_patch(data))
    assert len(whole) >= len(stored) // 5
    rebuilds(mind, texts)


@pytest.mark.parametrize("depth,since,weight,size,whole", [
    (1, 10, 10, 1000, False),
    (history.CHECKPOINT_DEPTH, 1, 1, 1000, True),
    (history.CHECKPOINT_DEPTH - 1, 1, 1, 1000, False),
    (1, 1000, 1, 1000, True),
    (1, 999, 1, 1000, False),
    (1, 10, 501, 1000, True),
    (1, 10, 500, 1000, False),
])
def test_each_rule_that_keeps_a_whole_state_stands_on_its_own(depth, since, weight, size, whole):
    assert history.keeps_whole(depth, since, weight, size) is whole


def test_a_patch_worth_more_than_the_document_is_not_stored_as_one(setup, monkeypatch):
    mind, source, clock = setup
    monkeypatch.setattr(history, "CHECKPOINT_SHARE", 10_000)
    patches(mind)
    texts = drive(mind, source, clock, rounds=3)
    assert not any(history.is_patch(data) for data in rows(mind).values())
    rebuilds(mind, texts)


def test_the_patches_standing_on_one_state_never_cost_more_than_that_state(setup, monkeypatch):
    mind, source, clock = setup
    # Only the byte rule can fire: the chain is allowed to be far deeper than this run goes, and
    # no single patch is ever half a document.
    monkeypatch.setattr(history, "CHECKPOINT_DEPTH", 10_000)
    monkeypatch.setattr(history, "CHECKPOINT_SHARE", 1)
    patches(mind)
    texts = drive(mind, source, clock, rounds=20)
    stored = rows(mind)
    for revision, data in stored.items():
        if history.is_patch(data):
            assert data["since"] < len(texts[revision].encode())
    # And the rule really fired: somewhere in twenty rounds the patches caught up with the
    # document they stand on, and a whole state was written that no other rule asked for.
    assert [revision for revision, data in stored.items()
            if revision > 1 and not history.is_patch(data)]
    rebuilds(mind, texts)


# --- healing itself -----------------------------------------------------------------------------

def test_a_parent_that_cannot_be_read_is_answered_with_a_whole_state(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=4)
    broken = max(texts)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     ("{not json at all", mind.scope.key(), broken))
    texts |= drive(mind, source, clock, rounds=1, start=9)
    healed = rows(mind)[broken + 1]
    assert healed["healed"] == "parent" and history.snapshot_of(healed)
    # The break stays broken — nothing here rewrites a row — and everything after it is sound.
    with mind.engine.db.connect() as conn:
        for revision in sorted(texts):
            if revision <= broken:
                continue
            assert history.canonical(history.materialize(conn, mind.scope.key(), revision)) == texts[revision]


def test_a_parent_whose_bytes_do_not_match_its_hash_is_answered_the_same_way(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=4)
    broken = max(texts)
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                       (mind.scope.key(), broken)).fetchone()[0])
        data["state_hash"] = "0" * 64
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (dumps(data), mind.scope.key(), broken))
    drive(mind, source, clock, rounds=1, start=9)
    assert rows(mind)[broken + 1]["healed"] == "parent"


def test_a_revision_taken_without_a_row_is_diffed_against_the_row_that_is_there(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=3)
    # A host operation that moves the revision without writing a row, which is what the wish sync
    # used to do. The next patch is computed against the row before, not against `revision - 1`.
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state["revision"] += 1
        mind._save(conn, state)
        gap = state["revision"]
    texts |= drive(mind, source, clock, rounds=1, start=8)
    stored = rows(mind)
    assert gap not in stored
    written = min(revision for revision in stored if revision > gap)
    assert stored[written]["base"] == gap - 1
    with mind.engine.db.connect() as conn:
        for revision in (written, max(stored)):
            assert history.canonical(
                history.materialize(conn, mind.scope.key(), revision)) == texts[revision]


# --- the old reader's shim ------------------------------------------------------------------------

def test_every_patch_row_carries_the_two_keys_the_previous_release_reads(setup):
    mind, source, clock = setup
    patches(mind)
    drive(mind, source, clock, rounds=6)
    for revision, data in rows(mind).items():
        if history.is_patch(data):
            assert set(data["snapshot"]) == set(history.SHIM_KEYS)
            assert data["snapshot"]["revision"] == revision


def test_the_previous_release_still_finds_the_evidence_key_in_a_patch_row(setup):
    mind, source, clock = setup
    patches(mind)
    drive(mind, source, clock, rounds=4)
    with mind.engine.db.connect() as conn:
        key = mind._load(conn)["last_evidence_key"]
        # The literal guard query the previous release ships, run unchanged.
        assert conn.execute(
            "SELECT 1 FROM mind_events WHERE scope=? AND kind='affect' "
            "AND json_extract(data,'$.snapshot.last_evidence_key')=? LIMIT 1",
            (mind.scope.key(), key)).fetchone()
    # And the table that will one day replace that query agrees with it, row for row, which it
    # can only do if the shim carries the key the scan reads.
    from kin_mind.evidence_keys import verify as verify_keys
    assert verify_keys(mind)["state"] == "verified"


def test_a_mind_that_has_scored_nothing_yet_writes_the_shim_without_a_key(setup, monkeypatch):
    """`last_evidence_key` is legitimately absent until the first appraisal, and the shim says so
    rather than inventing one: absent and null read the same through `json_extract`."""
    mind, source, clock = setup
    # The subject here is the shim, so the one rule that could make this row a whole state for an
    # unrelated reason — a first change that is large next to a still-small document — is off.
    monkeypatch.setattr(history, "CHECKPOINT_SHARE", 1)
    patches(mind)
    revision = mind.read()["revision"]
    clock[0] += timedelta(minutes=3)
    mind.configure_contact({"command_id": "first-preference", "agent_version": "synthetic-v1",
                            "expected_revision": revision, "evidence_ids": [source("owner-said")],
                            "wait_for_reply": True, "reason": "The owner said which they prefer"})
    stored = rows(mind)[revision + 1]
    assert stored["snapshot"] == {"last_evidence_key": None, "revision": revision + 1}
    with mind.engine.db.connect() as conn:
        assert history.snapshot_of(stored) is None
        assert history.canonical(history.materialize(conn, mind.scope.key(), revision + 1)) == live(mind)


# --- while compaction owns the rows ---------------------------------------------------------------

def test_nothing_is_written_while_compaction_holds_the_history(setup):
    mind, source, clock = setup
    texts = drive(mind, source, clock, rounds=2)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO meta VALUES(?,?)", (history.COMPACTION_MARKER, 1))
    clock[0] += timedelta(minutes=5)
    with pytest.raises(Conflict) as raised:
        mind.record(AffectiveEvent(
            command_id="during-compaction", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[source("observed-later")],
            values={"mood": 44}, reason="A sourced synthetic observation"))
    assert raised.value.code == history.COMPACTING
    # Nothing landed: not the row, and not the revision either.
    assert set(rows(mind)) == set(texts)
    assert mind.read()["revision"] == max(texts)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM meta WHERE key=?", (history.COMPACTION_MARKER,))
    assert mind.record(AffectiveEvent(
        command_id="after-compaction", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("observed-after")],
        values={"mood": 44}, reason="A sourced synthetic observation"))["revision"] == max(texts) + 1


# --- what an operator is told ----------------------------------------------------------------------

def test_status_names_the_row_a_rollback_stops_at(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=3)
    before = run(mind, "history-status")
    assert before["writing_patches"] is False and before["first_patch_revision"] is None
    assert before["rows"] == before["rows_checkpoint"] and before["rows_patch"] == 0
    patches(mind)
    drive(mind, source, clock, rounds=3, start=3)
    after = run(mind, "history-status")
    assert after["writing_patches"] is True and after["compacting"] is False
    assert after["first_patch_revision"] > before["last_revision"]
    assert after["rows_patch"] > 0 and after["patch_bytes"] < after["bytes"]
    assert after["deepest"] >= 1 and after["healed"] == 0


def test_verification_never_calls_a_row_it_could_not_check_verified(setup):
    mind, source, clock = setup
    patches(mind)
    drive(mind, source, clock, rounds=4)
    assert run(mind, "history-verify")["state"] == "verified"
    # Now the shape every row in the store already has: a whole state and no hash to check it
    # against. They are never rewritten, so they stay this way, and the report has to keep saying
    # so rather than growing quietly into a clean bill of health.
    whole = [revision for revision, data in rows(mind).items() if not history.is_patch(data)]
    with mind.engine.db.connect(write=True) as conn:
        for revision in whole:
            data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                           (mind.scope.key(), revision)).fetchone()[0])
            data.pop("state_hash", None)
            conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                         (dumps(data), mind.scope.key(), revision))
    checked = run(mind, "history-verify")
    assert checked["state"] == "partial"
    assert checked["unverifiable"] == len(whole) and checked["failed"] == 0
    assert checked["verified"] + checked["unverifiable"] == checked["rows"]
    assert checked["rows_legacy"] == len(whole) and checked["rows_checkpoint"] == 0
    assert checked["unverifiable_reason"]


def test_verification_finds_a_break_the_listing_no_longer_hashes_for(setup):
    """The sweep is where a whole state's bytes are checked, and it is the only place.

    A view of a hundred revisions is not going to hash a hundred documents on the path a reply is
    waiting behind, so a corrupted whole state is listed as if it were fine — right up until
    something rebuilds through it, which is what this command does to every row at once."""
    mind, source, clock = setup
    patches(mind)
    drive(mind, source, clock, rounds=5)
    stored = rows(mind)
    assert history.snapshot_of(stored[1])
    with mind.engine.db.connect(write=True) as conn:
        data = stored[1]
        data["snapshot"]["updated_at"] = "2001-01-01T00:00:00+00:00"
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (dumps(data), mind.scope.key(), 1))
    listed = {entry["revision"] for entry in mind.read(history=100)["history"]
              if entry["snapshot"] is None}
    assert 1 not in listed
    checked = run(mind, "history-verify")
    assert checked["state"] == "incomplete"
    assert 1 in checked["failures"] and checked["failed"] >= 1


def test_the_commands_write_nothing_at_all(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=3)
    with mind.engine.db.connect() as conn:
        before = [row["data"] for row in conn.execute(
            "SELECT data FROM mind_events WHERE scope=? ORDER BY revision", (mind.scope.key(),))]
    run(mind, "history-status")
    run(mind, "history-verify")
    with mind.engine.db.connect() as conn:
        assert [row["data"] for row in conn.execute(
            "SELECT data FROM mind_events WHERE scope=? ORDER BY revision",
            (mind.scope.key(),))] == before
    assert mind.read()["revision"] == max(texts)


# --- the replay an operator runs on a copy before deciding ------------------------------------------

def test_a_history_already_stored_replays_into_patches_from_any_point(setup):
    """What proving this on a copy of the real store comes down to.

    The rows below the flip are left exactly as they are, because that is what deploying this
    does to a store: it never rewrites a row. So the first patch really does stand on a row of
    the shape the store already holds — a whole document with no hash to check it by — and the
    boundary under test is the one that will exist. Everything then rebuilds against what each
    revision held before any of it was touched."""
    mind, source, clock = setup
    texts = drive(mind, source, clock, rounds=10)
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        for revision in texts:
            data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                           (scope, revision)).fetchone()[0])
            data.pop("state_hash", None)
            conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                         (dumps(data), scope, revision))
    flip = sorted(texts)[len(texts) // 2]
    patches(mind)
    with mind.engine.db.connect(write=True) as conn:
        for revision in sorted(texts):
            if revision < flip:
                continue
            row = conn.execute("SELECT kind,data FROM mind_events WHERE scope=? AND revision=?",
                               (scope, revision)).fetchone()
            data = json.loads(row["data"])
            conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                         (dumps(history.row_for(conn, scope, data["snapshot"], row["kind"],
                                                data.get("request"))), scope, revision))
    rebuilds(mind, texts)
    stored = rows(mind)
    below = [revision for revision in texts if revision < flip]
    assert all(history.shape_of(stored[revision]) == "legacy" for revision in below)
    status = run(mind, "history-status")
    assert status["first_patch_revision"] >= flip and status["rows_legacy"] == len(below)
    checked = run(mind, "history-verify")
    assert checked["state"] == "partial" and checked["failed"] == 0
    assert checked["unverifiable"] == len(below)
    assert checked["verified"] == len(texts) - len(below)


# --- the rollback code path -----------------------------------------------------------------------

def test_the_flag_turned_back_off_reads_both_shapes_and_writes_the_old_one(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=4)
    patches(mind, on=False)
    first_old = max(texts) + 1
    texts |= drive(mind, source, clock, rounds=2, start=9)
    stored = rows(mind)
    assert any(history.is_patch(data) for data in stored.values())
    assert all(set(data) == UNCHANGED_ROW for revision, data in stored.items() if revision >= first_old)
    rebuilds(mind, texts)
    view = mind.read(history=100)["history"]
    assert len(view) == len(texts)
    for entry in view:
        assert history.canonical(entry["snapshot"]) == texts[entry["revision"]]
