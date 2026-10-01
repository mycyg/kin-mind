"""Enrichment waits for a quiet conversation (`enrichment_settle_minutes`, kin_mind.settling).

The action lane still assesses every turn. Only the memory lane waits: an enrichment job becomes
available once the scope's conversation has been quiet for the setting's minutes, each new message
moves that time, an outreach of Kin's own keeps the conversation open for its answer, and the jobs
that waited together are one request. Off (the default), every job is available at once, carries
exactly the data it always did, and runs alone.

Times are put in the past rather than the queue's clock moved: an event's own time is what the wait
counts from."""
import json
import time
from datetime import timedelta

from eventmem.core.models import RecallRequest

from kin_mind import settling
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.memory import DEFAULTS, MemoryAssessment, MemoryContinuity, MemoryNote
from test_derived_erasure import answered, paid, queue_row
from test_erasure import settle

pytest_plugins = ('test_kin_mind',)

# What an enrichment row carried before settling existed.
ENRICHMENT_KEYS = {"evidence_ids", "agent_version", "origin", "stimulus", "parent_id", "seed_memory", "seed_receipt",
                   "seed_sources", "evaluated_ids", "seed_tombstone_mark"}


class Action:
    """The action lane's model: it feels and decides, and leaves memory to the enrichment lane."""
    def appraise(self, context):
        paid(self)
        return answered(Appraisal(reason="收到了"))


class Seeding:
    """An action lane's model that also proposes a note, which its enrichment row keeps as a seed."""
    def __init__(self, mind):
        self.mind = mind

    def appraise(self, context):
        paid(self)
        [source] = [s for s in context["new_evidence"]]
        record = self.mind.engine.source(source["id"])["record_ids"][0]
        return answered(Appraisal(reason="记下了", memory=MemoryAssessment(notes=[
            MemoryNote(key="note", title="一句话", content="她说了一句话", evidence_ids=[record])])))


class Organizer:
    """The enrichment lane's model: what each request was given."""
    def __init__(self):
        self.requests = []

    def appraise(self, context):
        self.requests.append([source["id"] for source in context["new_evidence"]])
        paid(self)
        return answered(Appraisal(reason="整理了这段对话"))


class Never:
    def appraise(self, context):
        raise AssertionError("a seed of its own is used without a call")


def configured(mind, **values):
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "operational_lanes": True, **values})
    return memory


def at(clock, minutes):
    """A time `minutes` before the mind's now."""
    return (clock[0] - timedelta(minutes=minutes)).isoformat()


def said(memory, clock, key, text, minutes_ago):
    return memory.ingest({"id": key, "kind": "owner-message", "at": at(clock, minutes_ago), "text": text})["source_id"]


def reached_out(memory, clock, key, text, minutes_ago, origin="proactive"):
    return memory.ingest({"id": "delivery:feishu:" + key + ":accepted", "kind": "delivery", "at": at(clock, minutes_ago),
                          "channel": "feishu", "delivery_id": key, "bubble_id": key, "text": text, "state": "accepted",
                          "message_id": "om-" + key, "origin": origin})["source_id"]


def assessed(jobs, source_id, provider=None, stimulus=None):
    """The action lane's turn for one new event, as the host queues it."""
    kwargs = {"origin": "reflection", "stimulus": stimulus} if stimulus else {}
    job = jobs.enqueue([source_id], "synthetic-v1", **kwargs)
    assert jobs.run_one(provider or Action(), lane="action")["state"] == "complete"
    return job


def enrichments(mind):
    with mind.engine.db.connect() as conn:
        return {row["id"]: (row["state"], row["available"], json.loads(row["data"])) for row in conn.execute(
            "SELECT id,state,available,data FROM mind_appraisals WHERE json_extract(data,'$.stimulus')='memory-enrichment' ORDER BY id")}


def ledger(mind, job_id):
    with mind.engine.db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT outcome,lane FROM mind_appraisal_attempts WHERE appraisal_id=?", (job_id,))]


