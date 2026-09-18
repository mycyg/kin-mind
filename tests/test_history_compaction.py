"""Stage 5 WP4: rewriting history that already exists, which is the only place in this programme
where data the system already holds is touched at all.

Everything here is asked twice. Once of the store — does every revision still rebuild to exactly the
document it held — and once of the archive, because the store's own answer after a compaction is one
pass agreeing with itself: the hash a rewritten row is checked against was written by the pass that
rewrote it. The document each revision really held is captured from `mind_state` as each command
commits, before anything is compacted, and that is what every comparison in this file is against.

The refusals get as much room as the successes. A package that can destroy the history has to be
more willing to stop than to proceed, so there are tests for a host that is still running, a backup
that is of some other store, an archive that cannot be written, a disk with no room, and a row that
changed underneath the rewrite — and each of them asserts not only the refusal but that the store is
byte for byte what it was before the attempt.
"""

import json
import sqlite3
from datetime import timedelta

import pytest
from test_history_layer import evolve, revert
from test_history_patches import drive, live, patches, rows
from test_liveness import probe, quiet_host

from eventmem.core.db import Conflict, dumps
from kin_mind import history, history_compaction, liveness
from kin_mind.history_admin import dispatch
from kin_mind.memory import MemoryContinuity
from kin_mind.state import AffectiveEvent

pytest_plugins = ("test_kin_mind",)


@pytest.fixture
def quiet(tmp_path, monkeypatch):
    """A host that can be shown to have stopped: pid files naming processes that are gone, a status
    file whose heartbeat is ten minutes old, and a process table this test wrote itself."""
    monkeypatch.setattr(liveness, "probe_process", probe({}))
    directory = tmp_path / "host"
    directory.mkdir(exist_ok=True)
    return quiet_host(directory)


@pytest.fixture
def host(tmp_path):
    (tmp_path / "host").mkdir(exist_ok=True)
    return tmp_path / "host"


def run(mind, action, request=None, config=None):
    return dispatch(mind, action, request or {}, config)


def prepared(mind, source, clock, *, rounds=6, flag=False):
    """A history, and the exact text of every revision in it, captured as each one committed."""
    MemoryContinuity(mind)
    if flag:
        patches(mind)
    return drive(mind, source, clock, rounds=rounds)


def stored_rows(mind):
    with mind.engine.db.connect() as conn:
        return {row["revision"]: dict(row) for row in conn.execute(
            "SELECT id,scope,revision,kind,occurred_at,data FROM mind_events WHERE scope=?"
            " ORDER BY revision", (mind.scope.key(),)).fetchall()}


def rebuilds(mind, texts):
    with mind.engine.db.connect() as conn:
        for revision, text in texts.items():
            assert history.canonical(
                history.materialize(conn, mind.scope.key(), revision)) == text, revision


def archive_of(mind, name):
    path = history_compaction.archive_dir(mind.engine.db) / name
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {row["revision"]: dict(row) for row in conn.execute(
            "SELECT * FROM mind_events_v1 WHERE scope=? ORDER BY revision", (mind.scope.key(),))}
    finally:
        conn.close()


def marker(mind):
    with mind.engine.db.connect() as conn:
        return history.compacting(conn)


# --- what a compaction does, and what it leaves exactly as it was ---------------------------------


def test_every_revision_rebuilds_byte_identical_after_compaction(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=8)
    before = stored_rows(mind)
    done = run(mind, "history-compact", {"apply": True}, quiet)
    assert done["state"] == "complete" and done["cursor"] == max(texts)
    assert done["rewritten"] == len(texts) and done["patched"] > done["checkpoints"]
    # Every revision, against the document the mind really held when that revision committed.
    rebuilds(mind, texts)
    # And it really did shrink: patches instead of documents, by a wide margin.
    assert done["saved_bytes"] > 0
    assert done["bytes_after"] < done["bytes_before"] / 2


def test_every_row_and_every_column_survives(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=6)
    before = stored_rows(mind)
    run(mind, "history-compact", {"apply": True}, quiet)
    after = stored_rows(mind)
    assert set(after) == set(before) == set(texts)
    for revision, row in after.items():
        for column in ("id", "scope", "revision", "kind", "occurred_at"):
            assert row[column] == before[revision][column], (revision, column)
        # The request a revision was made by is part of the row, not part of the snapshot, and it
        # is carried across unchanged.
        assert json.loads(row["data"])["request"] == json.loads(before[revision]["data"])["request"]


def test_the_view_a_caller_reads_is_identical_before_and_after(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=8)
    before = mind.read(history=100)["history"]
    run(mind, "history-compact", {"apply": True}, quiet)
    after = mind.read(history=100)["history"]
    assert dumps(after) == dumps(before)
    assert {key for entry in after for key in entry} == {
        "id", "kind", "revision", "occurred_at", "request", "snapshot"}


