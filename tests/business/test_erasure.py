"""An explicit delete reaches every layer the mind built from what it deleted, and stays in force
when an old history archive is put back (K4-01, K4-20, K4-21, E2-06, K3-15). A model answer that
comes back after the delete, for a compression or a judgment that was waiting on it, keeps nothing
but its cost; nor does one that comes back after its source was revised (CR3-MM-03). Nor does a
context's own receipt, prepared injection or window receipt, whichever way the delete lands
(CR4-MM-01)."""
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


def test_a_history_read_is_scrubbed_before_the_rewrite_has_run(system):
    """CR-MEM-03: the stored rows are rewritten by a job, which may wait behind compaction or a
    stopped worker; a read of the history meets the delete at once."""
    mind, memory, source, clock = system
    secret = source("secret", f"Private {MARKER}")
    wish(mind, clock, [secret], "secret-wish", f"Remember {MARKER}")
    rounds(mind, clock, source, 2)
    mind.engine.delete(secret)
    history_compaction._set_marker(mind.engine.db, True)  # the rewrite now waits
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_events WHERE instr(data,?)>0", (MARKER,)).fetchone()[0]
    view = mind.read(history=100)["history"]
    assert len(view) >= 3 and MARKER not in json.dumps(view, ensure_ascii=False)
    assert any(erasure.ERASED in json.dumps(entry["request"], ensure_ascii=False) for entry in view)


def every_row(engine):
    """Every row of every table of the store, to prove a run wrote nothing at all."""
    with sqlite3.connect(engine.db.path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: sorted(map(repr, conn.execute(f"SELECT * FROM '{table}'").fetchall())) for table in tables}


def test_a_second_reerase_changes_nothing_and_a_rerun_reuses_the_history_under_way(system, monkeypatch):
    from eventmem.core import repair

    mind, memory, source, clock = system
    engine = mind.engine
    secret, other = source("secret", f"The owner said {MARKER} in confidence."), source("other", "It stays.")
    with engine.db.connect(write=True) as conn:
        graph = memory.graph
        proof, kept = graph.proof(conn, [secret]), graph.proof(conn, [other])
        graph._put(conn, {"id": "secret-event", "kind": "event", "title": f"Talk about {MARKER}",
                          "text": f"They discussed {MARKER}.", "source_ids": [secret], "evidence": proof,
                          "basis": "documented"})
        graph._put(conn, {"id": "shared-event", "kind": "event", "title": "A shared evening",
                          "text": "Two sources describe it.", "source_ids": [secret, other],
                          "evidence": proof + kept, "basis": "documented"})
    rounds(mind, clock, source, 2)
    wish(mind, clock, [secret], "secret-wish", f"Ask about {MARKER}")
    rounds(mind, clock, source, 2, start=2)
    # A delete as the release before this one made it: the source, its records and their
    # tombstones, and nothing of what the mind built from them.
    monkeypatch.setattr(erasure, "erase", lambda *args, **kwargs: {})
    monkeypatch.setattr(erasure, "queue_history", lambda *args, **kwargs: None)
    engine.delete(secret)
    monkeypatch.undo()
    settle(engine)
    assert texts_everywhere(engine, MARKER)

    plan = repair.run(engine.db.root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert plan["layers"]["graph"] == 2 and plan["history_rows"] > 0 and plan["history_ids"] == plan["tombstones"]
    first = repair.run(engine.db.root, apply=True, steps=("reerase",))["steps"]["reerase"]["done"]
    assert first["layers"]["graph"] == 2 and first["history_job"]
    # A rerun before the worker has taken the history up: the layers are done, and the step
    # already queued for the same identifiers is the one it answers with.
    rerun = repair.run(engine.db.root, apply=True, steps=("reerase",))["steps"]["reerase"]["done"]
    assert rerun == {"layers": {}, "history_ids": first["history_ids"], "history_job": first["history_job"]}
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='erase_history' AND json_extract(payload,'$.ids')"
                            " IS NOT NULL AND state!='done'").fetchone()[0] == 1
        node = conn.execute("SELECT revision,updated_at FROM mind_graph_nodes WHERE id='secret-event'").fetchone()
    settle(engine)
    assert texts_everywhere(engine, MARKER) == set()
    assert "failed" not in sweep(mind).values()

    # Done: a second run finds nothing to change, and writes nothing — not a node revision, not
    # a timestamp, not a history job, not a metric.
    before = every_row(engine)
    again = repair.run(engine.db.root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert again["layers"] == {} and again["derived_rows"] == 0
    assert again["history_ids"] == 0 and again["history_rows"] == 0 and again["history_passes_owed"] == 0
    done = repair.run(engine.db.root, apply=True, steps=("reerase",))["steps"]["reerase"]["done"]
    assert done == {"layers": {}, "history_ids": 0, "history_job": None}
    assert every_row(engine) == before
    with engine.db.connect() as conn:
        assert tuple(conn.execute("SELECT revision,updated_at FROM mind_graph_nodes WHERE id='secret-event'")
                     .fetchone()) == tuple(node)


class LateModel:
    """A model whose answer arrives only after `meanwhile` ran: the owner deletes or corrects the
    source while the request is out. Its answer repeats the source's words, as a summary or a
    verdict would."""

    def __init__(self, engine, monkeypatch, meanwhile, answer):
        import httpx

        from kin_mind.appraisal import APPRAISAL_MODEL, DeepSeek

        self.calls, self.meanwhile, self.answer = [], meanwhile, answer
        monkeypatch.setenv("EVENTMEM_API_KEY", "synthetic-key")
        self.provider = DeepSeek("https://api.deepseek.com/anthropic", APPRAISAL_MODEL,
                                 transport=httpx.MockTransport(self.respond))
        self.provider.engine = engine
        self.model = APPRAISAL_MODEL

    def respond(self, request):
        import httpx

        sent = json.loads(request.content)
        asked = json.loads(sent["messages"][0]["content"])
        self.calls.append(sent["tools"][0]["name"])
        self.meanwhile()
        return httpx.Response(200, json={
            "id": f"synthetic-{len(self.calls)}", "model": self.model, "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": sent["tools"][0]["name"], "input": self.answer(asked)}],
            "usage": {"input_tokens": 900, "output_tokens": 30}})


