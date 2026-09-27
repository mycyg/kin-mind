"""The assessment queue as the 2026-09-27 audit found it, and what changed.

A fixed 64k input budget for a model with a million-token window made evidence compression the
queue's largest cost; a pass that still left evidence out was tried thirteen times; a resumed batch
kept members that other batches had finished, so one stimulus was judged three times; a quarantined
row could only wait for an operator; the personality chain never showed the model what it could cite.
"""

import json
import time
from datetime import timedelta

import httpx
import pytest

from eventmem.core.db import Conflict, digest, dumps
from eventmem.core.retrieval import tokens

from kin_mind import appraisal as A
from kin_mind import attempts
from kin_mind.appraisal import Appraisal, Appraisals, DeepSeek
from kin_mind.memory import MemoryContinuity
from kin_mind.recovery import recover_quarantined, triage_quarantined

from test_kin_mind import FakeReviewer

pytest_plugins = ("test_kin_mind",)


def row_of(mind, job_id):
    with mind.engine.db.connect() as conn:
        found = conn.execute("SELECT state,attempts,available,data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()
    return found["state"], found["attempts"], found["available"], json.loads(found["data"])


def due_now(mind, *job_ids):
    with mind.engine.db.connect(write=True) as conn:
        for job_id in job_ids:
            conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job_id,))


def record_id(source_id):
    """The root record of a source: what an internal review names as what it is about."""
    return "mem_" + digest([source_id, "root"])[:32]


def semantic(setup):
    """Production's switches: records, the semantic queue and the operational lanes."""
    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True, "semantic": True, "operational_lanes": True})
    return mind, source, clock


class Seeing(FakeReviewer):
    """A reviewer that keeps what each call was shown."""

    def __init__(self, proposal=None, error=None):
        super().__init__(proposal or Appraisal(reason="Looked at it"), error)
        self.contexts = []

    def appraise(self, context):
        self.contexts.append(context)
        return super().appraise(context)


def shown(context):
    return sorted(source["id"] for source in context["new_evidence"])


# --- 1. The budget comes from the model's window ------------------------------------------------------

def test_the_deepseek_budget_comes_from_the_catalog_window_under_a_cap(tmp_path):
    from kin_mind.codex_executor import DEEPSEEK_MODEL_CATALOG
    assert A.MODEL_CATALOG == DEEPSEEK_MODEL_CATALOG, "one catalog, the executor's"
    window = A.catalog_window()
    assert window == int(1048576 * 95 / 100)
    assert A.appraisal_input_budget() == A.APPRAISAL_INPUT_CAP == 256000
    assert A.appraisal_input_budget(100000) == 100000
    assert A.appraisal_input_budget(10**7) == window - A.APPRAISAL_MAX_OUTPUT, "never past the window less the output"
    assert A.appraisal_input_budget(catalog=tmp_path / "missing.json") == A.APPRAISAL_INPUT_BUDGET == 64000
    small = tmp_path / "models.json"
    small.write_text(json.dumps({"models": [{"slug": "deepseek-flash", "context_window": 200000}]}))
    assert A.appraisal_input_budget(catalog=small) == 200000 - A.APPRAISAL_MAX_OUTPUT


def test_the_configured_budget_is_validated_and_reaches_the_provider(setup):
    mind, source, _ = semantic(setup)
    memory = MemoryContinuity(mind)
    assert memory.settings()["appraisal_input_budget"] is None
    for wrong in (1000, 2 * 10**6, "256000", 100000.0):
        with pytest.raises(ValueError, match="appraisal input budget"):
            memory.configure({"appraisal_input_budget": wrong})
    memory.configure({"appraisal_input_budget": 100000})
    jobs = Appraisals(mind)
    jobs.enqueue([source("budget-configured")], "synthetic-v1")
    reviewer = Seeing()
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert reviewer.input_budget == 100000
    memory.configure({"appraisal_input_budget": None})
    jobs.enqueue([source("budget-default")], "synthetic-v1")
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert reviewer.input_budget == 256000


