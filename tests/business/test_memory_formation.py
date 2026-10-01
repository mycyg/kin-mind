"""How a conversation becomes memory (kin_mind.memory_formation), each rule behind its own switch.

Off -- unset -- every one of them is the behaviour before it: the request's schema and instructions,
what a commit organises, what the graph writes and the tables the store has. On: an entity is named in
what it cites, the memory instructions carry the faithful-note lines, every conversation source gets a
disposition or is asked about once more and then left to the ledger, and the main session's marks hold
the assessment that organises their sources to them. An erase reaches the skip receipts and the marks.
"""
import asyncio
import inspect
import json
import sqlite3
from datetime import timedelta

import pytest

from eventmem.core.db import digest

from kin_mind import appraisal as A
from kin_mind import lifecycle
from kin_mind import memory_formation as F
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.graph import GraphAssessment, GraphNode
from kin_mind.memory import MemoryAssessment, MemoryContinuity, MemoryNote
from kin_mind.memory_formation import MemorySkip

from test_kin_mind import FakeReviewer

pytest_plugins = ("test_kin_mind",)

MARKER = "lanternquux"
TABLES = (F.SKIPS, F.MARK_TABLE)


class Seeing(FakeReviewer):
    """A reviewer that keeps what each call was shown and the switches it was framed by."""

    def __init__(self, proposal=None):
        super().__init__(proposal or Appraisal(reason="看过了"))
        self.contexts, self.rules = [], []

    def appraise(self, context):
        self.contexts.append(context)
        self.rules.append(frozenset(getattr(self, "memory_rules", ()) or ()))
        return super().appraise(context)


def world(setup, **switches):
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "operational_lanes": True, **switches})
    return mind, memory, clock


def said(memory, clock, key, kind, text):
    """A conversation event as the host records it, and its source."""
    return memory.ingest({"id": key, "kind": kind, "at": clock[0].isoformat(), "text": text, "channel": "wechat"})["source_id"]


def conversation(mind, memory, clock):
    """小光 says something and Kin answers; both are queued as the host queues them."""
    owner = said(memory, clock, "in-1", "owner-message", f"今天我们去看了灯塔 {MARKER}")
    kin = said(memory, clock, "out-1", "assistant-message", "灯塔那么高，你爬上去了吗")
    jobs = Appraisals(mind)
    jobs.enqueue([owner], "synthetic-v1")
    jobs.enqueue([kin], "synthetic-v1", origin="reflection", stimulus="assistant-result")
    return jobs, owner, kin


def rows(mind, sql, *args):
    with mind.engine.db.connect() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def tables(mind):
    return {row["name"] for row in rows(mind, "SELECT name FROM sqlite_master WHERE type='table'")}


def appraisal_rows(mind, prefix):
    return [{**row, "data": json.loads(row["data"])} for row in
            rows(mind, "SELECT id,state,data FROM mind_appraisals WHERE id LIKE ?", prefix + "%")]


def noted(*evidence, key="lighthouse"):
    return MemoryAssessment(notes=[MemoryNote(key=key, title="灯塔", content="她去看了灯塔", evidence_ids=list(evidence))])


# --- Off is the behaviour before ---------------------------------------------------------------------------

def test_unset_switches_leave_the_request_exactly_as_it_was():
    for historical in (False, True):
        for operational in (False, True):
            plain = A.appraisal_schema(operational, historical, (), A.REVIEW_MAX_MINUTES)
            assert "skipped" not in json.dumps(plain) and "MemorySkip" not in json.dumps(plain)
            assert A.appraisal_schema(operational, historical, (), A.REVIEW_MAX_MINUTES, (), disposition=False) == plain
    provider = A.DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "UNSET", timeout=30)
    for stimulus in (None, "memory-enrichment"):
        system = provider._system({"stimulus": stimulus, "state": {}}, None)
        for text in (F.FAITHFUL_NOTE_RULES, F.SKIPPED_PROMPT, F.DISPOSITION_PROMPT, F.MEMORABLE_PROMPT):
            assert text not in system
    framed = A.DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "UNSET", timeout=30)
    framed.memory_rules = F.rules({key: False for key in F.SWITCHES})
    for context in ({"stimulus": None, "state": {}}, {"stimulus": "memory-enrichment", "state": {}}):
        assert framed._system(context, None) == provider._system(context, None)
        assert framed.request_profile(context) == provider.request_profile(context)
        assert not framed._skipped(context)
    # An assessment without skips is dumped, stored and compared as it always was.
    assert "skipped" not in MemoryAssessment().model_dump()
    assert "skipped" not in json.dumps(A.proposal_record(Appraisal(reason="r")))
    assert MemoryAssessment(skipped=[MemorySkip(evidence_id="src_x", reason="greeting")]).model_dump()["skipped"] == [
        {"evidence_id": "src_x", "reason": "greeting"}]