def paid(engine):
    with engine.db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM metrics WHERE name='structured_model_usage'").fetchone()[0]


def test_a_compression_that_comes_back_after_its_source_was_deleted_keeps_nothing(system, monkeypatch):
    """Blocked on the model, the source is deleted; then the answer arrives, repeating its words.
    Neither the part nor the summary is cached, the words are in no table, and the call's cost
    stays on record (CR3-MM-03)."""
    mind, memory, source, clock = system
    engine = mind.engine
    filler = "They checked the harbour path, the tide table and the lamps along the pier. " * 12
    secret = source("secret", f"The owner said {MARKER} about the harbour walk. " + filler)
    others = [source(f"walk-{i}", f"Harbour walk note {i}. " + filler) for i in range(2)]
    contexts = Contexts(mind)
    items = [contexts.record_item(engine.get(engine.source(sid)["record_ids"][0])) for sid in [secret, *others]]
    model = LateModel(engine, monkeypatch, lambda: engine.delete(secret), lambda asked: {
        "entries": [{"item_ids": asked["allowed_item_ids"], "summary": f"They walked the harbour; {MARKER}."}],
        "omitted_ids": []})

    packed = contexts.pack(items, "harbour walk", 600, provider=model.provider, allow_model=True, persist=True)
    settle(engine)

    assert model.calls == ["submit_compression"]
    assert packed["state"] == "needs-compression" and packed["reason"] == "Conflict"
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_context_cache").fetchone()[0] == 0
    assert texts_everywhere(engine, MARKER) == set()
    assert paid(engine) == 1  # what the call cost is all it leaves


