"""The repair's `lineage` step and what `reerase` does with it (CR5-MM-02, follow-up).

A store written before this release holds sources written from others -- reflections,
exploration reports, a creation's events -- with no record of it: no `derived_from`, no
dependencies. `lineage` registers what the store recorded they were written from; a delete of it
then takes them, and nothing but a delete does. An input already deleted before the release
leaves them to `reerase`, which erases them by the delete's own rule, together with the words
older builds left in queue rows, runs and receipts."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from eventmem.core import Engine, repair
from eventmem.core.db import digest
from eventmem.core.models import RevisionInput, Scope, SourceInput

from kin_mind import autonomy_schema
from kin_mind.appraisal import Appraisals
from kin_mind.exploration import Explorations
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind
from test_erasure import settle, texts_everywhere

MARKER = "plughxyzzy"


def root_id(sid):
    return "mem_" + digest([sid, "root"])[:32]


def snapshot(path):
    """Every row of every ordinary table, to prove a dry run wrote nothing."""
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%search%' ORDER BY name")]
        return {table: conn.execute(f"SELECT * FROM '{table}' ORDER BY 1").fetchall() for table in tables}


class OldStore:
    """A store as the release before this one wrote it."""

    def __init__(self, tmp_path):
        self.clock = [datetime(2026, 9, 20, 8, tzinfo=timezone.utc)]
        self.engine = Engine(tmp_path / "db")
        self.scope = Scope(persona="synthetic-lineage")
        self.mind = Mind(self.engine, self.scope, clock=lambda: self.clock[0].isoformat(timespec="microseconds"))
        MemoryContinuity(self.mind)
        Appraisals(self.mind)
        Explorations(self.mind)
        with self.engine.db.connect(write=True) as conn:
            conn.executescript(autonomy_schema.SCHEMA)
        self.revision = 900

    def at(self):
        self.clock[0] += timedelta(seconds=1)
        return self.mind.clock()

    def receive(self, namespace, key, text, *, authority="explicit", kind="observation", version="1", metadata=None):
        return self.engine.receive(SourceInput(namespace=namespace, key=key, version=version, text=text, scope=self.scope,
                                               authority=authority, kind=kind, occurred_at=self.at(),
                                               metadata=metadata or {}))["id"]

    def message(self, key, text):
        return self.receive("kin-owner-input", key, text, metadata={"role": "user", "host_event": "message"})

    def event(self, key, evidence_ids):
        """A mind event's history row: its request names what the appraisal was about."""
        self.revision += 1
        event = "mind_" + digest([key])[:32]
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_events VALUES(?,?,?,?,?,?)",
                         (event, self.scope.key(), self.revision, "affect", self.at(),
                          json.dumps({"request": {"command_id": key, "evidence_ids": evidence_ids}, "snapshot": {}})))
        return event

    def reflection(self, key, evidence_ids, text, *, about=()):
        """As the last release kept one: keyed by its appraisal's event, citing what the understanding
        cited; what the appraisal was about is in that event's history row (`about`; None: no row
        holds the event any more)."""
        event = self.event(key, list(about)) if about is not None else "mind_" + digest([key])[:32]
        return self.receive("kin-reflection", event, "Kin 自己的想法（日记与感想，不是主人的原话或已确认事实）：\n" + text,
                            authority="model", kind="episode",
                            metadata={"role": "assistant", "basis": "internal_thought", "internal": True, "host_event": "diary",
                                      "appraisal_event_id": event, "evidence_ids": evidence_ids})

    def report(self, key, evidence_ids, text):
        result = {"summary": text, "findings": [text], "sources": [], "open_questions": []}
        sid = self.receive("kin-exploration", key, json.dumps({"state": "complete", "result": result, "partial": False}),
                           authority="model", metadata={"host_event": "exploration-result", "exploration_id": key})
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                         (key, self.scope.key(), "complete", self.at(), json.dumps(
                             {"desire_id": "desire-" + key, "selected_brief": f"查清 {MARKER} 的来历",
                              "evidence_ids": evidence_ids, "source_id": sid, "result": result}, ensure_ascii=False)))
        return sid

    def runtime(self, key, event):
        sid = self.receive("kin-runtime", key, json.dumps(event, ensure_ascii=False), authority="operation",
                           metadata={"host_event": event["kind"], "runtime_event_id": key})
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_runtime_events(id,scope,kind,occurred_at,digest,data) VALUES(?,?,?,?,?,?)",
                         ("runtime-" + key, self.scope.key(), event["kind"], event["at"], digest(event),
                          json.dumps({**event, "source_id": sid, "receipt": {"source_id": sid}}, ensure_ascii=False)))
        return sid

    def creation(self, key, input_source_ids, text, *, run=None):
        """A creation's artifact event as the last release wrote it: it names the step's evidence."""
        return self.runtime(key, {"id": key, "kind": "artifact-created", "at": self.at(), "task_id": run or "run-" + key,
                                  "actor": "Kin", "artifact": {"name": "clock.json", "sha256": "0" * 64, "bytes": 12,
                                                               "inspection": {"format": ".json", "excerpt": text}},
                                  "text": text, "input_source_ids": input_source_ids})

    def result(self, key, run, text):
        """A creation's result event as the last release wrote it: no inputs of its own, only the run
        (`task_id`) whose step it settled."""
        return self.runtime(key, {"id": key, "kind": "task-result", "at": self.at(), "task_id": run, "text": text,
                                  "verified": False, "completion_review": {"complete": False, "reason": text}})

    def run(self, key, evidence, text):
        data = {"id": key, "decision": {"id": "decision-" + key, "evidence": evidence, "reason": "依据充分"},
                "result": {"summary": text, "verified": False, "verification_gaps": ["completion-not-established", text],
                           "completion_review": {"complete": False, "reason": text}}}
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_plan_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (key, self.scope.key(), "plan-1", "make", "create", "failed", 0, "worker", 1,
                          json.dumps(data, ensure_ascii=False)))

    def queue_row(self, key, evaluated, text):
        data = {"evidence_ids": [], "evaluated_sources": evaluated, "error": "Conflict",
                "proposed_result": {"reason": text, "values": {"curiosity": 60}}}
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_appraisals VALUES(?,?,?,?,0,1,?)",
                         (key, self.scope.key(), "pending", 0, json.dumps(data, ensure_ascii=False)))
        return data

    def receipt(self, key, evidence_ids, text):
        """A mind command's stored receipt, and the history row of the event it names."""
        event = self.event(key, evidence_ids)
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO commands VALUES(?,?,?)",
                         ("mind:" + digest([key])[:32], "d", json.dumps(
                             {"event_id": event, "revision": self.revision, "proposal": {"reason": text}}, ensure_ascii=False)))
        return event

    def ref(self, sid):
        with self.engine.db.connect() as conn:
            refs = self.mind._evidence(conn, [sid])
        return {key: refs[0][key] for key in ("source_id", "record_id", "hash", "revision", "authority")}

    def row(self, table, key, column="data"):
        with self.engine.db.connect() as conn:
            found = conn.execute(f"SELECT {column} FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(found[0]) if found else None


def plan(root, steps):
    report = repair.run(root, apply=False, steps=steps)
    return {step: entry["plan"] for step, entry in report["steps"].items()}


def test_lineage_gives_old_derived_sources_what_they_were_written_from_and_a_second_run_finds_nothing(tmp_path):
    """The shapes the last release wrote: a reflection names what its understanding cited, and its
    appraisal's history row what the appraisal was about; a report's run names the wish's evidence;
    a creation's artifact event names the step's evidence, and its result event names only its run.
    A native task's reply the host recorded names nothing it was written from."""
    store = OldStore(tmp_path)
    said = store.message("said", "我们聊到了那座桥")
    asked = store.message("asked", "那座桥是哪年建的？")
    reflection = store.reflection("reflect-1", [root_id(said)], "那座桥让我想了很久", about=[asked])
    lost = store.reflection("reflect-2", [root_id(said)], "又想起那座桥", about=None)
    report = store.report("explore_1", [root_id(said)], "那座桥建于 1937 年")
    store.run("run-1", [store.ref(said)], "桥画好了一半")
    creation = store.creation("make-1", [said], "画好了那座桥", run="run-1")
    result = store.result("make-1-result", "run-1", "画好了那座桥")
    observed = store.result("native-1", "task-native-1", "宿主记下的一次原生任务回复")
    # This release's writer kept a result as a fact only when its inputs changed under it: no words,
    # no inputs -- nothing to give a lineage, whatever its run says.
    withheld = store.runtime("make-1-withheld", {"id": "make-1-withheld", "kind": "task-result", "at": store.at(), "task_id": "run-1",
                                                 "text": "[已删除]", "input_source_ids": [], "shown_sources": [],
                                                 "inputs_withheld": "derived-from-changed"})
    store.receipt("appraise-1", [said], "想起那座桥")
    root = store.engine.db.root
    before = snapshot(store.engine.db.path)
    dry = plan(root, ("lineage",))["lineage"]
    assert snapshot(store.engine.db.path) == before, "a dry run writes nothing"
    assert dry == {"reflections": 2, "targets": 1, "targets_unfound": 1, "reports": 1, "creations": 1, "results": 1,
                   "dependencies": 6, "inputs_deleted": 0, "inputs_unresolved": 0, "without_inputs": 0, "runs": 1,
                   "receipts": 1, "results_without_basis": 1}
    done = repair.run(root, apply=True, steps=("lineage",))["steps"]["lineage"]["done"]
    assert done == {"sources": 5, "dependencies": 6, "runs": 1, "receipts": 1}
    with store.engine.db.connect() as conn:
        lineage = {sid: json.loads(data).get("derived_from") for sid, data in conn.execute("SELECT id,data FROM sources")}
        depends = {tuple(row) for row in conn.execute("SELECT record_id,evidence_id FROM dependencies")}
    assert lineage[reflection] == [{"record_id": root_id(said)}, {"source_id": asked}], "what the appraisal was about too"
    assert lineage[lost] == [{"record_id": root_id(said)}] and lineage[report] == [{"record_id": root_id(said)}]
    assert lineage[creation] == [{"source_id": said}] and lineage[result] == [{"source_id": said}], "the result, by its run"
    assert lineage[observed] is None, "a reply the host observed is the conversation record, no derived source"
    assert lineage[withheld] is None
    assert {(root_id(sid), root_id(said)) for sid in (reflection, lost, report, creation, result)} | {(root_id(reflection), root_id(asked))} <= depends
    assert store.row("mind_plan_runs", "run-1")["result"]["evidence_ids"] == sorted([said, root_id(said)])
    with store.engine.db.connect() as conn:
        [receipt] = [json.loads(r[0]) for r in conn.execute("SELECT result FROM commands WHERE id LIKE 'mind:%'")]
    assert receipt["rests_on"] == [said]
    # Idempotent: nothing is left to register, and a second apply writes nothing. The host's reply
    # is still counted, and still left as it is.
    again = plan(root, ("lineage",))["lineage"]
    assert again.pop("results_without_basis") == 1 and set(again.values()) == {0}
    before = snapshot(store.engine.db.path)
    assert repair.run(root, apply=True, steps=("lineage",))["steps"]["lineage"]["done"] == {
        "sources": 0, "dependencies": 0, "runs": 0, "receipts": 0}
    assert snapshot(store.engine.db.path) == before
    # From now on a delete of what they were written from takes them: of what the appraisal was
    # about, the reflection alone; of the message, all the rest.
    store.engine.delete(asked)
    settle(store.engine)
    with store.engine.db.connect() as conn:
        left = {row[0] for row in conn.execute("SELECT id FROM sources")}
    assert reflection not in left and {lost, report, creation, result, observed} <= left
    store.engine.delete(said)
    settle(store.engine)
    with store.engine.db.connect() as conn:
        left = {row[0] for row in conn.execute("SELECT id FROM sources")}
    assert not {lost, report, creation, result} & left and observed in left
    assert texts_everywhere(store.engine, "那座桥") == set()


def test_lineage_fills_in_what_a_source_given_a_lineage_before_lacks_and_leaves_a_complete_one_alone(tmp_path):
    """An earlier run registered what an old reflection's understanding cited and nothing of what
    its appraisal was about; this release's writer stored another reflection with both. The step
    adds the missing reference and dependency to the first, leaves the second, and a second run
    finds nothing (CL6-MM-06)."""
    from eventmem.core.models import SourceInput

    store = OldStore(tmp_path)
    said, asked = store.message("said", "我们聊到了那座桥"), store.message("asked", "那座桥是哪年建的？")
    earlier = store.reflection("reflect-1", [root_id(said)], "那座桥让我想了很久", about=[asked])
    with store.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET data=json_set(data,'$.derived_from',json(?)) WHERE id=?",
                     (json.dumps([{"record_id": root_id(said)}]), earlier))
        conn.execute("INSERT INTO dependencies VALUES(?,?)", (root_id(earlier), root_id(said)))
    event = store.event("reflect-2", [asked])
    complete = store.engine.receive(SourceInput(
        namespace="kin-reflection", key=event, text="Kin 自己的想法：那座桥很老了", scope=store.scope, authority="model",
        kind="episode", occurred_at=store.at(), metadata={"basis": "internal_thought", "appraisal_event_id": event,
                                                          "evidence_ids": [root_id(said)]}),
        derived_from=[root_id(said), asked])["id"]
    root = store.engine.db.root
    dry = plan(root, ("lineage",))["lineage"]
    assert (dry["reflections"], dry["targets"], dry["dependencies"], dry["without_inputs"]) == (1, 1, 1, 0)
    assert repair.run(root, apply=True, steps=("lineage",))["steps"]["lineage"]["done"] == {
        "sources": 1, "dependencies": 1, "runs": 0, "receipts": 0}
    with store.engine.db.connect() as conn:
        lineage = {sid: json.loads(data).get("derived_from") for sid, data in conn.execute("SELECT id,data FROM sources")}
    assert lineage[earlier] == [{"record_id": root_id(said)}, {"source_id": asked}]
    assert lineage[complete] == [{"record_id": root_id(said)}, {"source_id": asked}], "left as its writer stored it"
    again = plan(root, ("lineage",))["lineage"]
    assert set(again.values()) == {0}
    store.engine.delete(asked)
    settle(store.engine)
    with store.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE id IN (?,?)", (earlier, complete)).fetchone()


