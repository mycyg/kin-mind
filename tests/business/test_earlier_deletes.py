"""A delete made before a model began is none of its business (CL6E-MM-02), and a commit refused
for a delete made while it answered costs the row nothing (CL6E-MM-03).

The state keeps the tombstone reference of every delete for good -- a wish whose message was
deleted, abandoned or not -- and a fork's tool that reads the state returns those ids. Nothing of
the words is left anywhere by then. So what a draft, an appraisal, a daily review, an exploration
or a creation had before it is checked against the deletes after its claim or its start
(`tombstone_mark`), never against the ones before: counted, the first delete would refuse every
draft and every answer after it, a draft every five minutes for good. A delete after the start
still refuses, as before.

A commit refused because something the model had was deleted while it answered was refused for
the owner's delete, not for the judgment: it spends none of the row's charged attempts and makes no
repeated-failure signature, and a counter and a backoff of its own bound it."""
import json
import time
from datetime import datetime, timedelta, timezone

from eventmem.core.db import NAMED
from eventmem.core.models import SourceInput

from kin_mind import attempts
from kin_mind.appraisal import (DELETION_RETRY_SECONDS, MAX_CHARGED_ATTEMPTS, MAX_DELETION_REFUSALS, Appraisal,
                                Appraisals, DailyReview)
from kin_mind.continuity import Understanding
from kin_mind.context import Contexts
from kin_mind.creation import accept_result
from kin_mind.erasure import ERASED
from kin_mind.memory import MemoryContinuity, fingerprint_file
from kin_mind.recovery import recover_quarantined
from test_derived_erasure import (Review, answered, artifact_kept, paid, prospective_check, queue_row, read_note,
                                  state_wish, tool_read)
from test_erasure import settle, system, texts_everywhere  # noqa: F401  (`system`: the compression fixture)
from test_fork_reads import contact_row, fork_receipt, stored_wish
from test_kin_mind import wish

pytest_plugins = ('test_kin_mind', 'test_autonomous_plans')

MARKER = "brambleton"


def state_ids(mind):
    """What the host names beside a draft with the memory context off, the default: every id of the
    state as read (mind-host.mjs `contactDraft`, `shown`)."""
    return sorted(set(NAMED.findall(json.dumps(mind.read(), ensure_ascii=False))))


def test_a_delete_from_before_the_claim_stops_no_draft_though_the_state_still_names_it(setup):
    """A wish's message was deleted long ago; the state keeps its tombstone reference and the host
    names it beside every draft, as its fork's tool returns it. A note the draft was shown is
    deleted after the claim: that draft is refused. Five minutes later the wish is drafted again,
    named the same state, the note's tombstone and all, and nothing deleted since the claim: the
    draft is kept, and the row names neither delete (CL6E-MM-02)."""
    mind, source, clock = setup
    old = wish(mind, source, "old-wish", content="Ask about the old trip")["desire_id"]
    old_source = stored_wish(mind, old)["evidence"][0]["source_id"]
    mind.engine.delete(old_source)
    settle(mind.engine)
    assert old_source in state_ids(mind), "the state keeps the tombstone reference"
    seen_source, seen_record = read_note(mind, clock, "seen-note", f"她说周末去 {MARKER} 爬山")
    ready = wish(mind, source, "ready-wish", content="Ask about the hike")["desire_id"]
    attempt = mind.claim_contact(owner_epoch="owner-1")
    assert attempt["desire_ids"] == [ready]
    mind.engine.delete(seen_source)
    refused = mind.settle_contact(attempt_id=attempt["id"], state="pending", text=f"{MARKER} 爬得累吗",
                                  shown_ids=[*state_ids(mind), seen_source, seen_record], draft_receipt=fork_receipt(old_source))
    assert refused["state"] == "canceled" and refused["reason"] == "draft-sources-deleted"
    assert seen_record in refused["evaluated_ids"] and old_source not in refused["evaluated_ids"]
    clock[0] += timedelta(seconds=301)
    assert mind.reconsider_contacts(owner_epoch="owner-1")["resumed"] == [ready]
    again = mind.claim_contact(owner_epoch="owner-1")
    kept = mind.settle_contact(attempt_id=again["id"], state="pending", text="周末爬山累吗",
                               shown_ids=[*state_ids(mind), seen_source, seen_record],
                               draft_receipt=fork_receipt(old_source, seen_record))
    assert kept["state"] == "pending", kept.get("reason")
    assert not {old_source, seen_source, seen_record} & set(kept["evaluated_ids"])
    assert contact_row(mind, again["id"])[1]["text_excerpt"] == "周末爬山累吗"


