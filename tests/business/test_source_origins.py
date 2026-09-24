"""The supplementary origin of a source (K4-12, K4-13, K4-14, K4-16, E3-06, T-16): six kinds over
the existing source identity, one table read by recall, the graph's writer and the evidence
predicates alike."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.models import RecallRequest, Scope, SourceInput
from eventmem.core.read_policy import (
    ORIGIN_KINDS,
    ORIGINS_FILE,
    ReadPolicy,
    host_maintenance,
    origin_of,
)

from kin_mind import isolation_migration
from kin_mind.evidence_classes import never_evidence, owner_statement
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 21, 3, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, Scope(persona="synthetic-origins"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)

    def source(namespace, key, text, *, authority="model", metadata=None):
        clock[0] += timedelta(seconds=1)
        return engine.receive(SourceInput(namespace=namespace, key=key, text=text, scope=mind.scope,
                                          authority=authority, occurred_at=mind.clock(), extract=False,
                                          metadata=metadata or {}))["id"]

    mind.initialize(agent_version="fixture-v1",
                    evidence_ids=[source("kin-owner-input", "initial", "Initial owner setup", authority="explicit",
                                         metadata={"role": "user", "host_event": "message"})])
    memory.configure({"graph": True})
    return mind, memory, source, clock


def root(engine, sid):
    with engine.db.connect() as conn:
        return json.loads(conn.execute("SELECT data FROM records WHERE json_extract(data,'$.source_ids[0]')=?",
                                       (sid,)).fetchone()[0])


def test_the_table_is_exported_and_names_six_kinds():
    table = json.loads(ORIGINS_FILE.read_text())
    assert set(table["kinds"]) == {"host_maintenance", "synthetic_example", "configuration", "kin_thought",
                                   "observation", "external_excerpt"}
    assert {kind: ORIGIN_KINDS[kind][0] for kind in ("host_maintenance", "synthetic_example", "configuration")} == {
        "host_maintenance": "host_envelope", "synthetic_example": "synthetic_example",
        "configuration": "role_configuration"}
    assert all(ORIGIN_KINDS[kind][0] == "experience" for kind in ("kin_thought", "observation", "external_excerpt"))
    assert origin_of("kin-session-maintenance") == "host_maintenance"
    assert origin_of("mind-internal-event:follow-up") == "host_maintenance"
    assert origin_of("anything", {"maintenance_only": True}) == "host_maintenance"
    assert origin_of("kin-owner-input") is None


def test_each_origin_reads_as_its_kind_and_maintenance_stays_out_of_recall(system):
    mind, memory, source, clock = system
    engine = mind.engine
    owner = source("kin-owner-input", "owner", "我们周末去看海吧", authority="explicit",
                   metadata={"role": "user", "host_event": "message"})
    maintenance = source("kin-session-maintenance", "review-1",
                         "宿主请求检查当前原生会话。依据 session_context 判断压缩与接续；此事件不是用户消息。",
                         authority="operation", metadata={"session_snapshot_id": "snap", "maintenance_only": True})
    internal = source("mind-internal-event", "tick-1", '{"kind": "clock"}',
                      metadata={"role": "assistant", "internal": True})
    thought = source("kin-reflection", "diary-1", "小Kin自己琢磨的：看海那天也许会下雨",
                     metadata={"role": "assistant", "basis": "internal_thought", "internal": True})
    explored = source("kin-exploration", "explore-1", '{"result": "海边的潮汐表显示周六早上退潮"}',
                      metadata={"host_event": "exploration-result"})
    page = source("kin-web-observation", "page-1", '{"excerpt": "潮汐预报：周六 06:12 低潮"}', authority="document",
                  metadata={"host_event": "web-observation"})
    policy = ReadPolicy.load(engine, mind.scope, "experience_recall")
    audit = ReadPolicy.load(engine, mind.scope, "audit")
    records = {name: root(engine, sid) for name, sid in (("owner", owner), ("maintenance", maintenance),
               ("internal", internal), ("thought", thought), ("explored", explored), ("page", page))}
    assert policy.visible(records["owner"]) and policy.label(records["owner"]) is None
    for name in ("maintenance", "internal"):
        assert not policy.visible(records[name]) and audit.visible(records[name])
        assert policy.classify(records[name]).kind == "host_envelope"
    assert policy.label(records["thought"]) == "kin_thought" and policy.visible(records["thought"])
    assert policy.prefix(records["thought"]) == "[kin_thought] "
    assert policy.label(records["explored"]) == "observation"
    assert policy.label(records["page"]) == "external_material"
    # Kin's thought, an observation and a web page are never the owner speaking.
    assert owner_statement(records["owner"], policy)
    assert not any(owner_statement(records[name], policy) for name in ("thought", "explored", "page"))
    # The maintenance note is kept, and nothing reads it as memory: not indexed, not queued.
    with engine.db.connect() as conn:
        indexed = {row[0] for row in conn.execute("SELECT id FROM search")}
        dirty = {row[0] for row in conn.execute("SELECT record_id FROM dirty")}
        embedded = {json.loads(row[0]).get("record_id") for row in conn.execute("SELECT payload FROM jobs WHERE kind='embed'")}
    assert records["maintenance"]["id"] not in indexed | dirty | embedded
    assert records["owner"]["id"] in indexed and records["owner"]["id"] in embedded
    found = engine.recall(RecallRequest(query="宿主请求检查当前原生会话 session_context", scope=mind.scope, limit=40))
    assert records["maintenance"]["id"] not in json.dumps(found, ensure_ascii=False)


def test_one_definition_of_an_internal_event():
    assert never_evidence({"namespace": "mind-internal-event"})
    assert never_evidence({"namespace": "mind-internal-event:follow-up"})
    assert never_evidence({"namespace": "kin-session-maintenance"})
    assert never_evidence({"namespace": "kin.desktop_receipts"})
    assert never_evidence({"namespace": "host:wechat", "metadata": {"maintenance_only": True}})
    assert not never_evidence({"namespace": "kin-reflection"})
    assert not never_evidence({"namespace": "kin-owner-input"})
    assert host_maintenance({"namespace": "kin-session-maintenance"}) is True


def test_the_graph_never_mixes_experience_with_examples(system):
    mind, memory, source, clock = system
    owner = source("kin-owner-input", "owner-walk", "我每天晚上散步", authority="explicit",
                   metadata={"role": "user", "host_event": "message"})
    example = source("synthetic-example", "example-walk", "示例：用户每天晚上散步", authority="explicit")
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [owner, example])
        node = memory.graph._put(conn, {"id": "walks", "kind": "event", "title": "Evening walks", "text": "",
                                        "source_ids": [owner, example], "evidence": refs, "basis": "explicit"})
        alone = memory.graph._put(conn, {"id": "example-only", "kind": "event", "title": "Example", "text": "",
                                         "source_ids": [example], "evidence": memory.graph.proof(conn, [example]),
                                         "basis": "explicit"})
    assert [ref["source_id"] for ref in node["evidence"]] == [owner] and node["source_ids"] == [owner]
    assert [ref["source_id"] for ref in node["configuration_evidence"]] == [example]
    # An item that stands on nothing but an example keeps it, and reads as what it is.
    assert [ref["source_id"] for ref in alone["evidence"]] == [example]
    policy = ReadPolicy.load(mind.engine, mind.scope, "experience_recall")
    assert policy.node_visible(node) and not policy.node_visible(alone)


def test_a_stalled_isolation_migration_is_finished_on_the_maintenance_tick(system):
    mind, memory, source, clock = system
    engine = mind.engine
    source("kin-session-maintenance", "old-review", "宿主请求检查当前原生会话。", authority="operation")
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",
                     (mind.scope.key(), isolation_migration.MIGRATION, 0,
                      json.dumps({"state": "classified", "mixed_nodes_refused": [{"id": "x"}]})))
        before = isolation_migration.status(conn, mind.scope.key())
    assert before["strict"] and before["refused"] == 1
    assert ReadPolicy.load(engine, mind.scope).strict
    ran = isolation_migration.resume_stalled(engine, now=10_000)
    assert ran == [mind.scope.key()]
    with engine.db.connect() as conn:
        after = isolation_migration.status(conn, mind.scope.key())
    assert after["state"] == "complete" and not after["strict"] and after["auto_retries"] == 1
    assert not ReadPolicy.load(engine, mind.scope).strict
    # Settled: the tick leaves it alone from now on.
    assert isolation_migration.resume_stalled(engine, now=20_000) == []