def ds_provider(mind, asked, monkeypatch):
    """The DeepSeek transport as the queue uses it, on a synthetic answer: every call is named."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "synthetic")

    def respond(request):
        body = json.loads(request.content)
        tool = body["tools"][0]["name"]
        payload = json.loads(body["messages"][0]["content"])
        asked.append({"tool": tool, "max_tokens": body.get("max_tokens"), "budget_tokens": payload.get("budget_tokens")})
        if tool == "submit_compression":
            answer = {"entries": [{"item_ids": payload["allowed_item_ids"], "summary": "她说周末想去海边拍照。"}], "omitted_ids": []}
        else:
            answer = {"reason": "记下了她的周末计划。"}
        return httpx.Response(200, json={"id": f"msg-{len(asked)}", "model": A.APPRAISAL_MODEL, "stop_reason": "tool_use",
                                         "usage": {"input_tokens": 1000, "output_tokens": 20},
                                         "content": [{"type": "tool_use", "name": tool, "input": answer}]})
    # The queue's own timeout (DeepSeek.from_engine): a prepared request is sent in the same attempt.
    provider = DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "KIN_TEST_DS_KEY", timeout=600,
                        transport=httpx.MockTransport(respond))
    provider.engine = mind.engine
    return provider


def long_letter():
    unit = "今天的观察：她说周末想去海边走走，顺便拍几张照片。\n"
    text = unit * 200
    while tokens(text) < 90000:
        text += unit * 200
    assert tokens(text) < 200000
    return text


def test_an_ordinary_long_request_goes_out_whole_and_a_capped_one_is_prepared_within_one_calls_output(setup, monkeypatch):
    """52–58k-token action appraisals were compressed to fit 64k; ninety thousand tokens now go out as
    they are. Under a configured cap the same request is prepared, and no compression call is asked
    for more than one call can write."""
    mind, source, _ = semantic(setup)
    jobs = Appraisals(mind)
    jobs.enqueue([source("long-letter", long_letter())], "synthetic-v1")
    asked = []
    assert jobs.run_one(ds_provider(mind, asked, monkeypatch))["state"] == "complete"
    assert [call["tool"] for call in asked] == ["submit_appraisal"], "no compression under the catalog budget"
    assert asked[0]["max_tokens"] == A.APPRAISAL_MAX_OUTPUT
    MemoryContinuity(mind).configure({"appraisal_input_budget": 64000})
    jobs.enqueue([source("second-long-letter", long_letter() + "又及。")], "synthetic-v1")
    asked.clear()
    out = jobs.run_one(ds_provider(mind, asked, monkeypatch))
    assert out["state"] == "complete", (out.get("error"), out.get("error_detail"), [call["tool"] for call in asked])
    tools = [call["tool"] for call in asked]
    assert tools[-1] == "submit_appraisal" and "submit_compression" in tools
    from kin_mind.context import TARGET_TOKENS_MAX
    assert all(call["budget_tokens"] <= TARGET_TOKENS_MAX for call in asked if call["tool"] == "submit_compression")


# --- 2. A pass that still leaves evidence out --------------------------------------------------------

class Preparing:
    """Evidence preparation that leaves something out every time, after two paid compression calls."""

    def __init__(self):
        self.contexts = []

    def appraise(self, context):
        self.contexts.append(context)
        for _ in range(2):
            attempts.record_call(self, "submit_compression", outcome="ok", model=A.APPRAISAL_MODEL,
                                 usage={"input_tokens": 5000, "output_tokens": 900})
        self.compression_parts.add("part-" + str(len(self.contexts)))
        raise RuntimeError("deepseek-evidence-compression-pending:compressed")


def test_compression_passes_and_paid_calls_are_bounded(setup):
    mind, _, _ = setup
    jobs = Appraisals(mind)
    data = {"error": "deepseek-evidence-compression-pending:compressed"}
    assert [jobs._compression_wait(data, 1) for _ in range(3)] == ["pending", "pending", "needs-repair"]
    assert data["repair_reason"] == "compression-passes-exhausted:3"
    data = {"error": "deepseek-evidence-compression-pending:compressed", "compression_calls": A.MAX_COMPRESSION_CALLS + 1}
    assert jobs._compression_wait(data, 1) == "needs-repair"
    assert data["repair_reason"] == "compression-calls-exhausted:" + str(A.MAX_COMPRESSION_CALLS + 1)


def test_a_row_that_cannot_be_prepared_is_set_aside_after_three_passes_with_its_calls_counted(setup):
    mind, source, _ = setup
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("never-fits")], "synthetic-v1")
    states = []
    for _ in range(3):
        due_now(mind, job["id"])
        states.append(jobs.run_one(Preparing())["state"])
    state, charged, _, data = row_of(mind, job["id"])
    assert states == ["pending", "pending", "needs-repair"]
    assert data["repair_reason"] == "compression-passes-exhausted:3" and data["compression_calls"] == 6
    assert charged == 0, "a compression pass is still not a charged appraisal"
    ledger = attempts.read(mind.engine, mind.scope.key(), job_id=job["id"])["attempts"]
    assert [entry["compression_calls"] for entry in ledger] == [2, 2, 2] and not any(entry["charged"] for entry in ledger)


def test_a_batch_too_large_to_prepare_becomes_its_members_after_one_pass(setup):
    """The first pass that leaves evidence out splits the batch: each member is its own job again,
    solo, and the row goes back to what it was before it absorbed them."""
    mind, source, _ = setup
    jobs = Appraisals(mind)
    lead = jobs.enqueue([source("lead")], "synthetic-v1", origin="reflection", stimulus="wish-review")
    rest = [jobs.enqueue([source(key)], "synthetic-v1", origin="reflection", stimulus="wish-review") for key in ("second", "third")]
    preparing = Preparing()
    assert jobs.run_one(preparing)["state"] == "pending"
    assert len(shown(preparing.contexts[0])) == 3, "one assessment for the three"
    state, _, _, data = row_of(mind, lead["id"])
    assert state == "pending" and data["solo"] is True and "batch_ids" not in data and "own" not in data
    assert data["evidence_ids"] == [source("lead")] and data["stimulus"] == "wish-review"
    assert data["split_from_batch"]["members"] == 2 and data["compression_calls"] == 2
    for job in rest:
        state, _, _, member = row_of(mind, job["id"])
        assert (state, member["solo"], member["split_from"]) == ("pending", True, lead["id"])
    due_now(mind, lead["id"], *(job["id"] for job in rest))
    reviewer = Seeing()
    for _ in range(3):
        assert jobs.run_one(reviewer)["state"] == "complete"
    assert sorted(len(shown(context)) for context in reviewer.contexts) == [1, 1, 1], "each judged alone, none absorbed again"
    assert jobs.run_one(reviewer)["state"] == "idle"


def test_a_batch_recorded_before_its_own_part_was_kept_is_not_split(setup):
    mind, source, _ = setup
    jobs = Appraisals(mind)
    old_lead, old_member = source("old-lead"), source("old-member")
    lead = jobs.enqueue([old_lead], "synthetic-v1", origin="reflection", stimulus="wish-review")
    member = jobs.enqueue([old_member], "synthetic-v1", origin="reflection", stimulus="wish-review")
    data = row_of(mind, lead["id"])[3]
    data.update(batch_ids=[member["id"]], evidence_ids=[old_lead, old_member], stimulus="internal-batch")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET data=? WHERE id=?", (dumps(data), lead["id"]))
        conn.execute("UPDATE mind_appraisals SET state='batched' WHERE id=?", (member["id"],))
    assert jobs.run_one(Preparing(), job_id=lead["id"])["state"] == "pending"
    assert row_of(mind, lead["id"])[3]["batch_ids"] == [member["id"]]
    assert row_of(mind, member["id"])[0] == "batched"


# --- 3. What a batch settles, and what it judges when it runs again -----------------------------------

def quarantine_batch(mind, jobs, lead_key, member_ids):
    """A batch set aside for repair, its members back in the queue, as after two identical failures."""
    for _ in range(2):
        due_now(mind, lead_key)
        jobs.run_one(FakeReviewer(error=RuntimeError("deepseek-invalid-result")), job_id=lead_key)
    assert row_of(mind, lead_key)[0] == "needs-repair"
    assert all(row_of(mind, member)[0] == "pending" for member in member_ids)


def test_a_committed_batch_settles_a_member_that_was_set_aside_meanwhile(setup):
    mind, source, _ = semantic(setup)
    jobs = Appraisals(mind)
    lead = jobs.enqueue([source("zombie-lead")], "synthetic-v1")
    member = jobs.enqueue([source("zombie-member")], "synthetic-v1")
    quarantine_batch(mind, jobs, lead["id"], [member["id"]])
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='needs-repair',data=json_set(data,'$.error','Conflict') WHERE id=?", (member["id"],))
    recover_quarantined(mind, job_ids=[lead["id"]], command_id="resume-lead", source="test")
    reviewer = Seeing()
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert shown(reviewer.contexts[0]) == sorted([source("zombie-lead"), source("zombie-member")])
    state, _, _, data = row_of(mind, member["id"])
    assert state == "complete" and data["completed_from"] == "batch-parent" and data["result"]["batch_id"] == lead["id"]
    assert data["recovery_history"][-1]["command_id"] == "settled-by-batch" and "error" not in data
    assert jobs.run_one(reviewer)["state"] == "idle" and len(reviewer.contexts) == 1


def test_a_resumed_batch_leaves_out_a_member_another_commit_finished(setup):
    """The zombie of 2026-09-27: a batch resumed after its member had been judged on its own judged
    the member again. It now judges only what is still its own."""
    mind, source, _ = semantic(setup)
    jobs = Appraisals(mind)
    lead = jobs.enqueue([source("resumed-lead")], "synthetic-v1", origin="reflection", stimulus="wish-review")
    about = source("what-the-member-is-about")
    member = jobs.enqueue([source("member-timer"), record_id(about)], "synthetic-v1", origin="reflection", stimulus="wish-review")
    quarantine_batch(mind, jobs, lead["id"], [member["id"]])
    first = Seeing()
    assert jobs.run_one(first)["state"] == "complete"
    assert shown(first.contexts[0]) == sorted([source("member-timer"), about]), "the member ran on its own"
    recover_quarantined(mind, job_ids=[lead["id"]], command_id="resume-lead", source="test")
    again = Seeing()
    assert jobs.run_one(again)["state"] == "complete"
    assert shown(again.contexts[0]) == [source("resumed-lead")], "not the member's timer, nor what it was about"
    data = row_of(mind, lead["id"])[3]
    assert data["batch_left"] == [member["id"]] and "batch_ids" not in data
    with mind.engine.db.connect() as conn:
        events = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_events WHERE kind='affect'")]
    assert sum(source("member-timer") in json.dumps(event) for event in events) == 1, "one stimulus, one judgment"


def test_a_resumed_batch_takes_back_a_member_the_queue_had_not_run_yet(setup):
    mind, source, _ = semantic(setup)
    jobs = Appraisals(mind)
    lead = jobs.enqueue([source("taking-back-lead")], "synthetic-v1")
    member = jobs.enqueue([source("taking-back-member")], "synthetic-v1")
    quarantine_batch(mind, jobs, lead["id"], [member["id"]])
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (time.time() + 3600, member["id"]))
    recover_quarantined(mind, job_ids=[lead["id"]], command_id="resume-lead", source="test")
    reviewer = Seeing()
    assert jobs.run_one(reviewer)["state"] == "complete"
    state, _, _, data = row_of(mind, member["id"])
    assert state == "complete" and data["result"]["batch_id"] == lead["id"]
    assert "completed_from" not in data, "taken back into the batch, not settled after it"


def test_a_resumed_batch_leaves_a_member_that_is_being_judged_elsewhere(setup):
    """Once a batch is set aside its members are the queue's again: another batch may take one in,
    or one may have been claimed on its own (here an attempt that died, whose lease ran out: the lane
    holds one live lease). The resumed batch leaves both to where they are judged."""
    mind, source, _ = semantic(setup)
    jobs = Appraisals(mind)
    lead = jobs.enqueue([source("elsewhere-lead")], "synthetic-v1")
    taken = jobs.enqueue([source("taken-in")], "synthetic-v1")
    running = jobs.enqueue([source("running-alone")], "synthetic-v1")
    quarantine_batch(mind, jobs, lead["id"], [taken["id"], running["id"]])
    other_lead, taken_in = source("other-lead"), source("taken-in")
    other = jobs.enqueue([other_lead], "synthetic-v1")
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (other["id"],)).fetchone()[0])
        data.update(batch_ids=[taken["id"]], evidence_ids=[other_lead, taken_in], stimulus="interaction-batch")
        conn.execute("UPDATE mind_appraisals SET data=?,available=? WHERE id=?", (dumps(data), time.time() + 3600, other["id"]))
        conn.execute("UPDATE mind_appraisals SET state='batched' WHERE id=?", (taken["id"],))
        conn.execute("UPDATE mind_appraisals SET state='running',lease=? WHERE id=?", (time.time() - 10, running["id"]))
    recover_quarantined(mind, job_ids=[lead["id"]], command_id="resume-lead", source="test")
    reviewer = Seeing()
    assert jobs.run_one(reviewer, job_id=lead["id"])["state"] == "complete"
    assert shown(reviewer.contexts[0]) == [source("elsewhere-lead")], "neither is judged here as well"
    assert row_of(mind, lead["id"])[3]["batch_left"] == sorted([taken["id"], running["id"]])
    assert row_of(mind, taken["id"])[0] == "batched" and row_of(mind, running["id"])[0] == "running"
    due_now(mind, other["id"])
    assert jobs.run_one(reviewer, job_id=other["id"])["state"] == "complete"
    assert shown(reviewer.contexts[1]) == sorted([other_lead, taken_in])
    assert row_of(mind, taken["id"])[3]["result"]["batch_id"] == other["id"]


def test_a_job_whose_every_source_is_integrated_ends_without_a_call_whatever_its_stimulus(setup):
    """Internal reviews and batches were never checked, and a record they name kept any job from ever
    being done: an idle review answered by another batch ran again on its references alone."""
    mind, source, _ = semantic(setup)
    jobs = Appraisals(mind)
    timer, about = source("idle-timer"), source("idle-about")
    idle = jobs.enqueue([timer, record_id(about)], "synthetic-v1", origin="reflection", stimulus="idle-review")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_semantic_sources VALUES(?,?,?)", (mind.scope.key(), timer, "event-elsewhere"))
    reviewer = Seeing()
    out = jobs.run_one(reviewer)
    assert out["state"] == "complete" and out["completed_from"] == "already-integrated" and not reviewer.contexts
    fresh, answered = source("fresh-message"), source("answered-message")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_semantic_sources VALUES(?,?,?)", (mind.scope.key(), answered, "event-elsewhere"))
    job = jobs.enqueue([fresh, answered], "synthetic-v1")
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert shown(reviewer.contexts[-1]) == [fresh], "what is integrated is left out"
    assert row_of(mind, job["id"])[3]["evidence_ids"] == [fresh]


# --- 4. The triage of what is set aside -----------------------------------------------------------------

def quarantined(mind, key, evidence, stimulus=None, *, error="native-review-unconfirmed", started="2026-09-21T06:00:00+00:00", **extra):
    job_id = "appraise_" + digest(["triage", key])[:32]
    data = {"evidence_ids": evidence, "agent_version": "synthetic-v1", "origin": "interaction", "stimulus": stimulus,
            "error": error, "repair_reason": extra.pop("repair_reason", "repeated-failure:RuntimeError"),
            "attempt_started_at": started, "error_signature": "sig", "error_repeats": 2, **extra}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,attempts,data) VALUES(?,?,?,?,?,?)",
                     (job_id, mind.scope.key(), extra.get("state", "needs-repair"), time.time(), 2, dumps(data)))
    return job_id


@pytest.fixture
def triage_world(setup):
    mind, source, clock = semantic(setup)
    jobs = Appraisals(mind)
    # Integrated by a real commit: the event is there.
    done = source("integrated-message")
    jobs.enqueue([done], "synthetic-v1")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Judged")))["state"] == "complete"
    rows = {
        "zombie": quarantined(mind, "zombie", [done], "interaction-batch"),
        "owner": quarantined(mind, "owner", [source("only-holder-of-an-owner-message")]),
        "delivery": quarantined(mind, "delivery", [source("a-delivery")], "delivery", started="2026-09-21T06:05:00+00:00"),
        "compression": quarantined(mind, "compression", [source("long-result")], "exploration-result",
                                   error="deepseek-evidence-compression-pending:compressed",
                                   repair_reason="compression-passes-exhausted:13"),
        "old-session": quarantined(mind, "old-session", [], "session-maintenance", started="2026-09-21T06:17:00+00:00"),
        "organized": quarantined(mind, "organized", [done], "memory-enrichment", error="Conflict", repair_reason="Conflict"),
        "unorganized": quarantined(mind, "unorganized", [source("not-organized")], "memory-enrichment", error="Missing",
                                   repair_reason="Missing"),
        "gone": quarantined(mind, "gone", [source("deleted-later")]),
    }
    quarantined(mind, "later-session", [], "session-maintenance", started="2026-09-27T09:49:00+00:00", state="complete")
    quarantined(mind, "organizer", [done], "memory-enrichment", state="complete")
    gone = source("deleted-later")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (gone,))
        conn.execute("INSERT INTO mind_action_events VALUES(?,?,?,?,?,?)",
                     ("action_owner", mind.scope.key(), "wish-review", mind.clock(), "needs-review", dumps({"job_id": rows["owner"]})))
    return mind, jobs, rows


def every_row(mind):
    with mind.engine.db.connect() as conn:
        return sorted(tuple(r) for r in conn.execute("SELECT id,state,available,attempts,data FROM mind_appraisals"))


def test_a_dry_run_sorts_every_quarantined_row_and_writes_nothing(triage_world):
    mind, _, rows = triage_world
    before = every_row(mind)
    result = triage_quarantined(mind, resume=["native-review"])
    assert every_row(mind) == before
    found = {entry["id"]: (entry["class"], entry["action"]) for entry in result["rows"]}
    assert found == {rows["zombie"]: ("integrated", "supersede"), rows["owner"]: ("native-review", "resume"),
                     rows["delivery"]: ("native-review", "resume"), rows["compression"]: ("compression", "report"),
                     rows["old-session"]: ("answered", "supersede"), rows["organized"]: ("organized", "supersede"),
                     rows["unorganized"]: ("enrichment", "report"), rows["gone"]: ("evidence-unavailable", "supersede")}
    gone = next(entry for entry in result["rows"] if entry["id"] == rows["gone"])
    assert gone["reason"] == "root-evidence-unavailable"
    assert result["state"] == "dry-run" and result["actions"] == {"supersede": 4, "resume": 2, "report": 2}
    owner = next(entry for entry in result["rows"] if entry["id"] == rows["owner"])
    assert owner["evidence"] == {"sources": 1, "records": 0, "integrated": 0, "kinds": {"message": 1}}
    assert "proposed_result" not in json.dumps(result) and "only-holder" not in json.dumps(result)
    assert triage_quarantined(mind)["actions"]["resume"] == 0, "nothing resumes that was not named"


def test_applying_supersedes_what_is_settled_and_spreads_what_resumes(triage_world):
    mind, jobs, rows = triage_world
    with pytest.raises(ValueError, match="sourced command"):
        triage_quarantined(mind, apply=True, resume=["native-review"])
    with pytest.raises(ValueError, match="chosen class"):
        triage_quarantined(mind, resume=["integrated"])
    applied = triage_quarantined(mind, apply=True, command_id="triage-1", source="owner-decision", resume=["native-review"],
                                 per_slot=1, spacing_minutes=10)
    assert applied["state"] == "applied"
    now = time.time()
    for key in ("zombie", "old-session", "organized", "gone"):
        state, _, _, data = row_of(mind, rows[key])
        assert state == "superseded" and data["recovery_history"][-1]["retired"] is True, key
    owner, delivery = row_of(mind, rows["owner"]), row_of(mind, rows["delivery"])
    assert (owner[0], owner[1], delivery[0]) == ("pending", 0, "pending")
    assert owner[2] <= now and 590 <= delivery[2] - now <= 610, "one row now, the next ten minutes later"
    assert "error" not in owner[3] and owner[3]["recovery_history"][-1]["triage"] == "native-review"
    for key in ("compression", "unorganized"):
        assert row_of(mind, rows[key])[0] == "needs-repair", key
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM mind_action_events WHERE id='action_owner'").fetchone()[0] == "queued"
    assert triage_quarantined(mind, apply=True, command_id="triage-1", source="owner-decision",
                              resume=["native-review"], per_slot=1, spacing_minutes=10) == applied
    with pytest.raises(Conflict):
        triage_quarantined(mind, apply=True, command_id="triage-1", source="owner-decision", resume=["compression"])
    # Resumed, the owner's message is judged -- by whatever provider the host now runs, DeepSeek here.
    reviewer = Seeing()
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert shown(reviewer.contexts[0]) == [row_of(mind, rows["owner"])[3]["evidence_ids"][0]]


def test_a_row_whose_commit_already_landed_is_resumed_and_finishes_without_a_call(setup):
    mind, source, _ = semantic(setup)
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("committed-then-set-aside")], "synthetic-v1")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Judged")))["state"] == "complete"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE id=?", (job["id"],))
    result = triage_quarantined(mind, apply=True, command_id="triage-committed", source="test")
    assert [(entry["class"], entry["action"]) for entry in result["rows"]] == [("committed", "resume")]
    out = jobs.run_one(FakeReviewer(error=AssertionError("no call")))
    assert out["state"] == "complete" and out["completed_from"] == "already-committed"


# --- 5. The personality chain can complete ------------------------------------------------------------

def test_a_stamp_from_before_dimensions_were_added_still_holds(setup):
    """2026-09-27 09:08: eight dimensions were added, no definition changed, and every check, prediction
    and decision stamped before went stale at once. A declared addition no longer moves them; a changed,
    removed or undeclared definition still does."""
    from copy import deepcopy

    from kin_mind import compat
    mind, _, _ = setup
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        current = compat.stamp(mind, conn, state)
        before = deepcopy(state)
        for group in compat.DEFINITION_ADDITIONS:
            for key in group:
                before["profile"]["dimensions"].pop(key)
        earlier = compat.stamp(mind, conn, before)
        assert earlier["key"] != current["key"] and current["key"] == "compat_" + digest(current["parts"])[:32]
        assert compat.holds(earlier, current) and compat.stale_reason(earlier, current) is None
        changed = deepcopy(before)
        changed["profile"]["dimensions"]["mood"]["label"] += "（改）"
        assert compat.stale_reason(compat.stamp(mind, conn, changed), current) == "compat-changed:definitions"
        undeclared = deepcopy(state)
        undeclared["profile"]["dimensions"]["zeal"] = deepcopy(undeclared["profile"]["dimensions"]["mood"])
        assert not compat.holds(current, compat.stamp(mind, conn, undeclared)), "an undeclared addition still moves it"
        assert not compat.holds(current, earlier), "and so does a dimension taken away"
        other = {**earlier, "parts": {**earlier["parts"], "contract": "behavior-x"}}
        assert compat.stale_reason(other, current) == "compat-changed:contract,definitions"
        moved = compat.stamp(mind, conn, changed)
    # A proposal the merge marked stale for the addition is one the day's merge can use again.
    from kin_mind.behavior_chain import SCHEMA, due

    def stale_proposal(key, stamp):
        with mind.engine.db.connect(write=True) as conn:
            for statement in SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO mind_evolution_proposals VALUES(?,?,?,?,?,?)",
                         (key, mind.scope.key(), "stale", mind.clock(), stamp["key"],
                          dumps({"evolution": {}, "compat": stamp, "stale_reason": "compat-changed:definitions"})))
        with mind.engine.db.connect() as conn:
            return due(conn, mind)
    assert stale_proposal("still-moved", moved) is False, "nothing for the merge while it is still stale"
    assert stale_proposal("held-again", earlier) is True


def chain_commit(jobs, mind, source, key, **proposal):
    reviewer = Seeing(Appraisal(reason="A chain step", **proposal))
    jobs.enqueue([source(key)], "synthetic-v1")
    out = jobs.run_one(reviewer)
    assert out["state"] == "complete", out
    return reviewer, out


def test_a_confirmed_check_is_shown_cited_and_merged(setup):
    """The chain end to end: a hypothesis and its prediction, a later verified outcome, the confirmed
    check shown to the model, an evolution that cites it, and the day's merge that applies it. The
    model was only ever shown open predictions, so no evolution could name a check."""
    from kin_mind.appraisal import DailyReview, Prediction, PredictionOutcome, SelfHypothesis
    from kin_mind.behavior_chain import merge_daily
    from kin_mind.state import Evolution
    mind, source, clock = setup
    jobs = Appraisals(mind)
    daily = DailyReview(mind)
    assert daily.due() is False, "nothing to merge"
    first = source("she-asks-for-photos")
    chain_commit(jobs, mind, source, "she-asks-for-photos", self_hypothesis=SelfHypothesis(
        statement="她提到出门时我会主动要照片", reason="几次都是这样", evidence_ids=[first],
        predictions=[Prediction(statement="下次她说出门，我会先问能不能看照片", test_window_hours=48)]))
    [open_one] = mind.read()["trait_ledger"]["open_predictions"]
    clock[0] += timedelta(hours=2)
    later = source("she-went-out-and-was-asked")
    chain_commit(jobs, mind, source, "she-went-out-and-was-asked", prediction_outcomes=[PredictionOutcome(
        prediction_id=open_one["id"], outcome="confirmed", evidence_ids=[later], reason="她说出门后我先问了照片")])
    clock[0] += timedelta(hours=2)
    reviewer = Seeing()
    jobs.enqueue([source("an-ordinary-evening")], "synthetic-v1")
    jobs.run_one(reviewer)
    # The state the queue hands the provider, and the projection DeepSeek is sent.
    [check] = reviewer.contexts[0]["state"]["trait_ledger"]["confirmed_checks"]
    assert A.appraisal_context(reviewer.contexts[0])["state"]["confirmed_checks"] == [check]
    assert check["claim_id"] == open_one["claim_id"] and check["prediction_id"] == open_one["id"]
    baseline = mind.read()["dimensions"]["initiative"]["baseline"]
    clock[0] += timedelta(hours=2)
    chain_commit(jobs, mind, source, "growth", evolution=Evolution(
        claim_id=check["claim_id"], assessment_id=check["assessment_id"], baseline_changes={"initiative": baseline + 1}))
    assert daily.due() is True
    merged = merge_daily(mind, "synthetic-v1")
    assert merged["state"] == "complete", merged
    assert mind.read()["dimensions"]["initiative"]["baseline"] == baseline + 1
    assert daily.due() is False, "merged for today"
    with mind.engine.db.connect() as conn:
        from kin_mind.behavior_chain import confirmed_checks
        assert confirmed_checks(conn, mind) == [], "a check an applied evolution rested on is not offered again"


def test_a_prediction_nothing_can_settle_is_closed_as_inconclusive(setup):
    from kin_mind.appraisal import Prediction, SelfHypothesis
    mind, source, clock = setup
    jobs = Appraisals(mind)
    evidence = source("a-guess")
    chain_commit(jobs, mind, source, "a-guess", self_hypothesis=SelfHypothesis(
        statement="她晚上会主动聊今天的事", reason="最近几天都这样", evidence_ids=[evidence],
        predictions=[Prediction(statement="今晚她会先开口", test_window_hours=1)]))
    [prediction] = mind.read()["trait_ledger"]["open_predictions"]
    clock[0] += timedelta(hours=1 + 23)
    chain_commit(jobs, mind, source, "within-the-grace")
    assert [p["id"] for p in mind.read()["trait_ledger"]["open_predictions"]] == [prediction["id"]], "a day's grace"
    clock[0] += timedelta(hours=2)
    chain_commit(jobs, mind, source, "past-the-grace")
    assert mind.read()["trait_ledger"]["open_predictions"] == []
    record = mind.engine.get(prediction["id"])
    closed = record["attributes"]["self_knowledge"]["closed"]
    assert record["status"] == "archived" and (closed["outcome"], closed["reason"]) == ("inconclusive", "window-expired")


def test_a_prediction_made_under_a_configuration_that_moved_is_closed_with_what_moved(setup):
    from kin_mind.appraisal import Prediction, SelfHypothesis
    mind, source, clock = setup
    jobs = Appraisals(mind)
    evidence = source("a-guess-before-a-change")
    chain_commit(jobs, mind, source, "a-guess-before-a-change", self_hypothesis=SelfHypothesis(
        statement="她忙的时候我会少说两句", reason="观察", evidence_ids=[evidence],
        predictions=[Prediction(statement="她说在忙，我只回一句", test_window_hours=72)]))
    [prediction] = mind.read()["trait_ledger"]["open_predictions"]
    clock[0] += timedelta(minutes=5)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,data) VALUES('behavior_models',?)",
                     (json.dumps({"chat": "another-model", "chat_effort": "high"}),))
    chain_commit(jobs, mind, source, "after-the-change")
    closed = mind.engine.get(prediction["id"])["attributes"]["self_knowledge"]["closed"]
    assert closed["reason"] == "compat-changed:models"


# --- 6. A refusal is shown until the section commits again --------------------------------------------

def test_a_sections_old_refusal_goes_when_the_section_commits(setup):
    from kin_mind.appraisal import ExpressionIntent, last_refusal, record_refusal
    mind, source, clock = setup
    jobs = Appraisals(mind)
    with mind.engine.db.connect(write=True) as conn:
        record_refusal(conn, mind.scope.key(), "expression_intent", "intent-evidence-unknown", "2026-09-22T09:33:42+00:00")
        record_refusal(conn, mind.scope.key(), "next_move", "next-move-forged-grounds", "2026-09-27T08:06:11+00:00")
    evidence = source("an-evening-chat")
    reviewer = Seeing(Appraisal(reason="Stay close tonight", expression_intent=ExpressionIntent(
        stance="今晚多陪她聊聊", evidence_ids=[evidence])))
    jobs.enqueue([evidence], "synthetic-v1")
    assert jobs.run_one(reviewer)["state"] == "complete"
    with mind.engine.db.connect() as conn:
        refusals = last_refusal(conn, mind.scope.key())
    assert "expression_intent" not in refusals, "committed since: no longer shown"
    assert refusals["next_move"]["code"] == "next-move-forged-grounds", "a section that did not commit keeps its refusal"
    wrong = Seeing(Appraisal(reason="Again", expression_intent=ExpressionIntent(stance="今晚多陪她聊聊", evidence_ids=["src_" + "0" * 32])))
    jobs.enqueue([source("another-evening")], "synthetic-v1")
    assert jobs.run_one(wrong)["state"] == "complete"
    with mind.engine.db.connect() as conn:
        assert last_refusal(conn, mind.scope.key(), "expression_intent")["expression_intent"]["at"] == mind.clock()


# --- 8. One review range ------------------------------------------------------------------------------

def test_the_stored_review_minutes_are_validated_against_the_range_kin_chooses_from(setup):
    mind, _, _ = setup
    memory = MemoryContinuity(mind)
    config = memory.configure({"review_min_minutes": 10, "first_review_minutes": 10, "review_max_minutes": 1440})
    assert (config["review_min_minutes"], config["review_max_minutes"]) == (A.REVIEW_MIN_MINUTES, A.REVIEW_MAX_MINUTES) == (10, 1440)
    for wrong in ({"review_min_minutes": 9, "first_review_minutes": 10}, {"review_max_minutes": 1441}):
        with pytest.raises(ValueError, match="10..1440"):
            memory.configure(wrong)
    assert "10到1440" in A.SYSTEM
