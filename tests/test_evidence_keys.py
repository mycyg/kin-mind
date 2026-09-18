"""The evidence key index: what has already been scored, read without the snapshots.

Synthetic replays only: an injected clock, sources built through the engine, no model and no
network. The guard's question never changes here — has this evidence been appraised before — only
where the answer comes from, so every test asks it both ways.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing, digest
from eventmem.core.models import Scope, SourceInput
from kin_mind import evidence_keys
from kin_mind.memory import MemoryContinuity
from kin_mind.state import AffectiveEvent, Evolution, Mind


@pytest.fixture
def setup(tmp_path):
    clock = [datetime.now(timezone.utc)]
    engine = Engine(tmp_path)
    scope = Scope(persona="synthetic")
    mind = Mind(engine, scope, clock=lambda: clock[0].isoformat())

    def source(key, text=None, authority="explicit"):
        return engine.receive(SourceInput(namespace="test", key=key, version="1", scope=scope,
            text=text or key, authority=authority, occurred_at=clock[0].isoformat(),
            metadata={"role": "user", "host_event": "message"}))["id"]

    # The cursor lives in the memory layer's own table, and every path that scores anything installs
    # that layer: an appraisal, the host and the MCP server all build it before they reach a `record`.
    MemoryContinuity(mind)
    mind.initialize(agent_version="synthetic-v1", evidence_ids=[source("configuration")])
    return mind, source, clock


def event(mind, source, key, values):
    return AffectiveEvent(command_id=key, agent_version="synthetic-v1",
                          expected_revision=mind.read()["revision"], evidence_ids=[source(key)],
                          values=values, reason="A sourced synthetic event")


def score(mind, source, key, values=None):
    return mind.record(event(mind, source, key, values or {"mood": 60}))


def key_of(mind, source_id):
    """The key the guard would compute for one source, without going through an appraisal."""
    with mind.engine.db.connect() as conn:
        refs = mind._evidence(conn, [source_id])
    return digest(sorted({(r["source_id"], r["hash"]) for r in refs}))


def rows(mind):
    with mind.engine.db.connect() as conn:
        return evidence_keys.stored(conn, mind.scope.key())


def config(mind):
    return {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
            "agent_version": "synthetic-v1", "session_id": "synthetic-session"}


def forget(mind):
    """Throw the table and its cursor away, as a rollback to the previous release would leave
    them: the rows it wrote while the index did not exist are the hole the catch-up fills."""
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_evidence_keys WHERE scope=?", (mind.scope.key(),))
        conn.execute("DELETE FROM mind_memory_migrations WHERE scope=? AND name=?",
                     (mind.scope.key(), evidence_keys.MIGRATION))


# --- the index keeps up with the rows -------------------------------------------------------------

def test_every_scored_event_is_indexed_as_it_is_written(setup):
    mind, source, _clock = setup
    score(mind, source, "first")
    score(mind, source, "second", {"focus": 40})
    stored = rows(mind)
    assert len(stored) == 2
    with mind.engine.db.connect() as conn:
        history = {(row["id"], row["revision"]) for row in conn.execute(
            "SELECT id,revision FROM mind_events WHERE scope=? AND kind='affect'", (mind.scope.key(),))}
    assert set(stored.values()) == history
    assert evidence_keys.verify(mind)["state"] == "verified"


def test_a_second_scoring_of_the_same_evidence_is_refused_by_either_side(setup):
    mind, source, _clock = setup
    shared = source("shared")
    mind.record(AffectiveEvent(command_id="first", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[shared], values={"mood": 60},
        reason="A sourced synthetic event"))
    again = AffectiveEvent(command_id="second", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[shared], values={"mood": 70},
        reason="The same underlying event under another command")
    with pytest.raises(Conflict):
        mind.record(again)
    memory = MemoryContinuity(mind)
    # The table alone, with the snapshots no longer consulted, still refuses it.
    memory.configure({"history_legacy_guard": False})
    with pytest.raises(Conflict):
        mind.record(again)
    # And the scan alone, which is the previous release exactly.
    memory.configure({"history_legacy_guard": True, "evidence_key_index": False})
    with pytest.raises(Conflict):
        mind.record(again)


def test_the_table_alone_refuses_after_the_snapshots_stop_carrying_the_key(setup):
    mind, source, _clock = setup
    shared = source("shared")
    mind.record(AffectiveEvent(command_id="first", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[shared], values={"mood": 60},
        reason="A sourced synthetic event"))
    MemoryContinuity(mind).configure({"history_legacy_guard": False})
    # What compaction will do to the snapshots, done by hand: the scan can no longer answer.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_events SET data=json_remove(data,'$.snapshot.last_evidence_key')"
                     " WHERE scope=?", (mind.scope.key(),))
        assert not conn.execute(evidence_keys.LEGACY_QUERY, (mind.scope.key(), key_of(mind, shared))).fetchone()
    with pytest.raises(Conflict):
        mind.record(AffectiveEvent(command_id="second", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[shared], values={"mood": 70},
            reason="The same underlying event under another command"))


def test_a_disagreement_between_the_two_is_recorded_as_a_metric(setup):
    mind, source, _clock = setup
    shared = source("shared")
    mind.record(AffectiveEvent(command_id="first", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[shared], values={"mood": 60},
        reason="A sourced synthetic event"))
    # A key the table lost below its own cursor, which is the one hole the catch-up cannot find:
    # it only reads forward. The scan still knows, so the evidence is still refused, and the
    # disagreement is what says the table is not yet to be trusted on its own.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_evidence_keys WHERE scope=?", (mind.scope.key(),))
        conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,'{}')",
                     (mind.scope.key(), evidence_keys.MIGRATION, evidence_keys.head(conn, mind.scope.key())))
    with pytest.raises(Conflict):
        mind.record(AffectiveEvent(command_id="second", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[shared], values={"mood": 70},
            reason="The same underlying event under another command"))
    with mind.engine.db.connect() as conn:
        recorded = conn.execute("SELECT data FROM metrics WHERE name=?",
                                (evidence_keys.MISMATCH_METRIC,)).fetchall()
    assert len(recorded) == 1
    assert json.loads(recorded[0][0]) == {"evidence_key": key_of(mind, shared), "table": False, "legacy": True}


def test_the_catch_up_fills_what_a_rollback_period_left_behind(setup):
    mind, source, _clock = setup
    scored = [source("first"), source("second")]
    for index, identifier in enumerate(scored):
        mind.record(AffectiveEvent(command_id="round-" + str(index), agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[identifier], values={"mood": 50 + index},
            reason="A sourced synthetic event"))
    forget(mind)
    assert rows(mind) == {}
    # Nothing has been backfilled, so the next appraisal's own catch-up has to find them.
    score(mind, source, "after-the-gap")
    assert set(rows(mind)) == {key_of(mind, identifier) for identifier in scored} | {key_of(mind, source("after-the-gap"))}
    assert evidence_keys.verify(mind)["state"] == "verified"
    with pytest.raises(Conflict):
        mind.record(AffectiveEvent(command_id="replay", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[scored[0]], values={"mood": 80},
            reason="The same underlying event under another command"))


# --- the backfill ---------------------------------------------------------------------------------

def test_the_backfill_is_a_dry_run_by_default_and_writes_nothing(setup):
    mind, source, _clock = setup
    score(mind, source, "first")
    score(mind, source, "second", {"focus": 40})
    forget(mind)
    plan = evidence_keys.backfill(mind)
    assert plan["state"] == "dry-run" and plan["would_insert"] == 2 and plan["cursor"] == 0
    assert rows(mind) == {}
    with mind.engine.db.connect() as conn:
        assert evidence_keys.cursor(conn, mind.scope.key()) == 0


def test_the_backfill_walks_the_rows_and_verifies_both_ways(setup):
    mind, source, _clock = setup
    scored = []
    for index in range(3):
        scored.append(source("round-" + str(index)))
        mind.record(AffectiveEvent(command_id="round-" + str(index), agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[scored[-1]], values={"mood": 50 + index},
            reason="A sourced synthetic event"))
    forget(mind)
    applied = evidence_keys.backfill(mind, apply=True)
    assert (applied["state"], applied["inserted"], applied["ignored"], applied["keys"]) == ("complete", 3, 0, 3)
    # The run proves itself: what it says it did is what the verification found afterwards.
    assert applied["verification"] == evidence_keys.verify(mind)
    checked = evidence_keys.verify(mind)
    assert checked["state"] == "verified" and checked["rows"] == 3 and checked["keys"] == 3
    assert checked["matched"] == 3 and checked["caught_up"] is True
    assert (checked["missing"], checked["extra"], checked["mismatched"]) == ([], [], [])
    # Idempotent: a second apply reads nothing new and inserts nothing.
    again = evidence_keys.backfill(mind, apply=True)
    assert (again["scanned"], again["inserted"], again["keys"]) == (0, 0, 3)
    # What the fill was for: the table alone refuses a second scoring of what it filled in.
    MemoryContinuity(mind).configure({"history_legacy_guard": False})
    with pytest.raises(Conflict):
        mind.record(AffectiveEvent(command_id="replay", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[scored[0]], values={"mood": 80},
            reason="The same underlying event under another command"))


def test_the_backfill_resumes_from_its_cursor_one_batch_at_a_time(setup):
    mind, source, _clock = setup
    for index in range(4):
        score(mind, source, "round-" + str(index), {"mood": 50 + index})
    forget(mind)
    stop_after = []

    def one_batch(conn, scope, after, *, limit=None):
        """Stand in for an interrupted run: the first batch is written, the rest is not."""
        if stop_after:
            raise KeyboardInterrupt("interrupted between batches")
        stop_after.append(after)
        return original(conn, scope, after, limit=limit)

    original = evidence_keys.scan
    evidence_keys.scan = one_batch
    try:
        with pytest.raises(KeyboardInterrupt):
            evidence_keys.backfill(mind, apply=True, batch=2)
    finally:
        evidence_keys.scan = original
    partial = rows(mind)
    assert len(partial) == 2
    with mind.engine.db.connect() as conn:
        assert evidence_keys.cursor(conn, mind.scope.key()) > 0
    resumed = evidence_keys.backfill(mind, apply=True, batch=2)
    # It reads only what it had not read, and the two halves together are the whole.
    assert resumed["scanned"] == 2 and resumed["inserted"] == 2 and resumed["keys"] == 4
    assert set(partial) < set(rows(mind))
    assert evidence_keys.verify(mind)["state"] == "verified"


def test_a_carried_over_key_is_credited_to_the_row_that_introduced_it(setup):
    mind, source, clock = setup
    scored = source("scored")
    mind.record(AffectiveEvent(command_id="scored", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[scored], values={"mood": 60},
        reason="A sourced synthetic event"))
    introduced = rows(mind)[key_of(mind, scored)]
    clock[0] += timedelta(hours=1)
    # An affect row that never went through the scoring path, which is what the appraisal lane
    # writes for an evolution: it carries the key of the row before it and scored nothing.
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state["revision"] += 1
        state["updated_at"] = mind.clock()
        mind._save(conn, state)
        mind._history(conn, "mind_carried_over", state, "affect", {"command_id": "carried"})
    # A row with no key at all, which is what an evolution before any scoring leaves.
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state.pop("last_evidence_key")
        state["revision"] += 1
        mind._save(conn, state)
        mind._history(conn, "mind_without_key", state, "affect", {"command_id": "no-key"})
    assert rows(mind)[key_of(mind, scored)] == introduced
    forget(mind)
    applied = evidence_keys.backfill(mind, apply=True)
    assert (applied["scanned"], applied["inserted"], applied["ignored"], applied["without_key"]) == (3, 1, 1, 1)
    assert rows(mind)[key_of(mind, scored)] == introduced
    checked = evidence_keys.verify(mind)
    assert checked["state"] == "verified" and checked["rows"] == 3 and checked["keys"] == 1
    assert checked["carried_over"] == 1 and checked["without_key"] == 1


def test_an_evolution_through_the_scoring_path_indexes_nothing_of_its_own(setup):
    mind, source, _clock = setup
    score(mind, source, "first")
    before = rows(mind)
    with pytest.raises(Missing):
        # A reversion naming no such event is refused, but it takes the evolution branch of the
        # guard's own method: the point is that this path never reaches an evidence key at all.
        mind.record(AffectiveEvent(command_id="reversion", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[source("reversion")],
            evolution=Evolution(revert_event_id="mind_missing"), reason="Take the last change back"))
    assert rows(mind) == before


# --- the verification -----------------------------------------------------------------------------

def test_the_verification_names_what_does_not_line_up(setup):
    mind, source, _clock = setup
    score(mind, source, "first")
    score(mind, source, "second", {"focus": 40})
    present = sorted(rows(mind))
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_evidence_keys WHERE scope=? AND evidence_key=?",
                     (mind.scope.key(), present[0]))
        conn.execute("UPDATE mind_evidence_keys SET revision=999 WHERE scope=? AND evidence_key=?",
                     (mind.scope.key(), present[1]))
        conn.execute("INSERT INTO mind_evidence_keys VALUES(?,?,?,?)",
                     (mind.scope.key(), "key_from_nowhere", "mind_nowhere", 1))
    checked = evidence_keys.verify(mind)
    assert checked["state"] == "incomplete"
    assert checked["missing"] == [present[0]] and checked["mismatched"] == [present[1]]
    assert checked["extra"] == ["key_from_nowhere"] and checked["matched"] == 0


# --- the host's own routes -------------------------------------------------------------------------

def test_the_host_backfills_and_verifies_without_the_model(setup):
    from kin_mind.host import dispatch

    mind, source, _clock = setup
    score(mind, source, "first")
    score(mind, source, "second", {"focus": 40})
    forget(mind)
    plan = dispatch(config(mind), "evidence-keys-backfill", {})
    assert plan["state"] == "dry-run" and plan["would_insert"] == 2
    assert dispatch(config(mind), "evidence-keys-verify", {})["missing_count"] == 2
    applied = dispatch(config(mind), "evidence-keys-backfill", {"apply": True})
    assert applied["state"] == "complete" and applied["inserted"] == 2
    checked = dispatch(config(mind), "evidence-keys-verify", {})
    assert checked["state"] == "verified" and checked["matched"] == 2
