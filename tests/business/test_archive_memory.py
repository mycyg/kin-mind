"""An archived record becomes a short memory in Kin's own voice, written by DeepSeek (the owner's
decision, 2026-09-28), and the main session recalls it and reads the whole record back.

Asserted here: one call carries at most twenty items and only the fields a summary may use, never
the evidence and its copied metadata; the call is the ordinary structured call, accounted and
receipted; a failure goes back to the queue with a backoff and is tried again, and an item is
written once however often it is archived or run; an erase of what a wish rested on takes its memory
with it, before or after it was written; the wishes archived before this are queued once by the
backfill; and a question finds a memory, whose id reads the whole record.

The endpoint is a script behind httpx.MockTransport. No network, no paid call."""
import json
import math
from datetime import datetime, timedelta

import httpx
import pytest

from eventmem.core.db import NAMED, Missing
from eventmem.core.engine import root_id
from eventmem.core.models import RecallRequest
from kin_mind import archive_memory, desire_archive
from kin_mind.appraisal import APPRAISAL_MODEL, DeepSeek
from kin_mind.host import dispatch
from kin_mind.memory import MemoryContinuity
from kin_mind.state import DesireChange
from kin_mind.strict_schema import strict_schema
from test_desire_retention import allow, config, queued, rows, settle, state_of
from test_erasure import settle as settle_jobs, texts_everywhere

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

DAY = timedelta(days=1)
USAGE = {"input_tokens": 1200, "output_tokens": 300}
MARKER = "lighthousequux"
SUMMARY_FIELDS = {"topic", "content", "wish_kind", "status", "outcome", "completion", "reason",
                  "created_at", "last_activity_at", "expires_at", "timezone"}


class Endpoint:
    """DeepSeek's messages endpoint. By default it answers every item with one first-person line
    naming the item's topic; `answers` scripts the next replies (content, a function of the request,
    or an httpx.Response)."""

    def __init__(self, mind, monkeypatch, *answers):
        monkeypatch.setenv("KIN_TEST_DS_KEY", "synthetic")
        self.answers, self.sent = list(answers), []
        self.provider = DeepSeek("https://api.deepseek.com", APPRAISAL_MODEL, "KIN_TEST_DS_KEY", timeout=60,
                                 transport=httpx.MockTransport(self.respond))
        self.provider.engine = mind.engine

    def respond(self, request):
        sent = json.loads(request.content)
        self.sent.append(sent)
        answer = self.answers.pop(0) if self.answers else summarise
        answer = answer(sent) if callable(answer) else answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={
            "id": f"msg-{len(self.sent)}", "model": APPRAISAL_MODEL, "stop_reason": "tool_use", "usage": USAGE,
            "content": [{"type": "tool_use", "id": f"toolu_{len(self.sent)}", "name": archive_memory.TOOL,
                         "input": {"entries": answer}}]})


def items(sent):
    return json.loads(sent["messages"][0]["content"])["items"]


def summarise(sent):
    return [{"key": item["key"], "text": f"我那时想着{item['record']['topic']}，后来{item['record']['outcome']}。"}
            for item in items(sent)]


def wish(mind, source, clock, key, *, text=None, settled=None):
    clock[0] += timedelta(minutes=5)
    evidence = source(key, text or f"她提到 {key}")
    identifier = mind.manage_desire(DesireChange(
        command_id=key, agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[evidence], action="create", content=f"想和她聊 {text or key}", topic=f"{key} 那件事",
        kind="contact", strength=50, expires_at=(datetime.fromisoformat(mind.clock()) + 3 * DAY).isoformat(),
        completion="她收到了", reason="值得分享"))["desire_id"]
    if settled:
        settle(mind, source, identifier, settled)
    return identifier, evidence


def archived(mind, source, clock, count, **extra):
    """`count` wishes made more than a week ago, moved by the rule."""
    made = [wish(mind, source, clock, f"wish{index}", **extra)[0] for index in range(count)]
    clock[0] += 9 * DAY
    moved = desire_archive.archive(mind, apply=True)
    assert set(moved["moved"]) == set(made)
    return made


