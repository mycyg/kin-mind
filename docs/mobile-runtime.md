# Mobile companion instructions and runtime upgrades

The private host loads a companion base through `model_instructions_file` and
generates `developer_instructions` from the approved persona and speaking style.
The private text and shared experiences are not part of this public repository.
Kin has her own interests and feelings; she may disagree, defer, do less or
decline work based on her actual mood and reasons, without a fixed score cutoff.
A decline settles through the existing task and delivery records, never as a
claim that unfinished work was completed.

## Instructions in the native session

The phone's app-server runs on Kin's own Codex home, `state/codex-home`, which
the deployment names in `CODEX_HOME`; the desktop's home is never read. Before
each start the host regenerates its `config.toml` from a non-secret template,
the mind configuration and the chat permissions: the memory tool server and
optional computer use, with apps, plugins, multi-agent work, hooks, native
memories and the skill instruction layer off. `auth.json` links to the desktop
login and is never read. An `AGENTS.md` in the home stops the launch instead of
being injected; `kin-check` reports configuration drift, a changed auth link, an
`AGENTS.md` or `hooks.json` in the home and a `.codex` project layer in the
session's working directory.

The ACP launch configuration adds the companion base, the developer
instructions, the candidate's frozen model catalog,
`include_collaboration_mode_instructions=false`, `project_doc_max_bytes=0` and a
`compact_prompt`; the candidate's pinned launcher repeats the switches that shape
every request. The frozen catalog uses standard Responses
(`use_responses_lite=false`) and omits the multi-agent version that would add
agent-team layers. Project rules and operational guides are read when needed;
native tools and actual permissions still come from Codex.

The `compact_prompt` keeps the current topic, relationship context, mood,
corrections and agreements, Kin's own interests and decisions, the owner's goal
and authorized scope, each open task's identity, input version, progress and
next step, and which replies were sent with which receipts. The native proof
shows it in a compaction request on a loopback provider; compaction performed on
the provider's side is outside that proof. Session management restores a sourced
checkpoint after every completed compaction, before the next dispatch
([mobile sessions](mobile-sessions.md)).

Complete model choices, reasoning settings and Fast preference are retained; an
actual service tier needs a native or provider receipt.

## Candidate verification and activation

Public `adapters/mobile-runtime-bundle.mjs` provides versioned preparation,
verification, activation, status, resolution and rollback. A bundle holds the
Codex binary, the ACP package with its allow-listed dependency closure and an
owned ACP entry, each hashed in a manifest that records no absolute source path.
The owned entry is generated from the unmodified vendor entry by string patches
whose anchors must each match exactly once, so an upstream change stops
preparation for review. It adds the `_kin/*` extensions session management uses
(runtime status, last reply, bounded compaction with a receipt that `checkOnly`
reads back, checkpoint injection, session retirement, input status and read-only
assessment forks) and passes the companion developer instructions with every
turn. Installing this library does not attach a mobile session or install a
Codex release.

Verification runs the repository's own proof runner, never caller-supplied
evidence. It starts the bundled ACP and launcher in the phone's shape (the
candidate's Codex home settings without tool servers or credentials) against a
local synthetic provider, captures native RPC and HTTP, and checks instruction
loading, new and resumed sessions, a maintenance candidate, compaction with the
candidate's `compact_prompt`, restart, model, effort and Fast-preference changes,
and one local tool call. Requests may carry only the companion base, the
developer layer and permission and model-switch notes, never a catalog template
or an `AGENTS.md`. The receipt calls itself a protocol proof, lists what it does
not cover (live provider requests, provider-side compaction, tool servers,
credentials and the host launch script) and records whether this Codex is inside
the range the ACP package declares.

Current and previous bundles share one atomic activation index; activating the
current bundle again changes nothing. Rollback returns to the previous verified
bundle and refuses a Codex older than the one that has written the thread unless
`--allow-downgrade` is given. An active bundle that fails verification is not
started; nothing falls back to the global CLI or changes a desktop installation.

## Daily upgrade

The private host's daily check (a launchd job) runs `kin-check` and upgrades
only an install that passes. It compares the installed stable Codex and a
fingerprint of the candidate's inputs (the binary, the active bundle's ACP, the
approved persona, the sources that shape the bundle, instructions and proof, and
the frozen catalog) with the active candidate, and prepares and proves a new one
when they differ. A rejected candidate is not retried until its inputs change or
a retry is requested; failed attempts are set aside, never deleted. Preparation
never moves the activation pointer or restarts anything.

A verified candidate is activated only through `kin-deploy runtime-upgrade`.
After 15 minutes without owner input on either channel it freezes dispatch,
waits up to 10 minutes for in-flight work, stops the host, applies the
candidate's configuration increment, moves the activation pointer, regenerates
Kin's Codex home, restarts and verifies the host (`kin-check`, a read-only tool
call, nothing in flight) and thaws. A failure from the stop onward restores the
configuration and the previous pointer, restarts the host and sends the owner a
system notice. The conversation binding, history, tasks and receipts are not
moved.

At service start the host refreshes the observed provider catalog with the
active bundle's Codex on Kin's home; the result feeds the next candidate, and
only a proven candidate's frozen catalog is in effect. Creation and exploration
run the active bundle's Codex, or an explicit `native_codex_command`, with Kin's
Codex home when it exists, and keep their own contracts. Their configured
`creation_command` or `exploration_command` runs only where no runtime bundle is
installed, and the receipt names it `legacy-command`; an unreadable bundle makes
them wait rather than use the desktop CLI.

## Evidence

Declared configuration, prepared files, native loading and verified requests
are distinct facts. Native `instructionSources` can be empty despite correct
request content, and native loading trims the base file's surrounding
whitespace; the proof checks the captured request instead of inventing metadata
and counts title-generation requests apart from the turn they serve. A file
digest protects release bytes, not a semantic decision or delivery receipt.

In production, a completed gateway request bound to the current session, or the
main thread's recorded developer message followed by a turn, verifies the
running instructions. A resumed thread keeps the developer message it recorded
until a compaction or a new thread rebuilds its context; until then the host
reports `developer-differs`. If the persona file fails verification, differs
from the approval record or does not project to the pinned instructions, the
phone starts on the active candidate's frozen, last approved instructions and
reports the drift. A new persona takes effect only through a new candidate.