def test_kin_reaches_out_and_xiaoguang_answers_half_an_hour_later_and_one_request_has_both(setup):
    """Serein's E1: Kin's message and 小光's answer half an hour later were organised apart. The
    contact's enrichment waits for the answer -- an outreach keeps the conversation open -- and
    once the answer has been quiet for twenty minutes one request carries both."""
    mind, source, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    contact = reached_out(memory, clock, "contact-1", "在忙吗？刚看到一只很像你的猫", 60)
    assessed(jobs, contact, stimulus="delivery")
    [(contact_job, (state, available, data))] = enrichments(mind).items()
    assert state == "pending" and data["settle"] is True
    assert available > time.time(), "forty minutes after the outreach it still waits for the answer"
    assert not jobs.runnable("enrichment")

    answer = said(memory, clock, "owner-1", "哈哈哪只猫，刚开完会", 30)
    assessed(jobs, answer)
    rows = enrichments(mind)
    assert len(rows) == 2, "the answer was assessed in its own turn"
    with mind.engine.db.connect() as conn:
        quiet = settling.due(conn, mind.scope.key(), 20)
    assert all(available == quiet for _, available, _ in rows.values()), "both wait for the same quiet"
    assert quiet <= time.time(), "the answer has been quiet for twenty minutes"
    assert jobs.runnable("enrichment")

    organizer = Organizer()
    assert jobs.run_one(organizer, lane="enrichment")["state"] == "complete"
    assert len(organizer.requests) == 1 and {contact, answer} <= set(organizer.requests[0])
    assert {state for state, _, _ in enrichments(mind).values()} == {"complete"}
    assert jobs.run_one(organizer, lane="enrichment")["state"] == "idle"
    assert len(organizer.requests) == 1


def test_each_message_moves_the_wait_and_a_historical_event_does_not(setup):
    mind, source, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    first = said(memory, clock, "owner-1", "今天想去海边", 45)
    assessed(jobs, first)
    [(job_id, (_, available, _))] = enrichments(mind).items()
    assert jobs.runnable("enrichment"), "forty-five minutes of quiet"
    backoff = time.time() + 86400
    with mind.engine.db.connect(write=True) as conn:
        # A job of its own time -- one waiting out a failure, or made with the setting off -- is not moved.
        conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,?,?)",
                     ("enrich_other", mind.scope.key(), "pending", backoff, json.dumps({"evidence_ids": [first], "stimulus": "memory-enrichment"})))
    said(memory, clock, "owner-2", "要不要一起", 5)
    assert enrichments(mind)["enrich_other"][1] == backoff
    assert enrichments(mind)[job_id][1] > time.time() and not jobs.runnable("enrichment"), "the conversation moved on"
    moved = enrichments(mind)[job_id][1]
    memory.ingest({"id": "old-1", "kind": "owner-message", "at": at(clock, 1), "text": "以前说过的话", "historical": True})
    assert enrichments(mind)[job_id][1] == moved, "history replayed is not conversation"
    said(memory, clock, "owner-late", "这句晚到了", 6)
    assert enrichments(mind)[job_id][1] == moved, "a late message moves nothing; the latest is still the latest, history aside"


def test_an_ordinary_reply_does_not_keep_the_conversation_open(setup):
    """Only an outreach waits for its answer; Kin's reply to 小光 is quiet after the setting's minutes."""
    mind, source, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    reply = reached_out(memory, clock, "reply-1", "好呀，那就周六", 30, origin="reply")
    assessed(jobs, reply, stimulus="delivery")
    assert jobs.runnable("enrichment")


def test_waiting_words_are_found_by_lexical_recall(setup):
    """Nothing is organised yet; the raw message is stored and found."""
    mind, source, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    words = said(memory, clock, "owner-1", "周六去看 quokkahill 的日出", 1)
    assessed(jobs, words)
    assert not jobs.runnable("enrichment")
    settle(mind.engine)
    root = mind.engine.source(words)["record_ids"][0]
    found = mind.engine.recall(RecallRequest(scope=mind.scope, query="quokkahill"))
    assert root in {item["id"] for item in found["items"]}


