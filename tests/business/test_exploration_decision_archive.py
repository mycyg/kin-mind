"""Settled exploration decisions leave the state document for an archive (2026-09-28): what the
view shows, the owner's week and whatever still reads a decision hold it, by name; the move is a
revision of its own and the restore gives the document back; what moves is handed to memory with
the ids to its records; the review minute moves it only with the switch on; and every reader that
misses in the document still answers as it did — no result is decided twice, no shared result gets
a second contact intent, no appraisal is refused because a decision it was shown has moved, and an
erase reaches what moved."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from eventmem.core.db import Conflict, Missing, dumps
from eventmem.core.engine import root_id
from eventmem.core.models import SourceInput

from kin_mind import exploration_decision_archive as decisions
from kin_mind.appraisal import Appraisal, Appraisals, Wish
from kin_mind.erasure import ERASED
from kin_mind.exploration_decisions import VIEW, SharingDecision, apply_decisions
from kin_mind.memory import MemoryContinuity
from kin_mind.state import DesireChange
from test_derived_erasure import answered, paid
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind', 'test_autonomous_plans')

AGENT = "synthetic-v1"
MARKER = "plughquux"


def document(mind):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)


def rows(mind):
    with mind.engine.db.connect() as conn:
        return {row["id"]: row["data"] for row in conn.execute(
            "SELECT id,data FROM mind_exploration_decision_archive WHERE scope=?", (mind.scope.key(),))}


def history_kinds(mind):
    with mind.engine.db.connect() as conn:
        return [row[0] for row in conn.execute("SELECT kind FROM mind_events WHERE scope=? ORDER BY revision",
                                               (mind.scope.key(),))]


def switch(mind, on=True):
    MemoryContinuity(mind).configure({"exploration_decision_archive": on})


def explored(mind, clock, eid, *, state="complete", pages=1, words=None, desire_id=None):
    """An exploration as Explorations.run leaves one: its row, the pages it read, and the result
    source it wrote, which names the exploration."""
    engine, at = mind.engine, clock[0].isoformat()
    with engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,"
                     "state TEXT NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)")
    observed = [engine.receive(SourceInput(
        namespace="kin-web-observation", key=f"{eid}:page:{index}", scope=mind.scope, authority="document",
        text=f"page {index} read by {eid}", occurred_at=at,
        metadata={"host_event": "web-observation", "locator": f"https://example.com/{eid}/{index}",
                  "delivery": {"content_sha256": "0" * 64}}))["id"] for index in range(pages)]
    summary = words or f"what {eid} found"
    result = engine.receive(SourceInput(
        namespace="kin-exploration", key=eid, scope=mind.scope, authority="model", occurred_at=at,
        text=dumps({"state": state, "result": {"summary": summary}, "partial": False}),
        metadata={"host_event": "exploration-result", "exploration_id": eid, "observation_ids": observed,
                  "sources": [{"title": f"a page {eid} cited", "url": "https://example.com"}]}))["id"]
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (eid, mind.scope.key(), state, at, dumps(
            {"source_id": result, "observation_ids": observed, "exploration_target": "knowledge", "partial": False,
             "desire_id": desire_id, "result": {"summary": summary}})))
    return result, observed


def decided(mind, clock, eid, decision="keep", *, reconsider=None, reason=None, **run):
    """A decision on a fresh exploration result, written as apply_decisions writes a first one."""
    clock[0] += timedelta(minutes=1)
    result, observed = explored(mind, clock, eid, **run)

    def write(conn, state, event_id):
        state.setdefault("exploration_decisions", {})[eid] = {
            "exploration_id": eid, "decision": decision, "reason": reason or f"decided on {eid}",
            "reconsider_when": reconsider, "revision": 1, "updated_at": mind.clock(), "event_id": event_id,
            "agent_version": AGENT, "evidence": mind._evidence(conn, [result, *observed]),
            "runtime": {"provider": "synthetic", "model": "synthetic"}}
    mind._mutate({"command_id": "decide:" + eid, "agent_version": AGENT, "expected_revision": document(mind)["revision"],
                  "evidence_ids": [result]}, "test-decision", write)
    return result, observed


def older_and_newer(mind, clock, old=15, new=0):
    """`old` decisions, ten days before `new` ones: of the old ones, what the view does not show moves."""
    for index in range(old):
        decided(mind, clock, f"explore_old{index:02d}")
    clock[0] += timedelta(days=10)
    for index in range(new):
        decided(mind, clock, f"explore_new{index}")
    return [f"explore_old{index:02d}" for index in range(old)], [f"explore_new{index}" for index in range(new)]


def survey(mind, **options):
    with mind.engine.db.connect() as conn:
        return decisions.survey(conn, mind, mind._load(conn), mind.clock(), **options)


def wish(mind, clock, key, evidence, **fields):
    clock[0] += timedelta(minutes=1)
    return mind.manage_desire(DesireChange(
        command_id=key, agent_version=AGENT, expected_revision=document(mind)["revision"], evidence_ids=evidence,
        action="create", content="say what came of it " + key, topic="topic " + key, kind="contact", strength=60,
        expires_at=(clock[0] + timedelta(days=3)).isoformat(), completion="she heard it", reason="worth saying",
        **fields))["desire_id"]


def settle_wish(mind, clock, desire_id, evidence, action="abandon"):
    clock[0] += timedelta(minutes=1)
    mind.manage_desire(DesireChange(command_id="settle:" + desire_id, agent_version=AGENT,
                                    expected_revision=document(mind)["revision"], evidence_ids=evidence,
                                    action=action, desire_id=desire_id, reason="set down"))


# --- the rule --------------------------------------------------------------------------------------

def test_the_view_and_the_owners_week_hold_a_decision_and_the_rest_moves(setup):
    mind, _, clock = setup
    old, new = older_and_newer(mind, clock, old=16, new=3)
    moving, holding = survey(mind)
    # The view shows the twelve newest: the three of this week and the nine latest of the old ones.
    assert moving == old[:7]
    assert holding == {**{i: ["window"] for i in old[7:]}, **{i: ["recent", "window"] for i in new}}
    assert [d["exploration_id"] for d in mind.read()["exploration_decisions"]] == [*reversed(new), *reversed(old[7:])]
    # The owner's rule on its own: the last `days` days, and at most `keep` of them.
    _, kept = survey(mind, keep=2)
    assert [i for i in new if "recent" in kept[i]] == new[1:]
    _, none = survey(mind, days=0)
    assert not any("recent" in found for found in none.values())
    _, week = survey(mind, days=11)
    assert {i for i, found in week.items() if "recent" in found} == {*new, *old[-7:]}, "the ten newest of the week"
    assert len(decisions.REASONS) == 8 and VIEW == 12 and (decisions.DAYS, decisions.KEEP) == (7, 10)


def test_whatever_still_reads_a_decision_holds_it_by_name(setup):
    mind, _, clock = setup
    plan = {"share_bare": "share", "share_done": "share", "share_moved": "share", "defer_waiting": "defer",
            "keep_wanted": "keep", "keep_running": "keep", "keep_contacted": "keep", "keep_undated": "keep",
            "keep_plain": "keep"}
    for name, decision in plan.items():
        decided(mind, clock, "explore_" + name, decision, reconsider="when she asks" if decision == "defer" else None,
                state="running" if name == "keep_running" else "complete")
    clock[0] += timedelta(days=10)
    for index in range(VIEW):
        decided(mind, clock, f"explore_fill{index:02d}")
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)

        def linked(desire_id, eid, status):
            state["desires"][desire_id] = {"id": desire_id, "status": status, "kind": "contact",
                                           "exploration_id": "explore_" + eid, "sharing_revision": 1}
        linked("desire_done", "share_done", "completed")
        linked("desire_waiting", "defer_waiting", "waiting")
        linked("desire_wanted", "keep_wanted", "wanted")
        linked("desire_contacted", "keep_contacted", "completed")
        # The intent of `share_moved` is a wish that has itself moved to the wish archive.
        conn.execute("INSERT INTO mind_desire_archive VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (scope, "desire_moved", "completed", "contact", "explore_share_moved", 1, None, mind.clock(), 1,
                      dumps({"id": "desire_moved", "status": "completed", "exploration_id": "explore_share_moved"})))
        conn.execute("INSERT INTO mind_contacts VALUES(?,?,?,?)",
                     ("contact_open", scope, "unconfirmed", dumps({"desire_ids": ["desire_contacted"]})))
        del state["exploration_decisions"]["explore_keep_undated"]["updated_at"]
        moving, holding = decisions.survey(conn, mind, state, mind.clock())
    assert moving == ["explore_keep_plain", "explore_share_done", "explore_share_moved"]
    assert {k: v for k, v in holding.items() if "fill" not in k} == {
        "explore_share_bare": ["share-open"],
        "explore_defer_waiting": ["reconsider-open", "wish-open"],
        "explore_keep_wanted": ["wish-open"],
        "explore_keep_running": ["exploration-running"],
        "explore_keep_contacted": ["contact-open"],
        "explore_keep_undated": ["undated"],
    }


# --- the move and the restore ------------------------------------------------------------------------

def test_the_dry_run_writes_nothing_and_the_move_is_a_revision_of_its_own(setup):
    mind, _, clock = setup
    old, _ = older_and_newer(mind, clock)
    before, kinds = document(mind), history_kinds(mind)
    report = decisions.archive(mind)
    assert report["state"] == "dry-run" and report["enabled"] is False
    assert report["would_archive"] == old[:3] and report["holding_reasons"] == {"window": 12}
    assert report["document_chars_after"] < report["document_chars"] == len(dumps(before))
    assert document(mind) == before and rows(mind) == {} and history_kinds(mind) == kinds
    with pytest.raises(Conflict) as refused:
        decisions.archive(mind, apply=True)
    assert refused.value.code == "exploration-decision-archive-disabled" and rows(mind) == {}
    switch(mind)
    report = decisions.archive(mind, apply=True)
    assert report["moved"] == old[:3] and report["moved_count"] == 3 and report["archived"] == 3
    assert report["decisions"] == VIEW and report["revision"] == before["revision"] + 1
    after = document(mind)
    assert set(after["exploration_decisions"]) == set(before["exploration_decisions"]) - set(old[:3])
    assert after[decisions.STATE_KEY] == {"count": 3, "revisions": {i: 1 for i in old[:3]}}
    assert rows(mind) == {i: dumps(before["exploration_decisions"][i]) for i in old[:3]}, "whole, byte for byte"
    assert history_kinds(mind) == [*kinds, decisions.KIND]
    again = decisions.archive(mind, apply=True)
    assert again["moved_count"] == 0 and document(mind)["revision"] == after["revision"]


def test_the_restore_gives_back_the_document_it_had(setup):
    mind, _, clock = setup
    old, _ = older_and_newer(mind, clock)
    switch(mind)
    before = document(mind)
    decisions.archive(mind, apply=True)
    dry = decisions.restore(mind)
    assert dry["state"] == "dry-run" and dry["would_restore"] == old[:3] and len(rows(mind)) == 3
    done = decisions.restore(mind, apply=True)
    assert done["restored"] == old[:3] and done["archived"] == 0 and rows(mind) == {}
    after = document(mind)
    assert decisions.STATE_KEY not in after
    assert dumps(after["exploration_decisions"]) == dumps(before["exploration_decisions"])
    stable = lambda state: dumps({**state, "revision": 0, "updated_at": ""})
    assert stable(after) == stable(before), "the whole document, apart from its revision and time"
    assert history_kinds(mind)[-2:] == [decisions.KIND, decisions.RESTORE_KIND]
    assert decisions.restore(mind, apply=True)["restored_count"] == 0, "a second restore is the finished answer"
    with pytest.raises(Missing):
        decisions.restore(mind, apply=True, ids=["explore_never"])
    # One named decision alone, and never over a live copy of itself.
    decisions.archive(mind, apply=True)
    assert decisions.restore(mind, apply=True, ids=[old[1]])["restored"] == [old[1]]
    assert set(rows(mind)) == {old[0], old[2]}

    def copy(conn, state, event_id):
        state["exploration_decisions"][old[0]] = before["exploration_decisions"][old[0]]
    mind._mutate({"command_id": "copy", "agent_version": AGENT, "expected_revision": document(mind)["revision"]},
                 "test-copy", copy)
    with pytest.raises(Conflict) as refused:
        decisions.restore(mind, apply=True, ids=[old[0]])
    assert refused.value.code == "exploration-decision-archive-live"


def test_a_decision_taken_up_after_the_survey_stays(setup, monkeypatch):
    """The apply decides again inside its own transaction: a survey that is already stale when the
    write begins moves only what is still free."""
    mind, source, clock = setup
    old, _ = older_and_newer(mind, clock)
    held_since = old[3]
    real = decisions.survey
    calls = []

    def stale(conn, mind_, state, at, **options):
        calls.append(1)
        moving, holding = real(conn, mind_, state, at, **options)
        # The first survey, outside the write, did not see what holds `held_since` now.
        return (sorted([*moving, held_since]), holding) if len(calls) == 1 else (moving, holding)
    monkeypatch.setattr(decisions, "survey", stale)
    switch(mind)
    moved = decisions.archive(mind, apply=True)["moved"]
    assert moved == old[:3] and held_since in document(mind)["exploration_decisions"]


def test_revive_puts_one_decision_back_in_place(setup):
    mind, _, clock = setup
    old, _ = older_and_newer(mind, clock)
    switch(mind)
    before = document(mind)["exploration_decisions"][old[1]]
    decisions.archive(mind, apply=True)
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        held = state["exploration_decisions"]
        assert decisions.revive(mind, conn, state, old[1]) == before
        assert state["exploration_decisions"] is held and list(held) == sorted(held), "the caller's dict, sorted"
        assert state[decisions.STATE_KEY] == {"count": 2, "revisions": {old[0]: 1, old[2]: 1}}
        assert decisions.revive(mind, conn, state, old[1]) is held[old[1]], "a live one is simply returned"
        assert decisions.revive(mind, conn, state, "explore_never") is None
        assert set(r[0] for r in conn.execute("SELECT id FROM mind_exploration_decision_archive")) == {old[0], old[2]}
        conn.rollback()


# --- memory and the way back to the records -----------------------------------------------------------

def test_what_moves_is_handed_to_memory_with_the_ids_of_its_records(setup, monkeypatch):
    mind, source, clock = setup
    seed = source("wonder")
    clock[0] += timedelta(minutes=1)
    explore = mind.manage_desire(DesireChange(
        command_id="explore-wish", agent_version=AGENT, expected_revision=document(mind)["revision"], evidence_ids=[seed],
        action="create", content="find out about tea gardens", topic="tea gardens", kind="explore", strength=60,
        expires_at=(clock[0] + timedelta(days=3)).isoformat(), completion="known", reason="curious"))["desire_id"]
    result, observed = decided(mind, clock, "explore_tea", "defer", reconsider="when she mentions tea", pages=2,
                               desire_id=explore)
    older_and_newer(mind, clock, old=VIEW)
    switch(mind)
    stored = document(mind)["exploration_decisions"]["explore_tea"]
    handed = []
    report = decisions.archive(mind, apply=True, inject=lambda m, conn, kind, items: handed.append((kind, items)))
    assert report["moved"] == ["explore_tea"]
    [(kind, [item])] = handed
    assert kind == "exploration-decision" == decisions.MEMORY_KIND
    assert item == {
        "id": "explore_tea", "revision": 1, "occurred_at": stored["updated_at"],
        "summary_input": {"decision": "defer", "reason": "decided on explore_tea",
                          "reconsider_when": "when she mentions tea", "decided_at": stored["updated_at"],
                          "exploration": {"id": "explore_tea", "topic": "tea gardens", "target": "knowledge",
                                          "state": "complete", "partial": False,
                                          "summary": "what explore_tea found"}},
        "evidence_ids": [*[ref["record_id"] for ref in stored["evidence"]], root_id(seed)],
        "refs": ["explore_tea", result, *observed],
    }
    assert "metadata" not in dumps(item) and "a page explore_tea cited" not in dumps(item)
    # Wired once, at integration: the module's hook is what an apply hands to when none is passed.
    decided(mind, clock, "explore_later")
    for index in range(VIEW):
        decided(mind, clock, f"explore_filler{index:02d}")
    wired = []
    monkeypatch.setattr(decisions, "memory_hook", lambda m, conn, kind, items: wired.extend(items))
    moved = decisions.archive(mind, apply=True)["moved"]
    assert "explore_later" in moved and sorted(item["id"] for item in wired) == moved
    # A hook that fails leaves everything where it was: an item exists exactly when its decision moved.
    decided(mind, clock, "explore_last")
    for index in range(VIEW):
        decided(mind, clock, f"explore_final{index:02d}")
    clock[0] += timedelta(days=10)
    before = document(mind)

    def broken(m, conn, kind, items):
        raise RuntimeError("memory-unavailable")
    with pytest.raises(RuntimeError):
        decisions.archive(mind, apply=True, inject=broken)
    assert document(mind) == before and "explore_last" not in rows(mind)


def test_after_the_move_the_decision_and_its_result_are_read_by_id(setup):
    """The main session's way back: the whole decision by its id, and the result and the pages it
    read through the store's own reads — the ones the memory tools `read_memory` and
    `source_evidence` answer with."""
    from eventmem.core.reading import read_segment
    mind, _, clock = setup
    result, observed = decided(mind, clock, "explore_found", "keep", pages=2, words=f"the answer was {MARKER}")
    older_and_newer(mind, clock, old=VIEW)
    switch(mind)
    stored = document(mind)["exploration_decisions"]["explore_found"]
    items = []
    decisions.archive(mind, apply=True, inject=lambda m, conn, kind, handed: items.extend(handed))
    assert decisions.load_archived(mind, "explore_found") == stored
    assert decisions.load_archived(mind, "explore_old00") is None, "a decision still in the document is read there"
    [item] = items
    for identifier in item["refs"][1:]:
        assert mind.engine.source(identifier)["id"] == identifier
    report = read_segment(mind.engine, stored["evidence"][[r["source_id"] for r in stored["evidence"]].index(result)]["record_id"])
    assert MARKER in report["content"]
    for record_id in item["evidence_ids"]:
        assert read_segment(mind.engine, record_id)["id"] == record_id


# --- the switch --------------------------------------------------------------------------------------

def test_the_review_minute_moves_them_only_with_the_switch_on(setup, monkeypatch):
    mind, _, clock = setup
    surveys = []
    real = decisions.survey
    monkeypatch.setattr(decisions, "survey", lambda *a, **k: surveys.append(1) or real(*a, **k))
    for index in range(VIEW):
        decided(mind, clock, f"explore_first{index:02d}")
    assert decisions.auto(mind) == {"state": "disabled"}
    switch(mind)
    assert decisions.auto(mind) == {"state": "idle", "decisions": VIEW} and surveys == [], \
        "one count while the view could still show them all"
    # Ten days on, at the top of an hour, so the hour below is the one these calls fall in.
    clock[0] = (clock[0] + timedelta(days=10, hours=1)).replace(minute=0, second=1, microsecond=500000)
    for index in range(3):
        decided(mind, clock, f"explore_second{index}")
    moved = decisions.auto(mind)
    assert moved["state"] == "archived" and moved["moved_count"] == 3 and moved["decisions"] == VIEW
    assert set(rows(mind)) == {f"explore_first{index:02d}" for index in range(3)}
    decided(mind, clock, "explore_third")
    surveys.clear()
    assert decisions.auto(mind)["state"] == "idle" and surveys == [], "one survey an hour"
    clock[0] += timedelta(hours=1)
    assert decisions.auto(mind)["moved_count"] == 1
    # It never breaks the minute it runs in.
    clock[0] += timedelta(hours=1)
    decided(mind, clock, "explore_fourth")

    def raced(*a, **k):
        raise Conflict("Mind revision changed; read current state before updating", code="mind-revision-changed")
    monkeypatch.setattr(decisions, "archive", raced)
    assert decisions.auto(mind) == {"state": "deferred", "code": "mind-revision-changed"}
    monkeypatch.setattr(decisions, "archive", lambda *a, **k: 1 / 0)
    assert decisions.auto(mind) == {"state": "failed", "error": "ZeroDivisionError"}


def test_the_host_minute_and_the_operator_commands_reach_it(tmp_path):
    from eventmem.core import Engine
    from eventmem.core.models import Scope
    from kin_mind.host import APPLY_ACTIONS, dispatch
    from kin_mind.state import Mind
    # Seeded an hour back on a clock of its own; the host's minute runs on the real one.
    clock = [datetime.now(timezone.utc) - timedelta(hours=1)]
    engine, scope = Engine(tmp_path / "memory"), Scope(persona="synthetic-decisions")
    mind = Mind(engine, scope, clock=lambda: clock[0].isoformat())
    first = engine.receive(SourceInput(namespace="test", key="configuration", scope=scope, text="configuration",
                                       authority="explicit", occurred_at=clock[0].isoformat()))["id"]
    mind.initialize(agent_version=AGENT, evidence_ids=[first])
    for index in range(VIEW + 3):
        decided(mind, clock, f"explore_host{index:02d}")
    config = {"root": str(tmp_path / "memory"), "scope": scope.model_dump(), "agent_version": AGENT,
              "exploration_stop_file": str(tmp_path / "stop-exploration"), "exploration_directory": str(tmp_path / "jobs")}
    assert {"exploration-decision-archive", "exploration-decision-unarchive"} <= set(APPLY_ACTIONS)
    dispatch(config, "review-due", {})
    assert rows(mind) == {}, "off: the minute moves nothing"
    dry = dispatch(config, "exploration-decision-archive", {})
    assert dry["state"] == "dry-run" and dry["would_archive_count"] == 3 and rows(mind) == {}
    dispatch(config, "configure-memory", {"exploration_decision_archive": True})
    dispatch(config, "review-due", {})
    assert set(rows(mind)) == {f"explore_host{index:02d}" for index in range(3)}
    back = dispatch(config, "exploration-decision-unarchive", {"apply": True})
    assert back["restored_count"] == 3 and rows(mind) == {}
    assert dispatch(config, "exploration-decision-archive", {"apply": True, "limit": 1})["moved_count"] == 1


# --- the readers, after a move ------------------------------------------------------------------------

def moved_away(mind, clock, eid, decision="keep", **options):
    """One decision, twelve newer ones and ten days: the decision is archived."""
    result, observed = decided(mind, clock, eid, decision, **options)
    for index in range(VIEW):
        decided(mind, clock, f"explore_newer{index:02d}")
    clock[0] += timedelta(days=10)
    switch(mind)
    assert eid in decisions.archive(mind, apply=True)["moved"]
    return result, observed


def decide_again(mind, eid, decision, evidence, stimulus, key):
    event = SimpleNamespace(evidence_ids=evidence, command_id=key, agent_version=AGENT)

    def run(conn, state, event_id):
        apply_decisions(mind, conn, state, [SharingDecision(
            exploration_id=eid, decision=decision, reason="looked again",
            reconsider_when="when she asks" if decision == "defer" else None)], event, {"provider": "synthetic"}, stimulus)
    return mind._mutate({"command_id": key, "agent_version": AGENT, "expected_revision": document(mind)["revision"],
                         "evidence_ids": evidence}, "test-appraisal", run)


def test_a_result_decided_before_it_moved_is_not_decided_again(setup):
    mind, source, clock = setup
    result, _ = moved_away(mind, clock, "explore_x")
    archived = rows(mind)["explore_x"]
    # The same decision from the same evidence changes nothing, and the decision stays where it is.
    decide_again(mind, "explore_x", "keep", [result], "exploration-result", "same")
    assert "explore_x" not in document(mind)["exploration_decisions"] and rows(mind)["explore_x"] == archived
    # A clock or a delivery still cannot reopen it, and nothing reconsiders it without new evidence.
    for stimulus, refusal in (("delivery", "A clock or delivery event cannot reopen a result decision"),
                              ("exploration-result", "Reconsideration requires a new sourced thought or observation")):
        with pytest.raises(Conflict, match=refusal):
            decide_again(mind, "explore_x", "share", [result], stimulus, "reopen-" + stimulus)
    assert rows(mind)["explore_x"] == archived and "explore_x" not in document(mind)["exploration_decisions"]
    # A new thought reconsiders it: it comes back, at its next revision, in one place.
    clock[0] += timedelta(minutes=1)
    thought = source("thought", "she asked about it today")
    decide_again(mind, "explore_x", "share", [thought], "interaction", "reconsider")
    state = document(mind)
    assert state["exploration_decisions"]["explore_x"]["revision"] == 2
    assert state["exploration_decisions"]["explore_x"]["decision"] == "share"
    assert "explore_x" not in rows(mind) and decisions.STATE_KEY not in state


def test_a_result_already_shared_refuses_a_second_intent_after_it_moved(setup):
    mind, source, clock = setup
    result, _ = decided(mind, clock, "explore_shared", "share")
    intent = wish(mind, clock, "first-intent", [result], exploration_id="explore_shared")
    settle_wish(mind, clock, intent, [result])
    with pytest.raises(Conflict) as before_move:
        wish(mind, clock, "second-intent-before", [result], exploration_id="explore_shared")
    for index in range(VIEW):
        decided(mind, clock, f"explore_newer{index:02d}")
    clock[0] += timedelta(days=10)
    switch(mind)
    assert "explore_shared" in decisions.archive(mind, apply=True)["moved"]
    with pytest.raises(Conflict) as after_move:
        wish(mind, clock, "second-intent-after", [result], exploration_id="explore_shared")
    assert str(after_move.value) == str(before_move.value) == "This sharing decision already has a contact intent"
    assert [d["id"] for d in document(mind)["desires"].values() if d.get("exploration_id") == "explore_shared"] == [intent]


def test_a_wish_that_comes_to_rest_on_a_moved_decision_brings_it_back(setup):
    """No rule moves a share whose intent is not made yet; were one moved all the same, the wish that
    takes its revision brings it back into the document instead of failing on its absence."""
    mind, _, clock = setup
    result, _ = decided(mind, clock, "explore_early", "share")

    def move_by_hand(conn, state, event_id):
        decisions._store(conn, mind.scope.key(), state, "explore_early",
                         state["exploration_decisions"].pop("explore_early"), mind.clock())
        decisions._counted(conn, mind.scope.key(), state)
    mind._mutate({"command_id": "by-hand", "agent_version": AGENT, "expected_revision": document(mind)["revision"]},
                 "test-move", move_by_hand)
    assert "explore_early" in rows(mind)
    made = wish(mind, clock, "late-intent", [result], exploration_id="explore_early")
    state = document(mind)
    assert state["desires"][made]["sharing_revision"] == 1 and state["exploration_decisions"]["explore_early"]["revision"] == 1
    assert rows(mind) == {} and decisions.STATE_KEY not in state


def test_an_appraisal_naming_a_moved_shared_result_makes_no_second_wish(setup):
    mind, source, clock = setup
    result, _ = decided(mind, clock, "explore_told", "share")
    intent = wish(mind, clock, "told", [result], exploration_id="explore_told")
    settle_wish(mind, clock, intent, [result])
    for index in range(VIEW):
        decided(mind, clock, f"explore_newer{index:02d}")
    clock[0] += timedelta(days=10)
    switch(mind)
    assert "explore_told" in decisions.archive(mind, apply=True)["moved"]
    desires = set(document(mind)["desires"])

    class Proposer:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="想再说一遍", values={"curiosity": 61}, wishes=[Wish(
                content="再讲讲那个结果", topic="结果", kind="contact", strength=60, ttl_hours=24,
                completion="她听到了", exploration_id="explore_told")]))

    clock[0] += timedelta(minutes=1)
    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([source("again", "今天也在想那件事")], AGENT)
    jobs.run_one(Proposer())
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT state,data FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()
    data = json.loads(row["data"])
    assert row["state"] == "complete", data.get("error")
    assert not (data.get("rejected_sections") or []), data.get("rejected_sections")
    assert set(document(mind)["desires"]) == desires, "the intent this result had stands for it"


def test_an_appraisal_shown_a_decision_that_moved_since_is_not_refused_for_it(setup):
    from kin_mind.manifest import _written
    mind, _, clock = setup
    moved_away(mind, clock, "explore_seen")
    state = document(mind)
    shown = {"decisions": {"explore_seen": {"revision": 1}, "explore_newer00": {"revision": 1}}}
    assert _written(shown, state) == [("decisions", {"explore_seen": 1, "explore_newer00": 1},
                                       {"explore_seen": 1, "explore_newer00": 1})]
    assert decisions.revision_of(state, "explore_never") is None


def test_the_view_is_the_view_it_was(setup):
    mind, _, clock = setup
    older_and_newer(mind, clock, old=VIEW + 2, new=2)
    before = mind.read()["exploration_decisions"]
    switch(mind)
    assert decisions.archive(mind, apply=True)["moved_count"] == 4
    assert mind.read()["exploration_decisions"] == before


# --- erasure ---------------------------------------------------------------------------------------

def test_an_erase_reaches_a_decision_that_moved(setup):
    mind, source, clock = setup
    result, observed = moved_away(mind, clock, "explore_secret", "defer", reconsider=f"when {MARKER} comes up",
                                  reason=f"she once said {MARKER}")
    assert MARKER in rows(mind)["explore_secret"]
    mind.engine.delete(result)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    kept = json.loads(rows(mind)["explore_secret"])
    assert kept["reason"] == kept["reconsider_when"] == ERASED and kept["decision"] == "defer"
    assert any(ref.get("erased") for ref in kept["evidence"]), "the erased reference stays as a tombstone"
    back = decisions.restore(mind, apply=True, ids=["explore_secret"])
    assert back["restored"] == ["explore_secret"] and MARKER not in dumps(document(mind))


def test_a_moved_decision_becomes_a_memory_whose_id_reads_the_whole_decision(setup, monkeypatch):
    """Wired at integration (the owner, 2026-09-28: exploration decisions and results leave the
    document as short memories the main session can recall, the full record one read away). With no
    hook passed, what moves is queued for the archive memory in the transaction that moves it;
    DeepSeek writes it as a memory of kind exploration-decision from the summary fields alone; the
    entry reads the whole decision back, its result by id; a restore takes the item back out of what
    is remembered as archived."""
    from kin_mind import archive_memory
    from test_archive_memory import Endpoint, entries

    mind, source, clock = setup
    result, observed = decided(mind, clock, "explore_tea", "keep", pages=2)
    older_and_newer(mind, clock, old=VIEW)
    switch(mind)
    stored = document(mind)["exploration_decisions"]["explore_tea"]
    assert decisions.archive(mind, apply=True)["moved"] == ["explore_tea"]
    with mind.engine.db.connect() as conn:
        queued = conn.execute("SELECT item_id,kind,state FROM mind_archive_memory WHERE scope=?",
                              (mind.scope.key(),)).fetchall()
    assert [tuple(row) for row in queued] == [("explore_tea", decisions.MEMORY_KIND, "pending")]

    def answer(sent):
        records = json.loads(sent["messages"][0]["content"])["items"]
        assert [item["kind"] for item in records] == [decisions.MEMORY_KIND]
        assert records[0]["record"]["decision"] == "keep" and "metadata" not in dumps(records)
        assert decisions.MEMORY_INSTRUCTION in sent["system"]
        return [{"key": item["key"], "text": "我查过茶园的事，决定先自己留着。"} for item in records]
    archive_memory.run(mind, Endpoint(mind, monkeypatch, answer).provider)
    [entry] = entries(mind)
    whole = archive_memory.read(mind, entry)
    assert (whole["kind"], whole["id"], whole["outcome"]) == (decisions.MEMORY_KIND, "explore_tea", "keep")
    assert whole["record"]["reason"] == stored["reason"] and whole["record"]["revision"] == stored["revision"]
    assert all("metadata" not in ref for ref in whole["record"]["evidence"])
    assert whole["entries"][0]["refs"] == ["explore_tea", result, *observed]
    decisions.restore(mind, apply=True)
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM mind_archive_memory WHERE item_id='explore_tea'").fetchone()[0] == "restored"


def test_reconsidered_decision_memory_still_depends_on_the_copied_result(setup, monkeypatch):
    from kin_mind import archive_memory
    from test_archive_memory import Endpoint, entries

    mind, source, clock = setup
    result, _ = decided(mind, clock, "explore_reconsidered", words=MARKER)
    thought = source("new-thought")
    def reconsider(conn, state, event_id):
        decision = state["exploration_decisions"]["explore_reconsidered"]
        decision.update(revision=2, evidence=mind._evidence(conn, [thought]))
    mind._mutate({"command_id": "reconsider", "agent_version": AGENT,
                  "expected_revision": document(mind)["revision"], "evidence_ids": [thought]},
                 "test-reconsider", reconsider)
    older_and_newer(mind, clock, old=VIEW)
    switch(mind)
    decisions.archive(mind, apply=True)
    def answer(sent):
        records = json.loads(sent["messages"][0]["content"])["items"]
        return [{"key": item["key"], "text": "我记得 " + MARKER} for item in records]
    archive_memory.run(mind, Endpoint(mind, monkeypatch, answer).provider)
    assert len(entries(mind)) == 1
    mind.engine.delete(result)
    settle(mind.engine)
    assert all(row["deleted"] for row in entries(mind).values())
    assert texts_everywhere(mind.engine, MARKER) == set()
