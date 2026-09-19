"""Derived views obey the same read policy as the records behind them.

A graph projection, an event summary, a topic family, a cached overview, a window receipt and a
continuity manifest are all made out of records. None of them may carry a role declaration, a
synthetic example or a host envelope into an experience read, and none may label one `explicit`.
Under `audit` every one of them is still there, labelled with its class. Synthetic data only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from eventmem.core.read_policy import (
    MIGRATION,
    RULES_VERSION,
    ReadPolicy,
    configure_registry,
)
from kin_mind.context import Contexts
from kin_mind.continuity_manifest import ContinuityManifest
from kin_mind.lifecycle import EventLifecycle, EventSummary, SummaryUnit
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

SCOPE = Scope(persona="synthetic-derived")
ENVELOPE = "内部探索选题事件，宿主要求挑选一个 lantern 选题，不发送消息。"
ROLE_TEXT = "Installed role text: the agent always answers lantern questions warmly and at length."
# Specimens a derived view must not carry into an experience read, with the class each shows as.
SPECIMENS = {"configuration": "role_configuration", "example": "synthetic_example",
             "envelope": "host_envelope"}
SETTINGS = {"records": True, "semantic": True, "context": True, "graph": True, "graph_recall": True,
            "sharing": True, "event_lifecycle": True, "auto_volumes": True, "manifests": True}


def root(engine, source):
    return next(rid for rid in engine.source(source["id"])["record_ids"] if not engine.get(rid)["evidence_ids"])


class Summarizer:
    """A scripted digest provider: it repeats what it was given, so a summary that carries
    something it should not have been given is visible in the stored text."""

    timeout = 300
    background = False

    def __init__(self):
        self.seen = []

    def structured(self, name, schema, instruction, context, **options):
        records = context["records"]
        self.seen.append({r["id"] for r in records})
        return EventSummary(narrative=[SummaryUnit(text=r["text"], record_ids=[r["id"]]) for r in records]), {
            "model": "synthetic", "reasoning": "none"}


@pytest.fixture
def world(tmp_path):
    """One lived record and the three kinds of non-experience, each projected into the graph and
    gathered into one event, so every derived view has both to choose from."""
    clock = [datetime(2026, 9, 16, 5, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "memory")
    configure_registry(engine, {"synthetic-persona-store": "role_configuration"})
    mind = Mind(engine, SCOPE, clock=lambda: clock[0].isoformat(timespec="microseconds"))

    def receive(namespace, key, text, *, authority="explicit", **metadata):
        return engine.receive(SourceInput(namespace=namespace, key=key, text=text, scope=SCOPE, title=key,
                                          authority=authority, occurred_at=clock[0].isoformat(), metadata=metadata))

    sources = {"lived": receive("chat", "lantern-walk", "We walked through the lantern market together.",
                                role="user", host_event="message"),
               "configuration": receive("synthetic-persona-store", "installed", ROLE_TEXT,
                                        authority="operation", role="host", host_event="configuration-verified"),
               "example": receive("chat", "lantern-example", "Example dialogue: we watched lantern boats last winter.",
                                  role="assistant", host_event="message", examples_are_synthetic=True),
               "envelope": receive("chat", "lantern-envelope", ENVELOPE, role="user", host_event="message")}
    ids = {name: root(engine, source) for name, source in sources.items()}
    mind.initialize(agent_version="synthetic-v1", evidence_ids=[ids["lived"]])
    memory = MemoryContinuity(mind)
    memory.configure(SETTINGS)
    return {"engine": engine, "mind": mind, "memory": memory, "contexts": Contexts(mind),
            "ids": ids, "sources": sources, "clock": clock}


def policy(world, purpose="experience_recall"):
    return ReadPolicy.load(world["engine"], SCOPE, purpose)


def project(world, names=("lived", "configuration", "example", "envelope")):
    """Project each record as a graph node, exactly as `ensure` does for a domain object."""
    nodes = {}
    with world["engine"].db.connect(write=True) as conn:
        for name in names:
            nodes[name] = world["memory"].graph.ensure(conn, world["ids"][name])
    return nodes


def event_of(world, names=("lived", "configuration", "example", "envelope")):
    """One event whose members are those projections, built the way routing builds them."""
    graph, nodes = world["memory"].graph, project(world, names)
    with world["engine"].db.connect(write=True) as conn:
        refs = graph.proof(conn, [world["ids"]["lived"]])
        event = graph._put(conn, {"id": graph.identifier("event", ["synthetic-evening"]), "kind": "event",
                                  "title": "Lantern evening", "text": "", "basis": "inferred",
                                  "source_ids": [r["source_id"] for r in refs], "evidence": refs,
                                  "occurred_at": world["mind"].clock(), "lifecycle": "open",
                                  "membership_authority": "graph"})
        for node in nodes.values():
            graph.link(conn, node["id"], "part_of", event["id"], refs, reason="Synthetic membership")
    return event, nodes


def switch_off(world):
    world["memory"].configure({"recall_purpose_policy": False})


def start_migration(world, state="classified"):
    with world["engine"].db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",
                     (SCOPE.key(), MIGRATION, 0, json.dumps({"state": state, "rules_version": RULES_VERSION})))


def test_graph_candidates_hide_non_experience_and_audit_shows_them(world):
    nodes = project(world)
    graph = world["memory"].graph
    with world["engine"].db.connect() as conn:
        found = {n["id"] for n in graph.candidates(conn, "lantern")}
        audited = {n["id"]: n for n in graph.candidates(conn, "lantern", policy=policy(world, "audit"))}
    assert nodes["lived"]["id"] in found
    for name in SPECIMENS:
        assert nodes[name]["id"] not in found
        assert nodes[name]["id"] in audited
    with world["engine"].db.connect() as conn:
        records = graph.node_records(conn, list(audited.values()))
        audit = policy(world, "audit")
        for name, kind in SPECIMENS.items():
            assert audit.node_label(audited[nodes[name]["id"]], records) == kind


def test_graph_ensure_projects_a_class_never_explicit(world):
    nodes = project(world)
    assert nodes["lived"]["basis"] == world["engine"].get(world["ids"]["lived"])["confirmation"] == "explicit"
    for name, kind in SPECIMENS.items():
        assert nodes[name]["basis"] == kind
    # The stored node is what the projection wrote once; a later read never rewrites it.
    with world["engine"].db.connect() as conn:
        again = world["memory"].graph.ensure(conn, world["ids"]["configuration"])
    assert (again["revision"], again["basis"]) == (nodes["configuration"]["revision"], "role_configuration")


def test_graph_read_and_its_hops_do_not_cross_a_hidden_projection(world):
    graph, ids = world["memory"].graph, world["ids"]
    nodes = project(world)
    far = world["engine"].receive(SourceInput(namespace="chat", key="beyond", title="beyond",
        text="A note reachable only through the role declaration.", scope=SCOPE, authority="explicit",
        occurred_at=world["clock"][0].isoformat(), metadata={"role": "user", "host_event": "message"}))
    with world["engine"].db.connect(write=True) as conn:
        beyond = graph.ensure(conn, root(world["engine"], far))
        refs = graph.proof(conn, [ids["lived"]])
        graph.link(conn, nodes["lived"]["id"], "related", nodes["configuration"]["id"], refs, reason="Synthetic link")
        graph.link(conn, nodes["configuration"]["id"], "related", beyond["id"], refs, reason="Synthetic link")
    reached = {n["id"] for n in graph.read(focus=nodes["lived"]["id"], hops=2)["nodes"]}
    assert nodes["lived"]["id"] in reached
    assert nodes["configuration"]["id"] not in reached and beyond["id"] not in reached
    audited = graph.read(focus=nodes["lived"]["id"], hops=2, policy=policy(world, "audit"))
    assert {nodes["configuration"]["id"], beyond["id"]} <= {n["id"] for n in audited["nodes"]}
    # A read that starts on a hidden node returns nothing rather than its neighbourhood.
    assert graph.read(focus=nodes["configuration"]["id"], hops=2)["nodes"] == []
    hidden = {nodes[name]["id"] for name in SPECIMENS}
    assert {n["id"] for n in graph.read(query="lantern", hops=2)["nodes"]}.isdisjoint(hidden)
    assert hidden <= {n["id"] for n in graph.read(query="lantern", hops=2, policy=policy(world, "audit"))["nodes"]}
    assert all(e["subject"] not in hidden and e["object"] not in hidden
               for e in graph.read(focus=nodes["lived"]["id"], hops=2)["edges"])


def test_graph_read_freshness_cache_preserves_hidden_evidence_policy(world, monkeypatch):
    graph, ids = world["memory"].graph, world["ids"]
    nodes = project(world, ("lived", "configuration"))
    with world["engine"].db.connect(write=True) as conn:
        refs = graph.proof(conn, [ids["lived"]])
        for relation in ("related", "supports"):
            graph.link(conn, nodes["lived"]["id"], relation, nodes["configuration"]["id"], refs,
                       reason="Hidden evidence cache fixture")

    cached = graph.read(focus=nodes["lived"]["id"], hops=1)
    original_fresh = graph._fresh_value

    def uncached(conn, node, **_):
        return original_fresh(conn, node)

    monkeypatch.setattr(graph, "_fresh_value", uncached)
    expected = graph.read(focus=nodes["lived"]["id"], hops=1)
    assert cached == expected
    assert {node["id"] for node in cached["nodes"]} == {nodes["lived"]["id"]}
    assert cached["edges"] == []

    audited = graph.read(focus=nodes["lived"]["id"], hops=1, policy=policy(world, "audit"))
    assert nodes["configuration"]["id"] in {node["id"] for node in audited["nodes"]}
    assert len(audited["edges"]) == 2


def test_graph_item_text_fallback_never_carries_a_role_declaration(world):
    """The projection of a role declaration has no text of its own. Falling back to the record
    is what carried the whole persona text out under basis `explicit`."""
    nodes = project(world)
    contexts = world["contexts"]
    for name, kind in SPECIMENS.items():
        hidden = contexts.graph_item(nodes[name], policy=policy(world))
        assert hidden["text"] == nodes[name]["title"] and hidden["read_depth"] == "index"
        assert world["engine"].get(world["ids"][name])["content"] not in hidden["text"]
        shown = contexts.graph_item(nodes[name], policy=policy(world, "audit"))
        assert world["engine"].get(world["ids"][name])["content"] == shown["text"]
        assert shown["basis"] == shown["facts"]["basis"] == kind and shown["basis"] != "explicit"
    lived = contexts.graph_item(nodes["lived"], policy=policy(world))
    assert "lantern market" in lived["text"] and lived["basis"] == "explicit"


def test_graph_item_fallback_is_unchanged_with_the_switch_off(world):
    nodes = project(world)
    switch_off(world)
    item = world["contexts"].graph_item(nodes["configuration"], policy=policy(world))
    assert ROLE_TEXT in item["text"] and item["basis"] == "role_configuration"
    assert not policy(world).enabled


def test_event_thread_original_and_summary_exclude_non_experience(world):
    event = event_of(world)[0]
    contexts, ids = world["contexts"], world["ids"]
    original = contexts.event_thread(event["id"], detail="original", budget=4000)
    assert ids["lived"] in original["covered_ids"]
    for name in SPECIMENS:
        assert ids[name] not in original["covered_ids"] and ids[name] not in original["text"]
    assert ROLE_TEXT not in original["text"]
    audited = contexts.event_thread(event["id"], detail="original", budget=8000, recall_purpose="audit")
    assert {ids[name] for name in SPECIMENS} <= set(audited["covered_ids"])
    labelled = json.loads("[" + ",".join(audited["text"].splitlines()) + "]")
    for line in labelled:
        if line["id"] in {ids[name] for name in SPECIMENS}:
            assert line["basis"] == SPECIMENS[next(n for n in SPECIMENS if ids[n] == line["id"])]
    summary = contexts.event_thread(event["id"], detail="summary", budget=4000)
    assert ROLE_TEXT not in summary["text"] and ENVELOPE not in summary["text"]


def test_summary_snapshot_excludes_and_the_rebuild_archives_the_old_one(world):
    event, _ = event_of(world)
    lifecycle = EventLifecycle(world["mind"], world["memory"].graph)
    engine, ids = world["engine"], world["ids"]
    with engine.db.connect() as conn:
        snapshot = lifecycle.snapshot(conn, event["id"])
        audited = lifecycle.snapshot(conn, event["id"], policy=policy(world, "audit"))
    assert set(snapshot["records"]) == {ids["lived"]}
    assert snapshot["excluded"] == sorted(ids[name] for name in ("configuration", "example"))
    assert {ids[name] for name in SPECIMENS} <= set(audited["records"])
    assert audited["input_hash"] != snapshot["input_hash"]
    # A summary written before the policy existed: it carries the role declaration.
    stale = {"narrative": [{"text": ROLE_TEXT, "record_ids": [ids["configuration"]], "basis": "explicit"}],
             "summarized_at": world["mind"].clock(), "source_versions": {ids["configuration"]: 1}}
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_event_digests(scope,event_id,state,generation,revision,"
                     "input_hash,dirty_at,due_at,data) VALUES(?,?,'ready',1,4,'legacy-hash',?,0,?)",
                     (SCOPE.key(), event["id"], world["mind"].clock(), json.dumps(stale)))
    assert lifecycle.read(event["id"])["state"] == "dirty"
    provider = Summarizer()
    apply = lifecycle.prepare_digest(event["id"], provider=provider)
    with engine.db.connect(write=True) as conn:
        apply(conn)
    assert provider.seen == [{ids["lived"]}]
    rebuilt = lifecycle.read(event["id"])
    assert rebuilt["state"] == "ready" and ROLE_TEXT not in json.dumps(rebuilt["data"], ensure_ascii=False)
    with engine.db.connect() as conn:
        archived = conn.execute("SELECT kind,identifier,revision,rules_version,data FROM mind_isolation_archive").fetchall()
    assert [tuple(r)[:4] for r in archived] == [("event_digest", event["id"], 4, RULES_VERSION)]
    assert json.loads(archived[0]["data"]) == stale


def test_summary_signature_is_unchanged_where_the_policy_changes_nothing(world):
    """Only an event the policy actually trims is rebuilt; an untouched one keeps its hash."""
    event = event_of(world, names=("lived",))[0]
    lifecycle = EventLifecycle(world["mind"], world["memory"].graph)
    with world["engine"].db.connect() as conn:
        with_policy = lifecycle.snapshot(conn, event["id"])
    switch_off(world)
    with world["engine"].db.connect() as conn:
        without = lifecycle.snapshot(conn, event["id"])
    assert with_policy["excluded"] == [] and with_policy["input_hash"] == without["input_hash"]


def test_semantic_context_identity_evidence_and_topic_families_filter(world):
    nodes = event_of(world)[1]
    engine, ids = world["engine"], world["ids"]
    family = {"id": "family_synthetic", "scope": SCOPE.model_dump(), "kind": "family", "state": "candidate",
              "revision": 1, "title": "Lantern evenings", "members": sorted(ids.values()), "positions": {},
              "basis": "synthetic"}
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO families VALUES(?,?,?,?,?,?)",
                     (family["id"], SCOPE.key(), "family", "candidate", 1, json.dumps(family)))
    context = world["memory"].semantic_context(query="lantern")
    candidates = {n["id"] for n in context["graph_candidates"]}
    assert nodes["lived"]["id"] in candidates
    assert candidates.isdisjoint({nodes[name]["id"] for name in SPECIMENS})
    evidence = [e for n in context["graph_candidates"] for e in n.get("identity_evidence", [])]
    assert {e["id"] for e in evidence} <= {ids["lived"]}
    members = [m for f in context["topic_candidates"] for m in f["members"]]
    assert {m["id"] for m in members}.isdisjoint({ids[name] for name in SPECIMENS})
    assert ROLE_TEXT not in json.dumps(context, ensure_ascii=False, default=str)


def test_node_item_work_text_excludes_non_experience(world):
    """A work node repeats its members' text. It is a fallback of the same shape as the graph one."""
    node = {"id": "work_synthetic", "kind": "work", "revision": 1, "title": "Lantern report",
            "record_ids": [world["ids"]["lived"], world["ids"]["configuration"]], "state": "done"}
    hidden = world["contexts"].node_item(node, policy=policy(world))
    assert "lantern market" in hidden["text"] and ROLE_TEXT not in hidden["text"]
    assert ROLE_TEXT in world["contexts"].node_item(node, policy=policy(world, "audit"))["text"]


