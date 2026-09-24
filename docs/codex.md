# Codex

MemoryPalace supports Codex's native command hooks and MCP. Hooks receive user prompts, final assistant replies and tool results, and return scoped memories at session start, prompt submission and tool boundaries. MCP supplies explicit reads, writes, corrections and provenance lookup. Both use the same private database.

The installer registers the eight hook events listed below; the Codex release must support them. See the [official Codex hook reference](https://learn.chatgpt.com/docs/hooks).

## Install

Install the checkout as described in the [operations guide](operations.md), then start the service:

```sh
uv run eventmem serve --root /private/memorypalace
```

In another terminal, install project hooks:

```sh
uv run eventmem codex install --project /path/to/project --root /private/memorypalace
```

The installer merges `.codex/hooks.json`, preserves unrelated handlers and metadata, and updates its own handlers without duplication. A changed file keeps its previous version as `hooks.json.memorypalace-backup`. It records the installation's Python executable; reinstall after moving that environment. `--user` targets the effective Codex user configuration directory. Choose one level per project to avoid duplicate handlers.

Restart Codex in a trusted project, then open `/hooks` to inspect and trust the MemoryPalace definitions, as the installer reminds you. The installer writes only `hooks.json` and its backup; it does not bypass hook trust or alter tool approval settings.

## MCP

Add this table to the project's `.codex/config.toml`, using the same private root:

```toml
[mcp_servers.memorypalace]
command = "/absolute/path/to/memory-palace/.venv/bin/eventmem"
args = ["mcp", "--root", "/private/memorypalace"]
```

Restart Codex to load the server. The stdio server opens the shared database; the HTTP service handles hooks and background jobs. The `memorypalace` server name lets hooks exclude memory-tool output from new observations. Set `EVENTMEM_MCP_SERVER` if using another name.

The MCP server also exposes `create_contact_task`, `list_contact_tasks` and `manage_contact_task`. Codex can manage source-backed reminders through an existing scoped contact policy, including revisions and delivery states. Start the HTTP service for background scheduling and configure a host callback for delivery. These tools do not manage Codex app automations. See the [contact task guide](contact-tasks.md) for configuration and request examples.

## Shared companion memory and WeChat

The MCP server includes `record_self_claim`, `predict_self_behavior`, `assess_self_prediction` and `read_self_knowledge`. Use an explicit configuration version and retained evidence when recording or reading an agent's self-model. These entries distinguish agreed roles from unverified behavioral hypotheses and reported outcomes. See [self-knowledge and behavioral checks](self-knowledge.md) for provenance requirements, current views, history and calibration limits.

A hook without a configured scope uses the resolved working directory as its project, so each project directory keeps its own memory; an MCP call without a scope uses the `personal` project. Codex agents in different working directories share companion memory when each installs with the same root and an explicit scope:

```sh
uv run eventmem codex install \
  --project /path/to/agent-working-directory \
  --root /private/memorypalace \
  --scenario companion \
  --scope '{"project":"personal","persona":"companion","collection":"default","world":"real"}'
```

Use that scope in MCP calls too, and restart the agent after installation. MemoryPalace stores no WeChat credentials or transport implementation. Active-session continuity remains the host's responsibility; the database persists across sessions and transports.

The private Kin host's phone session does not use these hooks. It runs on Kin's own Codex home with hooks disabled; its memory tools come from the host's own MCP server over the same database, limited to one scope and to the tools its permission file grants, and `kin-check` reports a `.codex` project layer in its working directory. See [mobile runtime](mobile-runtime.md).

## Lifecycle

| Event | MemoryPalace behavior |
|---|---|
| SessionStart | Restore scoped memory; `source: compact` resets context accounting |
| UserPromptSubmit | Recall before recording the prompt, avoiding self-echo |
| PreToolUse / PostToolUse | Recall relevant context; record completed tool observations |
| Stop | Record the final reply as a model source; return JSON without extending the turn |
| PreCompact / Interrupt | Mark a checkpoint boundary without recall, so no context budget is used |
| SessionEnd | Mark the end boundary; like Interrupt's, its handler times out after 3 seconds |

User statements remain explicit sources; model replies remain inferred sources. Hooks use stable lifecycle fields, exclude memory MCP results, and do not parse rollout files or ingest developer instructions. They do not reconstruct older conversations. Incomplete assistant text that never reaches Stop is not guaranteed to be captured.

Each request is spooled locally before transmission. The worker replays offline observations idempotently without spending live context budget. Hooks fail open. There are no inline model requests; model-free storage and recall remain available while extraction awaits model configuration. Keep the service running for background processing.

Remove only MemoryPalace's hooks with:

```sh
uv run eventmem codex uninstall --project /path/to/project
```

Remove the MCP table separately if it is not needed. The private database is preserved.
