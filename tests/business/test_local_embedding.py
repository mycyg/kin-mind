"""The local embedding service and its client (E2-13, DB1-11): a request reads the credential
file instead of locking and asking for health first, a replaced credential is honoured at once,
the model is never downloaded, an index labelled for another model revision is refused, and a
local embedding leaves no "usage unknown" telemetry."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from eventmem.core import Engine
from eventmem.core import local_embedding, providers


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
