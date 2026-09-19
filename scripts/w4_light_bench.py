"""W4 light-path recall benchmark: fixed corpus, fixed 50 queries, cold vs warm.

Measures the no-model light path of a normal chat turn against a fixture built
by scripts/w4_light_fixture.py. No network and no paid models: every provider
entry point is patched to raise, and the run fails if one is ever called.

Workloads:
  A dispatch   — host.dispatch(config, "memory-context", ...) per turn, the real
                 host entry (fresh Engine/Mind per call = per-turn host startup).
  B inprocess  — Contexts.build(...) on one long-lived Engine (runtime caliber).
  C collect    — AdaptiveRecall.collect(mode="light") — the expansion light lane.

Sessions rotate every 10 queries within a pass: one session-start (startup
budget) plus nine steady turns, matching production window semantics.

Protocol: N cold probe subprocesses (fresh process + fresh copy each), then one
warm process: a full warmup pass, then R measured passes per workload, each on a
fresh copy of the fixture. Percentiles are nearest-rank, matching the frozen sim.

Usage:
    uv run python scripts/w4_light_bench.py --root <fixture> --output <report.json>
    uv run python scripts/w4_light_bench.py --root <copy> --cold-probe   # internal
"""

from __future__ import annotations

import time

PROCESS_T0 = time.perf_counter()  # before the heavy imports below

import argparse
import contextlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from collections import Counter, defaultdict
from pathlib import Path

import eventmem.core.db as core_db
import eventmem.core.providers as providers_mod
import kin_mind.adaptive_recall as adaptive_recall_mod
import kin_mind.context as context_mod
import kin_mind.lifecycle as lifecycle_mod
from eventmem.core import Engine

QUERIES_FILE = "w4-queries.json"
MANIFEST_FILE = "w4-fixture-manifest.json"


