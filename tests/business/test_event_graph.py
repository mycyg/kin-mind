from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine

from eventmem.core.db import Conflict, Missing

from eventmem.core.models import RevisionInput, Scope, SourceInput

from eventmem.core.read_policy import ReadPolicy

from kin_mind.context import Contexts

from kin_mind.graph import EventGraph, GraphAssessment, GraphEdge, GraphNode

from kin_mind.memory import MemoryAssessment, MemoryContinuity

from kin_mind.sharing import (
    ContentReference,
    CoverageAssessment,
    CoverageMapping,
    ShareCheck,
)

from kin_mind.state import Mind

@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 14, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, Scope(persona="synthetic-graph"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)
    def source(key, text=None):
        return engine.receive(SourceInput(namespace="synthetic", key=key, text=text or key, scope=mind.scope,
            authority="explicit", occurred_at=mind.clock(), extract=False))["id"]
    initial = source("initial")
    mind.initialize(agent_version="fixture-v1", evidence_ids=[initial])
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True, "sharing": True, "graph_recall": True, "associations": True})
    return mind, memory, source, clock

def findings(system):
    mind, memory, source, _ = system
    sid = source("observations", "The synthetic exploration has three independent observations.")
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [sid])
        owner = memory.graph._put(conn, {"id": "explore-fixture", "kind": "exploration", "title": "Synthetic star colors", "text": "",
            "source_ids": [sid], "evidence": refs, "basis": "documented"})
        units = memory.sharing.units(conn, owner["id"], ["Young synthetic stars appear blue in this fictional dataset.", "Older synthetic stars appear red in this fictional dataset.", "The fictional telescope has not confirmed the third observation."], [sid])
    return units

def send(system, unit, *, bubble="one", state="accepted", channel="a", mode="new", text=None):
    mind, memory, _, _ = system
    event = {"id": f"{channel}-{bubble}-{state}", "kind": "delivery", "at": mind.clock(), "channel": channel,
        "delivery_id": "batch", "bubble_id": bubble, "text": text or unit["text"], "state": state,
        "references": [{"unit_id": unit["id"], "version": 1, "mode": mode}]}
    if state == "accepted":
        event["message_id"] = channel + "-receipt-" + bubble
    return memory.ingest(event)

def freshness_graph(system):
    mind, memory, source, _ = system
    graph = memory.graph
    sid = source("freshness-cache", "One source supports two nodes and repeated edges.")
    with mind.engine.db.connect(write=True) as conn:
        refs = graph.proof(conn, [sid])
        left = graph._put(conn, {"id": "freshness-left", "kind": "event", "title": "left",
                                 "text": "", "source_ids": [sid], "evidence": refs,
                                 "basis": "documented"})
        right = graph._put(conn, {"id": "freshness-right", "kind": "event", "title": "right",
                                  "text": "", "source_ids": [sid], "evidence": refs,
                                  "basis": "documented"})
        edges = [graph.link(conn, left["id"], relation, right["id"], refs,
                            reason="Repeated endpoint freshness fixture")
                 for relation in ("related", "supports", "continues")]
    policy = ReadPolicy.load(mind.engine, mind.scope, "audit")
    return graph, sid, refs[0]["record_id"], left["id"], edges, policy