def test_the_evidence_guard_still_refuses_a_second_scoring_afterwards(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=4)
    from kin_mind.evidence_keys import verify as verify_keys
    run(mind, "history-compact", {"apply": True}, quiet)
    # The table and the old snapshot scan still agree, which they can only do because every patch
    # row keeps the two keys the scan reads.
    assert verify_keys(mind)["state"] == "verified"
    with mind.engine.db.connect() as conn:
        key = mind._load(conn)["last_evidence_key"]
        assert conn.execute(
            "SELECT 1 FROM mind_events WHERE scope=? AND kind='affect' "
            "AND json_extract(data,'$.snapshot.last_evidence_key')=? LIMIT 1",
            (mind.scope.key(), key)).fetchone()


def test_a_reversion_still_restores_the_row_before_a_compacted_evolution(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    evolution = evolve(mind, source, clock)
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == 77
    run(mind, "history-compact", {"apply": True}, quiet)
    # The reversion reads the row before the evolution, which compaction kept whole for exactly
    # this: the one rebuild that must not stand on anyone else's chain.
    revert(mind, source, clock, evolution["event_id"])
    current = mind.read()
    assert current["dimensions"]["curiosity"]["baseline"] == 75 and not current["traits"]


# --- which rows keep their whole document ----------------------------------------------------------


def test_the_rows_that_keep_a_whole_document_are_the_ones_the_ruling_names(setup, quiet, monkeypatch):
    mind, source, clock = setup
    monkeypatch.setattr(history_compaction, "CHECKPOINT_EVERY", 5)
    prepared(mind, source, clock, rounds=3)
    evolution = evolve(mind, source, clock)
    revert(mind, source, clock, evolution["event_id"])
    with mind.engine.db.connect() as conn:
        kinds = {row["revision"]: row["kind"] for row in conn.execute(
            "SELECT revision,kind FROM mind_events WHERE scope=? ORDER BY revision",
            (mind.scope.key(),))}
    run(mind, "history-compact", {"apply": True}, quiet)
    stored = rows(mind)
    whole = {revision for revision, data in stored.items() if not history.is_patch(data)}
    assert 1 in whole
    for revision, kind in kinds.items():
        if kind in history.CHECKPOINT_KINDS:
            assert revision in whole, revision
            # And the row before it, which is what a reversion reads.
            assert revision - 1 in whole or revision - 1 not in kinds
    for index, revision in enumerate(sorted(stored), start=1):
        if index % 5 == 0:
            assert revision in whole, revision
    # No chain ever grows past what the live writer's own bound allows.
    assert max(history.chain_depth(data) for data in stored.values()) < history.CHECKPOINT_DEPTH


def test_a_row_a_patch_already_stands_on_keeps_its_document(setup, quiet):
    """The brief's "last old-format row before the first new-format one", and why.

    A patch the live writer wrote recorded the depth of the chain under it as it was then — nothing,
    because it stands on a whole document. Rewrite that document into a patch and the row above goes
    on claiming a depth of one over a chain of however many, for as long as the store lives."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    boundary = max(texts)
    patches(mind)
    texts |= drive(mind, source, clock, rounds=3, start=9)
    before = rows(mind)
    first_patch = min(revision for revision, data in before.items() if history.is_patch(data))
    assert before[first_patch]["base"] == boundary
    run(mind, "history-compact", {"apply": True}, quiet)
    after = rows(mind)
    assert not history.is_patch(after[boundary])
    # Every row the live writer wrote is left exactly as it was, byte for byte.
    for revision in [r for r in texts if r > boundary]:
        assert dumps(after[revision]) == dumps(before[revision])
    # And the depth every one of them records is still the depth of the chain under it.
    for revision, data in after.items():
        if history.is_patch(data):
            assert history.chain_depth(data) == _walk(after, revision)
    rebuilds(mind, texts)


def _walk(stored, revision):
    depth = 0
    while history.is_patch(stored[revision]):
        depth, revision = depth + 1, stored[revision]["base"]
    return depth


def test_a_patch_that_would_not_be_smaller_is_not_written(setup, quiet, monkeypatch):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    # Every diff comes out larger than the document, so nothing is worth storing as a patch and
    # every row keeps what it has.
    monkeypatch.setattr(history_compaction.history, "diff",
                        lambda base, target, **kw: [["set", ["filler"], "x" * 400_000]])
    done = run(mind, "history-compact", {"apply": True}, quiet)
    assert done["patched"] == 0 and done["checkpoints"] == done["rewritten"]
    assert done["whole_reasons"].get("patch-is-not-smaller")


# --- the archive -------------------------------------------------------------------------------------


def test_the_archive_holds_every_original_verbatim(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=5)
    before = stored_rows(mind)
    done = run(mind, "history-compact", {"apply": True}, quiet)
    held = archive_of(mind, done["archive"])
    assert set(held) == set(before) and done["archived"] == len(before)
    for revision, row in held.items():
        for column in ("id", "scope", "revision", "kind", "occurred_at", "data"):
            assert row[column] == before[revision][column], (revision, column)
    # The file is the operator's to keep and nobody else's to read.
    path = history_compaction.archive_dir(mind.engine.db) / done["archive"]
    assert path.stat().st_mode & 0o777 == 0o600
    # And what the store can now rebuild is what the archive says was there.
    checked = run(mind, "history-compact-verify")
    assert checked["state"] == "verified"
    assert checked["rebuilt"] == len(texts) and checked["differs_count"] == 0


def test_the_deep_check_reaches_the_same_answer_without_carrying_anything(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    run(mind, "history-compact", {"apply": True}, quiet)
    deep = run(mind, "history-compact-verify", {"deep": True})
    assert deep["state"] == "verified" and deep["deep"] is True
    assert deep["rebuilt"] == len(texts)


def test_a_row_that_no_longer_rebuilds_to_its_archived_original_is_found(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    run(mind, "history-compact", {"apply": True}, quiet)
    broken = sorted(texts)[3]
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                       (mind.scope.key(), broken)).fetchone()[0])
        if history.is_patch(data):
            data["patch"] = data["patch"] + [["set", ["agent_version"], "tampered"]]
        else:
            data["snapshot"]["agent_version"] = "tampered"
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (dumps(data), mind.scope.key(), broken))
    checked = run(mind, "history-compact-verify")
    assert checked["state"] == "incomplete" and broken in checked["differs"]


def test_an_archive_whose_own_bytes_moved_is_not_taken_as_evidence(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=4)
    done = run(mind, "history-compact", {"apply": True}, quiet)
    path = history_compaction.archive_dir(mind.engine.db) / done["archive"]
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE mind_events_v1 SET data='{\"request\":null,\"snapshot\":{}}'"
                     " WHERE revision=2")
        conn.commit()
    finally:
        conn.close()
    checked = run(mind, "history-compact-verify")
    assert checked["state"] == "incomplete" and checked["archive_corrupt"] == 1


# --- the preconditions -------------------------------------------------------------------------------


def unchanged(mind, before):
    assert stored_rows(mind) == before


def test_a_host_that_is_still_running_stops_it(setup, host, monkeypatch):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    before = stored_rows(mind)
    config = quiet_host(host)
    monkeypatch.setattr(liveness, "probe_process",
                        probe({4242: ("Fri Sep 18 10:37:19 2026", "/usr/bin/node service.mjs")}))
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True}, config)
    assert refusal.value.code == history_compaction.REFUSED
    assert refusal.value.target == "store-is-not-quiet"
    assert "process-running" in refusal.value.actual["blocking"]
    unchanged(mind, before)
    assert not marker(mind)
    assert not history_compaction.archive_dir(mind.engine.db).exists()


def test_a_fresh_worker_lease_stops_it(setup, quiet, monkeypatch):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    before = stored_rows(mind)
    import time

    from kin_mind.appraisal import Appraisals
    job = Appraisals(mind).enqueue([source("held")], "synthetic-v1")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=? WHERE id=?",
                     (time.time() + 600, job["id"]))
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True}, quiet)
    assert refusal.value.target == "store-is-not-quiet"
    assert "leases-fresh" in refusal.value.actual["blocking"]
    unchanged(mind, before)
    assert not marker(mind)


def test_a_backup_of_some_other_store_stops_it(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    before = stored_rows(mind)
    # A real database, taken from this store and then left behind while the history moved on.
    directory = history_compaction.archive_dir(mind.engine.db)
    directory.mkdir(parents=True, exist_ok=True)
    stale = directory / "stale.sqlite3"
    history_compaction._take_backup(mind.engine.db, stale)
    clock[0] += timedelta(minutes=11)
    drive(mind, source, clock, rounds=1, start=20)
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True, "backup": str(stale)}, quiet)
    assert refusal.value.target == "backup-is-not-of-this-store"
    assert not marker(mind)
    # And a file that is not a database at all is refused rather than trusted.
    (directory / "nonsense.sqlite3").write_text("not a database")
    with pytest.raises(Conflict) as nonsense:
        run(mind, "history-compact", {"apply": True, "backup": str(directory / "nonsense.sqlite3")},
            quiet)
    assert nonsense.value.target == "backup-is-not-a-store"


def test_a_disk_with_no_room_stops_it(setup, quiet, monkeypatch):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    before = stored_rows(mind)
    monkeypatch.setattr(history_compaction.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 1024, "total": 0, "used": 0})())
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True}, quiet)
    assert refusal.value.target == "not-enough-free-space"
    assert refusal.value.actual["needed"] > refusal.value.actual["free"]
    unchanged(mind, before)
    assert not marker(mind)


def test_a_backup_that_cannot_be_written_stops_it_before_anything_begins(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    before = stored_rows(mind)
    # The place the archive and the backup would go is taken by something that is not a directory.
    history_compaction.archive_dir(mind.engine.db).write_text("in the way")
    with pytest.raises(OSError):
        run(mind, "history-compact", {"apply": True}, quiet)
    unchanged(mind, before)
    # Nothing had started, so the store was never closed.
    assert not marker(mind)


def test_an_archive_that_cannot_be_written_stops_it_with_the_store_still_closed(setup, quiet, tmp_path):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    before = stored_rows(mind)
    # A backup the operator supplies, somewhere writable, and an archive directory that is not.
    kept = tmp_path / "elsewhere" / "backup.sqlite3"
    kept.parent.mkdir(parents=True, exist_ok=True)
    history_compaction._take_backup(mind.engine.db, kept)
    directory = history_compaction.archive_dir(mind.engine.db)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o500)
    try:
        with pytest.raises(sqlite3.OperationalError):
            run(mind, "history-compact", {"apply": True, "backup": str(kept)}, quiet)
    finally:
        directory.chmod(0o700)
    unchanged(mind, before)
    # The marker stays set: past this point the store is not open again until someone has looked.
    assert marker(mind)


def test_a_store_with_nowhere_to_keep_a_cursor_stops_it(setup, quiet):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=2)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DROP TABLE IF EXISTS mind_memory_migrations")
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True}, quiet)
    assert refusal.value.target == "no-migration-table"


def test_the_writer_is_shut_out_while_a_run_is_unfinished(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    stopped = run(mind, "history-compact", {"apply": True, "limit": 3}, quiet)
    # A limit bounds the rows an invocation touches, not only the batches it runs.
    assert stopped["rewritten"] == 3 and stopped["cursor"] == 3
    assert stopped["state"] == "stopped" and stopped["stopped_because"] == "limit"
    assert stopped["compaction_active"] is True and marker(mind)
    clock[0] += timedelta(minutes=9)
    with pytest.raises(Conflict) as refused:
        mind.record(AffectiveEvent(
            command_id="during-compaction", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[source("observed-later")],
            values={"mood": 44}, reason="A sourced synthetic observation"))
    assert refused.value.code == history.COMPACTING
    assert set(rows(mind)) == set(texts)
    # Finishing it opens the store again.
    done = run(mind, "history-compact", {"apply": True}, quiet)
    assert done["state"] == "complete" and not marker(mind)
    clock[0] += timedelta(minutes=9)
    assert mind.record(AffectiveEvent(
        command_id="after-compaction", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("observed-after")],
        values={"mood": 44}, reason="A sourced synthetic observation"))["revision"] == max(texts) + 1


# --- stopping and starting again -----------------------------------------------------------------------


def test_a_run_interrupted_mid_history_carries_on_from_where_it_stopped(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=8)
    first = run(mind, "history-compact", {"apply": True, "batch": 5, "limit": 5}, quiet)
    assert first["state"] == "stopped" and 0 < first["cursor"] < max(texts)
    partway = stored_rows(mind)
    second = run(mind, "history-compact", {"apply": True, "batch": 5, "limit": 5}, quiet)
    assert second["cursor"] > first["cursor"]
    while run(mind, "history-compact", {"apply": True, "batch": 5, "limit": 5}, quiet)["cursor"] < max(texts):
        pass
    assert not marker(mind)
    rebuilds(mind, texts)
    assert run(mind, "history-compact-verify")["state"] == "verified"
    # The rows the first run finished were not touched again by any later one.
    done = stored_rows(mind)
    for revision in range(1, first["cursor"] + 1):
        assert done[revision] == partway[revision]


def test_a_batch_archived_but_not_yet_rewritten_is_picked_up_unharmed(setup, quiet, monkeypatch):
    """The one window an interruption can land in: the archive is committed and the rewrite is not.

    A resumed run re-archives those rows, and the archive keeps the copy it already has, which is
    the original. Replacing it with whatever is in the store at the time would be the one way this
    could lose a row."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=5)
    before = stored_rows(mind)
    real = history_compaction._archive_batch

    def archive_then_fall_over(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt("interrupted between the archive and the rewrite")

    monkeypatch.setattr(history_compaction, "_archive_batch", archive_then_fall_over)
    with pytest.raises(KeyboardInterrupt):
        run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    # Nothing was rewritten, and the marker is still set.
    unchanged(mind, before)
    assert marker(mind)
    monkeypatch.setattr(history_compaction, "_archive_batch", real)
    done = run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    assert done["state"] == "complete"
    held = archive_of(mind, done["archive"])
    for revision, row in held.items():
        assert row["data"] == before[revision]["data"], revision
    rebuilds(mind, texts)
    assert run(mind, "history-compact-verify")["state"] == "verified"


def test_running_it_again_on_a_compacted_history_does_nothing(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=5)
    first = run(mind, "history-compact", {"apply": True}, quiet)
    after = stored_rows(mind)
    again = run(mind, "history-compact", {"apply": True}, quiet)
    assert again["state"] == "complete" and again["rewritten"] == 0 and again["archived"] == 0
    assert stored_rows(mind) == after
    assert again["cursor"] == first["cursor"]
    rebuilds(mind, texts)


def test_new_revisions_written_after_a_run_are_compacted_by_the_next_one(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    run(mind, "history-compact", {"apply": True}, quiet)
    texts |= drive(mind, source, clock, rounds=2, start=9)
    done = run(mind, "history-compact", {"apply": True}, quiet)
    assert done["rewritten"] > 0 and done["cursor"] == max(texts)
    rebuilds(mind, texts)
    assert run(mind, "history-compact-verify")["state"] == "verified"


def test_a_row_that_moved_under_the_rewrite_stops_the_batch(setup, quiet, monkeypatch):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=4)
    real = history_compaction._archive_batch

    def change_a_row_behind_its_back(path, scope, rows_, **kwargs):
        written = real(path, scope, rows_, **kwargs)
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                         ('{"request":null,"snapshot":{"revision":3}}', scope, rows_[-1]["revision"]))
        return written

    monkeypatch.setattr(history_compaction, "_archive_batch", change_a_row_behind_its_back)
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    assert refusal.value.target == "row-changed-under-the-rewrite"
    # The whole batch rolled back, including the rows that were fine.
    with mind.engine.db.connect() as conn:
        assert history_compaction.progress(conn, mind.scope.key())["through"] == 0


def test_a_rebuild_that_does_not_come_back_out_right_takes_the_batch_with_it(setup, quiet, monkeypatch):
    """The last line of defence: what was written is read back inside the same transaction and
    compared with the text the row held before it was touched. A disagreement rolls the batch
    back, so a wrong patch is never a committed patch."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    before = stored_rows(mind)
    real = history.patch_row

    def a_patch_that_loses_a_key(payload, state, **kw):
        row = real(payload, state, **kw)
        row["patch"] = [op for op in row["patch"] if op[1][:1] != ["updated_at"]] or row["patch"]
        return row

    monkeypatch.setattr(history_compaction.history, "patch_row", a_patch_that_loses_a_key)
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    assert refusal.value.target in {"patch-does-not-rebuild-the-row",
                                    "rebuilt-row-differs-from-the-original"}
    unchanged(mind, before)


# --- the two phases, in order ------------------------------------------------------------------
#
# The commitment this module makes is stronger than per-batch archiving: the whole target set of a
# run is archived and read-back-verified before the first row is rewritten. These tests instrument
# the seam between the phases — the archive of the last row, the gap between the phases, a copy
# that is not of this store, a row that escapes the archive — and each asserts the same thing from
# a different side: no rewrite without a verified archive copy of what the row held.


def archive_name(mind):
    """The file the next run of this store will archive into, named for today as the run names it."""
    return f"{history_compaction.ARCHIVE_STEM}-{history_compaction._day(mind.clock())}.sqlite3"


def test_not_one_row_is_rewritten_before_the_whole_set_is_archived(setup, quiet, monkeypatch):
    """The phase order, proven at its worst point: the archive of the LAST target row fails.

    Every chunk before it is already committed and verified in the archive, and still nothing may
    have been rewritten — the rewrite loop has not run yet, by construction."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=6)
    before = stored_rows(mind)
    last = max(texts)
    real = history_compaction._archive_batch

    def fail_on_the_last_row(path, scope, rows_, **kwargs):
        if rows_[-1]["revision"] == last:
            raise KeyboardInterrupt("the archive of the last target row never happened")
        return real(path, scope, rows_, **kwargs)

    monkeypatch.setattr(history_compaction, "_archive_batch", fail_on_the_last_row)
    with pytest.raises(KeyboardInterrupt):
        run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    # Zero rewrites, byte for byte, even though most of the set is already safely archived.
    unchanged(mind, before)
    with mind.engine.db.connect() as conn:
        assert history_compaction.progress(conn, mind.scope.key())["through"] == 0
    assert marker(mind)
    held = archive_of(mind, archive_name(mind))
    assert last not in held and 0 < len(held) < len(texts)
    missing = set(texts) - set(held)
    # The resume re-walks the archive phase, keeps the copies already there, and finishes.
    monkeypatch.setattr(history_compaction, "_archive_batch", real)
    done = run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    assert done["state"] == "complete" and done["archived"] == len(missing)
    assert done["archive_confirmed"] == len(texts)
    rebuilds(mind, texts)
    assert run(mind, "history-compact-verify")["state"] == "verified"


def test_interrupted_between_the_phases_resumes_safely(setup, quiet, monkeypatch):
    """The widest window an interruption can land in: the whole set is archived and verified, and
    not one row of it has been rewritten yet. The resume re-verifies every copy it already holds
    and copies in nothing twice."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=5)
    before = stored_rows(mind)
    real = history_compaction._archive_target_set

    def archive_everything_then_fall_over(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt("interrupted between the archive and the rewrite phases")

    monkeypatch.setattr(history_compaction, "_archive_target_set", archive_everything_then_fall_over)
    with pytest.raises(KeyboardInterrupt):
        run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    unchanged(mind, before)
    assert marker(mind)
    # Phase one really had finished: the archive holds every original, verified.
    assert set(archive_of(mind, archive_name(mind))) == set(before)
    monkeypatch.setattr(history_compaction, "_archive_target_set", real)
    done = run(mind, "history-compact", {"apply": True, "batch": 4}, quiet)
    assert done["state"] == "complete"
    assert done["archived"] == 0 and done["archive_confirmed"] == len(before)
    rebuilds(mind, texts)
    assert run(mind, "history-compact-verify")["state"] == "verified"


def test_an_archive_row_that_disagrees_with_the_store_stops_the_run(setup, quiet):
    """`OR IGNORE` keeps the copy the archive already holds. When that copy is not the row in the
    store, the read-back comparison refuses — and because the comparison is phase one, it refuses
    before the first rewrite, with the store still closed to the writer."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    before = stored_rows(mind)
    path = history_compaction.archive_dir(mind.engine.db) / archive_name(mind)
    # A row that is not of this store, planted where the run will trust nothing but its own
    # read-back comparison to find it.
    row = before[sorted(texts)[1]]
    conn = history_compaction._archive_connection(path, create=True)
    try:
        conn.execute("INSERT INTO mind_events_v1 VALUES(?,?,?,?,?,?,?,?)",
                     (row["scope"], row["revision"], row["id"], row["kind"], row["occurred_at"],
                      '{"request":null,"snapshot":{"planted":true}}', "0" * 64, mind.clock()))
    finally:
        conn.close()
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True}, quiet)
    assert refusal.value.target == "archive-row-differs"
    assert refusal.value.actual["revision"] == row["revision"]
    # The rewrite never started, and the marker stays set: the store is closed until someone has
    # looked. A write attempted meanwhile is refused.
    unchanged(mind, before)
    assert marker(mind)
    clock[0] += timedelta(minutes=9)
    with pytest.raises(Conflict) as refused:
        mind.record(AffectiveEvent(
            command_id="during-compaction", agent_version="synthetic-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[source("observed-later")],
            values={"mood": 44}, reason="A sourced synthetic observation"))
    assert refused.value.code == history.COMPACTING
    # The operator's way out: the archive's bad copy is removed, and the next run archives the one
    # row it was missing, re-verifies the rest, and completes.
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM mind_events_v1 WHERE revision=?", (row["revision"],))
        conn.commit()
    finally:
        conn.close()
    done = run(mind, "history-compact", {"apply": True}, quiet)
    assert done["state"] == "complete" and done["archived"] == 1
    assert done["archive_confirmed"] == len(texts)
    rebuilds(mind, texts)