def test_unset_a_conversation_source_nothing_carries_is_organised_as_before(setup):
    mind, memory, clock = world(setup)
    jobs, owner, kin = conversation(mind, memory, clock)
    reviewer = Seeing(Appraisal(reason="看过了", memory=noted(owner)))
    assert jobs.run_one(reviewer, lane="action")["state"] == "complete"
    assert jobs.run_one(reviewer, lane="enrichment")["state"] == "complete"
    assert appraisal_rows(mind, F.FOLLOW_UP_PREFIX) == []
    assert rows(mind, "SELECT * FROM mind_memory_unorganized") == []
    assert not set(TABLES) & tables(mind), "a store with every switch off is the store it was"
    assert all(rules == frozenset() for rules in reviewer.rules)
    with pytest.raises(ValueError, match="Unknown"):
        memory.configure({"memory_dispositions": True})
    with pytest.raises(ValueError, match="boolean"):
        memory.configure({"memory_disposition": "yes"})


# --- 1. An entity is named in what it cites ----------------------------------------------------------------

def entity_world(setup, check):
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "graph": True, "entity_name_check": check})
    owner = said(memory, clock, "in-sea", "owner-message", "今天和阿Ｌｉｎ一起去看海了")
    jobs = Appraisals(mind)
    jobs.enqueue([owner], "synthetic-v1")
    graph = GraphAssessment(nodes=[
        GraphNode(key="lin", kind="entity", title="阿Lin同学", aliases=["阿lin", "林同学"], evidence_ids=[owner], entity_type="person"),
        GraphNode(key="wang", kind="entity", title="小王", aliases=["王老师"], evidence_ids=[owner], entity_type="person")])
    proposal = Appraisal(reason="看海", memory=MemoryAssessment(notes=[
        MemoryNote(key="sea", title="看海", content="她和朋友去看海", evidence_ids=[owner])], graph=graph))
    status = jobs.run_one(FakeReviewer(proposal))
    # The entities this assessment wrote, not the host's own (the owner a runtime event names).
    entities = {json.loads(row["data"])["title"]: json.loads(row["data"]) for row in
                rows(mind, "SELECT data FROM mind_graph_nodes WHERE json_extract(data,'$.kind')='entity' "
                           "AND json_extract(data,'$.assessment_event') IS NOT NULL")}
    return status, entities


def test_an_entity_none_of_whose_names_is_in_its_evidence_is_refused_alone(setup):
    status, entities = entity_world(setup, True)
    assert status["state"] == "complete"
    # NFKC folds the full-width letters and casefold the case: the alias "阿lin" is in the message.
    assert set(entities) == {"阿Lin同学"} and entities["阿Lin同学"]["aliases"] == ["阿lin"], "an unsourced alias is dropped"
    refused = [r for r in status["result"]["rejected_sections"] if r.get("item")]
    assert refused == [{"section": "memory", "item": {"kind": "graph-node", "key": "graph.nodes[1]"},
                        "code": "entity-name-unsourced", "message": "Entity name is not in its evidence"}]
    mind = setup[0]
    assert rows(mind, "SELECT id FROM records WHERE json_extract(data,'$.attributes.key')='sea' AND deleted=0"), "the rest of the section commits"


def test_unset_an_entity_is_written_as_proposed(setup):
    status, entities = entity_world(setup, False)
    assert set(entities) == {"阿Lin同学", "小王"} and entities["阿Lin同学"]["aliases"] == ["阿lin", "林同学"]
    assert not [r for r in (status["result"].get("rejected_sections") or []) if r.get("item")]


