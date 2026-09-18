"""Stage 5 WP5: a finished wish moves out of the state document without leaving the mind.

The document carries every wish that was ever made and is written again on every revision. What
this package removes from it is only ever what is finished and what nothing is still holding, and
what it removes is still there: the same identifier, the same plan links, the same evidence, the
same receipt, in a table of its own. So most of what is asserted here is not that wishes move. It
is that moving one changes no answer — the same refusal on an update, the same block on a second
contact intent for one sharing decision, the same projection window, the same count of how many
wishes this mind has — and that the wishes which must never move never do.

Synthetic replays only: an injected clock, sources through the engine, no model and no network.
"""

import json
from datetime import datetime, timedelta

import pytest
from test_autonomous_plans import create, decide

from eventmem.core.db import Conflict, Missing, digest, dumps
from kin_mind import desire_archive, manifest
from kin_mind.appraisal import appraisal_context
from kin_mind.exploration import Explorations
from kin_mind.host import dispatch
from kin_mind.memory import MemoryContinuity
from kin_mind.state import DesireChange

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

DAY = timedelta(days=1)


# --- driving one mind -----------------------------------------------------------------------

def allow(mind, on=True):
    MemoryContinuity(mind).configure({"desire_archive": on})


def make(mind, source, key, *, kind="contact", days=3, **extra):
    """One wish, through the public API."""
    return mind.manage_desire(DesireChange(
        command_id=key, agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source(key)], action="create", content="Share finding " + key,
        topic="synthetic", kind=kind, strength=50,
        expires_at=(datetime.fromisoformat(mind.clock()) + days * DAY).isoformat(),
        completion="The owner has it", reason="A finding worth discussing", **extra))


def settle(mind, source, identifier, action="complete", key=None):
    return mind.manage_desire(DesireChange(
        command_id=key or (action + ":" + identifier), agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source(key or action + identifier)],
        action=action, desire_id=identifier, reason="Settled by the model"))


def finished(mind, source, clock, count, *, age=None):
    """`count` finished wishes, each settled and then left to age."""
    made = []
    for index in range(count):
        clock[0] += timedelta(minutes=5)
        wish = make(mind, source, "wish-" + str(index))
        settle(mind, source, wish["desire_id"], "complete" if index % 2 else "abandon")
        made.append(wish["desire_id"])
    if age:
        clock[0] += age
    return made


def crowd_out(mind, source, identifier, count=desire_archive.WINDOW):
    """Enough finished wishes sorting after this one to push it out of the projection's tail.

    The tail the projection shows is the end of the document's own order, and that order is the
    identifiers sorted — a wish identifier is a digest of the command that made it, so which
    finished wishes the tail holds has nothing to do with when they were made. A test that wants
    one particular wish to be archivable has to say so in those terms."""
    made, index = [], 0
    while len(made) < count:
        key, index = "filler-" + str(index), index + 1
        if "desire_" + digest([mind.scope.key(), key])[:32] <= identifier:
            continue
        wish = make(mind, source, key)
        settle(mind, source, wish["desire_id"], "abandon")
        made.append(wish["desire_id"])
    return made


def state_of(mind):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)


def rows(mind):
    with mind.engine.db.connect() as conn:
        return {row["id"]: dict(row) for row in conn.execute(
            "SELECT * FROM mind_desire_archive WHERE scope=?", (mind.scope.key(),))}


def plan(mind, days=desire_archive.DAYS, **extra):
    return desire_archive.archive(mind, days=days, **extra)


def move(mind, days=desire_archive.DAYS, **extra):
    return desire_archive.archive(mind, apply=True, days=days, **extra)


def config(mind):
    return {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
            "agent_version": "synthetic-v1", "session_id": "synthetic-session"}


# --- a move, never a delete -----------------------------------------------------------------

