"""Run a frozen private recall set against a snapshot, without sending messages."""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from collections import Counter
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.db import Missing
from eventmem.core.models import Scope
from kin_mind.context import Contexts
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind


def evaluate(engine, scope, cases, *, mode="auto", legacy=False, models=False, progress=None):
    mind = Mind(engine, scope)
    contexts = Contexts(mind)
    outcomes = []
    # Exact duplicates from channel ingestion are equivalent evidence, but a
    # generated paraphrase or a different author's claim is not.
    with engine.db.connect() as conn:
        records = {r["id"]: json.loads(r["data"]) for r in conn.execute(
            "SELECT id,data FROM records WHERE scope=? AND status='active' AND deleted=0", (scope.key(),))}
    equivalent = {}
    for record in records.values():
        if record.get("attributes", {}).get("role") == "user" and not record["generated"]:
            equivalent.setdefault(record["content"].strip(), set()).add(record["id"])
    for case in cases:
        started = time.monotonic()
        expected = set(case["expected_ids"])
        expected_groups = []
        for rid in case["expected_ids"]:
            group = {rid}
            if rid in records:
                group.update(equivalent.get(records[rid]["content"].strip(), set()))
            # Frozen cases can point at an ingestion note while quoting the
            # complete original utterance it preserves. Retrieving that exact
            # owner utterance is the same evidence, not a generated paraphrase.
            quote = case.get("evidence_quotes", {}).get(rid)
            if quote:
                group.update(equivalent.get(quote.strip(), set()))
            expected.update(group)
            expected_groups.append(group)
        if legacy:
            result = contexts.build(case["query"], purpose="read", budget=8000, allow_model=False)
            selected = result["index"][:8]
            metadata = {"mode_used": "legacy", "degraded_reasons": [], "model_requests": 0}
        else:
            from kin_mind.adaptive_recall import AdaptiveRecall
            items, metadata = AdaptiveRecall(contexts).collect(
                case["query"], mode=mode, allow_model=models)
            selected = items[:8]
        reached = set()
        with engine.db.connect() as conn:
            for item in selected:
                reached.add(item["id"])
                reached.update(d["id"] for d in item.get("dependencies", []))
                try:
                    node = contexts.memory.graph.get(conn, item["id"])
                except Missing:
                    continue
                if node.get("membership_authority") == "graph":
                    from kin_mind.lifecycle import EventLifecycle
                    reached.update(EventLifecycle(mind, contexts.memory.graph).snapshot(conn, node["id"])["records"])
                else:
                    reached.update(node.get("record_ids", []))
                    reached.update(r["record_id"] for r in node.get("evidence", []))
        found = all(group & reached for group in expected_groups)
        outcome = {"id": case["id"], "category": case["category"], "critical": case["critical"],
                   "hit_at_8": found, "expected_ids": sorted(expected),
                   "expected_groups": [sorted(g) for g in expected_groups],
                   "selected_ids": [i["id"] for i in selected], "reached_ids": sorted(reached),
                   "elapsed_ms": round((time.monotonic() - started) * 1000, 2), **metadata}
        outcomes.append(outcome)
        if progress:
            progress(outcome, outcomes)
    hits = sum(r["hit_at_8"] for r in outcomes)
    critical = [r for r in outcomes if r["critical"]]
    return {"cases": len(outcomes), "hit_at_8": hits, "recall_at_8": hits / max(1, len(outcomes)),
            "critical_cases": len(critical), "critical_recall": sum(r["hit_at_8"] for r in critical) / max(1, len(critical)),
            "p95_ms": sorted(r["elapsed_ms"] for r in outcomes)[max(0, math.ceil(len(outcomes) * .95) - 1)],
            "median_ms": statistics.median(r["elapsed_ms"] for r in outcomes),
            "degraded": sum(bool(r["degraded_reasons"]) for r in outcomes),
            "categories": dict(Counter(r["category"] for r in outcomes)),
            "measurement": "retrieval of original evidence or its sourced event, not final-answer quality",
            "outcomes": outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--mode", choices=["auto", "light", "deep"], default="auto")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--models", action="store_true")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--embedding-token-file", type=Path)
    args = parser.parse_args()
    if args.credentials_file:
        for line in args.credentials_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.removeprefix("export ").split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    engine = Engine(args.root)
    scope = Scope(**json.loads(args.scope))
    if args.embedding_token_file:
        os.environ["KIN_REPLAY_EMBEDDING_TOKEN"] = args.embedding_token_file.read_text().strip()
        models = engine.settings("models")
        models["embedding"] = {**models["embedding"], "local_embedding": False, "api_key_env": "KIN_REPLAY_EMBEDDING_TOKEN"}
        engine.settings("models", models)
    if not args.legacy:
        MemoryContinuity(Mind(engine, scope)).configure({"adaptive_recall": True})
    frozen = json.loads(args.cases.read_text())
    if not frozen.get("frozen_before_retrieval") or len(frozen["cases"]) < 48:
        raise ValueError("A frozen set of at least 48 independently authored cases is required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def progress(outcome, outcomes):
        args.output.with_suffix(".partial.json").write_text(json.dumps(outcomes, ensure_ascii=False, indent=2))
        print(json.dumps({k: outcome[k] for k in ("id", "hit_at_8", "elapsed_ms", "mode_used", "degraded_reasons")}), flush=True)
    result = evaluate(engine, scope, frozen["cases"], mode=args.mode, legacy=args.legacy, models=args.models, progress=progress)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "outcomes"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
