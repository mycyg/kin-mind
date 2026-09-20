# Mobile companion instructions and runtime upgrades

The mobile main session can load a private companion base through Codex's
`model_instructions_file`. The existing persona remains in `developer_instructions`.
Both texts stay private. Exploration and creation executors retain their own
contracts. Role instructions do not establish model identity, permissions or delivery.

`adapters/mobile-runtime-bundle.mjs` implements `prepare`, `verify`, `activate`,
`status`, `resolve` and `rollback`. Preparation copies the actual CLI and explicit
ACP dependency closure into a versioned mobile directory. The host-owned adapter
is separate from the pristine vendor package; desktop installations are untouched.

Verification runs the retained native CLI and ACP against a local synthetic
provider, capturing native RPC and HTTP. It covers new and existing sessions,
maintenance promotion, compaction recovery, restart and model/effort/Fast round
trips. It proves instruction transport and compatibility, not real model answer
quality, upstream service tier or operating-system isolation. Caller-authored
observations are not accepted as native evidence.

Activation requires a verified main-session candidate and atomically records
current and previous versions. Repeated activation is idempotent. Damaged files
fail closed instead of selecting the global CLI. A failed future candidate leaves
the active version running. The first migration separately retains its pre-bundle
runtime and host rollback procedure. Rollback preserves new chats and receipts.

Declared configuration, prepared files, native loading and verified requests are
distinct. Codex 0.155 can return empty `instructionSources` despite correct request
content. It trims base-file whitespace and places the base in `instructions` or a
developer message depending on the model. The runner checks observed forms without
inventing metadata. A private gateway observer can verify production adoption only
for a completed request with matching native headers, metadata, current host session
and base/developer pair. Drafts and failed or unbound requests remain unverified.

File digests protect release and instruction boundaries. Ordinary state uses
versions and transactions; a digest is not a semantic decision or delivery receipt.
Complete changes first, then run affected checks without repeating unchanged matrices.
