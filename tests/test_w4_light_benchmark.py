"""W4 light-path benchmark harness: fixture determinism, fixed query set,
percentile convention, and the no-model guard. Runs on a tiny scaled fixture."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture_mod = _load("w4_light_fixture")
bench_mod = _load("w4_light_bench")

ANCHOR = fixture_mod.datetime(2026, 9, 17, tzinfo=fixture_mod.timezone.utc)


@pytest.fixture(scope="module")
def fixture_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("w4-fixture") / "store"
    manifest = fixture_mod.build(root, seed=20260919, anchor=ANCHOR, scale=0.03)
    fixture_mod.build_queries(root, seed=20260919, anchor=ANCHOR, manifest=manifest)
    return root


def test_fixture_counts_and_determinism(fixture_root, tmp_path):
    manifest = json.loads((fixture_root / "w4-fixture-manifest.json").read_text())
    counts = manifest["counts"]
    assert counts["sources"] > 90 and counts["records"] > 250
    assert counts["graph_nodes"] > 300
    assert counts["temperature_rows"] == counts["records"]
    # Same seed and anchor reproduce the same record content (received_at is
    # wall-clock by design and excluded).
    other = tmp_path / "again"
    fixture_mod.build(other, seed=20260919, anchor=ANCHOR, scale=0.03)
    def sample(root):
        with sqlite3.connect(root / "memory.sqlite3") as conn:
            return conn.execute(
                "SELECT id, kind, valid_from, json_extract(data,'$.content') FROM records ORDER BY id LIMIT 25"
            ).fetchall()
    assert sample(fixture_root) == sample(other)


def test_query_set_fixed_shaped_and_deterministic(fixture_root, tmp_path):
    queries = json.loads((fixture_root / "w4-queries.json").read_text())
    assert len(queries) == 50 and len({q["id"] for q in queries}) == 50
    shapes = {q["shape"] for q in queries}
    assert {"keyword", "preference", "owner_words", "date", "correction", "commitment",
            "work_share", "id_lookup", "empty", "gibberish", "long_verbose"} <= shapes
    manifest = json.loads((fixture_root / "w4-fixture-manifest.json").read_text())
    again = fixture_mod.build_queries(tmp_path, seed=20260919, anchor=ANCHOR, manifest=manifest)
    assert again == queries


def test_light_path_uses_no_model(fixture_root):
    from eventmem.core import Engine
    from eventmem.core.models import Scope
    from kin_mind.context import Contexts
    from kin_mind.state import Mind

    scope = Scope.model_validate(json.loads((fixture_root / "w4-fixture-manifest.json").read_text())["scope"])
    contexts = Contexts(Mind(Engine(fixture_root), scope))
    guard = bench_mod.install_no_model_guard()
    queries = json.loads((fixture_root / "w4-queries.json").read_text())
    try:
        for case in queries[:12]:
            result = contexts.build(case["query"], purpose="chat",
                                    session=f"t-{case['id']}", mode="auto", allow_model=False)
            assert result["mode_used"] == "light"
            assert result["model_requests"] == 0
            assert result["tokens"] <= 2000  # startup budget bound
    finally:
        bench_mod.restore_no_model_guard()
    assert not guard, dict(guard)


def test_percentile_nearest_rank_matches_frozen_sim():
    assert bench_mod.percentile(list(range(1, 101)), 0.95) == 95
    assert bench_mod.percentile(list(range(1, 101)), 0.50) == 50
    assert bench_mod.percentile(list(range(1, 101)), 0.99) == 99
    assert bench_mod.percentile([7], 0.99) == 7
    assert bench_mod.percentile([], 0.95) is None
    dist = bench_mod.distribution([10, 20, 30])
    assert dist["n"] == 3 and dist["p50"] == 20 and dist["max"] == 30


def test_output_equivalence_uses_fixture_time_and_preserves_existing_paths(fixture_root, tmp_path, monkeypatch):
    queries = bench_mod.load_queries(fixture_root)[:2]
    monkeypatch.setattr(bench_mod, "load_queries", lambda root: queries)
    collect = bench_mod.run_turn_collect
    observed = []

    def checked_collect(contexts, query):
        observed.append(contexts.mind.clock())
        return collect(contexts, query)

    monkeypatch.setattr(bench_mod, "run_turn_collect", checked_collect)
    first, second = tmp_path / "before.json", tmp_path / "after.json"
    existing = tmp_path / "before-store"
    existing.mkdir()
    (existing / "keep.txt").write_text("unrelated caller data")
    bench_mod.dump_outputs(fixture_root, first)
    bench_mod.dump_outputs(fixture_root, second)
    assert first.read_bytes() == second.read_bytes()
    assert observed == ["2026-09-17T12:00:00.000000+00:00"] * 4
    assert (existing / "keep.txt").read_text() == "unrelated caller data"
    assert not bench_mod._GUARD_ORIGINALS


def test_graph_item_compact_edge_query_matches_or_form(fixture_root):
    """The indexed UNION read returns exactly the pre-optimization OR query's rows,
    including the self-loop edge (deduped) and the LIMIT 8 ordering by id."""
    import random

    from eventmem.core.models import Scope

    scope = Scope.model_validate(json.loads((fixture_root / "w4-fixture-manifest.json").read_text())["scope"])
    or_form = ("SELECT data FROM mind_graph_edges WHERE scope=? AND state='active' AND (subject=? OR object=?) "
               "ORDER BY id LIMIT 8")
    union_form = ("SELECT data FROM ("
                  "SELECT id, data FROM mind_graph_edges INDEXED BY mind_graph_left WHERE scope=? AND subject=? AND state='active' "
                  "UNION "
                  "SELECT id, data FROM mind_graph_edges INDEXED BY mind_graph_right WHERE scope=? AND object=? AND state='active') "
                  "ORDER BY id LIMIT 8")
    rng = random.Random(7)
    with sqlite3.connect(fixture_root / "memory.sqlite3") as conn:
        node_ids = [r[0] for r in conn.execute("SELECT id FROM mind_graph_nodes LIMIT 200")]
        sample = rng.sample(node_ids, 40)
        for nid in sample:
            expected = conn.execute(or_form, (scope.key(), nid, nid)).fetchall()
            got = conn.execute(union_form, (scope.key(), nid, scope.key(), nid)).fetchall()
            assert [r[0] for r in got] == [r[0] for r in expected], nid


def test_query_graph_expansion_batches_each_frontier_once(fixture_root, monkeypatch):
    from eventmem.core import Engine
    from eventmem.core.models import Scope
    from kin_mind.graph import EventGraph
    from kin_mind.state import Mind

    graph = EventGraph(Mind(Engine(fixture_root), Scope.model_validate(bench_mod.SCOPE_DICT)))
    with graph.engine.db.connect() as conn:
        anchor_id = conn.execute(
            "SELECT e.subject FROM mind_graph_edges e JOIN mind_graph_nodes n ON n.id=e.subject "
            "WHERE e.scope=? GROUP BY e.subject HAVING COUNT(DISTINCT e.object)>2 ORDER BY e.subject LIMIT 1",
            (graph.scope.key(),),
        ).fetchone()[0]
        anchor = graph.get(conn, anchor_id)
    monkeypatch.setattr(graph, "candidates", lambda conn, query, **kwargs: [anchor])
    original, batches = graph._get_many, []

    def counted(conn, identifiers):
        batches.append(list(identifiers))
        return original(conn, identifiers)

    monkeypatch.setattr(graph, "_get_many", counted)
    result = graph.read(query="synthetic frontier", hops=1)
    assert len(batches) == 1
    assert len(batches[0]) > 1
    assert {node["id"] for node in result["nodes"]} == {anchor_id, *batches[0]}


def test_graph_item_compact_edge_query_self_loop_once(tmp_path):
    from eventmem.core import Engine
    from eventmem.core.db import digest
    from eventmem.core.models import Scope, SourceInput
    from kin_mind.graph import EventGraph
    from kin_mind.state import Mind

    scope = Scope(persona="synthetic-w4-edges")
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, scope, clock=lambda: "2026-09-17T12:00:00.000000+00:00")
    sid = engine.receive(SourceInput(namespace="synthetic", key="edge-source", scope=scope,
                                     text="edge source", authority="explicit",
                                     occurred_at=mind.clock()))["id"]
    graph = EventGraph(mind)
    with engine.db.connect(write=True) as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        rid = "mem_" + digest([sid, "root"])[:32]
        record = engine._get(conn, rid)
        data = json.loads(row["data"])
        ref = {"source_id": sid, "hash": row["hash"], "record_id": rid, "revision": record["revision"],
               "authority": data["authority"], "session": row["session"], "occurred_at": row["occurred_at"],
               "received_at": row["received_at"], "namespace": row["namespace"],
               "source_key": row["source_key"], "metadata": {}}
        graph._put(conn, {"id": "node-a", "kind": "knowledge", "title": "a", "text": "a",
                          "evidence": [ref], "source_ids": [sid], "occurred_at": mind.clock()})
        for i in range(10):
            # i == 0 is the self-loop: present once in both query forms.
            object_ = "node-a" if i == 0 else f"node-{i}"
            graph._put(conn, {"id": f"edge-{i:02d}", "kind": "edge", "subject": "node-a", "object": object_,
                              "predicate": "related", "layer": "evidence", "basis": "observed",
                              "confidence": 1, "reason": "", "source_ids": [sid], "evidence": [ref],
                              "state": "active"}, edge=True)
    with engine.db.connect() as conn:
        union_rows = conn.execute(
            "SELECT data FROM ("
            "SELECT id, data FROM mind_graph_edges INDEXED BY mind_graph_left WHERE scope=? AND subject=? AND state='active' "
            "UNION "
            "SELECT id, data FROM mind_graph_edges INDEXED BY mind_graph_right WHERE scope=? AND object=? AND state='active') "
            "ORDER BY id LIMIT 8", (scope.key(), "node-a", scope.key(), "node-a")).fetchall()
        or_rows = conn.execute(
            "SELECT data FROM mind_graph_edges WHERE scope=? AND state='active' AND (subject=? OR object=?) "
            "ORDER BY id LIMIT 8", (scope.key(), "node-a", "node-a")).fetchall()
    ids = [json.loads(r[0])["id"] for r in union_rows]
    assert ids == [json.loads(r[0])["id"] for r in or_rows]
    assert ids == [f"edge-{i:02d}" for i in range(8)]  # self-loop once, ordered, capped
