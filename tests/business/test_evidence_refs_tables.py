"""The tables keep evidence references the way the document does (the owner's decision, 2026-09-28:
来源的元数据只做为索引检索，不应该注入哈，也不必要引用，只需要可以溯源就行).

What is asserted here: whatever a reader of a stored reference needs of its source -- the class a graph
item is shown under, whether its evidence is the owner's explicit word, what the isolation migration
moves -- it reads from the source's own row, so a full reference and a slim one read the same; once a
group of tables is marked its writers store references slim, and unmarked they store them exactly as
before; a replay answers what the first call answered; the migration reads by default, applies in
batches, applies once, survives an interruption and undoes exactly -- every table row byte for byte,
including a reference written before `received_at` was kept, one whose copied words an erase blanked,
one twice in a list, one whose source is gone; an erase takes what the undo kept of what it erases;
and a frozen memory context shows a model the same whether its references are full or slim.

Synthetic replays only: an injected clock, sources through the engine, no model and no network."""
import json

import pytest
from test_erasure import texts_everywhere
from test_event_graph import system  # noqa: F401  (the graph fixture)
from test_evidence_refs import MARKER, carrying, refs_in

from eventmem.core.db import dumps
from eventmem.core.models import SourceInput
from eventmem.core.read_policy import ReadPolicy, SourceFacts, ref_classes
from kin_mind import evidence_refs
from kin_mind.appraisal import Appraisals, appraisal_context, queue_row
from kin_mind.erasure import ERASED
from kin_mind.graph import GraphAssessment, GraphNode
from kin_mind.isolation_migration import IsolationMigration

AGENT = "synthetic-v1"


# --- helpers -----------------------------------------------------------------------------------------

def said(mind, key, **metadata):
    """An owner message whose source carries metadata of its own, as a host message does."""
    return mind.engine.receive(SourceInput(
        namespace="synthetic", key=key, scope=mind.scope, authority="explicit", occurred_at=mind.clock(),
        text=f"synthetic message {key}", extract=False,
        metadata={"role": "user", "host_event": "message", "title": f"{MARKER} title {key}", **metadata}))["id"]


def written(mind, key, **metadata):
    """Material the host wrote, not the owner's words: what its metadata says it is decides its class
    (the owner's own words are always experience)."""
    return mind.engine.receive(SourceInput(
        namespace="synthetic", key=key, scope=mind.scope, authority="model", occurred_at=mind.clock(),
        text=f"synthetic material {key}", extract=False,
        metadata={"role": "assistant", "title": f"{MARKER} title {key}", **metadata}))["id"]


def unclassified(mind, sid):
    """A source classified before the classification table existed: no row, so what it is comes
    from what is stored about it -- its own row now, the reference's copy before."""
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM source_evidence_class WHERE source_id=?", (sid,))
        mind.engine.db.bump(conn)


def rows(mind, table):
    with mind.engine.db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            return {}
        return {row[0]: row[1] for row in conn.execute(f"SELECT rowid,data FROM {table} ORDER BY rowid")}


def every_table(mind):
    return {name: rows(mind, name) for name in evidence_refs.TABLES}


def full_left(mind):
    """Every table row that still carries a reference with what the trace drops."""
    return {(name, rowid) for name, found in every_table(mind).items() for rowid, data in found.items()
            if carrying(json.loads(data))}


def slim_left(mind):
    return {(name, rowid) for name, found in every_table(mind).items() for rowid, data in found.items()
            if any(evidence_refs.is_slim(ref) for _, ref in refs_in(json.loads(data)))}


def mark(mind, on=True):
    with mind.engine.db.connect(write=True) as conn:
        for statement in evidence_refs.SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        for group in evidence_refs.GROUPS:
            evidence_refs._mark(conn, mind.scope.key(), group, on, mind.clock())


def node(memory, key, evidence, event="graph", kind="entity", **fields):
    with memory.mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, evidence)
        memory.graph.apply(conn, GraphAssessment(nodes=[GraphNode(key=key, kind=kind, title=key, evidence_ids=evidence, **fields)]),
                           refs, event, {})
        return memory.graph.get(conn, memory.graph.identifier(kind, [event, key]))


def habits(memory, sid, command="habit", revision=0):
    return memory.habits.update({"command_id": command, "expected_revision": revision, "evidence_ids": [sid],
                                 "reason": "Explicit owner request",
                                 "preferences": {"exploration_directions": ["fiction"], "reply_choice": "autonomous"}})


