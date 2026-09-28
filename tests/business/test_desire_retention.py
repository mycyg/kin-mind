"""The wish retention rule (the owner's, 2026-09-28): the state document keeps the wishes active in
the last seven days, at most the ten newest, and whatever an open execution still holds. Everything
else moves to the archive -- finished, or let go unfinished -- and every move queues a memory of it.

What is asserted here is the rule and what it changes: which wishes stay; that a wish an execution
holds stays until the execution ends; that a wish let go no longer blocks raising the same intent
again while a finished one still does; that the round trip gives the document back; and that the
host runs the rule by itself after a committed assessment and at no other time.

Synthetic replays only: an injected clock, sources through the engine, no model and no network."""
import json
from datetime import datetime, timedelta

import pytest

from eventmem.core.db import Conflict, digest, dumps
from kin_mind import archive_memory, desire_archive, manifest
from kin_mind.appraisal import Appraisals, appraisal_context
from kin_mind.exploration import Explorations
from kin_mind.host import dispatch
from kin_mind.memory import MemoryContinuity
from kin_mind.state import DesireChange

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

DAY = timedelta(days=1)


# --- driving one mind ------------------------------------------------------------------------

def allow(mind, on=True, **settings):
    MemoryContinuity(mind).configure({"desire_archive": on, **settings})


def make(mind, source, key, *, kind="contact", days=3, **extra):
    return mind.manage_desire(DesireChange(
        command_id=key, agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source(key)], action="create", content="Share finding " + key,
        topic="synthetic " + key, kind=kind, strength=50,
        expires_at=(datetime.fromisoformat(mind.clock()) + days * DAY).isoformat(),
        completion="The owner has it", reason="A finding worth discussing", **extra))["desire_id"]


def settle(mind, source, identifier, action="complete", key=None):
    return mind.manage_desire(DesireChange(
        command_id=key or (action + ":" + identifier), agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source(key or action + identifier)],
        action=action, desire_id=identifier, reason="Settled by the model"))


def series(mind, source, clock, count, *, step=timedelta(hours=1), finish=None, prefix="wish"):
    """`count` wishes, one every `step`, oldest first; every `finish`-th one settled."""
    made = []
    for index in range(count):
        clock[0] += step
        identifier = make(mind, source, f"{prefix}-{index}")
        if finish and index % finish == 0:
            settle(mind, source, identifier, "complete" if index % 2 else "abandon")
        made.append(identifier)
    return made


def state_of(mind):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)


def rows(mind):
    with mind.engine.db.connect() as conn:
        return {row["id"]: dict(row) for row in conn.execute(
            "SELECT * FROM mind_desire_archive WHERE scope=?", (mind.scope.key(),))}


def queued(mind):
    with mind.engine.db.connect() as conn:
        return {(row["kind"], row["item_id"]): dict(row) for row in conn.execute(
            "SELECT * FROM mind_archive_memory WHERE scope=?", (mind.scope.key(),))}


def config(mind):
    return {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
            "agent_version": "synthetic-v1", "session_id": "synthetic-session"}


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


# --- the rule ----------------------------------------------------------------------------------

