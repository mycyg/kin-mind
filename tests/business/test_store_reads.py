"""Reads that leave the store as they found it.

Opening a store that is already current takes no write lock, and health names the structure it
has (E2-08). A recall without a session writes nothing however often it is retried; one that
names its session is a use as before (S1-02). A pre-action cue with nothing to recall answers
empty (E3-19). The delivery inbox closes every connection it opens (E3-21)."""
import sqlite3
import time

from fastapi.testclient import TestClient

from eventmem.core import Engine
from eventmem.core.api import create_app
from eventmem.core.db import structure_digest
from eventmem.core.hosts import handle
from eventmem.core.models import Scope, SourceInput
from eventmem.sdk import DeliveryInbox

from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

HEADERS = {"Authorization": "Bearer synthetic-reads"}
KIN = Scope(project="personal", persona="Kin")
# What the console's recall lab sends: no session, the default purpose.
LAB = {"query": "数据库迁移", "scenario": "tool", "mode": "fast", "budget": 2000, "history": False, "explain": True}
TABLES = ("mind_memory_access", "mind_event_usage", "mind_foreground_leases", "feedback")


def test_opening_a_current_store_takes_no_write_lock(tmp_path):
    Engine(tmp_path / "db")
    holder = sqlite3.connect(tmp_path / "db" / "memory.sqlite3", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        engine = Engine(tmp_path / "db")  # a writer is busy: opening the store must not wait for it
        assert time.monotonic() - started < 5
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    with engine.db.connect() as conn:
        assert {r[0]: r[1] for r in conn.execute("SELECT key,value FROM meta WHERE key IN ('generation','schema_version')")} \
            == {"generation": 0, "schema_version": 1}
        assert conn.execute("PRAGMA foreign_key_list(jobs)").fetchall() == []
        shape = structure_digest(conn)
    client = TestClient(create_app(engine=engine, token="synthetic-reads", workers=False, mcp_enabled=False))
    assert client.get("/v1/health", headers=HEADERS).json()["schema"] == shape


def kin_store(tmp_path):
    engine = Engine(tmp_path / "store")
    for i in range(3):
        engine.receive(SourceInput(namespace="contract", key=str(i), scope=KIN, kind="knowledge", title=f"迁移记录 {i}",
                                   authority="explicit", text=f"数据库迁移第{i}次在隔离目录完成，恢复检查通过。"))
    evidence = engine.source(engine.receive(SourceInput(namespace="contract", key="evidence", scope=KIN,
                                                        text="合成证据", authority="explicit"))["id"])
    mind = Mind(engine, KIN)
    mind.initialize(agent_version="reads-v1", evidence_ids=[evidence["record_ids"][0]])
    MemoryContinuity(mind).configure({"records": True, "context": True, "usage_reinforcement": True,
                                      "temperature_shadow": True, "event_lifecycle": True})
    return engine, TestClient(create_app(engine=engine, token="synthetic-reads", workers=False, mcp_enabled=False))


def counts(engine):
    with engine.db.connect() as conn:
        found = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
        found["memory_metrics"] = conn.execute("SELECT COUNT(*) FROM metrics WHERE name NOT LIKE 'model\\_%' ESCAPE '\\'").fetchone()[0]
    return found


def test_a_recall_without_a_session_writes_nothing(tmp_path):
    engine, client = kin_store(tmp_path)
    before = counts(engine)
    for _ in range(2):  # the owner retries after an error banner
        answer = client.post("/v1/recall", headers=HEADERS, json={**LAB, "scope": KIN.model_dump()})
        assert answer.status_code == 200, answer.text[:300]
        assert answer.json()["index"]
    listed = client.get("/v1/memories", headers=HEADERS, params=KIN.model_dump()).json()["items"]
    assert client.get(f"/v1/memories/{listed[0]['id']}", headers=HEADERS).status_code == 200
    assert counts(engine) == before
    # Named by its session, the same recall is a use of the memory, as before.
    client.post("/v1/recall", headers=HEADERS, json={**LAB, "scope": KIN.model_dump(), "session": "s1"})
    after = counts(engine)
    assert after["mind_memory_access"] > before["mind_memory_access"] and after["memory_metrics"] > before["memory_metrics"]


def test_a_pre_action_cue_with_nothing_to_recall_answers_empty(tmp_path):
    engine = Engine(tmp_path / "db")
    payload = {"session_id": "s1", "tool_name": "shell", "tool_input": {"command": "ls"}}
    assert handle(engine, "pre_action", payload, receipt_only=True) == {}
    assert handle(engine, "pre_action", {**payload, "memory_context_managed": True}) == {}


class Tracked(sqlite3.Connection):
    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False
        Tracked.opened.append(self)

    def close(self):
        self.closed = True
        super().close()


def test_the_delivery_inbox_closes_what_it_opens(tmp_path, monkeypatch):
    from eventmem import sdk

    real = sqlite3.connect
    monkeypatch.setattr(sdk.sqlite3, "connect", lambda *args, **kwargs: real(*args, factory=Tracked, **kwargs))
    Tracked.opened = []
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite3")
    assert inbox.accept({"id": "d1"}, lambda conn, delivery: None)
    assert not inbox.accept({"id": "d1"}, lambda conn, delivery: None)
    assert len(Tracked.opened) == 3 and all(conn.closed for conn in Tracked.opened)
