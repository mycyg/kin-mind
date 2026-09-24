"""The background worker and its queue: parsing model replies, what a failure costs a job, a loop
that outlives its own failures, host receipts that cannot be replayed, and the graph index that
an update no longer scans (E1-01, E2-02, E2-04, E2-05, E2-12, E1-02, E3-17, K4-04, K4-10). A paid
result is not thrown away because a lease lapsed, and one whose job was taken over is kept for
the run that took it (CR-MEM-09) - never for a job a delete ended while the model answered
(CR2-MEM-01)."""
import json
import sqlite3
import threading
import time
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


class PaidModel:
    """The extraction role over HTTP, answered by a mock transport that counts the calls it was
    paid for and can act on the queue while it answers, as the world does while a model thinks."""

    def __init__(self, engine, monkeypatch, *, during=None):
        self.engine, self.calls, self.during = engine, 0, during
        engine.settings("models", {"extraction": {"endpoint": "https://synthetic.invalid/v1", "model": "synthetic-model",
                                                  "input_price_per_million": 1, "output_price_per_million": 2}})
        real = httpx.Client

        def client(*args, **kwargs):
            return real(*args, **{**kwargs, "transport": httpx.MockTransport(self.answer)})

        monkeypatch.setattr(httpx, "Client", client)

    def answer(self, request):
        self.calls += 1
        if self.during:
            self.during(self.engine)
        text = json.loads(request.content)["messages"][1]["content"]
        quote = "prefers tea in the morning" if "prefers tea" in text else "walks every evening"
        body = {"candidates": [{"kind": "preference", "content": "Tea in the morning", "quote": quote}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def running_extract(engine):
    with engine.db.connect() as conn:
        return conn.execute("SELECT id FROM jobs WHERE kind='extract' AND state='running'").fetchone()[0]


def lapse(engine):
    """The machine slept through the lease while the model answered, and nobody took the job."""
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 5, running_extract(engine)))


def metrics(engine, name):
    with engine.db.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT data FROM metrics WHERE name=?", (name,))]


def test_a_result_whose_lease_lapsed_unclaimed_is_committed_not_paid_for_again(engine, monkeypatch):
    model = PaidModel(engine, monkeypatch, during=lapse)
    sid = receive(engine, "tea", "The owner prefers tea in the morning.", extract=True)
    settle(engine)
    with engine.db.connect() as conn:
        extracted = conn.execute("SELECT COUNT(*) FROM records WHERE json_extract(data,'$.generated')=1").fetchone()[0]
        state = conn.execute("SELECT model FROM sources WHERE id=?", (sid,)).fetchone()[0]
        extract = dict(conn.execute("SELECT state,attempts FROM jobs WHERE kind='extract'").fetchone())
    assert model.calls == 1 and extracted == 1 and state == "complete"
    assert extract == {"state": "complete", "attempts": 1}
    assert len(metrics(engine, "job_lease_late_commit")) == 1 and not metrics(engine, "job_result_discarded")
    assert len(metrics(engine, "model_cost")) == 1


def test_a_result_taken_over_is_kept_for_the_run_that_took_the_job(engine, monkeypatch):
    def taken(engine):
        # Another run claimed the job while this one waited for its answer.
        with engine.db.connect(write=True) as conn:
            conn.execute("UPDATE jobs SET owner='another-run',fence=fence+1,lease_until=? WHERE id=?",
                         (time.time() + 90, running_extract(engine)))

    model = PaidModel(engine, monkeypatch, during=taken)
    sid = receive(engine, "tea", "The owner prefers tea in the morning.", extract=True)
    worker = Worker(engine)
    while worker.run_once():
        pass
    with engine.db.connect() as conn:
        kept = conn.execute("SELECT id,result FROM commands WHERE id LIKE 'job-answer:%'").fetchall()
        assert not conn.execute("SELECT 1 FROM records WHERE json_extract(data,'$.generated')=1").fetchone()
    assert model.calls == 1 and len(kept) == 1 and sid in kept[0]["result"]
    assert metrics(engine, "job_result_discarded")[0]["answers_kept"] == 1
    # The run that took it over lets its lease lapse in turn; this worker claims the job again and
    # is answered by what was already paid for, since it asks exactly the same.
    model.during = None
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET lease_until=? WHERE kind='extract'", (time.time() - 5,))
    while worker.run_once():
        pass
    with engine.db.connect() as conn:
        extracted = conn.execute("SELECT COUNT(*) FROM records WHERE json_extract(data,'$.generated')=1").fetchone()[0]
        left = conn.execute("SELECT COUNT(*) FROM commands WHERE id LIKE 'job-answer:%'").fetchone()[0]
        state = conn.execute("SELECT model FROM sources WHERE id=?", (sid,)).fetchone()[0]
    assert model.calls == 1 and extracted == 1 and state == "complete" and left == 0
    assert len(metrics(engine, "model_answer_reused")) == 1 and len(metrics(engine, "model_cost")) == 1


def test_a_kept_answer_is_used_only_for_the_very_same_request(engine, monkeypatch):
    def taken(engine):
        with engine.db.connect(write=True) as conn:
            conn.execute("UPDATE jobs SET owner='another-run',fence=fence+1,lease_until=? WHERE id=?",
                         (time.time() + 90, running_extract(engine)))

    model = PaidModel(engine, monkeypatch, during=taken)
    receive(engine, "tea", "The owner prefers tea in the morning.", extract=True)
    worker = Worker(engine)
    while worker.run_once():
        pass
    # What the job asks has changed since (here, the model the role names): the kept answer
    # does not stand for the new request, which is asked and paid for.
    model.during = None
    engine.settings("models", {"extraction": {**engine.settings("models")["extraction"], "model": "synthetic-model-2"}})
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET lease_until=? WHERE kind='extract'", (time.time() - 5,))
    while worker.run_once():
        pass
    assert model.calls == 2 and not metrics(engine, "model_answer_reused")
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM commands WHERE id LIKE 'job-answer:%'").fetchone()[0] == 0


def test_a_late_answer_for_a_source_deleted_meanwhile_leaves_only_its_cost(engine, monkeypatch):
    """The source is deleted while the extraction waits for the model. The delete cancels the job,
    blanks its payload and takes the stored results that name the source; the answer that comes
    back afterwards is not kept, so the erased words do not come back with it (CR2-MEM-01)."""
    words = "prefers tea in the morning"
    source = []

    def deleted(engine):
        engine.delete(source[0])

    model = PaidModel(engine, monkeypatch, during=deleted)
    source.append(receive(engine, "tea", f"The owner {words}.", extract=True))
    worker = Worker(engine)
    while worker.run_once():
        pass
    assert model.calls == 1
    with engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM commands WHERE id LIKE 'job-answer:%'").fetchone()
        extract = dict(conn.execute("SELECT state,payload FROM jobs WHERE kind='extract'").fetchone())
    assert extract == {"state": "canceled", "payload": "{}"}
    assert metrics(engine, "job_result_discarded")[0]["answers_kept"] == 0
    # What the call cost is on record; what it said is not.
    assert len(metrics(engine, "model_cost")) == 1
    with sqlite3.connect(engine.db.path) as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")]
            for column in columns:
                try:
                    found = conn.execute(f"SELECT 1 FROM '{table}' WHERE instr(CAST(\"{column}\" AS TEXT),?)>0 LIMIT 1",
                                         (words,)).fetchone()
                except sqlite3.DatabaseError:
                    continue
                assert not found, (table, column)