def test_a_row_that_escapes_the_archive_is_never_rewritten(setup, quiet, monkeypatch):
    """The loop's own proof, fault-injected: phase one is made to skip one row, and the rewrite
    must stop at that row rather than touch it — however a row came to be uncovered."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=5)
    before = stored_rows(mind)
    escaped = sorted(texts)[3]
    real = history_compaction._archive_batch

    def skip_one_row(path, scope, rows_, **kwargs):
        kept = [row for row in rows_ if row["revision"] != escaped]
        return real(path, scope, kept, **kwargs) if kept else 0

    monkeypatch.setattr(history_compaction, "_archive_batch", skip_one_row)
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True, "batch": 2}, quiet)
    assert refusal.value.target == "row-not-in-verified-archive"
    assert refusal.value.actual["revision"] == escaped
    # The batches before it committed; it, the row batched with it, and everything after are
    # exactly as they were.
    with mind.engine.db.connect() as conn:
        through = history_compaction.progress(conn, mind.scope.key())["through"]
    assert 0 < through < escaped
    current = stored_rows(mind)
    for revision, row in before.items():
        if revision > through:
            assert current[revision] == row, revision
    assert marker(mind)
    # With the fault removed the resume archives the row it missed — that one row, no other — and
    # finishes.
    monkeypatch.setattr(history_compaction, "_archive_batch", real)
    done = run(mind, "history-compact", {"apply": True, "batch": 2}, quiet)
    assert done["state"] == "complete" and done["archived"] == 1
    rebuilds(mind, texts)
    assert run(mind, "history-compact-verify")["state"] == "verified"


def test_a_bad_rewrite_in_a_later_batch_is_still_caught(setup, quiet, monkeypatch):
    """The per-batch landed verification, unchanged by the phase split: batches have committed
    before this one, the patch it writes does not rebuild its row, and the batch goes down with
    nothing of it committed."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=5)
    before = stored_rows(mind)
    target = sorted(texts)[7]
    real = history.patch_row

    def a_patch_that_loses_a_key(payload, state, **kw):
        row = real(payload, state, **kw)
        if state.get("revision") == target:
            row["patch"] = [op for op in row["patch"] if op[1][:1] != ["updated_at"]] or row["patch"]
        return row

    monkeypatch.setattr(history_compaction.history, "patch_row", a_patch_that_loses_a_key)
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True, "batch": 2}, quiet)
    assert refusal.value.target in {"patch-does-not-rebuild-the-row",
                                    "rebuilt-row-differs-from-the-original"}
    # Earlier batches are committed work; the batch holding the bad rewrite is rolled back whole.
    with mind.engine.db.connect() as conn:
        through = history_compaction.progress(conn, mind.scope.key())["through"]
    assert 0 < through < target
    current = stored_rows(mind)
    assert current[sorted(texts)[1]] != before[sorted(texts)[1]]
    for revision, row in before.items():
        if revision > through:
            assert current[revision] == row, revision
    assert marker(mind)


