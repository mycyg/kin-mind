from __future__ import annotations

import json
import os
import platform
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .db import dumps, tokenize
from .engine import Engine
from .models import RecallRequest, RecordInput, RevisionInput, Scope, SourceInput, now


def percentile(values, q):
    ordered = sorted(values)
    return (
        ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))] if ordered else None
    )


def distribution(values):
    return {
        "samples": len(values),
        "p50_ms": percentile(values, 0.5),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
    }


def fixture_cases():
    return [
        {
            "scenario": "tool",
            "query": "port allocation",
            "sources": [
                {"text": "port allocation uses range A", "kind": "procedure"},
                {"text": "port allocation uses range B", "kind": "procedure"},
            ],
            "replace": [0, 1],
            "expected": [1],
        },
        {
            "scenario": "tool",
            "query": "deployment rollback",
            "sources": [
                {
                    "text": "deployment rollback requires the previous verified artifact",
                    "kind": "procedure",
                },
                {
                    "text": "deployment rollback forbidden in roleplay",
                    "kind": "procedure",
                    "scope": {"world": "roleplay"},
                },
            ],
            "expected": [0],
        },
        {
            "scenario": "companion",
            "query": "tea preference",
            "sources": [
                {"text": "tea preference: jasmine", "kind": "preference"},
                {"text": "tea preference: oolong", "kind": "preference"},
            ],
            "replace": [0, 1],
            "expected": [1],
        },
        {
            "scenario": "companion",
            "query": "concert promise",
            "sources": [{"text": "concert promise for Saturday", "kind": "commitment"}],
            "retract": [0],
            "expected": [],
        },
        {
            "scenario": "companion",
            "query": "shared garden experience",
            "sources": [
                {
                    "text": "shared garden experience on Sunday; the user said they enjoyed it",
                    "kind": "episode",
                }
            ],
            "expected": [0],
        },
        {
            "scenario": "knowledge",
            "query": "enzyme source",
            "sources": [
                {"text": "enzyme source A reports 17 units", "kind": "knowledge"},
                {"text": "enzyme source B reports 21 units", "kind": "knowledge"},
            ],
            "expected": [0, 1],
        },
        {
            "scenario": "knowledge",
            "query": "service specification",
            "sources": [
                {
                    "text": "service specification version 1 uses 30 seconds",
                    "kind": "knowledge",
                },
                {
                    "text": "service specification version 2 uses 60 seconds",
                    "kind": "knowledge",
                },
            ],
            "replace": [0, 1],
            "expected": [1],
        },
        {
            "scenario": "knowledge",
            "query": "answer never supplied",
            "sources": [
                {"text": "The document does not state the answer", "kind": "knowledge"}
            ],
            "expected": [],
        },
    ]