def test_a_judgment_that_comes_back_after_its_source_changed_keeps_nothing(system, monkeypatch):
    """The same for a judgment: deleted while the verdict is out, no pending row and no words; and
    a verdict about a record that was corrected meanwhile is not kept for the old version either.
    Both calls stay paid for (CR3-MM-03)."""
    from pydantic import BaseModel

    from eventmem.core.models import RevisionInput

    class Verdict(BaseModel):
        verdict: str

    mind, memory, source, clock = system
    engine = mind.engine
    judgment = {"scope": mind.scope.key(), "type": "step-complete", "goal": "walk", "completion": "walked",
                "obligation_version": "1"}
    secret = source("secret", f"The owner said {MARKER} about the harbour walk.")
    record = engine.source(secret)["record_ids"][0]
    model = LateModel(engine, monkeypatch, lambda: engine.delete(secret), lambda asked: {"verdict": f"done: {MARKER}"})
    _, receipt = model.provider.structured("judge_step", Verdict, "Judge the step.",
                                           {"records": [{"id": record, "text": engine.get(record)["content"]}]},
                                           judgment=judgment, depends_on=[record])
    settle(engine)
    assert model.calls == ["judge_step"] and "judgment_cache" not in receipt
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_judgment_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mind_judgment_cache_deps").fetchone()[0] == 0
    assert texts_everywhere(engine, MARKER) == set()

    walk = source("walk", "The owner walked to the harbour.")
    kept = engine.source(walk)["record_ids"][0]
    corrected = RevisionInput(expected_revision=1, command_id="correct-walk", action="correct",
                              content="The owner walked to the station.", reason="synthetic correction")
    model = LateModel(engine, monkeypatch, lambda: engine.revise(kept, corrected), lambda asked: {"verdict": "harbour"})
    _, receipt = model.provider.structured("judge_step", Verdict, "Judge the step.",
                                           {"records": [{"id": kept, "text": "The owner walked to the harbour."}]},
                                           judgment={**judgment, "goal": "harbour"}, depends_on=[kept])
    assert "judgment_cache" not in receipt
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_judgment_cache").fetchone()[0] == 0
    assert paid(engine) == 2

    # Nothing changed while it was out: the verdict is kept, pending, as before.
    model = LateModel(engine, monkeypatch, lambda: None, lambda asked: {"verdict": "station"})
    _, receipt = model.provider.structured("judge_step", Verdict, "Judge the step.",
                                           {"records": [{"id": kept, "text": engine.get(kept)["content"]}]},
                                           judgment={**judgment, "goal": "station"}, depends_on=[kept])
    assert receipt.get("judgment_cache")
    with engine.db.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT accepted FROM mind_judgment_cache")] == [(0,)]


def test_an_overview_of_a_source_deleted_after_its_summary_came_back_is_not_kept(system, monkeypatch):
    """Overviews are written after their pack returns. A source deleted in between gets no overview:
    the words are in no table, and the other source's overview is kept as before (CR3-MM-03)."""
    mind, memory, source, clock = system
    engine = mind.engine
    filler = "They checked the harbour path, the tide table and the lamps along the pier. " * 40
    secret = source("secret", f"The owner said {MARKER} about the harbour walk. " + filler)
    other = source("walk", "Harbour walk note. " + filler)
    contexts = Contexts(mind)
    records = {sid: engine.source(sid)["record_ids"][0] for sid in (secret, other)}
    items = [{**contexts.record_item(engine.get(rid)), "needs_review": False} for rid in records.values()]
    monkeypatch.setattr(contexts.memory, "history",
                        lambda kind, query="", limit=20, **_: {"items": items if kind == "work" else []})
    monkeypatch.setattr(contexts, "node_item", lambda node, policy=None: node)
    model = LateModel(engine, monkeypatch, lambda: None, lambda asked: {
        "entries": [{"item_ids": [i], "summary": f"Harbour walk; {MARKER}." if i == records[secret] else "Harbour walk."}
                    for i in asked["allowed_item_ids"]], "omitted_ids": []})
    real = contexts.pack

    def deleted_on_return(*args, **kwargs):
        packed = real(*args, **kwargs)
        engine.delete(secret)
        return packed

    monkeypatch.setattr(contexts, "pack", deleted_on_return)
    assert contexts.warm("harbour walk", provider=model.provider)["state"] == "compressed"
    settle(engine)
    assert texts_everywhere(engine, MARKER) == set()
    with engine.db.connect() as conn:
        overviews = [json.loads(row[0]) for row in conn.execute("SELECT data FROM mind_context_cache WHERE id LIKE 'overview-v2:%'")]
    assert [view["source"]["id"] for view in overviews] == [records[other]]



def harbour(mind, source, length):
    """Three accounts of the harbour walk. The short one says the marker and ranks first, so it is
    among the originals a context falls back to; the others are `length` times longer."""
    filler = "They checked the harbour path, the tide table and the lamps along the pier. " * length
    secret = source("secret", f"The owner said {MARKER} about the harbour walk.")
    for i in range(2):
        source(f"walk-{i}", f"Harbour walk note {i}. " + filler)
    return secret


def stored_words(engine, session):
    """What the session's prepared injections and window receipts hold."""
    found = []
    with engine.db.connect() as conn:
        for table in ("mind_context_deliveries", "mind_context_windows"):
            present = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
            found.append([row[0] for row in conn.execute(f"SELECT data FROM {table} WHERE session=?", (session,))]
                         if present else [])
    return tuple(found)