def test_a_send_of_unknown_outcome_whose_sources_were_deleted_before_the_claim_holds_up_no_draft(setup):
    """A send of unknown outcome names what its draft had before it until it is reconciled, which
    has no deadline, and every later draft is told of it. One of those is deleted before a claim:
    that draft is not held up. One deleted after a claim still refuses that draft, and the next is
    kept (CL6E-MM-02)."""
    mind, source, clock = setup
    early_source, early_record = read_note(mind, clock, "unknown-early", f"她的猫叫 {MARKER}")
    late_source, late_record = read_note(mind, clock, "unknown-late", "她的猫三岁了")
    wish(mind, source, "unknown-wish", content="Ask about the cat")
    first = mind.claim_contact(owner_epoch="owner-1")
    mind.settle_contact(attempt_id=first["id"], state="pending", text=f"{MARKER} 今天乖吗",
                        shown_ids=[early_source, early_record, late_source, late_record], draft_receipt=fork_receipt())
    mind.settle_contact(attempt_id=first["id"], state="unconfirmed", reason="timeout")
    mind.engine.delete(early_source)
    settle(mind.engine)
    [fact] = mind.read()["contact_unconfirmed"]
    assert fact["excerpt"] == ERASED and early_record in fact["excerpt_evidence_ids"]
    wish(mind, source, "next-wish", content="Ask how the day went")
    second = mind.claim_contact(owner_epoch="owner-1")
    assert {early_record, late_record} <= set(second["unconfirmed"][0]["excerpt_evidence_ids"])
    mind.engine.delete(late_source)
    refused = mind.settle_contact(attempt_id=second["id"], state="pending", text="今天过得怎么样",
                                  shown_ids=state_ids(mind), draft_receipt=fork_receipt())
    assert refused["state"] == "canceled" and refused["reason"] == "draft-sources-deleted"
    assert late_record in refused["evaluated_ids"] and early_record not in refused["evaluated_ids"]
    clock[0] += timedelta(seconds=301)
    mind.reconsider_contacts(owner_epoch="owner-1")
    third = mind.claim_contact(owner_epoch="owner-1")
    kept = mind.settle_contact(attempt_id=third["id"], state="pending", text="今天过得怎么样",
                               shown_ids=state_ids(mind), draft_receipt=fork_receipt())
    assert kept["state"] == "pending", kept.get("reason")
    assert not {early_source, early_record, late_source, late_record} & set(kept["evaluated_ids"])


def test_an_appraisal_shown_an_earlier_delete_by_the_state_and_its_tools_commits_with_its_words(setup):
    """A wish's message was deleted before the appraisal began: the state it is shown keeps the
    tombstone reference, and the fork's tool, reading the state, returned it. The commit goes
    through, the reflection is kept, and the queue row keeps the proposal as written; the row names
    neither (CL6E-MM-02)."""
    mind, source, clock = setup
    said = state_wish(mind, source)
    said_record = mind.engine.source(said)["record_ids"][0]
    mind.engine.delete(said)
    settle(mind.engine)
    primary = source("walk", "今天去散步了")
    record = mind.engine.source(primary)["record_ids"][0]

    class Thinker:
        def appraise(self, context):
            assert said in json.dumps(context["state"]), "the state names the tombstone"
            paid(self)
            proposal, receipt = answered(Appraisal(reason="散步很舒服", values={"curiosity": 58}, understanding=Understanding(
                meaning="散步的时候想起很多事", topic="散步", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))
            return proposal, {**receipt, **tool_read(said, said_record)}

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Thinker())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete", data.get("error_detail")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 58
    assert data["proposed_result"]["reason"] == "散步很舒服"
    assert not {said, said_record} & set(data["evaluated_ids"])
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone(), "the reflection is kept"