def plan(mind, sid, key):
    from kin_mind.memory import MemoryContinuity
    from kin_mind.plans import AutonomousPlans
    MemoryContinuity(mind).configure({"autonomous_plans": True})
    return AutonomousPlans(mind).manage({"command_id": "create:" + key, "action": "create", "key": key, "goal": "list " + key,
                                         "motivation": "a plan of ours", "reason": "sourced", "evidence_ids": [sid],
                                         "steps": [{"id": "list", "actor": "create", "goal": "write it", "completion": "saved"}]})


def explored(mind, key):
    """An exploration result's source, as the host receives one: what an appraisal's targets cite."""
    return mind.engine.receive(SourceInput(
        namespace="synthetic-exploration", key=key, scope=mind.scope, authority="model", occurred_at=mind.clock(),
        text=f"exploration result {key}", extract=False,
        metadata={"host_event": "exploration-result", "exploration_id": "explore_" + key,
                  "title": f"{MARKER} pages read {key}", "sources": [{"url": "https://example.test/" + key,
                                                                      "excerpt": f"{MARKER} excerpt {key}"}]}))["id"]


# --- readers: what a source is comes from its row ----------------------------------------------------

def test_a_slim_reference_classifies_as_its_full_one_from_the_sources_own_row(system):
    mind, _, _, _ = system
    example = written(mind, "example", examples_are_synthetic=True)
    plain = said(mind, "plain")
    unclassified(mind, example)
    with mind.engine.db.connect() as conn:
        full = sorted(mind._evidence(conn, [example, plain]), key=lambda ref: ref["source_id"] != example)
        slim = [evidence_refs.trace(ref) for ref in full]
        assert [f.kind for f in ref_classes(mind.engine, conn, mind.scope, full)] == ["synthetic_example", "experience"]
        assert ref_classes(mind.engine, conn, mind.scope, slim) == ref_classes(mind.engine, conn, mind.scope, full)
        policy = ReadPolicy.load(mind.engine, mind.scope, "experience_recall", conn=conn)
        item = {"id": "graph_item", "evidence": [full[0]]}
        lean = {"id": "graph_item", "evidence": [slim[0]]}
        # Without records the evidence decides: a slim reference is classified from its source's row.
        assert policy.node_class(item).kind == "synthetic_example"
        assert policy.node_class(lean, None, SourceFacts(conn)) == policy.node_class(item)
        assert not policy.node_visible(lean, None, SourceFacts(conn))
        # A reference whose source is gone keeps what it carries itself.
        facts = SourceFacts(conn)
        assert facts.of({"source_id": "src_gone", "record_id": "mem_gone", "hash": "h", "namespace": "n"})["namespace"] == "n"
        assert all(facts.of(lean)[key] == ref[key] for lean, ref in zip(slim, full) for key in evidence_refs.DROPPED)


def test_a_graph_read_hides_what_a_slim_reference_says_is_not_experience(system):
    mind, memory, _, _ = system
    example = written(mind, "hidden-example", examples_are_synthetic=True)
    unclassified(mind, example)
    mark(mind)
    item = node(memory, "hidden", [example])
    assert not carrying(item) and item["evidence"]
    with mind.engine.db.connect(write=True) as conn:
        # Only the evidence can say what it is: no record of it is left to read.
        conn.execute("UPDATE records SET deleted=1 WHERE id=?", (item["evidence"][0]["record_id"],))
    read = memory.graph.read(limit=50)
    assert item["id"] not in {n["id"] for n in read["nodes"]}
    assert memory.graph.detail(item["id"])["evidence_class"] == "synthetic_example"


def test_the_owners_explicit_word_is_read_from_the_source_when_a_stored_reference_no_longer_says_it(system):
    mind, memory, _, _ = system
    mark(mind)
    owner = said(mind, "owner-word")
    configuration = written(mind, "setup", configuration_only=True)
    unclassified(mind, configuration)
    a, b = node(memory, "a", [owner]), node(memory, "b", [owner])
    with mind.engine.db.connect(write=True) as conn:
        first = memory.graph.link(conn, a["id"], "related", b["id"], memory.graph.proof(conn, [owner]), basis="explicit",
                                  reason="she said so")
        assert first["basis"] == "explicit" and not carrying(first)
        # The stored explicit reference is slim now; a configuration source cited beside it moves out
        # of the evidence, and what is left is still her explicit word.
        again = memory.graph.link(conn, a["id"], "related", b["id"], memory.graph.proof(conn, [configuration]),
                                  basis="explicit", reason="she said so")
    assert [r["source_id"] for r in again["evidence"]] == [owner]
    assert [r["source_id"] for r in again["configuration_evidence"]] == [configuration]
    assert again["basis"] == "explicit"