def evaluate(output, dataset=None, answer_engine=None):
    """Deterministic retrieval replay. External answer-model evaluation is separate.

    Each fixture is applied sequentially at a recorded cutoff. No future source or
    revision participates. Baselines use the same scoped snapshot and 2000 tokens.
    The vector baseline uses a disclosed local hashed-token embedding, not a claim
    about a remote production model's quality.
    """
    import numpy as np
    from eventmem.recall import _bm25
    from .retrieval import tokens
    from .baselines import LegacyAdapter, LEGACY_REVISION

    legacy = LegacyAdapter()
    cases = json.loads(Path(dataset).read_text()) if dataset else fixture_cases()
    report = {
        "evaluation": "deterministic retrieval replay",
        "model": "configured answer and judge roles"
        if answer_engine
        else "none; no generated answers",
        "budget": 2000,
        "legacy_revision": LEGACY_REVISION,
        "summary_baseline": "extractive summary: first sentence of the 10 most recent scoped records",
        "vector_model": "hashed-token-256 (test baseline)",
        "scenarios": {},
        "cases": [],
        "scarlett": {
            "status": "diagram coverage only; runnable adapter unavailable",
            "measurements": None,
        },
        "answer_quality": {
            "task_success": None,
            "unsupported_answer_rate": None,
            "reason": "Requires a configured answer model and task-specific judge; retrieval metrics do not establish answer quality.",
        },
    }
    partial = Path(str(output) + ".partial.json")
    from .db import digest
    from eventmem.paths import atomic_write

    fingerprint = digest(
        {
            "cases": cases,
            "models": answer_engine.settings("models") if answer_engine else {},
            "budget": 2000,
            "legacy": LEGACY_REVISION,
            "replay_schema": 1,
        }
    )
    resume = {}
    if answer_engine and partial.exists():
        checkpoint = json.loads(partial.read_text())
        if checkpoint.get("run_fingerprint") != fingerprint:
            raise ValueError(
                "Replay checkpoint belongs to a different dataset or model configuration; use a new output path"
            )
        resume = checkpoint["cells"]

    def vector(text):
        import hashlib

        v = np.zeros(256)
        for word in tokenize(text).split():
            v[int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % 256] += 1
        return v / (np.linalg.norm(v) or 1)

    for number, case in enumerate(cases):
        with tempfile.TemporaryDirectory(prefix="memorypalace-replay-") as temp:
            engine = Engine(temp)
            ids = []
            for i, source in enumerate(case["sources"]):
                s = engine.receive(
                    SourceInput(namespace="replay", key=str(i), **source)
                )
                ids.append(engine.source(s["id"])["record_ids"][0])
            if case.get("replace"):
                old, new = case["replace"]
                engine.revise(
                    ids[old],
                    RevisionInput(
                        expected_revision=1,
                        command_id="replace",
                        action="replace",
                        replacement_id=ids[new],
                    ),
                )
            for i in case.get("retract", []):
                engine.revise(
                    ids[i],
                    RevisionInput(
                        expected_revision=1, command_id=f"retract{i}", action="retract"
                    ),
                )
            cutoff = now()
            # Future knowledge is deliberately present in storage during replay.
            engine.receive(
                SourceInput(
                    namespace="future",
                    key="future",
                    text=case["query"] + " secret future answer",
                    occurred_at="2099-01-01T00:00:00Z",
                )
            )
            records = [engine.get(rid, known_at=cutoff) for rid in ids]
            scoped = [r for r in records if r["scope"] == Scope().model_dump()]
            words = [tokenize(r["content"]).split() for r in scoped]
            bm25 = _bm25(words, tokenize(case["query"]).split())
            ranks = {
                "none": [],
                "recent_summary": [r["id"] for r in list(reversed(scoped))[:10]],
                "bm25": [
                    r["id"]
                    for score, r in sorted(zip(bm25, scoped), key=lambda p: -p[0])
                    if score > 0
                ],
                "vector": [
                    r["id"]
                    for r in sorted(
                        scoped,
                        key=lambda r: (
                            -float(vector(case["query"]) @ vector(r["content"]))
                        ),
                    )
                ],
                "legacy_memorypalace": legacy.recall(
                    query=case["query"], records=scoped, budget=2000, cutoff=cutoff
                ),
                "memorypalace_1": [
                    r["id"]
                    for r in engine.recall(
                        RecallRequest(
                            query=case["query"],
                            scenario=case["scenario"],
                            known_at=cutoff,
                            at=cutoff,
                            budget=2000,
                        )
                    )["items"]
                ],
            }
            expected = {ids[i] for i in case["expected"]}
            result = {
                "case": number,
                "scenario": case["scenario"],
                "cutoff": cutoff,
                "baselines": {},
            }
            for name, selected in ranks.items():
                resume_key = str(number) + ":" + name
                if answer_engine and resume_key in resume:
                    result["baselines"][name] = resume[resume_key]
                    continue
                used = 0
                bounded = []
                context = []
                for rid in selected:
                    content = engine.get(rid, known_at=cutoff)["content"]
                    if name == "recent_summary":
                        content = content.split(". ")[0]
                    line = f"[{rid}] {content}"
                    n = tokens(line)
                    if used + n <= 2000:
                        bounded.append(rid)
                        used += n
                        context.append(line)
                hits = len(set(bounded) & expected)
                stale = sum(engine.get(r)["status"] != "active" for r in bounded)
                metric = {
                    "recall": hits / len(expected) if expected else float(not bounded),
                    "stale_results": stale,
                    "unexpected_results": len(set(bounded) - expected),
                    "tokens": used,
                    "scope_leaks": sum(
                        engine.get(r)["scope"] != Scope().model_dump() for r in bounded
                    ),
                }
                if answer_engine:
                    from .providers import Providers

                    provider = Providers(answer_engine)
                    start = time.perf_counter()
                    answer = provider.json(
                        "answer",
                        'Answer the question using only the supplied memory. If evidence is missing say so. Return {"answer":"...","evidence_ids":[id,...],"abstained":boolean}.',
                        {"question": case["query"], "memory": "\n".join(context)},
                    )
                    elapsed = (time.perf_counter() - start) * 1000
                    judge = provider.json(
                        "judge",
                        'Assess the answer against authoritative expected evidence. Return {"task_success":boolean,"unsupported_answer":boolean,"stale_misuse":boolean}. Missing expected evidence requires an explicit abstention. Source disagreement must be retained.',
                        {
                            "question": case["query"],
                            "answer": answer,
                            "expected": [
                                engine.get(r, known_at=cutoff)["content"]
                                for r in sorted(expected)
                            ],
                            "obsolete": [
                                r["content"] for r in scoped if r["status"] != "active"
                            ],
                        },
                    )
                    metric.update(
                        answer=answer,
                        task_success=judge.get("task_success") is True,
                        unsupported_answer=judge.get("unsupported_answer") is True,
                        stale_misuse=judge.get("stale_misuse") is True,
                        answer_ms=elapsed,
                    )
                result["baselines"][name] = metric
                if answer_engine:
                    resume[resume_key] = metric
                    partial.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(
                        partial,
                        json.dumps(
                            {"run_fingerprint": fingerprint, "cells": resume},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    print(
                        f"Completed replay case {number + 1}/{len(cases)}: {name}",
                        flush=True,
                    )
            report["cases"].append(result)
    for scenario in {c["scenario"] for c in cases}:
        group = [c for c in report["cases"] if c["scenario"] == scenario]
        report["scenarios"][scenario] = {
            name: {
                "mean_recall": statistics.mean(
                    c["baselines"][name]["recall"] for c in group
                ),
                "stale_results": sum(
                    c["baselines"][name]["stale_results"] for c in group
                ),
                "scope_leaks": sum(c["baselines"][name]["scope_leaks"] for c in group),
            }
            for name in ranks
        }
    legacy.close()
    if answer_engine:
        report["answer_quality"] = {
            "method": "configured model judge; eight synthetic regression cases, not a general quality benchmark",
            "scenarios": {},
        }
        for scenario in report["scenarios"]:
            rows = [c for c in report["cases"] if c["scenario"] == scenario]
            report["answer_quality"]["scenarios"][scenario] = {
                name: {
                    key: statistics.mean(c["baselines"][name][key] for c in rows)
                    for key in ["task_success", "unsupported_answer", "stale_misuse"]
                }
                for name in ranks
            }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def benchmark(root, output, scale="smoke"):
    import numpy as np
    import pyarrow as pa
    import psutil
    from .vectors import VectorIndex
    from .retrieval import tokens

    root, output = Path(root), Path(output)
    if root.exists() and (root / "memory.sqlite3").exists():
        raise ValueError("Benchmark requires a fresh isolated root")
    count = 1_000_000 if scale == "full" else 5000
    memories = 100_000 if scale == "full" else 1000
    dimension = 1024
    engine = Engine(root)
    process = psutil.Process()
    peak = [process.memory_info().rss]
    stop = threading.Event()

    def sample():
        while not stop.wait(0.1):
            peak[0] = max(peak[0], process.memory_info().rss)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    started = time.perf_counter()
    rng = np.random.default_rng(60260908)
    centers = rng.normal(size=(256, dimension)).astype("float32")
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    index_id = VectorIndex.register(
        engine, "synthetic-mixture-1024", dimension, "benchmark-v1"
    )
    index = VectorIndex(engine, index_id)
    table = index.table()
    batch_size = 2048
    stamp = now()
    scope = Scope().key()
    print(f"Generating {memories} memories and {count} knowledge vectors", flush=True)
    for offset in range(0, count + memories, batch_size):
        size = min(batch_size, count + memories - offset)
        rows = []
        fts = []
        revs = []
        vector_rows = []
        for i in range(offset, offset + size):
            knowledge = i >= memories
            rid = f"bench_{i:09d}"
            topic = i % 256
            record = RecordInput(
                id=rid,
                kind="knowledge" if knowledge else "episode",
                title=f"Topic {topic} record {i}",
                content=f"topic_{topic} build deployment configuration reference item_{i} port allocation validation result {i % 97}",
            )
            data = record.model_dump() | {
                "id": rid,
                "revision": 1,
                "received_at": stamp,
                "updated_at": stamp,
                "read_url": f"/v1/memories/{rid}",
                "instruction_authority": "data",
            }
            raw = dumps(data)
            rows.append(
                (
                    rid,
                    record.kind,
                    scope,
                    "active",
                    stamp,
                    None,
                    stamp,
                    stamp,
                    1,
                    0.5,
                    None,
                    raw,
                )
            )
            fts.append((rid, tokenize(record.title + " " + record.content)))
            revs.append((rid, 1, stamp, "create", "synthetic", raw))
            if knowledge:
                vector_rows.append((rid, topic))
        with engine.db.connect(write=True) as conn:
            conn.executemany(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)", rows
            )
            conn.executemany("INSERT INTO search VALUES(?,?)", fts)
            conn.executemany("INSERT INTO revisions VALUES(?,?,?,?,?,?)", revs)
        if vector_rows:
            matrix = centers[[r[1] for r in vector_rows]] + rng.normal(
                0, 0.012, size=(len(vector_rows), dimension)
            ).astype("float32")
            matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
            arrow = pa.Table.from_arrays(
                [
                    pa.array([r[0] for r in vector_rows]),
                    pa.array([scope] * len(vector_rows)),
                    pa.array([1] * len(vector_rows), type=pa.int64()),
                    pa.FixedSizeListArray.from_arrays(
                        pa.array(matrix.reshape(-1)), dimension
                    ),
                ],
                names=["id", "scope", "revision", "vector"],
            )
            table.add(arrow)
        if offset % 102400 < batch_size:
            print(f"Loaded {offset + size}/{count + memories}", flush=True)
    fixture_seconds = time.perf_counter() - started
    print("Building IVF_HNSW_SQ", flush=True)
    build_start = time.perf_counter()
    index.build()
    build_seconds = time.perf_counter() - build_start
    queries = centers[rng.integers(0, 256, 100)] + rng.normal(
        0, 0.012, size=(100, dimension)
    ).astype("float32")
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    cold = []
    warm = []
    ann_recalls = []
    for i, vector in enumerate(queries):
        start = time.perf_counter()
        hits = index.search(vector.tolist(), scopes=[scope], limit=20)
        elapsed = (time.perf_counter() - start) * 1000
        cold.append(elapsed)
        exact = index.search(vector.tolist(), scopes=[scope], limit=20, exact=True)
        expected = {r["id"] for r in exact}
        ann_recalls.append(len(expected & {r["id"] for r in hits}) / 20)
        start = time.perf_counter()
        index.search(vector.tolist(), scopes=[scope], limit=20)
        warm.append((time.perf_counter() - start) * 1000)
        if i % 20 == 0:
            print(
                f"ANN queries {i + 1}/100; recall={statistics.mean(ann_recalls):.3f}",
                flush=True,
            )
    local = []
    assembly = []
    cold_start = time.perf_counter()
    engine.recall(RecallRequest(query="item_1", budget=2000))
    first_ms = (time.perf_counter() - cold_start) * 1000
    for i in range(300):
        query = RecallRequest(query=f"item_{i % 100}", budget=2000)
        start = time.perf_counter()
        result = engine.recall(query)
        local.append((time.perf_counter() - start) * 1000)
        assert tokens(result["text"]) <= 2000
        assembly.append(result["latency_ms"])
    acknowledged = []

    def receive(i):
        s = engine.receive(
            SourceInput(
                namespace="benchmark-ingest",
                key=str(i),
                text=f"Concurrent import operation {i}",
            )
        )
        acknowledged.append(s["id"])
        return s

    ingest_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(receive, list(range(200)) * 2))
    ingest_seconds = time.perf_counter() - ingest_start
    background = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(lambda: [receive(i) for i in range(200, 1200)])
        for i in range(100):
            start = time.perf_counter()
            engine.recall(RecallRequest(query=f"item_{i}"))
            background.append((time.perf_counter() - start) * 1000)
        future.result()
    stop.set()
    monitor.join()
    report = {
        "scale": scale,
        "hardware": {
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "physical_memory_gib": round(psutil.virtual_memory().total / 2**30, 2),
            "python": platform.python_version(),
        },
        "fixture": {
            "memories": memories,
            "knowledge_vectors": count,
            "dimensions": dimension,
            "distribution": "256-component Gaussian mixture; sigma .012; independent held-out queries; seed 60260908",
            "bulk_fixture_seconds": fixture_seconds,
        },
        "index": {
            "type": "IVF_HNSW_SQ",
            "build_seconds": build_seconds,
            "recall_at_20": statistics.mean(ann_recalls),
            "first_pass_queries": distribution(cold),
            "warm_queries": distribution(warm),
        },
        "local_recall": {
            "first_query_ms": first_ms,
            "repeated_queries": distribution(local),
            "background_import": distribution(background),
        },
        "assembly_with_retrieval": distribution(assembly),
        "rss_peak_gib": peak[0] / 2**30,
        "ingestion": {
            "source_requests": 400,
            "unique_sources": len(set(acknowledged[:400])),
            "seconds": ingest_seconds,
            "requests_per_second": 400 / ingest_seconds,
            "concurrency": 10,
        },
        "external_models": {
            "status": "not_measured",
            "reason": "This scale benchmark excludes external model calls. See the separate configured-model report.",
        },
        "hardware_profile": "Recorded host; measurements are not extrapolated to other machines",
        "scarlett": None,
    }
    report["targets"] = {
        "fast_p95": percentile(local, 0.95) <= 50,
        "fast_p99": percentile(local, 0.99) <= 150,
        "ann_p95": percentile(warm, 0.95) <= 250,
        "ann_p99": percentile(warm, 0.99) <= 750,
        "ann_recall": statistics.mean(ann_recalls) >= 0.95,
        "assembly_p95": percentile(assembly, 0.95) <= 100,
        "rss": peak[0] <= 8 * 2**30,
        "dedup": len(set(acknowledged[:400])) == 200,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