def status(store, sid):
    with store.engine.db.connect() as conn:
        found = conn.execute("SELECT status FROM records WHERE id=?", (root_id(sid),)).fetchone()
    return found[0] if found else None


def test_only_a_delete_takes_a_derived_source_an_archive_a_new_version_or_a_replacement_does_not(tmp_path):
    """The inputs of old reflections are archived (the maintenance step's archive of host notes
    among them), given a newer version, replaced by another record and merged into a family with
    it. Each reflection stays as it was, active and whole, through the repair's reerase too; the
    one whose input is deleted goes."""
    from eventmem.core.organize import Organizer

    store = OldStore(tmp_path)
    archived = store.message("archived", "要归档的那句话")
    versioned = store.message("versioned", "第一版的那句话")
    replaced, replacement = store.message("replaced", "被替换的那句话"), store.message("replacement", "替换它的那句话")
    merged, twin = store.message("merged", "说了两遍的那句话"), store.message("twin", "说了两遍的那句话。")
    note = store.receive("kin-session-maintenance", "review-1", "宿主请求检查当前原生会话。此事件不是用户消息。",
                         authority="operation", metadata={"maintenance_only": True})
    doomed = store.message("doomed", f"要删除的那句话 {MARKER}")
    reflections = {name: store.reflection("reflect-" + name, [root_id(sid)], f"{name} 让我想了很久")
                   for name, sid in (("archived", archived), ("versioned", versioned), ("replaced", replaced),
                                     ("merged", merged), ("note", note), ("doomed", doomed))}
    root = store.engine.db.root
    repair.run(root, apply=True, steps=("lineage",))

    def revise(sid, action, **extra):
        record = store.engine.get(root_id(sid))
        store.engine.revise(root_id(sid), RevisionInput(expected_revision=record["revision"], command_id=f"{action}:{sid}",
                                                        action=action, reason="test", **extra))

    revise(archived, "archive")
    revise(replaced, "replace", replacement_id=root_id(replacement))
    store.receive("kin-owner-input", "versioned", "第二版的那句话", version="2",
                  metadata={"role": "user", "host_event": "message"})
    Organizer(store.engine).create(store.scope, "同一句话", [root_id(merged), root_id(twin)], kind="family")
    maintenance = repair.run(root, apply=True, steps=("origins", "maintenance"))["steps"]["maintenance"]["done"]
    assert maintenance["archived"] == 1 and status(store, note) == "archived", "the host note was archived as in I2"
    assert status(store, archived) == "archived" and status(store, replaced) == "superseded" and status(store, versioned) == "superseded"
    assert plan(root, ("reerase",))["reerase"]["derived_sources"] == 0
    repair.run(root, apply=True, steps=("reerase",))
    for name in ("archived", "versioned", "replaced", "merged", "note"):
        assert status(store, reflections[name]) == "active", name
        with store.engine.db.connect() as conn:
            assert not conn.execute("SELECT 1 FROM tombstones WHERE key=?", (reflections[name],)).fetchone(), name
    store.engine.delete(doomed)
    settle(store.engine)
    assert status(store, reflections["doomed"]) is None
    with store.engine.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM tombstones WHERE key=?", (reflections["doomed"],)).fetchone()
    assert texts_everywhere(store.engine, MARKER) == set()


