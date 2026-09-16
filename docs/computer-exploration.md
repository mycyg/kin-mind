# Computer exploration, sharing choices and owner help

DeepSeek Flash/high chooses a question and its purpose in the existing appraisal.
`exploration_target` is `knowledge` (the backward-compatible default) or
`computer`. Kimi executes the question for at most 1,200 seconds. A computer
question can concern the owner's authorized work or everyday activity. A high
curiosity score alone does not authorize a scan: an actionable, sourced wish is
required. There is no continuous screen recorder or new polling schedule.

## On-demand observations

The computer profile exposes three filtered, read-only MCP tools:

| Tool | Result |
|---|---|
| `read_computer_context` | Current app, visible window titles, available accessibility text and observation time |
| `list_computer_files` | One authorized directory, bounded name filtering and modification times |
| `read_computer_resource` | Bounded text from an authorized file, Office document or PDF, with locator and content version |

The macOS adapter in `adapters/computer-context.swift` uses NSWorkspace,
CGWindowList and accessibility reads. It never moves a pointer, types, requests
system permissions or records continuously. An unavailable accessibility read
is reported as such; file and page availability are separate facts. Office text
is read locally; PDF text requires the optional `pdftotext` executable. Kimi's
web tools remain available for public sources and do not inherit browser login.

All computer file reads pass through `ComputerReader`: configured roots and
excluded runtime directories are checked after resolving symlinks. Credential
files are excluded and recognized credential values and URL secrets are redacted
before a tool result or observation is saved. This is a practical filter, not a
guarantee that arbitrary documents contain no sensitive information. Configure
roots and exclusions for the actual owner's authorization.

Each observation records locator, content hash, observation time, available source
time, a short excerpt and `actor=unknown`. File modification does not establish
that the owner performed an action. The evaluator distinguishes observations,
inferences and internal ideas. Source correction or deletion invalidates the
associated decision and blocks its old contact intent pending reassessment.

The optional `seen_database` preserves resource identities across workers and
context-window trimming. Kimi receives a small window of previously read
resources and follows the current question from recent clues. An unchanged
window is not a new owner interaction. Actual snapshots, paths and findings stay
in the private store, not the public source repository.

## Host configuration

These keys extend an existing private host configuration:

```json
{
  "exploration_decisions_enabled": true,
  "computer_exploration": {
    "enabled": true,
    "roots": ["/authorized/documents"],
    "exclude_roots": ["/authorized/documents/private-runtime"],
    "snapshot_command": ["/private/bin/computer-context"],
    "seen_database": "/private/state/computer-seen.sqlite",
    "kimi_home": "/private/existing-kimi-login"
  }
}
```

Compile the native adapter with `swiftc adapters/computer-context.swift -o
/private/bin/computer-context`. `kimi_home` is optional and otherwise follows
`KIMI_CODE_HOME` or `~/.kimi-code`. Computer exploration creates a private Kimi
home for that invocation, copies its model configuration and references the
existing OAuth credential store. Only the three computer MCP tools are granted
there. The custom agent profile exposes those tools and public web tools; it
has no raw filesystem, shell, input-control or subagent tools. The ordinary
knowledge profile is separate. Global Kimi configuration, workspace trust and
desktop Codex settings are unchanged.

This uses Kimi's documented [MCP configuration](https://moonshotai.github.io/kimi-code/en/customization/mcp),
[custom agent tool allowlists](https://moonshotai.github.io/kimi-code/en/customization/agents)
and [isolated configuration home](https://moonshotai.github.io/kimi-code/en/configuration/config-files.html).
In print mode, use `-p` with the configured rules; `--auto` cannot be combined
with `-p`. A project-only MCP file may be omitted in an untrusted print session.
Validate actual tool execution, not just a successful CLI exit.

## A result need not become a message

Kimi returns only a validated final object: findings, sources, open questions,
optional `suggested_share` and optional `assistance_needed`. Thinking blocks and
tool transcripts are excluded from the appraisal result. Tool names and status
receipts may be retained for operational verification.

For each new exploration result, the same DeepSeek appraisal returns one entry
in `sharing`:

```json
{
  "exploration_id": "explore_synthetic",
  "decision": "defer",
  "reason": "This question may become relevant to the next draft.",
  "reconsider_when": "New information or owner feedback about that draft arrives."
}
```

`share` permits one contact intent for that decision revision. `defer` stores its
reconsideration condition; `keep` finishes the assessment with no new contact.
All three persist with evidence, model receipt, configuration version and audit
history in the same transaction as scores, concerns and wishes. Changing a
decision requires new sourced information. Clock crossings, receipt events and
replayed evidence do not create another revision or another contact.

A new contact wish links `exploration_id`. Before claiming and sending, the
host rechecks the decision revision, source freshness, initiative threshold 75,
work locks, owner activity, quiet hours and original delivery identity. A later
keep/defer decision invalidates an already drafted message. Successful delivery
does not automatically manufacture another topic. Partial or uncertain receipts
continue through the existing stable-ID reconciliation.

## Asking the owner to help

Concerns accept an optional `owner_request`:

```json
{
  "kind": "help",
  "action": "Send a clearer photograph of the label.",
  "reason": "The current photograph does not show its model number.",
  "completion": "A legible photograph or the model number arrives.",
  "status": "proposed"
}
```

`kind` is `help`, `invitation` or `request`. A `request` can come directly from
the agent's own wish, such as asking the owner to choose a photograph or try a
prototype; it requires no capability gap or failed task. Its states are `proposed`, `accepted`, `waiting`,
`completed` and `declined`. A contact wish points to this concern and speaks the
request. The concern continues waiting after the wish is sent. A real owner
response is required to record acceptance, completion or refusal. Acceptance
records the agreed participation; an unaccepted invitation remains a proposal.
An owner being busy can move the request to waiting. Outcome evidence can resolve
the concern and resume the related exploration; new wishes follow normal review.

When Kimi reports `assistance_needed`, finishing its CLI run leaves the original
exploration wish waiting for evidence. Supplying the condition can resume that
same wish with a new execution receipt; a finished CLI process does not falsely
mark the unanswered question complete.

This reuses concerns and wishes instead of creating a second task system. Work
assigned by the owner still asks promptly for required inputs through its work
flow; voluntary exploration and invitations use the proactive contact flow.

## Readback and verification

`read_affective_state` adds `exploration_decisions`; its concerns expose
`owner_request`. Host `read` also returns `exploration_capabilities`. Both channels
use the same committed state and bounded expression projection. Normal chat and
proactive messages use 1–4 natural bubbles, with usually short, complete sentences.

Synthetic tests cover share/defer/keep, restart and duplicate events, transactional
rollback, source correction, request follow-up, resource identity, credential
filtering, isolated tool profiles, timeout and work preemption. Enable the flags
only after a real authorized read has returned an observed resource through the
Kimi tool path. Disabling computer exploration keeps its records and prevents
new computer workers; existing knowledge wishes retain their meaning.
