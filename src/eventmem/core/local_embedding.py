"""Opt-in local Qwen embedding endpoint with on-demand, process-safe recovery."""

import argparse
import fcntl
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .providers import ProviderError

MODEL = "Qwen/Qwen3-Embedding-0.6B"
MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
# The index a vector belongs to is named partly by its preprocessing label; a label that names
# a Qwen3 revision must name this one, or vectors of two models would share an index (E2-13).
REVISION_MARK = MODEL_REVISION[:8]


def check_endpoint(endpoint, model):
    """The only local embedding endpoint there is: loopback, /v1, this model."""
    address = urlsplit(endpoint)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or address.path != "/v1"
        or address.query
        or address.fragment
        or address.username
        or address.password
        or model != MODEL
    ):
        raise ValueError(
            "Local embedding requires Qwen3-Embedding-0.6B at http://127.0.0.1:PORT/v1"
        )
    return address.port or 80


def check_preprocessing(label):
    """A preprocessing label that names a Qwen3 revision must name the one this service loads."""
    if label and "qwen3" in label.lower() and REVISION_MARK not in label:
        raise ValueError("The embedding index names another Qwen3 revision than the local service loads")


class LocalEmbeddingUnavailable(ProviderError):
    """The local service could not be woken: it exited while it started, never answered, or its port
    answers for something else. The message names that, with the exit status and the error the
    service's own log ended on -- never a word of any request -- so a job keeps it as it is (a
    `ProviderError` is safe for durable diagnostics), and for the embed jobs, which call no paid
    model, the queue takes it for an environment that is down rather than for the item: the job
    waits and runs again, instead of failing for good while nothing can start the service."""


def source_root():
    """The directory this `eventmem` is imported from: the one the woken service imports it from."""
    return Path(__file__).resolve().parents[2]


def service_launch(root, port, environ=None):
    """The command, environment and working directory the service is started with.

    Whoever asks first wakes the service -- the memory service's workers, the host's mind worker, a
    hook, the MCP server -- and they did not all have `eventmem` on `PYTHONPATH`: one that put the
    source root on its own `sys.path` handed the child an environment in which `-m` found nothing,
    and every wake-up from it ended in `No module named 'eventmem'` (the memory service under
    launchd). So the child is told where the code is: the source root this module came from goes
    first on its `PYTHONPATH`, ahead of whatever the caller had there, and it starts in that
    directory. A shared service outlives whoever woke it: it is no execution's, so an execution's
    mark (kin_mind.worker_groups) is not handed on to it, and the end of that execution does not
    end it (CR5-MM-03). Everything else is inherited as it is."""
    source = str(source_root())
    env = {key: value for key, value in (os.environ if environ is None else environ).items()
           if key != "KIN_WORKER_MARK"}
    inherited = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry and entry != source]
    env["PYTHONPATH"] = os.pathsep.join([source, *inherited])
    command = [sys.executable, "-m", "eventmem.core.local_embedding", "--root", str(root), "--port", str(port)]
    return command, env, source


# The last error a Python process names in its log: its class and the first words of its message,
# up to a parenthesis or the end of the line.
_NAMED_ERROR = re.compile(r"\b([A-Z]\w*(?:Error|Exception)): ([^\n()]{1,120})")


def startup_failure(log_path, since, code):
    """Why the service ended while it started, as a job keeps it: the exit status and the last error
    its log named after `since` (the log's size before the start), in a line."""
    try:
        with open(log_path, "rb") as log:
            log.seek(since)
            tail = log.read()[-8192:].decode("utf-8", "replace")
    except OSError:
        tail = ""
    named = _NAMED_ERROR.findall(tail)
    detail = f", {named[-1][0]}: {named[-1][1].strip()}" if named else ""
    return f"Local embedding service exited during startup (exit {code}{detail})"


def token(root):
    path = Path(root) / "embedding-token"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as out:
        value = secrets.token_hex(32)
        out.write(value)
    return value