def test_the_jobs_merged_are_one_attempt_and_their_seeds_are_not_used(setup):
    """Two turns whose action lane each left a seed: merged, the request is made afresh for both
    (the seeds were written from half of it), the parent's ledger has its one charged attempt and
    the absorbed row none, and both rows end complete with the parent's result."""
    mind, source, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    first = said(memory, clock, "owner-1", "今天想去海边", 50)
    assessed(jobs, first, Seeding(mind))
    second = said(memory, clock, "owner-2", "还是改天吧，下雨了", 40)
    assessed(jobs, second, Seeding(mind))
    rows = enrichments(mind)
    assert all(data["seed_memory"] for _, _, data in rows.values())
    organizer = Organizer()
    assert jobs.run_one(organizer, lane="enrichment")["state"] == "complete"
    assert len(organizer.requests) == 1 and {first, second} <= set(organizer.requests[0])
    rows = enrichments(mind)
    [parent] = [job for job, (_, _, data) in rows.items() if data.get("batch_ids")]
    [child] = [job for job in rows if job != parent]
    state, _, data = queue_row(mind, parent)
    assert state == "complete" and data["batch_ids"] == [child] and data["own"]["stimulus"] == "memory-enrichment"
    assert "seed_memory" not in data and "settle" not in data
    child_state, child_attempts, child_data = queue_row(mind, child)
    assert child_state == "complete" and child_attempts == 0 and child_data["result"]["batch_id"] == parent
    assert [entry["outcome"] for entry in ledger(mind, parent)] == ["committed"] and ledger(mind, child) == []


def test_a_job_alone_keeps_its_seed(setup):
    mind, source, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    assessed(jobs, said(memory, clock, "owner-1", "今天想去海边", 50), Seeding(mind))
    assert jobs.run_one(Never(), lane="enrichment")["state"] == "complete"
    [(state, _, data)] = enrichments(mind).values()
    assert state == "complete" and data["seed_memory"] and not data.get("batch_ids")


def test_a_merge_stays_within_the_input_budget(setup):
    """Half of a 32k budget is 16k tokens of evidence: a message longer than that runs on its own."""
    mind, source, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20, appraisal_input_budget=32000)
    jobs = Appraisals(mind)
    short = said(memory, clock, "owner-1", "今天想去海边", 50)
    assessed(jobs, short)
    long = said(memory, clock, "owner-2", "海边的风 " * 9000, 40)
    assessed(jobs, long)
    organizer = Organizer()
    assert jobs.run_one(organizer, lane="enrichment")["state"] == "complete"
    assert jobs.run_one(organizer, lane="enrichment")["state"] == "complete"
    assert sorted(map(sorted, organizer.requests)) == sorted([[short], [long]])


def test_off_by_default_every_job_runs_at_once_with_the_data_it_always_had(setup):
    """Unset: no wait, no `settle` mark, no push, and two jobs are two requests."""
    mind, source, clock = setup
    assert DEFAULTS["enrichment_settle_minutes"] == 0
    memory = configured(mind)
    jobs = Appraisals(mind)
    before = time.time()
    first = said(memory, clock, "owner-1", "今天想去海边", 2)
    assessed(jobs, first)
    second = said(memory, clock, "owner-2", "还是改天吧", 1)
    assessed(jobs, second)
    rows = enrichments(mind)
    assert all(set(data) == ENRICHMENT_KEYS and before <= available <= time.time() for _, available, data in rows.values())
    organizer = Organizer()
    assert jobs.run_one(organizer, lane="enrichment")["state"] == "complete"
    assert jobs.run_one(organizer, lane="enrichment")["state"] == "complete"
    assert sorted(map(sorted, organizer.requests)) == sorted([[first], [second]])


def test_the_setting_is_a_whole_number_of_minutes(setup):
    import pytest
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    for wrong in (-1, 1441, 20.5, True, "20"):
        with pytest.raises(ValueError):
            memory.configure({"enrichment_settle_minutes": wrong})
    assert memory.configure({"enrichment_settle_minutes": 20})["enrichment_settle_minutes"] == 20
    assert memory.configure({"enrichment_settle_minutes": 0})["enrichment_settle_minutes"] == 0