def test_caches_written_before_the_classification_existed_are_misses(world):
    """An overview and a packed summary made while the store had no classification rows still
    name their inputs, so the item invalidates itself instead of replaying its text."""
    contexts, ids = world["contexts"], world["ids"]
    item = {"id": ids["configuration"], "revision": 1, "text": ROLE_TEXT, "basis": "explicit",
            "dependencies": [{"id": ids["configuration"], "revision": 1}]}
    with world["engine"].db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_context_cache VALUES(?,?,?,?)",
                     (contexts._overview_key(item), SCOPE.key(), json.dumps(
                         {"text": "A warm lantern voice.", "source": item, "coverage": "overview"}), world["mind"].clock()))
    assert contexts._overview(item, None).get("cached_summary")
    assert not contexts._overview(item, policy(world)).get("cached_summary")
    assert contexts._overview(item, policy(world, "audit")).get("cached_summary")
    packed = contexts.pack([item], "lantern", 4000, allow_model=False, policy=policy(world))
    assert packed["text"] == "" and packed["omitted_ids"] == [ids["configuration"]]
    assert ROLE_TEXT in contexts.pack([item], "lantern", 4000, allow_model=False)["text"]


def test_window_receipt_is_not_replayed_once_its_evidence_is_classified(world):
    contexts, ids = world["contexts"], world["ids"]
    stale = {"text": ROLE_TEXT, "tokens": 40, "rendered_text": ROLE_TEXT, "covered_ids": [ids["configuration"]],
             "index": [{"id": ids["configuration"], "revision": 1, "depth": "original"}]}
    with world["engine"].db.connect(write=True) as conn:
        contexts._save_window(conn, "synthetic-session", {"epoch": "initial", "used": 40, "seen": {},
                                                          "receipts": {"event-1": stale}})
    assert not contexts._receipt_current(stale, policy(world))
    assert contexts._receipt_current(stale, policy(world, "audit"))
    replayed = contexts.build(query="lantern", purpose="chat", session="synthetic-session", event_id="event-1")
    assert replayed is not stale and ROLE_TEXT not in replayed.get("rendered_text", "")
    with world["engine"].db.connect() as conn:
        window = contexts.window("synthetic-session", conn)
    # The replaced receipt gave its budget back: one event was injected once.
    assert window["used"] == replayed["tokens"] and window["receipts"]["event-1"]["tokens"] == replayed["tokens"]


