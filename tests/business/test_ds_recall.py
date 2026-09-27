"""A DeepSeek appraisal reads memory with the host's two read-only tools (K1-16 on the DeepSeek lane).

The fork reads memory with its own read-only MCP. A DeepSeek appraisal is an HTTP request, so the host
offers `recall_memory` and `read_memory` beside `submit_appraisal`, runs what the model asks for, sends
the answer back whole with a result for each read, and asks again: three rounds at most, begun within
the budget, and the last request offers the submission alone. What a read returned is named on the
receipt as a fork's reads are (`tool_calls` with their ids): a record it returned may be cited, and
goes with everything written from it; a read that returned nothing admits nothing. The lanes that read
nothing of their own, and a provider without a store, send the request exactly as before. Every call
leaves its entry in the attempt's record, and a round of reads is not a charged attempt.

The endpoint is a script behind httpx.MockTransport: one answer per request, in order."""
import itertools
import json
import time as real_time

import httpx
import pytest

from eventmem.core.db import digest, dumps
from eventmem.core.models import Scope, SourceInput

from kin_mind import appraisal as appraisal_module
from kin_mind import attempts, erasure
from kin_mind.appraisal import (APPRAISAL_EFFORT, APPRAISAL_MODEL, FOLLOW_UP, RECALL_CALLS, RECALL_DONE,
                                RECALL_PROMPT, RECALL_ROUNDS, RECALL_WITHHELD, Appraisals, DeepSeek)
from kin_mind.memory import MemoryContinuity
from kin_mind.state import fork_reads
from test_derived_erasure import queue_row, read_note
from test_erasure import settle, texts_everywhere
from test_main_session_review import native_provider
from test_tool_fetched_evidence import MARKER, interpreting, understanding

pytest_plugins = ('test_kin_mind',)

USAGE = {"input_tokens": 900, "output_tokens": 60}
READS = ["submit_appraisal", "recall_memory", "read_memory"]
SUBMIT = ["submit_appraisal"]
# How the system prompt ended before the reads existed.
PLAIN_END = "其他探索仅作背景。"
NUMBERS = itertools.count(1)


def thought():
    """A thinking block as DeepSeek returns one with thinking on; a request after a tool use has to
    carry it back unchanged."""
    return {"type": "thinking", "thinking": "先想想她说过什么", "signature": f"sig-{next(NUMBERS)}"}


def reads(*calls):
    """An answer that asks for reads and nothing else: (tool, input) pairs."""
    return [thought(), *({"type": "tool_use", "id": f"toolu_{next(NUMBERS)}", "name": name, "input": arguments}
                         for name, arguments in calls)]


def submits(proposal):
    return [thought(), {"type": "tool_use", "id": f"toolu_{next(NUMBERS)}", "name": "submit_appraisal", "input": proposal}]


def recalled_note(record):
    """The appraisal whose understanding rests on the note the reads returned."""
    return {"reason": "想起了那家店", "values": {"curiosity": 61}, "understanding": {
        "meaning": f"她说过 {MARKER} 周末也开门", "topic": "那家店", "importance": 50, "confidence": 0.7,
        "basis": "internal_thought", "evidence_ids": [record]}}


class Endpoint:
    """DeepSeek's messages endpoint, answering from a script. Each answer is the content of a reply,
    an httpx.Response, or a function of the request that returns either. Every request is kept as
    sent, with the timeout it was given."""

    def __init__(self, mind, monkeypatch, *answers, timeout=600, engine=True, clock=None, step=0):
        monkeypatch.setenv("KIN_TEST_DS_KEY", "synthetic")
        self.answers, self.sent, self.raw, self.timeouts = list(answers), [], [], []
        self.clock, self.step = clock, step
        self.provider = DeepSeek("https://api.deepseek.com", APPRAISAL_MODEL, "KIN_TEST_DS_KEY", timeout=timeout,
                                 transport=httpx.MockTransport(self.respond))
        if engine:
            self.provider.engine = mind.engine

    def respond(self, request):
        self.raw.append(request.content)
        self.timeouts.append(request.extensions["timeout"]["read"])
        sent = json.loads(request.content)
        self.sent.append(sent)
        if self.clock:
            # The model took `step` seconds to answer.
            self.clock.offset += self.step
        answer = self.answers.pop(0)
        answer = answer(sent) if callable(answer) else answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"id": f"msg-{len(self.sent)}", "model": APPRAISAL_MODEL,
                                         "stop_reason": "tool_use", "usage": USAGE, "content": answer})

    def tools(self):
        return [[tool["name"] for tool in sent["tools"]] for sent in self.sent]


