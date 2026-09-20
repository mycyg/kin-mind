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

## Evidence key index

A single underlying event may be appraised once. The record of what has already been scored used to exist only inside the state snapshot of each `affect` history row, so the guard read it by scanning every snapshot ever written — inside the write transaction of every new appraisal. `mind_evidence_keys` holds that one fact on its own, keyed by `(scope, evidence_key)`, with the event and the revision that introduced it. A store written before the table existed fills it once:

```sh
python -m kin_mind.host --config PRIVATE_CONFIG evidence-keys-backfill
python -m kin_mind.host --config PRIVATE_CONFIG evidence-keys-backfill --apply
python -m kin_mind.host --config PRIVATE_CONFIG evidence-keys-verify
```

`--dry-run` is the default and writes nothing at all, neither the rows nor the cursor; asking for both a dry run and an apply is refused. The backfill walks the `affect` rows oldest first, a batch per write transaction because production has other writers, and inserts each key only if it is not there already: a row that never went through the scoring path carries the key of the row before it, so the row that really introduced a key is the one recorded. Progress is the `cursor` of the `mind_memory_migrations` row named `evidence-keys-v1`, so an interrupted run resumes where it stopped and a finished one can be rerun without reading anything twice. An apply verifies itself when it has finished and reports what it found under `verification`, so its own `state` is `complete` only when the two sides line up.

`evidence-keys-verify` compares the two directions row by row and is read only: forward, every key a history row introduced is in the table against the event and revision that introduced it; backward, every row of the table was introduced by a history row that is still there. `missing` would let evidence be scored twice, `extra` would refuse evidence that never was, and `mismatched` means the table credits the wrong row. Both actions work one scope at a time, the scope the configuration names.

The table is never the only reader. Before it is consulted, a catch-up reads the rows written past the cursor, which is how a period spent on the previous release, or an imported history, cannot leave a hole. Run the backfill once soon after deploying, because a cursor still at zero leaves that first reading to the catch-up, inside the write transaction of whichever appraisal reaches it first. While both `evidence_key_index` and `history_legacy_guard` are on, the guard refuses when either the table or the old scan says it has seen the key, and a disagreement between them is recorded as the `evidence_key_guard_mismatch` metric — with the key digest and which side said what, never any evidence text. Turning `evidence_key_index` off leaves the old scan alone, which is the previous behavior exactly. Turning `history_legacy_guard` off stops paying for that scan and belongs only to a store whose snapshots no longer carry the key, once the two have agreed for long enough to be believed.

## Stored history

Every revision of the mind's state used to be stored as a whole document. That is what makes the history inspectable and it is also why it grew into a third of the database: a document whose wishes are never pruned is written again on every change, however little of it moved. With `history_patches` on, a revision is stored as a keyed diff against the row before it — `["set", [key] or [key, key], value]` and `["del", path]`, with lists replaced whole — inside the same column, with no new table and no new column anywhere. On a synthetic history of 121 revisions over an 82 KB document that is 6.16 MB of rows against 432 KB, a saving of 93 %, for 0.3 ms added to the write.

`history_patches` is **off by default**, and it is the only flag in this stage that is. The others choose how fast something runs, so leaving one on costs nothing worse than speed; this one chooses what a revision is written as, and from the first patch row committed, the release before this one can no longer rebuild the newest states. So it deploys off and is turned on as its own decision:

```sh
python -m kin_mind.host --config PRIVATE_CONFIG history-status
python -m kin_mind.host --config PRIVATE_CONFIG history-verify
```

`history-status` says what the rows are: how many carry a whole state, how many carry a patch, how deep the chains have grown, what they cost, and `first_patch_revision` — which is empty until the format changes and is the **rollback boundary** once it is not. Before that revision the previous release reads every row as it always did. After it, rolling back means rolling back to the release that deployed this code with the flag off, not to the release before that one. Take a rollback drill on a copy before turning it on.