# --- rows this will not touch ----------------------------------------------------------------------------


def test_a_row_that_will_not_parse_stops_the_run_rather_than_being_rewritten(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=4)
    before = stored_rows(mind)
    broken = sorted(texts)[2]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     ("{not json at all", mind.scope.key(), broken))
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True}, quiet)
    assert refusal.value.target == "row-will-not-parse"
    assert refusal.value.actual["revision"] == broken
    # The rows below it were archived and rewritten; the broken one is exactly as it was found.
    assert stored_rows(mind)[broken]["data"] == "{not json at all"
    assert dumps(before[broken + 1]) == dumps(stored_rows(mind)[broken + 1])


def test_a_row_carrying_something_nobody_described_is_not_rewritten(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=3)
    odd = sorted(texts)[2]
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                       (mind.scope.key(), odd)).fetchone()[0])
        data["something_else"] = {"kept": True}
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (dumps(data), mind.scope.key(), odd))
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-compact", {"apply": True}, quiet)
    assert refusal.value.target == "row-carries-keys-nobody-described"
    assert refusal.value.actual["keys"] == ["something_else"]


# --- the dry run -------------------------------------------------------------------------------------------


def test_the_dry_run_writes_nothing_and_reports_every_verdict(setup, host, monkeypatch):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=4)
    before = stored_rows(mind)
    config = quiet_host(host)
    monkeypatch.setattr(liveness, "probe_process",
                        probe({4242: ("Fri Sep 18 10:37:19 2026", "/usr/bin/node service.mjs")}))
    planned = run(mind, "history-compact", {}, config)
    assert planned["state"] == "dry-run" and planned["ready"] is False
    # Every precondition is reported, not only the first one that failed.
    verdicts = {check["check"]: check["ready"] for check in planned["preconditions"]}
    assert verdicts == {"quiescence": False, "cursor": True, "backup": True, "disk": True}
    assert planned["planned_whole"] and planned["estimated_patch_share"] < 0.5
    assert planned["rows"] == len(before) and planned["cursor"] == 0
    unchanged(mind, before)
    assert not marker(mind)
    assert not history_compaction.archive_dir(mind.engine.db).exists()


