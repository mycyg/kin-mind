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