def test_the_isolation_migration_moves_and_keeps_by_the_source_not_by_the_copy(system):
    mind, _, _, _ = system
    owner = said(mind, "iso-owner")
    configuration = written(mind, "iso-setup", configuration_only=True)
    unclassified(mind, configuration)
    migration = IsolationMigration(mind)
    with mind.engine.db.connect(write=True) as conn:
        refs = [evidence_refs.trace(ref) for ref in mind._evidence(conn, [owner, configuration])]
        stored = {"id": "graph_node_iso", "kind": "entity", "basis": "explicit", "evidence": refs}
        policy = ReadPolicy.load(mind.engine, mind.scope, "experience_recall", conn=conn)
        moved, kept = migration._node_moves(policy, stored, SourceFacts(conn))
    assert [r["source_id"] for r in moved] == [configuration] and [r["source_id"] for r in kept] == [owner]


# --- writers: slim once marked, exactly as before unmarked -------------------------------------------

def test_unmarked_every_writer_stores_references_exactly_as_before(system):
    mind, memory, _, _ = system
    sid = said(mind, "unmarked")
    item = node(memory, "unmarked", [sid])
    with mind.engine.db.connect() as conn:
        written = mind._evidence(conn, [sid])
        assert not evidence_refs.table_marked(conn, mind.scope.key(), "mind_graph_nodes")
        data = {"evaluated_sources": written, "exploration_targets": [{**written[0], "exploration_id": "x"}]}
        # Not a copy and not a byte different: the unmarked writer is the writer it was.
        assert evidence_refs.as_stored(conn, mind.scope.key(), "mind_appraisals", data) is data
        assert queue_row(conn, mind.scope.key(), data) == dumps(data)
    assert item["evidence"] == written
    stored = [json.loads(data) for data in rows(mind, "mind_graph_nodes").values() if json.loads(data)["id"] == item["id"]]
    revision = [json.loads(data) for data in rows(mind, "mind_graph_revisions").values() if json.loads(data)["id"] == item["id"]]
    assert stored == [item] and revision[-1] == item
    result = habits(memory, sid)
    assert result["entries"]["reply_choice"]["evidence"] == written
    assert json.loads(next(iter(rows(mind, "mind_conversation_habits").values()))) == result
    plan(mind, sid, "unmarked")
    assert [json.loads(data)["evidence"] for data in rows(mind, "mind_plans").values()] == [written]
    assert [json.loads(data)["evidence"] for data in rows(mind, "mind_plan_history").values()] == [written]


def test_once_marked_the_graph_the_queue_and_the_habits_store_slim_and_read_the_same(system):
    mind, memory, _, _ = system
    mark(mind)
    sid, other = said(mind, "marked"), said(mind, "marked-other")
    a, b = node(memory, "ma", [sid]), node(memory, "mb", [other])
    with mind.engine.db.connect(write=True) as conn:
        memory.graph.link(conn, a["id"], "related", b["id"], memory.graph.proof(conn, [sid, other]), reason="both")
    command = {"id": a["id"], "expected_revision": a["revision"], "target_id": b["id"], "target_revision": b["revision"],
               "action": "merge", "command_id": "merge-marked", "reason": "same thing", "evidence_ids": [sid]}
    merged = memory.graph.revise(command)
    assert merged == memory.graph.revise(command), "a replay answers what the first call answered"
    first = habits(memory, sid, "marked-habit")
    assert first == habits(memory, sid, "marked-habit")
    job = Appraisals(mind).enqueue([explored(mind, "marked")], AGENT, stimulus="exploration-result")
    plan(mind, sid, "marked")
    assert rows(mind, "mind_plans") and rows(mind, "mind_plan_history")
    assert not full_left(mind), "every table row keeps its references to what their readers read"
    stored = json.loads(next(iter(rows(mind, "mind_appraisals").values())))
    target = stored["exploration_targets"][0]
    assert target["exploration_id"] == "explore_marked" and set(target) - {"exploration_id"} <= set(evidence_refs.TRACE)
    # What reads a stored target reads it as it read a full one: freshness, and the targets taken up again.
    assert Appraisals(mind).exploration_targets(stored, []) == stored["exploration_targets"]
    assert job["id"]
    with mind.engine.db.connect() as conn:
        assert mind._fresh(conn, [r for _, r in refs_in(stored)])
    assert memory.graph.read(focus=b["id"], hops=1)["nodes"]
    assert memory.habits.read()["preferences"]["exploration_directions"] == ["fiction"]