def test_the_dry_run_is_what_the_run_then_does(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=5)
    planned = run(mind, "history-compact", {}, quiet)
    assert planned["ready"] is True
    done = run(mind, "history-compact", {"apply": True}, quiet)
    # Which rows keep their document is not an estimate at all, and is exact.
    assert done["checkpoints"] == planned["planned_whole"]
    # The bytes are, and the sample is the first batch: in this fixture the document doubles over
    # the run, so the later patches are bigger than the ones measured. Even here it is the right
    # size, and on a history whose document is not still growing it lands within a few per cent.
    assert abs(done["bytes_after"] - planned["estimated_bytes_after"]) < done["bytes_after"] / 2
    assert planned["estimated_bytes_after"] < planned["bytes_remaining"] / 3


# --- putting it back -------------------------------------------------------------------------------------


def test_the_archive_puts_every_row_back_exactly_as_it_was(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=5)
    before = stored_rows(mind)
    done = run(mind, "history-compact", {"apply": True}, quiet)
    assert stored_rows(mind) != before
    planned = run(mind, "history-restore", {}, quiet)
    assert planned["state"] == "dry-run" and planned["would_restore"] == done["rewritten"]
    assert stored_rows(mind) != before
    restored = run(mind, "history-restore", {"apply": True}, quiet)
    assert restored["state"] == "restored" and restored["restored"] == done["rewritten"]
    assert stored_rows(mind) == before
    assert restored["cursor"] == 0 and not marker(mind)
    rebuilds(mind, texts)
    # The archive is still there. Nothing here deletes it, ever.
    assert (history_compaction.archive_dir(mind.engine.db) / done["archive"]).exists()
    # And a compaction can simply be run again afterwards.
    again = run(mind, "history-compact", {"apply": True}, quiet)
    assert again["state"] == "complete" and again["rewritten"] == done["rewritten"]
    # Nothing new went into the archive: it already held every one of those rows, and each was
    # read back out and compared with the store again before anything was touched.
    assert again["archived"] == 0 and again["archive_confirmed"] == done["archive_confirmed"]
    rebuilds(mind, texts)


