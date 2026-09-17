"""Applying the evidence classification to a store that predates it, and taking it back.

A dry run writes nothing and says, in identifiers and counts, exactly what an apply would do.
An apply is idempotent and resumable: a crash at any step boundary, or in the middle of the
supersede chains or of the mixed-node rewrite, leaves the store strict and a second apply
finishes it without doubling a revision, an archive row or a classification row. An undo puts
the reads back where they were. Synthetic data only: the namespaces here are made up, and the
private ones a host installs never appear in this repository.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eventmem.core import Engine
from eventmem.core.db import dumps
from eventmem.core.models import RecallRequest, Scope, SourceInput
from eventmem.core.read_policy import MIGRATION, RULES_VERSION, ReadPolicy
from eventmem.core.self_knowledge import ClaimInput, SelfKnowledge
from kin_mind.isolation_migration import (
    CONFIGURATION_KEY,
    MARK,
    UNDONE,
    IsolationMigration,
    _load_registry,
)
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

SCOPE = Scope(persona="synthetic-isolation")
# Made-up namespaces. A host installs its own through the registry file, never through code.
STORE = "synthetic-role-store"
LIVED = "We walked the whole length of the lantern market that evening."
OBSERVED = "The lantern workshop confirmed the frame repair was finished."
ROLE_TEXT = "Installed role text: answer lantern questions warmly and at length."
APPROVED_TEXT = "Approved persona wording about lantern evenings, signed off by the owner."
REQUEST_TEXT = "Please keep speaking warmly about lantern evenings."
EXAMPLE_TEXT = "Example dialogue: we watched the lantern boats last winter."
FINDING_TEXT = "The lantern market visits and the installed wording both point the same way."
TITLES = ("Lantern market walk", "Workshop confirmation", "Installed wording", "Approved wording",
          "Owner request", "Synthetic example", "Mixed lantern finding", "Configuration lantern finding")
SETTINGS = {"records": True, "semantic": True, "context": True, "graph": True, "graph_recall": True,
            "event_lifecycle": True}


def root(engine, source):
    return next(rid for rid in engine.source(source["id"])["record_ids"] if not engine.get(rid)["evidence_ids"])


def fingerprint(engine):
    """The whole logical content of the store, plus the bytes of its main file."""
    conn = sqlite3.connect(engine.db.path)
    try:
        dump = hashlib.sha256("".join(conn.iterdump()).encode()).hexdigest()
    finally:
        conn.close()
    return dump, hashlib.sha256(Path(engine.db.path).read_bytes()).hexdigest()


def set_valid_from(engine, rid, value):
    """Test scaffolding: give a claim an exact validity so ordering and ties are deterministic."""
    with engine.db.connect(write=True) as conn:
        record = engine._get(conn, rid)
        record["valid_from"] = value
        conn.execute("UPDATE records SET valid_from=?,data=? WHERE id=?", (value, dumps(record), rid))
        engine.db.bump(conn)


@pytest.fixture
def world(tmp_path):
    """A store as it was before the classification existed: lived evidence, installed wording,
    an owner request inside the same namespace, four unchained role claims and two graph nodes."""
    at = [datetime(2026, 9, 16, 5, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "memory")
    mind = Mind(engine, SCOPE, clock=lambda: at[0].isoformat(timespec="microseconds"))

    def receive(namespace, key, title, text, *, authority="explicit", **metadata):
        return root(engine, engine.receive(SourceInput(namespace=namespace, key=key, text=text, title=title,
                                                       scope=SCOPE, authority=authority,
                                                       occurred_at=at[0].isoformat(), metadata=metadata)))

    ids = {
        "lived": receive("chat", "walk", TITLES[0], LIVED, role="user", host_event="message"),
        "observed": receive("chat", "workshop", TITLES[1], OBSERVED, authority="operation", role="assistant"),
        "installed": receive(STORE, "installed", TITLES[2], ROLE_TEXT, authority="operation", role="host"),
        "approved": receive(STORE, "approved", TITLES[3], APPROVED_TEXT, role="host"),
        "request": receive(STORE, "request", TITLES[4], REQUEST_TEXT, role="user", host_event="message"),
        "example": receive("chat", "example", TITLES[5], EXAMPLE_TEXT, role="user", examples_are_synthetic=True),
    }
    mind.initialize(agent_version="synthetic-v1", evidence_ids=[ids["lived"]])
    memory = MemoryContinuity(mind)
    memory.configure(SETTINGS)
    claims = SelfKnowledge(engine, SCOPE)
    for index, (name, aspect, basis) in enumerate([
            ("voice-1", "voice", "role"), ("voice-2", "voice", "role"), ("voice-3", "voice", "role"),
            ("pace-1", "pace", "role"), ("pace-2", "pace", "role"), ("guess", "voice", "hypothesis")]):
        ids[name] = claims.claim(ClaimInput(command_id=name, aspect=aspect, context="chat", basis=basis,
                                            agent_version="synthetic-v1", claim=f"Claim number {index}.",
                                            evidence_ids=[ids["lived"]]))["id"]
    day = datetime(2026, 9, 10, 5, tzinfo=timezone.utc)
    for name, offset in (("voice-1", 0), ("voice-2", 1), ("voice-3", 2), ("pace-1", 3), ("pace-2", 3)):
        set_valid_from(engine, ids[name], (day + timedelta(days=offset)).isoformat(timespec="microseconds"))
    graph = memory.graph
    with engine.db.connect(write=True) as conn:
        lived, configured = graph.proof(conn, [ids["observed"]]), graph.proof(conn, [ids["approved"]])
        ids["mixed"] = graph._put(conn, {"id": graph.identifier("finding", ["mixed"]), "kind": "finding",
            "title": TITLES[6], "text": FINDING_TEXT, "basis": "explicit", "evidence": [*lived, *configured],
            "source_ids": sorted({r["source_id"] for r in [*lived, *configured]}), "occurred_at": mind.clock()})["id"]
        second = graph.proof(conn, [ids["lived"]])
        ids["mixed_second"] = graph._put(conn, {"id": graph.identifier("finding", ["second"]), "kind": "finding",
            "title": TITLES[6], "text": FINDING_TEXT, "basis": "inferred", "evidence": [*second, *configured],
            "source_ids": sorted({r["source_id"] for r in [*second, *configured]}), "occurred_at": mind.clock()})["id"]
        ids["configuration_node"] = graph._put(conn, {"id": graph.identifier("finding", ["configuration"]),
            "kind": "finding", "title": TITLES[7], "text": ROLE_TEXT, "basis": "documented",
            "evidence": configured, "source_ids": [r["source_id"] for r in configured],
            "occurred_at": mind.clock()})["id"]
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({STORE: "role_configuration"}))
    return {"engine": engine, "mind": mind, "memory": memory, "graph": graph, "ids": ids,
            "registry": str(registry), "clock": at}


def migration(world, **options):
    return IsolationMigration(world["mind"], **options)


def run(world, **options):
    return migration(world).run(registry_file=world["registry"], **options)


def state(world):
    return (migration(world).state() or {}).get("state")


def strict(world):
    return ReadPolicy.load(world["engine"], SCOPE).strict


# One query per specimen, so the comparison is about the policy and not about lexical rank.
QUERIES = ("lantern market walk", "workshop frame repair", "installed role text",
           "approved persona wording", "keep speaking warmly", "example dialogue lantern boats")


def recalled(engine):
    """What ordinary experience recalls return over the whole synthetic corpus."""
    found = set()
    for query in QUERIES:
        found |= set(engine.recall(RecallRequest(scope=SCOPE, query=query, limit=100, budget=32000,
                                                 history=True))["covered_ids"])
    return found


def counts(engine):
    with engine.db.connect() as conn:
        return {
            "records": conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "revisions": conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0],
            "classes": conn.execute("SELECT COUNT(*) FROM source_evidence_class").fetchone()[0],
            "archive": conn.execute("SELECT COUNT(*) FROM mind_isolation_archive").fetchone()[0],
            "node_revisions": conn.execute("SELECT COUNT(*) FROM mind_graph_revisions").fetchone()[0],
            "relations": conn.execute("SELECT COUNT(*) FROM relations WHERE predicate='superseded_by'").fetchone()[0],
        }


def sections(impact):
    """The parts of an impact list that describe the end state rather than the work left."""
    return {key: impact[key] for key in ("classification", "chains", "graph", "summaries_to_rebuild")}


def test_dry_run_writes_nothing_and_names_no_content(world):
    engine = world["engine"]
    IsolationMigration(world["mind"])  # constructing the host objects is what any action does
    before = fingerprint(engine)
    impact = run(world)
    assert fingerprint(engine) == before
    assert state(world) is None and not strict(world)
    assert impact["applied"] is False and impact["state"] == "not-started"
    assert impact["rules_version"] == RULES_VERSION and impact["migration"] == MIGRATION
    text = json.dumps(impact, ensure_ascii=False)
    for private in (LIVED, OBSERVED, ROLE_TEXT, APPROVED_TEXT, REQUEST_TEXT, EXAMPLE_TEXT, FINDING_TEXT, *TITLES):
        assert private not in text


def test_dry_run_counts_what_the_rules_reach(world):
    impact = run(world)["classification"]
    ids = world["ids"]
    assert impact["namespaces"][STORE]["sources"] == 3
    assert impact["by_class"]["role_configuration"]["sources"] == 2
    assert impact["by_rule"]["owner-configuration-request"]["sources"] == 1
    # PM ruling: the owner's own turn inside a registered namespace stays experience.
    assert impact["owner_configuration_requests"]["record_ids"] == [ids["request"]]
    assert ids["request"] not in impact["records_hidden_from_experience_reads"]["record_ids"]
    for name in ("installed", "approved", "example"):
        assert ids[name] in impact["records_hidden_from_experience_reads"]["record_ids"]


def test_dry_run_describes_the_chains_and_the_tie(world):
    chains = {chain["current"]: chain for chain in run(world)["chains"]}
    ids = world["ids"]
    voice = chains[ids["voice-3"]]
    assert voice["member_ids"] == [ids["voice-1"], ids["voice-2"], ids["voice-3"]]
    assert [(link["old_id"], link["new_id"]) for link in voice["links"]] == [
        (ids["voice-1"], ids["voice-2"]), (ids["voice-2"], ids["voice-3"])]
    assert voice["current_ids"] == [ids["voice-3"]] and voice["skipped_ties"] == []
    pace = chains[ids["pace-2"]]
    assert pace["links"] == [] and pace["current_ids"] == [ids["pace-1"], ids["pace-2"]]
    assert [tie["reason"] for tie in pace["skipped_ties"]] == ["tie-would-invert-validity"]
    # A hypothesis claim shares the aspect and context of the voice chain and is not in it.
    assert ids["guess"] not in {i for chain in chains.values() for i in chain["member_ids"]}


def test_dry_run_separates_mixed_nodes_from_configuration_only_ones(world):
    graph = run(world)["graph"]
    ids = world["ids"]
    assert {node["id"]: node["references_moved"] for node in graph["mixed_nodes"]} == {
        ids["mixed"]: 1, ids["mixed_second"]: 1}
    assert graph["configuration_only_nodes"]["node_ids"] == [ids["configuration_node"]]
    assert graph["mixed_edges_left_unchanged"] == 0


def test_apply_end_to_end_then_an_identical_second_apply(world):
    engine = world["engine"]
    first = run(world, apply=True)
    assert first["state"] == "complete" and not strict(world)
    after = counts(engine)
    assert after["relations"] == 2
    second = run(world, apply=True)
    assert second["state"] == "complete"
    assert counts(engine) == after
    assert sections(second) == sections(first)
    assert second["summary"]["classification_rows_planned"] == 0 and second["summary"]["supersessions_planned"] == 0
    assert second["summary"]["mixed_nodes_planned"] == 0
    assert second["steps"]["chains"]["linked"] == 0 and second["steps"]["invalidated"]["nodes"] == 0


def test_apply_supersedes_the_chain_and_leaves_the_tie_and_the_hypothesis(world):
    engine, ids = world["engine"], world["ids"]
    run(world, apply=True)
    assert [engine.get(ids[name])["status"] for name in ("voice-1", "voice-2", "voice-3")] == [
        "superseded", "superseded", "active"]
    assert engine.get(ids["voice-1"])["attributes"]["superseded_by"] == ids["voice-2"]
    assert engine.get(ids["voice-1"])["valid_until"] == engine.get(ids["voice-2"])["valid_from"]
    for name in ("pace-1", "pace-2", "guess"):
        assert engine.get(ids[name])["revision"] == 1
    assert engine.get(ids["guess"])["status"] == "unverified"
    view = SelfKnowledge(engine, SCOPE).view(agent_version="synthetic-v1")
    assert {item["id"] for item in view["items"]} & {ids["voice-1"], ids["voice-2"]} == set()
    assert ids["voice-3"] in {item["id"] for item in view["items"]}
    history = SelfKnowledge(engine, SCOPE).view(history=True)
    assert {ids["voice-1"], ids["voice-2"]} <= {item["id"] for item in history["items"]}


def test_a_superseded_claim_stays_visible_and_labelled_under_audit(world):
    engine, ids = world["engine"], world["ids"]
    run(world, apply=True)
    audit = ReadPolicy.load(engine, SCOPE, "audit")
    for name in ("voice-1", "voice-2"):
        record = engine.get(ids[name])
        assert audit.visible(record) and audit.label(record) == "self_knowledge"
        assert audit.basis(record) == "self_knowledge"
    assert not ReadPolicy.load(engine, SCOPE).visible(engine.get(ids["voice-1"]))

    def read(purpose, history=False):
        return set(engine.recall(RecallRequest(scope=SCOPE, query="claim number", limit=100, budget=32000,
                                               history=history, recall_purpose=purpose))["covered_ids"])

    current = read("self_knowledge_view")
    assert ids["voice-3"] in current and {ids["voice-1"], ids["voice-2"]}.isdisjoint(current)
    assert {ids["voice-1"], ids["voice-2"], ids["voice-3"]} <= read("audit", history=True)
    assert read("experience_recall", history=True).isdisjoint({ids[n] for n in ("voice-1", "voice-3")})


def test_the_graph_projection_of_a_superseded_claim_goes_stale(world):
    engine, ids, graph = world["engine"], world["ids"], world["graph"]
    with engine.db.connect(write=True) as conn:
        node = graph.ensure(conn, ids["voice-1"])
    assert node["reference_revision"] == 1
    run(world, apply=True)
    with engine.db.connect() as conn:
        assert not graph.fresh(conn, graph.get(conn, node["id"]))


def test_a_mixed_node_keeps_its_experience_and_loses_its_configuration(world):
    engine, ids, graph = world["engine"], world["ids"], world["graph"]
    run(world, apply=True)
    with engine.db.connect() as conn:
        node = graph.get(conn, ids["mixed"])
        untouched = graph.get(conn, ids["configuration_node"])
        history = [json.loads(r[0]) for r in conn.execute(
            "SELECT data FROM mind_graph_revisions WHERE id=? ORDER BY revision", (ids["mixed"],))]
        archived = conn.execute("SELECT revision,data FROM mind_isolation_archive WHERE kind='graph_node' AND identifier=?",
                                (ids["mixed"],)).fetchone()
    assert [ref["record_id"] for ref in node["evidence"]] == [ids["observed"]]
    assert [ref["record_id"] for ref in node[CONFIGURATION_KEY]] == [ids["approved"]]
    assert node["source_ids"] == [engine.get(ids["observed"])["source_ids"][0]]
    # Its only explicit reference was the configuration one, so the basis follows it out.
    assert node["basis"] == "inferred" and node[MARK] == RULES_VERSION and node["text"] == ""
    assert [value["revision"] for value in history] == [1, 2]
    assert history[0]["text"] == FINDING_TEXT and CONFIGURATION_KEY not in history[0]
    assert (archived["revision"], json.loads(archived["data"])["text"]) == (1, FINDING_TEXT)
    assert untouched["revision"] == 1 and CONFIGURATION_KEY not in untouched
    policy = ReadPolicy.load(engine, SCOPE)
    with engine.db.connect() as conn:
        visible = {n["id"] for n in graph.visible(conn, [node, untouched], policy)}
    assert visible == {node["id"]}


def test_a_mixed_node_still_reaches_its_experience_evidence_in_a_recall(world):
    engine, ids, graph = world["engine"], world["ids"], world["graph"]
    run(world, apply=True)
    with engine.db.connect() as conn:
        found = {n["id"] for n in graph.candidates(conn, "lantern")}
    assert ids["mixed"] in found and ids["configuration_node"] not in found
    assert ids["observed"] in recalled(engine) and ids["approved"] not in recalled(engine)


def test_strict_mode_holds_from_the_first_write_until_the_last(world, monkeypatch):
    seen = []
    original = IsolationMigration._step_chains

    def watch(self, plan):
        seen.append((state(world), strict(world)))
        return original(self, plan)

    monkeypatch.setattr(IsolationMigration, "_step_chains", watch)
    run(world, apply=True)
    assert seen == [("classified", True)]
    assert state(world) == "complete" and not strict(world)


@pytest.mark.parametrize("step", ["_step_classified", "_step_chains", "_step_invalidated", "_advance"])
def test_a_crash_at_a_step_boundary_leaves_a_strict_store_a_second_apply_finishes(world, monkeypatch, step):
    engine, ids = world["engine"], world["ids"]
    expected = {"_step_classified": "pending", "_step_chains": "classified",
                "_step_invalidated": "chains", "_advance": "invalidated"}[step]
    original = getattr(IsolationMigration, step)

    def crash(self, *args, **options):
        if step != "_advance" or args[1] == "complete":
            raise RuntimeError("synthetic crash")
        return original(self, *args, **options)

    monkeypatch.setattr(IsolationMigration, step, crash)
    with pytest.raises(RuntimeError):
        run(world, apply=True)
    # The progress row is written before the first change, so nothing is ever half-applied
    # without the reading side knowing it.
    assert state(world) == expected and strict(world)
    monkeypatch.undo()
    assert run(world, apply=True)["state"] == "complete"
    assert not strict(world)
    assert engine.get(ids["voice-1"])["revision"] == 2
    with engine.db.connect() as conn:
        assert conn.execute("SELECT revision FROM mind_graph_nodes WHERE id=?", (ids["mixed"],)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM mind_isolation_archive WHERE kind='self_claim'").fetchone()[0] == 2


def test_a_crash_inside_the_chains_step_does_not_double_a_revision(world, monkeypatch):
    engine, ids = world["engine"], world["ids"]
    original, calls = SelfKnowledge.link_supersession, []

    def once(self, old_id, new_id, expected_revisions, command_id):
        calls.append(old_id)
        if len(calls) > 1:
            raise RuntimeError("synthetic crash")
        return original(self, old_id, new_id, expected_revisions, command_id)

    monkeypatch.setattr(SelfKnowledge, "link_supersession", once)
    with pytest.raises(RuntimeError):
        run(world, apply=True)
    assert state(world) == "classified" and strict(world)
    assert engine.get(ids["voice-1"])["status"] == "superseded"
    assert engine.get(ids["voice-2"])["status"] == "active"
    monkeypatch.undo()
    assert run(world, apply=True)["state"] == "complete"
    assert engine.get(ids["voice-1"])["revision"] == 2 and engine.get(ids["voice-2"])["revision"] == 2
    assert counts(engine)["relations"] == 2


def test_a_crash_inside_the_mixed_node_step_does_not_double_a_revision(world, monkeypatch):
    engine, ids = world["engine"], world["ids"]
    original, calls = IsolationMigration._rewrite_node, []

    def once(self, conn, policy, node_id, expected_revision):
        calls.append(node_id)
        if len(calls) > 1:
            raise RuntimeError("synthetic crash")
        return original(self, conn, policy, node_id, expected_revision)

    monkeypatch.setattr(IsolationMigration, "_rewrite_node", once)
    with pytest.raises(RuntimeError):
        run(world, apply=True)
    assert state(world) == "chains" and strict(world)
    monkeypatch.undo()
    assert run(world, apply=True)["state"] == "complete"
    with engine.db.connect() as conn:
        revisions = {i: conn.execute("SELECT revision FROM mind_graph_nodes WHERE id=?", (ids[i],)).fetchone()[0]
                     for i in ("mixed", "mixed_second", "configuration_node")}
        archived = conn.execute("SELECT COUNT(*) FROM mind_isolation_archive WHERE kind='graph_node'").fetchone()[0]
    assert revisions == {"mixed": 2, "mixed_second": 2, "configuration_node": 1} and archived == 2


def test_undo_puts_the_reads_back_where_they_were(world):
    engine, ids = world["engine"], world["ids"]
    before, before_counts = recalled(engine), counts(engine)
    # The synthetic example was already hidden by its own flag; the two configuration records
    # are what this migration takes out of an experience read.
    assert {ids["installed"], ids["approved"]} <= before and ids["example"] not in before
    run(world, apply=True)
    during = recalled(engine)
    assert during & {ids["installed"], ids["approved"]} == set()
    assert ids["request"] in during and ids["lived"] in during
    result = migration(world).undo(apply=True)
    assert result["state"] == UNDONE and not strict(world)
    assert recalled(engine) == before
    assert result["summary"]["skipped"] == 0 and result["summary"]["claims"] == 2
    with engine.db.connect() as conn:
        node = world["graph"].get(conn, ids["mixed"])
        assert conn.execute("SELECT COUNT(*) FROM source_evidence_class").fetchone()[0] == before_counts["classes"]
    assert node["text"] == FINDING_TEXT and CONFIGURATION_KEY not in node
    assert node["basis"] == "explicit" and node["revision"] == 3
    assert engine.get(ids["voice-1"])["status"] == "active"
    assert "superseded_by" not in engine.get(ids["voice-1"])["attributes"]
    assert counts(engine)["relations"] == 0


def test_undo_skips_and_lists_what_changed_since_the_migration(world):
    engine, ids, graph = world["engine"], world["ids"], world["graph"]
    run(world, apply=True)
    with engine.db.connect(write=True) as conn:
        graph._put(conn, {**graph.get(conn, ids["mixed"]), "title": "Renamed after the migration"})
    plan = migration(world).undo()
    assert [entry["id"] for entry in plan["undo"]["skipped"]] == [ids["mixed"]]
    assert plan["undo"]["skipped"][0]["reason"] == "changed-since-migration"
    assert ids["mixed_second"] in plan["undo"]["node_ids"]
    applied = migration(world).undo(apply=True)
    assert applied["summary"]["skipped"] == 1 and applied["state"] == UNDONE
    with engine.db.connect() as conn:
        assert graph.get(conn, ids["mixed"])["title"] == "Renamed after the migration"


def test_undo_is_a_dry_run_by_default_and_says_when_there_is_nothing_to_take_back(world):
    engine = world["engine"]
    untouched = migration(world).undo(apply=True)
    assert untouched["applied"] is False and untouched["state"] == "not-started" and state(world) is None
    run(world, apply=True)
    before = fingerprint(engine)
    plan = migration(world).undo()
    assert plan["applied"] is False and fingerprint(engine) == before
    assert state(world) == "complete"
    migration(world).undo(apply=True)
    # A second undo has nothing left to reverse and says so instead of refusing.
    again = migration(world).undo()
    assert {entry["reason"] for entry in again["undo"]["skipped"]} == {"already-restored"}
    assert again["undo"]["claim_ids"] == [] and again["undo"]["node_ids"] == []


def test_a_migration_taken_back_can_be_applied_again(world):
    """An undo writes a new revision of every claim it restores, so the plan a second apply
    makes is a different plan. Its commands have to say so, or the idempotency layer sees one
    command id arriving with another payload and the chains can never be linked again."""
    engine, ids = world["engine"], world["ids"]
    run(world, apply=True)
    migration(world).undo(apply=True)
    again = run(world, apply=True)
    assert again["state"] == "complete" and not strict(world)
    assert again["steps"]["chains"] == {"linked": 2, "refused": []}
    assert again["steps"]["invalidated"]["nodes"] == 2 and again["steps"]["invalidated"]["refused"] == []
    assert [engine.get(ids[name])["status"] for name in ("voice-1", "voice-2", "voice-3")] == [
        "superseded", "superseded", "active"]
    with engine.db.connect() as conn:
        node = world["graph"].get(conn, ids["mixed"])
    assert node[MARK] == RULES_VERSION and CONFIGURATION_KEY in node
    assert recalled(engine).isdisjoint({ids["installed"], ids["approved"]})


def test_a_second_undo_restores_everything_and_skips_nothing(world):
    engine, ids = world["engine"], world["ids"]
    before = recalled(engine)
    for _ in range(2):
        run(world, apply=True)
        result = migration(world).undo(apply=True)
        # Every round leaves its own archive rows; only the newest one decides a restore, and
        # the ones it superseded are never reported as work that could not be done.
        assert result["summary"]["skipped"] == 0, result["undo"]["skipped"]
        assert result["summary"]["claims"] == 2 and result["summary"]["nodes"] == 2
        assert result["state"] == UNDONE and not strict(world)
        assert recalled(engine) == before
    with engine.db.connect() as conn:
        kept = conn.execute("SELECT kind,COUNT(*) FROM mind_isolation_archive GROUP BY kind ORDER BY kind").fetchall()
        node = world["graph"].get(conn, ids["mixed"])
    # Nothing is deleted: both rounds are in the archive, the claims twice and the nodes twice.
    assert dict(map(tuple, kept)) == {"evidence_class": 3, "graph_node": 4, "registry": 1, "self_claim": 4}
    assert node["text"] == FINDING_TEXT and CONFIGURATION_KEY not in node and MARK not in node
    assert engine.get(ids["voice-1"])["status"] == "active"


def test_the_impact_list_reads_the_same_through_apply_undo_and_a_second_round(world):
    seen = [sections(run(world))]
    run(world, apply=True)
    seen.append(sections(run(world)))
    migration(world).undo(apply=True)
    seen.append(sections(run(world)))
    run(world, apply=True)
    seen.append(sections(run(world)))
    migration(world).undo(apply=True)
    seen.append(sections(run(world)))
    assert all(found == seen[0] for found in seen[1:])


def test_a_claim_moved_by_another_writer_is_refused_with_its_code(world):
    """A real concurrent change still stops the link, and the report says which code stopped
    it, so an operator can tell a moved claim from a command id that was already used."""
    engine, ids = world["engine"], world["ids"]
    from eventmem.core.models import RevisionInput

    original = IsolationMigration._step_chains

    def meddle(self, plan):
        engine.revise(ids["voice-1"], RevisionInput(command_id="outside-writer", expected_revision=1,
                                                    action="correct", content="Amended by someone else.",
                                                    reason="Synthetic concurrent correction"))
        return original(self, plan)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(IsolationMigration, "_step_chains", meddle)
        result = run(world, apply=True)
    refused = result["steps"]["chains"]["refused"]
    assert [entry["code"] for entry in refused] == ["supersession-revision-changed"]
    assert refused[0]["kind"] == "runtime" and refused[0]["old_id"] == ids["voice-1"]
    assert result["state"] == "invalidated" and strict(world)
    # The next apply plans against what is stored now and finishes.
    finished = run(world, apply=True)
    assert finished["state"] == "complete" and finished["steps"]["chains"]["refused"] == []
    assert not strict(world)
    assert [engine.get(ids[name])["status"] for name in ("voice-1", "voice-2")] == ["superseded", "superseded"]


def test_link_supersession_mints_no_record_and_swaps_on_both_revisions(world):
    engine, ids = world["engine"], world["ids"]
    claims = SelfKnowledge(engine, SCOPE)
    before = counts(engine)
    with pytest.raises(Exception, match="Self-claim revisions changed before the supersession"):
        claims.link_supersession(ids["voice-1"], ids["voice-2"], {ids["voice-1"]: 7, ids["voice-2"]: 1}, "a")
    assert counts(engine) == before
    linked = claims.link_supersession(ids["voice-1"], ids["voice-2"], {ids["voice-1"]: 1, ids["voice-2"]: 1}, "b")
    assert linked == {"state": "linked", "old_id": ids["voice-1"], "new_id": ids["voice-2"], "revision": 2}
    after = counts(engine)
    assert after["records"] == before["records"] and after["revisions"] == before["revisions"] + 1
    # The same command is one supersession, however often it arrives.
    again = claims.link_supersession(ids["voice-1"], ids["voice-2"], {ids["voice-1"]: 1, ids["voice-2"]: 1}, "b")
    assert again == linked and counts(engine) == after


def test_link_supersession_refuses_a_tie_a_hypothesis_and_another_chain(world):
    engine, ids = world["engine"], world["ids"]
    claims = SelfKnowledge(engine, SCOPE)
    with pytest.raises(Exception, match="A superseding claim must be valid after the one it replaces"):
        claims.link_supersession(ids["pace-1"], ids["pace-2"], {ids["pace-1"]: 1, ids["pace-2"]: 1}, "tie")
    with pytest.raises(Exception, match="Only role claims are chained by a supersession"):
        claims.link_supersession(ids["voice-1"], ids["guess"], {ids["voice-1"]: 1, ids["guess"]: 1}, "guess")
    with pytest.raises(Exception, match="A supersession keeps the same aspect, context and basis"):
        claims.link_supersession(ids["voice-1"], ids["pace-2"], {ids["voice-1"]: 1, ids["pace-2"]: 1}, "cross")
    with pytest.raises(Exception, match="A claim cannot supersede itself"):
        claims.link_supersession(ids["voice-1"], ids["voice-1"], {ids["voice-1"]: 1, "x": 1}, "self")
    claims.link_supersession(ids["voice-1"], ids["voice-2"], {ids["voice-1"]: 1, ids["voice-2"]: 1}, "first")
    with pytest.raises(Exception, match="Only a current claim can be superseded"):
        claims.link_supersession(ids["voice-1"], ids["voice-3"], {ids["voice-1"]: 2, ids["voice-3"]: 1}, "again")


def test_the_impact_list_reaches_the_host_action_and_a_file(world, tmp_path):
    from kin_mind.host import dispatch

    config = {"root": str(world["engine"].db.root), "scope": SCOPE.model_dump(),
              "agent_version": "synthetic-v1", "session_id": "synthetic-session"}
    output = tmp_path / "impact" / "evidence-isolation.json"
    result = dispatch(config, "migrate-evidence-isolation",
                      {"output": str(output), "registry": world["registry"]})
    assert set(result) == {"migration", "operation", "state", "applied", "rules_version", "summary", "output"}
    assert "steps" not in result and result["operation"] == "migrate"
    written = json.loads(output.read_text())
    assert written["summary"] == result["summary"] and written["applied"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert state(world) is None
    applied = dispatch(config, "migrate-evidence-isolation",
                       {"apply": True, "output": str(output), "registry": world["registry"]})
    assert applied["state"] == "complete" and applied["applied"] is True
    assert dispatch(config, "migrate-evidence-isolation", {"undo": True, "apply": True})["state"] == UNDONE


def test_a_registry_file_that_would_vouch_for_a_source_is_refused(world, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({STORE: "experience"}))
    with pytest.raises(ValueError, match="A namespace registry maps a namespace to a non-experience class"):
        migration(world).run(registry_file=str(bad))


def test_an_owner_configuration_request_stays_experience_and_says_why(world):
    """PM ruling: what the owner asked for is something that happened, however private the
    namespace it was stored in. It keeps its basis and carries a label instead."""
    engine, ids = world["engine"], world["ids"]
    run(world, apply=True)
    policy = ReadPolicy.load(engine, SCOPE)
    record = engine.get(ids["request"])
    assert policy.visible(record) and policy.label(record) == "configuration_request"
    assert policy.basis(record) == record["confirmation"] == "explicit"
    assert policy.prefix(record) == "[configuration_request] "
    assert ids["request"] in recalled(engine)
    with engine.db.connect() as conn:
        row = conn.execute("SELECT class,rule FROM source_evidence_class WHERE source_id=?",
                           (record["source_ids"][0],)).fetchone()
    assert tuple(row) == ("experience", "owner-configuration-request")


def test_no_legacy_record_is_rewritten(world):
    """Evidence freshness is pinned to the record revision: only the claims this migration
    chains may move, and every other stored record stays byte for byte as it was."""
    engine, ids = world["engine"], world["ids"]

    def snapshot():
        with engine.db.connect() as conn:
            return {row["id"]: (row["revision"], row["data"]) for row in conn.execute("SELECT id,revision,data FROM records")}

    before = snapshot()
    run(world, apply=True)
    after = snapshot()
    assert set(after) == set(before)
    assert {rid for rid in before if after[rid] != before[rid]} == {ids["voice-1"], ids["voice-2"]}


def test_the_migration_adds_no_table_no_column_and_no_state_name(world):
    engine = world["engine"]

    def schema():
        with engine.db.connect() as conn:
            return {row[0]: row[1] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}

    before = schema()
    run(world, apply=True)
    assert schema() == before
    with engine.db.connect() as conn:
        statuses = {row[0] for row in conn.execute("SELECT DISTINCT status FROM records")}
        states = {row[0] for row in conn.execute("SELECT DISTINCT state FROM mind_graph_nodes")}
        migrations = {row[0] for row in conn.execute("SELECT name FROM mind_memory_migrations")}
    assert statuses <= {"active", "unverified", "superseded", "archived", "retracted", "refuted"}
    assert states <= {"active", "merged", "retracted"}
    assert migrations == {MIGRATION}


def test_the_classification_step_is_written_in_small_transactions(world):
    engine = world["engine"]
    one = migration(world, chunk=1).run(apply=True, registry_file=world["registry"])
    assert one["state"] == "complete"
    with engine.db.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM source_evidence_class").fetchone()[0]
    assert rows == one["summary"]["sources_classified"]
    assert migration(world).run(registry_file=world["registry"])["summary"]["classification_rows_planned"] == 0


def test_the_impact_list_counts_the_cached_text_that_stops_being_served(world):
    from kin_mind.context import Contexts

    engine, ids = world["engine"], world["ids"]
    contexts = Contexts(world["mind"])
    item = {"id": ids["approved"], "revision": 1, "text": APPROVED_TEXT, "basis": "explicit",
            "dependencies": [{"id": ids["approved"], "revision": 1}]}
    receipt = {"text": "", "tokens": 20, "covered_ids": [ids["approved"]],
               "index": [{"id": ids["approved"], "revision": 1, "depth": "original"}]}
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_context_cache VALUES(?,?,?,?)",
                     (contexts._overview_key(item), SCOPE.key(),
                      json.dumps({"text": "A warm wording.", "source": item, "coverage": "overview"}),
                      world["mind"].clock()))
        contexts._save_window(conn, "synthetic-session", {"epoch": "initial", "used": 20, "seen": {},
                                                          "receipts": {"event-1": receipt}})
    caches = run(world)["caches"]
    assert caches == {"context_cache_rows": 1, "overviews": 1, "packs": 0, "context_cache_rows_total": 1,
                      "window_receipts": 1, "window_sessions": 1, "window_rows_total": 1}
    assert run(world)["summary"]["cached_contexts_missing_once"] == 1
    # The cache invalidates itself once the classification is stored: it is never rewritten.
    run(world, apply=True)
    assert not contexts._overview(item, ReadPolicy.load(engine, SCOPE)).get("cached_summary")
    assert not contexts._receipt_current(receipt, ReadPolicy.load(engine, SCOPE))
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_context_cache").fetchone()[0] == 1


def test_a_cached_judgment_that_rested_on_configuration_is_purged(world):
    """The one thing that does not invalidate itself: a judgment matches on the environment
    digest, which covers the persona file and the scope's settings, not the classification."""
    from kin_mind import judgment_cache

    engine, ids = world["engine"], world["ids"]
    judgment = {"scope": SCOPE.key(), "type": "step-complete", "goal": "synthetic goal"}
    receipt = judgment_cache.put(engine, "review_step", "synthetic-request", judgment, {"decision": "yes"},
                                 now=1000.0, depends_on=[ids["approved"]], valid_for=86400)
    judgment_cache.accept(engine, receipt)
    with engine.db.connect() as conn:
        assert judgment_cache.get(engine, conn, "review_step", "synthetic-request", judgment, now=1001.0)
    # Storing the classification does not move the environment digest this cache matches on,
    # so the verdict is still servable after the first step. That is why the purge exists.
    alone = migration(world)
    with engine.db.connect() as conn:
        alone._step_classified(alone.plan(conn, _load_registry(world["registry"])))
    with engine.db.connect() as conn:
        assert judgment_cache.get(engine, conn, "review_step", "synthetic-request", judgment, now=1001.0)
    result = run(world, apply=True)
    assert result["steps"]["invalidated"]["judgments_dropped"] == 1
    with engine.db.connect() as conn:
        assert judgment_cache.get(engine, conn, "review_step", "synthetic-request", judgment, now=1001.0) is None


def test_the_impact_list_prices_the_summaries_a_model_will_rewrite(world):
    engine, ids, graph = world["engine"], world["ids"], world["graph"]
    with engine.db.connect(write=True) as conn:
        refs = graph.proof(conn, [ids["lived"]])
        event = graph._put(conn, {"id": graph.identifier("event", ["evening"]), "kind": "event",
            "title": "An evening", "text": "", "basis": "inferred", "evidence": refs,
            "source_ids": [r["source_id"] for r in refs], "occurred_at": world["mind"].clock(),
            "lifecycle": "open", "membership_authority": "graph"})
        for name in ("lived", "installed", "example"):
            graph.link(conn, graph.ensure(conn, ids[name])["id"], "part_of", event["id"], refs, reason="Synthetic")
        conn.execute("INSERT OR REPLACE INTO mind_event_digests(scope,event_id,state,generation,revision,"
                     "input_hash,dirty_at,due_at,data) VALUES(?,?,'ready',1,3,'legacy',?,0,'{}')",
                     (SCOPE.key(), event["id"], world["mind"].clock()))
    impact = run(world)["summaries_to_rebuild"]
    assert event["id"] in impact["excluded_by_policy"] and impact["events"] >= 1
    assert impact["existing_digest_rows"] == 1
