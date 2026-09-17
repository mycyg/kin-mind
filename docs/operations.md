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

## Evidence isolation

A store written before [reading purpose and evidence classes](architecture.md#reading-purpose-and-evidence-classes) existed is classified on the fly and reads correctly, but its derived views were built under the older rules and its self-claim chains were never linked. The host action `migrate-evidence-isolation` applies the classification once, and takes it back:

```sh
python -m kin_mind.host --config PRIVATE_CONFIG migrate-evidence-isolation --output PLAN.json
python -m kin_mind.host --config PRIVATE_CONFIG migrate-evidence-isolation --registry REGISTRY.json --apply --output APPLIED.json
python -m kin_mind.host --config PRIVATE_CONFIG migrate-evidence-isolation --undo --apply --output UNDONE.json
```

`--dry-run` is the default and writes nothing at all; asking for both a dry run and an apply is refused. `--registry` names a JSON object mapping a namespace to a non-experience class, which is the host's own private list and never belongs in this repository; a dry run reads it as the installed registry would be read, and an apply installs it after archiving whatever was installed before. `--output` writes the complete impact list to that file with owner-only permissions; the action itself returns the summary counts and what each step did, because the list can name thousands of identifiers.

Progress is one row of `mind_memory_migrations` named `evidence-isolation-v1`, whose `data.state` runs `pending`, `classified`, `chains`, `invalidated`, `complete`. States only move forward, so rerunning a finished migration does not reopen it. While that row exists and is neither `complete` nor `undone` the read policy is strict: caches, overviews, window receipts and ready summaries are all misses, so a half-applied store answers from records rather than from derived text it has not rebuilt. Every step is idempotent and resumable, asks what is already done instead of counting what it did, archives before it writes, and keeps its write transactions small, because production has other writers.

The three steps are:

1. **classified** — fill `source_evidence_class` for what is already stored.
2. **chains** — link the self-claim supersede chains nobody ever linked. A chain is one group per casefolded aspect, context and role basis; a pair whose validity times would invert is skipped, and a claim already replaced elsewhere is left alone.
3. **invalidated** — move configuration references out of the graph nodes that mix them with lived evidence, as a new revision that keeps the moved references under their own key; a node that loses its last explicit evidence has its basis demoted from explicit to inferred, and its derived text is cleared: the archive and the node's revision history keep it, a read falls back to the text of the records this read may see, and the summaries of the events the node belongs to are marked dirty and rebuilt by the existing digest job. Accepted judgments that rested on newly hidden objects are purged, because that cache does not invalidate itself on a classification change. The database generation is then bumped, so every cached policy reloads.

No legacy record is rewritten: a record keeps its attributes and its revision, because evidence freshness is pinned to that revision and bumping it would stale every claim, node, habit and plan that cites it.

The impact list carries identifiers and counts only — never a title and never a line of content — so it can be signed off without opening a record. Read the summary first: how many sources and records the rules classify and under which rule, how many owner configuration requests keep their experience class with a label, which records an experience read will stop returning, how many claim chains and supersessions are planned, how many ties were skipped, how many graph nodes are mixed and how many are configuration-only, how many event summaries a model will have to write again, and how many cached contexts and window receipts will keep missing once the migration has finished. A number that surprises you is a reason to inspect the named identifiers before applying anything.

After an apply, check that the state reached `complete`. It does not when another writer moved a claim or a node between the plan and the write: those are listed as refused, the state stays strict, and a second `--apply` retries them against what is stored now. `--undo` reverses the applied steps from the archive, always by writing a new revision, restores the previously installed registry, and skips and lists anything that changed since; a store the migration never touched keeps its empty history and no row is written. Take a backup first, as for any migration.

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
