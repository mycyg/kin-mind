# Shared persona contract

A host can install `persona-policy.json` in its private memory root to keep an
owner-approved role consistent across chat, memory extraction, summaries,
portraits, affective appraisals and daily personality review. The file stays
outside the public repository. Other scopes keep their existing behavior.

The contract contains `schema: 1`, `version`, the complete four-field `scope`,
`approved_source`, `requires_owner_confirmation: true`, and the strings `core`,
`voice` and `maintenance`. Each string has a corresponding `*_sha256` containing
the SHA-256 of its UTF-8 bytes. `core` starts with `【MY_PERSONA_LOAD】` and ends
with `【/MY_PERSONA_LOAD】`. `mutable_trait_keys` names the interests or habits that
the existing evidence-based evolution workflow may change.

The host installs the approved core before SOUL in its instruction projections.
The JavaScript adapter checks the exact core and hashes before synchronization.
The Python consumers load the same scope-bound contract before model requests.
Their structured output schemas and evaluator roles remain intact. Quotations
remain verbatim, and historical assistant wording is evidence rather than a
template for the current voice. Metadata exposes the loaded version and hashes.

Personality proposals and reversions cannot write trait keys outside the approved
list. State scores, wishes and the existing evidence requirements remain separate.
This does not turn initialized traits into observed emotions or let a generated
portrait change the role agreement.

An explicit owner request is required to amend the core. The host records that
source, archives the prior contract, installs a new version and hashes, and
refreshes all projections. Automatic memory cleanup, repair, model switching and
persona synchronization must not generate that approval. Operational settings
remain editable through their existing authorized interfaces.

Hashes detect drift; they are not OS access control. An actor with permission to
rewrite both the contract and its hashes still has filesystem access. Hosts must
enforce the approval boundary in their configuration workflow and instructions.
Already-running turns retain their loaded instructions until an idle reload.

`tests/test_persona_contract.py` and `adapters/persona-contract.test.mjs` use only
synthetic roles. They cover scope isolation, stable prompt propagation, evaluator
receipts, frozen trait writes and detection of changed instruction prefixes.