def test_a_source_the_state_named_deleted_while_the_context_is_put_together_stops_the_commit(setup, monkeypatch):
    """The other way round: the state was read with a wish written from a message, and the message is
    deleted while the rest of the context is put together, before the model is asked. The context
    holds the wish's words, and the message is held no longer when the context is done: it is named
    all the same, as deleted since the attempt began. The commit refuses and no word is left
    (CL6E-MM-02)."""
    from kin_mind import appraisal
    from test_derived_erasure import MARKER as SAID

    mind, source, clock = setup
    said = state_wish(mind, source)
    primary = source("today", "今天天气不错")
    real, deleted = appraisal.recent_dialogue, []

    def meanwhile(of):
        if not deleted:
            deleted.append(mind.engine.delete(said))
        return real(of)

    monkeypatch.setattr(appraisal, "recent_dialogue", meanwhile)

    class Reader:
        def appraise(self, context):
            assert SAID in json.dumps(context["state"], ensure_ascii=False), "the state was read before the delete"
            paid(self)
            return answered(Appraisal(reason=f"还想陪她去 {SAID}", values={"curiosity": 66}))

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    settle(mind.engine)
    state, count, data = queue_row(mind, job["id"])
    assert deleted and state != "complete" and data["error_detail"]["code"] == "shown-deleted", data.get("error_detail")
    assert said in data["evaluated_ids"] and mind.read()["dimensions"]["curiosity"]["value"] != 66
    assert texts_everywhere(mind.engine, SAID) == set()


def test_a_stored_proposal_whose_fork_read_an_earlier_delete_goes_to_the_reuse_decision_with_its_words(setup, monkeypatch):
    """A proposal kept after a conflict it may be reused over; its fork's tool returned the
    tombstone of a note deleted before it was asked. The row keeps the proposal as written, with the
    mark its attempt began at, and the next attempt hands it to the reuse decision instead of
    asking in full. Deleted after its attempt began, the note keeps it from there, as before
    (test_derived_erasure) (CL6E-MM-02)."""
    from eventmem.core.models import RevisionInput

    from kin_mind import revalidation

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": "昨天说的一句话"})
    early_source, early_record = read_note(mind, clock, "early-read", "早先删掉的一条")
    mind.engine.delete(early_source)
    settle(mind.engine)
    primary = source("today", "今天天气不错")

    class Reader:
        def appraise(self, context):
            record = mind.engine.get(recalled["record_id"])
            mind.engine.revise(recalled["record_id"], RevisionInput(expected_revision=record["revision"], command_id="fix-earlier",
                                                                    action="correct", content="昨天说的另一句话", reason="更正"))
            paid(self)
            proposal, receipt = answered(Appraisal(reason="想出去走走", values={"curiosity": 66}))
            return proposal, {**receipt, **tool_read(early_record)}

    decided = []

    def reuse(jobs, provider, row, data, context, stored, **kwargs):
        decided.append(stored)
        return None  # What revalidation decides from here is its own business: asked in full.

    class Again:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["reuse"]["proposal"]["reason"] == "想出去走走", data.get("error_detail")
    assert data["reuse"]["tombstone_mark"] == data["tombstone_mark"] and early_record not in data["evaluated_ids"]
    monkeypatch.setattr(revalidation, "resume", reuse)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    assert [stored.proposal["reason"] for stored in decided] == ["想出去走走"]
    assert queue_row(mind, job["id"])[0] == "complete"


