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

`approved_source` also names what a read may not take for lived experience: the
source ids it lists, and the sources of any record id it lists, are classified as
role configuration and are returned to a self-knowledge or audit read only. The
contract is read leniently, so a contract awaiting host review never stops memory
from being read. See [reading purpose and evidence classes](architecture.md#reading-purpose-and-evidence-classes).

The host installs the approved core before SOUL in its instruction projections.
The file's own hashes and `requires_owner_confirmation` only catch accidental
damage: whoever rewrites the text can rewrite them too. The owner's approval is a
record the host keeps outside the file (mind-config `persona_contract`):
`{version, core_sha256, voice_sha256, maintenance_sha256}`, all four. A record
that leaves any of them out approves nothing, not even the text it does name,
because the part it leaves out could change unseen; readers refuse it as
`approval-record-incomplete`, the code the deployment rehearsal reports.
`readPersonaContract(file, approved)` checks the record, then the file's fields,
markers and hashes, and refuses a file that differs from the record;
synchronization requires the record and projects nothing without it. The Python
consumers load the same scope-bound contract before model requests and check the
file's fields and hashes. The one Python path that puts a canon in place is a
restore from backup — the library call, `eventmem restore --persona-approval` and
the service's restore endpoint — and it holds the backup's canon against the
host's record first (`approve_canon`): with no record, an incomplete one or a
canon that differs from it, nothing is restored and the canon is left for host
review. Their structured output schemas and evaluator roles remain intact.
Quotations remain verbatim, and historical assistant wording is evidence rather
than a template for the current voice. Metadata exposes the loaded version and
hashes.

Personality proposals and reversions cannot write trait keys outside the approved
list. State scores, wishes and the existing evidence requirements remain separate.
This does not turn initialized traits into observed emotions or let a generated
portrait change the role agreement.

In the host's instruction projection a new core takes effect only when the
approval record names its version and hashes. Memory cleanup, repair, model
switching and persona synchronization never write that record. Operational
settings remain editable through their existing authorized interfaces. The
private host's phone keeps running the last approved instructions until a runtime
candidate built from the newly approved persona is proved and activated, and a
resumed thread keeps the developer message it recorded until its context is
rebuilt; see [mobile runtime](mobile-runtime.md#evidence).

Neither the hashes nor the approval record is OS access control: an actor who
can rewrite both files still has filesystem access. Hosts enforce the approval
boundary in their configuration workflow and instructions.

`tests/business/persona-contract.test.mjs` uses only synthetic roles. It covers
the file's field, marker and hash checks and the rule that instructions begin
with the approved core ahead of SOUL. `persona-approval.test.mjs` and
`test_persona_approval.py` cover the record: an edit of any part is refused under
the record of the text before it, a record with only the core hash is refused
outright, and only a complete record issued for the new text lets it in, on the
host's reader and on each restore entry.
