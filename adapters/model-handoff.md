# Cross-provider continuity

A switch into or out of a gateway provider prepares an isolated native candidate. It
combines the existing `session-checkpoint` builder (`allow_model: false`) with a
bounded readable view of the source native history. Preparation does not start a
model turn; an explicit candidate title prevents automatic model-generated naming.

The independent checkpoint holds selected public dialogue, sourced cached summaries,
recent verbatim exchanges, tasks and memory references. Its completeness and coverage
refer to the selected snapshot, not to every byte of the native thread. The native
reader keeps messages and complete tool call/result pairs as historical data. An
explicit `compacted` replacement supersedes earlier active records. Opaque compaction
and reasoning items are not portable plaintext. Coverage records omissions and
unpaired items, declares `lossless: false`, and retains a source thread ID and hash.
The original rollout is never rewritten or deleted. The success notice tells the
owner that earlier original history remains in the source session.

The candidate is injected and its persisted checkpoint and native profile verified
without a model request. The ACP launcher must expose the selected model through its
catalog; gateway candidates use an explicitly bound local provider alias. Only after
source revisions, source bytes and the idle boundary still match does the existing
registry commit one new generation. Existing promotion recovery owns indeterminate
commits; pre-commit failures and cancellation retain the original binding. Cancellation
after commit must complete or recover that commit. A second simultaneous switch or
new delivery cannot silently replace the prepared source.

Offline tests cover both provider directions, bounded/compacted history, paired tools,
source changes, cancellation, concurrency and restart recovery. The opt-in host test
uses the pinned native ACP/app-server with an isolated home and synthetic loopback
server, checks zero preparation requests and removes its own subprocesses. This is
not proof that a real provider will accept the same request or that opaque context
has been reconstructed. Real endpoint acceptance needs a separately approved budget.
