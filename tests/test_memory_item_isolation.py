"""Item-level isolation inside the memory section (stage 2 WP8).

One item the host blocks is dropped alone and everything that names its key goes with it; a version
that moved and an unclassified failure still fail the attempt; a source whose only carrier was dropped
is never written down as organised and gets one more memory-only pass.

Synthetic replays only: an injected clock and scripted providers. No model or network call.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing
from eventmem.core.models import RevisionInput, Scope, SourceInput
from kin_mind import memory_items
from kin_mind.appraisal import FOLLOW_UP, Appraisal, Appraisals
from kin_mind.attempts import read as read_ledger
from kin_mind.conflicts import classify
from kin_mind.graph import GraphAssessment, GraphEdge, GraphNode
from kin_mind.habits import HabitProposal
from kin_mind.lifecycle import EventRoute
from kin_mind.memory import Disclosure, MemoryAssessment, MemoryContinuity, MemoryLink, MemoryNote
from kin_mind.sharing import ContentReference, CoverageAssessment, CoverageMapping
from kin_mind.state import Mind

RECEIPT = {"provider": "deepseek", "model": "deepseek-flash", "reasoning": "high", "request_id": "fixture"}
OUTSIDE_NOTE = "Semantic evidence is outside the evaluated source set"
OUTSIDE_GRAPH = "Graph evidence was not part of this evaluation"
OUTSIDE_COVERAGE = "Coverage mapping is outside evaluated delivery evidence"


def drop(kind, key, code, message, cause=None):
    return {"section": "memory", "item": {"kind": kind, "key": key}, "code": code, "message": message,
            **({"cascaded_from": cause} if cause else {})}


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 14, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, Scope(persona="synthetic-items"), clock=lambda: clock[0].isoformat(timespec="microseconds"))

    def source(key, text=None, version="1"):
        return engine.receive(SourceInput(namespace="synthetic", key=key, version=version, text=text or key, scope=mind.scope,
            authority="explicit", occurred_at=mind.clock(), extract=False, metadata={"role": "user", "host_event": "message"}))["id"]

    mind.initialize(agent_version="fixture-v1", evidence_ids=[source("initial")])
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True, "sharing": True,
                      "associations": True, "event_lifecycle": True})
    return SimpleNamespace(mind=mind, memory=memory, source=source, clock=clock)


def rows(system, sql, *args):
    with system.mind.engine.db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def job(system, identifier):
    return json.loads(rows(system, "SELECT data FROM mind_appraisals WHERE id=?", identifier)[0]["data"])


def metrics(system, name):
    return [{"value": r["value"], **json.loads(r["data"])} for r in rows(system, "SELECT value,data FROM metrics WHERE name=? ORDER BY id", name)]


def note_keys(system, event_id):
    """Keys of the notes one commit really wrote."""
    return sorted(r["key"] for r in rows(system, "SELECT json_extract(data,'$.attributes.key') key FROM records "
                                                 "WHERE deleted=0 AND json_extract(data,'$.attributes.semantic_event')=?", event_id))


def indexed(system, source_id):
    return bool(rows(system, "SELECT 1 FROM mind_semantic_sources WHERE source_id=?", source_id))


def unorganized(system):
    return {r["source_id"]: (r["state"], r["attempts"]) for r in rows(system, "SELECT source_id,state,attempts FROM mind_memory_unorganized")}


def commit(system, assessment, evidence_ids, event_id, **options):
    with system.mind.engine.db.connect(write=True) as conn:
        refs = system.mind._evidence(conn, evidence_ids)
        return system.memory.apply_assessment(conn, assessment, refs, event_id, 0, 20, RECEIPT, **{"schedule": False, **options})


def note(key, evidence_ids, **extra):
    return MemoryNote(key=key, title="A synthetic title for " + key, content="A synthetic body for " + key, evidence_ids=evidence_ids, **extra)


def findings(system):
    sid = system.source("observations", "The synthetic exploration has independent observations.")
    with system.mind.engine.db.connect(write=True) as conn:
        refs = system.memory.graph.proof(conn, [sid])
        owner = system.memory.graph._put(conn, {"id": "explore-fixture", "kind": "exploration", "title": "Synthetic star colors", "text": "",
            "source_ids": [sid], "evidence": refs, "basis": "documented"})
        return system.memory.sharing.units(conn, owner["id"], ["Young synthetic stars appear blue in this fictional dataset.",
            "Older synthetic stars appear red in this fictional dataset."], [sid])


def send(system, unit, *, channel, bubble):
    return system.memory.ingest({"id": channel + "-" + bubble, "kind": "delivery", "at": system.mind.clock(), "channel": channel,
        "delivery_id": "batch", "bubble_id": bubble, "text": unit["text"], "state": "accepted", "message_id": channel + "-receipt",
        "references": [{"unit_id": unit["id"], "version": 1, "mode": "new"}], "historical": True})


def coverage(system, unit, share_id):
    return [json.loads(r["data"]) for r in rows(system, "SELECT data FROM mind_share_coverage WHERE unit_id=? AND share_id=?", unit["id"], share_id)]


def test_one_out_of_bounds_coverage_mapping_is_dropped_and_the_rest_commits(system):
    units = findings(system)
    seen, unseen = send(system, units[0], channel="a", bubble="one"), send(system, units[1], channel="b", bubble="two")
    sid = system.source("evaluated-chat")
    def mapping(share, bubble, unit):
        return CoverageMapping(share_id=share["share_id"], bubble_id=bubble, confidence=.95, reason="Rechecked the public message",
                               references=[ContentReference(unit_id=unit["id"], version=1)])
    assessment = MemoryAssessment(notes=[note("kept", [sid])], coverage=CoverageAssessment(
        mappings=[mapping(seen, "one", units[0]), mapping(unseen, "two", units[1])]))
    dropped = commit(system, assessment, [sid, seen["source_id"]], "mind_items_coverage")
    assert dropped == [drop("coverage-mapping", "coverage.mappings[1]", "evidence-out-of-bounds", OUTSIDE_COVERAGE)]
    # The evaluated share's mapping and the note committed; the other share keeps what its receipt had registered.
    assert [c["basis"] for c in coverage(system, units[0], seen["share_id"])] == ["semantic-mapping"]
    assert [c["basis"] for c in coverage(system, units[1], unseen["share_id"])] == ["registered-reference"]
    assert note_keys(system, "mind_items_coverage") == ["kept"]
    # No carrier was dropped, so every evaluated source is organised.
    assert indexed(system, sid) and indexed(system, seen["source_id"]) and unorganized(system) == {}
    assert metrics(system, "memory_items_dropped") == [{"value": 1, "event_id": "mind_items_coverage", "codes": {"evidence-out-of-bounds": 1},
        "kinds": {"coverage-mapping": 1}, "cascaded": 0, "withheld_sources": 0, "abandoned_sources": 0}]


def test_a_note_citing_unevaluated_evidence_is_dropped_alone(system):
    first, second, outside = system.source("first"), system.source("second"), system.source("never-shown")
    assessment = MemoryAssessment(
        notes=[note("kept", [first]), note("blocked", [second, outside]), note("also-kept", [second])],
        links=[MemoryLink(subject="kept", object="also-kept", relation="follows", evidence_ids=[first])])
    dropped = commit(system, assessment, [first, second], "mind_items_note")
    assert dropped == [drop("note", "notes[1]", "evidence-out-of-bounds", OUTSIDE_NOTE)]
    assert note_keys(system, "mind_items_note") == ["also-kept", "kept"]
    assert len(rows(system, "SELECT 1 FROM relations WHERE predicate='follows'")) == 1
    # Another note carried the second source, so nothing of it is lost.
    assert indexed(system, first) and indexed(system, second) and unorganized(system) == {}


def cascade_case(system):
    units = findings(system)
    sent = send(system, units[0], channel="a", bubble="one")
    sid, outside = system.source("evaluated"), system.source("never-shown")
    assessment = MemoryAssessment(
        notes=[note("early", [sid], about_ids=["thing"]), note("blocked", [outside]),
               note("about-blocked", [sid], about_ids=["blocked"]), note("fine", [sid])],
        links=[MemoryLink(subject="fine", object="blocked", evidence_ids=[sid]), MemoryLink(subject="fine", object=sid, evidence_ids=[sid]),
               MemoryLink(subject="about-blocked", object="fine", evidence_ids=[sid])],
        disclosures=[Disclosure(share_id=sent["share_id"], topic="stars", summary="Shared once", about_ids=["blocked"])],
        graph=GraphAssessment(
            nodes=[GraphNode(key="thing", kind="event", title="An unevaluated thing", evidence_ids=[outside]),
                   GraphNode(key="solid", kind="event", title="A sourced thing", evidence_ids=[sid]),
                   GraphNode(key="owned", kind="finding", title="A part of the thing", text="A part", owner_id="thing", evidence_ids=[sid])],
            edges=[GraphEdge(subject="solid", object="blocked", relation="about", evidence_ids=[sid], reason="Names the blocked note"),
                   GraphEdge(subject="solid", object="fine", relation="about", evidence_ids=[sid], reason="Names the kept note")]),
        coverage=CoverageAssessment(mappings=[CoverageMapping(share_id=sent["share_id"], bubble_id="one", confidence=.9,
            reason="Names the dropped finding", references=[ContentReference(unit_id="owned", version=1)])]),
        event_routes=[EventRoute(key="with-blocked", action="create", title="An event", member_ids=["blocked"], evidence_ids=[sid], reason="Grouped"),
                      EventRoute(key="with-fine", action="create", title="A kept event", member_ids=["fine"], evidence_ids=[sid], reason="Grouped")])
    return SimpleNamespace(assessment=assessment, sid=sid, sent=sent, evidence=[sid, sent["source_id"]])


def test_the_cascade_takes_every_item_that_names_a_dropped_key_and_commits_nothing_dangling(system):
    case = cascade_case(system)
    event_id = "mind_items_cascade"
    dropped = commit(system, case.assessment, case.evidence, event_id)
    out = "evidence-out-of-bounds"
    # Deterministic, in the order the host decided: causes first, then what named them. `notes[0]` had already
    # been applied when `thing` was blocked; the pass was undone and run again, so it appears after its cause.
    assert dropped == [
        drop("note", "notes[1]", out, OUTSIDE_NOTE),
        drop("note", "notes[2]", out, memory_items.CASCADED, "notes[1]"),
        drop("graph-node", "graph.nodes[0]", out, OUTSIDE_GRAPH),
        drop("note", "notes[0]", out, memory_items.CASCADED, "graph.nodes[0]"),
        drop("graph-node", "graph.nodes[2]", out, memory_items.CASCADED, "graph.nodes[0]"),
        drop("graph-edge", "graph.edges[0]", out, memory_items.CASCADED, "notes[1]"),
        drop("event-route", "event_routes[0]", out, memory_items.CASCADED, "notes[1]"),
        drop("link", "links[0]", out, memory_items.CASCADED, "notes[1]"),
        drop("link", "links[2]", out, memory_items.CASCADED, "notes[2]"),
        drop("disclosure", "disclosures[0]", out, memory_items.CASCADED, "notes[1]"),
        drop("coverage-mapping", "coverage.mappings[0]", out, memory_items.CASCADED, "graph.nodes[2]"),
    ]
    assert note_keys(system, event_id) == ["fine"]
    graph = system.memory.graph
    gone = [memory_items_alias(system, event_id, key) for key in ("early", "blocked", "about-blocked")]
    gone += [graph.identifier("event", [event_id, "thing"]), graph.identifier("finding", [event_id, "owned"])]
    # Nothing that was committed names an id a dropped item would have had.
    for table in ("records", "relations", "mind_graph_nodes", "mind_graph_edges", "mind_memory_nodes", "mind_event_routes", "mind_share_coverage"):
        stored = json.dumps(rows(system, "SELECT * FROM " + table))
        assert not [identifier for identifier in gone if identifier in stored], table
    # What did not depend on a dropped item is all there.
    kept_node = graph.identifier("event", [event_id, "solid"])
    assert [r["id"] for r in rows(system, "SELECT id FROM mind_graph_nodes WHERE json_extract(data,'$.assessment_event')=?", event_id)] == [kept_node]
    assert len(rows(system, "SELECT 1 FROM mind_graph_edges WHERE subject=? AND predicate='about'", kept_node)) == 1
    assert [json.loads(r["data"])["requested_action"] for r in rows(system, "SELECT data FROM mind_event_routes")] == ["create"]
    assert len(rows(system, "SELECT 1 FROM relations WHERE predicate='related'")) == 1
    share = system.memory.history("share", identifier=case.sent["share_id"])["items"][0]
    assert share["semantic_state"] == "pending" and "topic" not in share
    assert metrics(system, "memory_items_dropped")[0] | {"event_id": None} == {"value": 11, "event_id": None, "codes": {out: 11},
        "kinds": {"note": 3, "graph-node": 2, "graph-edge": 1, "event-route": 1, "link": 2, "disclosure": 1, "coverage-mapping": 1},
        "cascaded": 9, "withheld_sources": 1, "abandoned_sources": 0}
    # `notes[1]` cited no evaluated source, so it may have carried any of them: the one no committed item
    # holds - the delivery - is withheld, and the one the kept note and node rest on is organised.
    assert indexed(system, case.sid) and unorganized(system) == {case.sent["source_id"]: ("pending", 1)}


def memory_items_alias(system, event_id, key):
    from eventmem.core.db import digest
    return "mem_" + digest([system.mind.scope.key(), event_id, key])[:32]


def test_a_node_refused_in_its_second_part_takes_its_first_part_with_it(system):
    sid = system.source("evaluated")
    graph = GraphAssessment(nodes=[GraphNode(key="loop", kind="finding", title="Owns itself", text="A part", owner_id="loop", evidence_ids=[sid]),
                                   GraphNode(key="plain", kind="event", title="A sourced thing", evidence_ids=[sid])])
    dropped = commit(system, MemoryAssessment(graph=graph), [sid], "mind_items_owner")
    assert dropped == [drop("graph-node", "graph.nodes[0]", "self-reference", "A graph node cannot own itself")]
    ids = [r["id"] for r in rows(system, "SELECT id FROM mind_graph_nodes WHERE json_extract(data,'$.assessment_event')='mind_items_owner'")]
    assert ids == [system.memory.graph.identifier("event", ["mind_items_owner", "plain"])]
    # The kept node rests on the same source, so its content did reach memory: it is organised.
    assert indexed(system, sid) and unorganized(system) == {}
    # Alone, the refused node would have been that source's only carrier.
    only = GraphAssessment(nodes=[GraphNode(key="loop", kind="finding", title="Owns itself", text="A part", owner_id="loop", evidence_ids=[sid])])
    lonely = system.source("lonely")
    alone = GraphAssessment(nodes=[only.nodes[0].model_copy(update={"evidence_ids": [lonely]})])
    assert [d["code"] for d in commit(system, MemoryAssessment(graph=alone), [lonely], "mind_items_owner_alone")] == ["self-reference"]
    assert not indexed(system, lonely) and unorganized(system) == {lonely: ("pending", 1)}


def test_a_reference_by_stored_id_survives_a_refused_update_and_one_by_key_does_not(system):
    sid = system.source("evaluated")
    with system.mind.engine.db.connect(write=True) as conn:
        refs = system.memory.graph.proof(conn, [sid])
        stored = system.memory.graph._put(conn, {"id": system.memory.graph.identifier("entity", ["fixture"]), "kind": "entity",
            "title": "A stored project", "entity_type": "project", "source_ids": [sid], "evidence": refs, "basis": "observed"})
    graph = GraphAssessment(
        nodes=[GraphNode(key="rewrite", id=stored["id"], kind="event", title="Not an entity any more", expected_revision=stored["revision"], evidence_ids=[sid]),
               GraphNode(key="fresh", kind="event", title="A sourced thing", evidence_ids=[sid])],
        edges=[GraphEdge(subject="fresh", object=stored["id"], relation="about", evidence_ids=[sid], reason="Names the stored node"),
               GraphEdge(subject="fresh", object="rewrite", relation="related", evidence_ids=[sid], reason="Names the proposal's item")])
    dropped = commit(system, MemoryAssessment(graph=graph), [sid], "mind_items_update")
    assert dropped == [drop("graph-node", "graph.nodes[0]", "graph-kind-immutable", "Graph kind is immutable"),
                       drop("graph-edge", "graph.edges[1]", "graph-kind-immutable", memory_items.CASCADED, "graph.nodes[0]")]
    fresh = system.memory.graph.identifier("event", ["mind_items_update", "fresh"])
    assert [(r["predicate"], r["object"]) for r in rows(system, "SELECT predicate,object FROM mind_graph_edges WHERE subject=?", fresh)] == [("about", stored["id"])]
    with system.mind.engine.db.connect() as conn:
        assert system.memory.graph.get(conn, stored["id"])["kind"] == "entity"


def test_positions_are_the_proposals_own_whatever_the_host_filtered_out(system):
    system.memory.configure({"associations": False})
    sid, outside = system.source("evaluated"), system.source("never-shown")
    graph = GraphAssessment(nodes=[GraphNode(key="musing", kind="association", title="A private association", evidence_ids=[sid]),
                                   GraphNode(key="blocked", kind="event", title="An unevaluated thing", evidence_ids=[outside])])
    assert commit(system, MemoryAssessment(graph=graph), [sid], "mind_items_filtered") == [
        drop("graph-node", "graph.nodes[1]", "evidence-out-of-bounds", OUTSIDE_GRAPH)]


def test_a_source_is_withheld_only_when_no_committed_carrier_holds_it(system):
    first, second, outside = system.source("first"), system.source("second"), system.source("never-shown")
    # The dropped note cites no evaluated source at all, so it may have carried either of them.
    dropped = commit(system, MemoryAssessment(notes=[note("kept", [first]), note("unattributable", [outside])]),
                     [first, second], "mind_items_withheld", processed_refs=None)
    assert [d["item"]["key"] for d in dropped] == ["notes[1]"]
    assert indexed(system, first) and not indexed(system, second)
    assert unorganized(system) == {second: ("pending", 1)}
    assert metrics(system, "memory_items_dropped")[0]["withheld_sources"] == 1


def test_a_stale_citation_still_fails_the_whole_section_and_drops_nothing(system):
    cited, other = system.source("cited"), system.source("other")
    with system.mind.engine.db.connect() as conn:
        supplied = system.mind._evidence(conn, [cited, other])
    # A correction of the cited source: still in bounds, but no longer current.
    system.source("cited", "A corrected synthetic source", version="2")
    assessment = MemoryAssessment(notes=[note("valid", [other]), note("stale", [supplied[0]["record_id"]])])
    with pytest.raises(Conflict) as stale, system.mind.engine.db.connect(write=True) as conn:
        system.memory.apply_assessment(conn, assessment, supplied, "mind_items_stale", 0, 20, RECEIPT, schedule=False)
    found = classify(stale.value)
    assert (found.kind, found.code, found.handling) == ("runtime", "cited-evidence-changed", "reuse")
    assert note_keys(system, "mind_items_stale") == [] and metrics(system, "memory_items_dropped") == []
    assert not indexed(system, other) and unorganized(system) == {}


class Scripted:
    """One proposal per stimulus; records what it was asked."""

    def __init__(self, **by_stimulus):
        self.by_stimulus, self.calls = by_stimulus, []

    def appraise(self, context):
        self.calls.append(context["stimulus"])
        proposal = self.by_stimulus.get(str(context["stimulus"]).replace("-", "_"), Appraisal(reason="Nothing to record"))
        return (proposal(context) if callable(proposal) else proposal), dict(RECEIPT)


def enrichment(system, evidence_ids):
    system.memory.configure({"operational_lanes": True})
    jobs = Appraisals(system.mind)
    return jobs, jobs.enqueue(evidence_ids, "fixture-v1", stimulus="memory-enrichment")["id"]


PRIVATE = "PRIVATE-WORDS-OF-THE-OWNER"


def blocked_proposal(first, second, outside):
    return Appraisal(reason="Organise two sourced events", memory=MemoryAssessment(
        notes=[note("kept", [first]),
               MemoryNote(key=PRIVATE + "-key", title=PRIVATE + " title", content=PRIVATE + " body", evidence_ids=[second, outside])],
        links=[MemoryLink(subject="kept", object=PRIVATE + "-key", evidence_ids=[first])]))


def test_a_source_whose_only_note_was_dropped_is_organised_again_by_a_later_enrichment(system):
    first, second, outside = system.source("first"), system.source("second"), system.source("never-shown")
    jobs, job_id = enrichment(system, [first, second])
    provider = Scripted(memory_enrichment=blocked_proposal(first, second, outside),
                        memory_backfill=lambda context: Appraisal(reason="Organise what was left", values={"mood": 3},
                            memory=MemoryAssessment(notes=[note("second-again", [second])])))
    result = jobs.run_one(provider, lane="enrichment")
    assert result["state"] == "complete" and provider.calls == ["memory-enrichment"]
    # The row and the durable receipt say what was dropped: static codes and host text, nothing of the items.
    data = job(system, job_id)
    assert data["rejected_sections"] == result["result"]["rejected_sections"] == [
        drop("note", "notes[1]", "evidence-out-of-bounds", OUTSIDE_NOTE),
        drop("link", "links[0]", "evidence-out-of-bounds", memory_items.CASCADED, "notes[1]")]
    assert "error" not in data and "held_sections" not in data
    assert note_keys(system, result["result"]["event_id"]) == ["kept"]
    assert indexed(system, first) and not indexed(system, second)
    assert unorganized(system) == {second: ("pending", 1)}
    # Nothing asks the model again about the drop itself.
    assert rows(system, "SELECT id FROM mind_appraisals WHERE json_extract(data,'$.stimulus')=?", FOLLOW_UP) == []
    before = system.mind.read()["dimensions"]["mood"]["value"]

    # The host's minute review gives the withheld source one memory-only pass, and only that source.
    assert system.memory.queue_unorganized(jobs, "fixture-v1")["state"] == "queued"
    assert system.memory.queue_unorganized(jobs, "fixture-v1")["state"] == "pending"
    later = Appraisals(system.mind).run_one(provider, lane="enrichment")
    assert later["state"] == "complete" and provider.calls == ["memory-enrichment", "memory-backfill"]
    assert job(system, later["id"])["evidence_ids"] == [second]
    assert note_keys(system, later["result"]["event_id"]) == ["second-again"]
    assert indexed(system, second) and unorganized(system) == {}
    # It ran as history: the experience was organised, not scored again.
    assert system.mind.read()["dimensions"]["mood"]["value"] == before
    assert system.memory.queue_unorganized(jobs, "fixture-v1") == {"state": "idle"}


def test_the_record_the_metric_and_the_ledger_hold_static_text_only(system):
    first, second, outside = system.source("first"), system.source("second"), system.source("never-shown")
    jobs, job_id = enrichment(system, [first, second])
    assert jobs.run_one(Scripted(memory_enrichment=blocked_proposal(first, second, outside)), lane="enrichment")["state"] == "complete"
    data = job(system, job_id)
    recorded = json.dumps([data["rejected_sections"], data["result"]["rejected_sections"], metrics(system, "memory_items_dropped"),
                           rows(system, "SELECT * FROM mind_memory_unorganized"), read_ledger(system.mind.engine, system.mind.scope.key(), job_id=job_id)])
    assert PRIVATE not in recorded and outside not in recorded
    # The proposal itself stays where it always was, for an operator to resolve `notes[1]` against.
    assert data["proposed_result"]["memory"]["notes"][1]["key"] == PRIVATE + "-key"
    assert metrics(system, "memory_items_dropped") == [{"value": 2, "event_id": data["result"]["event_id"], "codes": {"evidence-out-of-bounds": 2},
        "kinds": {"note": 1, "link": 1}, "cascaded": 1, "withheld_sources": 1, "abandoned_sources": 0}]
    [attempt] = read_ledger(system.mind.engine, system.mind.scope.key(), job_id=job_id)["attempts"]
    assert attempt["outcome"] == "committed" and "error" not in attempt


def test_a_source_withheld_again_is_left_for_an_operator_not_retried_for_ever(system):
    first, second, outside = system.source("first"), system.source("second"), system.source("never-shown")
    jobs, _ = enrichment(system, [first, second])
    again = Appraisal(reason="The same mistake", memory=MemoryAssessment(notes=[note("second-again", [second, outside])]))
    provider = Scripted(memory_enrichment=blocked_proposal(first, second, outside), memory_backfill=again)
    assert jobs.run_one(provider, lane="enrichment")["state"] == "complete"
    system.memory.queue_history(jobs, "fixture-v1")
    assert Appraisals(system.mind).run_one(provider, lane="enrichment")["state"] == "complete"
    assert unorganized(system) == {second: ("abandoned", 2)} and not indexed(system, second)
    assert metrics(system, "memory_items_dropped")[-1]["abandoned_sources"] == 1
    # Bounded: no third pass is queued, and the source is still not written down as organised.
    assert system.memory.queue_unorganized(jobs, "fixture-v1") == {"state": "idle"}
    assert Appraisals(system.mind).run_one(provider, lane="enrichment") == {"state": "idle"}
    assert provider.calls == ["memory-enrichment", "memory-backfill"]


def test_the_action_lane_had_already_indexed_the_source_so_the_ledger_is_what_withholds_it(system):
    system.memory.configure({"operational_lanes": True})
    sid, outside = system.source("owner-chat"), system.source("never-shown")
    jobs = Appraisals(system.mind)
    jobs.enqueue([sid], "fixture-v1")
    provider = Scripted(memory_enrichment=Appraisal(reason="Organise it", memory=MemoryAssessment(notes=[note("only", [sid, outside])])),
                        memory_backfill=Appraisal(reason="Organise it", memory=MemoryAssessment(notes=[note("only-again", [sid])])))
    assert jobs.run_one(provider, lane="action")["state"] == "complete"
    # commit_action indexes the source for the action lane before any memory is organised.
    assert indexed(system, sid)
    assert Appraisals(system.mind).run_one(provider, lane="enrichment")["state"] == "complete"
    assert unorganized(system) == {sid: ("pending", 1)}
    system.memory.queue_history(jobs, "fixture-v1")
    later = Appraisals(system.mind).run_one(provider, lane="enrichment")
    assert later["state"] == "complete" and note_keys(system, later["result"]["event_id"]) == ["only-again"]
    assert unorganized(system) == {} and provider.calls == ["interaction-batch", "memory-enrichment", "memory-backfill"]


def test_without_lanes_the_cursor_moves_on_and_the_source_is_organised_as_history(system):
    sid, outside = system.source("owner-chat", "A synthetic owner message"), system.source("never-shown")
    event = system.memory.ingest({"id": "owner-chat", "kind": "owner-message", "at": system.mind.clock(), "source_id": sid})
    jobs = Appraisals(system.mind)
    job_id = jobs.enqueue([sid], "fixture-v1")["id"]
    provider = Scripted(interaction_batch=Appraisal(reason="A real owner message", values={"mood": 72}, memory=MemoryAssessment(notes=[note("only", [sid, outside])])),
                        memory_backfill=Appraisal(reason="Organise it", values={"mood": 3}, memory=MemoryAssessment(notes=[note("only-again", [sid])])))
    result = jobs.run_one(provider, job_id=job_id)
    assert result["state"] == "complete" and [d["item"]["key"] for d in job(system, job_id)["rejected_sections"]] == ["notes[0]"]
    # The event was scored once and its cursor moves on; only its memory is still owed.
    assert system.mind.read()["dimensions"]["mood"]["value"] == 72
    assert system.memory.semantic_context()["cursor"] == event["seq"]
    assert not indexed(system, sid) and unorganized(system) == {sid: ("pending", 1)}
    system.memory.queue_history(jobs, "fixture-v1")
    later = Appraisals(system.mind).run_one(provider)
    assert later["state"] == "complete" and job(system, later["id"])["stimulus"] == "memory-backfill"
    assert note_keys(system, later["result"]["event_id"]) == ["only-again"]
    assert indexed(system, sid) and unorganized(system) == {}
    assert system.mind.read()["dimensions"]["mood"]["value"] == 72


def test_a_citation_that_moved_during_the_evaluation_fails_the_attempt_and_drops_nothing(system):
    earlier = system.memory.ingest({"id": "earlier", "kind": "owner-message", "at": system.mind.clock(),
                                    "text": "The earlier result is unfinished.", "historical": True})
    sid, outside = system.source("owner-chat"), system.source("never-shown")
    jobs, job_id = enrichment(system, [sid])

    def proposal(context):
        with system.mind.engine.db.connect() as conn:
            rid = system.mind._evidence(conn, [earlier["source_id"]])[0]["record_id"]
        system.mind.engine.revise(rid, RevisionInput(expected_revision=1, command_id="revise-during-evaluation",
                                                     action="correct", content="The result has now been completed."))
        return Appraisal(reason="Organise it", memory=MemoryAssessment(
            notes=[note("valid", [sid]), note("blocked", [sid, outside]), note("stale", [earlier["source_id"]])]))

    result = jobs.run_one(Scripted(memory_enrichment=proposal), lane="enrichment")
    data = job(system, job_id)
    assert result["state"] == "pending" and data["error"] == "Conflict"
    assert (data["error_detail"]["kind"], data["error_detail"]["code"]) == ("runtime", "cited-evidence-changed")
    assert "rejected_sections" not in data and metrics(system, "memory_items_dropped") == []
    assert rows(system, "SELECT 1 FROM records WHERE json_extract(data,'$.attributes.semantic_event') IS NOT NULL") == []
    assert not indexed(system, sid) and unorganized(system) == {}


def test_an_unclassified_failure_still_fails_the_attempt(system):
    sid, outside = system.source("owner-chat"), system.source("never-shown")
    jobs, job_id = enrichment(system, [sid])
    proposal = Appraisal(reason="Organise it", memory=MemoryAssessment(
        notes=[note("valid", [sid]), note("blocked", [sid, outside])],
        links=[MemoryLink(subject="valid", object="no-such-key", evidence_ids=[sid])]))
    result = jobs.run_one(Scripted(memory_enrichment=proposal), lane="enrichment")
    data = job(system, job_id)
    assert result["state"] == "pending" and data["error"] == "Missing" and data["error_detail"]["kind"] == "unknown"
    # The blocked note was not dropped in a commit that never happened.
    assert "rejected_sections" not in data and metrics(system, "memory_items_dropped") == []
    assert rows(system, "SELECT 1 FROM records WHERE json_extract(data,'$.attributes.semantic_event') IS NOT NULL") == []
    assert not indexed(system, sid) and unorganized(system) == {}


def test_with_the_switch_off_the_whole_appraisal_fails_as_before(system):
    system.memory.configure({memory_items.SWITCH: False})
    first, second, outside = system.source("first"), system.source("second"), system.source("never-shown")
    jobs, job_id = enrichment(system, [first, second])
    provider = Scripted(memory_enrichment=blocked_proposal(first, second, outside))
    result = jobs.run_one(provider, lane="enrichment")
    data = job(system, job_id)
    assert result["state"] == "pending" and data["error"] == "Conflict"
    assert data["error_detail"] == {"class": "Conflict", "message": OUTSIDE_NOTE, "kind": "semantic", "code": "evidence-out-of-bounds",
                                    "target": data["error_detail"]["target"]}
    assert "rejected_sections" not in data and metrics(system, "memory_items_dropped") == []
    assert rows(system, "SELECT 1 FROM records WHERE json_extract(data,'$.attributes.semantic_event') IS NOT NULL") == []
    assert not indexed(system, first) and unorganized(system) == {}
    assert system.memory.queue_unorganized(jobs, "fixture-v1") == {"state": "disabled"}
    # The historical lane still quarantines it on the second charged attempt.
    with system.mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job_id,))
    assert Appraisals(system.mind).run_one(provider, lane="enrichment")["state"] == "needs-repair"
    with pytest.raises(Conflict):
        commit(system, MemoryAssessment(notes=[note("blocked", [outside])]), [first], "mind_items_off")


def test_with_the_switch_off_coverage_faults_surface_in_the_order_they_always_did(system):
    """Every share was looked up before any mapping was applied, so an unreadable share in a later
    mapping surfaced before an earlier mapping's own fault. Off, that order is kept exactly."""
    units = findings(system)
    seen = send(system, units[0], channel="a", bubble="one")
    sid = system.source("evaluated-chat")
    reference = [ContentReference(unit_id=units[0]["id"], version=1)]
    assessment = MemoryAssessment(coverage=CoverageAssessment(mappings=[
        CoverageMapping(share_id=seen["share_id"], bubble_id="invented", confidence=.9, reason="An unknown bubble", references=reference),
        CoverageMapping(share_id="share_" + "0" * 32, bubble_id="one", confidence=.9, reason="No such share", references=reference)]))
    system.memory.configure({memory_items.SWITCH: False})
    with pytest.raises(Missing):
        commit(system, assessment, [sid, seen["source_id"]], "mind_items_order_off")
    # On, each mapping answers for itself: the first is blocked and dropped, the second is unclassified and fails the section.
    system.memory.configure({memory_items.SWITCH: True})
    with pytest.raises(Missing):
        commit(system, assessment, [sid, seen["source_id"]], "mind_items_order_on")
    assert coverage(system, units[0], seen["share_id"])[0]["basis"] == "registered-reference" and unorganized(system) == {}
    # And with only the blocked one, it is dropped alone.
    alone = MemoryAssessment(coverage=CoverageAssessment(mappings=assessment.coverage.mappings[:1]))
    assert commit(system, alone, [sid, seen["source_id"]], "mind_items_order_alone") == [
        drop("coverage-mapping", "coverage.mappings[0]", "bubble-unknown", "Coverage mapping refers to an unknown bubble")]


