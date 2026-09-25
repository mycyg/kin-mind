"""What is written from a source keeps none of its words once the source is deleted, however the
delete and the write interleave -- an appraisal, a reflection, a creation, an exploration, a daily
review (CR5-MM-01, CR5-MM-02, CR5-MM-09) -- and an attempt that ends before its model call is not
charged (CR5-MM-07).

Each case blocks where the model answers or the write is about to happen, deletes, lets it go on,
and then reads every table of the store for the words."""
import json
from datetime import datetime, timezone

from eventmem.core.db import Conflict
from eventmem.core.models import SourceInput

from kin_mind import attempts
from kin_mind.appraisal import Appraisal, Appraisals, DailyReview
from kin_mind.continuity import Understanding
from kin_mind.creation import accept_result
from kin_mind.erasure import ERASED
from kin_mind.exploration_decisions import SharingDecision
from kin_mind.memory import MemoryContinuity, fingerprint_file
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind', 'test_autonomous_plans')

MARKER = "xyzzyquux"
USAGE = {"input_tokens": 700, "output_tokens": 30}
TARGET = "explore_derived"


def answered(proposal):
    return proposal, {"provider": "deepseek", "model": "synthetic", "request_id": "req-1", "usage": USAGE}


def paid(caller):
    attempts.record_call(caller, "submit_appraisal", outcome="ok", model="synthetic", request_id="req-1", usage=USAGE)


def queue_row(mind, job_id):
    with mind.engine.db.connect() as conn:
        found = conn.execute("SELECT state,attempts,data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()
    return found["state"], found["attempts"], json.loads(found["data"])


def test_a_source_shown_only_for_recall_and_deleted_while_the_model_answers_leaves_no_words(setup):
    """The appraisal's own evidence stays; an earlier message, shown only as recent dialogue, is
    deleted while the model answers, and the answer repeats it. The commit refuses, and the queue
    row keeps the attempt, its cost and its error, with none of the words. The retry asks again in
    full: the proposal the delete emptied is neither reused nor revalidated (CR5-MM-01)."""
    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": f"我小时候住在 {MARKER} 街"})["source_id"]
    primary = source("today", "今天天气不错")

    class Reader:
        def appraise(self, context):
            assert MARKER in json.dumps(context, ensure_ascii=False), "the recall source was shown"
            mind.engine.delete(recalled)
            paid(self)
            return answered(Appraisal(reason=f"想起她住过 {MARKER} 街", values={"curiosity": 66}))

    class Again:
        """The retry: a full call. A light question would be about a proposal the delete emptied."""
        def appraise(self, context):
            assert MARKER not in json.dumps(context, ensure_ascii=False)
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal written from a deleted source is not revalidated")

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["error"] and count == 1
    assert any(ref.get("erased") for ref in data["evaluated_sources"]), "the deleted source stays named, as a tombstone"
    [ledger] = attempts.read(mind.engine, mind.scope.key(), job_id=job["id"])["attempts"]
    assert ledger["charged"] is True and ledger["calls"][0]["usage"] == USAGE
    assert mind.read()["dimensions"]["curiosity"]["value"] != 66, "the commit was refused"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and "tier" not in data, data.get("error")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 55 and ERASED not in json.dumps(mind.read(), ensure_ascii=False)


def test_a_deferred_share_keeps_no_words_of_its_deleted_result(setup):
    """A sharing decision deferred with a condition: deleting the result it was about takes the
    condition as well as the reason, in the state and in every revision of it (CR5-MM-01)."""
    mind, source, clock = setup
    engine = mind.engine
    with engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,"
                     "created_at TEXT NOT NULL,data TEXT NOT NULL)")
    result = engine.receive(SourceInput(namespace="test", key="derived-exploration", scope=mind.scope,
                                        text=f"探索结果：{MARKER} 的来历", authority="model", occurred_at=clock[0].isoformat(),
                                        metadata={"host_event": "exploration-result", "exploration_id": TARGET}))["id"]
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                     (TARGET, mind.scope.key(), "complete", mind.clock(), json.dumps({"source_id": result})))

    class Decider:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="结果先放一放", values={"curiosity": 61}, sharing=[SharingDecision(
                exploration_id=TARGET, decision="defer", reason="等她自己问起",
                reconsider_when=f"她再提到 {MARKER} 的时候")]))

    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([result], "synthetic-v1", origin="exploration", stimulus="exploration-result")
    jobs.run_one(Decider())
    with engine.db.connect() as conn:
        decision = mind._load(conn).get("exploration_decisions", {}).get(TARGET)
    assert decision and MARKER in decision["reconsider_when"], queue_row(mind, job["id"])[2].get("error")
    engine.delete(result)
    settle(engine)
    assert texts_everywhere(engine, MARKER) == set()
    with engine.db.connect() as conn:
        decision = mind._load(conn)["exploration_decisions"][TARGET]
    assert decision["decision"] == "defer" and decision["reconsider_when"] == "[已删除]", "what was decided stays"