def test_graph_links_roles_and_association_keep_their_basis(system):
    mind, memory, source, clock = system
    first = memory.ingest({"id": "request", "kind": "owner-message", "at": mind.clock(), "text": "Plan the fictional telescope", "task_id": "telescope"})
    clock[0] += timedelta(days=40)
    second = memory.ingest({"id": "result", "kind": "task-result", "at": mind.clock(), "text": "The telescope plan is ready", "task_id": "telescope"})
    sid = source("association", "A synthetic association, not an observed consequence")
    proposal = GraphAssessment(nodes=[GraphNode(key="thought", kind="association", title="A pocket telescope", evidence_ids=[sid], basis="internal_thought")],
        edges=[GraphEdge(subject=second["event_id"], object=first["event_id"], relation="continues", evidence_ids=[sid], reason="Same host task"),
               GraphEdge(subject=second["event_id"], object="thought", relation="association", basis="internal_thought", evidence_ids=[sid], reason="Inspired by the telescope")])
    with mind.engine.db.connect(write=True) as conn:
        memory.graph.apply(conn, proposal, memory.graph.proof(conn, [sid]), "evaluation", {"model": "synthetic"})
    graph = memory.graph.read(focus=first["event_id"], hops=3)
    assert second["event_id"] in {n["id"] for n in graph["nodes"]}
    assert any(e["role"] == "requester" for e in graph["edges"])
    assert any(e["layer"] == "association" for e in graph["edges"])
    assert not any(e["predicate"] == "causes" for e in graph["edges"])

def test_merge_and_split_retain_inverse_history(system):
    mind, memory, source, _ = system
    sid = source("identity", "The two synthetic labels name the same project")
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [sid])
        memory.graph.apply(conn, GraphAssessment(nodes=[GraphNode(key=k, kind="entity", title=k, entity_type="project", evidence_ids=[sid]) for k in ("a", "b")]), refs, "identity", {})
        a, b = [memory.graph.get(conn, memory.graph.identifier("entity", ["identity", k])) for k in ("a", "b")]
    command = {"id": a["id"], "expected_revision": a["revision"], "target_id": b["id"], "target_revision": b["revision"], "action": "merge", "command_id": "merge", "reason": "Source identity", "evidence_ids": [sid]}
    merged = memory.graph.revise(command)
    assert merged == memory.graph.revise(command)
    memory.graph.revise({"id": a["id"], "expected_revision": merged["after_revisions"][a["id"]], "action": "split", "previous_command_id": "merge", "command_id": "split", "reason": "New correction", "evidence_ids": [sid]})
    assert memory.graph.detail(a["id"])["state"] == "active"
    assert len(memory.graph.detail(a["id"])["history"]) == 3

def test_conversation_changes_habits_and_silence_is_per_input(system):
    mind,memory,_,_=system
    first=memory.ingest({"id":"owner-pref","kind":"owner-message","at":mind.clock(),"text":"You may choose silence in casual chat; explore fiction more often."})
    request={"command_id":"preference","expected_revision":0,"evidence_ids":[first["source_id"]],"reason":"Explicit owner request","preferences":{"reply_choice":"autonomous","exploration_frequency":"Explore when a fiction idea appears","exploration_directions":["fiction"],"exploration_min_interval_minutes":0}}
    result=memory.habits.update(request)
    assert result==memory.habits.update(request)
    assert memory.habits.read()["preferences"]["exploration_directions"]==["fiction"]
    choice=memory.habits.choose_reply({"input_id":"owner-pref","action":"silent","reason":"Let this casual turn rest"})
    assert choice==memory.habits.reply_status("owner-pref")
    memory.ingest({"id":"owner-next","kind":"owner-message","at":mind.clock(),"text":"What do you think of this new topic?"})
    assert memory.habits.reply_status("owner-next")["action"]=="reply"
    assert MemoryContinuity(mind).habits.reply_status("owner-pref")["action"]=="silent"
    with pytest.raises(Missing):
        memory.habits.choose_reply({"input_id":"invented","action":"silent","reason":"No input"})
    assistant=memory.ingest({"id":"assistant-pref","kind":"assistant-message","at":mind.clock(),"text":"I prefer that the owner never replies"})
    with pytest.raises(Conflict):
        memory.habits.update({**request,"command_id":"inferred","expected_revision":1,"evidence_ids":[assistant["source_id"]]})
    with pytest.raises(Conflict):
        memory.habits.update({**request,"command_id":"bad-revision","preferences":{"exploration_paused":True}})