def test_a_daily_review_whose_fork_read_an_earlier_delete_records_its_change(setup):
    """With the behaviour chain off the day is reviewed in the fork. The state it is shown keeps the
    tombstone reference of a message deleted before the review began, and its tool returned it. The
    change is recorded and the reason kept (CL6E-MM-02)."""
    from kin_mind.state import Evolution

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"behavior_chain": False, "trait_ledger": False})
    claim, assessment = prospective_check(mind, source, clock)
    said = state_wish(mind, source)
    said_record = mind.engine.source(said)["record_ids"][0]
    mind.engine.delete(said)
    settle(mind.engine)
    baseline = mind.read()["dimensions"]["curiosity"]["baseline"]

    class Daily:
        def appraise(self, context):
            assert said in json.dumps(context["state"]), "the state names the tombstone"
            proposal, receipt = answered(Appraisal(reason="更想去查证", evolution=Evolution(
                claim_id=claim["id"], assessment_id=assessment["id"], baseline_changes={"curiosity": baseline + 2})))
            return proposal, {**receipt, **tool_read(said, said_record)}

    reviewed = DailyReview(mind).run(Daily(), "synthetic-v1")
    assert reviewed["state"] == "complete" and reviewed["reason"] == "更想去查证", reviewed.get("error")
    assert "reason_withheld" not in reviewed and not {said, said_record} & set(reviewed["evaluated_ids"])
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == baseline + 2, "the change was recorded"


def explore_again(env, tmp_path, key):
    """Another planned exploration, whose runner hands back what its brief showed of the earlier ones."""
    from kin_mind.exploration import Explorations
    from test_autonomous_plans import create, decide

    mind, plans, _, _, _ = env
    decide(env, create(env, actor="explore", key=key))
    plans.sync_wishes()
    shown = []

    def runner(executable, brief, directory, **kwargs):
        shown.extend(brief["previous_explorations"])
        return {"state": "complete", "partial": False, "attempt": 1,
                "result": {"summary": f"又查到了 {MARKER} 的来历", "findings": [f"{MARKER} 在河边"],
                           "sources": [], "open_questions": [], "suggested_share": None}}
    return Explorations(mind).run("codex", str(tmp_path / key), "planning-v1", runner=runner), shown


def test_an_exploration_keeps_its_report_though_an_earlier_one_its_brief_names_was_deleted(env, tmp_path):
    """An earlier exploration's report was deleted; the run keeps the id of the source it was stored
    as, and the next brief shows that run among the earlier ones. The next report is kept with its
    words, derived from its wish's evidence (CL6E-MM-02)."""
    from test_derived_erasure import explore

    mind = env[0]
    first = explore(env, tmp_path)
    assert first["state"] == "complete"
    with mind.engine.db.connect(write=True) as conn:
        # The appraisal of the first result is done: the next exploration does not wait for it.
        conn.execute("UPDATE mind_action_events SET state='complete' WHERE state IN ('pending','queued')")
    mind.engine.delete(first["source_id"])
    settle(mind.engine)
    ran, shown = explore_again(env, tmp_path, "second")
    assert any(item["id"] == first["id"] for item in shown), ran
    assert ran["state"] == "complete" and "inputs_withheld" not in ran, ran.get("inputs_withheld")
    assert MARKER in mind.engine.source(ran["source_id"], content=True).read_text()
    assert ran["result"]["summary"] == f"又查到了 {MARKER} 的来历"
    assert all(ref.get("source_id") != first["source_id"] for ref in ran["evaluated_sources"])


class ReadingReview(Review):
    """The completion review in a fork whose tool, reading the state, returned these ids."""

    def __init__(self, *ids):
        super().__init__()
        self.ids = ids

    def structured(self, name, schema, system, context, **kwargs):
        value, receipt = super().structured(name, schema, system, context, **kwargs)
        return value, {**receipt, **tool_read(*self.ids)}