def test_a_copied_field_loses_its_words_with_what_it_was_copied_from():
    """A decision's words copied into the wish it settles name what they were written from beside
    them: an erase of that takes the copies and only them; the wish's own words stay (CR5-MM-01)."""
    from kin_mind.erasure import scrub

    gone = "src_" + "a" * 32
    wish = {"id": "desire-1", "content": "弄清那条街的来历", "evidence": [{"source_id": "src_" + "b" * 32, "record_id": "mem_" + "b" * 32}],
            "reason": f"等她提起 {MARKER} 再说", "reason_evidence_ids": [gone],
            "contact_wait": {"condition": "new_evidence", "reason": f"她再提到 {MARKER} 的时候", "reason_evidence_ids": [gone]}}
    erased = scrub(wish, frozenset({gone}))
    assert erased["reason"] == ERASED and erased["contact_wait"]["reason"] == ERASED
    assert erased["content"] == wish["content"] and erased["contact_wait"]["condition"] == "new_evidence"
    assert scrub(erased, frozenset({gone})) is erased, "a second erase changes nothing"
    assert scrub(wish, frozenset({"src_" + "c" * 32})) is wish, "nothing else erases them"


def test_what_a_run_or_a_concern_wrote_goes_and_its_codes_and_ids_stay():
    """The free text of an exploration's run, a creation's review gaps and a concern's target goes
    with what they rest on; codes and identities beside them stay (CR5-MM-01)."""
    from kin_mind.erasure import scrub

    gone = "mem_" + "a" * 32
    run = {"desire_id": "desire-1", "state": "complete", "selected_brief": f"查清 {MARKER} 街的来历", "evidence_ids": [gone],
           "result": {"summary": f"{MARKER} 街建于 1920 年", "findings": [f"{MARKER} 街原名西街"],
                      "sources": [{"url": "https://example.com/street", "title": f"{MARKER} 街志"}],
                      "open_questions": [f"{MARKER} 街为什么改名"]}}
    erased = scrub(run, frozenset({gone}))
    assert MARKER not in json.dumps(erased, ensure_ascii=False)
    assert erased["result"]["sources"][0]["url"] == "https://example.com/street" and erased["state"] == "complete"
    settled = {"evidence_ids": [gone], "verification_gaps": ["completion-not-established", f"还没核对 {MARKER} 的年份"]}
    assert scrub(settled, frozenset({gone}))["verification_gaps"] == ["completion-not-established", ERASED]
    concern = {"id": "concern-1", "kind": "care", "evidence": [{"source_id": "src_" + "a" * 32, "record_id": gone}],
               "target": f"{MARKER} 街的老房子", "error_detail": {"code": "evidence-not-current", "target": gone}}
    erased = scrub(concern, frozenset({gone}))
    assert erased["target"] == ERASED and erased["error_detail"] == concern["error_detail"], "an identity is no word"