`history-verify` rebuilds every revision in one forward pass and checks each document against the hash its row recorded. Both commands are read only and safe against a live store. Three outcomes, and the middle one is why there are three: `verified` means every row was checked and every one held; `incomplete` means something failed, and `failures` names the revisions; `partial` means nothing failed but some rows **could not be checked at all**. Those are the rows written before this release: they carry no hash, they are never rewritten, and nothing can be said about their contents beyond that they parse. They are counted as `unverifiable` and never added to the verified total, because a clean report about rows nobody looked at is the kind of thing that gets believed later.

A row is kept whole rather than stored as a patch when its kind is `initialize` or a personality `evolution`, when the chain under it would reach fifty rows, when the patches standing on one whole state have cost as much as that state did, and when a single patch would cost more than half of it. It is also kept whole when the row before it is missing, will not parse, or does not hash to what it claims: the writer never builds on a chain it cannot read and never tries to repair one, so it writes a whole state, records `healed` in the row, and everything after that stands on the new ground. That covers the row before; a break further down is what `history-verify` finds. To repair one, turn `history_patches` off for a single revision — the next row is then a whole state again, which is a checkpoint by another name — and turn it back on.

While compaction owns the rows, `meta.history_compaction_active` is set and no revision can be written at all: the commit is refused with `history-compaction-active` and nothing lands, because a row written in the middle of a rewrite is a row the archive does not hold.

## Compacting the history already written

Turning the flag on changes what the next revision is stored as. It does nothing to the rows already there, and in a store that has been running a while those are nearly all of them. `history-compact` rewrites them: the `data` column becomes what changed since the row before instead of a whole document, and nothing else about the row moves. Every row stays, every column stays, and `id`, `scope`, `revision`, `kind` and `occurred_at` are not written at all — four of the readers of this table want the columns and never look inside `data`.

This is the only command in the stage that touches data the store already holds, so the archive comes first — all of it. A run is two phases, and before either begins the run freezes a manifest of every row it will touch and proves the backup against it (the preconditions below say what that proof is). Phase one copies every row the run will touch, verbatim, into a separate database, `archive/mind-events-v1-<date>.sqlite3` beside the store at mode 0600, fsyncs it, then reads each row back on a fresh connection and compares it column by column with what is still in the store. Only when the whole set is archived and verified does the rewrite phase begin; it refuses any row the archive phase did not cover, and each batch's rewrite, the rebuild that checks it and the cursor that records it are one transaction. **Nothing here ever deletes the archive**, on success or on failure.

```sh
python -m kin_mind.host --config PRIVATE_CONFIG history-compact
python -m kin_mind.host --config PRIVATE_CONFIG history-compact '{"apply":true}'
python -m kin_mind.host --config PRIVATE_CONFIG history-compact-verify
```

Without `apply` it writes nothing at all: it checks every precondition and reports each verdict rather than stopping at the first, plans what each row would become, and computes one batch of real patches to say what the saving would be. With `apply` a failed precondition is a refusal that has written nothing.

Four preconditions, every run, none of them skippable. The store must be **quiet** by the whole proof in the liveness section — both pid files naming processes that are gone or are something else, a host status that is stopped or has not beaten for forty-five seconds, no fresh lease in any of the five lease tables, no live exploration, and an exclusive lock available. There must be a **backup** of the database that can be shown to hold this history — and "shown" is not counted: the run freezes a manifest of the store's content identity (one digest folded over every row's key columns and bytes) and of every target row's columns and content hash, opens the backup read-only, runs SQLite's `quick_check` on it, and compares it against the manifest row by row; what was trusted — the file's hash, size, permissions and name — is recorded in the run's own progress. One is taken beside the store, named for the history it is a copy of including the first bytes of that digest, unless a matching one is already there or the operator names a file; a named file that is of something else is refused rather than replaced, and same shape with different rows is something else. A resumed run checks the backup against the manifest frozen at the run's start, never against rows the run itself has already rewritten. There must be free **disk** for that backup, the archive and working room. And `meta.history_compaction_active` is set before the first batch, so the writer refuses revisions while this owns the rows.