def test_the_ten_newest_of_the_last_seven_days_stay_and_the_rest_move_finished_or_let_go(setup):
    mind, source, clock = setup
    allow(mind)
    old = series(mind, source, clock, 4, step=timedelta(hours=2), finish=2, prefix="old")
    clock[0] += 8 * DAY
    recent = series(mind, source, clock, 13, step=timedelta(hours=1), finish=3, prefix="new")
    report = desire_archive.archive(mind)
    newest = list(reversed(recent))[:desire_archive.KEEP]
    assert report["state"] == "dry-run" and report["days"] == 7 and report["keep"] == 10
    assert report["kept"] == newest and report["kept_count"] == 10
    assert set(report["would_archive"]) == set(old) | set(recent[:3])
    live = {i for i in report["would_archive"] if state_of(mind)["desires"][i]["status"] not in desire_archive.TERMINAL}
    assert set(report["would_let_go"]) == live and live, "unfinished wishes are let go, not held"
    assert report["would_finish_count"] + report["would_let_go_count"] == report["would_archive_count"]
    assert report["holding"] == {}

    moved = desire_archive.archive(mind, apply=True)
    current = state_of(mind)
    assert set(current["desires"]) == set(newest)
    assert set(moved["moved"]) == set(report["would_archive"]) and set(moved["let_go"]) == live
    stored = rows(mind)
    assert set(stored) == set(report["would_archive"])
    for identifier in live:
        # Let go keeps its whole record and its status as it was, which is how it is marked.
        assert stored[identifier]["status"] in desire_archive.LIVE
        assert desire_archive.outcome(stored[identifier]["status"]) == desire_archive.LET_GO
    assert current["desire_archive"] == {"count": len(stored)}
    # Every wish that moved is queued to be remembered, in the same transaction.
    assert {item for kind, item in queued(mind)} == set(stored) and moved["memory_queued"] == len(stored)


def test_activity_is_the_latest_of_made_changed_and_settled(setup):
    mind, source, clock = setup
    allow(mind)
    early = make(mind, source, "made-early")
    clock[0] += 10 * DAY
    late = make(mind, source, "made-late")
    # Settled today: its latest activity is today, whenever it was made.
    settle(mind, source, early, "complete")
    report = desire_archive.archive(mind)
    assert set(report["kept"]) == {early, late}
    clock[0] += 8 * DAY
    assert set(desire_archive.archive(mind)["would_archive"]) == {early, late}


def test_the_numbers_are_the_store_s_own_and_one_run_may_ask_for_others(setup):
    mind, source, clock = setup
    allow(mind, desire_retention_days=2, desire_retention_keep=3)
    made = series(mind, source, clock, 5)
    report = desire_archive.archive(mind)
    assert (report["days"], report["keep"]) == (2, 3) and report["kept"] == list(reversed(made))[:3]
    assert desire_archive.archive(mind, keep=5)["kept_count"] == 5
    clock[0] += 3 * DAY
    assert desire_archive.archive(mind)["kept"] == []
    for bad in ({"desire_retention_days": 0}, {"desire_retention_keep": "ten"}, {"archive_memory": 1}):
        with pytest.raises(ValueError):
            MemoryContinuity(mind).configure(bad)


@pytest.mark.parametrize("held", ["contact-open", "action-open", "exploration-running",
                                  "plan-active", "run-open", "delivery-open"])
def test_a_wish_an_open_execution_holds_stays_until_it_ends_and_takes_no_newer_wish_s_place(setup, held):
    mind, source, clock = setup
    allow(mind, desire_retention_keep=2)
    target = make(mind, source, "held-one")
    clock[0] += 10 * DAY
    newer = series(mind, source, clock, 3)
    if held == "exploration-running":
        Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        if held == "contact-open":
            conn.execute("INSERT INTO mind_contacts VALUES(?,?,?,?)",
                         ("attempt", mind.scope.key(), "unconfirmed", dumps({"desire_ids": [target]})))
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
    report = desire_archive.archive(mind)
    assert report["holding"] == {target: [held]} and held in report["reasons"]
    # The newest two keep their places; the held one is beside them, not instead of one.
    assert report["kept"] == list(reversed(newer))[:2]
    assert report["would_archive"] == [newer[0]]
    moved = desire_archive.archive(mind, apply=True)
    assert target not in moved["moved"] and target in state_of(mind)["desires"]
    # Once the execution ends, the next run lets the wish go.
    with mind.engine.db.connect(write=True) as conn:
        for table in ("mind_contacts", "mind_action_events", "mind_plan_runs", "mind_plans"):
            conn.execute(f"DELETE FROM {table}")
        if held == "exploration-running":
            conn.execute("DELETE FROM mind_explorations")
        if held == "delivery-open":
            state = mind._load(conn)
            state["desires"][target]["delivery"]["state"] = "accepted"
            mind._save(conn, state)
    assert desire_archive.archive(mind, apply=True)["moved"] == [target]


