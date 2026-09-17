"""Lifecycle behavior against real SQLite transactions and versioned sources."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict
from eventmem.core.jobs import Worker
from eventmem.core.models import RevisionInput, Scope, SourceInput
from kin_mind.adaptive_recall import (
    AdaptiveRecall,
    RecallFollowup,
    RecallRanking,
    bounded,
    fresh_leads,
)
from kin_mind.context import Contexts
from kin_mind.lifecycle import (
    EventIdentityJudgement,
    EventLifecycle,
    EventRoute,
    EventSummary,
    SummaryUnit,
    due_slot,
    record_usage,
    schedule,
)
from kin_mind.memory import MemoryAssessment, MemoryContinuity
from kin_mind.state import Mind


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 16, 5, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "memory")
    mind = Mind(engine, Scope(persona="synthetic-lifecycle"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)
    def source(key, text=None, authority="explicit", occurred_at=None):
        return engine.receive(SourceInput(namespace="synthetic", key=key, text=text or key,
            scope=mind.scope, authority=authority, occurred_at=occurred_at or mind.clock(), extract=False))
    initial = source("initial", "Synthetic lifecycle fixture")
    mind.initialize(agent_version="fixture", evidence_ids=[initial["id"]])
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True,
        "graph_recall": True, "sharing": True, "event_lifecycle": True, "adaptive_recall": True,
        "auto_volumes": True, "temperature_shadow": True})
    return mind, memory, EventLifecycle(mind, memory.graph), source, clock


def route(system, *, action="create", target=None, key="first", text="星图报告最初版本还未发送。", binding="semantic_candidate", identity_decision="same_event"):
    mind, memory, lifecycle, source, _ = system
    src = source(key, text)
    identity = None
    if target and binding == "explicit_reference":
        with mind.engine.db.connect() as conn:
            prior = lifecycle.snapshot(conn, target["id"])["records"]
            prior_ids = list(prior)[:1]
        identity = EventIdentityJudgement(decision=identity_decision, participants_match=True, object_match=True,
                                          time_compatible=True, continuation_supported=True, prior_record_ids=prior_ids)
    proposal = EventRoute(key=key, action=action, title="星图报告", event_id=target["id"] if target else None,
        expected_revision=target["revision"] if target else None, evidence_ids=[src["id"]],
        binding=binding, quote=text if binding == "explicit_reference" else "", identity=identity, reason="Sourced fictional event")
    with mind.engine.db.connect(write=True) as conn:
        proof = memory.graph.proof(conn, [src["id"]])
        if identity:
            proof += memory.graph.proof(conn, identity.prior_record_ids)
        result = lifecycle.apply_routes(conn, [proposal], proof, "appraisal-" + key)[0]
    return result, src, proposal


class Summarizer:
    timeout = 300
    def structured(self, name, schema, system, context, **options):
        assert name == "submit_event_digest"
        assert options["max_tokens"] == 65536
        assert "reasoning" in system
        records = context["records"]
        return EventSummary(narrative=[SummaryUnit(text=r["text"], record_ids=[r["id"]]) for r in records]), {"model": "synthetic", "reasoning": "high"}


def publish(system, identifier):
    mind, _, lifecycle, _, _ = system
    apply = lifecycle.prepare_digest(identifier, Summarizer())
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
    return lifecycle.read(identifier)


def test_scheduler_does_not_run_on_ineligible_dirty_or_unrelated_generation(system):
    mind, _, _, source, _ = system
    source("dirty", "Unverified synthetic claim", authority="model")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE records SET status='unverified'")
        # Leave the dirty entries for later confirmation.
    worker = Worker(mind.engine)
    worker.schedule_maintenance()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dirty").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='organize'").fetchone()[0] == 0


def test_active_batch_keeps_one_organize_identity_across_metrics(system):
    mind, _, _, source, _ = system
    for n in range(22):
        source(str(n), "Synthetic active topic " + str(n))
    worker = Worker(mind.engine)
    worker.schedule_maintenance()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET state='complete' WHERE kind='organize'")
        mind.engine.db.bump(conn)
    worker.last_maintenance = float("-inf")
    worker.schedule_maintenance()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='organize'").fetchone()[0] == 1


def test_append_keeps_identity_and_advances_revision_and_members(system):
    mind, memory, lifecycle, _, _ = system
    first, _, _ = route(system)
    event = memory.graph.detail(first["event_id"])
    second, _, _ = route(system, action="append", target=event, key="next",
        text="继续星图报告：第二版已经完成，但还没有发送。", binding="explicit_reference")
    assert second["state"] == "append" and second["event_id"] == event["id"]
    assert memory.graph.detail(event["id"])["revision"] > event["revision"]
    with mind.engine.db.connect() as conn:
        snapshot = lifecycle.snapshot(conn, event["id"])
    assert len(snapshot["records"]) == 2


def test_similarity_alone_stays_deferred(system):
    _, memory, _, _, _ = system
    first, _, _ = route(system)
    event = memory.graph.detail(first["event_id"])
    result, _, _ = route(system, action="append", target=event, key="other", text="另一份星图报告已发送。")
    assert result["state"] == "defer"
    assert memory.graph.detail(event["id"])["revision"] == event["revision"]


def test_route_is_idempotent_and_rejects_mutated_command(system):
    mind, memory, lifecycle, _, _ = system
    first, src, proposal = route(system)
    with mind.engine.db.connect(write=True) as conn:
        proof = memory.graph.proof(conn, [src["id"]])
        assert lifecycle.apply_routes(conn, [proposal], proof, "appraisal-first")[0] == first
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict):
        lifecycle.apply_routes(conn, [proposal.model_copy(update={"title": "Changed"})], proof, "appraisal-first")


def test_automatic_route_can_be_undone_without_deleting_originals(system):
    mind, memory, _, _, _ = system
    result, src, _ = route(system)
    event = memory.graph.detail(result["event_id"])
    receipt = memory.graph.revise({"command_id": "undo-route", "action": "undo", "id": event["id"],
        "expected_revision": event["revision"], "previous_command_id": result["undo_command_id"],
        "reason": "Wrong event grouping", "evidence_ids": [src["id"]]})
    assert receipt["state"] == "applied"
    assert memory.graph.detail(event["id"])["state"] == "retracted"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_graph_edges WHERE object=? AND state='active'", (event["id"],)).fetchone()[0] == 0
        rid = memory.graph.proof(conn, [src["id"]])[0]["record_id"]
    assert mind.engine.get(rid)["status"] == "active"


def test_feature_disable_fences_a_prepared_job_commit(system, monkeypatch):
    from eventmem.core.providers import NotConfigured
    from kin_mind.lifecycle import prepare_job
    mind, memory, _, _, _ = system
    result, _, _ = route(system)
    apply = prepare_job(mind.engine, {"kind": "event_digest"}, {"scope": mind.scope.model_dump(), "event_id": result["event_id"]})
    memory.configure({"event_lifecycle": False})
    with mind.engine.db.connect(write=True) as conn, pytest.raises(NotConfigured):
        apply(conn)
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT revision FROM mind_event_digests WHERE event_id=?", (result["event_id"],)).fetchone()[0] == 0


def test_legacy_host_envelopes_do_not_impersonate_owner_sources(system):
    mind, _, _, source, _ = system
    source("genuine", "我要求探索时间给到二十分钟。")
    source("old-envelope", "内部探索选题事件，不是用户的新消息，不发送消息。探索时间二十分钟。")
    items, _ = AdaptiveRecall(Contexts(mind)).collect("我要求探索时间多少分钟？", mode="light")
    assert any("我要求探索时间" in i["text"] for i in items)
    assert not any(i["text"].startswith("内部探索选题事件，") for i in items)


def test_appraisal_commits_route_in_same_transaction(system):
    mind, memory, _, source, _ = system
    src = source("transaction", "A fictional report is unfinished")
    assessment = MemoryAssessment(event_routes=[EventRoute(key="one", action="create", title="Report",
        evidence_ids=[src["id"]], reason="Original request")])
    with pytest.raises(RuntimeError), mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [src["id"]])
        memory.apply_assessment(conn, assessment, refs, "atomic", 0, 20, {}, schedule=False)
        raise RuntimeError("Simulated later validation failure")
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_event_routes").fetchone()[0] == 0


def test_concurrent_append_rejects_old_digest_and_rebuilds_complete_event(system):
    mind, memory, lifecycle, _, _ = system
    first, _, _ = route(system)
    event = memory.graph.detail(first["event_id"])
    pending = lifecycle.prepare_digest(event["id"], Summarizer())
    route(system, action="append", target=event, key="during", text="星图报告增加了第三个观测点。", binding="explicit_reference")
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict):
        pending(conn)
    final = publish(system, event["id"])
    assert final["state"] == "ready"
    assert len(final["data"]["source_versions"]) == 2


def test_source_correction_invalidates_digest_and_old_cached_item(system):
    mind, memory, lifecycle, _, _ = system
    first, _, _ = route(system)
    event_id = first["event_id"]
    summary = publish(system, event_id)
    contexts = Contexts(mind)
    item = contexts.graph_item(memory.graph.detail(event_id))
    assert contexts._current(item)
    rid = next(iter(summary["data"]["source_versions"]))
    record = mind.engine.get(rid)
    mind.engine.revise(rid, RevisionInput(expected_revision=record["revision"], action="correct",
        reason="Explicit correction", content="星图报告尚未完成，也没有发送。", command_id="source-correction"))
    assert lifecycle.read(event_id)["state"] == "dirty"
    assert not contexts._current(item)
    revised = publish(system, event_id)
    assert revised["data"]["source_versions"][rid] == record["revision"] + 1


def test_unknown_summary_citation_never_becomes_ready(system):
    _, _, lifecycle, _, _ = system
    first, _, _ = route(system)
    class Bad(Summarizer):
        def structured(self, *args, **kwargs):
            return EventSummary(conclusions=[SummaryUnit(text="Invented", record_ids=["mem_unknown"])]), {}
    with pytest.raises(Conflict):
        lifecycle.prepare_digest(first["event_id"], Bad())
    assert lifecycle.read(first["event_id"])["state"] != "ready"


def test_truncated_provider_never_replaces_last_good_digest(system):
    _, _, lifecycle, _, _ = system
    first, _, _ = route(system)
    good = publish(system, first["event_id"])
    class Truncated(Summarizer):
        def structured(self, *args, **kwargs):
            raise RuntimeError("deepseek-incomplete-or-unverified")
    with pytest.raises(RuntimeError):
        lifecycle.prepare_digest(first["event_id"], Truncated())
    assert lifecycle.read(first["event_id"])["data"] == good["data"]


@pytest.mark.parametrize("move_original", [False, True])
def test_split_event_and_undo_restore_exact_members(system, move_original):
    mind, memory, lifecycle, _, _ = system
    first, src, _ = route(system)
    event = memory.graph.detail(first["event_id"])
    second, _, _ = route(system, action="append", target=event, key="split-member", text="星图报告包含第二段。", binding="explicit_reference")
    event = memory.graph.detail(event["id"])
    request = {"id": event["id"], "expected_revision": event["revision"], "action": "split_event",
        "member_ids": (first if move_original else second)["member_ids"], "title": "第二个观测事件", "command_id": "split-test",
        "evidence_ids": [src["id"]], "reason": "Explicit separation"}
    split = memory.graph.revise(request)
    assert split == memory.graph.revise(request)
    with mind.engine.db.connect() as conn:
        original = set(lifecycle.snapshot(conn, event["id"])["records"])
        split_id = memory.graph.identifier("event", ["split", request["command_id"]])
        separated = set(lifecycle.snapshot(conn, split_id)["records"])
        assert len(original) == len(separated) == 1 and not original.intersection(separated)
    memory.graph.revise({"id": event["id"], "expected_revision": split["after_revisions"][event["id"]],
        "action": "undo", "previous_command_id": "split-test", "command_id": "undo-split",
        "evidence_ids": [src["id"]], "reason": "Undo synthetic split"})
    with mind.engine.db.connect() as conn:
        assert len(lifecycle.snapshot(conn, event["id"])["records"]) == 2


def test_original_detail_and_revision_mismatch(system):
    mind, memory, _, _, _ = system
    first, _, _ = route(system)
    event = memory.graph.detail(first["event_id"])
    contexts = Contexts(mind)
    assert contexts.event_thread(event["id"], expected_revision=event["revision"] + 1)["state"] == "revision_changed"
    original = contexts.event_thread(event["id"], detail="original", budget=2000)
    assert "最初版本还未发送" in original["text"]
    assert original["tokens"] <= 2000 and original["detail"] == "original"
    index = contexts.event_thread(event["id"], detail="index")
    assert index["text"] == "" and index["index"]


def test_cold_memory_stays_valid_and_background_reads_do_not_heat_it(system):
    mind, _, lifecycle, source, clock = system
    source("ancient", "很久以前的蓝色星图", occurred_at=(clock[0] - timedelta(days=120)).isoformat())
    with mind.engine.db.connect() as conn:
        rid = conn.execute("SELECT id FROM records WHERE json_extract(data,'$.content')='很久以前的蓝色星图'").fetchone()[0]
    with mind.engine.db.connect(write=True) as conn:
        record_usage(conn, mind.scope.key(), rid, "maintenance", "maintenance", mind.clock())
        record_usage(conn, mind.scope.key(), rid, "injection", "automatic_injection", mind.clock())
    apply = lifecycle.prepare_temperature()
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
        assert conn.execute("SELECT tier FROM mind_memory_temperature WHERE identifier=?", (rid,)).fetchone()[0] == "cold"
    assert mind.engine.get(rid)["status"] == "active"
    result = Contexts(mind).build("蓝色星图", purpose="read", mode="light", budget=4000)
    assert rid in {i["id"] for i in result["index"]}
    apply = lifecycle.prepare_temperature()
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
        assert conn.execute("SELECT tier FROM mind_memory_temperature WHERE identifier=?", (rid,)).fetchone()[0] == "hot"


def test_cooling_needs_full_week_and_current_validation(system):
    mind, memory, _, _, clock = system
    with pytest.raises(Conflict):
        memory.configure({"temperature_ranking": True})
    clock[0] += timedelta(days=7)
    with pytest.raises(Conflict):
        memory.configure({"temperature_ranking": True})
    validation = {"critical_recall": 1, "recall_at_8": .95, "wrong_merges": 0,
        "unsupported_upgrades": 0, "stale_facts": 0, "background_reinforcement": 0, "evaluated_at": mind.clock()}
    with pytest.raises(Conflict):
        memory.configure({"temperature_validation": validation, "temperature_ranking": True})
    with mind.engine.db.connect(write=True) as conn:
        for offset in range(7):
            at = (clock[0] - timedelta(days=offset)).isoformat()
            conn.execute("INSERT INTO mind_lifecycle_runs VALUES(?,?,?,?,?,?)",
                         (mind.scope.key(), "temperature", due_slot(at, "temperature"), "fixture", "complete", json.dumps({"scheduled_at": at, "observed_at": at, "observed_day": at[:10]})))
    assert memory.configure({"temperature_validation": validation, "temperature_ranking": True})["temperature_ranking"]


def test_maintenance_is_singapore_time_deduplicated_and_receipt_based(system):
    mind, _, _, _, _ = system
    assert due_slot("2026-09-16T01:00:00Z", "volumes") == "2026-09-16T04:00:00+08:00"
    assert due_slot("2026-09-15T18:00:00Z", "volumes") == "2026-09-13T04:00:00+08:00"
    with mind.engine.db.connect(write=True) as conn:
        schedule(mind.engine, conn, mind.clock())
        schedule(mind.engine, conn, mind.clock())
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='lifecycle_temperature'").fetchone()[0] == 1


def test_deep_recall_preserves_local_results_when_providers_fail(system, monkeypatch):
    mind, _, _, source, _ = system
    src = source("deep-evidence", "星图报告最终版本尚未发送")
    from eventmem.core.providers import Providers
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unavailable")))
    class Failed:
        timeout = 30
        def structured(self, *a, **k):
            raise TimeoutError()
    result = Contexts(mind).build("上次星图报告的版本", purpose="read", mode="deep", provider=Failed(), allow_model=True, budget=8000)
    assert result["mode_used"] == "deep" and result["degraded_reasons"]
    assert "尚未发送" in result["text"]
    assert src["id"] not in result["evidence_versions"]  # record identities, never invented source ids


def test_retrieval_mode_forwarded_through_generic_api(system, monkeypatch):
    mind, _, _, _, _ = system
    from eventmem.core.models import RecallRequest
    from eventmem.core.retrieval import recall
    calls = []
    def collect(self, query, **kwargs):
        calls.append(kwargs["mode"])
        return [], {"mode_used": kwargs["mode"], "pending_ids": []}
    monkeypatch.setattr(AdaptiveRecall, "collect", collect)
    recall(mind.engine, RecallRequest(scope=mind.scope, query="星图", mode="deep", phase="read"))
    assert calls == ["deep"]


def test_light_recall_never_calls_model_or_embedding(system, monkeypatch):
    mind, _, _, source, _ = system
    source("light", "今天看到了蓝色星图")
    from eventmem.core.providers import Providers
    def forbidden(*a, **k):
        raise AssertionError("Light recall must be local")
    monkeypatch.setattr(Providers, "embed", forbidden)
    items, info = AdaptiveRecall(Contexts(mind)).collect("蓝色星图", mode="light", allow_model=True)
    assert items and info["model_requests"] == 0 and info["mode_used"] == "light"


def test_absolute_deadline_does_not_wait_for_optional_provider():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        bounded(lambda: time.sleep(.15), .01)
    assert time.monotonic() - started < .12


def test_reranker_cannot_invent_source_ids(system, monkeypatch):
    mind, _, _, source, _ = system
    source("rank", "星图报告仍未发送")
    from eventmem.core.providers import Providers
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    class Bad:
        timeout = 30
        def structured(self, *a, **k):
            return RecallRanking(ids=["unknown"]), {}
    items, info = AdaptiveRecall(Contexts(mind)).collect("星图报告", mode="deep", allow_model=True, provider=Bad())
    assert items and "rerank:Conflict" in info["degraded_reasons"]
    assert "unknown" not in {i["id"] for i in items}


def test_same_host_task_can_append_record_members(system):
    mind, memory, lifecycle, _, _ = system
    first = memory.ingest({"id": "request", "kind": "owner-message", "text": "制作星图报告", "at": mind.clock(), "task_id": "star-map"})
    second = memory.ingest({"id": "result", "kind": "task-result", "text": "星图报告已有草稿", "at": mind.clock(), "task_id": "star-map"})
    target = memory.graph.detail(first["event_id"])
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [second["source_id"]])
        result = lifecycle.apply_routes(conn, [EventRoute(key="task", action="append", event_id=target["id"],
            expected_revision=target["revision"], evidence_ids=[second["source_id"]], binding="same_task",
            reason="Same bound host task")], refs, "task-result")
    assert result[0]["state"] == "append"


def test_volumes_follow_evidenced_threads_without_touching_manual_volumes(system):
    from eventmem.core.organize import Organizer
    mind, memory, lifecycle, _, _ = system
    one = memory.ingest({"id": "one", "kind": "owner-message", "text": "星图的第一次观测", "at": mind.clock(), "task_id": "stars"})
    two = memory.ingest({"id": "two", "kind": "task-result", "text": "星图的第二次观测", "at": mind.clock(), "task_id": "stars"})
    manual = Organizer(mind.engine).create(mind.scope, "Manual", [one["record_id"]])
    apply = lifecycle.prepare_volumes()
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
    with mind.engine.db.connect() as conn:
        volumes = [json.loads(r[0]) for r in conn.execute("SELECT data FROM families")]
    assert next(v for v in volumes if v["id"] == manual["id"]) == manual
    auto = [v for v in volumes if v.get("generated_by") == "kin-lifecycle"]
    assert len(auto) == 1 and set(auto[0]["members"]) == {one["record_id"], two["record_id"]}
    again = lifecycle.prepare_volumes()
    with mind.engine.db.connect(write=True) as conn:
        again(conn)
        assert conn.execute("SELECT revision FROM families WHERE id=?", (auto[0]["id"],)).fetchone()[0] == 1


def test_cluster_candidates_reach_model_without_creating_event_membership(system):
    from eventmem.core.organize import Organizer
    from kin_mind.appraisal import appraisal_context
    mind, memory, lifecycle, _, _ = system
    one, _, _ = route(system, key="first-observation", text="星图第一晚的观测")
    two, _, _ = route(system, key="second-observation", text="星图第二晚的独立观测")
    family = Organizer(mind.engine).create(mind.scope, "星图观测候选", [*one["member_ids"], *two["member_ids"]], kind="family")
    context = memory.semantic_context("星图")
    projected = appraisal_context({"memory_context": context})
    candidate = next(c for c in projected["memory_context"]["topic_candidates"] if c["id"] == family["id"])
    assert candidate["candidate_only"] is True and len(candidate["members"]) == 2
    assert all(r["revision"] == 1 for r in candidate["members"])
    commit = lifecycle.prepare_volumes()
    with mind.engine.db.connect(write=True) as conn:
        commit(conn)
        assert conn.execute("SELECT COUNT(*) FROM families WHERE json_extract(data,'$.generated_by')='kin-lifecycle'").fetchone()[0] == 0
        assert len(lifecycle.snapshot(conn, one["event_id"])["records"]) == 1


def test_worker_recovers_failed_digest_and_respects_priority_and_foreground(system, monkeypatch):
    mind, _, _, _, _ = system
    mind.engine.enqueue("event_digest", {"scope": mind.scope.model_dump(), "event_id": "event"}, "priority", priority=20)
    worker = Worker(mind.engine)
    mind.engine.interactive_until = time.monotonic() + 30
    assert worker.claim() is None
    mind.engine.interactive_until = 0
    job = worker.claim()
    assert job["kind"] == "event_digest"
    other = Worker(mind.engine)
    next_job = other.claim()
    assert next_job is None or next_job["kind"] != "event_digest"


def test_summary_does_not_upgrade_inferred_model_evidence(system):
    mind, memory, _, source, _ = system
    src = source("hypothesis", "我猜星图里有一颗红色恒星", authority="model")
    with mind.engine.db.connect(write=True) as conn:
        proof = memory.graph.proof(conn, [src["id"]])
        node = memory.graph._put(conn, {"id": "hypothesis-event", "kind": "event", "title": "猜测",
            "basis": "inferred", "occurred_at": mind.clock(), "source_ids": [src["id"]], "evidence": proof})
    summary = publish(system, node["id"])
    assert summary["data"]["narrative"][0]["basis"] == "inferred"


def test_current_queries_exclude_retracted_sources(system):
    mind, _, lifecycle, _, _ = system
    first, _, _ = route(system)
    summary = publish(system, first["event_id"])
    rid = next(iter(summary["data"]["source_versions"]))
    record = mind.engine.get(rid)
    mind.engine.revise(rid, RevisionInput(expected_revision=record["revision"], action="retract",
        reason="Retracted fixture", command_id="retract"))
    with mind.engine.db.connect() as conn:
        assert rid not in lifecycle.snapshot(conn, first["event_id"])["records"]
    with pytest.raises(ValueError):
        lifecycle.prepare_digest(first["event_id"], Summarizer())


def test_split_undo_does_not_overwrite_concurrent_change(system):
    _, memory, _, _, _ = system
    first, src, _ = route(system)
    original = memory.graph.detail(first["event_id"])
    next_member, _, _ = route(system, action="append", target=original, key="split-extra", text="星图报告补充材料", binding="explicit_reference")
    current = memory.graph.detail(original["id"])
    split = memory.graph.revise({"id": current["id"], "expected_revision": current["revision"], "action": "split_event",
        "member_ids": next_member["member_ids"], "title": "Extra", "command_id": "split-concurrent", "evidence_ids": [src["id"]], "reason": "Split"})
    memory.graph.revise({"id": current["id"], "expected_revision": split["after_revisions"][current["id"]],
        "action": "correct", "changes": {"title": "Changed after split"}, "command_id": "later",
        "evidence_ids": [src["id"]], "reason": "New evidence"})
    with pytest.raises(Conflict):
        memory.graph.revise({"id": current["id"], "expected_revision": memory.graph.detail(current["id"])["revision"],
            "action": "undo", "previous_command_id": "split-concurrent", "command_id": "undo-concurrent",
            "evidence_ids": [src["id"]], "reason": "Old undo"})


def test_historical_backfill_does_not_create_usage(system):
    mind, _, lifecycle, _, _ = system
    route(system)
    result = lifecycle.backfill(limit=1)
    assert result["processed"] == 1 and result["cursor"]
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_event_usage").fetchone()[0] == 0


def test_single_record_projection_needs_no_model(system, monkeypatch):
    mind, _, lifecycle, _, _ = system
    first, _, _ = route(system)
    from kin_mind.appraisal import DeepSeek
    monkeypatch.setattr(DeepSeek, "from_engine", lambda *a: (_ for _ in ()).throw(AssertionError("Unneeded model call")))
    apply = lifecycle.prepare_digest(first["event_id"])
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
    assert lifecycle.read(first["event_id"])["data"]["model_receipt"]["model_requests"] == 0


def test_source_expiry_invalidates_digest_without_new_record_revision(system):
    mind, _, lifecycle, _, clock = system
    result, _, _ = route(system)
    with mind.engine.db.connect() as conn:
        rid = next(iter(lifecycle.snapshot(conn, result['event_id'])['records']))
    record = mind.engine.get(rid)
    record['valid_until'] = (clock[0] + timedelta(minutes=1)).isoformat()
    with mind.engine.db.connect(write=True) as conn:
        mind.engine._save_revision(conn, record, 'temporary-fixture', 'Temporary source')
    apply = lifecycle.prepare_digest(result['event_id'])
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
    assert lifecycle.read(result['event_id'])['state'] == 'ready'
    clock[0] += timedelta(minutes=2)
    assert lifecycle.read(result['event_id'])['state'] == 'dirty'
    with mind.engine.db.connect(write=True) as conn:
        schedule(mind.engine, conn, mind.clock())
        row = conn.execute('SELECT state FROM mind_event_digests WHERE event_id=?', (result['event_id'],)).fetchone()
        assert row['state'] == 'dirty'


def test_erased_source_cannot_survive_as_stale_digest(system):
    mind, _, lifecycle, _, _ = system
    result, _, _ = route(system)
    apply = lifecycle.prepare_digest(result['event_id'])
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
    value = lifecycle.read(result['event_id'])
    rid = next(iter(value['data']['source_versions']))
    mind.engine.delete(rid)
    assert lifecycle.read(result['event_id'])['data'] == {}


def test_lease_exhaustion_settles_refreshing_state(system):
    mind, _, _, _, _ = system
    result, _, _ = route(system)
    with mind.engine.db.connect(write=True) as conn:
        identifier = mind.engine.enqueue('event_digest', {'scope': mind.scope.model_dump(), 'event_id': result['event_id']}, 'expired-digest', conn=conn)
        conn.execute("UPDATE jobs SET state='running',attempts=max_attempts,lease_until=0 WHERE id=?", (identifier,))
        conn.execute("UPDATE mind_event_digests SET state='refreshing' WHERE event_id=?", (result['event_id'],))
    Worker(mind.engine).claim()
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT state FROM mind_event_digests WHERE event_id=?', (result['event_id'],)).fetchone()[0] == 'failed'


def test_unrelated_constraints_do_not_steal_recall_reservations():
    from kin_mind.adaptive_recall import relevant_protection
    record = {"id": "mem_synthetic", "kind": "preference", "attributes": {"constraint": True},
              "content": "选题要求结合最近结果，避开重复。"}
    assert not relevant_protection(record, "反复要求说话自然，还确认过什么？")
    assert not relevant_protection(record, "之前对选题有什么要求？")  # Model judges relevance.
    assert relevant_protection(record, "读取 mem_synthetic 的原文")


def test_foreground_lease_is_shared_expires_and_allows_urgent_corrections(system):
    from kin_mind.lifecycle import foreground_lease
    mind, _, _, _, _ = system
    other = Engine(mind.engine.db.root)
    mind.engine.enqueue("lifecycle_backfill", {"scope": mind.scope.model_dump()}, "background-fixture", priority=220)
    foreground_lease(mind.engine, mind.scope.key(), "phone", seconds=90)
    worker = Worker(other)
    assert worker.claim() is None
    urgent = mind.engine.enqueue("event_digest", {"scope": mind.scope.model_dump()}, "urgent-fixture", priority=20)
    assert worker.claim()["id"] == urgent
    assert Worker(other).claim() is None
    foreground_lease(mind.engine, mind.scope.key(), "phone", seconds=-1)
    assert Worker(other).claim()["priority"] > 30


def test_background_backfill_resumes_cursor_and_finishes_without_repeating(system):
    from kin_mind.lifecycle import prepare_job
    mind, memory, lifecycle, source, _ = system
    memory.configure({"event_lifecycle": False})
    src = source("historical-batch", "过去的一批合成记录")
    with mind.engine.db.connect(write=True) as conn:
        proof = memory.graph.proof(conn, [src["id"]])
        for number in range(102):
            memory.graph._put(conn, {"id": f"historical-{number:03d}", "kind": "event", "title": "历史记录",
                "basis": "explicit", "occurred_at": mind.clock(), "source_ids": [src["id"]], "evidence": proof})
    memory.configure({"event_lifecycle": True})
    with mind.engine.db.connect(write=True) as conn:
        schedule(mind.engine, conn, mind.clock())
    payload = {"scope": mind.scope.model_dump(), "cursor": ""}
    apply = prepare_job(mind.engine, {"kind": "lifecycle_backfill"}, payload)
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
        apply(conn)  # A recovered attempt cannot advance the cursor twice.
    status = lifecycle.status()["backfill"]
    assert status["processed"] == 100 and status["state"] == "pending"
    payload["cursor"] = status["cursor"]
    apply = prepare_job(mind.engine, {"kind": "lifecycle_backfill"}, payload)
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
    assert lifecycle.status()["backfill"]["processed"] == 102
    assert lifecycle.status()["backfill"]["state"] == "complete"
    with mind.engine.db.connect(write=True) as conn:
        before = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='lifecycle_backfill'").fetchone()[0]
        schedule(mind.engine, conn, mind.clock())
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='lifecycle_backfill'").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM mind_event_usage").fetchone()[0] == 0


def test_scattered_shadow_receipts_are_not_a_continuous_week(system):
    from kin_mind.lifecycle import observation_days
    mind, memory, _, _, clock = system
    started = memory.settings()["temperature_shadow_started_at"]
    clock[0] += timedelta(days=14)
    with mind.engine.db.connect(write=True) as conn:
        for offset in range(0, 14, 2):
            at = (clock[0] - timedelta(days=offset)).isoformat()
            conn.execute("INSERT INTO mind_lifecycle_runs VALUES(?,?,?,?,?,?)",
                         (mind.scope.key(), "temperature", due_slot(at, "temperature"), "fixture", "complete", json.dumps({"scheduled_at": at, "observed_at": at, "observed_day": at[:10]})))
        assert observation_days(conn, mind.scope.key(), started, mind.clock()) == 1


def test_naive_legacy_timestamp_is_warm_not_fabricated_use(system):
    mind, _, lifecycle, source, _ = system
    src = source("legacy-time", "没有时区的旧时间")
    with mind.engine.db.connect(write=True) as conn:
        rid = conn.execute("SELECT record_id FROM evidence WHERE source_id=?", (src["id"],)).fetchone()[0]
        conn.execute("UPDATE records SET data=json_set(data,'$.valid_from','2020-01-01T12:00:00') WHERE id=?", (rid,))
    apply = lifecycle.prepare_temperature()
    with mind.engine.db.connect(write=True) as conn:
        apply(conn)
        row = conn.execute("SELECT tier,data FROM mind_memory_temperature WHERE identifier=?", (rid,)).fetchone()
        assert row["tier"] == "warm" and json.loads(row["data"])["time_needs_review"]
        assert conn.execute("SELECT COUNT(*) FROM mind_event_usage").fetchone()[0] == 0


def test_model_uncertainty_cannot_be_overruled_by_matching_title(system):
    mind, memory, _, _, _ = system
    first, _, _ = route(system)
    route(system, key="another-independent", text="星图报告的另一个独立项目。")
    with mind.engine.db.connect() as conn:
        target = memory.graph.get(conn, first["event_id"])
    result, _, _ = route(system, action="append", target=target, key="ambiguous-title",
                         text="继续上次的星图报告", binding="explicit_reference", identity_decision="uncertain")
    assert result["state"] == "defer"


def test_model_can_resolve_implicit_continuation_without_title_match(system):
    _, memory, _, _, _ = system
    first, _, _ = route(system)
    target = memory.graph.detail(first["event_id"])
    result, _, _ = route(system, action="append", target=target, key="implicit-next",
                         text="嗯，红色那部分也做好了。", binding="explicit_reference")
    assert result["state"] == "append"
    assert result["identity_judgement"]["decision"] == "same_event"


def test_model_identity_cannot_cite_an_unrelated_event_source(system):
    mind, memory, lifecycle, source, _ = system
    first, _, _ = route(system)
    unrelated, _, _ = route(system, key="unrelated", text="另一场音乐会的独立记录")
    target = memory.graph.detail(first["event_id"])
    src = source("bad-prior", "已经接着做完了。")
    with mind.engine.db.connect(write=True) as conn:
        unrelated_id = next(iter(lifecycle.snapshot(conn, unrelated["event_id"])["records"]))
        proposal = EventRoute(key="bad-prior", action="append", event_id=target["id"], expected_revision=target["revision"],
                              evidence_ids=[src["id"]], binding="explicit_reference", quote="已经接着做完了。", reason="Invalid cited source",
                              identity=EventIdentityJudgement(decision="same_event", participants_match=True, object_match=True,
                                  time_compatible=True, continuation_supported=True, prior_record_ids=[unrelated_id]))
        result = lifecycle.apply_routes(conn, [proposal], memory.graph.proof(conn, [src["id"]]), "bad-prior")[0]
    assert result["state"] == "defer"


def test_semantic_identity_requires_the_cited_prior_revision(system):
    mind, memory, lifecycle, source, _ = system
    first, _, _ = route(system)
    target = memory.graph.detail(first["event_id"])
    src = source("new-continuation", "红色的那部分也做完了。")
    with mind.engine.db.connect(write=True) as conn:
        prior = next(iter(lifecycle.snapshot(conn, target["id"])["records"].values()))
        proposal = EventRoute(key="stale-prior", action="append", event_id=target["id"], expected_revision=target["revision"],
            evidence_ids=[src["id"]], binding="sourced_continuation", quote="红色的那部分也做完了。", reason="Sourced semantic continuation",
            identity=EventIdentityJudgement(decision="same_event", participants_match=True, object_match=True,
                time_compatible=True, continuation_supported=True, prior_record_ids=[prior["id"]]))
        evaluated = memory.graph.proof(conn, [src["id"], prior["id"]])
        mind.engine._save_revision(conn, {**prior, "title": "Corrected prior source"}, "concurrent-source", "Concurrent source revision")
        result = lifecycle.apply_routes(conn, [proposal], evaluated, "stale-prior")[0]
    assert result["state"] == "defer"


def test_host_semantic_deep_enables_ranking_but_keeps_auto_local(system, monkeypatch):
    from kin_mind.host import dispatch
    mind, _, _, _, _ = system
    seen = []
    def build(self, **kwargs):
        seen.append(kwargs)
        return {}
    monkeypatch.setattr(Contexts, "build", build)
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "fixture", "session_id": "fixture"}
    dispatch(config, "memory-context", {"query": "把它接着做下去呀", "purpose": "chat", "mode": "deep"})
    dispatch(config, "memory-context", {"query": "还记得上次星图吗", "purpose": "chat"})
    dispatch(config, "memory-context", {"query": "星图", "purpose": "read", "mode": "deep", "allow_model": False})
    assert seen[0]["allow_model"] is True
    assert not seen[1].get("allow_model")
    assert seen[2]["allow_model"] is False


def test_light_full_context_never_compresses_with_a_remote_model(system):
    mind, _, _, source, _ = system
    source("large-local", "星图的完整原始说明。" * 1000)
    class Forbidden:
        def structured(self, *args, **kwargs):
            pytest.fail("An explicitly local read called a remote model")
    result = Contexts(mind).build("星图", purpose="read", mode="light", budget=200, provider=Forbidden(), allow_model=True)
    assert result["model_requests"] == 0 and result["tokens"] <= 200


def test_catching_up_old_slots_does_not_fabricate_a_trial_week(system):
    from kin_mind.lifecycle import observation_days
    mind, memory, _, _, clock = system
    started = memory.settings()["temperature_shadow_started_at"]
    clock[0] += timedelta(days=7)
    with mind.engine.db.connect(write=True) as conn:
        for offset in range(7):
            scheduled = (clock[0] - timedelta(days=offset)).isoformat()
            conn.execute("INSERT INTO mind_lifecycle_runs VALUES(?,?,?,?,?,?)",
                         (mind.scope.key(), "temperature", due_slot(scheduled, "temperature"), "fixture", "complete",
                          json.dumps({"scheduled_at": scheduled, "observed_at": mind.clock(), "observed_day": mind.clock()[:10]})))
        assert observation_days(conn, mind.scope.key(), started, mind.clock()) == 1
    memory.configure({"temperature_shadow": False})
    memory.configure({"temperature_shadow": True})
    assert memory.settings()["temperature_shadow_started_at"] == mind.clock()


def test_index_only_access_never_counts_as_real_use_and_digest_is_summary(system):
    mind, memory, _, _, _ = system
    first, _, _ = route(system)
    node = memory.graph.detail(first["event_id"])
    ctx = Contexts(mind)
    assert ctx.graph_item(node)["read_depth"] == "index"
    memory.access("fixture", node["id"], node["revision"], "index", origin="user_query", usage_id="pointer")
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_event_usage").fetchone()[0] == 0
    publish(system, node["id"])
    assert ctx.graph_item(node)["read_depth"] == "summary"


def test_first_successful_ranking_can_expand_after_an_earlier_timeout(system, monkeypatch):
    import kin_mind.adaptive_recall as module
    mind, _, _, source, clock = system
    source("follow-up-anchor", "继续讨论星图的蓝色主题")
    clock[0] += timedelta(minutes=1)
    source("follow-up-neighbor", "刚才那份后来改成了橙色")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE records SET data=json_set(data,'$.attributes.role','user')")
    from eventmem.core.providers import Providers
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    lookups, original = [], module.candidates
    def lookup(engine, request, **kwargs):
        lookups.append(request.query)
        return original(engine, request, **kwargs)
    monkeypatch.setattr(module, "candidates", lookup)
    class Recovering:
        timeout = 30
        calls = 0
        def structured(self, name, schema, prompt, payload, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError()
            ordered = sorted(payload["candidates"], key=lambda r: "继续讨论星图" not in r["text"])
            return RecallRanking(ids=[r["id"] for r in ordered[:8]],
                                 followups=[RecallFollowup(candidate_id=ordered[0]["id"], direction="after")]), {}
    _, info = AdaptiveRecall(Contexts(mind)).collect("星图后来怎样了？", mode="deep", allow_model=True, provider=Recovering())
    # The deadline is retried against the same candidates, so the first answer arrives inside
    # round one and its follow-up drives the second round. Two searches, three requests.
    assert info["rounds"] == 2 and "rerank:TimeoutError" in info["degraded_reasons"]
    assert info["model_requests"] == 3
    assert lookups == ["星图后来怎样了？", "星图后来怎样了？ "]
    assert info["ranking_trace"][0]["followups"]


CROWDED_QUERY = "9月16日星图蓝色主题后来改成什么了"
CROWDED_ANSWER = "星图蓝色主题后来改成蓝紫色。"
# The leading eight of the local ranking, and of a run whose first round was reranked on retry.
CROWDED_LOCAL_TOP = ["answer", "crowd-0", "crowd-1", "crowd-2", "crowd-3", "crowd-9", "crowd-4", "crowd-5"]
CROWDED_RANKED_TOP = ["answer", "event", "crowd-26", "event", "crowd-25", "crowd-24", "event", "crowd-23"]


class DeadRanker:
    """Every rerank call hits the provider deadline."""
    timeout = 30
    def structured(self, name, schema, prompt, payload, **kwargs):
        raise TimeoutError("recall-provider-deadline")


def crowded_corpus(mind, memory, source, clock):
    """More matching turns than the candidate cap, with one dated answer among them.

    Only the answer falls inside the question's date window. The crowd arrives the next day as host
    runtime events, so each round rescores the same pool even though the query never changes.
    """
    source("answer", CROWDED_ANSWER)
    clock[0] += timedelta(days=1)
    for index in range(70):
        memory.ingest({"id": f"crowd-{index}", "kind": "owner-message", "at": mind.clock(),
                       "text": f"星图蓝色主题的第{index}条讨论，后来改成什么还没定。"})
        clock[0] += timedelta(minutes=1)
    names = {}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE records SET data=json_set(data,'$.attributes.role','user')")
        conn.execute("UPDATE records SET data=json_set(data,'$.attributes.host_event','message')")
        for row in conn.execute("SELECT data FROM records"):
            record = json.loads(row[0])
            if record["content"] == CROWDED_ANSWER:
                names[record["id"]] = "answer"
            elif record["content"].startswith("星图蓝色主题的第"):
                names[record["id"]] = "crowd-" + record["content"].split("第")[1].split("条")[0]
    return names


def test_a_rerank_that_never_answers_keeps_the_first_rounds_leading_evidence(system, monkeypatch):
    from eventmem.core.providers import Providers
    mind, memory, _, source, clock = system
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    names = crowded_corpus(mind, memory, source, clock)
    recall = AdaptiveRecall(Contexts(mind))
    local, local_info = recall.collect(CROWDED_QUERY, mode="deep", allow_model=False)
    degraded, info = recall.collect(CROWDED_QUERY, mode="deep", allow_model=True, provider=DeadRanker())
    local_ids, degraded_ids = [i["id"] for i in local], [i["id"] for i in degraded]
    # Without a model the local ranking answers the question by itself.
    assert local_info["rounds"] == 1 and local_info["model_requests"] == 0
    assert [names.get(i, "event") for i in local_ids[:8]] == CROWDED_LOCAL_TOP
    # Nothing answered and nothing new to look for: the candidates are asked about again, but
    # the same string is not searched again.
    assert info["rounds"] == 1 and info["model_requests"] == 3
    assert "rerank:TimeoutError" in info["degraded_reasons"]
    # A ranking nobody answered may not demote what the first round found without one.
    assert degraded_ids[:8] == local_ids[:8]
    # And with no ranking at all, that first local order is the whole answer.
    assert degraded_ids == local_ids


def test_a_rerank_that_never_answers_returns_the_same_order_twice(system, monkeypatch):
    from eventmem.core.providers import Providers
    mind, memory, _, source, clock = system
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    crowded_corpus(mind, memory, source, clock)
    recall = AdaptiveRecall(Contexts(mind))
    first, first_info = recall.collect(CROWDED_QUERY, mode="deep", allow_model=True, provider=DeadRanker())
    second, second_info = recall.collect(CROWDED_QUERY, mode="deep", allow_model=True, provider=DeadRanker())
    assert [i["id"] for i in first] == [i["id"] for i in second]
    assert first_info["rounds"] == second_info["rounds"] == 1


def test_a_later_successful_rerank_still_decides_the_degraded_order(system, monkeypatch):
    from eventmem.core.providers import Providers
    mind, memory, _, source, clock = system
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    names = crowded_corpus(mind, memory, source, clock)
    class Recovering:
        timeout = 30
        calls = 0
        def structured(self, name, schema, prompt, payload, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("recall-provider-deadline")
            # A distinctive order of its own: the answer, then the tail of the request backwards.
            chosen = sorted(enumerate(payload["candidates"]), key=lambda c: ("蓝紫色" not in c[1]["text"], -c[0]))
            return RecallRanking(ids=[c[1]["id"] for c in chosen[:8]]), {}
    items, info = AdaptiveRecall(Contexts(mind)).collect(CROWDED_QUERY, mode="deep", allow_model=True,
                                                        provider=Recovering())
    # The ranking that did answer still decides, whether it answered a later round or a retry
    # of the one that timed out.
    assert info["rounds"] == 2 and "rerank:TimeoutError" in info["degraded_reasons"]
    assert [names.get(i["id"], "event") for i in items[:8]] == CROWDED_RANKED_TOP


def test_a_rerank_that_fails_outside_the_deadline_leaves_the_first_round_alone(system, monkeypatch):
    from eventmem.core.providers import Providers
    mind, memory, _, source, clock = system
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    crowded_corpus(mind, memory, source, clock)
    class Broken:
        timeout = 30
        def structured(self, *a, **k):
            raise RuntimeError("provider refused the request")
    recall = AdaptiveRecall(Contexts(mind))
    local, _ = recall.collect(CROWDED_QUERY, mode="deep", allow_model=False)
    items, info = recall.collect(CROWDED_QUERY, mode="deep", allow_model=True, provider=Broken())
    # Only a deadline buys another search round; anything else ends the loop where it failed.
    assert info["rounds"] == 1 and "rerank:RuntimeError" in info["degraded_reasons"]
    assert [i["id"] for i in items] == [i["id"] for i in local]


def test_model_selects_followup_without_a_keyword_trigger(system, monkeypatch):
    import kin_mind.adaptive_recall as module
    from eventmem.core.providers import Providers
    mind, _, _, source, clock = system
    for index in range(8):
        source(f"before-{index}", f"你不要这样说了，这是之前的第{index}条")
        clock[0] += timedelta(minutes=1)
    source("color-anchor", "色卡的蓝紫色儿继续保留")
    clock[0] += timedelta(minutes=1)
    for index in range(5):
        source(f"after-{index}", f"你不要这样说了，这是之后的第{index}条")
        clock[0] += timedelta(minutes=1)
    source("color-confirmation", "最后确定用蓝紫色")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE records SET data=json_set(data,'$.attributes.role','user')")
        anchor = json.loads(conn.execute("SELECT data FROM records WHERE json_extract(data,'$.content')=?",
                                        ("色卡的蓝紫色儿继续保留",)).fetchone()[0])
        confirmation = json.loads(conn.execute("SELECT data FROM records WHERE json_extract(data,'$.content')=?",
                                              ("最后确定用蓝紫色",)).fetchone()[0])
    monkeypatch.setattr(module, "candidates", lambda *a, **k: ([anchor], {}, 0))
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    ranked_inputs = []
    class Ranker:
        timeout = 30
        def structured(self, name, schema, prompt, payload, **kwargs):
            ranked_inputs.append([r["text"] for r in payload["candidates"]])
            ordered = sorted(payload["candidates"], key=lambda r: ("最后确定" not in r["text"], "色卡" not in r["text"]))
            return RecallRanking(ids=[r["id"] for r in ordered[:8]],
                                 followups=[RecallFollowup(candidate_id=ordered[0]["id"], direction="after")]), {}
    items, info = AdaptiveRecall(Contexts(mind)).collect("把色卡那句话的上下文找完整。", mode="deep", allow_model=True, provider=Ranker())
    assert "最后确定用蓝紫色" not in ranked_inputs[0]
    assert "最后确定用蓝紫色" in ranked_inputs[1]
    assert confirmation["id"] in [i["id"] for i in items[:8]]
    assert info["rounds"] <= 3


def test_frozen_quote_matches_original_but_not_a_paraphrase(system, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("lifecycle_replay", Path(__file__).parents[1] / "scripts/lifecycle_replay.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    mind, _, _, source, _ = system
    source("note", "用户确认：颜色保持蓝色。")
    source("original", "颜色保持蓝色。")
    source("paraphrase", "她偏好蓝色。")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE records SET data=json_set(data,'$.attributes.role','user')")
        records = {json.loads(r[0])["content"]: json.loads(r[0]) for r in conn.execute("SELECT data FROM records")}
    expected = records["用户确认：颜色保持蓝色。"]["id"]
    selected = records["颜色保持蓝色。"]["id"]
    monkeypatch.setattr(AdaptiveRecall, "collect", lambda *a, **k: ([{"id": selected}], {"degraded_reasons": []}))
    case = {"id": "source-equivalence", "query": "颜色？", "expected_ids": [expected], "evidence_quotes": {expected: "颜色保持蓝色。"},
            "critical": True, "category": "correction"}
    assert replay.evaluate(mind.engine, mind.scope, [case])["critical_recall"] == 1
    selected = records["她偏好蓝色。"]["id"]
    assert replay.evaluate(mind.engine, mind.scope, [case])["critical_recall"] == 0


class FadingRanker:
    """One ranking of candidates nine to sixteen, and after that only deadlines."""
    timeout = 30

    def __init__(self, queries=()):
        self.calls, self.selected, self.queries = 0, [], list(queries)

    def structured(self, name, schema, prompt, payload, **kwargs):
        self.calls += 1
        if self.calls > 1:
            raise TimeoutError("recall-provider-deadline")
        self.selected = [c["id"] for c in payload["candidates"][8:16]]
        return RecallRanking(ids=self.selected, queries=self.queries), {}


def test_a_timeout_after_a_ranking_keeps_the_whole_selection_it_answered(system, monkeypatch):
    from eventmem.core.providers import Providers
    mind, memory, _, source, clock = system
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    crowded_corpus(mind, memory, source, clock)
    provider = FadingRanker()
    items, info = AdaptiveRecall(Contexts(mind)).collect(CROWDED_QUERY, mode="deep", allow_model=True, provider=provider)
    chosen = info["ranking_trace"][0]["selected"]
    assert len(chosen) == 8 and provider.calls > 1 and "rerank:TimeoutError" in info["degraded_reasons"]
    # All eight, in the order it gave them: unreviewed leads follow the selection, never split it.
    assert [i["id"] for i in items[:8]] == chosen


def test_a_round_offers_only_the_leads_it_kept():
    """A candidate the cap already dropped is not a lead, whatever it scores."""
    scores = {"kept-new": 5.0, "kept-old": 4.0, "pruned-new": 9.0}
    ordered = ["kept-old", "kept-new"]
    assert fresh_leads(ordered, {"kept-old"}, scores) == ["kept-new"]
    assert fresh_leads(ordered, set(), scores) == ["kept-new", "kept-old"]
    assert fresh_leads(ordered, {"kept-old", "kept-new"}, scores) == []
    assert fresh_leads(["a", "b", "c"], set(), {"a": 1.0, "b": 3.0, "c": 2.0}, limit=2) == ["b", "c"]