@pytest.mark.parametrize("receipt_mode", [True, False])
def test_a_context_whose_source_is_deleted_while_it_is_compressed_keeps_no_words_of_it(system, monkeypatch, receipt_mode):
    """The build waits on the compression; the source is deleted; the answer comes back. The
    compression is not kept, and what the build falls back to is the originals as they are now:
    neither the prepared injection nor the window receipt holds a word of the deleted one
    (CR4-MM-01)."""
    mind, memory, source, clock = system
    engine = mind.engine
    secret = harbour(mind, source, 80)
    contexts = Contexts(mind)
    model = LateModel(engine, monkeypatch, lambda: engine.delete(secret), lambda asked: {
        "entries": [{"item_ids": asked["allowed_item_ids"], "summary": f"They walked the harbour; {MARKER}."}],
        "omitted_ids": []})
    packed = contexts.build("harbour walk", purpose="chat", session="thread-1", event_id="turn-1",
                            receipt_mode=receipt_mode, allow_model=True, provider=model.provider)
    settle(engine)
    assert model.calls and packed["reason"] == "Conflict"
    assert MARKER not in json.dumps(packed, ensure_ascii=False)
    deliveries, windows = stored_words(engine, "thread-1")
    assert (deliveries if receipt_mode else windows)  # the context was kept, from what is left
    assert texts_everywhere(engine, MARKER) == set()


@pytest.mark.parametrize("receipt_mode", [True, False])
def test_a_context_whose_source_is_deleted_before_its_receipt_is_written_keeps_nothing(system, monkeypatch, receipt_mode):
    """The build has its text; the source is deleted before the receipt is written. The write
    checks its sources and refuses: nothing is kept, and the caller is told to build again
    (CR4-MM-01)."""
    from eventmem.core.db import Conflict

    mind, memory, source, clock = system
    engine = mind.engine
    secret = harbour(mind, source, 1)
    contexts = Contexts(mind)
    real = contexts.pack

    def deleted_on_return(*args, **kwargs):
        packed = real(*args, **kwargs)
        assert MARKER in packed["text"]
        engine.delete(secret)
        return packed

    monkeypatch.setattr(contexts, "pack", deleted_on_return)
    with pytest.raises(Conflict, match="sources changed"):
        contexts.build("harbour walk", purpose="chat", session="thread-1", event_id="turn-1", receipt_mode=receipt_mode)
    settle(engine)
    assert stored_words(engine, "thread-1") == ([], [])
    assert texts_everywhere(engine, MARKER) == set()


def test_a_prepared_context_found_stale_after_a_delete_loses_its_words(system):
    """One an older build prepared after the delete had run: begin finds it stale, takes the
    deleted words out by the delete's own rule and keeps its identity for reconciliation
    (CR-MEM-02, CR4-MM-01)."""
    from kin_mind.context_delivery import ContextDelivery, text_hash

    mind, memory, source, clock = system
    engine = mind.engine
    secret = source("secret", f"The owner said {MARKER} about the harbour walk.")
    contexts = Contexts(mind)
    item = contexts.record_item(engine.get(engine.source(secret)["record_ids"][0]))
    engine.delete(secret)
    settle(engine)
    epoch = contexts.window("thread-1")["epoch"]
    body = "kin-context:context:late\n" + item["text"]
    late = {"id": "context:late", "session": "thread-1", "epoch": epoch, "event_id": "turn-1", "state": "prepared",
            "kind": "background", "marker": "kin-context:context:late", "text": body, "text_hash": text_hash(body),
            "tokens": 40, "content_tokens": 40, "overhead": 0, "items": [item], "prepared_at": mind.clock()}
    delivery = ContextDelivery(contexts)
    with engine.db.connect(write=True) as conn:
        delivery._put(conn, late)
    assert MARKER in json.dumps(stored_words(engine, "thread-1"), ensure_ascii=False)
    begun = delivery.begin("thread-1", epoch, "context:late")
    assert begun["state"] == "erased" and not begun.get("acquired")
    assert texts_everywhere(engine, MARKER) == set()
    with engine.db.connect() as conn:
        kept = json.loads(conn.execute("SELECT data FROM mind_context_deliveries WHERE id='context:late'").fetchone()[0])
    assert (kept["id"], kept["marker"], kept["text_hash"], kept["state"]) == ("context:late", late["marker"], late["text_hash"], "stale")
    assert kept["text"] == erasure.ERASED and kept["erased_at"]