# --- let go, and what each still blocks --------------------------------------------------------

def test_a_let_go_sharing_intent_may_be_raised_again_and_a_finished_one_still_blocks(setup):
    mind, source, clock = setup
    allow(mind, desire_retention_keep=1)
    for decision in ("exp-1", "exp-2", "exp-3"):
        share(mind, source, decision)
    unsent = make(mind, source, "share-1", exploration_id="exp-1")
    done = make(mind, source, "share-2", exploration_id="exp-2")
    settle(mind, source, done, "complete")
    dropped = make(mind, source, "share-3", exploration_id="exp-3")
    settle(mind, source, dropped, "abandon")
    clock[0] += 10 * DAY
    make(mind, source, "newest")
    moved = desire_archive.archive(mind, apply=True)
    assert {unsent, done, dropped} <= set(moved["moved"]) and moved["let_go"] == [unsent]
    with mind.engine.db.connect() as conn:
        assert desire_archive.let_go(conn, mind.scope.key(), unsent)
        assert not desire_archive.intent(conn, mind.scope.key(), "exp-1", 1)
        assert desire_archive.intent(conn, mind.scope.key(), "exp-2", 1)
        assert desire_archive.intent(conn, mind.scope.key(), "exp-3", 1)
    # The unsent intent is Kin's to raise again; a finished one -- completed or abandoned -- is still
    # this decision's.
    again = make(mind, source, "share-1-again", exploration_id="exp-1")
    assert again in state_of(mind)["desires"]
    for decision in ("exp-2", "exp-3"):
        with pytest.raises(Conflict, match="already has a contact intent"):
            make(mind, source, "again-" + decision, exploration_id=decision)


def test_what_a_bootstrap_made_still_blocks_when_finished_and_not_when_let_go(setup):
    mind, source, clock = setup
    allow(mind)
    finished = make(mind, source, "finished-one")
    settle(mind, source, finished, "complete")
    unfinished = make(mind, source, "unfinished-one")
    clock[0] += 10 * DAY
    desire_archive.archive(mind, apply=True)
    with mind.engine.db.connect() as conn:
        made = desire_archive.contents(conn, mind.scope.key())
    assert "Share finding finished-one" in made and "Share finding unfinished-one" not in made
    assert unfinished in rows(mind)


def test_a_let_go_wish_is_refused_an_update_under_its_own_code_and_its_identity_stays_taken(setup):
    mind, source, clock = setup
    allow(mind)
    wish = make(mind, source, "let-me-go")
    clock[0] += 10 * DAY
    assert desire_archive.archive(mind, apply=True)["let_go"] == [wish]
    with pytest.raises(Conflict, match="let go") as refused:
        settle(mind, source, wish, "resume")
    assert refused.value.code == "desire-let-go"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM commands")
    # The same command would land on the same id: one id, one wish, wherever it lives.
    with pytest.raises(Conflict, match="finished desire"):
        make(mind, source, "let-me-go")


def test_the_plan_sync_does_not_make_a_let_go_wish_again_from_the_same_decision(env):
    from test_autonomous_plans import create, decide
    mind, plans, source, clock, initial = env
    allow(mind)
    decide(env, create(env, actor="contact", key="reach-out"))
    created = plans.sync_wishes()
    assert len(created) == 1
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plans SET status='completed' WHERE scope=?", (mind.scope.key(),))
    clock[0] += 10 * DAY
    assert desire_archive.archive(mind, apply=True)["let_go"] == created
    revision = mind.read()["revision"]
    assert plans.sync_wishes() == [] and mind.read()["revision"] == revision