def test_reerase_erases_what_rests_on_an_input_deleted_before_the_release_and_the_words_older_builds_left(tmp_path):
    """The owner deleted a message before this release. Its reflections -- one citing it, one whose
    appraisal was about it -- its report, and the creation's artifact and result events stayed --
    nothing tied them to it -- and an older build's erase left words in the queue row, the
    exploration run, the creation run and a command receipt. `lineage` then `reerase`: all of it
    goes by the delete's rule; identities, states and codes stay; a second run finds nothing."""
    store = OldStore(tmp_path)
    said = store.message("said", f"我小时候住在 {MARKER} 街")
    kept = store.message("kept", "今天天气不错")
    reflection = store.reflection("reflect-1", [root_id(said)], f"{MARKER} 街让我想了很久")
    about = store.reflection("reflect-3", [root_id(kept)], f"天气不错，想起 {MARKER} 街", about=[said])
    report = store.report("explore_1", [root_id(said)], f"{MARKER} 街建于 1937 年")
    said_ref = store.ref(said)
    store.run("run-1", [said_ref], f"{MARKER} 街画好了一半")
    creation = store.creation("make-1", [said], f"画好了 {MARKER} 街", run="run-1")
    result = store.result("make-1-result", "run-1", f"{MARKER} 街画好了一半")
    unrelated = store.reflection("reflect-2", [root_id(kept)], "天气不错的一天")
    queue = store.queue_row("appraisal-1", [said_ref], f"想起她住过 {MARKER} 街")
    store.receipt("appraise-1", [said], f"她住过 {MARKER} 街")
    exploration = store.row("mind_explorations", "explore_1")
    # The delete, as the release before this one made it; then what its erase left, restored: the
    # rule it had did not know these fields.
    store.engine.delete(said)
    with store.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET data=? WHERE id='appraisal-1'", (json.dumps(queue, ensure_ascii=False),))
        conn.execute("UPDATE mind_explorations SET data=? WHERE id='explore_1'", (json.dumps(exploration, ensure_ascii=False),))
        conn.execute("INSERT OR REPLACE INTO mind_state VALUES(?,?,?)", (store.scope.key(), 1, json.dumps(
            {"revision": 1, "concerns": {"concern-1": {"id": "concern-1", "kind": "care", "status": "active",
                                                       "evidence": [said_ref], "target": f"{MARKER} 街的老房子"}}},
            ensure_ascii=False)))
    settle(store.engine)
    left = {table for table, _ in texts_everywhere(store.engine, MARKER)}
    assert {"records", "mind_appraisals", "mind_explorations", "mind_plan_runs", "commands", "mind_state"} <= left, left
    root = store.engine.db.root
    before = snapshot(store.engine.db.path)
    dry = plan(root, ("lineage", "reerase"))
    assert snapshot(store.engine.db.path) == before
    assert dry["lineage"]["inputs_deleted"] == 5 and dry["reerase"]["derived_sources"] == 5
    assert dry["reerase"]["receipts"] == 0, "the receipt names what it rests on only once lineage has run"
    applied = repair.run(root, apply=True, steps=("lineage", "reerase"))["steps"]
    assert applied["lineage"]["done"]["sources"] == 6 and applied["reerase"]["done"]["derived_sources"] == 5
    assert applied["reerase"]["done"]["receipts"] == 1
    settle(store.engine)
    assert texts_everywhere(store.engine, MARKER) == set()
    with store.engine.db.connect() as conn:
        for sid in (reflection, about, report, creation, result):
            assert conn.execute("SELECT 1 FROM tombstones WHERE key=?", (sid,)).fetchone(), sid
        assert conn.execute("SELECT 1 FROM sources WHERE id=?", (unrelated,)).fetchone(), "what rests on nothing deleted stays"
    run = store.row("mind_plan_runs", "run-1")
    assert run["result"]["verification_gaps"] == ["completion-not-established", "[已删除]"] and run["result"]["verified"] is False
    row = store.row("mind_appraisals", "appraisal-1")
    assert row["error"] == "Conflict" and row["proposed_result"]["values"] == {"curiosity": 60}
    assert store.row("mind_explorations", "explore_1")["desire_id"] == "desire-explore_1"
    with store.engine.db.connect() as conn:
        concern = json.loads(conn.execute("SELECT data FROM mind_state WHERE scope=?", (store.scope.key(),)).fetchone()[0])["concerns"]["concern-1"]
    assert concern["target"] == "[已删除]" and concern["kind"] == "care"
    # A second run finds nothing left.
    again = plan(root, ("lineage", "reerase"))
    assert set(again["lineage"].values()) == {0}, again["lineage"]
    assert (again["reerase"]["derived_sources"], again["reerase"]["receipts"], again["reerase"]["derived_rows"]) == (0, 0, 0)

