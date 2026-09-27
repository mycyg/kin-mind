"""The local embedding service and its client (E2-13, DB1-11): a request reads the credential
file instead of locking and asking for health first, a replaced credential is honoured at once,
the model is never downloaded, an index labelled for another model revision is refused, and a
local embedding leaves no "usage unknown" telemetry. Whoever wakes the service, it finds the code
it runs, and a service that cannot start leaves its embed jobs waiting with the reason, not failed
for good as `RuntimeError` (OPS-01)."""
import json
import os
import signal
import socket
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from eventmem.core import Engine
from eventmem.core import local_embedding, providers
from eventmem.core.jobs import Worker
from eventmem.core.models import Scope, SourceInput


class Model:
    def encode(self, texts, **kwargs):
        import numpy as np

        return np.ones((len(texts), 4))


def test_a_replaced_credential_is_honoured_without_a_restart(tmp_path):
    app = local_embedding.create_app(tmp_path, loader=Model)
    client = TestClient(app)
    old = (tmp_path / "embedding-token").read_text()
    body = {"model": local_embedding.MODEL, "input": "hello", "dimensions": 32}
    assert client.post("/v1/embeddings", json=body, headers={"Authorization": "Bearer " + old}).status_code == 200
    (tmp_path / "embedding-token").write_text("rotated-synthetic-token")
    assert client.post("/v1/embeddings", json=body, headers={"Authorization": "Bearer " + old}).status_code == 401
    answer = client.get("/health", headers={"Authorization": "Bearer rotated-synthetic-token"})
    assert answer.status_code == 200 and answer.json()["model_revision"] == local_embedding.MODEL_REVISION
    assert "revision" in answer.json()["source"]


def test_the_model_is_never_downloaded(tmp_path, monkeypatch):
    """With the pinned revision not complete in the cache, loading fails and says so."""
    pytest.importorskip("sentence_transformers")
    from huggingface_hub.errors import LocalEntryNotFoundError

    def missing(*args, **kwargs):
        raise LocalEntryNotFoundError("not cached")

    monkeypatch.setattr("huggingface_hub.snapshot_download", missing)
    client = TestClient(local_embedding.create_app(tmp_path))
    auth = {"Authorization": "Bearer " + (tmp_path / "embedding-token").read_text()}
    answer = client.post("/v1/embeddings", json={"model": local_embedding.MODEL, "input": "x"}, headers=auth)
    assert answer.status_code == 503 and "RuntimeError" in answer.json()["detail"]


def test_an_index_of_another_revision_is_refused():
    local_embedding.check_preprocessing(None)
    local_embedding.check_preprocessing("text-v1")
    local_embedding.check_preprocessing(f"qwen3-{local_embedding.REVISION_MARK}-normalized")
    with pytest.raises(ValueError):
        local_embedding.check_preprocessing("qwen3-0123abcd-normalized")