def test_names_are_compared_normalized_and_a_stored_alias_stays():
    texts = ["她说：ＡＬＩＣＥ明天来", "完全无关"]
    assert F.verbatim_names("alice", [], texts) == (True, [])
    assert F.verbatim_names("爱丽丝", ["Alice", "小爱"], texts) == (True, ["Alice"])
    assert F.verbatim_names("爱丽丝", ["小爱"], texts, kept=["小爱"]) == (False, ["小爱"]), "a kept alias passes no check"
    assert F.verbatim_names("  ", [""], texts) == (False, [])


# --- 2. Faithful notes ---------------------------------------------------------------------------------------

def test_the_faithful_lines_frame_every_memory_request_and_not_the_digest():
    provider = A.DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "UNSET", timeout=30)
    provider.memory_rules = F.rules({"faithful_notes": True})
    assert F.FAITHFUL_NOTE_RULES in provider._system({"stimulus": "memory-enrichment", "state": {}}, None)
    assert F.FAITHFUL_NOTE_RULES in provider._system({"stimulus": None, "state": {}}, None)
    assert F.FAITHFUL_NOTE_RULES not in provider._system({"stimulus": None, "operational_only": True, "state": {}}, None)
    assert F.SKIPPED_PROMPT not in provider._system({"stimulus": "memory-enrichment", "state": {}}, None)
    assert not provider._skipped({"stimulus": "memory-enrichment"})
    assert F.FAITHFUL_NOTE_RULES not in inspect.getsource(lifecycle), "every digest would be dirty at once"
    # The switches change what frames the request, so a stored proposal is never reused across them.
    plain = A.DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "UNSET", timeout=30)
    assert provider.request_profile({"stimulus": "memory-enrichment", "state": {}})["system"] != \
        plain.request_profile({"stimulus": "memory-enrichment", "state": {}})["system"]


# --- 3. Every conversation source has a disposition ---------------------------------------------------------

def test_a_conversation_source_nothing_carries_is_asked_once_more_with_only_itself(setup):
    mind, memory, clock = world(setup, memory_disposition=True)
    jobs, owner, kin = conversation(mind, memory, clock)
    assert jobs.run_one(Seeing(Appraisal(reason="看过了", memory=noted(owner))), lane="action")["state"] == "complete"
    [enrichment] = appraisal_rows(mind, "enrich_")
    assert sorted(enrichment["data"]["evidence_ids"]) == sorted([owner, kin])
    assert jobs.run_one(Seeing(), lane="enrichment")["state"] == "complete"
    [cover] = appraisal_rows(mind, F.FOLLOW_UP_PREFIX)
    assert cover["id"] == F.FOLLOW_UP_PREFIX + digest([enrichment["id"], "coverage-v1"])[:32]
    assert cover["data"]["evidence_ids"] == [kin] and cover["data"]["stimulus"] == "memory-enrichment"
    assert cover["data"][F.COVERAGE_OF] == enrichment["id"]
    [ledger] = rows(mind, "SELECT source_id,state,attempts,data FROM mind_memory_unorganized")
    assert (ledger["source_id"], ledger["state"], ledger["attempts"]) == (kin, "queued", 0)
    assert json.loads(ledger["data"])["job_id"] == cover["id"]
    [metric] = rows(mind, "SELECT value,data FROM metrics WHERE name=?", F.METRIC)
    assert metric["value"] == 1 and json.loads(metric["data"])["follow_up"] is True
    # The follow-up is shown only the source left over, told so, and offered `memory.skipped`.
    seeing = Seeing(Appraisal(reason="补上", memory=MemoryAssessment(skipped=[MemorySkip(evidence_id=kin, reason="no_lasting_content")])))
    assert jobs.run_one(seeing, lane="enrichment")["state"] == "complete"
    [context] = seeing.contexts
    assert [s["id"] for s in context["new_evidence"]] == [kin]
    assert context["coverage_review"] == {"missing_ids": [kin]} and F.DISPOSITION in seeing.rules[0]
    assert rows(mind, f"SELECT source_id,reason FROM {F.SKIPS}") == [{"source_id": kin, "reason": "no_lasting_content"}]
    assert rows(mind, "SELECT * FROM mind_memory_unorganized") == [], "a skip is a disposition"
    assert len(appraisal_rows(mind, F.FOLLOW_UP_PREFIX)) == 1


