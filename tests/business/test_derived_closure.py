"""What a delete takes when a record cites a derived source among its evidence (CL6-MM-01).

A derived source -- a reflection, an exploration report, a creation's event -- goes with its own
root record: that record holds the derived words, and it goes when something it was written from
goes. A note that cites a derived source beside other evidence is an ordinary generated record:
it goes when any of its evidence goes, and it takes nothing it cites with it. And it is marked
unverified, like any other generated record, when an input of it is corrected."""
import json

import pytest

from eventmem.core import Engine, repair
from eventmem.core.db import digest
from eventmem.core.maintenance import deletion_preview, erase_set
from eventmem.core.models import RecordInput, RevisionInput, Scope, SourceInput

from test_erasure import settle
from test_repair_lineage import OldStore, root_id


class Store:
    def __init__(self, tmp_path):
        self.engine = Engine(tmp_path / "db")
        self.scope = Scope(persona="synthetic-closure")
        self.count = 0

    def receive(self, namespace, key, text, *, authority="explicit", derived_from=None):
        self.count += 1
        return self.engine.receive(SourceInput(namespace=namespace, key=key, text=text, scope=self.scope, authority=authority,
                                               occurred_at=f"2026-09-20T08:00:{self.count:02d}+00:00",
                                               metadata={"role": "user", "host_event": "message"} if authority == "explicit" else {}),
                                   derived_from=derived_from)["id"]

    def note(self, key, cites, text):
        """A note as an appraisal writes one (memory.py): the sources of the evidence it cites, and
        their records as its evidence."""
        rid = "mem_" + digest(["note", key])[:32]
        self.engine.add_record(RecordInput(id=rid, kind="knowledge", title=key, content=text, scope=self.scope,
                                           source_ids=sorted(cites), evidence_ids=sorted(root_id(sid) for sid in cites),
                                           generated=True, confirmation="inferred"), "note:" + key)
        return rid

    def status(self, rid):
        with self.engine.db.connect() as conn:
            found = conn.execute("SELECT status FROM records WHERE id=?", (rid,)).fetchone()
        return found[0] if found else None

    def present(self, sid):
        with self.engine.db.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone())


def world(tmp_path):
    """A wish's evidence W, the report R written from it, a new message X, a note N the next
    appraisal wrote from X and R together, and a note N2 written from R alone."""
    store = Store(tmp_path)
    wish = store.receive("kin-owner-input", "wish", "想知道那座桥的来历")
    report = store.receive("kin-exploration", "report", "那座桥建于 1937 年", authority="model", derived_from=[wish])
    said = store.receive("kin-owner-input", "said", "今天路过了那座桥")
    note = store.note("both", [said, report], "她今天路过了查过的那座桥")
    alone = store.note("report-only", [report], "那座桥建于 1937 年")
    return store, wish, report, said, note, alone


def test_deleting_a_message_takes_the_note_that_cites_it_and_leaves_the_derived_source_it_also_cites(tmp_path):
    store, wish, report, said, note, alone = world(tmp_path)
    with store.engine.db.connect() as conn:
        records, sources = erase_set(conn, said)
    assert ({rid for rid in records if rid.startswith("mem_")}, sources) == ({root_id(said), note}, {said})
    preview = deletion_preview(store.engine, said)
    assert (preview["record_count"], preview["source_ids"]) == (2, [said])
    # What the console says a delete does (CL6-MM-09).
    assert "a creation's event -- goes with it" in preview["effect"] and "does not delete the answer to it" in preview["effect"]
    store.engine.delete(said)
    settle(store.engine)
    assert store.status(note) is None
    assert store.present(report) and store.status(root_id(report)) == "active", "the report was not written from the message"
    assert store.status(alone) == "active", "nor was the note written from the report alone"
    assert store.present(wish)


def test_deleting_a_note_leaves_the_derived_source_it_cites(tmp_path):
    store, wish, report, said, note, alone = world(tmp_path)
    with store.engine.db.connect() as conn:
        assert erase_set(conn, note) == ({note}, set())
    store.engine.delete(note)
    settle(store.engine)
    assert store.status(note) is None
    assert store.present(report) and store.status(root_id(report)) == "active"
    assert store.present(said) and store.status(alone) == "active"