def test_a_creation_keeps_its_result_though_an_earlier_exploration_its_brief_names_was_deleted(env, tmp_path):
    """The creator's brief shows the earlier explorations; one of their reports was deleted before
    the claim. The result is kept as a derived source and the settled run keeps its words; the run
    names neither the deleted report nor anything deleted before its claim (CL6E-MM-02)."""
    from kin_mind import host
    from test_autonomous_plans import create, decide
    from test_derived_erasure import explore

    mind = env[0]
    first = explore(env, tmp_path)
    mind.engine.delete(first["source_id"])
    settle(mind.engine)
    decide(env, create(env, key="clock"))
    claimed = host.dispatch({"root": str(tmp_path), "scope": mind.scope.model_dump()}, "plan-claim", {"actor": "create", "owner": "worker"})
    assert claimed["state"] == "claimed"
    # The host's process keeps real time; the fixture's clock catches up with it.
    env[3][0] = datetime.now(timezone.utc)
    assert any(item["id"] == first["id"] for item in claimed["brief"]["previous_explorations"])
    root = tmp_path / "creator"
    root.mkdir()
    made = root / "clock.json"
    made.write_text(json.dumps({"face": f"{MARKER} 的钟"}, ensure_ascii=False))
    run = claimed["run"]
    request = {"run_id": run["id"], "owner": "worker", "fence": run["fence"], "result": {
        "state": "produced", "summary": f"做好了 {MARKER} 的钟", "remaining": [], "verification": ["Parsed JSON"],
        "artifacts": [fingerprint_file(made)],
        "receipt": {"model": "gpt-6-astra", "run_id": run["id"], "thread_id": "isolated-native", "exit_code": 0, "workspace": str(root)}}}
    config = {"creation_directory": str(root), "creation_model": "gpt-6-astra", "agent_version": "planning-v1"}
    settled = accept_result(mind, config, request, ReadingReview(first["source_id"]))
    assert settled["state"] == "completed"
    [event] = artifact_kept(mind)
    assert "inputs_withheld" not in event and event["artifact"]["inspection"]["excerpt"] != ERASED
    assert settled["result"]["summary"] == f"做好了 {MARKER} 的钟"
    assert settled["result"]["completion_review"]["reason"].endswith("的钟做好了"), "the review keeps its words"
    assert all(ref.get("source_id") != first["source_id"] for ref in settled["result"]["evaluated_sources"])


def test_a_daily_review_counts_a_source_only_the_state_named_deleted_while_it_answers(setup):
    """The other way round: a message behind a wish in the state the review is shown is deleted
    while it answers, and nothing but the state named it. Held no longer when the answer is back,
    it is still named, as deleted since the review began: no change is recorded and no reason kept
    (CL6E-MM-02)."""
    from kin_mind.state import Evolution

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"behavior_chain": False, "trait_ledger": False})
    claim, assessment = prospective_check(mind, source, clock)
    said = state_wish(mind, source)
    baseline = mind.read()["dimensions"]["curiosity"]["baseline"]

    class Daily:
        def appraise(self, context):
            assert said in json.dumps(context["state"]) and MARKER not in json.dumps(context["new_evidence"], ensure_ascii=False)
            mind.engine.delete(said)
            return answered(Appraisal(reason="想陪她出去看看", evolution=Evolution(
                claim_id=claim["id"], assessment_id=assessment["id"], baseline_changes={"curiosity": baseline + 2})))

    reviewed = DailyReview(mind).run(Daily(), "synthetic-v1")
    settle(mind.engine)
    assert reviewed["state"] == "needs-review" and reviewed["error"] == "Conflict"
    assert reviewed["reason_withheld"] == "sources-changed" and said in reviewed["evaluated_ids"]
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == baseline, "no change was recorded"