def test_what_the_follow_up_leaves_too_goes_to_the_ledger_and_is_not_organised(setup):
    mind, memory, clock = world(setup, memory_disposition=True, operational_lanes=False)
    jobs, owner, kin = conversation(mind, memory, clock)
    # Without the lanes the one assessment writes memory itself: a source left out is not organised.
    assert jobs.run_one(Seeing(Appraisal(reason="看过了", memory=noted(owner))))["state"] == "complete"
    organised = {row["source_id"] for row in rows(mind, "SELECT source_id FROM mind_semantic_sources")}
    assert owner in organised and kin not in organised
    [cover] = appraisal_rows(mind, F.FOLLOW_UP_PREFIX)
    assert jobs.run_one(Seeing())["state"] == "complete"
    assert len(appraisal_rows(mind, F.FOLLOW_UP_PREFIX)) == 1, "a follow-up never asks again"
    [ledger] = rows(mind, "SELECT source_id,state,attempts FROM mind_memory_unorganized")
    assert ledger == {"source_id": kin, "state": "pending", "attempts": 1}
    assert kin not in {row["source_id"] for row in rows(mind, "SELECT source_id FROM mind_semantic_sources")}
    queued = memory.queue_unorganized(jobs, "synthetic-v1")
    assert queued["state"] == "queued" and queued["sources"] == 1
    [backfill] = [row for row in rows(mind, "SELECT data FROM mind_appraisals WHERE id=?", queued["job_id"])]
    assert json.loads(backfill["data"])["stimulus"] == "memory-backfill"


def test_a_carried_routed_or_skipped_source_needs_nothing_more(setup):
    mind, memory, clock = world(setup, memory_disposition=True)
    jobs, owner, kin = conversation(mind, memory, clock)
    proposal = Appraisal(reason="看过了", memory=MemoryAssessment(notes=noted(owner).notes,
                         skipped=[MemorySkip(evidence_id=kin, reason="greeting")]))
    assert jobs.run_one(Seeing(proposal), lane="action")["state"] == "complete"
    assert jobs.run_one(Seeing(), lane="enrichment")["state"] == "complete"
    assert appraisal_rows(mind, F.FOLLOW_UP_PREFIX) == [] and rows(mind, "SELECT * FROM mind_memory_unorganized") == []
    assert rows(mind, f"SELECT source_id,reason FROM {F.SKIPS}") == [{"source_id": kin, "reason": "greeting"}]


def test_internal_sources_and_a_held_sections_follow_up_need_no_disposition(setup):
    mind, memory, clock = world(setup, memory_disposition=True)
    timer = memory.ingest({"id": "task-1", "kind": "task-result", "at": clock[0].isoformat(), "text": "任务完成"})["source_id"]
    jobs = Appraisals(mind)
    jobs.enqueue([timer], "synthetic-v1", origin="reflection", stimulus="runtime-result")
    assert jobs.run_one(Seeing(), lane="action")["state"] == "complete"
    assert jobs.run_one(Seeing(), lane="enrichment")["state"] == "complete"
    assert appraisal_rows(mind, F.FOLLOW_UP_PREFIX) == []
    # A held-sections follow-up restates its parent's refused sections; the parent judged the memory.
    owner = said(memory, clock, "in-held", "owner-message", "一句话")
    nothing_carried = type("Items", (), {"enabled": True, "carriers": {}})()
    with mind.engine.db.connect(write=True) as conn:
        judged = {stimulus: F.settle(conn, mind, assessment=MemoryAssessment(), items=nothing_carried, routes=[],
                                     processed=[{"source_id": owner}], event_id="e", job={"id": "j-" + str(stimulus), "stimulus": stimulus})
                  for stimulus in ("held-sections", None)}
        conn.execute("ROLLBACK")
    assert judged["held-sections"] == F.NOTHING and judged[None].missing == {owner}


