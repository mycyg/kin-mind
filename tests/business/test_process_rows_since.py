"""What a process row's model read with its tools counts, in every erase, only from where its call
began; and a contact attempt names what its draft was shown from its claim on (CL6E-MM-02, with
WS6's CL6E-MM-01 and CL6D-MM-04).

A delete made before a draft's claim, an appraisal's attempt or a daily review's start had taken its
words out of everything the model could read: a tool could hand it a tombstone reference and no
words. A delete counts what it takes itself, after every row there is. The repair's reerase passes
every delete there ever was, and read the receipts of rows written since as if those deletes had
come after them. Now each receipt counts the deletes after the mark its call began at -- the row's,
a stored proposal's or a rejected result's own, a seed's `seed_tombstone_mark` -- and a part with
none counts from its row's. A row without any, as a release before this one wrote, counts them all.

A row that never names what it was shown is one a release before this one wrote, and any delete
takes its words (CL6D-MM-04). A contact attempt of this release names it from its claim, as
nothing yet, whether or not a draft ever comes back: no delete of something else takes its words.
And a stored proposal a delete took words from, by whichever erase, is judged afresh, never taken
up as it was left."""
import json

from eventmem.core import repair

from kin_mind import erasure
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.erasure import ERASED, UNNAMED_MARK
from kin_mind.memory import MemoryContinuity
from test_derived_erasure import answered, paid, queue_row, read_note
from test_fork_reads import contact_row, fork_receipt
from test_kin_mind import wish
from test_unnamed_process_rows import TABLES

pytest_plugins = ('test_kin_mind',)


def reads(*ids):
    """A fork turn's record whose tool returned `ids` (boundaries.mjs `forkReceipt`)."""
    return fork_receipt(*ids)


def words(value):
    return json.dumps(value, ensure_ascii=False)