class Transport:
    """The client side of one local embedding call: a refusal first, as after a credential
    change, then an answer without usage, as the local service gives."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        transport = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, **kwargs):
                transport.calls.append(headers["Authorization"])
                if len(transport.calls) == 1:
                    return httpx.Response(401, json={"detail": "Authentication required"})
                return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.5]}]})

        return Client()


def test_the_client_reads_the_credential_again_once_and_records_no_unknown_usage(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "db")
    engine.settings("models", {"embedding": {"endpoint": "http://127.0.0.1:8321/v1", "model": local_embedding.MODEL,
                                             "local_embedding": True, "dimensions": 2}})
    (engine.db.root / "embedding-token").write_text("synthetic-token")

    def never(*args, **kwargs):
        raise AssertionError("no wake-up for a service that answers")

    monkeypatch.setattr(local_embedding, "ensure_started", never)
    transport = Transport()
    monkeypatch.setattr(providers.httpx, "Client", transport)
    vectors, index = providers.Providers(engine).embed(["hello"])
    assert vectors == [[0.5, 0.5]] and transport.calls == ["Bearer synthetic-token"] * 2
    with engine.db.connect() as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM metrics")]
    assert "model_usage_unknown" not in names and "model_ms" in names


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_service_woken_by_a_process_without_the_package_on_its_path_still_finds_it(tmp_path, monkeypatch):
    """The memory service under launchd put the source root on its own `sys.path` only: the service it
    woke was started with no `PYTHONPATH`, in the host's directory, and ended at once with `No module
    named 'eventmem'` -- no vector was made from then on (OPS-01). A real start, from such a process."""
    pytest.importorskip("uvicorn")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    port, root = free_port(), tmp_path / "root"
    auth = local_embedding.ensure_started(root, f"http://127.0.0.1:{port}/v1", local_embedding.MODEL)
    with httpx.Client(timeout=5, trust_env=False) as client:
        health = client.get(f"http://127.0.0.1:{port}/health", headers={"Authorization": "Bearer " + auth}).json()
    try:
        assert health["service"] == "memorypalace-embedding" and health["loaded"] is False
        assert health["source"]["root"] == str(local_embedding.source_root()), "the code of the process that woke it"
    finally:
        os.kill(health["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(health["pid"], 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)


def test_the_service_is_started_from_its_own_source_root_first_on_the_path():
    source = str(local_embedding.source_root())
    command, env, cwd = local_embedding.service_launch("/r", 8321, environ={
        "PATH": "/bin", "KIN_WORKER_MARK": "mark", "PYTHONPATH": os.pathsep.join(["/elsewhere", source])})
    assert command[1:] == ["-m", "eventmem.core.local_embedding", "--root", "/r", "--port", "8321"]
    assert env["PYTHONPATH"].split(os.pathsep) == [source, "/elsewhere"], "first, once, and nothing inherited lost"
    assert cwd == source and env["PATH"] == "/bin" and "KIN_WORKER_MARK" not in env
    assert local_embedding.service_launch("/r", 1, environ={})[1]["PYTHONPATH"] == source
    assert (local_embedding.source_root() / "eventmem" / "core" / "local_embedding.py").is_file()


def test_a_start_that_failed_says_why_from_what_the_service_wrote_this_time(tmp_path):
    log = tmp_path / "embedding-service.log"
    log.write_text("python: Error while finding module specification for 'x' (ModuleNotFoundError: No module named 'old')\n")
    since = log.stat().st_size
    with log.open("a") as out:
        out.write("Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'fastapi'\n")
    assert local_embedding.startup_failure(log, since, 1) == (
        "Local embedding service exited during startup (exit 1, ModuleNotFoundError: No module named 'fastapi')")
    assert local_embedding.startup_failure(log, log.stat().st_size, 78) == "Local embedding service exited during startup (exit 78)"
    assert local_embedding.startup_failure(tmp_path / "missing.log", 0, 1).endswith("(exit 1)")


def test_an_embed_job_whose_service_cannot_start_waits_with_the_reason_and_is_not_charged(tmp_path, monkeypatch):
    """34 embed jobs failed for good as `RuntimeError` while the service could not start (OPS-01). A
    service that cannot be woken is an environment that is down: the job keeps the reason and waits,
    and runs once the service answers."""
    engine = Engine(tmp_path / "db")
    engine.settings("models", {"embedding": {"endpoint": f"http://127.0.0.1:{free_port()}/v1", "model": local_embedding.MODEL,
                                             "local_embedding": True, "dimensions": 2}})

    def popen(argv, stdout=None, **kwargs):
        stdout.write(b"python: Error while finding module specification for 'eventmem.core.local_embedding'"
                     b" (ModuleNotFoundError: No module named 'eventmem')\n")
        stdout.flush()
        return SimpleNamespace(poll=lambda: 1)

    monkeypatch.setattr(local_embedding, "subprocess", SimpleNamespace(Popen=popen, DEVNULL=-3))
    engine.receive(SourceInput(namespace="synthetic", key="k", text="words", scope=Scope(persona="embed"),
                               authority="explicit", occurred_at=datetime.now(timezone.utc).isoformat(), extract=False))
    worker = Worker(engine)
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET available=0 WHERE kind!='embed'")
    while worker.run_once():
        pass
    with engine.db.connect() as conn:
        embed = dict(conn.execute("SELECT * FROM jobs WHERE kind='embed'").fetchone())
    assert embed["state"] == "retry" and embed["attempts"] == 0, embed
    assert embed["error"] == ("Local embedding service exited during startup (exit 1, "
                              "ModuleNotFoundError: No module named 'eventmem')")
    assert "embed" in worker.paused
