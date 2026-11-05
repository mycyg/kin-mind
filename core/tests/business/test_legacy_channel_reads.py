"""A turn of the `legacy` assessment channel records nothing its tools read (CL6E follow-up).

`assessment_channel: legacy` keeps the in-session turn: the draft or the assessment runs in the
main session itself, with its tools, and no record of what they returned comes back -- the host
says so, `channel: legacy`, on the receipt. It is treated as a fork turn whose record was cut
short (`truncated`, CL6D-MM-01) from the first read: every delete since the claim or the start may
have been read and stops the words, and none before it does (CL6E-MM-02). Each case deletes while
the model answers, or before it began, and reads what was kept."""
import json
from datetime import timedelta

from kin_mind.appraisal import Appraisal, Appraisals, DailyReview
from kin_mind.context import Contexts
from kin_mind.erasure import reads_truncated
from kin_mind.memory import MemoryContinuity
from kin_mind.state import fork_reads
from test_derived_erasure import answered, paid, prospective_check, queue_row, read_note
from test_erasure import settle, system, texts_everywhere  # noqa: F401  (`system`: the compression fixture)
from test_kin_mind import wish

pytest_plugins = ('test_kin_mind',)

MARKER = "tamberlock"
# What the host's in-session turns return (boundaries.mjs runLegacyAssessment, runMindDraft).
LEGACY = {"channel": "legacy"}
NATIVE = {"channel": "legacy", "provider": "custom", "model": "synthetic", "native_turn_id": "turn-1", "usage": None}


def test_the_legacy_channel_is_a_record_cut_short_from_the_first_read():
    """Kept on a draft's row as a record of no calls, cut short and saying which channel it was;
    found as cut short wherever an assessment's receipt carries it (CL6E follow-up)."""
    assert fork_reads(LEGACY) == {"channel": "legacy", "tool_calls": [], "truncated": True}
    assert reads_truncated({"provider": "deepseek", "native_receipt": NATIVE})
    assert not reads_truncated({"provider": "deepseek", "native_receipt": {"channel": "fork", "tool_calls": []}})


def test_a_draft_of_the_legacy_channel_counts_every_delete_since_its_claim_and_none_before(setup):
    """A note nobody named is deleted while the in-session draft is written: her words are refused as
    for anything named. Drafted again, a delete from before the claim stops nothing."""
    mind, source, clock = setup
    before, _ = read_note(mind, clock, "legacy-before", "早就删掉的一条")
    during, _ = read_note(mind, clock, "legacy-during", "没被点名的一条")
    wish(mind, source, "legacy-wish", content="Say good night")
    mind.engine.delete(before)
    attempt = mind.claim_contact(owner_epoch="owner-1")
    mind.engine.delete(during)
    refused = mind.settle_contact(attempt_id=attempt["id"], state="pending", text=f"晚安，{MARKER}", shown_ids=[],
                                  draft_receipt=LEGACY)
    assert refused["state"] == "canceled" and refused["reason"] == "draft-sources-deleted"
    assert refused["reads_truncated"] is True and refused["draft_receipt"]["channel"] == "legacy"
    clock[0] += timedelta(seconds=301)
    mind.reconsider_contacts(owner_epoch="owner-1")
    again = mind.claim_contact(owner_epoch="owner-1")
    kept = mind.settle_contact(attempt_id=again["id"], state="pending", text="晚安", shown_ids=[], draft_receipt=LEGACY)
    assert kept["state"] == "pending", kept.get("reason")
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_an_appraisal_of_the_legacy_channel_is_refused_by_any_delete_while_it_answers(setup):
    """A note nobody named is deleted while the in-session assessment answers: the commit refuses, the
    row names the note and keeps none of the words, and it is not charged. The retry, with only that
    earlier delete behind it, commits."""
    mind, source, clock = setup
    unnamed, unnamed_record = read_note(mind, clock, "legacy-read", f"没被点名的 {MARKER}")
    primary = source("today", "今天天气不错")

    class During:
        def appraise(self, context):
            mind.engine.delete(unnamed)
            paid(self)
            proposal, receipt = answered(Appraisal(reason=f"想起 {MARKER}", values={"curiosity": 66}))
            return proposal, {**receipt, "native_receipt": NATIVE}

    class Again:
        def appraise(self, context):
            paid(self)
            proposal, receipt = answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))
            return proposal, {**receipt, "native_receipt": NATIVE}

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(During())
    settle(mind.engine)
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["error_detail"]["code"] == "shown-deleted" and count == 0
    assert unnamed_record in data["evaluated_ids"] and mind.read()["dimensions"]["curiosity"]["value"] != 66
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    assert queue_row(mind, job["id"])[0] == "complete" and mind.read()["dimensions"]["curiosity"]["value"] == 55


def test_a_daily_review_of_the_legacy_channel_keeps_no_reason_and_no_change_once_anything_is_deleted(setup):
    """With the behaviour chain off the day is reviewed in the session; a note nobody named is
    deleted while it answers: no change of personality is recorded and the reason is not kept."""
    from kin_mind.state import Evolution

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"behavior_chain": False, "trait_ledger": False})
    claim, assessment = prospective_check(mind, source, clock)
    unnamed, _ = read_note(mind, clock, "legacy-daily", f"没被点名的 {MARKER}")
    baseline = mind.read()["dimensions"]["curiosity"]["baseline"]

    class Daily:
        def appraise(self, context):
            mind.engine.delete(unnamed)
            proposal, receipt = answered(Appraisal(reason=f"因为 {MARKER}，更想去查证", evolution=Evolution(
                claim_id=claim["id"], assessment_id=assessment["id"], baseline_changes={"curiosity": baseline + 2})))
            return proposal, {**receipt, "native_receipt": NATIVE}

    reviewed = DailyReview(mind).run(Daily(), "synthetic-v1")
    settle(mind.engine)
    assert reviewed["state"] == "needs-review" and reviewed["error"] == "Conflict"
    assert reviewed["reason_withheld"] == "sources-changed" and "reason" not in reviewed
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == baseline, "no change was recorded"
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_compression_of_the_legacy_channel_is_used_and_not_kept(system):
    """An appraisal's evidence compressed in the session: nothing names what its tools read, so no
    later delete could be sure to find a cached copy. The appraisal that asked for it uses it, and
    it is not kept."""
    mind, memory, source, clock = system
    engine = mind.engine
    filler = "They checked the harbour path, the tide table and the lamps along the pier. " * 12
    contexts = Contexts(mind)
    items = [contexts.record_item(engine.get(engine.source(source(f"legacy-{i}", f"Harbour walk note {i}. " + filler))["record_ids"][0]))
             for i in range(3)]

    class Session:
        model = "synthetic"

        def structured(self, name, schema, system, payload, **kwargs):
            value = schema.model_validate({"entries": [{"item_ids": payload["allowed_item_ids"], "summary": "They walked the harbour."}],
                                           "omitted_ids": []})
            return value, dict(NATIVE)

    packed = contexts.pack(items, "harbour walk", 600, provider=Session(), allow_model=True, persist=True)
    assert packed["state"] == "compressed" and "They walked the harbour." in packed["text"]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_context_cache").fetchone()[0] == 0
    assert json.dumps(packed["receipt"]).count('"legacy"') >= 1