def test_the_reerase_counts_what_a_process_row_read_only_from_where_its_call_began(setup, monkeypatch):
    """A note is read by the tools of calls that began before it was deleted and of calls that began
    after, the latter handed only its tombstone reference. The delete is one a release before this
    one made, which took no words. The reerase takes the words of every call that began before the
    delete -- a row, a stored proposal, a rejected result, a seed -- and of a row that keeps no mark
    at all; it leaves every call that began after, and a part without a mark of its own goes by its
    row's."""
    mind, source, clock = setup
    engine = mind.engine
    Appraisals(mind)
    scope = mind.scope.key()
    note_source, note_record = read_note(mind, clock, "read-by-all", "工具读到的一条")
    with engine.db.connect(write=True) as conn:
        conn.execute(TABLES["mind_daily_reviews"])
        before = erasure.tombstone_mark(conn)
    # Every row names what its model was shown (`evaluated_ids`), and none names the note: only
    # what its tools read ties it to the note.
    early = {
        "mind_appraisals": {
            "row-before": ("complete", {"tombstone_mark": before, "evaluated_ids": [], "receipt": reads(note_record, note_source),
                                        "proposed_result": {"reason": "早先的判断 alder"}}),
        },
        "mind_contacts": {
            "draft-before": ("accepted", {"tombstone_mark": before, "evaluated_ids": [], "draft_receipt": reads(note_record, note_source),
                                          "text_excerpt": "早先的草稿 alder", "desire_ids": ["wish-1"]}),
        },
        "mind_daily_reviews": {
            "2026-09-20": ("complete", {"tombstone_mark": before, "evaluated_ids": [], "receipt": reads(note_record, note_source),
                                        "reason": "早先的日评 alder"}),
        },
    }

    def insert(rows):
        with engine.db.connect(write=True) as conn:
            for key, (state, data) in rows.get("mind_appraisals", {}).items():
                conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,0,?)", (key, scope, state, json.dumps(data)))
            for key, (state, data) in rows.get("mind_contacts", {}).items():
                conn.execute("INSERT INTO mind_contacts(id,scope,state,data) VALUES(?,?,?,?)", (key, scope, state, json.dumps(data)))
            for key, (state, data) in rows.get("mind_daily_reviews", {}).items():
                conn.execute("INSERT INTO mind_daily_reviews VALUES(?,?,?,?)", (scope, key, state, json.dumps(data)))

    insert(early)
    # The delete, as a release before this one made it: the tombstone and nothing more.
    monkeypatch.setattr(erasure, "erase", lambda *args, **kwargs: {})
    monkeypatch.setattr(erasure, "queue_history", lambda *args, **kwargs: None)
    engine.delete(note_source)
    monkeypatch.undo()
    with engine.db.connect() as conn:
        after = erasure.tombstone_mark(conn)
        # The source went last: the mark after the delete is its own deletion fact.
        assert conn.execute("SELECT rowid FROM tombstones WHERE key=?", (note_source,)).fetchone()[0] == after > before
    late = {
        "mind_appraisals": {
            # Began after the delete; what it kept to take up began before it, and so did the earlier
            # attempt whose result was rejected.
            "row-after": ("pending", {"tombstone_mark": after, "evaluated_ids": [], "receipt": reads(note_record, note_source),
                                      "proposed_result": {"reason": "之后的判断 birch"},
                                      "reuse": {"proposal": {"reason": "留着复用的 cedar"}, "receipt": reads(note_record, note_source),
                                                "tombstone_mark": before, "manifest_digest": "m", "conflict": {}, "n": 0, "sources": []},
                                      "rejected_results": [
                                          {"reason": "missing-target-decision", "proposal": {"reason": "早先被退回的 alder"},
                                           "receipt": reads(note_record, note_source), "tombstone_mark": before},
                                          {"reason": "missing-target-decision", "proposal": {"reason": "之后被退回的 birch"},
                                           "receipt": reads(note_record, note_source)}]}),
            # Its own call began after the delete; the parent's call its seed came from, before.
            "seed-before": ("pending", {"tombstone_mark": after, "evaluated_ids": [], "stimulus": "memory-enrichment",
                                        "seed_receipt": reads(note_record, note_source), "seed_tombstone_mark": before,
                                        "seed_memory": {"summary": "父评估写下的 alder"}}),
            "seed-after": ("pending", {"evaluated_ids": [], "stimulus": "memory-enrichment",
                                       "seed_receipt": reads(note_record, note_source), "seed_tombstone_mark": after,
                                       "seed_memory": {"summary": "父评估写下的 birch"}}),
            # Names what it was shown and keeps no mark: every delete counts.
            "unmarked": ("complete", {"evaluated_ids": [], "receipt": reads(note_record, note_source), "proposed_result": {"reason": "不知何时 alder"}}),
        },
        "mind_contacts": {
            "draft-after": ("accepted", {"tombstone_mark": after, "evaluated_ids": [], "draft_receipt": reads(note_record, note_source),
                                         "text_excerpt": "之后的草稿 birch", "desire_ids": ["wish-2"]}),
        },
        "mind_daily_reviews": {
            "2026-09-21": ("complete", {"tombstone_mark": after, "evaluated_ids": [], "receipt": reads(note_record, note_source),
                                        "reason": "之后的日评 birch"}),
        },
    }
    insert(late)
    root = engine.db.root
    repair.run(root, steps=("reerase",))
    repair.run(root, apply=True, steps=("reerase",))
    with engine.db.connect() as conn:
        appraisals = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT id,data FROM mind_appraisals")}
        contacts = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT id,data FROM mind_contacts")}
        daily = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT day,data FROM mind_daily_reviews")}
    # Every call that began before the delete, and the row that cannot say when it began: no words.
    for data in (appraisals["row-before"], contacts["draft-before"], daily["2026-09-20"], appraisals["unmarked"],
                 appraisals["seed-before"]):
        assert "alder" not in words(data) and ERASED in words(data), data
        assert UNNAMED_MARK not in data, "taken by what it read, not as a row that names nothing"
    # Every call that began after it: as written.
    assert appraisals["row-after"]["proposed_result"]["reason"] == "之后的判断 birch"
    assert contacts["draft-after"]["text_excerpt"] == "之后的草稿 birch"
    assert daily["2026-09-21"]["reason"] == "之后的日评 birch"
    assert appraisals["seed-after"]["seed_memory"]["summary"] == "父评估写下的 birch"
    # Within the row: the proposal kept to take up and the earlier rejected result began before it
    # and lose their words; the rejected result without a mark goes by its row's, and keeps them.
    row = appraisals["row-after"]
    assert row["reuse"]["proposal"]["reason"] == ERASED and erasure.read_ids(row["reuse"]["receipt"]) == [note_record, note_source]
    assert [item["proposal"]["reason"] for item in row["rejected_results"]] == [ERASED, "之后被退回的 birch"]
    # Nothing is left to take: a second reerase finds nothing.
    again = repair.run(root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert again["derived_rows"] == 0, again["layers"]


def test_a_contact_attempt_names_what_its_draft_was_shown_from_its_claim_and_no_other_delete_takes_its_words(setup):
    """One attempt is settled before any draft came back (`draft-not-started`), another after its
    draft was sent. A delete of something neither names takes no words from either: each names
    what its draft was shown from its claim on, nothing for the first. A row as a release before this
    one wrote it, naming nothing, loses its words to the same delete (CL6D-MM-04)."""
    mind, source, clock = setup
    wish(mind, source, "never-drafted", content="Ask about the kite")
    first = mind.claim_contact(owner_epoch="owner-1")
    assert first["evaluated_ids"] == [], "named from the claim: nothing shown yet"
    mind.settle_contact(attempt_id=first["id"], state="canceled", reason="draft-not-started")
    wish(mind, source, "drafted", content="Ask about the lanterns")
    second = mind.claim_contact(owner_epoch="owner-1")
    mind.settle_contact(attempt_id=second["id"], state="pending", text="灯笼挂好了吗", shown_ids=[], draft_receipt=fork_receipt())
    mind.settle_contact(attempt_id=second["id"], state="accepted", message_id="wx-1")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_contacts(id,scope,state,data) VALUES('old-contact',?,'accepted',?)",
                     (mind.scope.key(), json.dumps({"desire_ids": ["wish-0"], "reason": "早先的理由", "text_excerpt": "早先的话"})))
    before = {key: contact_row(mind, key) for key in (first["id"], second["id"])}
    assert before[first["id"]][1]["desire"]["content"] == "Ask about the kite"
    mind.engine.delete(source("elsewhere", "别的事"))
    for key in (first["id"], second["id"]):
        assert contact_row(mind, key) == before[key], key
    old = contact_row(mind, "old-contact")[1]
    assert old["text_excerpt"] == ERASED and old[UNNAMED_MARK] is True