That marker is **not self-clearing**. A run that stops part way leaves it set and the store closed to new revisions until someone decides: run `history-compact` again, which carries on from the cursor, or `history-restore`, which puts the archived rows back and opens the store again. The cost of that is a host that stays stopped; the cost of clearing it would be a revision written into a half-rewritten history.

Rows that keep their whole document are the first row, every fiftieth row, every `evolution` row and the row before it, and the row under any row that was already a patch — the last of those is what keeps the depth recorded in an existing patch true, since it recorded the chain under it as nothing. Two more are the command's own and can only keep more rows whole: a patch that would not be smaller than the document it replaces, and a chain that would reach the writer's own bound. The report counts each reason separately.

`history-compact-verify` is the check the rewrite is allowed to have happened for, and it is the one to read. `history-verify` asks whether a row still hashes to what it claims; after a compaction that is two halves of the same pass agreeing with each other, which is why its report now carries `compacted_through` and says so. `history-compact-verify` rebuilds each revision from the store and compares it with the **archived original bytes**, which were copied out before the first row moved. `deep` rebuilds each revision from its own nearest whole document instead of carrying the state forward: the same answer reached independently, much slower, and worth one run on a copy before a release.

`history-restore` writes the archived originals back row by row. It needs the same quiet, it compares the columns beside `data` and refuses if one of them has moved, it leaves the archive exactly where it is, and it opens the store again.

## Wish archive

The state document carries every wish that was ever made, and it is written again on every
revision. Most of them are finished: a hundred and twenty wishes, over half the document by bytes,
all but a few already completed or abandoned. `mind_desire_archive` holds the finished ones that
nothing is still waiting on, whole — the same identifier, the same plan links, the same evidence
references, the same decision receipt — and the document carries only what is still live plus the
tail of finished wishes the appraisal projection shows.

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
is what runs **before a rollback**, because the release before this one cannot see the archive at
all and would read those wishes as gone. Nothing is ever deleted — an unarchived wish leaves the
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

**The cache sweep is the one hard delete in this stage, and it deletes a derived cache and nothing else.** Everywhere else a bounded table is bounded by moving rows into an archive that is never deleted, because those rows are the owner's. These rows are the model's working copy of rows that stay where they are: drop one and the next reader misses, pays one model call, and writes the same summary again — which is not an argument about the design but something this store has already lived through, when the evidence-isolation migration took 80 of these rows out of service and the whole consequence was that they were compressed again. So `context_cache_sweep` is **off** until someone has been told exactly that and has agreed to it, and off the tick only counts.

Two rules decide what goes: an age of 14 days on the row's own `created_at`, and a cap of 2,000 newest rows per scope on whatever survives the age. Two guards decide when: the sweep **never deletes the newest row**, because `Appraisals._cache_mark` takes `MAX(rowid)` of this table before an appraisal pass and counts the rows written after it — deleting the highest row would let SQLite hand the same number out again and a pass that did make progress would look stalled, which ends in a quarantine; and it **refuses while a running appraisal still holds a fresh lease** (`worker-lease-fresh`), because those are the rows that pass is caching right now. A sweep that held a row back says so with `retained_newest`.

Nothing else is reachable from there. The two statements that can remove a cached summary spell `mind_context_cache` out in full, and no table name in that module is ever built or passed in; each row goes by its own identifier, out of a plan that can be printed first; and `mind_context_windows` — what was actually delivered and read — is touched by none of it. Nothing runs the tick on its own either: like the backfill above, it is an operator action, so a sweep happens when somebody runs one.

An erase is the other half. Erasing a record already clears the stored command responses, session sets and prefetch rows that may hold the same text; with `context_cache_sweep` on it clears the compressed context of that scope as well, and only of scopes whose own configuration asked for the sweep. **With the flag off, that gap stays open**: text erased from the records can still be sitting in a summary in this table, which is worth knowing before deciding the flag is not urgent.