def test_an_enrichment_seed_whose_parent_fork_read_an_earlier_delete_is_used_as_it_is(setup):
    """The action lane commits and hands the memory its model proposed to an enrichment row; its
    fork's tool had returned the tombstone of a note deleted before it was asked. The enrichment
    uses the seed as it always has, with no model call, and the row carries the parent's mark
    (CL6E-MM-02)."""
    from kin_mind.memory import MemoryAssessment, MemoryNote

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True, "semantic": True, "operational_lanes": True})
    early_source, early_record = read_note(mind, clock, "early-seed", "早先删掉的一条")
    mind.engine.delete(early_source)
    settle(mind.engine)
    primary = source("today", "今天天气不错")
    record = mind.engine.source(primary)["record_ids"][0]

    class Reader:
        def appraise(self, context):
            paid(self)
            proposal, receipt = answered(Appraisal(reason="看了看今天", values={"curiosity": 61}, memory=MemoryAssessment(notes=[
                MemoryNote(key="today", title="今天", content=f"今天她说起 {MARKER}", evidence_ids=[record])])))
            return proposal, {**receipt, **tool_read(early_record)}

    class Never:
        def appraise(self, context):
            raise AssertionError("the seed is used as it is")

    jobs = Appraisals(mind)
    parent = jobs.enqueue([primary], "synthetic-v1")
    assert jobs.run_one(Reader(), lane="action")["state"] == "complete"
    with mind.engine.db.connect() as conn:
        [(enrichment, data)] = conn.execute("SELECT id,data FROM mind_appraisals WHERE id LIKE 'enrich_%'").fetchall()
    data = json.loads(data)
    assert data["seed_tombstone_mark"] == queue_row(mind, parent["id"])[2]["tombstone_mark"]
    assert jobs.run_one(Never(), lane="enrichment")["state"] == "complete"
    state, count, data = queue_row(mind, enrichment)
    assert state == "complete" and not data.get("seed_rejected")
    assert MARKER in json.dumps(data["seed_memory"], ensure_ascii=False)


def test_what_a_sharing_repair_or_a_light_question_read_of_an_earlier_delete_is_not_named(setup):
    """A sharing repair's fork and a light question's fork returned the tombstone of a note deleted
    before the attempt began, beside one that stands: the commit goes through, and each names only
    what stands (CL6E-MM-02)."""
    from kin_mind import revalidation
    from kin_mind.erasure import tombstone_mark
    from kin_mind.exploration_decisions import SharingDecision
    from test_derived_erasure import TARGET, USAGE

    mind, source, clock = setup
    engine = mind.engine
    early_source, early_record = read_note(mind, clock, "early-share", "早先删掉的一条")
    stands_source, stands_record = read_note(mind, clock, "stands-share", "还在的一条")
    engine.delete(early_source)
    settle(engine)
    with engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,"
                     "created_at TEXT NOT NULL,data TEXT NOT NULL)")
    result = engine.receive(SourceInput(namespace="test", key="shared-exploration", scope=mind.scope,
                                        text="探索结果：那座桥的来历", authority="model", occurred_at=clock[0].isoformat(),
                                        metadata={"host_event": "exploration-result", "exploration_id": TARGET}))["id"]
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                     (TARGET, mind.scope.key(), "complete", mind.clock(), json.dumps({"source_id": result})))

    class Repairs:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="看了探索结果", values={"curiosity": 62}))

        def repair_sharing(self, proposal, context):
            attempts.record_call(self, "repair_sharing", outcome="ok", model="synthetic", request_id="req-2", usage=USAGE)
            return ([SharingDecision(exploration_id=TARGET, decision="keep", reason="等她想听的时候再说")],
                    {"provider": "deepseek", "model": "synthetic", "request_id": "req-2", "usage": USAGE,
                     **tool_read(early_record, stands_record)["native_receipt"]})

    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([result], "synthetic-v1", origin="exploration", stimulus="exploration-result")
    jobs.run_one(Repairs())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete", data.get("error_detail")
    assert stands_record in data["evaluated_ids"] and early_record not in data["evaluated_ids"]

    light = jobs.enqueue([source("light", "今天早上下雨")], "synthetic-v1")
    with engine.db.connect(write=True) as conn:
        row = {"attempt_token": "token-1", "proposed_result": {"reason": "原来的提案"}, "tombstone_mark": tombstone_mark(conn)}
        conn.execute("UPDATE mind_appraisals SET state='running',data=? WHERE id=?", (json.dumps(row), light["id"]))

    class Light:
        timeout = 60

        def structured(self, name, schema, system, request, **kwargs):
            answer = revalidation.Revalidation(items=[revalidation.RevalidationItem(conflict_id="c1", verdict="replan", reason="要重新想")])
            return answer, {"model": "synthetic", **tool_read(early_record, stands_record)["native_receipt"]}

    stored = revalidation.Stored("reuse", {"reason": "原来的提案"}, {}, [], [], None, {}, 1, None, False)
    entries = [{"public": {"conflict_id": "c1", "kind": "evidence", "object": "x"}, "paths": []}]
    try:
        revalidation._revalidate(jobs, Light(), {"id": light["id"]}, row, stored, entries, {})
        raise AssertionError("a replan is refused")
    except RuntimeError:
        pass
    assert row["revalidation"]["evaluated_ids"] == [stands_record]


