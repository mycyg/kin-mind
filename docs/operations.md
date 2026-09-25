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

Use `eventmem serve` without opening the console. Both listen on `127.0.0.1:8319`; when a service already answers there, `eventmem console` opens the console on it instead of starting a second one. `--root /private/path` selects a separate database. `EVENTMEM_HOME` provides the same default for the service and host bridges; `EVENTMEM_URL` selects the bridge URL. Keep the service running while using automatic host integration. Model-free receipt, FTS recall, revision and provenance remain available without optional endpoint configuration.

## Models

Configure roles in the console, or with `eventmem api PUT /v1/settings/models --json '<configuration>'`. The settings API replaces the role map, so include existing roles when updating it. Example (substitute your own endpoint/model; this is not a working credential):

```json
{
  "extraction": {"endpoint":"http://127.0.0.1:8000/v1","model":"configured-model","protocol":"openai","timeout_seconds":60},
  "embedding": {"endpoint":"http://127.0.0.1:8001/v1","model":"configured-embedding","dimensions":1024,"preprocessing":"text-v1"}
}
```

OpenAI-compatible JSON chat, embeddings and ASR endpoints and Anthropic Messages JSON roles are supported. `api_key_env` names an environment variable available to the service; it never stores the secret value. Roles are extraction, conflict, summary, rerank, embedding, visual_embedding, vision, ASR, prediction, query, answer and judge. Visual embedding requires the documented multimodal schema. Prices are optional per-million input/output settings; zero/unconfigured prices do not establish that a remote model was free.

A role configuration update makes waiting tasks retryable. The jobs view exposes failures, cancellation and retries. Source receipt, mechanical parsing and model completion have independent states. Background jobs are incremental and delay new work during interactive requests. Heavy decoding and optional full-layout model execution can still consume CPU/RAM; run `eventmem serve --no-worker` beside a separate `eventmem worker` process when process-level resource controls are needed. One background loop works a store at a time: another service or worker on the same root stands by until the first releases `worker.lock`. `GET /v1/health` answers 503 with `status: "degraded"` while that loop has stopped, stands by, or has neither gone round nor renewed a job lease for five minutes; a service started with `--no-worker` reports `worker.enabled: false` instead. It also reports the running source's root and `revision`, `schema` (a digest of the store's table, index and trigger definitions) and the service's `default_scope`.

## Host connections

Codex: run `eventmem codex install --project /path/to/project`, restart Codex, and review/trust the generated native definitions with `/hooks`. Prompts, final replies and tool results are collected through stable lifecycle fields; startup, prompt and tool hooks return bounded context. Add the MCP configuration for explicit memory tools. The [Codex guide](codex.md) covers scope sharing, ACP/WeChat, offline recovery and removal.