def test_a_plan_that_decides_again_after_its_wish_was_let_go_raises_a_new_wish(env):
    from test_autonomous_plans import create, decide
    mind, plans, source, clock, initial = env
    allow(mind)
    plan = create(env, actor="contact", key="reach-again")
    decide(env, plan)
    [first] = plans.sync_wishes()
    with mind.engine.db.connect(write=True) as conn:
        # Paused, the plan holds nothing, and its wish is let go with the rest.
        conn.execute("UPDATE mind_plans SET status='paused' WHERE scope=?", (mind.scope.key(),))
    clock[0] += 10 * DAY
    assert desire_archive.archive(mind, apply=True)["let_go"] == [first]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plans SET status='active' WHERE scope=?", (mind.scope.key(),))
        current = plans.get(conn, plan["id"])
    decide(env, current)
    # The new decision raises a new wish; the let-go one is neither updated nor made again.
    [second] = plans.sync_wishes()
    assert second != first and second in state_of(mind)["desires"] and first in rows(mind)


# --- the way back --------------------------------------------------------------------------------

def test_the_round_trip_gives_the_document_back_let_go_wishes_and_all(setup):
    mind, source, clock = setup
    allow(mind)
    series(mind, source, clock, 6, finish=2, prefix="old")
    clock[0] += 9 * DAY
    series(mind, source, clock, 12, finish=4, prefix="new")
    before = state_of(mind)
    moved = desire_archive.archive(mind, apply=True)
    assert moved["let_go_count"] and moved["moved_count"] > moved["let_go_count"]
    result = desire_archive.restore(mind, apply=True)
    after = state_of(mind)
    assert result["restored_count"] == moved["moved_count"]
    assert dumps(after["desires"]) == dumps(before["desires"])
    assert rows(mind) == {} and "desire_archive" not in after
    # Their memories are no longer memories of an archived wish, and none is sent.
    assert {row["state"] for row in queued(mind).values()} == {archive_memory.RESTORED}
    assert archive_memory.due(mind) == 0
    # Archived again at the same revision, the queue picks each up where it was.
    again = desire_archive.archive(mind, apply=True)
    assert again["memory_queued"] == again["moved_count"]
    assert {row["state"] for row in queued(mind).values()} == {archive_memory.PENDING}


# --- the window and the count -------------------------------------------------------------------

def test_the_projection_shows_the_newest_finished_wishes_and_still_counts_every_wish(setup):
    mind, source, clock = setup
    allow(mind, desire_retention_keep=12)
    made = series(mind, source, clock, 14, finish=1)
    live = make(mind, source, "live-one")
    moved = desire_archive.archive(mind, apply=True)["moved"]
    shown = appraisal_context({"state": mind.read()})["state"]
    finished = [d["id"] for d in shown["desires"] if d["status"] in desire_archive.TERMINAL]
    assert finished == made[-desire_archive.WINDOW:]
    assert live in {d["id"] for d in shown["desires"]}
    assert shown["desire_window"]["total"] == 15 and shown["desire_window"]["archived"] == len(moved) == 3


def test_the_manifest_counts_what_moved_so_an_assessment_across_a_move_commits(setup):
    mind, source, clock = setup
    allow(mind, desire_retention_keep=3)
    series(mind, source, clock, 8, finish=1)
    view = mind.read()
    shown = appraisal_context({"state": view})["state"]
    assert desire_archive.archive(mind, apply=True)["moved"]
    classes, _values = manifest._mind_state(view, shown)
    name, seen, current = next(row for row in manifest._written(classes, state_of(mind)) if row[0] == "desires")
    assert seen["#total"] == current["#total"]


# --- it runs by itself ---------------------------------------------------------------------------

class Provider:
    """What `review` builds for its model call; the assessment itself is stubbed below."""
    background = False