def test_a_compression_whose_tools_returned_an_earlier_delete_is_kept(system):
    """An appraisal's evidence compressed in the fork, whose tool returned the tombstone of a note
    deleted before the pack began: the compression is kept. A note its tool returned that is
    deleted while it compresses keeps it from being kept, as before (CL6E-MM-02)."""
    mind, memory, source, clock = system
    engine = mind.engine
    filler = "They checked the harbour path, the tide table and the lamps along the pier. " * 12
    contexts = Contexts(mind)
    early = source("early-note", "早先删掉的一条")
    early_record = engine.source(early)["record_ids"][0]
    during = source("during-note", "压缩时删掉的一条")
    during_record = engine.source(during)["record_ids"][0]
    engine.delete(early)
    settle(engine)

    def compress(key, meanwhile=lambda: None, *read):
        items = [contexts.record_item(engine.get(engine.source(source(f"{key}-{i}", f"Harbour walk note {key} {i}. " + filler))["record_ids"][0]))
                 for i in range(3)]

        class Fork:
            model = "synthetic-fork"

            def structured(self, name, schema, system, payload, **kwargs):
                meanwhile()
                value = schema.model_validate({"entries": [{"item_ids": payload["allowed_item_ids"], "summary": "They walked the harbour."}],
                                               "omitted_ids": []})
                return value, {"model": self.model, "channel": "fork", "tool_calls": [
                    {"name": "memorypalace.read_affective_state", "ok": True, "ids": [{"id": identifier} for identifier in read]}]}

        return contexts.pack(items, "harbour walk " + key, 600, provider=Fork(), allow_model=True, persist=True)

    def cached():
        with engine.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM mind_context_cache").fetchone()[0]

    kept = compress("early", lambda: None, early, early_record)
    assert kept["state"] == "compressed" and cached() > 0
    count = cached()
    refused = compress("during", lambda: engine.delete(during), during_record)
    assert refused["state"] != "compressed" and cached() == count