Claude Code: load the checkout as a plugin with `claude --plugin-dir /absolute/path/to/memory-palace`; its `hooks/hooks.json` handles session start, prompts, pre/post tool use, compaction and exit. The plugin launcher selects its checkout `.venv` or `EVENTMEM_PYTHON`. See the [Claude Code plugin reference](https://code.claude.com/docs/en/plugins-reference). PreCompact captures the transcript and checkpoint; the subsequent SessionStart with source `compact` restores the context budget and injects the current working set. See [hook lifecycle semantics](https://code.claude.com/docs/en/hooks#sessionstart). Offline spool replay records receipt without consuming the live injection budget.

DeepSeek Harness: build `dsh-plugin` with `npm ci`, `npm run build`, then install the local `dsh-eventmem` bundle using the harness plugin workflow. The plugin sends every event to the service and keeps it in the private spool until the service accepts it; its options are listed in the [plugin guide](../dsh-plugin/README.md). An enabled plugin whose configuration still sets `legacyMode: true` refuses to load. User and assistant messages retain their distinct source authority; injected plugin messages do not become user statements.

MCP stdio:

```json
{"mcpServers":{"memorypalace":{"command":"/absolute/path/to/.venv/bin/eventmem","args":["mcp","--root","/private/path"]}}}
```

Streamable HTTP is available at `http://127.0.0.1:8319/mcp/` with the local bearer token. MCP by itself provides explicit tool reads and writes; it does not observe a host's conversation or inject passive context automatically.

Record lists contain bounded previews; open a record to read its content in cursor-based segments. The correction editor reads all segments at a consistent revision before editing.

## Back up, recover and delete

```sh
eventmem backup /private/backups/memory.tar.gz --root /isolated/memorypalace
eventmem restore /private/backups/memory.tar.gz --root /another/empty/root
eventmem export /private/exports/memory.jsonl --root /isolated/memorypalace
```

A backup is SQLite's online backup of the database, read as one consistent snapshot without taking the write lock, so other writers carry on meanwhile. It carries the attachments that snapshot references and the store's own files — the persona policy, the self-knowledge file, configuration and persona versions and the history archive — each with its SHA-256 in a manifest; credentials, locks, caches and vectors stay out. Restoring requires a separate empty target: every entry is checked against the manifest and the database against SQLite's integrity check, and the store is prepared in a sibling staging directory and published only once it is complete. The restored store puts running jobs back in the queue, marks a delivery that was being sent `uncertain`, and queues a rebuild of its text index and a fresh embedding of every record that index holds. The console restores into a new staging directory and reports its location. A backup that carries the persona policy puts that canon in place, so it is restored only as the host's approval record names it: `--persona-approval` takes the host configuration that holds the record under `persona_contract` (Kin's `mind-config.json`), and the service's endpoint reads the record its host passes when a restore asks. With no record, one that leaves out the version or any of the three hashes, or a canon that differs from it, nothing is restored and the canon is left for host review ([persona contract](persona-contract.md)).

Archive keeps provenance and history. Permanent deletion erases the selected source closure and the records that depend on it — the set `GET /v1/objects/{id}/deletion` previews — and tombstones their ids. A source written from others — a reflection, a creation's summary and result, an exploration's report — is stored with what it cites as its root record's dependencies, and with what its writer was only shown (recent dialogue, earlier results) checked beside them, all in the transaction that stores it: written after any of it was deleted or changed it is not kept (a creation keeps its artifact and run, an exploration its run and observations, not the words -- the artifact as which file and which bytes, without the excerpt the host read from it or the text a render showed), and deleting what it cites later takes the derived source too, with whatever cites it. A note or a summary that cites a derived source goes without it, like any record that cites a source, and is marked unverified when another of its inputs is corrected. What an appraisal's model was shown besides the references it was asked about -- the mind's state, methods, entries marked for review -- and what it read with the main session fork's read-only memory tools while it answered, cited or not (the records and sources the fork's receipt says each tool call returned, in every turn of the attempt: the call, its repairs, its evidence compression, a light question), is named by its queue row and checked at its commit and before its reflection is stored; so is a daily review's, where the behaviour chain is off. So is a contact draft's, on its attempt row: what the memory context and the state handed to it name, the wishes and sends it was offered, what its fork's tools returned. Kin's decision and text are kept only while none of it is deleted, checked where they are written, and a reason copied onto a wish goes with any of it; a draft already sent stays as her message, and only the row's copy of its text goes. A compression the fork made is cached only while what its tools read stands. A fork's receipt keeps 64 tool calls, 1000 ids a call and 2000 a turn, and says `truncated`, and why (`truncated_by`), past any of them, and where the owned ACP says it could not read a call's result whole -- or, from an ACP too old to say, where a call returned 100 ids or more: then any delete since the attempt began stops the commit or the draft, a proposal kept for reuse is asked again in full, and a compression is used but not cached. A creation's brief shows the plan's own evidence and is checked the same way. The exploration and creation executors and the completion review have no memory tools. A turn of the legacy in-session assessment channel keeps no record of what its tools read: its receipt says `channel: legacy`, and it is checked as a fork turn cut short from its first read. What was only shown is not followed later, as a committed mind entry goes with what it cites: the receipt a wish, a plan, a trait or any other committed entry keeps of the appraisal that wrote it says what the fork read, and does not make the entry go with it. The rows of the call's own process -- its queue row, a draft's attempt row, a daily review -- name everything shown and lose their words with any of it, and a row written before this release that names the fork's reads only in its receipt loses them by that receipt. The memory context a queue row keeps frozen for its retry may hold more than the row names: an excerpt in it that the context labels by a record's own id -- an event's identity evidence, a topic candidate's members -- loses its words with that record at the delete, not at the next attempt, and a topic candidate with any member it shows, as the store deletes the family with it. A topic candidate is titled only by a member it shows -- the family's title where one of them has it, else the first one's topic or title -- so a member it leaves out, not current or hidden from the read, leaves no title in the copy when it is deleted. A process row that does not name what its model or executor was shown at all -- every queue row, daily review, draft attempt, exploration run and plan run a release before this one wrote -- loses its words to any delete, its ids, state and receipts kept: no delete can be matched with what it saw, and a delete that release made before it did not take the words it left in the state such a row was shown either. A queue row still to be judged also loses the proposal and the context it kept, and is judged afresh. A row at work is left to the code working it. Among the previous explorations Kin and its executors are shown, a run whose words were taken shows its id and state, and the summary of its report while the report stands -- a report goes with what it was written from, so one still there may be shown; without it the run names no report, so no brief passes a deleted one on as shown. Kin's replies stay as the conversation record: deleting the owner's message does not take the answer to it, which can be deleted by itself. The same transaction takes their words out of the mind's own layers — graph, state, wishes, plans and every cached summary that names them — and a background job takes them out of every revision of the state history, a batch at a time; `operational-status` reports that rewrite under `memory.history_erase`. Exports and backup files, including the downloads generated in the root's `exports` folder, are independent copies and must be deleted separately when appropriate. Restoring an older backup intentionally restores its historical snapshot; it does not consult a later database's tombstones.

Three things about a delete that are easy to get wrong:

- **A delete is an erase.** Nothing is archived before it and nothing keeps a cold copy for it. The rule that the programme archives instead of deleting governs its own migrations, repairs and deploy steps; it does not apply to a delete somebody explicitly asked for.
- **HTTP is not the only way in.** Besides `DELETE /v1/objects/{id}`, the generic MemoryPalace MCP server defines a `delete_memory` tool. Kin's own tool list (`chat-permissions.json` in the host) leaves it out, so Kin cannot call it; any other client connected to the generic server can.
- **Old backups and exports are separate copies.** Deleting data from the live store does not erase it from a backup or an export made before, and nothing here establishes how long backups are kept, so do not count on them expiring by themselves: a copy that has to go has to be deleted where it is. The host's own copies hold no words for a delete to miss: `state/mind-status.json` keeps the states, ids, codes and receipts of the mind's results, never a proposal, a reason, a draft or a report; and once a run is settled, a stopped one too, the files of an exploration's or a creation's working directory (the brief, each answer, the checkpoints, the receipts, the observation ledgers, the file reader's settings) keep their ids, states and receipts and lose their words, and each page the web reader fetched loses its text whole. A word here is anything that does not name or classify something: a sentence, anything outside ASCII, a query string, percent-encoding, an email address, and a single word unless a field of codes (a state, a provider, an error's class) holds it or it names a key of the same file; plain paths and locators stay. The directories stay, and so do a creation's artifacts, which are its work.

## Deploy-time repair

`eventmem.core.repair` brings a store written by an earlier release in line with the current rules. Run it with the memory service stopped, after a backup:

```sh
python -m eventmem.core.repair --root /private/memorypalace
python -m eventmem.core.repair --root /private/memorypalace --snapshot --output /outside/the/root/dry-run.json
python -m eventmem.core.repair --root /private/memorypalace --apply
python -m eventmem.core.repair --root /private/memorypalace --apply --steps reerase
```

Without `--apply` it plans and writes nothing. `--steps` takes a comma-separated list. The default steps write a classification row by the source-origin table for each source that has none (`origins`), take the host's own bookkeeping out of the text index and the organise queue and archive the session-maintenance notes nothing current cites (`maintenance`), supersede root records of an older source version still active beside the newer one (`versions`), settle queue rows that cannot finish and queue again the embeddings that failed only because the embedding service was down (`queues`), rebuild the graph and memory node indexes (`indexes`), move host receipts that can never be replayed to `host-spool/rejected/` and replay the rest (`spool`), fill the evidence-key table (`evidence`, see below), and give the sources written from others before this release -- reflections, exploration reports, a creation's artifact and result events -- the lineage this release registers when it stores one, adding to a source that has one already only what it lacks (`lineage`). Two steps run only when named: `reerase` deletes the derived sources whose lineage names a deletion fact, as that delete would have taken them, and runs the erase again for every tombstone, through the mind's derived layers and its state history; its plan counts what those deletes take (`records`, `sources`, `derived_in_closure`), and by kind: the derived sources as reflections, reports, a creation's artifact and result events (`derived_by_kind`, `closure_by_kind`), the reflections taken only for what their appraisal was about (`reflections_by_target`), the sources' own records beside the notes that cite them (`records_by_kind`); beside them, every derived source the store holds, by the same kinds (`store_by_kind`), so the share a deletion takes can be read; among the layers, by table, the process rows that do not name what they were shown and lose their words to the erase (`unnamed:<table>`), those left at work included, since nothing runs then. The release's gate holds these, and the apply holds itself to the plan of the same run: deletes that would take more are not made, and deletes that took more stop the run with one line on stderr and exit status 1. `reerase` may run in a later release than `lineage`: `lineage` alone registers and deletes nothing, and a later `reerase`, alone or beside `lineage` (which then finds nothing left), plans and takes what it would have taken in the same run. `quarantine` retires quarantined appraisals whose evidence is gone, without a model call, and lists the others by reason.

Every step is idempotent, so a second run reports nothing left. Only `reerase` deletes, and only what a delete made before this release should have taken: the derived sources that rest on a deletion fact, with what their delete takes, and the stored receipts, sessions and metrics that name a deletion fact. No other step deletes a record, a source, a revision or a history row: records leave active use by a new revision, queue rows change state and indexes are rebuilt. The report carries identifiers, namespaces and counts only; `--output` also writes it to a file with owner-only permissions.

The dry run opens the store read-only and builds nothing — not this release's tables or indexes, not its metadata, not the tokenizer cache under the root — so it can be read before anything is decided, on a store from before this release as well. SQLite itself still keeps a reader's `-wal` and `-shm` beside a store in WAL mode, and a read-only reader cannot take them away when it is the last to leave. `--snapshot`, for a store in use -- a dry run before the services stop -- leaves not even those: it clones the store and its side files from a moment when no process holds any of them open and none of them changes (lsof, and inode, size and times, before and after the clone), into a private temporary directory outside the root, recovers a WAL a crash left in the clone, dry-runs the clone with the rest of the root read where it lies, and removes the clone. The report then says how the store was taken (`snapshot`). A store that is not still for `--wait` seconds (120 by default) is refused; with `--apply` it is refused too. A root with no `memory.sqlite3` is refused rather than turned into a new store. The command exits 0 with the report on standard output; 2 when it refuses (no store at the root, an unknown step), with one line on standard error and nothing on standard output; anything else is a failure, and a failed `--apply` may have finished the steps before the one that failed, each of which a rerun finds done. `reerase` queues the state-history rewrite only for identifiers it has neither finished nor still owes a pass for; that rewrite runs in the service's background loop, so until the service has run it a dry run still counts those identifiers under `reerase.history_ids`.

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
3. **invalidated** — move configuration references out of the graph nodes and edges that mix them with lived evidence, as a new revision that keeps the moved references under their own key; a node a run already marked is taken apart again when it cites configuration again. An edge keeps its reason and stands on the evidence that remains. A node that loses its last explicit evidence has its basis demoted from explicit to inferred, and its derived text is cleared: the archive and the node's revision history keep it, a read falls back to the text of the records this read may see, and the summaries of the events the node belongs to are marked dirty and rebuilt by the existing digest job. Accepted judgments that rested on newly hidden objects are purged, because that cache does not invalidate itself on a classification change. The database generation is then bumped, so every cached policy reloads.

No legacy record is rewritten: a record keeps its attributes and its revision, because evidence freshness is pinned to that revision and bumping it would stale every claim, node, habit and plan that cites it. With `recall_purpose_policy` on, the graph's writer keeps the moved classes apart in the same way whenever an item also cites other evidence.

The impact list carries identifiers and counts only — never a title and never a line of content — so it can be signed off without opening a record. Read the summary first: how many sources and records the rules classify and under which rule, how many owner configuration requests keep their experience class with a label, which records an experience read will stop returning, how many claim chains and supersessions are planned, how many ties were skipped, how many graph nodes are mixed and how many are configuration-only, how many edges are mixed, how many event summaries a model will have to write again, and how many cached contexts and window receipts will keep missing once the migration has finished. A number that surprises you is a reason to inspect the named identifiers before applying anything.

After an apply, check that the state reached `complete`. It does not when another writer moved a claim or a node between the plan and the write: those are listed as refused, the state stays strict, and a second `--apply` retries them against what is stored now. The service's background loop makes that second apply itself, at most once an hour and 24 times in all; `operational-status` reports the state, whether reads are strict, the refusals and the retries under `memory.evidence_isolation`. `--undo` reverses the applied steps from the archive, always by writing a new revision, restores the previously installed registry, and skips and lists anything that changed since; a store the migration never touched keeps its empty history and no row is written. Take a backup first, as for any migration.

## Evidence key index

A single underlying event may be appraised once. The state snapshot of each `affect` history row records the key it scored, and the old guard reads that by scanning every snapshot ever written — inside the write transaction of every new appraisal. `mind_evidence_keys` holds that one fact on its own, keyed by `(scope, evidence_key)`, with the event and the revision that introduced it. A store written before the table existed fills it once:

```sh
python -m kin_mind.host --config PRIVATE_CONFIG evidence-keys-backfill
python -m kin_mind.host --config PRIVATE_CONFIG evidence-keys-backfill --apply
python -m kin_mind.host --config PRIVATE_CONFIG evidence-keys-verify
```

`--dry-run` is the default and writes nothing at all, neither the rows nor the cursor; asking for both a dry run and an apply is refused. The backfill walks the `affect` rows oldest first, a batch per write transaction because production has other writers, and inserts each key only if it is not there already: a row that never went through the scoring path carries the key of the row before it, so the row that really introduced a key is the one recorded. Progress is the `cursor` of the `mind_memory_migrations` row named `evidence-keys-v1`, so an interrupted run resumes where it stopped and a finished one can be rerun without reading anything twice. An apply verifies itself when it has finished and reports what it found under `verification`, so its own `state` is `complete` only when the two sides line up.

`evidence-keys-verify` compares the two directions row by row and is read only: forward, every key a history row introduced is in the table against the event and revision that introduced it; backward, every row of the table was introduced by a history row that is still there. `missing` would let evidence be scored twice, `extra` would refuse evidence that never was, and `mismatched` means the table credits the wrong row. Both actions work one scope at a time, the scope the configuration names.

The table is never the only reader. Before it is consulted, a catch-up reads the rows written past the cursor, so rows written without the table, or an imported history, cannot leave a hole. Run the backfill once soon after deploying — the deploy-time repair's `evidence` step does — because a cursor still at zero leaves that first reading to the catch-up, inside the write transaction of whichever appraisal reaches it first. While both `evidence_key_index` and `history_legacy_guard` are on, the guard refuses when either the table or the old scan says it has seen the key, and a disagreement between them is recorded as the `evidence_key_guard_mismatch` metric — with the key digest and which side said what, never any evidence text. With `evidence_key_index` off the guard asks the old scan alone. Turning `history_legacy_guard` off stops paying for that scan; the repair's `evidence` step turns it off for a scope only when the backfill has reached the head of history and its verification agrees row by row, and leaves it on, with the counts in its report, everywhere else.

## Stored history

Without `history_patches`, every revision of the mind's state is stored as a whole document. That is what makes the history inspectable, and it is also why it grows so fast: a document whose wishes are never pruned is written again on every change, however little of it moved. With `history_patches` on, a revision is stored as a keyed diff against the row before it — `["set", [key] or [key, key], value]` and `["del", path]`, with lists replaced whole — inside the same column, with no new table and no new column anywhere. On a synthetic history of 121 revisions over an 82 KB document that is 6.16 MB of rows against 432 KB, a saving of 93 %, for 0.3 ms added to the write.

`history_patches` is **off by default**. It chooses what a revision is written as, and from the first patch row committed, a release that cannot read patches cannot rebuild the newest states. So it deploys off and is turned on as its own decision:

```sh
python -m kin_mind.host --config PRIVATE_CONFIG history-status
python -m kin_mind.host --config PRIVATE_CONFIG history-verify
```

`history-status` says what the rows are: how many carry a whole state, how many carry a patch, how deep the chains have grown, what they cost, and `first_patch_revision` — which is empty until the format changes and is the **rollback boundary** once it is not. Before that revision a release without patch support reads every row. After it, a rollback can only go to a release that reads patches, running with the flag off. Take a rollback drill on a copy before turning it on.

`history-verify` rebuilds every revision in one forward pass and checks each document against the hash its row recorded. Both commands are read only and safe against a live store. Three outcomes, and the middle one is why there are three: `verified` means every row was checked and every one held; `incomplete` means something failed, and `failures` names the revisions; `partial` means nothing failed but some rows **could not be checked at all**. Those are rows written without a recorded hash: they are never rewritten, and nothing can be said about their contents beyond that they parse. They are counted as `unverifiable` and never added to the verified total, because a clean report about rows nobody looked at is the kind of thing that gets believed later.

A row is kept whole rather than stored as a patch when its kind is `initialize` or a personality `evolution`, when the chain under it would reach fifty rows, when the patches standing on one whole state have cost as much as that state did, and when a single patch would cost more than half of it. It is also kept whole when the row before it is missing, will not parse, or does not hash to what it claims: the writer never builds on a chain it cannot read and never tries to repair one, so it writes a whole state, records `healed` in the row, and everything after that stands on the new ground. That covers the row before; a break further down is what `history-verify` finds. To repair one, turn `history_patches` off for a single revision — the next row is then a whole state again, which is a checkpoint by another name — and turn it back on.

While compaction owns the rows, `meta.history_compaction_active` is set and no revision can be written at all: the commit is refused with `history-compaction-active` and nothing lands, because a row written in the middle of a rewrite is a row the archive does not hold. An appraisal that meets the marker pauses instead of failing, and one the marker alone set aside goes back to its queue when the marker is lifted.

## Compacting the history already written

Turning the flag on changes what the next revision is stored as. It does nothing to the rows already there, and in a store that has been running a while those are nearly all of them. `history-compact` rewrites them: the `data` column becomes what changed since the row before instead of a whole document, and nothing else about the row moves. Every row stays, every column stays, and `id`, `scope`, `revision`, `kind` and `occurred_at` are not written at all — four of the readers of this table want the columns and never look inside `data`.

This command rewrites data the store already holds, so the archive comes first — all of it. A run is two phases, and before either begins the run freezes a manifest of every row it will touch and proves the backup against it (the preconditions below say what that proof is). Phase one copies every row the run will touch, verbatim, into a separate database, `archive/mind-events-v1-<date>.sqlite3` beside the store at mode 0600, fsyncs it, then reads each row back on a fresh connection and compares it column by column with what is still in the store. Only when the whole set is archived and verified does the rewrite phase begin; it refuses any row the archive phase did not cover, and each batch's rewrite, the rebuild that checks it and the cursor that records it are one transaction. **Nothing here ever deletes the archive**, on success or on failure.

```sh
python -m kin_mind.host --config PRIVATE_CONFIG history-compact
python -m kin_mind.host --config PRIVATE_CONFIG history-compact --apply
python -m kin_mind.host --config PRIVATE_CONFIG history-compact-verify
```

Without `--apply` it writes nothing at all: it checks every precondition and reports each verdict rather than stopping at the first, plans what each row would become, and computes one batch of real patches to say what the saving would be. With `--apply` a failed precondition is a refusal that has written nothing.

Four preconditions, every run, none of them skippable. The store must be **quiet** by the whole quiescence proof of [mobile recovery](mobile-recovery.md) — both pid files naming processes that are gone or are something else, a host status that is stopped or has not beaten for forty-five seconds, no fresh lease in any of the five lease tables, no live exploration, and an exclusive lock available. There must be a **backup** of the database that can be shown to hold this history — and "shown" is not counted: the run freezes a manifest of the store's content identity (one digest folded over every row's key columns and bytes) and of every target row's columns and content hash, opens the backup read-only, runs SQLite's `quick_check` on it, and compares it against the manifest row by row; what was trusted — the file's hash, size, permissions and name — is recorded in the run's own progress. One is taken beside the store, named for the history it is a copy of including the first bytes of that digest, unless a matching one is already there or the operator names a file; a named file that is of something else is refused rather than replaced, and same shape with different rows is something else. A resumed run uses the backup it started with, wherever that file is, unless the operator names one, and checks it against the manifest frozen at the run's start, never against rows the run itself has already rewritten; if that backup is gone the run is refused rather than a new copy taken. There must be free **disk** for that backup, the archive and working room. And `meta.history_compaction_active` is set before the first batch, so the writer refuses revisions while this owns the rows.

That marker is **not self-clearing**. A run that stops part way leaves it set and the store closed to new revisions until someone decides: run `history-compact` again, which carries on from the cursor, or `history-restore`, which puts the archived rows back and opens the store again. The cost of that is a host that stays stopped; the cost of clearing it would be a revision written into a half-rewritten history.

Rows that keep their whole document are the first row, every fiftieth row, every `evolution` row and the row before it, and the row under any row that was already a patch — the last of those is what keeps the depth recorded in an existing patch true, since it recorded the chain under it as nothing. Two more are the command's own and can only keep more rows whole: a patch that would not be smaller than the document it replaces, and a chain that would reach the writer's own bound. The report counts each reason separately.

`history-compact-verify` is the check the rewrite is allowed to have happened for, and it is the one to read. `history-verify` asks whether a row still hashes to what it claims; after a compaction that is two halves of the same pass agreeing with each other, which is why its report carries `compacted_through` and says so. `history-compact-verify` rebuilds each revision from the store and compares it with the **archived original bytes**, which were copied out before the first row moved, less the words an explicit delete has taken out since, removed by the same scrub. `deep` rebuilds each revision from its own nearest whole document instead of carrying the state forward: the same answer reached independently, much slower, and worth one run on a copy before a release.

`history-restore --apply` writes the archived rows back a batch at a time; without `--apply` it reports what it would restore. Each archived row is checked against the digest taken when it was archived, and a row that does not match stops the restore. Words an explicit delete has taken out of the store since the archive was made do not come back: every row goes through the same scrub as the delete's own history rewrite, and the hashes after it are carried forward until the chain agrees again; a row with nothing erased in it goes back byte for byte. It needs the same quiet, it compares the columns beside `data` and refuses if one of them has moved, it leaves the archive exactly where it is, and it opens the store again.

## Wish archive

The state document carries every wish that was ever made and is written again on every revision;
most of those wishes end up finished, completed or abandoned. `mind_desire_archive` holds the
finished ones that nothing is still waiting on, whole — the same identifier, the same plan links,
the same evidence references, the same decision receipt — and the document carries only what is
still live plus the tail of finished wishes the appraisal projection shows.

```sh
python -m kin_mind.host --config PRIVATE_CONFIG desire-archive
echo '{"days": 14}' | python -m kin_mind.host --config PRIVATE_CONFIG desire-archive
python -m kin_mind.host --config PRIVATE_CONFIG desire-archive --apply
python -m kin_mind.host --config PRIVATE_CONFIG desire-unarchive --apply
```

The dry run is the default, writes nothing at all, and asks for no flag: run it against a copy
first and read what it says. `would_archive` names every wish that would move; `holding` names
every wish that would stay and **why**, by identifier, with a sentence for each reason under
`reasons` and totals under `holding_reasons`. `days` is the floor under a settled wish and is the
only number worth trying at several values — the rest of the decision does not change with it.

**Time is never a reason.** Only a wish the model has already settled as `completed` or
`abandoned` can move. A long-running plan is not finished because it is old; an expired `wanted`
or `waiting` wish is not touched at all, because expiry is not a verdict and the wish is still the
model's to settle. Nor does a wish move while anything is still open on it: a contact attempt
drafting, pending or unconfirmed; a plan run running or unconfirmed; an active plan or step; an
action event that has not settled, including one left for review; a running exploration; or a
delivery recorded as partial. Each of those appears by name in `holding`.

A wish that moved is still this mind's. Every reader that asks for a wish by its identifier looks
in the archive when the document misses, so an update of an archived wish is refused exactly as a
finished wish is refused, a sharing decision whose contact intent has moved still refuses a second
one, and the plan synchronisation skips it instead of making it again. The count of what has moved
is kept in the state document, so the projection's `desire_window.total` still says how many
wishes this mind has rather than how many are left in the document.

`desire_archive` is **off by default** and the move needs the explicit command as well as the
flag. `desire-unarchive` is never gated by it: with no identifiers it puts everything back, and it
is what runs **before a rollback**, because a release without the archive cannot see it at all
and would read those wishes as gone. Nothing is ever deleted — an unarchived wish leaves the
table only in the same transaction that puts it back into the document, and the document sorts its
keys, so a full round trip gives back the document it had.

Both the move and the restore are ordinary revisions with their own history rows, `desire-archive`
and `desire-unarchive`, whose request names the wishes that moved.

## Derived caches and telemetry

Two tables grow without an end and neither of them holds a memory. `mind_context_cache` holds **compressed context**: the summary a model produced of records that are all still there, keyed by a digest of the inputs that produced it. `metrics` holds telemetry and is already a ring. The maintenance tick bounds both, and reports what it would do before it is allowed to do anything:

```sh
python -m kin_mind.host --config PRIVATE_CONFIG maintenance-tick
python -m kin_mind.host --config PRIVATE_CONFIG maintenance-tick --apply
```

**The cache sweep deletes a derived cache and nothing else.** Everywhere else a bounded table is bounded by moving rows into an archive that is never deleted, because those rows are the owner's. These rows are the model's working copy of rows that stay where they are: drop one and the next reader misses, pays one model call, and writes the same summary again. So `context_cache_sweep` is **off** until someone has been told exactly that and has agreed to it, and off the tick only counts.

Two rules decide what goes: an age of 14 days on the row's own `created_at`, and a cap of 2,000 newest rows per scope on whatever survives the age. Two guards decide when: the sweep **never deletes the newest row**, because `Appraisals._cache_mark` takes `MAX(rowid)` of this table before an appraisal pass and counts the rows written after it — deleting the highest row would let SQLite hand the same number out again and a pass that did make progress would look stalled, which ends in a quarantine; and it **refuses while a running appraisal still holds a fresh lease** (`worker-lease-fresh`), because those are the rows that pass is caching right now. A sweep that held a row back says so with `retained_newest`.

Nothing else is reachable from there. The two statements of `kin_mind.maintenance` that can remove a cached summary spell `mind_context_cache` out in full, and no table name in that module is ever built or passed in; each row goes by its own identifier, out of a plan that can be printed first; and `mind_context_windows` — what was actually delivered and read — is touched by none of it. Nothing runs the tick on its own either: like the backfill above, it is an operator action, so a sweep happens when somebody runs one.

An erase is the other half. Erasing a record removes the stored command responses, session sets, metrics and prefetch rows that name what it erased, and every cached summary in `mind_context_cache` and `mind_semantic_cache` that names it, or a graph item it took words from, whatever the flag says. With `context_cache_sweep` on, an erase also clears the whole compressed context of every scope whose own configuration asked for the sweep, and of those scopes only.

`metrics_name_ring` gives the telemetry table a ring per name. One shared ring of 20,000 rows is the same bound applied in the wrong place: `model_ms`, `model_cost` and `model_tokens` are written on every model call, so a day of work pushes `recall_ms`, `appraisal_quarantined` and `structured_rejected` out of the window entirely — and the store then cannot report its own recall latency, because `Engine.overview` reads the newest 2,000 rows of the table and none of them are that name. With the ring on, a name keeps its own newest 2,000 rows, a name can only ever evict itself, and that reader asks per name as well. Turning it on trims each name that is already over the ring on its next write; the tick with `--apply` does the same for every name at once. The tick's report lists the distribution, what the ring would remove, and which names are already `crowded_out` of the read window.

Lance keeps one manifest per write, and a store that writes all day accumulates thousands of them against a much smaller amount of data. The worker compacts every vector table once a day, as the last job in its queue: the job waits, without spending an attempt, until no other job is running, no foreground lease is held and nobody is being answered, then drops the versions older than an hour and fails if a table's row count changes. `vector-optimize` runs the same compaction on demand, keeping seven days of versions unless the request names `older_than_days`, and it is a command with three locks: the `vector_optimize` flag off refuses it, a store that is not provably quiet refuses it (an old version can be the version a running read is holding — the quiescence proof is the same one compaction uses), and without `--apply` it only reports.

```sh
python -m kin_mind.host --config PRIVATE_CONFIG vector-optimize
python -m kin_mind.host --config PRIVATE_CONFIG vector-optimize --apply
```

It reports `versions_before`/`versions_after`, `rows_before`/`rows_after` and the bytes on both sides. Unchanged row counts are the half of the condition the code can check; `optimize` also merges small files and folds new rows into the existing index, and nothing here can prove an approximate search still returns what it returned, so compare recall over the same queries before and after.

## Which copy of the source runs

A deployment says where the code lives; the start-up self-check asks the interpreter whether it agrees. An editable install left behind in the virtual environment puts a second, older copy of `eventmem` or `kin_mind` on `sys.path`, and the current working directory comes before everything a deployment controls. Either is enough for a host to import code that was replaced days ago, and the only symptom is that a fix which was deployed appears not to have been.

Set `source_root` in the host configuration — the same directory the host already puts on `PYTHONPATH` — and every private entry point checks it before opening the store. `eventmem` and `kin_mind` must both resolve to a file under that root; otherwise the process prints where each one actually came from, next to the root it was told to use, and exits 78 without reading the request. The refusal is deliberately a process exit rather than an error result: an error result is one more line in a log, and the host would carry on running the wrong code.

Both paths are resolved, and a root that resolves to a different string is still compared with `samefile`, so a symlinked deployment, a case-insensitive volume or a tree reachable through two mounts is not mistaken for a foreign install. Everything it cannot establish refuses: a module that resolves nowhere, a root that is not on disk, a namespace package with no file. A root that was never configured is the one skip, because there is then nothing to compare against.

`KIN_ALLOW_FOREIGN_SOURCE=1` — that exact value, not `true` and not any other — starts anyway, for an operator deliberately running from somewhere else. `KIN_SOURCE_ROOT` supplies the root to a process that has no host configuration. The `eventmem` command line only warns and always continues: it is pointed at whatever checkout its operator meant to use, and the services that must not start on the wrong code refuse for themselves.

The interpreter is the other half of the same question, and the half that is easier to lose. A configuration naming a `python` that has been deleted fails every spawn, once per attempt, with nothing written down: the host keeps the process it already has, so it never notices it has stopped being able to start any others. So `python`, where the configuration names one, is checked too — the path exists, is a file, and is executable.

That one is only reported, never refused. Whatever finds it is by definition still running, and stopping it would take down the one path still able to say so. A host action prints a static warning line to stderr and carries on; note that a host spawned by the Node bridge has its stderr discarded, so `operational-status` is the channel that actually reaches an operator.

`operational-status` carries all of it under `source`: the verdict, and `shadows` — every other copy of either package still reachable on `sys.path`, listed even when the verdict is clean, because a shadow only wins when it comes first and that list is the warning that arrives before the fault. Uninstalling a stale editable install (`uv pip uninstall`, or removing its `.pth` from the environment's `site-packages`) is what empties it.

`source.interpreter` states its own subject, because a clean answer there would otherwise read as a remark about the process doing the asking — which is worthless, since that process demonstrably started. It is about the interpreter the **next** spawn will use. It reports `configured` (what the configuration says), `resolved` (what that path actually leads to), `state` (`usable`, `missing`, `not-a-file`, `not-executable`, `unreadable`, or `not-configured`) and `running` (the interpreter answering right now).

When `state` is `usable` but `running_is_configured` is false, the two disagree: something started this process from a path the configuration does not name, which is how a corrected configuration leaves a caller still spawning the old environment from a path hardcoded of its own. That comparison is on the paths, not on `samefile`: a virtual environment's `bin/python` is usually a link to a shared base build, so two entirely different environments — different packages, different installed code — are one file and two interpreters. `shares_base_interpreter` reports that separately, which distinguishes the ordinary shape of the fault (two environments over one Python) from the stranger one (two unrelated installations).

The long-running services answer for themselves as well: `/v1/health`, and the local embedding service's `/health`, report the source root and the commit its checkout was at when the process started, read from the repository's files without running git, so a service that outlived a deployment shows it.

## Contact callbacks

MCP and Python hosts can create, inspect and change source-backed reminders using the [contact task tools](contact-tasks.md). Configure a scoped policy first. Keep `eventmem serve` running for the worker to process due schedules. Model-composed reminder text retains model provenance; revisions and callback receipts distinguish scheduled tasks from delivered messages.

Start `uvicorn examples.v1.callback:app --host 127.0.0.1 --port 8320`. Configure a policy with a matching scope and `http://127.0.0.1:8320/callback`, then schedule a supported record. Enable automatic sending only through the policy settings. Default policies generate suggestions. `EVENTMEM_WEBHOOK_SECRET` signs the body; the example validates it when set. Its effect and delivery inbox commit in one SQLite transaction. External effects require the downstream service's own idempotency mechanism. Non-idempotent uncertain deliveries remain visible for reconciliation.

The callback runs with no database transaction open, so it may write back to MemoryPalace, including `acknowledge_delivery`, before it answers. Answer 2xx only after the effect is durable, and repeat the delivery id as `id` or `delivery_id` in the JSON answer. A 4xx answer is a definite refusal, and so is a connection that was never made; a delivery that has met nothing but refusals is tried again. After a 5xx answer, or a timeout or network error once connected, the same body is sent again under the same `Idempotency-Key` only when the policy sets `idempotent_channel` and the channel's last 2xx answer repeated the id; `outbox_channel_contracts` records that evidence per callback URL. Retries wait at most five minutes apart and stop at the occurrence's deadline, its due time plus 30 minutes, which no dispatch passes; a delivery nothing was sent for is then canceled with `gave_up`, and one that may have been sent stays `uncertain`. The body carries that very moment as `deadlineAt` (ISO 8601, UTC, to the millisecond, `Z`), under the signature like every other field, for a host that answers 2xx and sends later to check before its own send; a 2xx makes a delivery `sent`, handed to the host, and only an acknowledgment says it was delivered. A row an older build queued names no due time, and when it was queued is not one: it is held to the due time its schedule still holds under the row's own id and generation, frozen into the row, and one whose due time cannot be confirmed is held `suggested` with `hold.reason: "due-time-unknown"` for reconciliation, never sent on its own. Either way a recurring schedule moves on to its next occurrence, and a one-time schedule stays queued with its delivery's outcome. A due schedule whose policy is missing or does not validate is paused. The body is frozen at the first dispatch, so a later correction of the record never changes the bytes of a retry. `GET /v1/contact/outbox` adds `phase: "dispatching"` to a `sending` delivery whose request is out.

## Development and release checks

Run relevant business regressions after implementation:

```sh
uv sync --extra dev --extra vector --extra graph
uv run pytest -q tests/business
node --test tests/business/*.test.mjs
```

Client checks use `npm test --prefix sdk/typescript`, `npm test --prefix dsh-plugin` and `npm test --prefix console` after their dependencies are installed. Tests live under `tests/`; optional historical and fault replays stay outside the working tree, and the lifecycle replay and the light fixture refuse a live memory root.

`python scripts/generate_contract.py` writes `contracts/openapi.json`, the Python SDK's generated operations, types and stub, and the TypeScript SDK's operation table from the FastAPI app; with `--check` it writes nothing and exits 1 when a committed file is stale. `npm run generate --prefix sdk/typescript` writes `sdk/typescript/src/schema.ts` from the contract.

A release moves the package version in the commit it ships: `python scripts/release_version.py --bump [patch|minor|major]` writes the next version into `pyproject.toml` and into the package's own entry in `uv.lock`, and `--check REV` exits 1 when `src/eventmem`, `src/kin_mind`, `pyproject.toml` or `uv.lock` changed since `REV` but the version did not move up. After `uv build`, `python scripts/check_wheel.py` takes the one `dist/kin_mind-<version>-*.whl` of the current version and checks that it carries this tree's modules byte for byte, the typed SDK and the console assets. `python -I scripts/python_env.py [--extras graph,local-embedding,vector]` checks that the interpreter running it is the environment `uv.lock` resolves to for it and for the extras a deployment installs. `--extras` defaults to production's choice, graph, local-embedding and vector; empty means none. The lock's markers are evaluated for this Python and platform with those extras switched on, and every package in the closure reached from the project through its dependencies and those extras must be installed at the one version its edge selects, which for local-embedding is its own side of the conflict with media and all. Any other installed distribution the lock names must be at a version the lock selects for this interpreter beside those extras. It exits 0 on a match, 1 listing every difference, and 2 when it cannot decide: a marker it cannot parse, an extra the lock does not have, or extras the lock declares in conflict. It installs and downloads nothing.

`uv build` compiles console sources and includes the assets in both wheel and source distribution. Building from Git needs Node.js 22 and npm; installing a release wheel or building from its sdist needs no Node runtime. For editable development, run `npm ci --prefix console && npm run build --prefix console` when using the UI. Generated assets are ignored by Git.

CI checks affected components once. Python 3.11 compatibility is run for release tags or the manual release option; ordinary checks use Python 3.13. The core and client jobs run `generate_contract.py --check`; the client job also compares `schema.ts` with the contract, builds the wheel and runs `check_wheel.py`; the DeepSeek Harness plugin has a job of its own. Real-model replay, diagram rendering and large-scale benchmarks are deliberate acceptance tasks, not per-commit gates. Diagram sources and SVGs remain in the repository; render with `npm run docs:render` when a source changes.