def test_a_dropped_memory_item_is_recorded_beside_a_refused_section_and_asked_for_by_no_follow_up(system):
    sid, outside = system.source("owner-asks-to-pause-exploring"), system.source("never-shown")
    jobs = Appraisals(system.mind)
    job_id = jobs.enqueue([sid], "fixture-v1")["id"]
    proposal = Appraisal(reason="She wants fewer explorations", values={"mood": 70},
        habits=HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[sid], reason="She asked to pause", expected_revision=7),
        memory=MemoryAssessment(notes=[note("kept", [sid]), note("blocked", [sid, outside])]))
    result = jobs.run_one(Scripted(interaction_batch=proposal), job_id=job_id)
    assert result["state"] == "complete"
    stale_habits = {"section": "habits", "code": "conflict", "message": "Conversation preferences changed"}
    blocked = drop("note", "notes[1]", "evidence-out-of-bounds", OUTSIDE_NOTE)
    assert job(system, job_id)["rejected_sections"] == [stale_habits, blocked]
    # The refused owner preference is asked for again; the dropped note is not part of that question.
    [follow_up] = rows(system, "SELECT data FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=?", job_id)
    review = json.loads(follow_up["data"])
    assert review["stimulus"] == FOLLOW_UP and review["section_review"]["rejected_sections"] == [stale_habits]
    # Another note carried the source, so it is organised.
    assert indexed(system, sid) and unorganized(system) == {}


def test_an_unknown_failure_inside_an_item_is_never_dropped(system):
    sid = system.source("evaluated")
    with pytest.raises(Missing):
        commit(system, MemoryAssessment(notes=[note("valid", [sid])], links=[MemoryLink(subject="valid", object="no-such-key", evidence_ids=[sid])]),
               [sid], "mind_items_unknown")
    assert note_keys(system, "mind_items_unknown") == [] and not indexed(system, sid)
