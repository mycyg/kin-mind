"""A graph edge moved by a merge or a split is the edge of its new ends (K4-09); `split` is no
second name for `undo` (K4-08); a light keep never resets the version check over what the owner
wrote (K4-11); and a citation resolves to what this run observed (K4-17)."""
from datetime import datetime, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput

from kin_mind import revalidation
from kin_mind.graph import GraphAssessment, GraphNode
from kin_mind.memory import MemoryContinuity
from kin_mind.source_ledger import LAYER_HISTORICAL, LAYER_OBSERVED, legitimize
from kin_mind.state import Mind


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 14, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, Scope(persona="synthetic-edges"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)
    sid = engine.receive(SourceInput(namespace="synthetic", key="identity", text="Two labels name one project",
                                     scope=mind.scope, authority="explicit", occurred_at=mind.clock(), extract=False))["id"]
    mind.initialize(agent_version="fixture-v1", evidence_ids=[sid])
    memory.configure({"graph": True})
    graph = memory.graph
    with engine.db.connect(write=True) as conn:
        refs = graph.proof(conn, [sid])
        graph.apply(conn, GraphAssessment(nodes=[GraphNode(key=k, kind="entity", title=k, entity_type="project",
                                                           evidence_ids=[sid]) for k in ("a", "b", "c")]), refs, "identity", {})
        a, b, c = [graph.get(conn, graph.identifier("entity", ["identity", k])) for k in ("a", "b", "c")]
        graph.link(conn, a["id"], "about", c["id"], refs, reason="fixture")
        graph.link(conn, b["id"], "about", c["id"], refs, reason="fixture")
    return mind, graph, sid, refs, a, b, c


def active_edges(mind, subject, object_id):
    with mind.engine.db.connect() as conn:
        return [row[0] for row in conn.execute(
            "SELECT id FROM mind_graph_edges WHERE scope=? AND subject=? AND object=? AND predicate='about' AND state='active'",
            (mind.scope.key(), subject, object_id))]


def merge(graph, sid, a, b, command="merge"):
    return graph.revise({"id": a["id"], "expected_revision": a["revision"], "target_id": b["id"],
                         "target_revision": b["revision"], "action": "merge", "command_id": command,
                         "reason": "Same project", "evidence_ids": [sid]})


def test_a_merge_leaves_one_edge_between_the_same_ends_and_a_later_link_finds_it(system):
    mind, graph, sid, refs, a, b, c = system
    merge(graph, sid, a, b)
    edges = active_edges(mind, b["id"], c["id"])
    assert len(edges) == 1 and active_edges(mind, a["id"], c["id"]) == []
    with mind.engine.db.connect(write=True) as conn:
        again = graph.link(conn, b["id"], "about", c["id"], refs, reason="later")
    assert [again["id"]] == edges == active_edges(mind, b["id"], c["id"])


def test_undoing_a_merge_puts_the_edges_back(system):
    mind, graph, sid, refs, a, b, c = system
    before = {"a": active_edges(mind, a["id"], c["id"]), "b": active_edges(mind, b["id"], c["id"])}
    merged = merge(graph, sid, a, b)
    graph.revise({"id": a["id"], "expected_revision": merged["after_revisions"][a["id"]], "action": "undo",
                  "previous_command_id": "merge", "command_id": "undo-merge", "reason": "Not the same",
                  "evidence_ids": [sid]})
    assert active_edges(mind, a["id"], c["id"]) == before["a"] and active_edges(mind, b["id"], c["id"]) == before["b"]


def test_split_is_not_a_name_for_undo(system):
    mind, graph, sid, refs, a, b, c = system
    with pytest.raises(ValueError, match="split_event"):
        graph.revise({"id": a["id"], "expected_revision": a["revision"], "action": "split",
                       "previous_command_id": "x", "command_id": "split", "reason": "r", "evidence_ids": [sid]})


def test_a_light_keep_resets_nothing_over_what_the_owner_wrote():
    proposal = {"graph": {"nodes": [{"id": "node-1", "expected_revision": 1}]}, "habits": {"expected_revision": 2}}
    paths = [["graph", "nodes", 0]]
    assert revalidation._resets(proposal, "graph", "node-1", paths, 3)[0] == [(["graph", "nodes", 0, "expected_revision"], 3)]
    assert revalidation._resets(proposal, "graph", "node-1", paths, 3, owner_held=True)[0] == []
    assert revalidation._resets(proposal, "habits", "habits", [["habits"]], 4, owner_held=True)[0] == []


def test_a_citation_resolves_to_what_this_run_observed():
    carried = {"state": LAYER_HISTORICAL, "locator": "https://example.org/page", "version": "v1"}
    read_now = {"state": LAYER_OBSERVED, "locator": "https://example.org/page", "version": "v2"}
    assert legitimize([read_now, carried], "https://example.org/page")["version"] == "v2"
    assert legitimize([carried], "https://example.org/page")["version"] == "v1"