def test_a_row_the_migration_has_not_reached_is_not_revised_again_for_its_shape_alone(system):
    mind, memory, _, _ = system
    sid = said(mind, "shape")
    item = node(memory, "shape", [sid])
    assert carrying(item)
    mark(mind)
    with mind.engine.db.connect(write=True) as conn:
        again = memory.graph._put(conn, {**item})
    assert again["revision"] == item["revision"], "the same node in a slimmer shape is the same revision"
    assert json.loads(next(d for d in rows(mind, "mind_graph_nodes").values() if json.loads(d)["id"] == item["id"])) == item


def test_a_replay_answers_what_the_first_call_did_when_it_copied_a_row_the_migration_had_not_reached(system):
    mind, memory, _, _ = system
    sid, other = said(mind, "replay"), said(mind, "replay-other")
    a, b = node(memory, "ra", [sid]), node(memory, "rb", [other])
    assert carrying(a) and carrying(b)
    mark(mind)
    command = {"id": a["id"], "expected_revision": a["revision"], "target_id": b["id"], "target_revision": b["revision"],
               "action": "merge", "command_id": "merge-replay", "reason": "same thing", "evidence_ids": [sid]}
    merged = memory.graph.revise(command)
    assert not carrying(merged), "the command keeps what it copied as it stores it"
    assert merged == memory.graph.revise(command)


def test_a_frozen_memory_context_shows_a_model_the_same_full_or_slim(system):
    """The rendered request, which a judgment's digest and a manifest are taken over, does not move
    when the queue row's frozen context is slimmed: a cached judgment is neither lost nor wrongly
    served by the migration."""
    mind, memory, _, _ = system
    sid = said(mind, "frozen")
    item = node(memory, "frozen", [sid])
    frozen = {"graph_candidates": [item], "shares": [], "works": [], "pending_events": [], "recent_interaction": [],
              "conversation_habits": {"revision": 1, "preferences": {}, "entries": {"reply_choice": {"evidence": item["evidence"]}}}}
    full = appraisal_context({"memory_context": frozen, "new_evidence": []})
    slim = appraisal_context({"memory_context": evidence_refs.lean(frozen), "new_evidence": []})
    assert carrying(frozen) and not carrying(evidence_refs.lean(frozen))
    assert dumps(full) == dumps(slim)


# --- the migration ------------------------------------------------------------------------------------

def legacy(mind, memory):
    """Every group of tables as the release before this one leaves them, and the quirks an exact undo
    has to put back: a reference written before `received_at` was kept, one whose copied words an
    erase blanked, the same reference twice in one list, one whose source is gone, a tombstone."""
    first, second, third = said(mind, "first"), said(mind, "second"), said(mind, "third")
    a, b = node(memory, "la", [first, second]), node(memory, "lb", [third])
    with mind.engine.db.connect(write=True) as conn:
        memory.graph.link(conn, a["id"], "related", b["id"], memory.graph.proof(conn, [first, third]), reason="both")
    memory.graph.revise({"id": a["id"], "expected_revision": a["revision"] + 0, "action": "correct",
                         "changes": {"title": "corrected"}, "command_id": "legacy-correct", "reason": "fix",
                         "evidence_ids": [first]})
    habits(memory, second, "legacy-habit")
    Appraisals(mind).enqueue([explored(mind, "legacy")], AGENT, stimulus="exploration-result")
    gone = said(mind, "gone")
    with mind.engine.db.connect(write=True) as conn:
        by = {ref["source_id"]: ref for ref in mind._evidence(conn, [first, second, third, gone])}
        refs = [by[first], by[second], by[third], by[gone]]
        quirks = [dict(refs[0]), dict(refs[0]), dict(refs[1]), dict(refs[2]), refs[3],
                  {**{k: refs[2][k] for k in ("source_id", "record_id", "hash", "revision", "authority")}, "erased": True}]
        quirks[1].pop("received_at")
        quirks[2]["metadata"] = {**quirks[2]["metadata"], "title": ERASED}
        # A copy with words its source's row no longer has: kept for the undo, and an erase's to take.
        quirks[3]["metadata"] = {**quirks[3]["metadata"], "summary": f"{MARKER} summary third"}
        data = {"stimulus": "interaction", "evidence_ids": [first], "evaluated_sources": quirks,
                "frozen_memory_context": {"graph_candidates": [{"id": "graph_x", "evidence": quirks[:3]}]},
                "reuse": {"sources": [refs[2]]}}
        conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,?,?)",
                     ("appraise_legacy", mind.scope.key(), "complete", 0, dumps(data)))
        row = conn.execute("SELECT rowid,data FROM mind_graph_nodes WHERE id=?", (b["id"],)).fetchone()
        stored = json.loads(row[1])
        stored["evidence"][0].pop("received_at")
        conn.execute("UPDATE mind_graph_nodes SET data=? WHERE rowid=?", (dumps(stored), row[0]))
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (gone,))
    return {"first": first, "second": second, "third": third, "gone": gone}