def test_restoring_half_a_compaction_reopens_the_store(setup, quiet):
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=6)
    before = stored_rows(mind)
    stopped = run(mind, "history-compact", {"apply": True, "batch": 4, "limit": 4}, quiet)
    assert marker(mind)
    restored = run(mind, "history-restore", {"apply": True}, quiet)
    # Only the batches that were archived are in the archive, and they are exactly the rows that
    # were rewritten, so every one of them goes back.
    assert restored["restored"] == stopped["rewritten"] == restored["archived"]
    assert stored_rows(mind) == before
    assert not marker(mind)
    clock[0] += timedelta(minutes=9)
    assert mind.record(AffectiveEvent(
        command_id="after-restore", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("observed-after-restore")],
        values={"mood": 41}, reason="A sourced synthetic observation"))["revision"] == max(texts) + 1


def test_a_restore_refuses_while_the_host_is_running(setup, host, monkeypatch, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    run(mind, "history-compact", {"apply": True}, quiet)
    after = stored_rows(mind)
    config = quiet_host(host)
    monkeypatch.setattr(liveness, "probe_process",
                        probe({4242: ("Fri Sep 18 10:37:19 2026", "/usr/bin/node service.mjs")}))
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-restore", {"apply": True}, config)
    assert refusal.value.target == "store-is-not-quiet"
    assert stored_rows(mind) == after


def test_a_restore_without_an_archive_is_a_refusal_not_an_empty_success(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=2)
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-restore", {"apply": True}, quiet)
    assert refusal.value.target == "archive-missing"


def test_a_row_whose_columns_moved_is_never_written_over(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=3)
    done = run(mind, "history-compact", {"apply": True}, quiet)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_events SET kind='rewritten-by-someone-else'"
                     " WHERE scope=? AND revision=2", (mind.scope.key(),))
    with pytest.raises(Conflict) as refusal:
        run(mind, "history-restore", {"apply": True}, quiet)
    assert refusal.value.target == "row-column-has-moved"
    assert refusal.value.actual["column"] == "kind"


