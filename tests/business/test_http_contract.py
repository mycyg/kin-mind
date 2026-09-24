"""What the console and the SDKs are promised over HTTP: the published contract is the one the
service answers with, and a console read changes nothing it reads (S1-01, S1-02, S1-10, E3-21)."""

import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from eventmem.core import Engine, SourceInput
from eventmem.core.api import create_app
from eventmem.core.models import Scope

HEADERS = {"Authorization": "Bearer contract"}
KIN = Scope(project="personal", persona="Kin")
# Exactly what the console's recall lab sends: no session, the default purpose.
LAB = {"query": "数据库迁移", "scenario": "tool", "mode": "fast", "budget": 2000,
       "history": False, "explain": True}
SIDE_EFFECTS = ("mind_memory_access", "mind_event_usage", "mind_foreground_leases", "feedback")


def test_generated_contract_and_sdk_tables_are_current():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from generate_contract import stale

    found = stale()
    assert not found, "stale generated files, run python scripts/generate_contract.py: " + ", ".join(found)


def store(tmp_path, scope, **features):
    engine = Engine(tmp_path / "store")
    for i in range(3):
        engine.receive(SourceInput(namespace="contract", key=str(i), scope=scope, kind="knowledge",
                                   title=f"迁移记录 {i}", authority="explicit",
                                   text=f"数据库迁移第{i}次在隔离目录完成，恢复检查通过。"))
    if features:
        from kin_mind.memory import MemoryContinuity
        from kin_mind.state import Mind

        evidence = engine.source(engine.receive(SourceInput(
            namespace="contract", key="evidence", scope=scope, text="合成证据", authority="explicit"))["id"])
        mind = Mind(engine, scope)
        mind.initialize(agent_version="contract-v1", evidence_ids=[evidence["record_ids"][0]])
        MemoryContinuity(mind).configure(features)
    client = TestClient(create_app(engine=engine, token="contract", workers=False, mcp_enabled=False),
                        raise_server_exceptions=False)
    return engine, client


def counts(engine):
    with engine.db.connect() as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in SIDE_EFFECTS}


def published(client, method, path):
    """The response model a route declares: the contract and both SDKs are generated from it."""
    route = next(r for r in client.app.routes if isinstance(r, APIRoute) and r.path == path and method in r.methods)
    return TypeAdapter(route.response_model)


def conforms(client, scope):
    recall = client.post("/v1/recall", headers=HEADERS, json={**LAB, "scope": scope.model_dump()})
    assert recall.status_code == 200, recall.text[:300]
    published(client, "POST", "/v1/recall").validate_python(recall.json())
    listed = client.get("/v1/memories", headers=HEADERS, params=scope.model_dump()).json()["items"]
    record = client.get(f"/v1/memories/{listed[0]['id']}", headers=HEADERS)
    assert record.status_code == 200, record.text[:300]
    published(client, "GET", "/v1/memories/{record_id}").validate_python(record.json())


def test_plain_scope_recall_and_read_follow_the_contract(tmp_path):
    _, client = store(tmp_path, Scope())
    conforms(client, Scope())


@pytest.mark.xfail(strict=True, reason="S1-01/E3-20: the kin context branch answers outside the model /v1/recall "
                   "publishes (500); the service side is WS6's. Remove this mark when it lands.")
def test_kin_scope_recall_and_read_follow_the_contract(tmp_path):
    _, client = store(tmp_path, KIN, records=True, context=True)
    conforms(client, KIN)


@pytest.mark.xfail(strict=True, reason="S1-02: a recall without a session still writes mind_memory_access and "
                   "mind_event_usage rows (and leases the foreground); the service side is WS6's. Remove this mark "
                   "when it lands.")
def test_a_console_recall_changes_nothing_it_reads(tmp_path):
    engine, client = store(tmp_path, KIN, records=True, context=True, usage_reinforcement=True,
                           temperature_shadow=True, event_lifecycle=True)
    before = counts(engine)
    for _ in range(2):  # the owner retries after an error banner
        client.post("/v1/recall", headers=HEADERS, json={**LAB, "scope": KIN.model_dump()})
    listed = client.get("/v1/memories", headers=HEADERS, params=KIN.model_dump()).json()["items"]
    client.get(f"/v1/memories/{listed[0]['id']}", headers=HEADERS)
    assert counts(engine) == before
