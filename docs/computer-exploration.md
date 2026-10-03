# Computer exploration, sharing choices and owner help

The assessment chooses a question and its purpose in the appraisal — the main
session's own model with `main_session_review`, otherwise DeepSeek Flash/high.
`exploration_target` is `knowledge` (the default) or `computer`. The exploration
executor runs the question for at most 1,200 seconds: the active runtime
bundle's Codex, labelled `codex-cli` in receipts, against DeepSeek
(deepseek-flash, reasoning high) through the dedicated exploration gateway. An
executor that cannot start pauses the question with a recorded waiting reason
instead of falling back to anything. Receipts carry executor and model provider
separately. A computer question can concern the owner's authorized work or
everyday activity. A high curiosity score alone does not authorize a scan: an
actionable, sourced wish is required. There is no continuous screen recorder or
new polling schedule.

## On-demand observations and controlled interaction

The computer profile retains three filtered, read-only MCP tools:

| Tool | Result |
|---|---|
| `read_computer_context` | Current app, visible window titles, available accessibility text and observation time |
| `list_computer_files` | One authorized directory, bounded name filtering and modification times |
| `read_computer_resource` | Bounded text from an authorized file, Office document or PDF, with locator and content version |

The macOS observation adapter in `adapters/computer-context.swift` uses NSWorkspace,
CGWindowList and accessibility reads. It never moves a pointer, types, requests
system permissions or records continuously. An unavailable accessibility read
is reported as such; file and page availability are separate facts. Office text
is read locally; PDF text requires the optional `pdftotext` executable. Public
web search/read is a separate `kin_web` MCP surface with its own receipts.

An optional `kin_ui` MCP surface uses the installed Codex Computer Use service,
not shell, AppleScript or a renamed file reader. It exposes fixed tools only:

| Area | Fixed tools and boundary |
|---|---|
| Browser | Open a new run-owned tab, read fresh AX/DOM text, navigate to a validated public URL, and—when interaction is enabled—click or enter bounded non-sensitive text under the host's rules, then close that tab |
| Native app | Observe an exact allowlisted bundle id; click or scroll against fresh AX state where `allowed_app_actions` permits it, with a matching native Computer Use approval |

The assessment chooses the question and whether exploration is useful; the
exploration model chooses the semantic action and target. The host does not
replace those choices with keywords, and no second model reviews an interaction.
The executor declares each interaction's category, and the host applies the
owner's hard rules. `external_send`, `purchase`, `destructive`, `credential` and
`control_plane` are refused whatever else is said about them. A target whose own
label names sending, payment, deletion or credentials is handed back with its
reason rather than guessed at, and so is an undeclared category; words such as
“save” or “confirm” decide nothing. `read`, `navigation` and `local_reversible`
need only a current target; `local_write` needs a target the host scoped for
writing. Before acting, the host reads the target fresh, and the selected element
must still be there with the expected label; each interaction leaves an action
receipt and an observation of what the target showed afterwards. Codex,
terminal, system-settings and browser apps are hard-denied as native targets
because native control of them would bypass this adapter; document and
communication apps are configurable, with observe-only as the default. Arbitrary
JavaScript, coordinates, key presses and file upload are not exposed. Browser
interaction is off unless explicitly enabled, and browser text entry is at most
500 characters with credential-shaped text refused. `host_allowlist` is an
optional narrowing rule, while all URLs pass scheme, credential, sensitive-query
and SSRF checks. Native Computer Use approval is accepted only for the same
low-risk request on an allowed bundle id and action; its receipt is attached to
the action's record.

This route supplies accessibility/DOM text to the exploration model
(deepseek-flash). It does not send screenshots to the model and therefore does
not claim pixel-vision support.

All computer file reads pass through `ComputerReader`: configured roots and
excluded runtime directories are checked after resolving symlinks. Credential
files are excluded and recognized credential values and URL secrets are redacted
before a tool result or observation is saved. This is a practical filter, not a
guarantee that arbitrary documents contain no sensitive information. Configure
roots and exclusions for the actual owner's authorization.

Each successful observation records its execution id, attempt, adapter and fixed
tool name plus locator, content hash, observation time, available source time, a
short excerpt and `actor=unknown`. File modification does not establish
that the owner performed an action. The evaluator distinguishes observations,
inferences and internal ideas. Source correction or deletion invalidates the
associated decision and blocks its old contact intent pending reassessment.

