"""The deploy-time data repair: a dry run writes nothing, an apply archives rather than deletes,
and a second run finds nothing left (DB1-01, DB1-07, DB1-08, DB1-13, K4-12)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine, repair
from eventmem.core.db import digest
from eventmem.core.models import Scope, SourceInput

from kin_mind import graph as graph_module
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind


def root_id(sid):
    return "mem_" + digest([sid, "root"])[:32]


def snapshot(path):
    """Every row of every ordinary table, to prove a dry run wrote nothing."""
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%search%' ORDER BY name")]
        return {table: conn.execute(f"SELECT * FROM '{table}' ORDER BY 1").fetchall() for table in tables}


@pytest.fixture
def store(tmp_path):
    clock = [datetime(2026, 9, 22, 8, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    scope = Scope(persona="synthetic-repair")
    mind = Mind(engine, scope, clock=lambda: clock[0].isoformat(timespec="microseconds"))
    MemoryContinuity(mind)

    def source(namespace, key, text, *, version="1", authority="operation", metadata=None):
        clock[0] += timedelta(seconds=1)
        return engine.receive(SourceInput(namespace=namespace, key=key, version=version, text=text, scope=scope,
                                          authority=authority, occurred_at=mind.clock(), extract=False,
                                          metadata=metadata or {}))["id"]

    notes = [source("kin-session-maintenance", f"review-{i}", "宿主请求检查当前原生会话。此事件不是用户消息。",
                    metadata={"maintenance_only": True}) for i in range(4)]
    owner = source("kin-owner-input", "owner", "今天去买菜", authority="explicit",
                   metadata={"role": "user", "host_event": "message"})
    old, new = (source("kin-ica-setup", "user-names", text, version=version, authority="explicit")
                for version, text in (("1", "旧的称呼设置"), ("2", "新的称呼设置")))
    with engine.db.connect(write=True) as conn:
        # What a store written before this release looks like: the maintenance notes have no
        # classification row and sit in the text index and the organise queue like any memory,
        # both versions of one source are active, the organise queue holds a record that left
        # active use, and the node indexes are not aligned.
        conn.execute("DELETE FROM source_evidence_class WHERE source_id IN (%s)" % ",".join("?" * len(notes)), notes)
        for sid in notes:
            rowid, data = conn.execute("SELECT rowid,data FROM records WHERE id=?", (root_id(sid),)).fetchone()
            conn.execute("INSERT INTO search(rowid,id,tokens) VALUES(?,?,?)", (rowid, root_id(sid), "宿主 请求 检查 会话"))
            conn.execute("INSERT INTO dirty VALUES(?,1)", (root_id(sid),))
        data = json.loads(conn.execute("SELECT data FROM records WHERE id=?", (root_id(old),)).fetchone()[0])
        data["status"] = "active"
        conn.execute("UPDATE records SET status='active',data=? WHERE id=?", (json.dumps(data), root_id(old)))
        conn.execute("INSERT OR REPLACE INTO dirty VALUES(?,1)", ("mem_" + "0" * 32,))
        conn.execute("DELETE FROM meta WHERE key IN (?,?)", (graph_module.SEARCH_ALIGNED, "mind_memory_search_rowid"))
    return engine, mind, notes, owner, (old, new)


def test_dry_run_writes_nothing_and_says_what_it_would_do(store):
    engine, mind, notes, owner, (old, new) = store
    before = snapshot(engine.db.path)
    report = repair.run(engine.db.root, apply=False)
    assert snapshot(engine.db.path) == before
    steps = report["steps"]
    assert steps["origins"]["plan"]["by_namespace"] == {"kin-session-maintenance": 4}
    assert steps["maintenance"]["plan"] == {"unindex": 4, "unqueue": 4, "archive": 4, "kept_because_cited": 0}
    assert steps["versions"]["plan"]["records_to_supersede"] == 1
    assert steps["queues"]["plan"]["dirty"] == 1
    assert set(steps["indexes"]["plan"]) == {"graph", "memory"}
    # Identifiers and counts only: no text of any record is in the report.
    assert "宿主" not in json.dumps(report, ensure_ascii=False) and "称呼" not in json.dumps(report, ensure_ascii=False)


def test_apply_archives_relabels_and_a_second_run_finds_nothing(store):
    engine, mind, notes, owner, (old, new) = store
    with engine.db.connect() as conn:
        records_before = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        sources_before = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    report = repair.run(engine.db.root, apply=True)
    assert report["steps"]["maintenance"]["done"]["archived"] == 4
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == records_before
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == sources_before
        statuses = {rid: status for rid, status in conn.execute("SELECT id,status FROM records")}
        rules = {sid: rule for sid, rule in conn.execute("SELECT source_id,rule FROM source_evidence_class")}
        indexed = {row[0] for row in conn.execute("SELECT id FROM search")}
        dirty = {row[0] for row in conn.execute("SELECT record_id FROM dirty")}
        aligned = {row[0] for row in conn.execute("SELECT key FROM meta WHERE key IN (?,?)",
                                                  (graph_module.SEARCH_ALIGNED, "mind_memory_search_rowid"))}
    assert all(statuses[root_id(sid)] == "archived" for sid in notes)
    assert all(rules[sid] == "origin-host_maintenance" for sid in notes)
    assert not {root_id(sid) for sid in notes} & (indexed | dirty)
    assert statuses[root_id(owner)] == "active" and root_id(owner) in indexed
    assert statuses[root_id(old)] == "superseded" and statuses[root_id(new)] == "active"
    assert "mem_" + "0" * 32 not in dirty and len(aligned) == 2
    # Archived by a revision: the history of each note is kept.
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM revisions WHERE record_id=?", (root_id(notes[0]),)).fetchone()[0] == 2
    again = repair.run(engine.db.root, apply=False)
    plans = {step: entry["plan"] for step, entry in again["steps"].items()}
    assert plans["origins"]["sources"] == 0
    assert plans["maintenance"] == {"unindex": 0, "unqueue": 0, "archive": 0, "kept_because_cited": 0}
    assert plans["versions"]["groups"] == 0 and plans["queues"]["dirty"] == 0 and plans["indexes"] == {}


def test_a_note_the_current_state_still_cites_is_left_active(store):
    engine, mind, notes, owner, versions = store
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_state VALUES(?,?,?)",
                     (mind.scope.key(), 1, json.dumps({"session_advice": {"evidence": [{"source_id": notes[0]}]}})))
    report = repair.run(engine.db.root, apply=True, steps=("origins", "maintenance"))
    assert report["steps"]["maintenance"]["plan"]["kept_because_cited"] == 1
    assert report["steps"]["maintenance"]["done"]["archived"] == 3
    with engine.db.connect() as conn:
        assert conn.execute("SELECT status FROM records WHERE id=?", (root_id(notes[0]),)).fetchone()[0] == "active"


def test_unknown_steps_are_refused(store):
    engine = store[0]
    with pytest.raises(ValueError):
        repair.run(engine.db.root, steps=("origins", "drop-everything"))


def test_embeds_that_failed_on_a_down_service_are_queued_again_once(store):
    engine, mind, notes, owner, versions = store
    with engine.db.connect(write=True) as conn:
        revision = conn.execute("SELECT revision FROM records WHERE id=?", (root_id(owner),)).fetchone()[0]
        down = engine.enqueue("embed", {"record_id": root_id(owner), "revision": revision},
                              f"embed:{root_id(owner)}:{revision}:down", conn=conn)
        bad = engine.enqueue("embed", {"record_id": root_id(owner), "revision": revision},
                             f"embed:{root_id(owner)}:{revision}:bad", conn=conn)
        conn.execute("UPDATE jobs SET state='failed',attempts=5,error='ConnectError' WHERE id=?", (down,))
        conn.execute("UPDATE jobs SET state='failed',attempts=5,error='ValueError' WHERE id=?", (bad,))
    report = repair.run(engine.db.root, apply=True, steps=("queues",))
    assert report["steps"]["queues"]["plan"]["embed_jobs"] == 1 and report["steps"]["queues"]["done"]["embed_jobs"] == 1
    with engine.db.connect() as conn:
        states = {jid: state for jid, state in conn.execute("SELECT id,state FROM jobs WHERE id IN (?,?)", (down, bad))}
        assert conn.execute("SELECT COUNT(*) FROM job_recovery WHERE job_id=?", (down,)).fetchone()[0] == 1
    assert states == {down: "pending", bad: "failed"}
    assert repair.run(engine.db.root, steps=("queues",))["steps"]["queues"]["plan"]["embed_jobs"] == 0


def test_quarantined_work_whose_evidence_is_gone_is_retired_only_when_asked(store):
    engine, mind, notes, owner, versions = store
    from kin_mind.appraisal import Appraisals

    Appraisals(mind)
    with engine.db.connect(write=True) as conn:
        for identifier, evidence, reason in (("gone", ["src_" + "0" * 32], "Missing"),
                                             ("kept", [owner], "native-review-unconfirmed")):
            conn.execute("INSERT INTO mind_appraisals VALUES(?,?,?,?,0,2,?)",
                         (identifier, mind.scope.key(), "needs-repair", 0,
                          json.dumps({"evidence_ids": evidence, "repair_reason": reason})))
    assert "quarantine" not in repair.run(engine.db.root)["steps"]
    report = repair.run(engine.db.root, apply=True, steps=("quarantine",))
    assert report["steps"]["quarantine"]["plan"] == {
        "quarantined": 2, "by_reason": {"Missing": 1, "native-review-unconfirmed": 1}, "evidence_gone": 1}
    assert report["steps"]["quarantine"]["done"] == {"retired": 1}
    with engine.db.connect() as conn:
        states = dict(conn.execute("SELECT id,state FROM mind_appraisals WHERE id IN ('gone','kept')").fetchall())
    assert states == {"gone": "superseded", "kept": "needs-repair"}


def test_the_old_evidence_scan_is_switched_off_only_where_the_table_agrees(store):
    engine, mind, notes, owner, versions = store
    from kin_mind import evidence_keys
    from kin_mind.autonomy_schema import optimized

    other = Mind(engine, Scope(persona="synthetic-repair-other"))
    MemoryContinuity(other)
    with engine.db.connect(write=True) as conn:
        conn.execute(evidence_keys.SCHEMA)
        for scope in (mind.scope.key(), other.scope.key()):
            conn.execute("INSERT OR IGNORE INTO mind_memory_config VALUES(?,?)", (scope, "{}"))
        # The other scope's table credits a key no history row introduced: it disagrees.
        conn.execute("INSERT INTO mind_evidence_keys VALUES(?,?,?,?)", (other.scope.key(), "k" * 64, "evt", 1))
    dry = repair.run(engine.db.root, steps=("evidence",))["steps"]["evidence"]["plan"]
    assert dry["scopes"] == 2 and dry["disagreements"] >= 1
    done = repair.run(engine.db.root, apply=True, steps=("evidence",))["steps"]["evidence"]["done"]
    assert done == {"legacy_scan_off": 1, "left_on": 1}
    with engine.db.connect() as conn:
        assert not optimized(conn, mind.scope.key(), evidence_keys.LEGACY_FLAG)
        assert optimized(conn, other.scope.key(), evidence_keys.LEGACY_FLAG)


# What this release adds to a store's structure. A store written by the release before has none of
# it, and a dry run must leave it that way (CR-MEM-05).
ADDED_INDEXES = ("job_recent", "job_state_recent", "mind_action_event_recent", "mind_action_event_state",
                 "mind_plan_history_command", "mind_plan_wish_sync")
ADDED_TABLES = ("mind_session_advice",)
SQLITE_OWN = ("memory.sqlite3-wal", "memory.sqlite3-shm")


def as_before_this_release(root):
    conn = sqlite3.connect(root / "memory.sqlite3", isolation_level=None)
    for name in ADDED_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    for name in ADDED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    cache = root / "cache"
    if cache.exists():
        for path in sorted(cache.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        cache.rmdir()
    (root / "memory.sqlite3").chmod(0o644)


def on_disk(root):
    """The store's structure, its metadata, its bytes and mode, and every path under the root."""
    import hashlib

    path = root / "memory.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        structure = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        meta = conn.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
    conn.close()
    return {"structure": structure, "meta": meta, "bytes": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": path.stat().st_mode & 0o777,
            "paths": sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.name not in SQLITE_OWN)}