def ensure_started(root, endpoint, model):
    """Start the service if nothing answers on its port, and return the credential. Called when
    a request could not connect, not before every request (E2-13)."""
    port = check_endpoint(endpoint, model)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Separate processes (HTTP workers, hooks, MCP) serialize wake-up through
    # one root/port lock. HTTP health never counts as a completed embedding.
    with (root / f"embedding-{port}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        auth = token(root)
        with httpx.Client(
            timeout=1, trust_env=False, headers={"Authorization": "Bearer " + auth}
        ) as client:
            url = f"http://127.0.0.1:{port}/health"

            def alive():
                try:
                    response = client.get(url)
                except httpx.ConnectError:
                    return False
                if response.status_code == 401:
                    # The service reads the credential file on every request, so a refusal
                    # means the file changed under this call: the caller reads it again.
                    return True
                if (
                    response.status_code != 200
                    or response.json().get("service") != "memorypalace-embedding"
                ):
                    raise LocalEmbeddingUnavailable(
                        "Local embedding port belongs to another service"
                    )
                return True

            if alive():
                return auth
            log_path = root / "embedding-service.log"
            command, service_env, cwd = service_launch(root, port)
            with log_path.open("ab") as log:
                os.chmod(log_path, 0o600)
                since = log_path.stat().st_size
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    env=service_env,
                    cwd=cwd,
                )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                code = process.poll()
                if code is not None:
                    raise LocalEmbeddingUnavailable(startup_failure(log_path, since, code))
                if alive():
                    return auth
                time.sleep(0.1)
            raise LocalEmbeddingUnavailable("Local embedding service did not become ready within 30 s")


def create_app(root, loader=None):
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel, Field

    app = FastAPI()
    token(root)
    guard = threading.Lock()
    state = {"model": None}
    from .integrity import loaded_revision

    source = {"root": str(Path(__file__).resolve().parents[2]), "revision": loaded_revision()}

    def authorize(value):
        # The credential file as it is now: a replaced token is honoured at once, instead of
        # every client being refused until this process is stopped by hand (E2-13).
        expected = ("Bearer " + token(root)).encode()
        if not secrets.compare_digest((value or "").encode(), expected):
            raise HTTPException(401, "Authentication required")

    def load():
        # The model comes from the local cache only; a service never downloads it (E2-13).
        os.environ["HF_HUB_OFFLINE"] = "1"
        import torch
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError
        from sentence_transformers import SentenceTransformer

        model_path = MODEL
        try:
            cached = Path(
                snapshot_download(MODEL, revision=MODEL_REVISION, local_files_only=True)
            )
            required = (
                "model.safetensors",
                "config.json",
                "modules.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "1_Pooling/config.json",
            )
            if all((cached / name).is_file() for name in required):
                model_path = str(cached)
        except LocalEntryNotFoundError:
            pass
        if model_path == MODEL:
            raise RuntimeError("Qwen3-Embedding-0.6B at the pinned revision is not complete in the local cache")
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = SentenceTransformer(
            model_path,
            revision=MODEL_REVISION,
            device=device,
            local_files_only=True,
            tokenizer_kwargs={"padding_side": "left"},
        )
        model.max_seq_length = 8192
        return model

    class Input(BaseModel):
        model: str
        input: str | list[str]
        dimensions: int = Field(default=1024, ge=32, le=1024)

    @app.get("/health")
    def health(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {
            "service": "memorypalace-embedding",
            "loaded": state["model"] is not None,
            "model": MODEL,
            "model_revision": MODEL_REVISION,
            "pid": os.getpid(),
            "source": source,
        }

    @app.post("/v1/embeddings")
    def embed(request: Input, authorization: str | None = Header(default=None)):
        authorize(authorization)
        if request.model != MODEL:
            raise HTTPException(400, "Unsupported model")
        texts = [request.input] if isinstance(request.input, str) else request.input
        if (
            not texts
            or len(texts) > 16
            or any(not text.strip() or len(text) > 1000000 for text in texts)
        ):
            raise HTTPException(
                400, "Expected 1-16 nonempty texts of at most 1000000 characters"
            )
        # Only one load/inference touches the model at once; MPS allocation is
        # bounded. A failure discards the model so the next retry can reload it.
        with guard:
            try:
                if state["model"] is None:
                    state["model"] = (loader or load)()
                vectors = state["model"].encode(
                    texts,
                    batch_size=1,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
                import numpy as np

                vectors = np.asarray(vectors)[:, : request.dimensions]
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                if not np.isfinite(vectors).all() or (norms <= 0).any():
                    raise ValueError("Invalid embedding")
                vectors = vectors / norms
                return {
                    "object": "list",
                    "model": MODEL,
                    "data": [
                        {"object": "embedding", "index": i, "embedding": row.tolist()}
                        for i, row in enumerate(vectors)
                    ],
                }
            except Exception as exc:  # noqa: BLE001 - sanitize inference failures and reset model
                state["model"] = None
                # Do not send document text, provider URLs or exception bodies.
                raise HTTPException(
                    503, "Local embedding model unavailable: " + type(exc).__name__
                ) from None

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8321)
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.root), host="127.0.0.1", port=args.port, access_log=False
    )


if __name__ == "__main__":
    main()