def review(mind, monkeypatch, answer):
    import kin_mind.appraisal as appraisal_module
    monkeypatch.setattr(appraisal_module.DeepSeek, "from_engine", classmethod(lambda cls, engine: Provider()))
    monkeypatch.setattr(Appraisals, "run_one", lambda self, provider, lane=None, job_id=None: dict(answer))
    return dispatch(config(mind), "review", {})


def test_after_a_committed_assessment_the_rule_runs_and_at_no_other_time(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind, desire_retention_keep=2)
    clock[0] -= 30 * DAY
    old = series(mind, source, clock, 3)
    clock[0] = datetime.now(clock[0].tzinfo)
    revision = mind.read()["revision"]
    # An assessment that did not commit runs nothing.
    assert "desire_retention" not in (review(mind, monkeypatch, {"state": "pending"}) or {})
    assert mind.read()["revision"] == revision and rows(mind) == {}
    result = review(mind, monkeypatch, {"state": "complete"})
    assert result["desire_retention"]["state"] == "archived"
    assert result["desire_retention"]["moved_count"] == 3 and result["desire_retention"]["let_go_count"] == 3
    assert set(rows(mind)) == set(old) and mind.read()["revision"] == revision + 1
    # Nothing left to move: a read, and no revision.
    assert "desire_retention" not in review(mind, monkeypatch, {"state": "complete"})
    assert mind.read()["revision"] == revision + 1


def test_the_automatic_run_waits_for_an_assessment_that_was_shown_the_wishes(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    Appraisals(mind)
    clock[0] -= 30 * DAY
    make(mind, source, "old-one")
    clock[0] = datetime.now(clock[0].tzinfo)
    import time
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,lease,data) VALUES(?,?,?,?,?,?)",
                     ("appraisal-running", mind.scope.key(), "running", time.time(), time.time() + 600,
                      dumps({"evidence_ids": [], "stimulus": "idle-review"})))
    revision = mind.read()["revision"]
    assert desire_archive.retain(mind)["state"] == "deferred"
    assert mind.read()["revision"] == revision and rows(mind) == {}
    # The history lane was shown no wishes: it does not hold the rule back.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET data=? WHERE id='appraisal-running'",
                     (dumps({"evidence_ids": [], "stimulus": "memory-enrichment"}),))
    assert desire_archive.retain(mind)["state"] == "archived"


def test_with_the_flag_off_nothing_runs_by_itself_and_the_dry_run_still_answers(setup, monkeypatch):
    mind, source, clock = setup
    MemoryContinuity(mind)
    clock[0] -= 30 * DAY
    series(mind, source, clock, 12)
    clock[0] = datetime.now(clock[0].tzinfo)
    before = state_of(mind)
    assert desire_archive.retain(mind) == {"state": "disabled"}
    assert "desire_retention" not in review(mind, monkeypatch, {"state": "complete"})
    assert dumps(state_of(mind)) == dumps(before)
    assert desire_archive.archive(mind)["would_archive_count"] == 12
    with pytest.raises(Conflict, match="switched off"):
        desire_archive.archive(mind, apply=True)


def test_the_operator_commands_route_and_write_nothing_without_apply(setup):
    mind, source, clock = setup
    allow(mind)
    clock[0] -= 60 * DAY
    series(mind, source, clock, 4, finish=2)
    clock[0] = datetime.now(clock[0].tzinfo)
    settings = config(mind)
    dry = dispatch(settings, "desire-archive", {})
    assert dry["state"] == "dry-run" and dry["would_archive_count"] == 4 and rows(mind) == {}
    assert dispatch(settings, "desire-archive", {"days": 100})["would_archive"] == []
    moved = dispatch(settings, "desire-archive", {"apply": True})
    assert moved["moved_count"] == 4 and len(rows(mind)) == 4
    assert dispatch(settings, "desire-unarchive", {})["would_restore_count"] == 4
    assert dispatch(settings, "desire-unarchive", {"apply": True})["restored_count"] == 4
    assert rows(mind) == {}