class Clock:
    """The appraisal module's clock, which the endpoint moves on as its model answers."""

    offset = 0.0

    def monotonic(self):
        return real_time.monotonic() + self.offset

    def __getattr__(self, name):
        return getattr(real_time, name)


def results(sent):
    """The tool results the last turn of a request carries, as blocks."""
    return [block for block in sent["messages"][-1]["content"] if block.get("type") == "tool_result"]


def shown(block):
    return json.loads(block["content"])


def ledger(mind, job):
    [entry] = attempts.read(mind.engine, mind.scope.key(), job_id=job["id"])["attempts"]
    return entry


def paid(mind):
    with mind.engine.db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM metrics WHERE name='structured_model_usage'").fetchone()[0]


def as_before(sent):
    """The request one call sent before the host's reads existed, rebuilt from what this one carries:
    the same bytes mean nothing was added -- no tool, no turn, no field."""
    return httpx.Request("POST", "https://api.deepseek.com/v1/messages", json={
        "model": APPRAISAL_MODEL, "max_tokens": 131072, "system": sent["system"],
        "messages": [{"role": "user", "content": sent["messages"][0]["content"]}],
        "tools": [{"name": "submit_appraisal", "description": "提交有来源的状态提案", "input_schema": sent["tools"][0]["input_schema"]}],
        "tool_choice": {"type": "auto"}, "thinking": {"type": "enabled"},
        "output_config": {"effort": APPRAISAL_EFFORT}}).content


@pytest.mark.parametrize("memory_context", [False, True])
def test_a_note_recalled_in_one_round_may_be_cited_and_goes_with_everything_written_from_it(setup, monkeypatch, memory_context):
    """The first request offers the reads beside the submission and says so. The model asks for a
    recall; the second request carries its answer whole -- the thinking block included -- and the
    recall's result, which shows the note no context supplied. The appraisal cites it: the commit takes
    it as evidence, the receipt names the read and what it returned, and the queue row names it. Deleted
    later, the note takes every word written from it. With the memory context on, as Kin has it, the
    recall still shows the records themselves, not the context's rendering (the affect and what the
    mind made from the store, whose sources it would not name)."""
    mind, source, clock = setup
    if memory_context:
        MemoryContinuity(mind).configure({"context": True})
    note_source, note_record = read_note(mind, clock, "recalled", f"她说 {MARKER} 周末也开门")
    interpreting(mind, source, clock)
    primary = source("walk", "今天路过那家店")
    first = reads(("recall_memory", {"query": MARKER}))

    def cite(sent):
        [result] = results(sent)
        assert [item["id"] for item in shown(result)["items"]] == [note_record] and MARKER in shown(result)["text"]
        return submits(recalled_note(note_record))
    ds = Endpoint(mind, monkeypatch, first, cite)
    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "complete"

    assert ds.tools() == [READS, READS] and ds.sent[0]["system"].endswith(RECALL_PROMPT)
    assert ds.sent[1]["system"] == ds.sent[0]["system"]
    user, assistant, answered = ds.sent[1]["messages"]
    assert user == ds.sent[0]["messages"][0] and assistant == {"role": "assistant", "content": first}
    assert [block["tool_use_id"] for block in answered["content"]] == [first[1]["id"]]
    assert "is_error" not in answered["content"][0]
    state, count, data = queue_row(mind, job["id"])
    receipt = data["receipt"]
    assert receipt["tool_fetched_evidence"] == [note_record]
    assert receipt["tool_calls"] == [{"name": "kin_memory.recall_memory", "ok": True,
                                      "ids": sorted([{"id": note_record, "revision": 1}, {"id": note_source, "revision": None}],
                                                    key=lambda entry: entry["id"])}]
    assert [r["request_id"] for r in receipt["recall_rounds"]] == ["msg-1"] and receipt["request_id"] == "msg-2"
    # Stored, it reads as a fork's receipt does: every id it names, nothing cut short, and the host's
    # trimming of a fork turn keeps all of it.
    assert erasure.read_ids(receipt) == sorted([note_record, note_source]) and not erasure.reads_truncated(receipt)
    assert fork_reads({"tool_calls": receipt["tool_calls"]}) == {"tool_calls": receipt["tool_calls"]}
    assert note_record in {ref["record_id"] for ref in data["evaluated_sources"]} and note_record in data["evaluated_ids"]
    assert [ref["record_id"] for ref in understanding(mind)["evidence"]] == [note_record]
    mind.engine.delete(note_source)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_record_read_by_its_id_may_be_cited(setup, monkeypatch):
    """`read_memory` shows the record's own text at its revision, with its source: the appraisal
    that cites it commits, and the receipt names what the read returned."""
    mind, source, clock = setup
    note_source, note_record = read_note(mind, clock, "read", f"她说 {MARKER} 周末也开门")
    interpreting(mind, source, clock)
    ds = Endpoint(mind, monkeypatch, reads(("read_memory", {"record_id": note_record})), submits(recalled_note(note_record)))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "complete"
    read = shown(results(ds.sent[1])[0])
    assert (read["id"], read["revision"], read["source_ids"]) == (note_record, 1, [note_source]) and MARKER in read["content"]
    receipt = queue_row(mind, job["id"])[2]["receipt"]
    assert receipt["tool_fetched_evidence"] == [note_record]
    assert receipt["tool_calls"] == [{"name": "kin_memory.read_memory", "ok": True,
                                      "ids": [{"id": note_record, "revision": 1}, {"id": note_source, "revision": None}]}]


