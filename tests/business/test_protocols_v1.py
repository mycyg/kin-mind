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