def test_continuity_manifest_and_build_exclude_a_configuration_projection(world):
    nodes = event_of(world)[1]
    manifest = ContinuityManifest(world["mind"], contexts=world["contexts"])
    selected = manifest.select("lantern")
    assert {i["id"] for i in selected["items"]}.isdisjoint({nodes[name]["id"] for name in SPECIMENS})
    assert ROLE_TEXT not in json.dumps(selected, ensure_ascii=False)
    built = world["contexts"].build(query="lantern", purpose="chat", session="", budget=4000)
    assert ROLE_TEXT not in built["text"] and ENVELOPE not in built["text"]
    assert {i["id"] for i in built["index"]}.isdisjoint(
        {world["ids"][name] for name in SPECIMENS} | {nodes[name]["id"] for name in SPECIMENS})
    audited = world["contexts"].build(query="lantern", purpose="read", session="", budget=8000,
                                      recall_purpose="audit", history=True)
    assert ROLE_TEXT in audited["text"]


def test_strict_mode_treats_caches_summaries_and_receipts_as_misses(world):
    event = event_of(world)[0]
    contexts, ids = world["contexts"], world["ids"]
    lifecycle = EventLifecycle(world["mind"], world["memory"].graph)
    provider = Summarizer()
    apply = lifecycle.prepare_digest(event["id"], provider=provider)
    with world["engine"].db.connect(write=True) as conn:
        apply(conn)
    assert lifecycle.read(event["id"])["state"] == "ready"
    item = {"id": ids["lived"], "revision": 1, "text": "We walked through the lantern market together.",
            "basis": "explicit", "dependencies": [{"id": ids["lived"], "revision": 1}]}
    with world["engine"].db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_context_cache VALUES(?,?,?,?)",
                     (contexts._overview_key(item), SCOPE.key(), json.dumps(
                         {"text": "A lantern walk.", "source": item, "coverage": "overview"}), world["mind"].clock()))
    receipt = {"text": "", "tokens": 12, "covered_ids": [ids["lived"]],
               "index": [{"id": ids["lived"], "revision": 1, "depth": "original"}]}
    assert contexts._overview(item, policy(world)).get("cached_summary")
    assert contexts._receipt_current(receipt, policy(world))
    start_migration(world)
    strict = policy(world)
    assert strict.strict
    assert not contexts._overview(item, strict).get("cached_summary")
    assert not contexts._receipt_current(receipt, strict)
    with world["engine"].db.connect() as conn:
        node = world["memory"].graph.get(conn, event["id"])
    assert contexts.graph_item(node, policy=strict)["facts"]["digest_state"] == "dirty"
    thread = contexts.event_thread(event["id"], detail="summary", budget=4000)
    assert thread["summary_state"] == "dirty" and thread["stale_summary_revision"] is None
    start_migration(world, "complete")
    assert not policy(world).strict
    assert contexts.graph_item(node, policy=policy(world))["facts"]["digest_state"] == "ready"


