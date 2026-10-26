# Shared persona contract

A host can install `persona-policy.json` in its private memory root. The file stays
outside the public repository and seeds the persona: its `core`, `voice` and
`maintenance` strings, with the host's own sections, become the code default of the
editable `prompts/persona` document. Other scopes keep their existing behavior.

There is no approval gate. The persona is Kin's own: Kin edits `prompts/persona`
and the other project prompts through `edit_prompt`, and every model consumer reads
the current document on its next request. The `*_sha256` fields that
`readPersonaContract(file)` computes, and the host's `persona_contract` record of the
path, version and hashes, are metadata only. A restore from backup puts the
backup's seed and prompts in place; its `persona_approval` argument is accepted and
not checked.

`approved_source` still names what a read may not take for lived experience: the
source ids it lists, and the sources of any record id it lists, are classified as
role configuration and are returned to a self-knowledge or audit read only. See
[reading purpose and evidence classes](architecture.md#reading-purpose-and-evidence-classes).

## Edits, defaults and history

`PromptStore` keeps each document with its versions in `.history`. Every save
records the input that led to it (`source_input_id`) and whether that turn read a web
page or a file (`external_read`); `history` lists the versions and `restore` saves
one again as the newest. `restore` with `default` makes the document follow its
code default again.

An edit is never overwritten by code. When the code default changes after an edit,
the document is stale: `read` shows the new default as `default_text`, and the host
tells its owner and Kin once. Writing a merged text, writing the same text again
(keeping the edit), or restoring `default` each settle it. Whole system texts that
are assembled per call from their context (`assessment/<name>`, `provider/<role>`)
are frozen at the saved variant and never marked stale.

A new seed takes effect only while `prompts/persona` follows its default. A host
that ships a new seed while Kin's edit stands refuses the change as
`persona-edit-conflict` rather than reporting it applied.

`tests/business/persona-contract.test.mjs` and `persona-approval.test.mjs` use only
synthetic roles.