def test_a_note_that_cites_a_derived_source_is_unverified_when_its_other_input_is_corrected(tmp_path):
    store, wish, report, said, note, alone = world(tmp_path)
    store.engine.revise(root_id(said), RevisionInput(expected_revision=1, command_id="correct-said", action="correct",
                                                     content="今天路过的是另一座桥", reason="小光更正了"))
    assert store.status(note) == "unverified", "an ordinary generated record keeps its protection"
    assert store.status(root_id(report)) == "active" and store.status(alone) == "active"
    # An archive of the report's own input leaves the report, and what was built on it, as it was.
    record = store.engine.get(root_id(wish))
    store.engine.revise(root_id(wish), RevisionInput(expected_revision=record["revision"], command_id="archive-wish",
                                                     action="archive", reason="test"))
    assert store.status(root_id(report)) == "active" and store.status(alone) == "active"


def test_the_repair_on_a_store_whose_notes_cite_reports_and_creations_erases_only_what_rests_on_a_deletion(tmp_path):
    """A store as appraisals leave one: their notes cite the exploration results and creation
    events they were shown beside the messages (actions.py puts an internal event and the sources
    it names into the evaluation's evidence). The owner deleted one message before the release.
    `lineage` then `reerase`: the report written from that message goes, with the notes that cite
    it and the reflection written from it, and nothing else -- not the creations and the other
    report those notes cite beside it, nor the notes that cite only those."""
    store = OldStore(tmp_path)
    doomed_input, other_input, made_from, thought_of = (store.message(key, f"消息 {key}") for key in ("m1", "m2", "m3", "m4"))
    later, again = store.message("m5", "后来的消息"), store.message("m6", "又一条消息")
    doomed = store.report("explore_1", [root_id(doomed_input)], "第一份报告")
    kept_report = store.report("explore_2", [root_id(other_input)], "第二份报告")
    creation = store.creation("make-1", [made_from], "做好的钟")
    reflection = store.reflection("reflect-1", [root_id(thought_of)], "想了想")
    on_doomed = store.reflection("reflect-2", [root_id(doomed)], "那份报告让我想了很久")
    # An appraisal about a batch that held the deleted message: what its understanding cites stands.
    by_target = store.reflection("reflect-3", [root_id(thought_of)], "又想起那天", about=[doomed_input])

    def note(key, cites):
        rid = "mem_" + digest(["note", key])[:32]
        store.engine.add_record(RecordInput(id=rid, kind="knowledge", title=key, content=f"笔记 {key}", scope=store.scope,
                                            source_ids=sorted(cites), evidence_ids=sorted(root_id(sid) for sid in cites),
                                            generated=True, confirmation="inferred"), "note:" + key)
        return rid

    notes = {
        "doomed+message": note("n1", [later, doomed]),
        "doomed-only": note("n2", [doomed]),
        "doomed+creation": note("n3", [doomed, creation]),
        "doomed+report+creation": note("n4", [doomed, kept_report, creation, again]),
        "creation+message": note("n5", [creation, later]),
        "creation-only": note("n6", [creation]),
        "report+reflection": note("n7", [kept_report, reflection]),
        "reflection-on-doomed": note("n8", [on_doomed]),
    }
    store.engine.delete(doomed_input)  # before the release: nothing tied the report to it
    settle(store.engine)
    assert store.engine.source(doomed)["status"] == "received"
    root = store.engine.db.root
    # The dry run says how much the only deleting step will take, as it will be once `lineage` has
    # run before it: the report, the reflection written from it, the notes that cite either, and
    # the reflection whose appraisal was about the deleted message (CL6-MM-02). A release's expect
    # holds these, so a closure that grew would stop it. By kind too (CL6D-MM-02).
    dry = repair.run(root, apply=False, steps=("lineage", "reerase"))["steps"]["reerase"]["plan"]
    assert {key: dry[key] for key in ("derived_sources", "derived_in_closure", "records", "sources")} == {
        "derived_sources": 2, "derived_in_closure": 1, "records": 8, "sources": 3}
    nothing = dict.fromkeys(repair.KINDS, 0)
    assert dry["derived_by_kind"] == {**nothing, "reports": 1, "reflections": 1} and dry["reflections_by_target"] == 1
    assert dry["closure_by_kind"] == {**nothing, "reflections": 1}
    assert dry["records_by_kind"] == {"own": 3, "notes": 5}, "three sources' own records, five notes citing them"
    alone = repair.run(root, apply=False, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert (alone["records"], alone["sources"], alone["derived_in_closure"]) == (6, 2, 0), "without lineage first, as it is now"
    applied = repair.run(root, apply=True, steps=("lineage", "reerase"))["steps"]
    assert applied["reerase"]["done"]["derived_sources"] == 2
    assert (applied["reerase"]["done"]["records"], applied["reerase"]["done"]["sources"]) == (8, 3), "what the plan said"
    settle(store.engine)
    with store.engine.db.connect() as conn:
        sources = {row[0] for row in conn.execute("SELECT id FROM sources")}
        records = {row[0] for row in conn.execute("SELECT id FROM records")}
    erased = {"doomed+message", "doomed-only", "doomed+creation", "doomed+report+creation", "reflection-on-doomed"}
    assert {name for name, rid in notes.items() if rid not in records} == erased
    assert doomed not in sources and on_doomed not in sources, "the report and the reflection written from it go"
    assert by_target not in sources, "and the reflection written about the deleted message"
    assert {kept_report, creation, reflection, other_input, made_from, thought_of, later, again} <= sources
    assert {root_id(sid) for sid in (kept_report, creation, reflection, later, again)} <= records
    # A second run finds nothing left.
    report = repair.run(root, apply=False, steps=("lineage", "reerase"))["steps"]
    assert set(report["lineage"]["plan"].values()) == {0}
    assert [report["reerase"]["plan"][key] for key in ("derived_sources", "derived_in_closure", "records", "sources",
                                                        "reflections_by_target")] == [0, 0, 0, 0, 0]
    assert set(report["reerase"]["plan"]["derived_by_kind"].values()) == set(report["reerase"]["plan"]["records_by_kind"].values()) == {0}
    with store.engine.db.connect() as conn:
        derived = {row[0]: json.loads(row[1])["derived_from"] for row in conn.execute(
            "SELECT id,data FROM sources WHERE json_extract(data,'$.derived_from') IS NOT NULL")}
    assert set(derived) == {kept_report, creation, reflection}


def doomed_store(tmp_path):
    """A report written, before the release, from a message the owner has deleted since, with a
    note that cites the report."""
    store = OldStore(tmp_path)
    said = store.message("m1", "消息 m1")
    doomed = store.report("explore_1", [root_id(said)], "第一份报告")
    rid = "mem_" + digest(["note", "n1"])[:32]
    store.engine.add_record(RecordInput(id=rid, kind="knowledge", title="n1", content="笔记", scope=store.scope, source_ids=[doomed],
                                        evidence_ids=[root_id(doomed)], generated=True, confirmation="inferred"), "note:n1")
    store.engine.delete(said)
    settle(store.engine)
    return store, doomed


def test_the_apply_deletes_nothing_its_own_dry_run_did_not_count(tmp_path, monkeypatch):
    """The apply holds itself to the plan of the same run (CL6D-MM-02): a closure larger than the
    plan -- here a plan that counted less -- and nothing is deleted; deletes that turned out larger
    than the plan stop the run once they are made."""
    store, doomed = doomed_store(tmp_path)
    plan = repair.run(store.engine.db.root, apply=False, steps=("lineage", "reerase"))["steps"]["reerase"]["plan"]
    assert (plan["derived_sources"], plan["records"], plan["sources"]) == (1, 2, 1)
    repair.run(store.engine.db.root, apply=True, steps=("lineage",))
    with pytest.raises(repair.Exceeded, match="would take more than its plan said: records 2 > 1"):
        repair.apply_reerase(store.engine, plan={**plan, "records": 1})
    assert store.engine.source(doomed)["status"] == "received", "nothing was deleted"
    # A closure that says the deletes take nothing lets them through; what they took stops the run.
    monkeypatch.setattr(repair, "_closure", lambda conn, doomed, pending=None: (set(), set(), set()))
    with pytest.raises(repair.Exceeded, match="took more than its plan said: records 2 > 0, sources 1 > 0"):
        repair.apply_reerase(store.engine, plan={**plan, "records": 0, "sources": 0})


def test_a_run_that_would_delete_more_than_its_dry_run_stops_with_one_line(tmp_path, monkeypatch, capsys):
    """Through the command: the apply's own plan counts less than its deletes would take (as if the
    store had moved under it), the run stops before deleting, exits 1 and says so in one line."""
    store, doomed = doomed_store(tmp_path)
    real = repair.plan_reerase
    monkeypatch.setattr(repair, "plan_reerase", lambda *args, **kwargs: {**real(*args, **kwargs), "records": 1})
    code = repair.main(["--root", str(store.engine.db.root), "--apply", "--steps", "lineage,reerase"])
    assert code == 1
    assert capsys.readouterr().err.strip() == "repair-20260924: stopped: reerase would take more than its plan said: records 2 > 1"
    assert store.engine.source(doomed)["status"] == "received"
