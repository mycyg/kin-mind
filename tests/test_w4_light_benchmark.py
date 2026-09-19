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
