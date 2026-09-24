"""The background worker and its queue: parsing model replies, what a failure costs a job, a loop
that outlives its own failures, host receipts that cannot be replayed, and the graph index that
an update no longer scans (E1-01, E2-02, E2-04, E2-05, E2-12, E1-02, E3-17, K4-04, K4-10)."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

import httpx
import pytest

from eventmem.core import Engine
from eventmem.core import jobs as jobs_module
from eventmem.core.db import digest
from eventmem.core.jobs import Worker
from eventmem.core.models import Scope, SourceInput
from eventmem.core.providers import NotConfigured, ParseError, Providers, json_object

from kin_mind import graph as graph_module
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "db")


def receive(engine, key, text, extract=False):
    return engine.receive(SourceInput(namespace="synthetic", key=key, text=text, scope=Scope(persona="queue"),
                                      authority="explicit", occurred_at=datetime.now(timezone.utc).isoformat(),
                                      extract=extract))["id"]


def job(engine, jid):
    with engine.db.connect() as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def test_a_reply_is_read_as_the_one_object_it_carries():
    inner = {"candidates": [{"quote": "```code```", "content": "A fenced example"}]}
    fenced = "```json\n" + json.dumps(inner) + "\n```"
    assert json_object(fenced) == inner
    assert json_object("Here it is:\n" + fenced) == inner
    assert json_object('An example {"kind": "x"} first, then the answer:\n' + json.dumps(inner)) == inner
    assert json_object(json.dumps(inner)) == inner
    for broken in ("[1, 2]", "no object here", json.dumps(inner) + " and some prose after it"):
        with pytest.raises(ParseError):
            json_object(broken)


def test_only_an_unreadable_reply_is_asked_again(engine, monkeypatch):
    calls = []

    def unreadable(self, *args, **kwargs):
        calls.append(1)
        raise ParseError("not an object")

    monkeypatch.setattr(Providers, "_json_once", unreadable)
    with pytest.raises(ValueError, match="no valid structured result"):
        Providers(engine).json("extraction", "x", {})
    assert len(calls) == 2
    calls.clear()

    def unreachable(self, *args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(Providers, "_json_once", unreachable)
    with pytest.raises(httpx.ReadTimeout):
        Providers(engine).json("extraction", "x", {})
    assert len(calls) == 1


class Extraction:
    """A model that answers the extraction role only, and counts its calls."""
    calls = []
    answer = {}

    def __init__(self, engine):
        pass

    def json(self, role, instruction, payload, image=None):
        if role != "extraction":
            raise NotConfigured("only extraction is configured in this test")
        Extraction.calls.append(payload["text"])
        return Extraction.answer


def settle(engine, limit=100):
    worker = Worker(engine)
    for _ in range(limit):
        if not worker.run_once():
            return worker
    raise AssertionError("the worker did not settle")


def test_extraction_keeps_every_good_candidate_and_only_descriptive_attributes(engine, monkeypatch):
    text = "The owner prefers tea in the morning. The owner walks every evening."
    Extraction.calls, Extraction.answer = [], {"candidates": [
        {"kind": "preference", "content": "Prefers tea in the morning", "quote": "prefers tea in the morning",
         "attributes": {"topic": "drinks", "origin_kind": "configuration", "constraint": "always", "completed": True}},
        {"content": "No kind given", "quote": "walks every evening"},
        {"kind": "fact", "content": "Invented", "quote": "a quote that is not in the text"},
        "not even an object",
        {"kind": "habit", "content": "An unknown kind", "quote": "walks every evening"},
        {"kind": "episode", "content": "Walks every evening", "quote": "walks every evening", "attributes": ["x"]},
    ]}
    monkeypatch.setattr(jobs_module, "Providers", Extraction)
    sid = receive(engine, "tea", text, extract=True)
    settle(engine)
    with engine.db.connect() as conn:
        records = [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM records WHERE json_extract(data,'$.generated')=1")]
        state = conn.execute("SELECT model FROM sources WHERE id=?", (sid,)).fetchone()[0]
        dropped = conn.execute("SELECT SUM(value) FROM metrics WHERE name='extraction_candidates_dropped'").fetchone()[0]
    assert sorted(r["content"] for r in records) == ["Prefers tea in the morning", "Walks every evening"]
    assert {k: v for r in records for k, v in r["attributes"].items()} == {"topic": "drinks"}
    assert state == "complete" and dropped == 2 and len(Extraction.calls) == 1


def test_an_empty_source_is_extracted_without_a_model_call(engine, monkeypatch):
    Extraction.calls, Extraction.answer = [], {"candidates": []}
    monkeypatch.setattr(jobs_module, "Providers", Extraction)
    sid = receive(engine, "blank", "   \n  ", extract=True)
    settle(engine)
    with engine.db.connect() as conn:
        assert conn.execute("SELECT model FROM sources WHERE id=?", (sid,)).fetchone()[0] == "complete"
    assert Extraction.calls == []


def test_a_part_that_fails_for_good_leaves_its_source_failed_not_pending(engine):
    sid = receive(engine, "long", "Some text")
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET model='pending' WHERE id=?", (sid,))
        row = {"kind": "extract_part", "payload": json.dumps({"source_id": sid, "text": "Some", "part": 1})}
        Worker.settled(conn, row, "failed", "ValueError")
    with engine.db.connect() as conn:
        assert conn.execute("SELECT model FROM sources WHERE id=?", (sid,)).fetchone()[0] == "failed"


def test_an_environment_that_is_down_costs_a_job_no_attempt(engine, monkeypatch):
    worker = Worker(engine)
    parse = engine.enqueue("parse", {"source_id": "src_missing"}, "parse-env")
    summary = engine.enqueue("summary", {"scope": {"persona": "queue"}}, "summary-env")

    def down(self, job):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(Worker, "prepare", down)
    while worker.run_once():
        pass
    for jid in (parse, summary):
        found = job(engine, jid)
        assert found["state"] == "retry" and found["attempts"] == 0, found
    assert {"parse", "summary"} <= set(worker.paused)

    def slow(self, job):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(Worker, "prepare", slow)
    worker.paused.clear()
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET available=0")
    while worker.run_once():
        pass
    # A read timeout may have been billed: it counts for a job that calls a model, not for one
    # that calls none.
    assert job(engine, summary)["attempts"] == 1 and job(engine, parse)["attempts"] == 0


def test_the_loop_outlives_a_failing_step_and_says_so(engine, monkeypatch):
    worker = Worker(engine)
    monkeypatch.setattr(jobs_module, "LOOP_BACKOFF", 0)

    def locked():
        raise sqlite3.OperationalError("database is locked")

    rounds = []

    def maintenance():
        rounds.append(1)
        if len(rounds) >= 2:
            worker.stopped.set()

    worker.replay_hosts = locked
    worker.schedule_maintenance = maintenance
    runner = threading.Thread(target=worker.run)
    runner.start()
    runner.join(timeout=20)
    assert not runner.is_alive()
    assert len(rounds) == 2 and worker.failures == 2
    assert worker.last_error["step"] == "replay_hosts" and worker.last_error["error"] == "OperationalError"
    assert worker.last_tick is not None
    assert worker.health()["alive"] is False  # stopped on purpose
    worker.stopped.clear()
    assert worker.health()["alive"] is True


def test_host_receipts_that_cannot_be_replayed_are_moved_aside(engine, monkeypatch):
    spool = engine.db.root / "host-spool"
    spool.mkdir()
    (spool / "broken.json").write_text("{not json")
    (spool / "busy.json").write_text(json.dumps({"event": "message", "payload": {}}))
    calls = []

    def busy(engine, event, payload, receipt_only=False):
        calls.append(event)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("eventmem.core.hosts.handle", busy)
    worker = Worker(engine)
    worker.replay_hosts()
    assert (spool / "rejected" / "broken.json").exists() and not (spool / "broken.json").exists()
    assert (spool / "busy.json").exists() and worker.spool_failures["busy.json"][0] == 1
    for attempt in range(jobs_module.SPOOL_ATTEMPTS):
        name = "busy.json"
        if name in worker.spool_failures:
            worker.spool_failures[name] = (worker.spool_failures[name][0], 0)
        worker.replay_hosts()
    assert (spool / "rejected" / "busy.json").exists() and not (spool / "busy.json").exists()
    assert len(calls) == jobs_module.SPOOL_ATTEMPTS
    with engine.db.connect() as conn:
        reasons = [json.loads(row[0])["reason"] for row in conn.execute(
            "SELECT data FROM metrics WHERE name='host_spool_rejected' ORDER BY id")]
    assert reasons == ["JSONDecodeError", "OperationalError"]


def test_a_graph_node_keeps_one_index_row_under_its_own_rowid(tmp_path):
    engine = Engine(tmp_path / "graph")
    mind = Mind(engine, Scope(persona="graph-index"), clock=lambda: datetime.now(timezone.utc).isoformat())
    memory = MemoryContinuity(mind)
    sid = receive(engine, "seen", "Observed")
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET scope=? WHERE id=?", (mind.scope.key(), sid))
        conn.execute("UPDATE records SET scope=?,data=json_set(data,'$.scope',json(?)) WHERE id=?",
                     (mind.scope.key(), json.dumps(mind.scope.model_dump()), "mem_" + digest([sid, "root"])[:32]))
        refs = memory.graph.proof(conn, [sid])
        for title in ("First title", "Second title", "Third title"):
            memory.graph._put(conn, {"id": "indexed", "kind": "event", "title": title, "text": "",
                                     "source_ids": [sid], "evidence": refs, "basis": "documented"})
        rowid = conn.execute("SELECT rowid FROM mind_graph_nodes WHERE id='indexed'").fetchone()[0]
        rows = conn.execute("SELECT rowid,tokens FROM mind_graph_search WHERE id='indexed'").fetchall()
        assert conn.execute("SELECT 1 FROM meta WHERE key=?", (graph_module.SEARCH_ALIGNED,)).fetchone()
    assert [row[0] for row in rows] == [rowid] and "third" in rows[0][1]