def test_a_dry_run_on_a_store_from_before_this_release_builds_nothing(store, capsys):
    engine = store[0]
    root = engine.db.root
    as_before_this_release(root)
    before = on_disk(root)
    assert not {name for _, name, _ in before["structure"]} & {"job_recent", "job_state_recent"}
    assert repair.main(["--root", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False and report["steps"]["origins"]["plan"]["by_namespace"] == {
        "kin-session-maintenance": 4}
    # Every step read, nothing built: no index or table of this release, no metadata filled in, not a
    # byte of the file, not its mode, no cache directory beside it.
    assert on_disk(root) == before
    # The read-only connection refuses a write outright.
    with pytest.raises(PermissionError):
        with repair.ReadOnlyStore(root / "memory.sqlite3").connect(write=True):
            pass
    # Only an apply builds this release's structure.
    repair.run(root, apply=True, steps=("indexes",))
    after = on_disk(root)
    assert {"job_recent", "job_state_recent"} <= {name for _, name, _ in after["structure"]}
    assert ("schema_version", 1) in after["meta"]


def test_a_root_without_a_store_is_refused_and_nothing_is_created(tmp_path, capsys):
    missing = tmp_path / "mistyped" / "MemoryPalace"
    for argv in (["--root", str(missing)], ["--root", str(missing), "--apply"]):
        assert repair.main(argv + ["--output", str(tmp_path / "report.json")]) == 2
        out, err = capsys.readouterr()
        assert out == "" and "refused" in err and "No memory store" in err
    assert not (tmp_path / "mistyped").exists() and not (tmp_path / "report.json").exists()
    with pytest.raises(repair.Refused):
        repair.run(missing, apply=True)
    assert not (tmp_path / "mistyped").exists()


def test_the_operations_guide_says_what_the_repair_deletes_as_the_repair_itself_does():
    """The guide's repair section and the module's own description agree: only `reerase` deletes, and
    no sentence of the guide says that no step does (CL6D-MM-03)."""
    import re
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[2] / "docs" / "operations.md").read_text(encoding="utf-8")
    start = guide.index("python -m eventmem.core.repair")
    section = " ".join(guide[start:guide.index("\n## ", start)].split())
    described = " ".join((repair.__doc__ or "").split())
    for text in (section, described):
        assert "Only `reerase` deletes" in text
    assert not re.search(r"(?<!other )\b[Nn]o step deletes", section), "the guide's own words contradict the reerase step"
    # Both say what the store holds beside what the deletes take, and that reerase may come a release after lineage.
    for text in (section, described):
        assert "`store_by_kind`" in text
    assert "`reerase` may run in a later release than `lineage`" in section and "in the same run or a later one" in described


def side_files(root):
    return sorted(path.name for path in root.iterdir() if path.name in SQLITE_OWN)


def leave(path):
    """The last connection to leave a store in WAL mode, as a writer can: it takes the side files away."""
    conn = sqlite3.connect(path)
    conn.execute("SELECT 1 FROM meta").fetchall()
    conn.close()


def test_a_snapshot_dry_run_leaves_not_even_sqlites_side_files_and_reports_what_a_dry_run_of_the_store_does(store, capsys):
    """A dry run of the store itself leaves SQLite's `-wal` and `-shm` beside a store in WAL mode: a
    read-only reader cannot take them away when it is the last to leave. A snapshot reads a clone
    taken while the store is still, outside the root, and leaves nothing beside the store -- not
    those either, not a byte, not its times -- and no clone behind it; its report is the one a dry
    run of the store gives. It is for a dry run only (WS7's dry run before the services stop)."""
    import os
    import tempfile

    engine = store[0]
    root = engine.db.root
    as_before_this_release(root)
    # The rest of the root is read where it lies: a host receipt nobody can replay.
    (root / "host-spool").mkdir()
    (root / "host-spool" / "unreadable.json").write_text("{")
    assert side_files(root) == []
    plain = repair.run(root)
    assert plain["steps"]["spool"]["plan"] == {"receipts": 1, "unreadable": 1}
    assert side_files(root) == sorted(SQLITE_OWN), "the plain dry run leaves them"
    # (`on_disk` reads the store as the plain dry run does, and leaves them too.)
    before = on_disk(root)
    leave(root / "memory.sqlite3")
    listing, stat = sorted(path.name for path in root.iterdir()), os.stat(root / "memory.sqlite3")
    assert side_files(root) == []
    still = repair.run(root, still=True)
    assert still["snapshot"] == {"how": "still-clone", "tries": 1, "side_files": []}
    assert still["steps"] == plain["steps"] and still["applied"] is False
    # The command: --snapshot says how the store was taken; with --apply it is refused, and nothing runs.
    assert repair.main(["--root", str(root), "--snapshot", "--steps", "origins"]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot"]["how"] == "still-clone"
    assert repair.main(["--root", str(root), "--snapshot", "--apply"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "refused" in err and "dry run" in err
    # Nothing beside the store, the store as it was to its times, no clone left behind.
    assert sorted(path.name for path in root.iterdir()) == listing
    now = os.stat(root / "memory.sqlite3")
    assert (now.st_ino, now.st_size, now.st_mtime_ns, now.st_ctime_ns) == (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    assert not [entry for entry in os.scandir(tempfile.gettempdir()) if entry.name.startswith("kin-repair-snapshot-")]
    assert on_disk(root) == before
    # The guide and the module say so.
    from pathlib import Path

    guide = " ".join((Path(__file__).resolve().parents[2] / "docs" / "operations.md").read_text(encoding="utf-8").split())
    assert "--snapshot --output /outside/the/root/dry-run.json" in guide and "leaves not even those" in guide
    assert "`--snapshot` leaves not even those" in " ".join((repair.__doc__ or "").split())


def test_a_snapshot_waits_for_a_still_moment_takes_again_a_store_that_moved_and_is_refused_by_one_that_never_stops(tmp_path):
    """Held open: it waits and looks again. Written while it cloned: it clones again, and the clone
    has the write. Never still within the wait: refused, and nothing is left where the clone was to go."""
    root = tmp_path / "root"
    root.mkdir()
    path = root / "memory.sqlite3"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.close()
    # Held before the clone; free before it, held after it; free before and after.
    slept, answers = [], iter(["some", "none", "some", "none", "none"])
    into = tmp_path / "held"
    into.mkdir()
    taken = repair.snapshot(path, into, held=lambda files: next(answers), sleep=slept.append, clock=lambda: 0.0)
    assert taken == {"how": "still-clone", "tries": 3, "side_files": []} and slept == [repair.SNAPSHOT_PAUSE] * 2
    looks = []

    def meanwhile(files):
        looks.append(files)
        if len(looks) == 2:
            # Between the clone and the look after it, a writer came and went.
            writer = sqlite3.connect(path, isolation_level=None)
            writer.execute("INSERT INTO meta VALUES('written-meanwhile','1')")
            writer.close()
        return "none"

    into = tmp_path / "moved"
    into.mkdir()
    taken = repair.snapshot(path, into, held=meanwhile, sleep=lambda seconds: None, clock=lambda: 0.0)
    assert taken["tries"] == 2 and len(looks) == 4
    with sqlite3.connect(into / "memory.sqlite3") as clone:
        assert clone.execute("SELECT value FROM meta WHERE key='written-meanwhile'").fetchone() == ("1",)
    clock, into = [0.0], tmp_path / "busy"
    into.mkdir()
    with pytest.raises(repair.Refused, match="not still for a moment in 5 seconds"):
        repair.snapshot(path, into, wait=5, held=lambda files: "some", sleep=lambda seconds: clock.__setitem__(0, clock[0] + 1),
                        clock=lambda: clock[0])
    assert list(into.iterdir()) == []


def test_a_wal_a_crash_left_behind_is_cloned_with_the_store_and_recovered_in_the_clone_never_beside_the_store(tmp_path):
    """A process that wrote and died leaves its WAL, which nobody holds: the snapshot clones it with
    the store and replays it in the clone. The store and its side files stay exactly as they were."""
    import os
    import subprocess
    import sys

    root = tmp_path / "root"
    root.mkdir()
    path = root / "memory.sqlite3"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.close()
    crash = ("import os, sqlite3, sys\n"
             "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
             "conn.execute('PRAGMA wal_autocheckpoint=0')\n"
             "conn.execute(\"INSERT INTO meta VALUES('only-in-the-wal','1')\")\n"
             "os._exit(0)\n")
    subprocess.run([sys.executable, "-c", crash, str(path)], check=True)
    assert side_files(root) == sorted(SQLITE_OWN)
    stat = {name: os.stat(root / name) for name in ("memory.sqlite3", *SQLITE_OWN)}
    into = tmp_path / "clone"
    into.mkdir()
    taken = repair.snapshot(path, into)
    assert taken == {"how": "still-clone", "tries": 1, "side_files": ["-wal", "-shm"]}
    # Replayed into the clone's own file: read as it lies, without any WAL, it has the write.
    clone = sqlite3.connect((into / "memory.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)
    assert clone.execute("SELECT value FROM meta WHERE key='only-in-the-wal'").fetchone() == ("1",)
    clone.close()
    for name, was in stat.items():
        now = os.stat(root / name)
        assert (now.st_ino, now.st_size, now.st_mtime_ns) == (was.st_ino, was.st_size, was.st_mtime_ns), name