def test_the_skipped_field_is_offered_only_while_a_switch_asks_for_it_and_is_strict():
    from test_strict_schema import violations
    from kin_mind.strict_schema import strict_schema
    for historical in (False, True):
        offered = A.appraisal_schema(False, historical, (), A.REVIEW_MAX_MINUTES, (), disposition=True)
        assert "MemorySkip" in offered["$defs"] and "skipped" in offered["$defs"]["MemoryAssessment"]["properties"]
        assert violations(strict_schema(offered)) == []
    assert "skipped" not in json.dumps(A.appraisal_schema(True, False, (), A.REVIEW_MAX_MINUTES, (), disposition=True))
    provider = A.DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "UNSET", timeout=30)
    provider.memory_rules = F.rules({"memory_disposition": True})
    assert provider._skipped({"stimulus": "memory-enrichment"}) and not provider._skipped({"operational_only": True})
    system = provider._system({"stimulus": "memory-enrichment", "state": {}}, None)
    assert F.SKIPPED_PROMPT in system and F.DISPOSITION_PROMPT in system and F.MEMORABLE_PROMPT not in system
    # Not offered, a skip the model sent anyway is not kept.
    proposal = Appraisal(reason="r", memory=MemoryAssessment(skipped=[MemorySkip(evidence_id="src_x", reason="greeting")]))
    assert F.blank_skipped(proposal, frozenset()).memory.skipped == []
    assert F.blank_skipped(proposal, F.rules({"memorable_marks": True})) is proposal


# --- 4. The main session's marks -----------------------------------------------------------------------------

def test_a_mark_holds_the_assessment_that_organises_its_source_and_is_consumed(setup):
    mind, memory, clock = world(setup, memorable_marks=True)
    jobs, owner, kin = conversation(mind, memory, clock)
    # By the owner input's id, as the main session knows it.
    result = F.mark(mind, ["in-1"], "她第一次说起灯塔")
    assert result["state"] == "recorded" and result["marked"] == [owner] and result["quota"]["used"] == 1
    assert F.mark(mind, [owner], "再标一次")["state"] == "already-marked"
    assert jobs.run_one(Seeing(), lane="action")["state"] == "complete"
    seeing = Seeing(Appraisal(reason="记下", memory=noted(owner)))
    assert jobs.run_one(seeing, lane="enrichment")["state"] == "complete"
    [context] = seeing.contexts
    assert context["memorable"] == [{"source_id": owner, "reason": "她第一次说起灯塔"}] and F.MARKS in seeing.rules[0]
    assert "coverage_review" not in context
    [row] = rows(mind, f"SELECT state,event_id FROM {F.MARK_TABLE}")
    assert row["state"] == "consumed" and row["event_id"]
    assert appraisal_rows(mind, F.FOLLOW_UP_PREFIX) == [], "Kin's reply was not marked and the disposition switch is off"


def test_a_marked_source_nothing_carries_gets_its_follow_up(setup):
    mind, memory, clock = world(setup, memorable_marks=True)
    jobs, owner, kin = conversation(mind, memory, clock)
    assert F.mark(mind, [kin], "想记住自己说的话")["state"] == "recorded"
    assert jobs.run_one(Seeing(Appraisal(reason="看过了", memory=noted(owner))), lane="action")["state"] == "complete"
    assert jobs.run_one(Seeing(), lane="enrichment")["state"] == "complete"
    [cover] = appraisal_rows(mind, F.FOLLOW_UP_PREFIX)
    assert cover["data"]["evidence_ids"] == [kin]
    seeing = Seeing(Appraisal(reason="补上", memory=noted(kin, key="reply")))
    assert jobs.run_one(seeing, lane="enrichment")["state"] == "complete"
    assert seeing.contexts[0]["memorable"] == [{"source_id": kin, "reason": "想记住自己说的话"}]
    assert rows(mind, f"SELECT state FROM {F.MARK_TABLE}") == [{"state": "consumed"}]