def entries(mind):
    with mind.engine.db.connect() as conn:
        return {row["id"]: dict(row) for row in conn.execute(
            "SELECT id,source_key,deleted,data FROM sources WHERE namespace=?", (archive_memory.NAMESPACE,))}


# --- one batch ---------------------------------------------------------------------------------

def test_a_batch_is_one_structured_call_for_at_most_twenty_items_and_sends_only_the_summary_fields(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    made = archived(mind, source, clock, 25)
    endpoint = Endpoint(mind, monkeypatch)
    first = archive_memory.run(mind, endpoint.provider)
    second = archive_memory.run(mind, endpoint.provider)
    assert (first["asked"], second["asked"]) == (20, 5) and len(endpoint.sent) == 2
    assert first["counts"] == {"written": 20} and second["counts"] == {"written": 5}
    assert archive_memory.run(mind, endpoint.provider) == {"state": "idle"}
    for sent in endpoint.sent:
        [tool] = sent["tools"]
        # The ordinary structured call: the named tool, its pydantic schema, which is already strict.
        assert tool["name"] == archive_memory.TOOL
        assert tool["input_schema"] == archive_memory.ArchiveMemories.model_json_schema()
        entry = tool["input_schema"]["$defs"]["ArchiveMemory"]
        assert set(entry["required"]) == set(entry["properties"]) and entry["additionalProperties"] is False
        assert strict_schema(tool["input_schema"])["$defs"]["ArchiveMemory"]["required"] == entry["required"]
        assert desire_archive.MEMORY_INSTRUCTION in sent["system"] and "第一人称" in sent["system"]
        for item in items(sent):
            assert set(item) == {"key", "kind", "record"} and item["kind"] == "desire"
            assert set(item["record"]) == SUMMARY_FIELDS
        body = sent["messages"][0]["content"]
        # Never the evidence: no store id, no copied metadata, no namespace of what it was read from.
        assert not NAMED.findall(body) and "metadata" not in body and "namespace" not in body
    stored = queued(mind)
    assert {item for _, item in stored} == set(made)
    for row in stored.values():
        data = json.loads(row["data"])
        assert row["state"] == "written" and row["attempts"] == 1
        assert data["receipt"]["request_id"] in {"msg-1", "msg-2"} and data["receipt"]["model"] == APPRAISAL_MODEL
        assert [call["purpose"] for call in data["calls"]] == ["archive-memory"]
        assert data["calls"][0]["usage_status"] == "reported"
    written = entries(mind)
    assert len(written) == 25 and {row["source_id"] for row in stored.values()} == set(written)
    with mind.engine.db.connect() as conn:
        # No answer cache keeps a second copy of the words: the queue is what makes it once.
        for table in ("mind_semantic_cache", "mind_judgment_cache"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    one = next(iter(written.values()))
    metadata = json.loads(one["data"])["metadata"]
    assert metadata["archive_kind"] == "desire" and metadata["read_with"] == archive_memory.READ_TOOL
    assert metadata["item_id"] in made and metadata["refs"] and metadata["outcome"] == "let_go_unfinished"
    # A derived source: it rests on what the wish rested on, so deleting that takes it.
    assert json.loads(one["data"])["derived_from"]


def test_a_failed_call_goes_back_with_a_backoff_and_is_tried_again(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    made = archived(mind, source, clock, 3)
    endpoint = Endpoint(mind, monkeypatch, httpx.Response(500, json={"error": "busy"}))
    failed = archive_memory.run(mind, endpoint.provider)
    assert failed["state"] == "failed" and failed["counts"] == {"pending": 3}
    for row in queued(mind).values():
        data = json.loads(row["data"])
        assert row["state"] == "pending" and row["attempts"] == 1 and data["error"] == "deepseek-http-500"
        assert data["calls"][0]["outcome"] == "http-500"
    # Not due again until its backoff has passed.
    assert archive_memory.due(mind) == 0 and archive_memory.run(mind, endpoint.provider) == {"state": "idle"}
    clock[0] += timedelta(seconds=archive_memory.BACKOFF_SECONDS + 1)
    assert archive_memory.due(mind) == 3
    again = archive_memory.run(mind, endpoint.provider)
    assert again["counts"] == {"written": 3} and len(endpoint.sent) == 2
    assert all(json.loads(row["data"]).get("error") is None for row in queued(mind).values())
    assert {row["item_id"] for row in queued(mind).values()} == set(made)


def test_an_answer_that_misses_an_item_or_runs_long_puts_only_that_item_back(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    archived(mind, source, clock, 3)

    def partial(sent):
        found = summarise(sent)
        found[1]["text"] = "长" * (archive_memory.TEXT_LIMIT + 1)
        return found[:2]

    endpoint = Endpoint(mind, monkeypatch, partial)
    result = archive_memory.run(mind, endpoint.provider)
    assert result["counts"] == {"written": 1, "pending": 2}
    errors = sorted(json.loads(row["data"]).get("error") or "" for row in queued(mind).values())
    assert errors == ["", "entry-missing", "entry-too-long"]


def test_an_item_that_keeps_failing_waits_for_an_operator(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    archived(mind, source, clock, 1)
    endpoint = Endpoint(mind, monkeypatch, *[httpx.Response(503) for _ in range(archive_memory.MAX_ATTEMPTS)])
    for _ in range(archive_memory.MAX_ATTEMPTS):
        archive_memory.run(mind, endpoint.provider)
        clock[0] += timedelta(seconds=archive_memory.BACKOFF_CAP_SECONDS + 1)
    [row] = queued(mind).values()
    assert row["state"] == "failed" and row["attempts"] == archive_memory.MAX_ATTEMPTS
    assert archive_memory.run(mind, endpoint.provider) == {"state": "idle"}
    assert archive_memory.status(mind)["errors"] == {"deepseek-http-503": 1}
    assert archive_memory.retry_failed(mind) == 1
    assert archive_memory.run(mind, endpoint.provider)["counts"] == {"written": 1}


# --- once per item and revision ----------------------------------------------------------------

def test_an_item_is_written_once_however_often_it_is_queued_or_run(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    [made] = archived(mind, source, clock, 1)
    endpoint = Endpoint(mind, monkeypatch)
    assert archive_memory.run(mind, endpoint.provider)["counts"] == {"written": 1}
    with mind.engine.db.connect(write=True) as conn:
        record = desire_archive.archived(conn, mind.scope.key(), made)
        assert archive_memory.enqueue(mind, conn, "desire", [desire_archive.memory_item(record)]) == 0
    assert archive_memory.run(mind, endpoint.provider) == {"state": "idle"} and len(endpoint.sent) == 1
    # A run that wrote the entry and stopped before it could say so: the next one asks nobody.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_archive_memory SET state='pending',source_id=NULL,next_at=0")
    assert archive_memory.run(mind, endpoint.provider)["counts"] == {"written": 1}
    assert len(endpoint.sent) == 1 and len(entries(mind)) == 1
    [row] = queued(mind).values()
    assert row["source_id"] in entries(mind)


def test_the_queue_answers_only_while_its_flag_is_on(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind, archive_memory=False)
    archived(mind, source, clock, 2)
    endpoint = Endpoint(mind, monkeypatch)
    assert archive_memory.due(mind) == 0 and archive_memory.run(mind, endpoint.provider) == {"state": "disabled"}
    assert not endpoint.sent and len(queued(mind)) == 2
    MemoryContinuity(mind).configure({"archive_memory": True})
    assert archive_memory.due(mind) == 2


# --- the host's background lane ----------------------------------------------------------------

def test_the_background_lane_writes_them_when_it_has_nothing_else_to_run(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind, operational_lanes=True, semantic=True)
    clock[0] -= 30 * DAY
    for index in range(2):
        wish(mind, source, clock, f"lane{index}")
    clock[0] = datetime.now(clock[0].tzinfo)
    assert desire_archive.archive(mind, apply=True)["moved_count"] == 2
    endpoint = Endpoint(mind, monkeypatch)
    monkeypatch.setattr(DeepSeek, "from_engine", classmethod(lambda cls, engine: endpoint.provider))
    settings = config(mind)
    due = dispatch(settings, "review-due", {"tick": False})
    assert due["enrichment"] is True and due["action"] is False
    result = dispatch(settings, "review-enrichment", {})
    assert result["counts"] == {"written": 2} and len(endpoint.sent) == 1
    assert dispatch(settings, "review-due", {"tick": False})["enrichment"] is False
    # The operator's own routes: counts only without --apply.
    status = dispatch(settings, "archive-memory", {})
    assert status["counts"] == {"desire": {"written": 2}} and status["due"] == 0


# --- an erase takes the memory with what it rested on --------------------------------------------

def test_an_erase_of_what_a_wish_rested_on_takes_its_memory_and_leaves_no_words(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    identifier, evidence = wish(mind, source, clock, "erased", text=MARKER)
    other, _ = wish(mind, source, clock, "kept")
    clock[0] += 9 * DAY
    desire_archive.archive(mind, apply=True)
    endpoint = Endpoint(mind, monkeypatch, lambda sent: [
        {"key": item["key"], "text": f"我想和她聊 {MARKER}，后来放下了。" if MARKER in item["record"]["content"] else "我想过另一件事。"}
        for item in items(sent)])
    archive_memory.run(mind, endpoint.provider)
    assert len(entries(mind)) == 2 and texts_everywhere(mind.engine, MARKER)
    mind.engine.delete(evidence)
    settle_jobs(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    live = {row["id"] for row in entries(mind).values() if not row["deleted"]}
    kept = {row["source_id"] for row in queued(mind).values() if row["item_id"] == other}
    assert live == kept, "only the memory of the wish that rested on it went"
    found = archive_memory.read(mind, identifier, kind="desire")
    assert found["state"] == "erased" and MARKER not in json.dumps(found, ensure_ascii=False)


def test_an_item_whose_evidence_is_erased_before_its_turn_is_never_sent(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    identifier, evidence = wish(mind, source, clock, "gone", text=MARKER)
    clock[0] += 9 * DAY
    desire_archive.archive(mind, apply=True)
    mind.engine.delete(evidence)
    settle_jobs(mind.engine)
    endpoint = Endpoint(mind, monkeypatch)
    result = archive_memory.run(mind, endpoint.provider)
    assert result["counts"] == {"withheld": 1} and not endpoint.sent
    [row] = queued(mind).values()
    assert row["state"] == "withheld" and json.loads(row["data"])["summary_input"] == {}
    assert texts_everywhere(mind.engine, MARKER) == set()


# --- the wishes archived before ------------------------------------------------------------------

def test_the_backfill_queues_what_was_archived_before_once(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind)
    made = archived(mind, source, clock, 23, settled="complete")
    with mind.engine.db.connect(write=True) as conn:
        # As the wishes archived by the release before this one are: in the archive, never queued.
        conn.execute("DELETE FROM mind_archive_memory")
    dry = archive_memory.backfill(mind)
    assert dry["state"] == "dry-run" and dry["would_queue"] == 23 and dry["queued"] == 0
    assert dry["model_calls"] == math.ceil(23 / archive_memory.BATCH) == 2 and queued(mind) == {}
    applied = archive_memory.backfill(mind, apply=True)
    assert applied["queued"] == 23 and applied["kinds"]["desire"]["archived"] == 23
    assert archive_memory.backfill(mind, apply=True)["queued"] == 0
    assert dispatch(config(mind), "archive-memory-backfill", {})["would_queue"] == 0
    endpoint = Endpoint(mind, monkeypatch)
    archive_memory.run(mind, endpoint.provider)
    archive_memory.run(mind, endpoint.provider)
    assert {row["state"] for row in queued(mind).values()} == {"written"} and len(endpoint.sent) == 2
    assert {item for _, item in queued(mind)} == set(made)
    assert {json.loads(row["data"])["summary_input"]["outcome"] for row in queued(mind).values()} == {"completed"}


# --- recalled by a question, read back whole ---------------------------------------------------------

def test_a_question_finds_the_memory_whose_id_reads_the_whole_record_and_an_erase_takes_both(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind, records=True, semantic=True, context=True)
    # engine.recall reads with a mind on the wall clock: what it reads was made in the past.
    clock[0] -= 30 * DAY
    identifier, evidence = wish(mind, source, clock, "lighthouse", text=MARKER)
    clock[0] = datetime.now(clock[0].tzinfo)
    desire_archive.archive(mind, apply=True)
    endpoint = Endpoint(mind, monkeypatch)
    archive_memory.run(mind, endpoint.provider)
    [entry] = entries(mind)
    recalled = mind.engine.recall(RecallRequest(scope=mind.scope, query="lighthouse 那件事"))
    assert recalled["index"][0]["id"] == root_id(entry) and root_id(entry) in recalled["covered_ids"]
    assert "我那时想着lighthouse 那件事" in recalled["text"] and archive_memory.READ_TOOL in recalled["text"]
    # From the memory, the whole record: its words, its status, its evidence by id.
    whole = archive_memory.read(mind, entry)
    assert whole["state"] == "archived" and whole["id"] == identifier and whole["outcome"] == desire_archive.LET_GO
    assert MARKER in whole["record"]["content"] and whole["record"]["status"] == "wanted"
    assert whole["entries"][0]["source_id"] == entry and whole["entries"][0]["refs"]
    assert all("metadata" not in ref for ref in whole["record"]["evidence"])
    assert archive_memory.read(mind, root_id(entry)) == whole
    assert archive_memory.read(mind, identifier, kind="desire")["record"] == whole["record"]
    # Its refs are ordinary records, for the ordinary read.
    with mind.engine.db.connect() as conn:
        assert all(mind.engine._get(conn, ref)["id"] == ref for ref in whole["entries"][0]["refs"])
    mind.engine.delete(evidence)
    settle_jobs(mind.engine)
    again = mind.engine.recall(RecallRequest(scope=mind.scope, query="lighthouse 那件事"))
    assert root_id(entry) not in {item["id"] for item in again["index"]}
    assert MARKER not in json.dumps(again, ensure_ascii=False)
    gone = archive_memory.read(mind, identifier, kind="desire")
    assert gone["state"] == "erased" and gone["entries"][0]["state"] == "erased"
    assert MARKER not in json.dumps(gone, ensure_ascii=False)
    with pytest.raises(Missing):
        archive_memory.read(mind, entry)


def test_a_question_is_matched_by_words_not_by_the_first_person(setup, monkeypatch):
    mind, source, clock = setup
    allow(mind, records=True, semantic=True, context=True)
    clock[0] -= 30 * DAY
    wish(mind, source, clock, "harbour")
    clock[0] = datetime.now(clock[0].tzinfo)
    desire_archive.archive(mind, apply=True)
    archive_memory.run(mind, Endpoint(mind, monkeypatch).provider)
    [entry] = entries(mind)
    matched = mind.engine.recall(RecallRequest(scope=mind.scope, query="harbour"))
    assert matched["index"][0]["id"] == root_id(entry) and root_id(entry) in matched["covered_ids"]
    # The lane of its own answers only a word of the question; what the ordinary record lane finds on
    # a single character is that lane's business, and comes after.
    from eventmem.core.read_policy import ReadPolicy
    from kin_mind.context import Contexts
    policy = ReadPolicy.load(mind.engine, mind.scope, "experience_recall")
    assert archive_memory.recall_items(Contexts(mind), "我 今天 天气", policy) == []
    assert [item["id"] for item in archive_memory.recall_items(Contexts(mind), "harbour", policy)] == [root_id(entry)]
    unrelated = mind.engine.recall(RecallRequest(scope=mind.scope, query="我 今天 天气"))
    assert unrelated["index"][0]["id"] != root_id(entry)