def test_a_finished_wish_moves_whole_and_leaves_the_document(setup):
    mind, source, clock = setup
    allow(mind)
    made = finished(mind, source, clock, 10, age=40 * DAY)
    before = state_of(mind)["desires"]
    result = move(mind)

    moved = result["moved"]
    # The newest of them stay: the projection shows a tail of finished wishes and this move is
    # not allowed to change what it shows.
    assert len(moved) == 10 - desire_archive.WINDOW
    stored, current = rows(mind), state_of(mind)
    assert set(stored) == set(moved)
    assert not set(moved) & set(current["desires"])
    assert set(current["desires"]) == set(before) - set(moved)
    for identifier in moved:
        kept = json.loads(stored[identifier]["data"])
        # Whole: the wish as the document held it, key for key and byte for byte.
        assert dumps(kept) == dumps(before[identifier])
        assert kept["id"] == identifier and kept["evidence"] and kept["status"] in {"completed", "abandoned"}
        assert stored[identifier]["status"] == kept["status"]
        assert stored[identifier]["archived_revision"] == current["revision"]
    assert current["desire_archive"] == {"count": len(moved)}
    assert mind.read()["desire_archive"] == {"count": len(moved)}
    assert {d["id"] for d in mind.read()["desires"]} == set(current["desires"])


def test_the_move_is_one_ordinary_revision_with_its_own_history_row(setup):
    mind, source, clock = setup
    allow(mind)
    finished(mind, source, clock, 10, age=40 * DAY)
    before = mind.read()["revision"]
    result = move(mind)
    assert result["revision"] == before + 1
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT kind,data FROM mind_events WHERE scope=? AND revision=?",
                           (mind.scope.key(), before + 1)).fetchone()
        revisions = [r[0] for r in conn.execute(
            "SELECT revision FROM mind_events WHERE scope=? ORDER BY revision", (mind.scope.key(),))]
    assert row["kind"] == desire_archive.KIND
    assert json.loads(row["data"])["request"]["desire_ids"] == result["moved"]
    # No hole: every revision this mind has taken owns a row of its own.
    assert revisions == list(range(1, before + 2))


def test_the_archive_reads_back_through_the_history_layer_with_patches_on(setup):
    mind, source, clock = setup
    allow(mind)
    MemoryContinuity(mind).configure({"history_patches": True})
    finished(mind, source, clock, 12, age=40 * DAY)
    moved = move(mind)["moved"]
    before = mind.read(history=6)["history"]
    from kin_mind.history_admin import dispatch as history_command
    checked = history_command(mind, "history-verify", {})
    assert moved and checked["failed"] == 0 and checked["shim_missing"] == []
    assert [entry["kind"] for entry in before if entry["kind"] == desire_archive.KIND]
    # Nothing about the move needs the reader to know it happened.
    assert mind.read(history=6)["history"] == before


# --- what never moves -----------------------------------------------------------------------

def test_an_expired_wanted_or_waiting_wish_is_never_touched(setup):
    mind, source, clock = setup
    allow(mind)
    wanted = make(mind, source, "still-wanted", days=1)["desire_id"]
    waiting = make(mind, source, "still-waiting", days=1)["desire_id"]
    settle(mind, source, waiting, "wait")
    clock[0] += 400 * DAY
    report = plan(mind, days=0)
    assert report["would_archive"] == []
    assert report["holding"][wanted] == [desire_archive.LIVE_WISH]
    assert report["holding"][waiting] == [desire_archive.LIVE_WISH]
    # Even asked to move everything, with the flag on and no floor at all.
    assert move(mind, days=0)["moved"] == []
    assert set(state_of(mind)["desires"]) == {wanted, waiting}


def test_age_is_a_floor_under_the_decision_and_never_a_reason_for_it(setup):
    mind, source, clock = setup
    allow(mind)
    running = make(mind, source, "long-running", days=900)["desire_id"]
    done = finished(mind, source, clock, 1)[0]
    crowd_out(mind, source, done)
    clock[0] += 400 * DAY
    # The oldest wish here is the one still being pursued, and no amount of time settles it.
    assert plan(mind, days=0)["holding"][running] == [desire_archive.LIVE_WISH]
    # And a settled one is held only while it is newer than the floor this run was given.
    assert desire_archive.RECENT in plan(mind, days=1000)["holding"][done]
    assert done in plan(mind, days=0)["would_archive"]


@pytest.mark.parametrize("held", ["contact-open", "action-open", "exploration-running",
                                  "plan-active", "run-open", "delivery-open"])