`metrics_name_ring` gives the telemetry table a ring per name. One shared ring of 20,000 rows is the same bound applied in the wrong place: `model_ms`, `model_cost` and `model_tokens` are written on every model call, so a day of work pushes `recall_ms`, `appraisal_quarantined` and `structured_rejected` out of the window entirely — and the store then cannot report its own recall latency, because `Engine.overview` reads the newest 2,000 rows of the table and none of them are that name. With the ring on, a name keeps its own newest 2,000 rows, a name can only ever evict itself, and that reader asks per name as well. Turning it on trims each name that is already over the ring on its next write; the tick with `--apply` does the same for every name at once. The tick's report lists the distribution, what the ring would remove, and which names are already `crowded_out` of the read window.

Lance keeps one manifest per write, and a store that writes all day accumulates thousands of them against a much smaller amount of data. `vector-optimize` removes them, and it is a command with three locks: the `vector_optimize` flag off refuses it, a store that is not provably quiet refuses it (an old version can be the version a running read is holding — the quiescence proof is the same one compaction uses), and without `--apply` it only reports.

```sh
python -m kin_mind.host --config PRIVATE_CONFIG vector-optimize
python -m kin_mind.host --config PRIVATE_CONFIG vector-optimize --apply
```

It reports `versions_before`/`versions_after`, `rows_before`/`rows_after` and the bytes on both sides. Unchanged row counts are the half of the condition the code can check; the other half is the operator's, because `optimize` also merges small files and folds new rows into the existing index, and nothing here can prove an approximate search still returns what it returned. **Compare recall over the same queries before and after, and do not enable this in a release until that comparison has been made.**

## Which copy of the source runs

A deployment says where the code lives; until the start-up self-check, nothing asked the interpreter whether it agreed. An editable install left behind in the virtual environment puts a second, older copy of `eventmem` or `kin_mind` on `sys.path`, and the current working directory comes before everything a deployment controls. Either is enough for a host to import code that was replaced days ago, and the only symptom is that a fix which was deployed appears not to have been.

Set `source_root` in the host configuration — the same directory the host already puts on `PYTHONPATH` — and every private entry point checks it before opening the store. `eventmem` and `kin_mind` must both resolve to a file under that root; otherwise the process prints where each one actually came from, next to the root it was told to use, and exits 78 without reading the request. The refusal is deliberately a process exit rather than an error result: an error result is one more line in a log, and the host would carry on running the wrong code.

Both paths are resolved, and a root that resolves to a different string is still compared with `samefile`, so a symlinked deployment, a case-insensitive volume or a tree reachable through two mounts is not mistaken for a foreign install. Everything it cannot establish refuses: a module that resolves nowhere, a root that is not on disk, a namespace package with no file. A root that was never configured is the one skip, because there is then nothing to compare against.

`KIN_ALLOW_FOREIGN_SOURCE=1` — that exact value, not `true` and not any other — starts anyway, for an operator deliberately running from somewhere else. `KIN_SOURCE_ROOT` supplies the root to a process that has no host configuration. The `eventmem` command line only warns and always continues: it is pointed at whatever checkout its operator meant to use, and the services that must not start on the wrong code refuse for themselves.

The interpreter is the other half of the same question, and the half that is easier to lose. A configuration naming a `python` that has been deleted fails every spawn, once per attempt, with nothing written down: the host keeps the process it already has, so it never notices it has stopped being able to start any others. So `python`, where the configuration names one, is checked too — the path exists, is a file, and is executable.

That one is only reported, never refused. Whatever finds it is by definition still running, and stopping it would take down the one path still able to say so. A host action prints a static warning line to stderr and carries on; note that a host spawned by the Node bridge has its stderr discarded, so `operational-status` is the channel that actually reaches an operator.

