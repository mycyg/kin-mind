# Installation and operations

## Install from the GitHub checkout

Python 3.11–3.13, Node.js 22 and a local SSD are the tested development combination. Runtime support is macOS and Linux. Build the bundled console before building a Python wheel; package-registry publication is separate from this GitHub release.

```sh
git clone https://github.com/mycyg/memory-palace.git
cd memory-palace
uv sync --frozen --extra all --extra dev
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
npm ci --prefix console
npm run build --prefix console
uv run eventmem console
```

Use `eventmem serve` without opening the console. Both listen on `127.0.0.1:8319`. `--root /private/path` selects a separate database. `EVENTMEM_HOME` provides the same default for the service and host bridges; `EVENTMEM_URL` selects the bridge URL. Keep the service running while using automatic host integration. Model-free receipt, FTS recall, revision and provenance remain available without optional endpoint configuration.

## Models

Configure roles in the console, or with `eventmem api PUT /v1/settings/models --json '<configuration>'`. The settings API replaces the role map, so include existing roles when updating it. Example (substitute your own endpoint/model; this is not a working credential):

```json
{
  "extraction": {"endpoint":"http://127.0.0.1:8000/v1","model":"configured-model","protocol":"openai","timeout_seconds":60},
  "embedding": {"endpoint":"http://127.0.0.1:8001/v1","model":"configured-embedding","dimensions":1024,"preprocessing":"text-v1"}
}
```

OpenAI-compatible JSON chat, embeddings and ASR endpoints and Anthropic Messages JSON roles are supported. `api_key_env` names an environment variable available to the service; it never stores the secret value. Roles are extraction, conflict, summary, rerank, embedding, visual_embedding, vision, ASR, prediction, query, answer and judge. Visual embedding requires the documented multimodal schema. Prices are optional per-million input/output settings; zero/unconfigured prices do not establish that a remote model was free.

A role configuration update makes waiting tasks retryable. The jobs view exposes failures, cancellation and retries. Source receipt, mechanical parsing and model completion have independent states. Background jobs are incremental and delay new work during interactive requests. Heavy decoding and optional full-layout model execution can still consume CPU/RAM; run a separate worker process when process-level resource controls are needed.

## Host connections

Codex: run `eventmem codex install --project /path/to/project`, restart Codex, and review/trust the generated native definitions with `/hooks`. Prompts, final replies and tool results are collected through stable lifecycle fields; startup, prompt and tool hooks return bounded context. Add the MCP configuration for explicit memory tools. The [Codex guide](codex.md) covers scope sharing, ACP/WeChat, offline recovery and removal.

Claude Code: load the checkout as a plugin with `claude --plugin-dir /absolute/path/to/memory-palace`; its `hooks/hooks.json` handles session start, prompts, pre/post tool use, compaction and exit. The plugin launcher selects its checkout `.venv` or `EVENTMEM_PYTHON`. See the [Claude Code plugin reference](https://code.claude.com/docs/en/plugins-reference). PreCompact captures the transcript and checkpoint; the subsequent SessionStart with source `compact` restores the context budget and injects the current working set. See [hook lifecycle semantics](https://code.claude.com/docs/en/hooks#sessionstart). Offline spool replay records receipt without consuming the live injection budget. The legacy hook modules remain importable for existing configurations; migrate to the bridge to use the 1.0 core.

DeepSeek Harness: build `dsh-plugin` with `npm ci`, `npm run build`, then install the local `dsh-eventmem` bundle using the harness plugin workflow. The default transport calls the service. `legacyMode: true` retains the old file-based adapter for rollback. User and assistant messages retain their distinct source authority; injected plugin messages do not become user statements.

MCP stdio:

```json
{"mcpServers":{"memorypalace":{"command":"/absolute/path/to/.venv/bin/eventmem","args":["mcp","--root","/private/path"]}}}
```

Streamable HTTP is available at `http://127.0.0.1:8319/mcp/` with the local bearer token. MCP by itself provides explicit tool reads and writes; it does not observe a host's conversation or inject passive context automatically.

Record lists contain bounded previews; open a record to read its content in cursor-based segments. The correction editor reads all segments at a consistent revision before editing.

## Migrate, recover and delete

```sh
eventmem migrate /old/project/.memory --root /isolated/memorypalace --scope '{"project":"my-project"}'
eventmem backup /private/backups/memory.tar.gz --root /isolated/memorypalace
eventmem restore /private/backups/memory.tar.gz --root /another/empty/root
eventmem export /private/exports/memory.jsonl --root /isolated/memorypalace
```

Migration copies source snapshots and archive packages, preserves original ids and links, validates integrity, and writes a migration report. External conversation pointers remain explicitly unverified until separately imported. The target must be empty and separate from the old directory. Compare recall from the same snapshot before selecting the new root. Restoring a backup also requires a separate empty target; the console restores into a new staging directory and reports its location.

Archive keeps provenance and history. Permanent deletion erases the selected source closure and dependent records, tombstones the source ids and invalidates caches/indexes. Exports and backup files, including files previously generated in the root's `exports` folder, are independent copies and must be deleted separately when appropriate. Restoring an older backup intentionally restores its historical snapshot; it does not consult a later database's tombstones.

## Contact callbacks

MCP and Python hosts can create, inspect and change source-backed reminders using the [contact task tools](contact-tasks.md). Configure a scoped policy first. Keep `eventmem serve` running for the worker to process due schedules. Model-composed reminder text retains model provenance; revisions and callback receipts distinguish scheduled tasks from delivered messages.

Start `uvicorn examples.v1.callback:app --host 127.0.0.1 --port 8320`. Configure a policy with a matching scope and `http://127.0.0.1:8320/callback`, then schedule a supported record. Enable automatic sending only through the policy settings. Default policies generate suggestions. `EVENTMEM_WEBHOOK_SECRET` signs the body; the example validates it when set. Its effect and delivery inbox commit in one SQLite transaction. External effects require the downstream service's own idempotency mechanism. Non-idempotent uncertain deliveries remain visible for reconciliation.

The callback runs with no database transaction open, so it may write back to MemoryPalace, including `acknowledge_delivery`, before it answers. Answer 2xx only after the effect is durable, and repeat the delivery id as `id` or `delivery_id` in the JSON answer. A 4xx answer is a definite refusal and may be retried. After a 5xx answer, a timeout or a network error the same body is sent again under the same `Idempotency-Key` only when the policy sets `idempotent_channel` and the channel's last 2xx answer repeated the id; `outbox_channel_contracts` records that evidence per callback URL. The body is frozen at the first dispatch, so a later correction of the record never changes the bytes of a retry. `GET /v1/contact/outbox` adds `phase: "dispatching"` to a `sending` delivery whose request is out.

## Reproduce checks

```sh
uv run pytest -q
npm test --prefix sdk/typescript
npm test --prefix dsh-plugin
npm test --prefix console
uv run python scripts/generate_contract.py
npm run generate --prefix sdk/typescript
npm ci
npm run docs:render
uv run eventmem evaluate --output /tmp/replay.json
uv run eventmem benchmark --scale full --root /tmp/new-scale-root --output /tmp/scale.json
uv build
uv run python scripts/check_wheel.py
```

Scale fixtures need an empty root and roughly 10 GB disk space. The full test creates 100,000 memories, 1,000,000 knowledge vectors and corresponding SQLite records, and reports actual hardware and all target checks. CI runs routine tests; the scale workflow is separately invocable. The legacy replay adapter reads the fixed historical git commit, so use a full GitHub checkout for that comparison.
