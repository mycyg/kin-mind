import json

import os

from pathlib import Path

import socket

import subprocess

import sys

import threading

import time

import httpx

import pytest

import uvicorn

from eventmem.core import Engine, SourceInput

from eventmem.core.api import create_app

from eventmem.sdk import Client, DeliveryInbox

from eventmem.core.jobs import Worker
from eventmem.core.models import RecordInput

@pytest.fixture
def service(tmp_path):
    engine = Engine(tmp_path / "service")
    (engine.db.root / "local-token").write_text("test-protocol")
    app = create_app(engine=engine, token="test-protocol", workers=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started
    yield engine, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)

def test_python_typescript_http_cli_share_contract(service):
    engine, url = service
    with Client(url, token="test-protocol") as client:
        client.receive_source(
            SourceInput(namespace="sdk", key="1", text="One shared contract")
        )
        python = client.recall({"query": "shared"})
        http = httpx.post(
            url + "/v1/recall",
            json={"query": "shared"},
            headers={"Authorization": "Bearer test-protocol"},
        ).json()
        assert python["items"] == http["items"] and python["text"] == http["text"]
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "eventmem.cli",
                "api",
                "POST",
                "/v1/recall",
                "--url",
                url,
                "--root",
                str(engine.db.root),
                "--json",
                '{"query":"shared"}',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(cli.stdout)["items"] == http["items"]
        module = Path(__file__).parents[2] / "sdk/typescript/dist/index.js"
        if module.exists():
            script = (
                "import {Client} from "
                + json.dumps(module.as_uri())
                + "; const c=new Client(process.argv[1], 'test-protocol'); console.log(JSON.stringify(await c.call('recall',{body:{query:'shared'}})));"
            )
            node = subprocess.run(
                ["node", "--input-type=module", "-e", script, url],
                capture_output=True,
                text=True,
                check=True,
            )
            assert json.loads(node.stdout)["items"] == http["items"]

def test_delivery_sdk_effect_is_transactional_and_deduplicated(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite3")

    def effect(conn, delivery):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,text TEXT)"
        )
        conn.execute(
            "INSERT INTO messages VALUES(?,?)", (delivery["id"], delivery["text"])
        )

    delivery = {"id": "stable-delivery", "text": "One reminder"}
    assert inbox.accept(delivery, effect)
    assert not DeliveryInbox(tmp_path / "inbox.sqlite3").accept(delivery, effect)

def test_sdk_contract_types_and_environment_root(tmp_path, monkeypatch):
    import httpx
    from eventmem.sdk import Scope as ScopeDict

    assert ScopeDict(project="sample") == {"project": "sample"}
    (tmp_path / "local-token").write_text("private-local-fixture")
    monkeypatch.setenv("EVENTMEM_HOME", str(tmp_path))
    seen = []

    def request(req):
        seen.append(req.headers["Authorization"])
        return httpx.Response(200, json={"status": "ok"})

    with Client(transport=httpx.MockTransport(request)) as client:
        assert client.health()["status"] == "ok"
    assert seen == ["Bearer private-local-fixture"]


def test_source_versions_stay_current_through_http(service, monkeypatch):
    engine, url = service
    with Client(url, token="test-protocol") as client:
        source = SourceInput(namespace="sdk", key="milestone", text="Milestone pending",
                             occurred_at="2026-01-01T00:00:00Z")
        old = client.receive_source(source)
        old_root = engine.source(old["id"])["record_ids"][0]
        independent = client.receive_source(SourceInput(namespace="sdk", key="other", text="Independent evidence"))
        derived = engine.add_record(RecordInput(
            kind="fact", content="Milestone interim conclusion", generated=True, confirmation="verified",
            source_ids=[old["id"], independent["id"]],
        ), "derived")
        current = client.receive_source(source.model_copy(update={
            "version": "2", "text": "Milestone completed", "kind": "fact",
            "occurred_at": "2026-01-02T00:00:00Z",
        }))
        # A delayed older version cannot become current merely by arriving last.
        late = client.receive_source(source.model_copy(update={"version": "0"}))
        ids = {s["id"]: engine.source(s["id"])["record_ids"][0] for s in (old, current, late)}
        ids[old["id"]] = old_root
        assert engine.get(ids[old["id"]])["status"] == "superseded"
        assert engine.get(ids[late["id"]])["status"] == "superseded"
        assert engine.get(derived["id"])["status"] == "unverified"
        recalled = client.recall({"query": "Milestone"})["items"]
        assert {r["id"] for r in recalled} == {ids[current["id"]]}
        history = client.recall({"query": "Milestone", "history": True})["items"]
        assert set(ids.values()) <= {r["id"] for r in history}
        # Simulate a store written before supersession covered observations.
        with monkeypatch.context() as legacy:
            legacy.setattr(engine, "supersede_source_versions", lambda *args: None)
            earlier = client.receive_source(source.model_copy(update={"key": "legacy"}))
            latest = client.receive_source(source.model_copy(update={
                "key": "legacy", "version": "2", "text": "Milestone delivered",
                "occurred_at": "2026-01-02T00:00:00Z",
            }))
        earlier_id = engine.source(earlier["id"])["record_ids"][0]
        latest_id = engine.source(latest["id"])["record_ids"][0]
        recalled = {r["id"] for r in client.recall({"query": "Milestone"})["items"]}
        assert latest_id in recalled and earlier_id not in recalled
        assert engine.get(earlier_id)["status"] == "active"  # Read-only filtering.
        assert earlier_id in {r["id"] for r in client.recall({"query": "Milestone", "history": True})["items"]}
        engine.receive(source.model_copy(update={"key": "legacy", "version": "3",
                       "occurred_at": "2026-01-03T00:00:00Z"}), attachment=b"Milestone awaiting parser")
        assert latest_id in {r["id"] for r in client.recall({"query": "Milestone"})["items"]}
        checkpoints = [client.receive_source(SourceInput(namespace="sdk", key="shared-checkpoint",
            version=str(i), session=f"session-{i}", kind="checkpoint", text=f"Checkpoint session {i}"))
            for i in (1, 2)]
        client.receive_source(SourceInput(namespace="sdk", key="shared-checkpoint", version="3",
                                          text="An ordinary source with the same key"))
        checkpoint_ids = {engine.source(s["id"])["record_ids"][0] for s in checkpoints}
        assert checkpoint_ids <= {r["id"] for r in client.recall({"query": "Checkpoint"})["items"]}


def test_invalid_maintenance_settings_keep_last_valid_configuration(service):
    engine, url = service
    valid = {"interval_seconds": 60, "organize_batch": 20, "narratives": False}
    headers = {"Authorization": "Bearer test-protocol"}
    assert httpx.put(url + "/v1/settings/maintenance", headers=headers, json=valid).status_code == 200
    for invalid in ({"interval_seconds": "invalid"}, {"interval_seconds": True},
                    {"interval_seconds": 0}, {"organize_batch": "20"},
                    {"narratives": "false"}):
        response = httpx.put(url + "/v1/settings/maintenance", headers=headers, json=invalid)
        assert response.status_code == 422
        assert engine.settings("maintenance") == valid
    with pytest.raises(ValueError):
        engine.settings("maintenance", {"interval_seconds": float("nan")})
    Worker(engine).schedule_maintenance()