def test_an_id_a_read_shows_of_something_deleted_before_it_is_not_named(setup, monkeypatch):
    """A note names another note by its id, and the other is deleted after the attempt began but
    before the read. The read shows that id -- all a delete leaves, no words -- and names only what
    it read: the model had nothing of the deleted note before it, so its delete does not stop the
    commit (CL6E-MM-02)."""
    mind, source, clock = setup
    other_source, other_record = read_note(mind, clock, "named", "另一条笔记")
    note_source, note_record = read_note(mind, clock, "naming", f"她说 {MARKER} 周末也开门，另见 {other_record}")

    def first(sent):
        mind.engine.delete(other_source)
        return reads(("read_memory", {"record_id": note_record}))
    ds = Endpoint(mind, monkeypatch, first, submits({"reason": "想起了那家店", "values": {"curiosity": 61}}))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "complete"
    assert other_record in shown(results(ds.sent[1])[0])["content"]
    assert queue_row(mind, job["id"])[2]["receipt"]["tool_calls"] == [{"name": "kin_memory.read_memory", "ok": True,
        "ids": [{"id": note_record, "revision": 1}, {"id": note_source, "revision": None}]}]


@pytest.mark.parametrize("case,refused", [("deleted", ("Missing", None)),
                                          ("never-was", ("Conflict", "evidence-out-of-bounds")),
                                          ("other-scope", ("Conflict", "reference-cross-scope"))])
def test_a_read_that_returned_nothing_admits_nothing(setup, monkeypatch, case, refused):
    """The model reads a record that was deleted, one that never was, or one of another scope, and
    cites it -- or, when the read found nothing, a note that exists but nothing returned. Each read
    answers `not-found` and returns no id; the receipt says the call failed, and the commit refuses
    the citation as it always refused evidence this appraisal was not given."""
    mind, source, clock = setup
    note_source, note_record = read_note(mind, clock, "never-read", f"她说 {MARKER} 周末也开门")
    asked, cited = {"never-was": ("mem_" + "0" * 32, note_record)}.get(case, (None, None))
    if case == "deleted":
        gone_source, gone_record = read_note(mind, clock, "deleted", f"她还说 {MARKER} 的面包好吃")
        mind.engine.delete(gone_source)
        asked, cited = gone_record, gone_record
    if case == "other-scope":
        other = mind.engine.receive(SourceInput(namespace="kin-notes", key="elsewhere", text=f"别处的 {MARKER}",
                                                scope=Scope(persona="somebody-else"), authority="document",
                                                occurred_at=clock[0].isoformat()))["id"]
        asked = cited = mind.engine.source(other)["record_ids"][0]
    interpreting(mind, source, clock)
    ds = Endpoint(mind, monkeypatch, reads(("read_memory", {"record_id": asked})), submits(recalled_note(cited)))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    jobs.run_one(ds.provider)

    [result] = results(ds.sent[1])
    assert result["is_error"] is True and shown(result) == {"error": "not-found"}
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and (data["error_detail"]["class"], data["error_detail"].get("code")) == refused, data
    assert "tool_fetched_evidence" not in data["receipt"]
    assert data["receipt"]["tool_calls"] == [{"name": "kin_memory.read_memory", "ok": False, "ids": []}]
    assert understanding(mind) is None or cited not in json.dumps(understanding(mind))