@pytest.mark.asyncio
async def test_memory_history_and_read_revisions_show_the_class(world):
    from fastapi.testclient import TestClient

    from eventmem.core.api import create_app
    from eventmem.core.mcp import create_mcp
    from eventmem.core.models import RevisionInput

    engine, ids = world["engine"], world["ids"]
    for name in ("lived", "configuration"):
        engine.revise(ids[name], RevisionInput(command_id="amend-" + name, expected_revision=1, action="correct",
                                               content="Amended " + name, reason="Synthetic correction"))
    rows = engine.history(ids["configuration"])
    assert rows and all(r["data"]["confirmation"] == r["data"]["evidence_class"] == "role_configuration" for r in rows)
    lived = engine.history(ids["lived"])
    assert [r["data"]["confirmation"] for r in lived] == ["verified", "explicit"]
    assert all("evidence_class" not in r["data"] for r in lived)
    server = create_mcp(engine)
    shown = json.loads((await server.call_tool("memory_history", {"record_id": ids["configuration"]}))[0].text)
    assert [i["data"]["confirmation"] for i in shown["items"]] == ["role_configuration"] * 2
    with TestClient(create_app(engine=engine, token="test-credential", workers=False)) as client:
        body = client.get("/v1/memories/" + ids["configuration"] + "/revisions",
                          headers={"Authorization": "Bearer test-credential"}).json()
        assert [i["data"]["confirmation"] for i in body["items"]] == ["role_configuration"] * 2
        assert [i["data"]["evidence_class"] for i in body["items"]] == ["role_configuration"] * 2
    switch_off(world)
    # Before: the stored confirmation of an installed role text was shown as the owner's own word.
    stored = engine.history(ids["configuration"])
    assert [r["data"]["confirmation"] for r in stored] == ["verified", "observed"]
    assert all("evidence_class" not in r["data"] for r in stored)