def test_a_commit_refused_for_a_delete_made_while_the_model_answered_is_not_charged(setup):
    """Each attempt's fork reads a note that is deleted while it answers, and the commit is refused
    for it: more times than the row has charged attempts. None is charged -- the attempt count stays
    0, the ledger keeps each call's cost and says it was not charged -- and none makes a repeated
    failure. Each waits longer for the deletes to end, and past a bound of its own the row is set
    aside, as any exhausted wait is (CL6E-MM-03)."""
    mind, source, clock = setup
    notes = [read_note(mind, clock, f"refused-{n}", f"第 {n} 条") for n in range(MAX_DELETION_REFUSALS + 1)]
    primary = source("today", "今天天气不错")
    turn = [0]

    class Deleting:
        def appraise(self, context):
            note_source, note = notes[turn[0]]
            turn[0] += 1
            mind.engine.delete(note_source)
            paid(self)
            proposal, receipt = answered(Appraisal(reason="平常的一天", values={"curiosity": 60}))
            return proposal, {**receipt, **tool_read(note)}

    def available(job_id):
        with mind.engine.db.connect() as conn:
            return conn.execute("SELECT available FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()[0]

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    assert MAX_DELETION_REFUSALS > MAX_CHARGED_ATTEMPTS
    for n in range(1, MAX_DELETION_REFUSALS + 1):
        jobs.run_one(Deleting())
        state, count, data = queue_row(mind, job["id"])
        assert state == "pending" and count == 0 and data["deletion_refusals"] == n, data.get("repair_reason")
        assert jobs.status(job["id"])["deletion_refusals"] == n, "the operator's view says why it waits"
        assert data["error_detail"]["code"] == "shown-deleted" and "error_repeats" not in data
        wait = available(job["id"]) - time.time()
        assert abs(wait - min(1800, DELETION_RETRY_SECONDS * 2 ** (n - 1))) < 60, (n, wait)
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    ledger = attempts.read(mind.engine, mind.scope.key(), job_id=job["id"])["attempts"]
    assert len(ledger) == MAX_DELETION_REFUSALS
    assert all(entry["charged"] is False and entry["calls"] for entry in ledger)
    assert mind.read()["dimensions"]["curiosity"]["value"] != 60, "no commit"
    jobs.run_one(Deleting())
    state, count, data = queue_row(mind, job["id"])
    assert state == "needs-repair" and count == 0
    assert data["repair_reason"] == f"deletion-refusals-exhausted:{MAX_DELETION_REFUSALS + 1}"
    with mind.engine.db.connect() as conn:
        [metric] = [json.loads(row[0]) for row in conn.execute("SELECT data FROM metrics WHERE name='appraisal_quarantined'")]
    assert metric["deletion_refusals"] == MAX_DELETION_REFUSALS + 1 and metric["charged_attempts"] == 0
    # An operator's resume starts the count again, as for every other counter.
    recover_quarantined(mind, job_ids=[job["id"]], command_id="resume-after-deletes", source="operator")
    state, count, data = queue_row(mind, job["id"])
    assert state == "pending" and "deletion_refusals" not in data
    assert data["recovery_history"][-1]["deletion_refusals"] == MAX_DELETION_REFUSALS + 1


def test_a_refusal_for_a_delete_leaves_the_charged_budget_whole_for_the_attempts_after_it(setup):
    """Refused once for a delete made while it answered, the row commits at the next attempt: one
    charged attempt in all, and nothing of the refusal's count is left on it (CL6E-MM-03)."""
    mind, source, clock = setup
    note_source, note = read_note(mind, clock, "refused-once", "答的时候删掉的一条")
    primary = source("today", "今天天气不错")

    class Deleting:
        def appraise(self, context):
            mind.engine.delete(note_source)
            paid(self)
            proposal, receipt = answered(Appraisal(reason="平常的一天", values={"curiosity": 60}))
            return proposal, {**receipt, **tool_read(note)}

    class Again:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 57}))

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Deleting())
    assert queue_row(mind, job["id"])[2]["deletion_refusals"] == 1
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and count == 1 and "deletion_refusals" not in data
    assert sorted(entry["charged"] for entry in attempts.read(mind.engine, mind.scope.key(), job_id=job["id"])["attempts"]) == [False, True]
