# Local embeddings and channel context

## Qwen3-Embedding-0.6B

Install the optional local inference dependencies in the same Python environment as the memory service. Production installs this combination, and `python -I scripts/python_env.py` checks an interpreter against it by default:

```sh
uv sync --frozen --extra vector --extra graph --extra local-embedding
```

`local-embedding` cannot be installed with `media` or `all`: `uv.lock` declares them in conflict because docling, which `media` brings in, locks a typer older than the one `local-embedding` requires. `--extra dev` can be added for the tests.

Add this role to the existing model configuration. Preserve other roles when writing `/v1/settings/models`, because that endpoint replaces the configuration map.

```json
{
  "embedding": {
    "endpoint": "http://127.0.0.1:8321/v1",
    "model": "Qwen/Qwen3-Embedding-0.6B",
    "protocol": "openai",
    "dimensions": 1024,
    "preprocessing": "qwen3-97b0c614-last-token-8192-normalized-v1",
    "timeout_seconds": 600,
    "local_embedding": true
  }
}
```

The opt-in `local_embedding` flag serves the role from a loopback-only service. It accepts only `http://127.0.0.1:PORT/v1` with this model, so a remote endpoint never reaches the launcher, and it refuses a `preprocessing` label that names a Qwen3 revision other than the pinned one. Each request reads the service credential from `embedding-token` under the memory root and goes straight to the service; the service is started only when a request cannot connect. A file lock per port serializes that start across processes, and a port that answers as another service is an error. The service reads the credential file on every request, so a replaced token works at once; a client refused with HTTP 401 reads the file again, once.

The model is pinned to revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` and loads only from a complete snapshot of that revision in the local Hugging Face cache. The service never downloads it: with the snapshot absent or incomplete, every embedding request fails with HTTP 503. The process starts before loading weights; the first embedding request loads the model. Apple Silicon uses PyTorch MPS when available; other machines use CPU. Inference is serialized, uses normalized vectors, and truncates each input to 8,192 tokens. A request carries 1–16 texts; large documents should be chunked before embedding. There is no idle-unload policy; the process retains the loaded model.

A request that cannot connect, or whose connection drops, starts the service if nothing answers on its port and is retried once. HTTP 503 triggers one inference retry; the server discards a failed model instance so it can reload. Embeddings are idempotent. A second failure propagates into the durable job retry policy, and no vector is committed as a successful result. Read timeouts follow that durable retry policy rather than killing a potentially working process. This retry behavior does not apply to chat generation or external side effects.

`/health` takes the same credential and reports whether the model has loaded, the model and its revision, the process id, and the source root and commit the service runs. Only a successful `/v1/embeddings` response demonstrates completed inference. The endpoint reports no token usage, so a local embedding records its duration only: no token count, no cost and no unknown-usage entry.

Changing model revision, truncation or normalization requires a new `preprocessing` identity and rebuilding vectors. Deep retrieval uses configured embeddings; the normal fast path remains lexical and relational. Documents and queries are embedded alike, without a task prompt.

[Official model instructions](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/blob/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/README.md).

## Transport context

Recognized channel envelopes separate the current user message from repeated host delivery history. Host ingestion preserves the full envelope in an immutable source snapshot, while extraction and passive message recall use the current body. Malformed envelopes and ordinary quotations remain unchanged. Parsing an envelope does not grant its contents instruction authority. Extraction retries exclude archived records and prior generated proposals; completed media annotations remain eligible evidence. Queued extraction chunks of an enveloped message are read through the same envelope rule, so they cannot produce claims supported only by excluded transport history.

Empty tool cues do not request unrelated memories. Companion passive recall excludes raw tool events; explicit searches and reads retain access to the operation evidence. Graph neighbors have a lower retrieval weight and seeds are not counted again through their own relation edges.

Semantic preference changes require an evidence-backed `replace` revision linking the obsolete record to its replacement.

## Operational diagnosis

The overview is explicitly store-wide. `job_details` groups failed, retrying and waiting-for-configuration tasks by kind and sanitized error. Missing embedding configuration is distinct from failed extraction. Provider HTTP status codes are retained without response bodies, credential-bearing URLs or headers. HTTP 402 requires resolving the provider account or selecting an available model; retry loops cannot repair it.
