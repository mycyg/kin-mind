"""A source deleted while its appraisal waits for the model (CR4-MM-02). The model's answer still
quotes it, and however the attempt then ends -- a failure after the answer, the commit refusing it,
a sharing repair -- nothing the worker writes back to its queue row brings the deleted words back.
What the attempt cost, why it ended and which attempt it was stay."""
import json

from eventmem.core.models import SourceInput

from kin_mind import attempts
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.exploration_decisions import SharingDecision
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind',)

MARKER = "quuxzeta"
USAGE = {"input_tokens": 600, "output_tokens": 40}
TARGET = "explore_synthetic"


class DeletesWhileItThinks:
    """The model is shown the words; while it answers, the owner deletes their source; the answer
    still quotes them."""

    def __init__(self, mind, doomed):
        self.mind, self.doomed = mind, doomed

    def appraise(self, context):
        assert MARKER in json.dumps(context, ensure_ascii=False), "the model was shown the words"
        self.mind.engine.delete(self.doomed)
        attempts.record_call(self, "submit_appraisal", outcome="ok", model="synthetic", request_id="req-1", usage=USAGE)
        return (Appraisal(reason=f"她提到了 {MARKER}，想记下来", values={"curiosity": 70}),
                {"provider": "deepseek", "model": "synthetic", "request_id": "req-1", "usage": USAGE})


class RepairsSharing(DeletesWhileItThinks):
    """The same, with a sharing repair to ask: the queue row is read as it stands when it is asked."""

    seen_at_repair = None

    def repair_sharing(self, proposal, context):
        with self.mind.engine.db.connect() as conn:
            self.seen_at_repair = conn.execute("SELECT data FROM mind_appraisals WHERE instr(data,?)>0", (MARKER,)).fetchall()
        attempts.record_call(self, "repair_sharing", outcome="ok", model="synthetic", request_id="req-2", usage=USAGE)
        return ([SharingDecision(exploration_id=TARGET, decision="keep", reason=f"关于 {MARKER} 的结果先留着")],
                {"provider": "deepseek", "model": "synthetic", "request_id": "req-2", "usage": USAGE})


def exploration_result(mind, clock):
    """An exploration's result, the evidence of an appraisal that must decide whether to share it."""
    return mind.engine.receive(SourceInput(
        namespace="test", key="doomed-exploration", scope=mind.scope, text=f"探索结果：{MARKER} 的来历",
        authority="model", occurred_at=clock[0].isoformat(),
        metadata={"host_event": "exploration-result", "exploration_id": TARGET}))["id"]


def after(mind, job_id):
    """What the attempt kept: every table free of the words; its cost on the ledger; its identity."""
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect() as conn:
        found = conn.execute("SELECT state,attempts,data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()
    data = json.loads(found["data"])
    [ledger] = attempts.read(mind.engine, mind.scope.key(), job_id=job_id)["attempts"]
    assert ledger["charged"] is True and found["attempts"] == 1, "the call it paid for is still counted"
    assert ledger["calls"][0]["usage"] == USAGE and ledger["attempt_token"] == data["attempt_token"]
    return found["state"], data


def test_a_failure_after_the_answer_writes_no_deleted_words_back(setup):
    # No sharing repair to ask, and no sharing decision: the attempt fails after the answer.
    mind, _, clock = setup
    doomed = exploration_result(mind, clock)
    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([doomed], "synthetic-v1", origin="internal", stimulus="exploration-result")
    jobs.run_one(DeletesWhileItThinks(mind, doomed))
    state, data = after(mind, job["id"])
    assert (state, data["error"]) == ("pending", "deepseek-missing-sharing-decision"), "why it ended stays"


def test_a_commit_refused_for_the_deletion_writes_no_deleted_words_back(setup):
    mind, source, _ = setup
    doomed = source("doomed-refused", f"小光说起 {MARKER} 的事")
    jobs = Appraisals(mind)
    job = jobs.enqueue([doomed], "synthetic-v1")
    jobs.run_one(DeletesWhileItThinks(mind, doomed))
    state, data = after(mind, job["id"])
    assert state != "complete" and data.get("error")


def test_a_sharing_repair_writes_no_deleted_words_back_before_or_after_it(setup):
    mind, _, clock = setup
    doomed = exploration_result(mind, clock)
    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([doomed], "synthetic-v1", origin="internal", stimulus="exploration-result")
    reviewer = RepairsSharing(mind, doomed)
    jobs.run_one(reviewer)
    assert reviewer.seen_at_repair == [], "the row written before the repair holds none of the words"
    after(mind, job["id"])