def test_a_wish_something_still_holds_does_not_move(setup, held):
    mind, source, clock = setup
    allow(mind)
    made = finished(mind, source, clock, 10, age=40 * DAY)
    target = plan(mind)["would_archive"][0]
    if held == "exploration-running":
        Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        if held == "contact-open":
            conn.execute("INSERT INTO mind_contacts VALUES(?,?,?,?)",
                         ("attempt", mind.scope.key(), "unconfirmed", dumps({"desire_id": target})))
        if held == "action-open":
            conn.execute("INSERT INTO mind_action_events VALUES(?,?,?,?,?,?)",
                         ("event", mind.scope.key(), "delivery", mind.clock(), "pending",
                          dumps({"desire_id": target})))
        if held == "exploration-running":
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                         ("exp", mind.scope.key(), "running", mind.clock(), dumps({"desire_id": target})))
        if held in {"plan-active", "run-open"}:
            document = {"id": "plan", "revision": 1, "goal": "Keep going", "desire_id": None,
                        "steps": [{"id": "reach", "desire_id": target}]}
            conn.execute("INSERT INTO mind_plans VALUES(?,?,?,?,?,?,?)",
                         ("plan", mind.scope.key(), 1, "active" if held == "plan-active" else "completed",
                          None, mind.clock(), dumps(document)))
            if held == "run-open":
                conn.execute("INSERT INTO mind_plan_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                             ("run", mind.scope.key(), "plan", "reach", "contact", "running", 0, "worker", 1,
                              dumps({"state": "running"})))
        if held == "delivery-open":
            state = mind._load(conn)
            state["desires"][target]["delivery"] = {"state": "partial", "message_id": "m1"}
            mind._save(conn, state)

    report = plan(mind)
    assert held in report["holding"][target]
    assert held in report["reasons"]
    assert target not in report["would_archive"]
    assert target not in move(mind)["moved"]
    assert target in state_of(mind)["desires"]


def test_the_projection_keeps_the_window_it_had_and_still_counts_every_wish(setup):
    mind, source, clock = setup
    allow(mind)
    finished(mind, source, clock, 14, age=40 * DAY)
    make(mind, source, "live-one")
    before = appraisal_context({"state": mind.read()})["state"]
    moved = move(mind)["moved"]
    after = appraisal_context({"state": mind.read()})["state"]
    assert moved
    # The same wishes, in the same order, with the same account of how many there are.
    assert [d["id"] for d in after["desires"]] == [d["id"] for d in before["desires"]]
    assert after["desire_window"]["total"] == before["desire_window"]["total"]
    assert after["desire_window"]["included"] == before["desire_window"]["included"]
    assert after["desire_window"]["remaining_in_storage"] == before["desire_window"]["remaining_in_storage"]
    assert after["desire_window"]["archived"] == len(moved)
    assert "archived" not in before["desire_window"]


def test_the_manifest_counts_the_wishes_the_model_was_shown_the_same_way(setup):
    mind, source, clock = setup
    allow(mind)
    finished(mind, source, clock, 14, age=40 * DAY)
    make(mind, source, "live-one")
    view = mind.read()
    shown = appraisal_context({"state": view})["state"]
    assert move(mind)["moved"]
    classes, _values = manifest._mind_state(view, shown)
    # What the model was shown against what the state holds now, as the commit path compares them.
    # The total the projection gave counts the wishes that have moved, so this side has to as well:
    # otherwise the move alone would read as the wish inventory having changed under the proposal,
    # and every appraisal in flight across it would be refused.
    name, seen, current = next(row for row in manifest._written(classes, state_of(mind))
                               if row[0] == "desires")
    assert name == "desires" and seen["#total"] == current["#total"]


# --- a reader that misses in the document looks in the archive -------------------------------

def test_an_update_of_an_archived_wish_is_a_conflict_and_not_a_miss(setup):
    mind, source, clock = setup
    allow(mind)
    made = finished(mind, source, clock, 10, age=40 * DAY)
    moved = move(mind)["moved"]
    with pytest.raises(Conflict, match="finished desire"):
        settle(mind, source, moved[0], "resume")
    with pytest.raises(Missing, match="outside this scope"):
        settle(mind, source, "desire_" + digest(["never"])[:32], "resume")
    assert made


