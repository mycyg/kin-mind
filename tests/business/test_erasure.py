"""An explicit delete reaches every layer the mind built from what it deleted, and stays in force
when an old history archive is put back (K4-01, K4-20, K4-21, E2-06, K3-15)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.jobs import Worker
from eventmem.core.maintenance import deletion_preview
from eventmem.core.models import RecordInput, Scope, SourceInput

from kin_mind import erasure, history, history_compaction
from kin_mind.context import Contexts
from kin_mind.memory import MemoryContinuity
from kin_mind.state import DesireChange, Mind

MARKER = "zetaquux"


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 20, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, Scope(persona="synthetic-erase"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)

    def source(key, text=None):
        return engine.receive(SourceInput(namespace="synthetic", key=key, text=text or key, scope=mind.scope,
                                          authority="explicit", occurred_at=mind.clock(), extract=False))["id"]

    mind.initialize(agent_version="fixture-v1", evidence_ids=[source("initial")])
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True})
    Contexts(mind)
    return mind, memory, source, clock


def settle(engine, limit=500):
    worker = Worker(engine)
    for _ in range(limit):
        if not worker.run_once():
            return
    raise AssertionError("the worker did not settle")


def wish(mind, clock, evidence, key, content):
    clock[0] += timedelta(minutes=5)
    return mind.manage_desire(DesireChange(
        command_id=key, agent_version="fixture-v1", expected_revision=mind.read()["revision"],
        evidence_ids=evidence, action="create", content=content, topic="synthetic " + key, kind="contact",
        strength=60, expires_at=(datetime.fromisoformat(mind.clock()) + timedelta(days=3)).isoformat(),
        completion="The owner has it", reason="Worth raising: " + content))


def rounds(mind, clock, source, count, start=0):
    """Revisions that do not name the erased source, so the state keeps moving past it."""
    for index in range(start, start + count):
        wish(mind, clock, [source(f"other-{index}")], f"other-{index}", f"Unrelated finding {index}")


def texts_everywhere(engine, needle):
    """Every table and column of the store that still holds `needle`, as (table, column)."""
    found = set()
    with sqlite3.connect(engine.db.path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            try:
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")]
                for column in columns:
                    if conn.execute(f"SELECT 1 FROM '{table}' WHERE instr(CAST(\"{column}\" AS TEXT),?)>0 LIMIT 1",
                                    (needle,)).fetchone():
                        found.add((table, column))
            except sqlite3.DatabaseError:
                continue
    return found


def sweep(mind):
    with mind.engine.db.connect() as conn:
        return {row["revision"]: row["verdict"] for row in history.sweep(conn, mind.scope.key())}


def test_delete_takes_the_words_out_of_every_layer_the_mind_built(system):
    mind, memory, source, clock = system
    secret = source("secret", f"The owner said {MARKER} in confidence.")
    other = source("other", "An unrelated observation that stays.")
    with mind.engine.db.connect(write=True) as conn:
        graph = memory.graph
        proof, kept = graph.proof(conn, [secret]), graph.proof(conn, [other])
        node = graph._put(conn, {"id": "secret-event", "kind": "event", "title": f"Talk about {MARKER}",
                                 "text": f"They discussed {MARKER}.", "source_ids": [secret], "evidence": proof,
                                 "basis": "documented"})
        both = graph._put(conn, {"id": "shared-event", "kind": "event", "title": "A shared evening",
                                 "text": "Two sources describe it.", "source_ids": [secret, other],
                                 "evidence": proof + kept, "basis": "documented"})
        graph.link(conn, both["id"], "related", node["id"], proof + kept, reason=f"Both mention {MARKER}")
        # A compressed context row that summarised the node, as the context layer writes them.
        conn.execute("INSERT INTO mind_context_cache VALUES(?,?,?,?)",
                     ("cached-summary", mind.scope.key(),
                      json.dumps({"text": f"Summary: {MARKER}", "items": [{"id": node["id"]}]}), mind.clock()))
        conn.execute("INSERT INTO mind_context_cache VALUES(?,?,?,?)",
                     ("unrelated-summary", mind.scope.key(), json.dumps({"text": "Nothing erased here"}), mind.clock()))
    rounds(mind, clock, source, 2)
    created = wish(mind, clock, [secret], "secret-wish", f"Ask about {MARKER}")
    rounds(mind, clock, source, 3, start=2)
    assert texts_everywhere(mind.engine, MARKER)

    preview = deletion_preview(mind.engine, secret)
    mind.engine.delete(secret)
    settle(mind.engine)

    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect() as conn:
        states = {row["id"]: row["state"] for row in conn.execute("SELECT id,state FROM mind_graph_nodes")}
        remaining = conn.execute("SELECT id FROM mind_context_cache").fetchall()
        shared = json.loads(conn.execute("SELECT data FROM mind_graph_nodes WHERE id='shared-event'").fetchone()[0])
    assert states["secret-event"] == "deleted"
    # A node that still stands on other evidence keeps its words and loses only the erased refs.
    assert states["shared-event"] != "deleted" and shared["title"] == "A shared evening"
    assert all(ref.get("source_id") != secret for ref in shared["evidence"])
    assert [row[0] for row in remaining] == ["unrelated-summary"]
    # The wish is still there as an entry that needs review: its words are gone, not its identity.
    desire = next(d for d in mind.read()["desires"] if d["id"] == created["desire_id"])
    assert desire["content"] == erasure.ERASED
    # Every revision of the state still rebuilds and verifies.
    verdicts = sweep(mind)
    assert verdicts and "failed" not in verdicts.values()
    view = mind.read(history=100)["history"]
    assert view and not any(entry.get("history_error") for entry in view)
    assert preview["source_ids"] == [secret]


def test_the_patch_chain_is_hashed_again_after_an_erase(system):
    mind, memory, source, clock = system
    memory.configure({"history_patches": True})
    secret = source("secret", f"Private {MARKER}")
    rounds(mind, clock, source, 3)
    wish(mind, clock, [secret], "secret-wish", f"Remember {MARKER}")
    rounds(mind, clock, source, 6, start=3)
    with mind.engine.db.connect() as conn:
        before = {row["revision"]: json.loads(row["data"]) for row in conn.execute(
            "SELECT revision,data FROM mind_events WHERE scope=?", (mind.scope.key(),))}
    assert any(history.is_patch(data) for data in before.values())

    mind.engine.delete(secret)
    settle(mind.engine)

    assert texts_everywhere(mind.engine, MARKER) == set()
    verdicts = sweep(mind)
    assert set(verdicts.values()) <= {"verified", "unverifiable"}
    with mind.engine.db.connect() as conn:
        for revision in verdicts:
            history.materialize(conn, mind.scope.key(), revision)
        progress = json.loads(conn.execute("SELECT data FROM mind_memory_migrations WHERE name=?",
                                           (erasure.HISTORY_MIGRATION,)).fetchone()[0])
    assert progress["passes"] == [] and progress["rewritten"] > 0 and progress["broken"] == 0
    # New revisions go on writing patches on top of the rewritten chain.
    rounds(mind, clock, source, 2, start=20)
    assert "failed" not in sweep(mind).values()


def test_a_second_delete_during_a_rewrite_waits_for_its_own_pass(system):
    mind, memory, source, clock = system
    memory.configure({"history_patches": True})
    first, second = source("first", f"One {MARKER}"), source("second", "Two zetaother")
    wish(mind, clock, [first], "first-wish", f"First {MARKER}")
    rounds(mind, clock, source, 4)
    wish(mind, clock, [second], "second-wish", "Second zetaother")
    rounds(mind, clock, source, 4, start=4)
    mind.engine.delete(first)
    worker = Worker(mind.engine)
    worker.run_once()  # the purge or the plan: one step only, so the first pass is under way
    mind.engine.delete(second)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    assert texts_everywhere(mind.engine, "zetaother") == set()
    assert "failed" not in sweep(mind).values()


def test_the_rewrite_waits_while_compaction_owns_the_rows(system):
    mind, memory, source, clock = system
    secret = source("secret", f"Private {MARKER}")
    wish(mind, clock, [secret], "secret-wish", f"Remember {MARKER}")
    mind.engine.delete(secret)
    history_compaction._set_marker(mind.engine.db, True)
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        waiting = conn.execute("SELECT state,attempts,error FROM jobs WHERE kind='erase_history'"
                               " AND json_extract(payload,'$.run')=1").fetchall()
        held = conn.execute("SELECT COUNT(*) FROM mind_events WHERE instr(data,?)>0", (MARKER,)).fetchone()[0]
    assert held and waiting and all(row["state"] == "retry" and row["attempts"] == 0 for row in waiting)
    assert waiting[0]["error"] == history.COMPACTING
    history_compaction._set_marker(mind.engine.db, False)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET available=0 WHERE kind='erase_history'")
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_restoring_an_archive_that_holds_the_original_keeps_the_delete(system, monkeypatch):
    """The drill: create, derive, compact (the archive now holds the original words), delete,
    restore from that archive — the delete is still in force and the rest of the history
    rebuilds from what came back."""
    mind, memory, source, clock = system
    secret = source("secret", f"Private {MARKER}")
    rounds(mind, clock, source, 2)
    wish(mind, clock, [secret], "secret-wish", f"Remember {MARKER}")
    rounds(mind, clock, source, 5, start=2)
    monkeypatch.setattr(history_compaction, "_quiet_check",
                        lambda mind, config: {"check": "quiescence", "ready": True, "reason": "store-is-quiet"})
    compacted = history_compaction.compact(mind, {}, apply=True)
    assert compacted["state"] == "complete" and compacted["patched"] > 0
    archive = history_compaction.archive_dir(mind.engine.db) / compacted["archive"]
    with sqlite3.connect(archive) as conn:
        originals = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT revision,data FROM mind_events_v1")}
        assert conn.execute("SELECT COUNT(*) FROM mind_events_v1 WHERE instr(data,?)>0", (MARKER,)).fetchone()[0]

    mind.engine.delete(secret)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    assert history_compaction.compact_verify(mind)["state"] == "verified"

    dry = history_compaction.restore(mind, {}, apply=False)
    assert dry["erased_rows"] > 0
    restored = history_compaction.restore(mind, {}, apply=True)
    assert restored["state"] == "restored" and restored["erased_rows"] > 0

    # The delete is still in force; the archive still holds the originals it was taken with.
    assert texts_everywhere(mind.engine, MARKER) == set()
    with sqlite3.connect(archive) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_events_v1 WHERE instr(data,?)>0", (MARKER,)).fetchone()[0]
    # The rest of the history is back as whole documents and rebuilds, verified, to exactly the
    # originals with the erased words taken out.
    with mind.engine.db.connect() as conn:
        erased = erasure.erased_ids(conn)
        for revision, data in originals.items():
            want = erasure.scrub(history.snapshot_of(data), erased)
            assert history.canonical(history.materialize(conn, mind.scope.key(), revision)) == history.canonical(want)
    assert "failed" not in sweep(mind).values()
    rounds(mind, clock, source, 2, start=30)
    assert "failed" not in sweep(mind).values()


def test_restore_refuses_an_archive_row_that_does_not_match_its_digest(system, monkeypatch):
    mind, memory, source, clock = system
    rounds(mind, clock, source, 4)
    monkeypatch.setattr(history_compaction, "_quiet_check",
                        lambda mind, config: {"check": "quiescence", "ready": True, "reason": "store-is-quiet"})
    compacted = history_compaction.compact(mind, {}, apply=True)
    archive = history_compaction.archive_dir(mind.engine.db) / compacted["archive"]
    with sqlite3.connect(archive) as conn:
        revision, data = conn.execute("SELECT revision,data FROM mind_events_v1 ORDER BY revision DESC LIMIT 1").fetchone()
        conn.execute("UPDATE mind_events_v1 SET data=? WHERE revision=?", (data.replace("Unrelated", "Tampered"), revision))
    with mind.engine.db.connect() as conn:
        before = conn.execute("SELECT revision,data FROM mind_events ORDER BY revision").fetchall()
    with pytest.raises(Exception) as refused:
        history_compaction.restore(mind, {}, apply=True)
    assert getattr(refused.value, "target", None) == "archive-row-differs-from-its-digest"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT revision,data FROM mind_events ORDER BY revision").fetchall() == before


def test_a_resumed_compaction_uses_the_backup_it_started_against(system, monkeypatch, tmp_path):
    mind, memory, source, clock = system
    rounds(mind, clock, source, 6)
    monkeypatch.setattr(history_compaction, "_quiet_check",
                        lambda mind, config: {"check": "quiescence", "ready": True, "reason": "store-is-quiet"})
    outside = tmp_path / "elsewhere" / "before-compaction.sqlite3"
    outside.parent.mkdir()
    history_compaction._take_backup(mind.engine.db, outside)
    stopped = history_compaction.compact(mind, {}, apply=True, backup=str(outside), limit=3)
    assert stopped["state"] == "stopped"
    taken = []
    monkeypatch.setattr(history_compaction, "_take_backup", lambda db, path: taken.append(path))
    dry = history_compaction.compact(mind, {}, apply=False)
    assert dry["backup"] == outside.name and dry["ready"]
    finished = history_compaction.compact(mind, {}, apply=True)
    assert finished["state"] == "complete" and not taken


def test_deleting_a_derived_summary_leaves_the_sources_it_cites(system):
    mind, memory, source, clock = system
    engine = mind.engine
    first, second = source("first", "First owner message"), source("second", "Second owner message")
    roots = ["mem_" + __import__("eventmem.core.db", fromlist=["digest"]).digest([sid, "root"])[:32]
             for sid in (first, second)]
    summary = engine.add_record(RecordInput(kind="summary", content="A summary of both messages", scope=mind.scope,
                                            source_ids=[first, second], evidence_ids=roots, generated=True,
                                            confirmation="inferred"), "summary-of-both")
    preview = deletion_preview(engine, summary["id"])
    assert preview["source_ids"] == [] and preview["record_ids"] == [summary["id"]]
    engine.delete(summary["id"])
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE id IN (?,?)", (first, second)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM records WHERE id IN (?,?)", tuple(roots)).fetchone()[0] == 2
    # Deleting one original takes its own source and everything built on it, and nothing else.
    again = engine.add_record(RecordInput(kind="summary", content="Another summary", scope=mind.scope,
                                          source_ids=[first, second], evidence_ids=roots, generated=True,
                                          confirmation="inferred"), "summary-again")
    engine.delete(roots[0])
    with engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE id=?", (first,)).fetchone()
        assert conn.execute("SELECT 1 FROM sources WHERE id=?", (second,)).fetchone()
        assert not conn.execute("SELECT 1 FROM records WHERE id=?", (again["id"],)).fetchone()


def test_delete_leaves_receipts_sessions_and_metrics_that_name_nothing_erased(system):
    mind, memory, source, clock = system
    engine = mind.engine
    secret = source("secret", f"Private {MARKER}")
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO commands VALUES('unrelated-command','d','{\"ok\":true}')")
        conn.execute("INSERT INTO commands VALUES('secret-command','d',?)", (json.dumps({"id": secret}),))
        conn.execute("INSERT INTO sessions VALUES('unrelated-session','s','{}')")
        conn.execute("INSERT INTO sessions VALUES('secret-session','s',?)", (json.dumps({"ids": [secret]}),))
        conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES('unrelated_metric',1,'t','{}')")
    engine.delete(secret)
    with engine.db.connect() as conn:
        commands = {row[0] for row in conn.execute("SELECT id FROM commands")}
        sessions = {row[0] for row in conn.execute("SELECT id FROM sessions")}
        metrics = {row[0] for row in conn.execute("SELECT name FROM metrics")}
    assert "unrelated-command" in commands and "secret-command" not in commands
    assert sessions == {"unrelated-session"}
    assert "unrelated_metric" in metrics and "memory_erased" in metrics


def test_a_context_rendered_from_erased_words_keeps_its_identity_and_is_never_sent(system):
    """CR-MEM-02: a delivery names what it rests on as items[].id and dependencies[].id, and a
    window keeps receipts with index[].id and rendered_text. The erase finds them by what they
    really name, takes the words out, and leaves id, hash and state for reconciliation."""
    from kin_mind.context_delivery import ContextDelivery

    mind, memory, source, clock = system
    secret = source("secret", f"The owner said {MARKER} about the harbour walk.")
    contexts = Contexts(mind)
    packed = contexts.build("harbour walk", purpose="chat", session="thread-1", event_id="turn-2", receipt_mode=True)
    contexts.build("harbour walk", purpose="chat", session="thread-1", event_id="turn-1")
    injection = packed["injection"]
    assert injection["state"] == "prepared" and MARKER in injection["text"]
    tables = {table for table, _ in texts_everywhere(mind.engine, MARKER)}
    assert {"mind_context_deliveries", "mind_context_windows"} <= tables

    mind.engine.delete(secret)
    settle(mind.engine)

    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect() as conn:
        stored = json.loads(conn.execute("SELECT data FROM mind_context_deliveries WHERE id=?",
                                         (injection["id"],)).fetchone()[0])
        window = json.loads(conn.execute("SELECT data FROM mind_context_windows WHERE session='thread-1'").fetchone()[0])
    assert (stored["id"], stored["marker"], stored["text_hash"], stored["state"]) == \
        (injection["id"], injection["marker"], injection["text_hash"], "prepared")
    assert stored["text"] == erasure.ERASED and stored["erased_at"] and all("text" not in i for i in stored["items"])
    begun = ContextDelivery(contexts).begin("thread-1", injection["epoch"], injection["id"])
    assert begun["state"] == "erased" and not begun.get("acquired")
    receipt = window["receipts"]["turn-1"]
    assert receipt["erased_at"] and receipt["rendered_text"] == erasure.ERASED
    assert not contexts._receipt_current(receipt, None)
    # The same turn asked again is rendered afresh, from what is left.
    again = contexts.build("harbour walk", purpose="chat", session="thread-1", event_id="turn-1")
    assert MARKER not in json.dumps(again, ensure_ascii=False)