def test_after_three_rounds_the_next_request_offers_the_submission_alone(setup, monkeypatch):
    """The model asks for reads three times. The fourth request answers the third round, says the
    reads are over, and offers `submit_appraisal` alone; the turns before it are all there. Every call
    is in the attempt's record with its purpose and usage -- three rounds of reads and the appraisal --
    and the attempt is one charged attempt, as a single call's is."""
    mind, source, clock = setup
    read_note(mind, clock, "recalled", f"她说 {MARKER} 周末也开门")
    ds = Endpoint(mind, monkeypatch, *[reads(("recall_memory", {"query": f"{MARKER} {index}"})) for index in range(RECALL_ROUNDS)],
                  submits({"reason": "想起了那家店", "values": {"curiosity": 61}}))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "complete"

    assert ds.tools() == [READS] * RECALL_ROUNDS + [SUBMIT]
    last = ds.sent[-1]["messages"]
    assert len(last) == 1 + 2 * RECALL_ROUNDS and [m["role"] for m in last] == ["user"] + ["assistant", "user"] * RECALL_ROUNDS
    assert [block["type"] for block in last[-1]["content"]] == ["tool_result", "text"]
    assert last[-1]["content"][-1]["text"] == RECALL_DONE
    entry = ledger(mind, job)
    assert [(c["purpose"], c["outcome"], c["usage"]) for c in entry["calls"]] == [("recall", "ok", USAGE)] * RECALL_ROUNDS + [("appraise", "ok", USAGE)]
    assert [c["detail"]["round"] for c in entry["calls"][:-1]] == list(range(1, RECALL_ROUNDS + 1))
    assert entry["charged"] is True and entry["context_digest"] == entry["calls"][-1]["context_digest"]
    assert queue_row(mind, job["id"])[1] == 1 and paid(mind) == RECALL_ROUNDS + 1
    receipt = queue_row(mind, job["id"])[2]["receipt"]
    assert len(receipt["tool_calls"]) == len(receipt["recall_rounds"]) == RECALL_ROUNDS


def test_calls_past_the_limit_of_one_round_are_refused(setup, monkeypatch):
    """One answer asks for more reads than a round runs: the ones past the limit are answered as
    refused, return nothing, and the receipt says so."""
    mind, source, clock = setup
    ds = Endpoint(mind, monkeypatch, reads(*[("recall_memory", {"query": f"那家店 {index}"}) for index in range(RECALL_CALLS + 1)]),
                  submits({"reason": "想起了那家店", "values": {"curiosity": 61}}))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "complete"
    blocks = results(ds.sent[1])
    assert len(blocks) == RECALL_CALLS + 1 and ["is_error" in block for block in blocks] == [False] * RECALL_CALLS + [True]
    assert shown(blocks[-1]) == {"error": "round-limit"}
    calls = queue_row(mind, job["id"])[2]["receipt"]["tool_calls"]
    assert [call["ok"] for call in calls] == [True] * RECALL_CALLS + [False] and calls[-1]["ids"] == []


@pytest.mark.parametrize("timeout,step,tools,given", [
    # Rounds begin only within the window: the third request, 200 seconds in, offers the submission alone.
    (600, 100, [READS, READS, SUBMIT], [420, 320, 400]),
    # A request that offers the reads ends early enough for the one that must submit; the second
    # would have had less than an appraisal call is given, so it is that one.
    (320, 70, [READS, SUBMIT], [140, 250]),
    # Not enough time for a round at all (a long preparation, say): the reads are not offered, and the
    # one request has all the time there is.
    (290, 70, [SUBMIT], [290]),
])
def test_rounds_keep_to_the_window_and_leave_the_submission_its_time(setup, monkeypatch, timeout, step, tools, given):
    mind, source, clock = setup
    tick = Clock()
    monkeypatch.setattr(appraisal_module, "time", tick)
    answers = [reads(("recall_memory", {"query": "那家店"})) for _ in tools[:-1]]
    ds = Endpoint(mind, monkeypatch, *answers, submits({"reason": "想起了那家店", "values": {"curiosity": 61}}),
                  timeout=timeout, clock=tick, step=step)
    jobs = Appraisals(mind)
    jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "complete"
    assert ds.tools() == tools
    assert [round(value) for value in ds.timeouts] == given
    # The lane reads, so the prompt says so whether or not this request could offer the reads.
    assert all(sent["system"].endswith(RECALL_PROMPT) for sent in ds.sent)