def test_a_stored_proposal_a_delete_took_words_from_is_judged_afresh(setup, monkeypatch):
    """A proposal kept after a conflict it may be reused over; an erase takes its words, and the
    reuse gate has no deletion fact to go by -- as when the erase counted a delete from before the
    proposal's call. It is judged afresh, never taken up as the erase left it."""
    from eventmem.core.models import RevisionInput

    from kin_mind import revalidation

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": "昨天说的一句话"})
    primary = source("today", "今天天气不错")

    class Reviser:
        def appraise(self, context):
            record = mind.engine.get(recalled["record_id"])
            mind.engine.revise(recalled["record_id"], RevisionInput(expected_revision=record["revision"], command_id="fix-earlier",
                                                                    action="correct", content="昨天说的另一句话", reason="更正"))
            paid(self)
            return answered(Appraisal(reason="想出去走走", values={"curiosity": 66}))

    decided = []

    def reuse(jobs, provider, row, data, context, stored, **kwargs):
        decided.append(stored)
        return None

    class Again:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reviser())
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["reuse"]["proposal"]["reason"] == "想出去走走", data.get("error_detail")
    data["reuse"]["proposal"]["reason"] = ERASED
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET data=?,available=0 WHERE id=?", (json.dumps(data), job["id"]))
    monkeypatch.setattr(revalidation, "resume", reuse)
    jobs.run_one(Again())
    assert decided == [], "not taken up"
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and mind.read()["dimensions"]["curiosity"]["value"] == 55
    assert data["proposed_result"]["reason"] == "重新看了一遍今天"