def test_prepare_communities_clusters_experience_only(world):
    pytest.importorskip("igraph")
    from eventmem.core.organize import prepare_communities

    engine, ids = world["engine"], world["ids"]
    other = engine.receive(SourceInput(namespace="chat", key="second-walk", title="second-walk",
        text="We returned to the lantern market the next week.", scope=SCOPE, authority="explicit",
        occurred_at=world["clock"][0].isoformat(), metadata={"role": "user", "host_event": "message"}))
    partner = root(engine, other)
    for name in ("configuration", "example", "envelope", "lived"):
        engine.relate(partner, "coexists", ids[name])
    with engine.db.connect(write=True) as conn:
        conn.executemany("INSERT OR REPLACE INTO dirty VALUES(?,?)", [(rid, 1) for rid in (*ids.values(), partner)])
    apply = prepare_communities(engine, SCOPE)
    with engine.db.connect(write=True) as conn:
        apply(conn)
        members = {r[0] for r in conn.execute("SELECT record_id FROM members")}
    assert members and members.isdisjoint({ids[name] for name in SPECIMENS})


def test_bm25_accumulates_terms_in_an_order_the_hash_seed_cannot_change():
    """Floating-point addition is not associative, so the order the query terms are accumulated
    in decides the last bits of every score. Iterating a set made that order a property of the
    interpreter's hash seed. Each term here has its own document frequency, so the recorded
    inverse-frequency arguments say exactly which order the loop took."""
    program = textwrap.dedent("""
        import json, math, types
        import eventmem.recall as recall
        seen, original = [], math.log
        recall.math = types.SimpleNamespace(log=lambda x: seen.append(round(x, 6)) or original(x))
        terms = ["t%02d" % j for j in range(11)]
        recall._bm25([terms[:i] for i in range(1, 12)], terms)
        print(json.dumps(seen))
    """)
    orders = []
    for seed in ("1", "1000"):
        found = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                               env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
                                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}, check=True)
        orders.append(json.loads(found.stdout))
    assert orders[0] == orders[1] and len(orders[0]) == 11
    # Sorted terms run from the most frequent to the least, so the argument only rises.
    assert orders[0] == sorted(orders[0])


def test_switch_off_leaves_every_derived_view_as_it_was(world):
    event, nodes = event_of(world)
    switch_off(world)
    graph, ids, contexts = world["memory"].graph, world["ids"], world["contexts"]
    with world["engine"].db.connect() as conn:
        found = {n["id"] for n in graph.candidates(conn, "lantern")}
        snapshot = EventLifecycle(world["mind"], graph).snapshot(conn, event["id"])
    assert {nodes[name]["id"] for name in SPECIMENS} <= found
    # The prefix rule the policy replaced still keeps host envelopes out of a summary.
    assert set(snapshot["records"]) == {ids["lived"], ids["configuration"], ids["example"]}
    assert snapshot["excluded"] == []
    original = contexts.event_thread(event["id"], detail="original", budget=8000)
    assert ROLE_TEXT in original["text"]
    assert ROLE_TEXT in json.dumps(world["memory"].semantic_context(query="lantern"), ensure_ascii=False, default=str)