def test_marks_are_refused_without_a_pass_to_come_over_quota_and_expire(setup):
    mind, memory, clock = world(setup, memorable_marks=True)
    jobs, owner, kin = conversation(mind, memory, clock)
    loose = said(memory, clock, "in-loose", "owner-message", "一句没有排队的话")
    assert F.mark(mind, [loose, "src_" + "0" * 32, "in-unknown"], "想记住")["refused"] == [
        {"id": "src_" + "0" * 32, "code": "evidence-unavailable"}, {"id": "in-unknown", "code": "evidence-unavailable"},
        {"id": loose, "source_id": loose, "code": "no-pending-assessment"}]
    sources = []
    for index in range(F.MARK_QUOTA + 1):
        sid = said(memory, clock, f"in-q{index}", "owner-message", f"第 {index} 句")
        jobs.enqueue([sid], "synthetic-v1")
        sources.append(sid)
    first = F.mark(mind, sources[:F.MARK_QUOTA - 1], "一起记住")
    assert len(first["marked"]) == F.MARK_QUOTA - 1
    second = F.mark(mind, sources[F.MARK_QUOTA - 1:], "还有这些")
    assert len(second["marked"]) == 1 and [r["code"] for r in second["refused"]] == ["quota"]
    clock[0] += timedelta(hours=F.MARK_WINDOW_HOURS, minutes=1)
    assert F.mark(mind, [sources[-1]], "过了一个窗口")["state"] == "recorded"
    clock[0] += timedelta(hours=F.MARK_TTL_HOURS)
    with mind.engine.db.connect() as conn:
        assert F.open_marks(conn, mind.scope.key(), set(sources), mind.clock()) == {}
    F.mark(mind, [owner], "过期后")
    assert {row["state"] for row in rows(mind, f"SELECT state FROM {F.MARK_TABLE} WHERE source_id<>?", owner)} == {"expired"}
    with pytest.raises(ValueError):
        F.mark(mind, [], "空")
    with pytest.raises(ValueError):
        F.mark(mind, [owner], "")


def test_unset_marks_are_refused_and_nothing_is_written(setup):
    mind, memory, clock = world(setup)
    jobs, owner, kin = conversation(mind, memory, clock)
    assert F.mark(mind, [owner], "想记住") == {"state": "disabled"}
    assert not set(TABLES) & tables(mind)


def test_the_main_session_marks_through_its_tool(setup):
    from eventmem.core.mcp import create_mcp
    mind, memory, clock = world(setup, memorable_marks=True)
    jobs, owner, kin = conversation(mind, memory, clock)
    server = create_mcp(mind.engine)
    [tool] = [t for t in server._tool_manager.list_tools() if t.name == "mark_memorable"]
    assert tool.description == F.MARK_TOOL_DESCRIPTION
    result = asyncio.run(server.call_tool("mark_memorable", {"scope": mind.scope.model_dump(), "evidence_ids": [owner],
                                                             "reason": "想记住"}))
    answer = result[1] if isinstance(result, tuple) else json.loads(result[0].text)
    assert answer["state"] == "recorded" and answer["marked"] == [owner]


# --- Erasure ---------------------------------------------------------------------------------------------------

def test_an_erase_takes_the_skip_receipts_and_every_row_of_a_mark_with_its_reason(setup):
    mind, memory, clock = world(setup, memory_disposition=True, memorable_marks=True)
    jobs, owner, kin = conversation(mind, memory, clock)
    other = said(memory, clock, "in-2", "owner-message", "另一件事")
    jobs.enqueue([other], "synthetic-v1")
    assert F.mark(mind, [owner, other], f"想记住 {MARKER} 这一段")["state"] == "recorded"
    proposal = Appraisal(reason="看过了", memory=MemoryAssessment(notes=noted(owner).notes, skipped=[
        MemorySkip(evidence_id=kin, reason="greeting"), MemorySkip(evidence_id=other, reason="no_lasting_content")]))
    assert jobs.run_one(Seeing(proposal), lane="action")["state"] == "complete"
    assert jobs.run_one(Seeing(), lane="enrichment")["state"] == "complete"
    assert {r["state"] for r in rows(mind, f"SELECT state FROM {F.MARK_TABLE}")} == {"consumed"}
    assert {r["source_id"] for r in rows(mind, f"SELECT source_id FROM {F.SKIPS}")} == {kin, other}
    mind.engine.delete(owner)
    assert rows(mind, f"SELECT * FROM {F.MARK_TABLE}") == [], "the mark goes with every source it named"
    with sqlite3.connect(mind.engine.db.path) as conn:
        assert not conn.execute(f"SELECT 1 FROM {F.MARK_TABLE} WHERE instr(reason,?)>0", (MARKER,)).fetchone()
    mind.engine.delete(kin)
    assert [r["source_id"] for r in rows(mind, f"SELECT source_id FROM {F.SKIPS}")] == [other]
    with mind.engine.db.connect(write=True) as conn:
        assert F.erase(conn, {owner, kin}) == 0, "a second erase finds nothing"