def test_every_call_is_recorded_and_a_round_of_reads_charges_nothing(setup, monkeypatch):
    """A round of reads, then an outage on the request that should have submitted. Nothing was
    appraised: the attempt is an outage like any other -- no charged attempt, the transient counter --
    and both calls are in its record, the round with the usage it reported and the refused request
    with none."""
    mind, source, clock = setup
    ds = Endpoint(mind, monkeypatch, reads(("recall_memory", {"query": "那家店"})), httpx.Response(503, json={"error": "synthetic"}))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "pending"
    state, count, data = queue_row(mind, job["id"])
    assert (count, data["error"], data["transient_failures"]) == (0, "deepseek-http-503", 1)
    entry = ledger(mind, job)
    assert entry["charged"] is False
    assert [(c["purpose"], c["outcome"], c["usage_status"]) for c in entry["calls"]] == [("recall", "ok", "reported"),
                                                                                        ("appraise", "http-503", "unknown")]
    assert paid(mind) == 1



@pytest.mark.parametrize("refused_at", [0, 1])
def test_a_request_of_the_reads_the_endpoint_refuses_is_asked_again_as_before(setup, monkeypatch, refused_at):
    """The endpoint refuses outright a request the reads made: the first, which offers them, or the one
    that carries a round of them back. That is no fault of the appraisal, and a change on the endpoint's
    side must not stop every appraisal: the attempt asks once more with the very bytes the single call
    sent before the reads existed, and commits. That answer saw no read, so the receipt names none; the
    round it paid for and the refusal stay on it, and the refused request is in the record."""
    mind, source, clock = setup
    refusal = httpx.Response(400, json={"error": {"message": "synthetic"}})
    before = [reads(("recall_memory", {"query": "那家店"}))] * refused_at
    ds = Endpoint(mind, monkeypatch, *before, refusal, submits({"reason": "路过那家店", "values": {"curiosity": 61}}))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "complete"

    assert ds.tools() == [READS] * (refused_at + 1) + [SUBMIT]
    assert ds.raw[-1] == as_before(ds.sent[-1]) and ds.sent[-1]["system"].endswith(PLAIN_END)
    assert ds.sent[-1]["messages"] == ds.sent[0]["messages"][:1]
    state, count, data = queue_row(mind, job["id"])
    receipt = data["receipt"]
    assert "tool_calls" not in receipt and "tool_fetched_evidence" not in receipt
    assert receipt["recall_refused"] == "deepseek-http-400" and len(receipt.get("recall_rounds") or ()) == refused_at
    entry = ledger(mind, job)
    assert [(c["purpose"], c["outcome"]) for c in entry["calls"]] == [("recall", "ok")] * refused_at + [
        ("appraise", "http-400"), ("appraise", "ok")]
    assert entry["charged"] is True and count == 1
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name='recall_refused'").fetchone()[0] == 1


def test_a_refusal_of_the_plain_request_or_a_rate_limit_is_what_it_always_was(setup, monkeypatch):
    """Only a request the reads made is asked again. A rate limit on one is an outage like any other,
    and a refusal of a request that offered no reads -- a lane that reads nothing -- fails as before."""
    mind, source, clock = setup
    ds = Endpoint(mind, monkeypatch, httpx.Response(429, json={"error": "synthetic"}))
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    assert jobs.run_one(ds.provider)["state"] == "pending" and len(ds.sent) == 1
    assert queue_row(mind, job["id"])[2]["error"] == "deepseek-http-429"
    ds = Endpoint(mind, monkeypatch, httpx.Response(400, json={"error": "synthetic"}))
    backfill = jobs.enqueue([source("old", "很久以前的事")], "synthetic-v1", stimulus="memory-backfill")
    jobs.run_one(ds.provider)
    assert len(ds.sent) == 1 and queue_row(mind, backfill["id"])[2]["error"] == "deepseek-http-400"

