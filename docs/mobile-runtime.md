# Mobile companion instructions and runtime upgrades

The private host loads a companion base through `model_instructions_file` and
generates `developer_instructions` from the approved persona and speaking style.
The private text and shared experiences are not part of this public repository.
Kin has her own interests and feelings; she may disagree, defer, do less or
decline work based on her actual mood and reasons, without a fixed score cutoff.
A decline settles through the existing task and delivery records, never as a
claim that unfinished work was completed.

## Instructions in the native session

The host does not copy the full desktop AGENTS file. Mobile configuration sets
`include_collaboration_mode_instructions=false` and `project_doc_max_bytes=0`
to avoid repeating general collaboration or project instructions. Project rules
and operational guides are read when needed. Native tools, skills and actual
permissions still come from the CLI.

The existing `compact_prompt` retains the current topic, relationship context,
Kin's own interests and decisions, corrections, agreements and open tasks. The
mobile model catalog uses standard Responses (`use_responses_lite=false`) for
instruction transport. Complete model choices, reasoning settings and Fast
preference are retained; an actual service tier needs a native or provider
receipt. Exploration and creation executors retain their own contracts.

## Candidate verification and activation

Public `adapters/mobile-runtime-bundle.mjs` provides versioned preparation,
verification, activation, status, resolution and rollback. It retains the
actual CLI and explicit ACP dependencies, keeping the host-owned adapter
separate from the original vendor package. The private host owns persona
projection, candidate discovery and maintenance scheduling. Installing this
library does not attach a mobile session or install every new CLI release.

During its existing daily maintenance cycle, the private host checks the
locally installed stable Codex version. It prepares a candidate when the
installed version or relevant adapter source changes and does not repeatedly
test an unchanged failed candidate. The public proof runner connects the
candidate CLI and ACP to a local synthetic provider, capturing native RPC and
HTTP. It checks instruction loading, new and resumed sessions, maintenance
continuation, compaction recovery, restart, model and Fast-preference changes,
and a local tool call. These checks establish protocol compatibility and
instruction transport; real model behavior is evaluated separately.

Only a verified candidate is activated after the existing coordinator becomes
idle. The session, messages, tasks, history, full model profile and delivery
receipts remain intact. A failed candidate leaves the previous version in use;
the same failure is recorded rather than fixed by an automatic patch. Current
and previous bundles share one atomic index. The first migration also retains
its pre-bundle rollback path, and rollback preserves later chats and receipts.
Damaged bundle files do not silently select the global CLI or change desktop
installations.

## Evidence

Declared configuration, prepared files, native loading and verified requests
are distinct facts. Native `instructionSources` can be empty despite correct
request content, and native loading may normalize trailing whitespace; proof
checks the captured request rather than inventing metadata. A file digest
protects release bytes, not a semantic decision or delivery receipt. The owned
ACP adapter reapplies stable developer instructions on ordinary turns and
profile changes. `_kin/last-reply` exposes the complete final and original
input text for binding an internal assessment to its native turn without a
second receipt ledger.