def test_a_create_that_lands_on_an_archived_identifier_is_refused(setup):
    mind, source, clock = setup
    allow(mind)
    wish = make(mind, source, "one-of-a-kind")
    settle(mind, source, wish["desire_id"], "abandon")
    crowd_out(mind, source, wish["desire_id"])
    clock[0] += 40 * DAY
    assert wish["desire_id"] in move(mind, days=0)["moved"]
    # The identifier is derived from the command, so the same command would land on the wish that
    # moved. One identifier, one wish: a second copy in the document is refused, not written.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM commands")
    with pytest.raises(Conflict, match="finished desire"):
        make(mind, source, "one-of-a-kind")


def test_an_archived_sharing_decision_still_blocks_a_duplicate_intent(setup):
    mind, source, clock = setup
    allow(mind)
    share(mind, source, "exp-1")
    wish = make(mind, source, "share-it", exploration_id="exp-1")["desire_id"]
    settle(mind, source, wish, "complete")
    crowd_out(mind, source, wish)
    clock[0] += 40 * DAY
    assert wish in move(mind, days=0)["moved"]
    with pytest.raises(Conflict, match="already has a contact intent"):
        make(mind, source, "share-it-again", exploration_id="exp-1")
    with mind.engine.db.connect() as conn:
        assert desire_archive.intent(conn, mind.scope.key(), "exp-1", 1)
        assert not desire_archive.intent(conn, mind.scope.key(), "exp-1", 2)
        assert not desire_archive.intent(conn, mind.scope.key(), "exp-1", 1, exclude=wish)


def test_the_wish_sync_does_not_make_an_archived_wish_again(env):
    mind, plans, source, clock, initial = env
    allow(mind)
    decide(env, create(env, actor="contact", key="reach-out"))
    created = plans.sync_wishes()
    assert len(created) == 1
    settle(mind, source, created[0], "abandon")
    crowd_out(mind, source, created[0])
    with mind.engine.db.connect(write=True) as conn:
        # A finished plan still names its wish, and the sync still reads finished plans.
        conn.execute("UPDATE mind_plans SET status='completed' WHERE scope=?", (mind.scope.key(),))
    clock[0] += 40 * DAY
    assert created[0] in move(mind, days=0)["moved"]
    revision = mind.read()["revision"]
    assert plans.sync_wishes() == []
    assert mind.read()["revision"] == revision
    assert created[0] not in state_of(mind)["desires"]


def test_what_a_bootstrap_already_asked_for_is_still_asked_for(setup):
    mind, source, clock = setup
    allow(mind)
    wish = make(mind, source, "one-wish")
    content = state_of(mind)["desires"][wish["desire_id"]]["content"]
    settle(mind, source, wish["desire_id"], "complete")
    crowd_out(mind, source, wish["desire_id"])
    clock[0] += 40 * DAY
    move(mind, days=0)
    with mind.engine.db.connect() as conn:
        assert content in desire_archive.contents(conn, mind.scope.key())


# --- the way back ----------------------------------------------------------------------------

def test_the_round_trip_gives_the_document_back(setup):
    mind, source, clock = setup
    allow(mind)
    finished(mind, source, clock, 12, age=40 * DAY)
    make(mind, source, "still-live")
    before = state_of(mind)
    moved = move(mind)["moved"]
    assert moved and state_of(mind)["desires"] != before["desires"]
    result = desire_archive.restore(mind, apply=True)
    after = state_of(mind)
    assert result["restored_count"] == len(moved)
    assert dumps(after["desires"]) == dumps(before["desires"])
    assert rows(mind) == {}
    # And the count goes with the last wish, so the document is the one the previous release reads.
    assert "desire_archive" not in after and "desire_archive" not in mind.read()
    assert desire_archive.restore(mind, apply=True)["state"] == "restored"


def test_one_wish_comes_back_on_its_own(setup):
    mind, source, clock = setup
    allow(mind)
    finished(mind, source, clock, 12, age=40 * DAY)
    moved = move(mind)["moved"]
    chosen = moved[1]
    assert desire_archive.restore(mind, ids=[chosen])["would_restore"] == [chosen]
    assert rows(mind) and chosen in rows(mind)
    result = desire_archive.restore(mind, apply=True, ids=[chosen])
    assert result["restored"] == [chosen]
    assert chosen in state_of(mind)["desires"] and chosen not in rows(mind)
    assert state_of(mind)["desire_archive"] == {"count": len(moved) - 1}
    with pytest.raises(Missing):
        desire_archive.restore(mind, apply=True, ids=["desire_" + digest(["nothing"])[:32]])
    # And the wish that came back is in the document again, where every reader looks first.
    assert chosen in {d["id"] for d in mind.read()["desires"]}
    with mind.engine.db.connect() as conn:
        assert mind._desire(conn, state_of(mind), chosen)["id"] == chosen
        assert desire_archive.archived(conn, mind.scope.key(), chosen) is None