def percentile(values, q):
    """Nearest-rank percentile, the same convention the frozen sim reports."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def distribution(values):
    values = [v for v in values if v is not None]
    if not values:
        return {}
    return {"n": len(values), "p50": round(percentile(values, 0.50), 1),
            "p95": round(percentile(values, 0.95), 1), "p99": round(percentile(values, 0.99), 1),
            "mean": round(statistics.fmean(values), 1), "max": round(max(values), 1)}


# ---------------------------------------------------------------------------
# Stage tracing (bench process only; production code is not modified)

class StageTracer:
    """Nested span tracer. Per-thread stacks; stage totals are inclusive."""

    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self.enabled = False
        self.reset()

    def reset(self):
        with self._lock:
            self.records = []          # (query_seq, stage, inclusive_ms, thread)
            self.db = []               # (query_seq, stage, ms, write)
            self.counts = Counter()    # per-query counters via note()
        self.query_seq = None

    def _stack(self):
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def begin_query(self, seq):
        self.query_seq = seq

    @contextlib.contextmanager
    def span(self, stage):
        if not self.enabled or self.query_seq is None:
            yield
            return
        stack = self._stack()
        stack.append(stage)
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            stack.pop()
            with self._lock:
                self.records.append((self.query_seq, stage, elapsed, threading.get_ident()))

    def note_db(self, ms, write):
        if not self.enabled or self.query_seq is None:
            return
        stack = self._stack()
        stage = stack[-1] if stack else "unscoped"
        with self._lock:
            self.db.append((self.query_seq, stage, ms, write))

    def note(self, key, amount=1):
        if not self.enabled or self.query_seq is None:
            return
        with self._lock:
            self.counts[(self.query_seq, key)] += amount

    def call(self, stage, func, *args, **kwargs):
        with self.span(stage):
            return func(*args, **kwargs)

    # -- per-query aggregation ------------------------------------------------
    def per_query(self):
        stages = defaultdict(lambda: defaultdict(float))
        db_time = defaultdict(lambda: {"read_ms": 0.0, "write_ms": 0.0, "read_n": 0, "write_n": 0})
        counters = defaultdict(Counter)
        for seq, stage, ms, _ in self.records:
            stages[seq][stage] += ms
        for seq, stage, ms, write in self.db:
            slot = db_time[seq]
            slot["write_ms" if write else "read_ms"] += ms
            slot["write_n" if write else "read_n"] += 1
            # DB time also accrues to the stage it ran under.
            stages[seq]["db:" + stage] += ms
        for (seq, key), value in self.counts.items():
            counters[seq][key] = value
        return stages, db_time, counters


def install_tracer(tracer):
    """Patch the light-path lanes with timing spans. Returns the undo closure."""
    from eventmem.core.engine import Engine as EngineClass
    from eventmem.core.read_policy import ReadPolicy
    from kin_mind.adaptive_recall import AdaptiveRecall
    from kin_mind.continuity_manifest import ContinuityManifest
    from kin_mind.graph import EventGraph
    from kin_mind.habits import ConversationHabits
    from kin_mind.memory import MemoryContinuity
    from kin_mind.state import Mind

    originals = []

    def patch(obj, name, stage):
        original = getattr(obj, name)
        originals.append((obj, name, original))

        def wrapped(*args, **kwargs):
            return tracer.call(stage, original, *args, **kwargs)
        setattr(obj, name, wrapped)

    patch(EngineClass, "__init__", "engine_init")
    patch(ReadPolicy, "load", "policy_load")
    patch(Mind, "read", "mind_read")
    patch(ConversationHabits, "read", "habits")
    patch(EventGraph, "read", "graph_read")
    patch(MemoryContinuity, "history", "history_lane")
    patch(ContinuityManifest, "select", "manifest_select")
    patch(AdaptiveRecall, "collect", "collect_total")
    patch(AdaptiveRecall, "temperature_order", "temperature_order")
    patch(context_mod.Contexts, "pack", "pack")
    patch(context_mod.Contexts, "build", "build_total")
    patch(lifecycle_mod, "foreground_lease", "lease")
    patch(core_db.Database, "metric", "metric_write")

    # `candidates` and `tokens` were imported into both module namespaces.
    for module in (context_mod, adaptive_recall_mod):
        patch(module, "candidates", "lexical_candidates")
        patch(module, "tokens", "tokens")

    # overview lookups: count cache hits too.
    original_overview = context_mod.Contexts._overview
    originals.append((context_mod.Contexts, "_overview", original_overview))

    def overview(self, item, policy=None):
        with tracer.span("overview"):
            result = original_overview(self, item, policy)
        if result.get("cached_summary"):
            tracer.note("overview_hits")
        return result
    context_mod.Contexts._overview = overview

    original_overviews = context_mod.Contexts._overviews
    originals.append((context_mod.Contexts, "_overviews", original_overviews))

    def overviews(self, items, policy=None):
        with tracer.span("overview"):
            result = original_overviews(self, items, policy)
        tracer.note("overview_hits", sum(1 for i in result if i.get("cached_summary")))
        return result
    context_mod.Contexts._overviews = overviews

    # DB connection-block accounting (connection open + statements inside).
    original_connect = core_db.Database.connect
    originals.append((core_db.Database, "connect", original_connect))

    @contextlib.contextmanager
    def connect(self, write=False):
        started = time.perf_counter()
        try:
            with original_connect(self, write) as conn:
                yield conn
        finally:
            tracer.note_db((time.perf_counter() - started) * 1000, write)
    core_db.Database.connect = connect

    def unpatch():
        for obj, name, original in originals:
            setattr(obj, name, original)
    return unpatch


_GUARD_ORIGINALS = []


def install_no_model_guard():
    """Every paid/remote entry point raises; the light path must never call one."""
    calls = Counter()

    def guard(name):
        def blocked(*args, **kwargs):
            calls[name] += 1
            raise AssertionError(f"light path attempted a model/provider call: {name}")
        return blocked

    for name in ("embed", "json", "rerank", "visual_embed", "complete"):
        if hasattr(providers_mod.Providers, name):
            _GUARD_ORIGINALS.append((providers_mod.Providers, name, getattr(providers_mod.Providers, name)))
            setattr(providers_mod.Providers, name, guard(f"Providers.{name}"))
    from kin_mind.appraisal import DeepSeek
    for name in ("structured", "complete", "appraise"):
        if hasattr(DeepSeek, name):
            _GUARD_ORIGINALS.append((DeepSeek, name, getattr(DeepSeek, name)))
            setattr(DeepSeek, name, guard(f"DeepSeek.{name}"))
    return calls


def restore_no_model_guard():
    while _GUARD_ORIGINALS:
        obj, name, original = _GUARD_ORIGINALS.pop()
        setattr(obj, name, original)


# ---------------------------------------------------------------------------
# Harness

def copy_fixture(fixture: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(fixture, dst)


def load_queries(root: Path):
    queries = json.loads((root / QUERIES_FILE).read_text())
    assert len(queries) == 50 and len({q["id"] for q in queries}) == 50
    return queries


def run_turn_dispatch(root, scope, query, session):
    from kin_mind import host
    config = {"root": str(root), "scope": scope}
    request = {"query": query, "purpose": "chat", "session": session}
    return host.dispatch(config, "memory-context", request)


def run_turn_build(contexts, query, session):
    return contexts.build(query, purpose="chat", session=session, mode="auto", allow_model=False)


def run_turn_collect(contexts, query):
    from kin_mind.adaptive_recall import AdaptiveRecall
    return AdaptiveRecall(contexts).collect(query, mode="light", allow_model=False)


def run_pass(root, queries, workload, contexts, tracer, pass_index):
    """One measured pass over the fixed query set. Sessions rotate every 10."""
    outcomes = []
    for i, case in enumerate(queries):
        session = f"w4-{workload}-{pass_index}-{i // 10}"
        tracer.begin_query(i)
        started = time.perf_counter()
        if workload == "dispatch":
            result = run_turn_dispatch(root, SCOPE_DICT, case["query"], session)
            meta = {"mode_used": result.get("mode_used"), "tokens": result.get("tokens"),
                    "model_requests": result.get("model_requests"),
                    "local_recall_ms": result.get("local_recall_ms"),
                    "items": len(result.get("index", []))}
        elif workload == "inprocess":
            result = run_turn_build(contexts, case["query"], session)
            meta = {"mode_used": result.get("mode_used"), "tokens": result.get("tokens"),
                    "model_requests": result.get("model_requests"),
                    "local_recall_ms": result.get("local_recall_ms"),
                    "items": len(result.get("index", []))}
        else:
            items, info = run_turn_collect(contexts, case["query"])
            meta = {"mode_used": info.get("mode_used"), "model_requests": info.get("model_requests"),
                    "candidates": info.get("candidate_count"), "tokens": None, "items": len(items)}
        elapsed = (time.perf_counter() - started) * 1000
        # The light path never picks another mode and never calls a model.
        assert meta["mode_used"] == "light", (case["id"], meta)
        assert meta["model_requests"] == 0, (case["id"], meta)
        outcomes.append({"seq": i, "id": case["id"], "shape": case["shape"],
                         "ms": round(elapsed, 3), **meta})
        tracer.begin_query(None)
    return outcomes


def stage_table(tracer, outcomes):
    """Per-stage and per-query decomposition for one pass."""
    stages, db_time, counters = tracer.per_query()
    e2e = {o["seq"]: o["ms"] for o in outcomes}
    per_stage = defaultdict(list)
    for seq in e2e:
        for stage, ms in stages.get(seq, {}).items():
            per_stage[stage].append(ms)
    return {"per_stage_values": dict(per_stage),
            "overview_hits": sum(c.get("overview_hits", 0) for c in counters.values()),
            "conn_read_n": [v["read_n"] for v in db_time.values()],
            "conn_write_n": [v["write_n"] for v in db_time.values()],
            "conn_read_ms": [round(v["read_ms"], 2) for v in db_time.values()],
            "conn_write_ms": [round(v["write_ms"], 2) for v in db_time.values()]}


def merge_stage_values(tables):
    merged = defaultdict(list)
    for table in tables:
        for stage, values in table["per_stage_values"].items():
            merged[stage].extend(values)
    return merged


def machine_info(root):
    return {"platform": platform.platform(), "machine": platform.machine(),
            "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
            "db_bytes": (root / "memory.sqlite3").stat().st_size}


def cold_probe():
    """Fresh-process pass: import/engine/policy caches all start empty."""
    args = argparse.ArgumentParser()
    args.add_argument("--root", type=Path, required=True)
    args.add_argument("--queries", type=Path, required=True)
    args.add_argument("--cold-probe", action="store_true")
    ns = args.parse_args()
    imports_ms = (time.perf_counter() - PROCESS_T0) * 1000
    queries = json.loads(ns.queries.read_text())
    tracer = StageTracer()
    unpatch = install_tracer(tracer)
    guard = install_no_model_guard()
    tracer.enabled = True
    outcomes = run_pass(ns.root, queries, "dispatch", None, tracer, 0)
    unpatch()
    stages, db_time, _ = tracer.per_query()
    per_query = []
    for outcome in outcomes:
        seq = outcome["seq"]
        per_query.append({**outcome, "stages": {k: round(v, 2) for k, v in stages.get(seq, {}).items()},
                          "conn": db_time.get(seq, {})})
    print(json.dumps({"import_ms": round(imports_ms, 1), "guard_calls": dict(guard),
                      "outcomes": per_query}))


SCOPE_DICT = {"project": "personal", "persona": "Kin", "collection": "default", "world": "real"}


def dump_outputs(root: Path, output: Path):
    """Canonical per-query outputs of the light path, for before/after equivalence diffs."""
    import hashlib
    from eventmem.core.models import Scope
    from kin_mind.context import Contexts
    from kin_mind.state import Mind
    # Time-projected motivations and temperature evidence are part of the result.
    # Removing only a top-level clock does not make two wall-clock runs comparable.
    manifest = json.loads((root / MANIFEST_FILE).read_text())
    fixed_at = manifest["anchor_date"][:10] + "T12:00:00.000000+00:00"
    queries = load_queries(root)
    volatile = {"elapsed_ms", "local_recall_ms", "clock", "candidate_wait_ms"}
    results = {}
    guard = install_no_model_guard()
    try:
        with tempfile.TemporaryDirectory(prefix="w4-equivalence-") as temporary:
            copy = Path(temporary) / "store"
            copy_fixture(root, copy)
            engine = Engine(copy)
            contexts = Contexts(Mind(engine, Scope.model_validate(SCOPE_DICT), clock=lambda: fixed_at))
            for i, case in enumerate(queries):
                session = f"w4-dump-{i // 10}"
                built = contexts.build(case["query"], purpose="chat", session=session, mode="auto", allow_model=False)
                items, info = run_turn_collect(contexts, case["query"])
                results[case["id"]] = {
                    "build": {k: v for k, v in built.items() if k not in volatile},
                    "collect": {"items": items, "info": {k: v for k, v in info.items() if k not in volatile}},
                }
    finally:
        restore_no_model_guard()
    assert not guard, dict(guard)
    canonical = json.dumps(results, ensure_ascii=False, sort_keys=True, indent=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical)
    print(json.dumps({"outputs": len(results), "at": fixed_at,
                      "sha256": hashlib.sha256(canonical.encode()).hexdigest()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Fixture root (never written to)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runs", type=int, default=3, help="Measured warm passes per workload")
    parser.add_argument("--cold-processes", type=int, default=3)
    parser.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()) / "w4-light-bench")
    parser.add_argument("--dump-outputs", type=Path,
                        help="Write canonical per-query build+collect outputs and exit (equivalence diffs)")
    args = parser.parse_args()
    if args.dump_outputs:
        dump_outputs(args.root, args.dump_outputs)
        return

    manifest = json.loads((args.root / MANIFEST_FILE).read_text())
    queries = load_queries(args.root)
    report = {"benchmark": "w4-light", "fixture": {"root": str(args.root), **manifest},
              "machine": machine_info(args.root),
              "protocol": {"queries": len(queries), "warmup_passes": 1, "measured_passes": args.runs,
                           "cold_processes": args.cold_processes,
                           "session_rotation": "new session every 10 queries",
                           "percentile": "nearest-rank, same as the frozen sim",
                           "os_file_cache": "not flushed; cold = fresh process + fresh copy"},
              "guard": {}, "workloads": {}}

    # --- cold: fresh process per run ------------------------------------------
    cold_runs, import_ms = [], []
    for run in range(args.cold_processes):
        copy = args.workdir / f"cold-{run}"
        copy_fixture(args.root, copy)
        raw = subprocess.check_output(
            [sys.executable, str(Path(__file__).resolve()), "--root", str(copy),
             "--queries", str(args.root / QUERIES_FILE), "--cold-probe"], text=True)
        probe = json.loads(raw)
        assert not probe["guard_calls"], probe["guard_calls"]
        cold_runs.append(probe["outcomes"])
        import_ms.append(probe["import_ms"])
        shutil.rmtree(copy, ignore_errors=True)
    cold_ms = [o["ms"] for run in cold_runs for o in run]
    report["workloads"]["dispatch"] = {
        "cold": {"e2e": distribution(cold_ms),
                 "per_run_p95": [round(percentile([o["ms"] for o in run], 0.95), 1) for run in cold_runs],
                 "first_query_ms": [run[0]["ms"] for run in cold_runs],
                 "import_ms": import_ms,
                 "stage_means": _cold_stage_means(cold_runs)}}

    # --- warm: one process, warmup pass, measured passes -------------------------
    for workload in ("dispatch", "inprocess", "collect"):
        passes, stage_tables, guard_total = [], [], Counter()
        for run in range(args.runs):
            copy = args.workdir / f"warm-{workload}-{run}"
            copy_fixture(args.root, copy)
            tracer = StageTracer()
            unpatch = install_tracer(tracer)
            guard = install_no_model_guard()
            engine = Engine(copy)
            from eventmem.core.models import Scope
            from kin_mind.context import Contexts
            from kin_mind.state import Mind
            contexts = Contexts(Mind(engine, Scope.model_validate(SCOPE_DICT)))
            # Warmup pass on this copy: fills the policy snapshot cache, overview
            # reads, jieba/tiktoken state and the OS/SQLite page cache, so every
            # measured pass runs against the same warm state.
            run_pass(copy, queries, workload, contexts, tracer, -1)
            tracer.reset()
            tracer.enabled = True
            outcomes = run_pass(copy, queries, workload, contexts, tracer, run)
            tracer.enabled = False
            stage_tables.append(stage_table(tracer, outcomes))
            guard_total.update(guard)
            unpatch()
            passes.append(outcomes)
            shutil.rmtree(copy, ignore_errors=True)
        assert not guard_total, dict(guard_total)
        ms = [o["ms"] for p in passes for o in p]
        merged = merge_stage_values(stage_tables)
        mean_e2e = statistics.fmean(ms)
        entry = {"e2e": distribution(ms),
                 "per_run_p95": [round(percentile([o["ms"] for o in p], 0.95), 1) for p in passes],
                 "per_run_median": [round(statistics.median(o["ms"] for o in p), 1) for p in passes],
                 "stages": {stage: distribution(values) for stage, values in sorted(merged.items())},
                 "stage_mean_share_pct": {stage: round(statistics.fmean(values) / mean_e2e * 100, 1)
                                          for stage, values in sorted(merged.items())},
                 "connections_per_turn": {
                     "read": distribution([n for t in stage_tables for n in t["conn_read_n"]]),
                     "write": distribution([n for t in stage_tables for n in t["conn_write_n"]])},
                 "conn_ms_per_turn": {
                     "read": distribution([n for t in stage_tables for n in t["conn_read_ms"]]),
                     "write": distribution([n for t in stage_tables for n in t["conn_write_ms"]])},
                 "overview_hits": sum(t["overview_hits"] for t in stage_tables),
                 "by_shape": {shape: distribution([o["ms"] for p in passes for o in p if o["shape"] == shape])
                              for shape in sorted({o["shape"] for p in passes for o in p})}}
        if workload == "dispatch":
            report["workloads"]["dispatch"]["warm"] = entry
        else:
            report["workloads"][{"inprocess": "inprocess", "collect": "collect_light"}[workload]] = {"warm": entry}
        report["guard"][workload] = "no provider/model calls"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    summary = {"dispatch_cold": report["workloads"]["dispatch"]["cold"]["e2e"],
               "dispatch_warm": report["workloads"]["dispatch"]["warm"]["e2e"],
               "inprocess_warm": report["workloads"]["inprocess"]["warm"]["e2e"],
               "collect_light_warm": report["workloads"]["collect_light"]["warm"]["e2e"],
               "stage_share": report["workloads"]["dispatch"]["warm"]["stage_mean_share_pct"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _cold_stage_means(cold_runs):
    means = defaultdict(list)
    for run in cold_runs:
        for outcome in run:
            for stage, ms in outcome.get("stages", {}).items():
                means[stage].append(ms)
    return {stage: round(statistics.fmean(values), 1) for stage, values in sorted(means.items())}


if __name__ == "__main__":
    if "--cold-probe" in sys.argv:
        cold_probe()
    else:
        main()