`operational-status` carries all of it under `source`: the verdict, and `shadows` — every other copy of either package still reachable on `sys.path`, listed even when the verdict is clean, because a shadow only wins when it comes first and that list is the warning that arrives before the fault. Uninstalling a stale editable install (`uv pip uninstall`, or removing its `.pth` from the environment's `site-packages`) is what empties it.

`source.interpreter` states its own subject, because a clean answer there would otherwise read as a remark about the process doing the asking — which is worthless, since that process demonstrably started. It is about the interpreter the **next** spawn will use. It reports `configured` (what the configuration says), `resolved` (what that path actually leads to), `state` (`usable`, `missing`, `not-a-file`, `not-executable`, `unreadable`, or `not-configured`) and `running` (the interpreter answering right now).

When `state` is `usable` but `running_is_configured` is false, the two disagree: something started this process from a path the configuration does not name, which is how a corrected configuration leaves a caller still spawning the old environment from a path hardcoded of its own. That comparison is on the paths, not on `samefile`: a virtual environment's `bin/python` is usually a link to a shared base build, so two entirely different environments — different packages, different installed code — are one file and two interpreters. `shares_base_interpreter` reports that separately, which distinguishes the ordinary shape of the fault (two environments over one Python) from the stranger one (two unrelated installations).

## Contact callbacks

MCP and Python hosts can create, inspect and change source-backed reminders using the [contact task tools](contact-tasks.md). Configure a scoped policy first. Keep `eventmem serve` running for the worker to process due schedules. Model-composed reminder text retains model provenance; revisions and callback receipts distinguish scheduled tasks from delivered messages.

Start `uvicorn examples.v1.callback:app --host 127.0.0.1 --port 8320`. Configure a policy with a matching scope and `http://127.0.0.1:8320/callback`, then schedule a supported record. Enable automatic sending only through the policy settings. Default policies generate suggestions. `EVENTMEM_WEBHOOK_SECRET` signs the body; the example validates it when set. Its effect and delivery inbox commit in one SQLite transaction. External effects require the downstream service's own idempotency mechanism. Non-idempotent uncertain deliveries remain visible for reconciliation.

The callback runs with no database transaction open, so it may write back to MemoryPalace, including `acknowledge_delivery`, before it answers. Answer 2xx only after the effect is durable, and repeat the delivery id as `id` or `delivery_id` in the JSON answer. A 4xx answer is a definite refusal and may be retried. After a 5xx answer, a timeout or a network error the same body is sent again under the same `Idempotency-Key` only when the policy sets `idempotent_channel` and the channel's last 2xx answer repeated the id; `outbox_channel_contracts` records that evidence per callback URL. The body is frozen at the first dispatch, so a later correction of the record never changes the bytes of a retry. `GET /v1/contact/outbox` adds `phase: "dispatching"` to a `sending` delivery whose request is out.

## Development and release checks

Run relevant business regressions after implementation:

```sh
uv sync --extra dev --extra vector --extra graph
uv run pytest -q tests/business
node --test tests/business/*.test.mjs
```

Client checks use `npm test --prefix sdk/typescript`, `npm test --prefix dsh-plugin` and `npm test --prefix console` after their dependencies are installed. Tests live under `tests/`; optional historical and fault replays stay outside the working tree.

`uv build` compiles console sources and includes the assets in both wheel and source distribution. Building from Git needs Node.js 22 and npm; installing a release wheel or building from its sdist needs no Node runtime. For editable development, run `npm ci --prefix console && npm run build --prefix console` when using the UI. Generated assets are ignored by Git.

CI checks affected components once. Python 3.11 compatibility is run for release tags or the manual release option; ordinary checks use Python 3.13. Real-model replay, diagram rendering and large-scale benchmarks are deliberate acceptance tasks, not per-commit gates. Diagram sources and SVGs remain in the repository; render with `npm run docs:render` when a source changes.