def test_the_migration_reads_applies_in_batches_once_and_undoes_every_row_byte_for_byte(system, monkeypatch):
    mind, memory, _, _ = system
    legacy(mind, memory)
    before = every_table(mind)
    assert full_left(mind) and not slim_left(mind)
    monkeypatch.setattr(evidence_refs, "BATCH_ROWS", 1)

    dry = evidence_refs.run(mind)
    assert dry["state"] == "dry-run" and every_table(mind) == before
    tables = dry["tables"]
    assert tables["mind_appraisals"]["would_slim"] >= 8 and tables["mind_graph_nodes"]["would_slim"] >= 2
    assert tables["mind_graph_revisions"]["would_slim"] and tables["mind_graph_commands"]["would_slim"]
    assert tables["mind_conversation_habits"]["would_slim"] and tables["mind_habit_commands"]["would_slim"]
    assert tables["mind_graph_nodes"]["batches"] > 1, "a few rows per transaction"
    assert all(t["chars_after"] < t["chars"] for t in tables.values() if t["would_slim"])
    # The legacy node (no received_at), the blanked copy, the second copy and the gone source's.
    assert tables["mind_graph_nodes"]["originals_kept"] == 1 and tables["mind_appraisals"]["originals_kept"] >= 4
    assert not any(dry["table_marks"].values())

    applied = evidence_refs.run(mind, apply=True)
    assert applied["tables_total"]["slimmed"] == dry["tables_total"]["would_slim"]
    assert all(applied["table_marks"].values()) and not full_left(mind)
    stored = json.loads(rows(mind, "mind_appraisals")[max(rows(mind, "mind_appraisals"))])
    assert all(ref.get("erased") for ref in stored["evaluated_sources"][5:]), "a tombstone is left as erasure wrote it"
    after = every_table(mind)
    assert evidence_refs.run(mind, apply=True)["tables_total"]["slimmed"] == 0 and every_table(mind) == after

    assert evidence_refs.run(mind, undo=True)["tables_total"]["would_expand"] == applied["tables_total"]["slimmed"]
    undone = evidence_refs.run(mind, apply=True, undo=True)
    assert undone["tables_total"]["expanded"] == applied["tables_total"]["slimmed"]
    assert undone["tables_total"]["unexpandable"] == 0
    assert every_table(mind) == before, "every table row byte for byte"
    assert not any(undone["table_marks"].values())
    with mind.engine.db.connect() as conn:
        assert not conn.execute(f"SELECT COUNT(*) FROM {evidence_refs.TABLE}").fetchone()[0]
    assert evidence_refs.run(mind, apply=True, undo=True)["tables_total"]["expanded"] == 0 and every_table(mind) == before


def test_an_interrupted_apply_is_finished_by_the_next_and_the_mark_comes_with_the_first_batch(system, monkeypatch):
    mind, memory, _, _ = system
    legacy(mind, memory)
    before = every_table(mind)
    monkeypatch.setattr(evidence_refs, "BATCH_ROWS", 1)
    real, calls = evidence_refs._row, []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("interrupted")
        return real(*args, **kwargs)
    monkeypatch.setattr(evidence_refs, "_row", failing)
    with pytest.raises(RuntimeError):
        evidence_refs.run(mind, apply=True)
    with mind.engine.db.connect() as conn:
        # The first batch committed, and with it the mark: every later write of the graph is slim.
        assert evidence_refs.table_marked(conn, mind.scope.key(), "mind_graph_nodes")
        assert not evidence_refs.table_marked(conn, mind.scope.key(), "mind_appraisals")
    assert full_left(mind) and slim_left(mind)
    monkeypatch.setattr(evidence_refs, "_row", real)
    evidence_refs.run(mind, apply=True)
    assert not full_left(mind)
    evidence_refs.run(mind, apply=True, undo=True)
    assert every_table(mind) == before


