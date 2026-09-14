"""Synthetic scale check. Never opens a personal memory database or a model API."""
import argparse
import json
import tempfile
import time
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from eventmem.core.retrieval import tokens
from kin_mind.context import Contexts
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind


def benchmark():
    with tempfile.TemporaryDirectory(prefix="kin-memory-benchmark-") as directory:
        engine = Engine(directory)
        scope = Scope(persona="synthetic-scale")
        sid = engine.receive(SourceInput(namespace="fixture", key="scale", scope=scope, text="Synthetic dataset; these are not shared experiences."))["id"]
        mind = Mind(engine, scope)
        mind.initialize(agent_version="fixture-v1", evidence_ids=[sid])
        memory = MemoryContinuity(mind)
        memory.configure({"records": True, "context": True})
        with engine.db.connect(write=True) as conn:
            root = mind._evidence(conn, [sid])[0]["record_id"]
            for index in range(1000):
                memory._put(conn, {"id": f"work_fixture_{index}", "kind": "work", "title": f"A synthetic archive {index}", "created_by": "fixture", "versions": [], "source_ids": [sid], "record_ids": [root]})
            for index in range(10000):
                memory._put(conn, {"id": f"share_fixture_{index}", "kind": "share", "topic": f"A synthetic archive {index % 1000}", "state": "accepted", "visibility": "unverified", "source_ids": [sid], "record_ids": [root],
                    "bubbles": {str(index): {"id": str(index), "state": "accepted", "message_id": "fixture-" + str(index), "text": "A complete thought about a synthetic archive; no phone read receipt exists."}}, "about_ids": [f"work_fixture_{index % 1000}"]})
        contexts = Contexts(mind)
        started = time.perf_counter()
        first = contexts.build("synthetic archive 0", session="fixture-thread", event_id="first")
        elapsed = time.perf_counter() - started
        following = contexts.build("synthetic archive 0", session="fixture-thread", event_id="second")
        old = memory.history("work", identifier="work_fixture_0")
        assert old["items"][0]["id"] == "work_fixture_0"
        assert tokens(first["rendered_text"]) <= 2000
        assert tokens(following["rendered_text"]) <= 800
        assert first["session_used"] + following["tokens"] == following["session_used"]
        return {"shares": 10000, "works": 1000, "first_context_tokens": first["tokens"], "next_context_tokens": following["tokens"], "context_build_ms": round(elapsed * 1000),
            "old_work_found": True, "model_requests": 0, "omitted_count": len(first["omitted_ids"]), "receipt_replay_identical": contexts.build("synthetic archive 0", session="fixture-thread", event_id="first") == first}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = benchmark()
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