The optional `seen_database` preserves resource identities across workers and
context-window trimming. The executor receives a small window of previously read
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
    "ui": {
      "enabled": true,
      "browser": "iab",
      "host_allowlist": [],
      "allow_browser_click": true,
      "allow_browser_text": false,
      "allowed_apps": [
        "com.apple.finder",
        "com.apple.Preview",
        "com.apple.calculator"
      ],
      "allowed_app_actions": ["observe"],
      "local_write_hosts": [],
      "local_write_apps": [],
      "backend": {
        "command": "/path/to/cua_node/bin/node",
        "args": ["/path/to/@oai/cua-repl/bin/cua-repl.mjs"],
        "env_vars": ["CUA_REPL_ENABLED_SURFACES"]
      }
    }
  }
}
```

The three ordinary macOS bundle IDs above are an observation-only deployment
example, not a statement that every OS release exposes useful AX text. Add only
apps the owner authorized; `allowed_app_actions` adds `click` or `scroll` for
them. `local_write` additionally requires the category plus an exact host or app
in `local_write_hosts` / `local_write_apps`, read from `ui` or, where `ui` does
not set them, from `ui.action_review`; the private host must already have
confined that target to the owner's authorized task/files. No setting creates a
new scope.

The backend `command` and `args` must point to the installed Computer Use Node
runtime and `@oai/cua-repl` entry point; renaming another adapter is not valid.
Set `CUA_REPL_ENABLED_SURFACES` in the private host process to the surfaces
intentionally deployed. Per-run files contain only the name from `env_vars`,
never its value; inline backend `env` values are rejected. The file-reader config
also omits all UI/backend configuration and always denies its own execution
directory, even if an authorized root contains it. The
owning Codex/Computer Use process must already have macOS Accessibility access
for native apps, and the chosen browser must be available. This route never
grants OS permission itself. Before Codex starts, a readiness probe runs the
Computer Use bootstrap within `ui.readiness_timeout_seconds` (45 by default);
`kin_ui` runs the same bootstrap before it answers Codex's handshake, so Codex
waits for it just as long (kept within 10–120 seconds; the read-only servers get
ten). `kin_ui` is required, and Codex approves only this host-owned MCP server. [DeepSeek's official
Responses documentation](https://api-docs.deepseek.com/guides/responses_api/)
accepts ordinary function tools but rejects a custom `exec`
tool, so this profile keeps code mode disabled and uses a bundled DeepSeek model
catalog. Codex's three host-owned MCP namespace wrappers are flattened at the
exploration gateway into ordinary function names and mapped back on the response;
other namespaces, user-input tools and custom tools are dropped there. A native
CUA elicitation is accepted only when its low-risk
tool, bundle id and action match the private configuration; that approval is
recorded with the action it allowed.

Compile the native adapter with `swiftc adapters/computer-context.swift -o
/private/bin/computer-context`. The executor serves the same three tools from
the host's own `kin_computer` MCP server, injected into the run's configuration
alone: user configuration and rules are ignored, other MCP servers, hooks,
multi-agent, the generic shell and built-in web search are disabled, and the
sandbox is read-only. The only optional servers are host-owned `kin_web`,
`kin_computer` and `kin_ui`. The Computer Use backend command and environment
variable names come from private host configuration; inline values and
credential-shaped environment names are refused before a per-run config is
written. The ordinary knowledge profile may
use `kin_ui` for browser research while keeping local file reads disabled.

Validate actual tool execution, not just a successful CLI exit.

Only successful host-verified web/computer receipts enter the source ledger.
Completed exploration citations retain a sealed receipt bound to their
exploration id and attempt; a historical bare URL is ignored. Continuations must
carry that valid receipt for non-memory sources, while memory sources must still
match the current non-null revision. A failed tool call or model-written source
object cannot become evidence merely by matching a locator.

## A result need not become a message

The executor returns only a validated final object: findings, sources, open questions,
optional `suggested_share` and optional `assistance_needed`. Thinking blocks and
tool transcripts are excluded from the appraisal result. Tool names and status
receipts may be retained for operational verification.

For each new exploration result, the same appraisal returns one entry in
`sharing`:

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
host rechecks the decision revision, source freshness, a current verified
decision when `semantic_actions` is enabled, work locks, owner activity, quiet
hours and original delivery identity. A later
keep/defer decision invalidates an already drafted message. Successful delivery
does not automatically manufacture another topic. Partial or uncertain receipts
go through stable-ID reconciliation.

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

When the executor reports `assistance_needed`, finishing its run leaves the
original exploration wish waiting for evidence. Supplying the condition can
resume that same wish with a new execution receipt; a finished executor process
does not falsely mark the unanswered question complete.

This reuses concerns and wishes instead of creating a second task system. Work
assigned by the owner still asks promptly for required inputs through its work
flow; voluntary exploration and invitations use the proactive contact flow.

## Readback and verification

`read_affective_state` adds `exploration_decisions`; its concerns expose
`owner_request`. Host `read` also returns `exploration_capabilities`. Both channels
use the same committed state and bounded expression projection. Normal chat and
ordinary proactive messages prefer one short, complete bubble; genuine pauses,
emotional turns, deeper discussion, work, analysis and delivery may use more.
This is voice guidance, not a transport limit or lossy text splitter.

Synthetic tests cover share/defer/keep, restart and duplicate events, transactional
rollback, source correction, request follow-up, resource identity, credential
filtering, isolated tool profiles, timeout and work preemption. Enable the flags
only after a real authorized read has returned an observed resource through the
executor's tool path. The adapter acceptance probe uses a local HTTP page in a new
tab and a dedicated local AppKit window whose only button toggles `KIN_OFF` to
`KIN_ON` and back; it must never use a user's real app window for mutation tests.
Disabling computer exploration keeps its records and prevents new computer workers;
knowledge wishes keep their meaning.