def test_a_record_read_and_deleted_while_the_model_answers_stops_the_commit(setup, monkeypatch):
    """The model reads a note and repeats it without citing it; the note is deleted while the model
    answers. The read named it, so the commit is refused for the delete -- uncharged -- and no word of
    it is left anywhere."""
    mind, source, clock = setup
    note_source, note_record = read_note(mind, clock, "read-then-deleted", f"她说 {MARKER} 周末也开门")

    def answer(sent):
        mind.engine.delete(note_source)
        return submits({"reason": f"想起她说 {MARKER} 周末也开门", "values": {"curiosity": 61}})
    ds = Endpoint(mind, monkeypatch, reads(("read_memory", {"record_id": note_record})), answer)
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("walk", "今天路过那家店")], "synthetic-v1")
    jobs.run_one(ds.provider)
    assert MARKER in shown(results(ds.sent[1])[0])["content"]
    state, count, data = queue_row(mind, job["id"])
    assert (state, count, data["deletion_refusals"]) == ("pending", 0, 1), data.get("error_detail")
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()


@pytest.mark.parametrize("lane", ["session-maintenance", FOLLOW_UP, "memory-backfill", "memory-enrichment", "no-store"])
def test_lanes_that_read_nothing_and_a_provider_without_a_store_send_the_request_as_before(setup, monkeypatch, lane):
    """History, the follow-up and session maintenance judge what they were given, and a provider with
    no store has nothing to read: one request, the submission alone, no word of the reads in the
    prompt, and the very bytes the single call sent before the reads existed."""
    mind, source, clock = setup
    primary = source("walk", "今天路过那家店")
    answer = {"reason": "照旧", "values": {"curiosity": 61}}
    jobs = Appraisals(mind)
    if lane == "session-maintenance":
        jobs = Appraisals(mind, session_context={"id": "snapshot-1", "binding": {"generation": 1}, "evidence": [], "recent": []})
        jobs.enqueue_maintenance("snapshot-1", "synthetic-v1")
        answer = {"reason": "照旧", "session_advice": {"action": "keep", "reason": "还好"}}
    elif lane == FOLLOW_UP:
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,?,?)",
                         ("review_synthetic", mind.scope.key(), "pending", real_time.time(), dumps({
                             "evidence_ids": [primary], "agent_version": "synthetic-v1", "origin": "reflection",
                             "stimulus": FOLLOW_UP, "parent_id": "appraise_synthetic",
                             "section_review": {"rejected_sections": [], "held_sections": []}})))
    elif lane != "no-store":
        jobs.enqueue([primary], "synthetic-v1", stimulus=lane)
        answer = {"reason": "整理旧记录", "memory": {}}
    ds = Endpoint(mind, monkeypatch, submits(answer), engine=lane != "no-store")
    if lane == "no-store":
        ds.provider.memory_recall = True
        ds.provider.appraise({"state": mind.read(), "definitions": {}, "new_evidence": [], "stimulus": None})
    else:
        jobs.run_one(ds.provider)
    [sent] = ds.sent
    assert [tool["name"] for tool in sent["tools"]] == SUBMIT and len(sent["messages"]) == 1
    assert RECALL_PROMPT not in sent["system"] and sent["system"].endswith(PLAIN_END)
    assert ds.raw[0] == as_before(sent)


def test_the_request_profile_names_the_reads_only_where_they_are_offered(setup, monkeypatch):
    """The reads frame a request as much as its prompt: where they are offered the profile says so, in
    its prompt and its parameters, so a proposal asked without them is not reused as one asked with
    them. Where they are not -- a provider nobody asked, a lane that reads nothing, the fork -- the
    profile is what it always was."""
    mind, source, clock = setup
    provider = Endpoint(mind, monkeypatch).provider
    context = {"state": mind.read(), "stimulus": None}
    plain = provider.request_profile(context)
    assert plain["parameters"] == digest({"max_tokens": 131072, "thinking": "enabled", "effort": APPRAISAL_EFFORT,
                                          "tool_choice": "auto"})
    provider.memory_recall = True
    offered = provider.request_profile(context)
    assert offered["parameters"] != plain["parameters"] and offered["system"] != plain["system"]
    assert (offered["schema"], offered["model"]) == (plain["schema"], plain["model"])
    for stimulus in sorted(RECALL_WITHHELD):
        assert provider.request_profile({**context, "stimulus": stimulus})["parameters"] == plain["parameters"], stimulus
    native = native_provider(mind, None)
    native.memory_recall = True
    assert native._recall_scope(context) is None and RECALL_PROMPT not in native._system(context, None)