def test_an_undo_runs_again_over_what_a_writer_copied_slim_into_a_row_it_had_done(system, monkeypatch):
    mind, memory, _, _ = system
    ids = legacy(mind, memory)
    evidence_refs.run(mind, apply=True)
    real, done = evidence_refs._walk, []

    def walk(mind_, group, table, **kwargs):
        found = real(mind_, group, table, **kwargs)
        if table.name == "mind_graph_edges" and not done:
            done.append(1)
            # A writer, unmarked by now, copies a reference still slim into a node already expanded.
            with mind.engine.db.connect(write=True) as conn:
                rowid, data = conn.execute("SELECT rowid,data FROM mind_graph_nodes ORDER BY rowid LIMIT 1").fetchone()
                value = json.loads(data)
                value["evidence"].append(evidence_refs.trace(mind._evidence(conn, [ids["third"]])[0]))
                conn.execute("UPDATE mind_graph_nodes SET data=? WHERE rowid=?", (dumps(value), rowid))
        return found
    monkeypatch.setattr(evidence_refs, "_walk", walk)
    undone = evidence_refs.run(mind, apply=True, undo=True)
    assert not slim_left(mind)
    assert undone["tables"]["mind_graph_nodes"]["passes"] >= 2


def test_an_undo_after_new_writes_rebuilds_them_as_a_writer_would_have_written_them(system):
    mind, memory, _, _ = system
    evidence_refs.run(mind, apply=True)
    sid = said(mind, "later")
    item = node(memory, "later", [sid])
    assert not carrying(item)
    evidence_refs.run(mind, apply=True, undo=True)
    with mind.engine.db.connect() as conn:
        written = mind._evidence(conn, [sid])
    assert memory.graph.detail(item["id"])["evidence"] == written
    assert not slim_left(mind)
    # Unmarked again: the next node is written full, as before.
    assert node(memory, "unmarked-again", [said(mind, "unmarked-again")])["evidence"][0].get("metadata")


def test_an_erase_takes_what_the_undo_kept_of_what_it_erases(system):
    mind, memory, _, _ = system
    ids = legacy(mind, memory)
    evidence_refs.run(mind, apply=True)
    words = f"{MARKER} summary third"
    assert {name for name, _ in texts_everywhere(mind.engine, words)} == {evidence_refs.TABLE}, \
        "only what the undo keeps holds the copy's own words"
    mind.engine.delete(ids["third"])
    assert not {name for name, _ in texts_everywhere(mind.engine, words)} - {"mind_events"}
    with mind.engine.db.connect() as conn:
        left = [row[0] for row in conn.execute(f"SELECT data FROM {evidence_refs.TABLE} WHERE path=''")]
    assert left and not any(ids["third"] in data for data in left), "what was kept of the rest stays"
    # The rest is still put back. What names the erased source and is not a tombstone -- a stored
    # proposal's `sources`, which erasure leaves to the row's own rule -- has nothing to be rebuilt
    # from: it stays slim, and the undo says so.
    undone = evidence_refs.run(mind, apply=True, undo=True)
    left = [ref for name, rowid in slim_left(mind) for _, ref in refs_in(json.loads(rows(mind, name)[rowid]))
            if evidence_refs.is_slim(ref)]
    assert {ref["source_id"] for ref in left} <= {ids["third"]}
    assert undone["tables_total"]["unexpandable"] == len(left)


def test_the_operator_command_reports_every_table(system, monkeypatch, capsys, tmp_path):
    import sys

    from kin_mind import host
    mind, memory, _, _ = system
    legacy(mind, memory)
    before = every_table(mind)
    credentials = tmp_path / "credentials"
    credentials.write_text("# no key: nothing here calls a model\n")
    config = tmp_path / "mind-config.json"
    config.write_text(json.dumps({"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": AGENT,
                                  "session_id": "synthetic-session", "credentials_file": str(credentials)}))

    def run(*flags):
        monkeypatch.setattr(sys, "argv", ["kin_mind.host", "--config", str(config), "slim-evidence-refs", *flags])
        with open("/dev/null") as empty:
            monkeypatch.setattr(sys, "stdin", empty)
            host.main()
        return json.loads(capsys.readouterr().out)
    dry = run()
    assert dry["state"] == "dry-run" and dry["tables"]["mind_graph_nodes"]["would_slim"] and every_table(mind) == before
    assert run("--apply")["tables_total"]["slimmed"] and not full_left(mind)
    assert run("--undo", "--apply")["tables_total"]["expanded"] and every_table(mind) == before
