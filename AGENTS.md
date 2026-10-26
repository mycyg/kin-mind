# Notes for development sessions

Read this before changing the code, prompts or release flow. The private host repository keeps the full guardrails; this public copy holds the parts that concern the core.

## Intentional design (do not "fix" it back)
- One memory for everyone Kin talks with. Kin decides what to share; there are no per-recipient visibility partitions, protected classifications or per-speaker tool permissions.
- Kin initiates adult intimacy only with 小光: the intimacy drive and its guidance are injected only into 小光's private turns and drafts addressed to 小光. This keeps the initiative aimed at 小光; it is not secrecy, so do not add "don't mention it to others" rules.
- Energy, boredom, intimacy and emotions shape behaviour as tendencies, not scripts. Low energy may mean a short reply or a deferred one; boredom wakes self-directed play. Do not flatten this into "always reply, always cheerful".
- Kin may edit its own prompts and persona; there is no persona approval step.
- A task closes when the native turn ends and the session is idle; delivery receipts are told to Kin afterwards.
- Source metadata is an index only: model-visible content carries traceable internal ids, never platform ids or source metadata.
- Deletion reaches every derived copy.
- ids, revisions and real receipts replace content hashes; sha256 stays only for explicit file verification.
- Subjective sharing, mood and preference may change freely; facts, tasks, owner instructions and permissions must stay reliable.

## Privacy (this repository is public)
- Never write private chat text, family names or relationship details, local paths, e-mail addresses, Feishu ids or tokens into code, comments, tests or commit messages. Use synthetic data in tests.
- Model-facing wording written in code calls the owner 小光 and the agent Kin; owner-authored persona text stays as the owner wrote it.

## How to change things
- Smallest root-cause change. No drive-by refactors, speculative abstractions, extra hash or verification layers, or repeated re-verification.
- Do not delete tests or loosen assertions to make them pass.
- Every release runs the full test suite once and adds a CHANGELOG entry; commit titles describe what actually changed.