def test_a_restore_never_writes_over_a_live_wish(setup):
    mind, source, clock = setup
    allow(mind)
    made = finished(mind, source, clock, 10, age=40 * DAY)
    moved = move(mind)["moved"]
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state["desires"][moved[0]] = {"id": moved[0], "status": "wanted"}
        mind._save(conn, state)
    with pytest.raises(Conflict, match="restored over the live one"):
        desire_archive.restore(mind, apply=True)
    assert set(rows(mind)) == set(moved) and made


# --- the flag and the operator's own routes ---------------------------------------------------

def test_nothing_moves_while_the_flag_is_off(setup):
    mind, source, clock = setup
    MemoryContinuity(mind)
    finished(mind, source, clock, 12, age=40 * DAY)
    before = state_of(mind)
    # The dry run needs no flag at all: reading what would move is how the decision is taken.
    report = plan(mind)
    assert report["state"] == "dry-run" and report["enabled"] is False
    assert report["would_archive_count"] == 12 - desire_archive.WINDOW
    with pytest.raises(Conflict, match="switched off"):
        move(mind)
    assert rows(mind) == {}
    assert dumps(state_of(mind)) == dumps(before)
    assert "desire_archive" not in mind.read()
    # And with it on, the same wishes the dry run named are the ones that move.
    allow(mind)
    assert move(mind)["moved"] == report["would_archive"]


def test_the_report_says_what_stays_and_why(setup):
    mind, source, clock = setup
    allow(mind)
    made = finished(mind, source, clock, 10)
    live = make(mind, source, "current")["desire_id"]
    report = plan(mind)
    assert report["would_archive"] == [] and report["holding_count"] == 11
    assert report["holding_reasons"][desire_archive.RECENT] == 10
    assert report["holding_reasons"][desire_archive.LIVE_WISH] == 1
    assert report["holding"][live] == [desire_archive.LIVE_WISH]
    assert set(report["reasons"]) == set(report["holding_reasons"])
    assert report["desires"] == 11 and report["archived"] == 0 and made


def test_the_host_routes_both_commands_and_writes_nothing_without_apply(setup):
    mind, source, clock = setup
    allow(mind)
    # The host builds its own mind on the wall clock, so the wishes are made in the past rather
    # than the clock moved into the future: the two have to agree about what is old.
    clock[0] -= 60 * DAY
    finished(mind, source, clock, 10)
    clock[0] = datetime.now(clock[0].tzinfo)
    settings = config(mind)
    assert dispatch(settings, "desire-archive", {})["state"] == "dry-run"
    assert rows(mind) == {}
    moved = dispatch(settings, "desire-archive", {"apply": True})["moved"]
    assert moved and set(rows(mind)) == set(moved)
    assert dispatch(settings, "desire-archive", {"days": 0})["would_archive"] == []
    assert dispatch(settings, "desire-unarchive", {})["would_restore_count"] == len(moved)
    assert set(rows(mind)) == set(moved)
    assert dispatch(settings, "desire-unarchive", {"apply": True})["restored_count"] == len(moved)
    assert rows(mind) == {}


# --- helpers that need a little of the wider state --------------------------------------------

def share(mind, source, exploration_id, revision=1):
    """A current decision to communicate one result, as the appraisal writes it."""
    evidence = source("sharing-" + exploration_id)
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state.setdefault("exploration_decisions", {})[exploration_id] = {
            "exploration_id": exploration_id, "decision": "share", "reason": "Worth telling",
            "reconsider_when": None, "revision": revision, "updated_at": mind.clock(),
            "event_id": "mind_" + digest([exploration_id])[:32], "agent_version": "synthetic-v1",
            "evidence": mind._evidence(conn, [evidence]), "runtime": None}
        state["revision"] += 1
        mind._save(conn, state)
        mind._history(conn, "mind_" + digest([exploration_id, "decision"])[:32], state,
                      "exploration-decision", {"exploration_id": exploration_id})