# --- what an operator is told afterwards --------------------------------------------------------------------


def test_the_host_hands_these_commands_what_it_knows_about_itself(setup, quiet):
    """The one thing the registry could not decide for itself: compaction has to prove nothing is
    running, and where the host keeps its pid files is not in the store. It arrives positionally
    from the host's own configuration, so a request can never supply its own."""
    from kin_mind.host import dispatch as host_dispatch

    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=3)
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
              "agent_version": "synthetic-v1", "session_id": "fixture", **quiet}
    assert host_dispatch(config, "history-status", {})["rows"] == len(texts)
    done = host_dispatch(config, "history-compact", {"apply": True})
    assert done["state"] == "complete" and done["rewritten"] == len(texts)
    assert host_dispatch(config, "history-compact-verify", {})["state"] == "verified"
    assert host_dispatch(config, "history-restore", {"apply": True})["state"] == "restored"
    # A request that tried to name its own configuration is refused rather than believed.
    with pytest.raises(TypeError):
        host_dispatch(config, "history-compact", {"config": {"liveness": {}}})


def test_a_history_written_by_the_previous_release_is_the_one_that_gets_compacted(setup, quiet):
    """The shape the real store is in, which no other test here has.

    Every row production holds was written before there was a hash to write: `{request, snapshot}`
    and nothing else. Those are the rows compaction meets, and they are the rows `history-verify`
    can say nothing at all about — so this is also where the change in what may be claimed shows
    up. Before: eight hundred and seventy rows nobody can check. After: every one of them rebuilt
    and compared with the bytes the archive took before anything moved."""
    mind, source, clock = setup
    texts = prepared(mind, source, clock, rounds=6)
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        for revision in texts:
            data = json.loads(conn.execute(
                "SELECT data FROM mind_events WHERE scope=? AND revision=?",
                (scope, revision)).fetchone()[0])
            data.pop("state_hash", None)
            conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                         (dumps(data), scope, revision))
    before = run(mind, "history-verify")
    assert before["state"] == "partial" and before["unverifiable"] == len(texts)
    assert before["verified"] == 0 and before["compacted_through"] == 0
    view = mind.read(history=100)["history"]

    done = run(mind, "history-compact", {"apply": True}, quiet)
    assert done["state"] == "complete" and done["rewritten"] == len(texts)
    rebuilds(mind, texts)
    assert dumps(mind.read(history=100)["history"]) == dumps(view)

    after = run(mind, "history-verify")
    assert after["state"] == "verified" and after["unverifiable"] == 0
    # Which is a narrower claim than it reads as, and the report says which one it is.
    assert after["compacted_through"] == done["cursor"] and after["compacted_reason"]
    checked = run(mind, "history-compact-verify", {"deep": True})
    assert checked["state"] == "verified" and checked["rebuilt"] == len(texts)


def test_every_operator_action_that_writes_is_told_to_by_the_apply_flag():
    """The list a terminal reads `--apply` against, which two packages of this stage edited on the
    same line. It was left holding two assignments, the first of them dead, so the wish archive's
    two commands fell off it: from a terminal `--apply` set nothing and they could only ever report
    what they would have done. One list, and this is what keeps it one."""
    from kin_mind.host import APPLY_ACTIONS

    assert set(APPLY_ACTIONS) >= {
        "migrate-evidence-isolation", "evidence-keys-backfill", "desire-archive", "desire-unarchive",
        "maintenance-tick", "vector-optimize", "history-compact", "history-restore"}
    # And the one that writes nothing is not on it: it takes no `apply` and would refuse the word.
    assert "history-compact-verify" not in APPLY_ACTIONS


def test_the_reports_say_what_a_verified_verdict_covers(setup, quiet):
    mind, source, clock = setup
    prepared(mind, source, clock, rounds=4)
    before = run(mind, "history-verify")
    # Nothing carries a hash yet, so nothing can be said about any of it.
    assert before["state"] == "verified" and before["compacted_through"] == 0
    assert before["compacted_reason"] is None
    done = run(mind, "history-compact", {"apply": True}, quiet)
    after = run(mind, "history-verify")
    assert after["state"] == "verified" and after["compacted_through"] == done["cursor"]
    # And it says, in the report itself, that this verdict is not the one that matters for them.
    assert "history-compact-verify" in after["compacted_reason"]
    assert run(mind, "history-status")["compacted_through"] == done["cursor"]
