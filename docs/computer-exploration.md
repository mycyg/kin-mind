# Computer exploration, sharing choices and owner help

DeepSeek Flash/high chooses a question and its purpose in the existing appraisal.
`exploration_target` is `knowledge` (the backward-compatible default) or
`computer`. The exploration executor runs the question for at most 1,200
seconds — currently codex-cli with model provider DeepSeek (deepseek-flash,
reasoning high) behind a dedicated local gateway; an executor that cannot start
pauses the question with a recorded waiting reason instead of falling back to
anything. Receipts carry executor and model provider separately. A computer
question can concern the owner's authorized work or everyday activity. A high
curiosity score alone does not authorize a scan: an actionable, sourced wish is
required. There is no continuous screen recorder or new polling schedule.

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
| Browser | Open a new run-owned tab, read fresh AX/DOM text, navigate to a validated public URL, and—when interaction is enabled—independently review every click or bounded non-sensitive text entry, then close that tab |
| Native app | Observe an exact allowlisted bundle id; independently review every click/scroll against fresh AX state, with a separate matching native Computer Use approval |

DeepSeek chooses the question, whether exploration is useful, and the semantic
action/target. The host does not replace those choices with keywords. Host policy
only enforces capability, app/tab identity, URL/network boundary, current AX
identity and the owner's existing scope. Every interaction goes to a distinct
fixed-profile DeepSeek/high action reviewer. An optional exact control grant may
bind target, operation, AX label and effect as a reviewer hint, but never bypasses
that review.
That reviewer receives the untrusted current snapshot, proposed operation, host
permissions, snapshot hash and input version; it classifies the actual likely
effect, not the execution model's label. The host persists the review receipt,
checks its category against existing permissions, re-reads the target, and acts
only if the full raw snapshot hash, exact element line, element label and locator
are unchanged. Review text is bounded, but always includes the complete selected
element line. An executing model therefore
cannot relabel “Send” as a local edit. The host does not infer intent from button
words such as “save” or “confirm.” Codex, terminal, system-settings
and browser apps are hard-denied only because native control of them would bypass
this adapter; document and communication apps remain configurable, with
observe-only as the default. Arbitrary JavaScript, coordinates, key presses and
file upload are not exposed. Browser interaction is off unless explicitly
enabled; `host_allowlist` is an optional narrowing rule, while all URLs still
pass scheme, credential, sensitive-query and SSRF checks. The reviewer may allow
only `read`, `navigation`, `local_reversible` and separately scoped
`local_write`. `external_send`, `purchase`, `destructive`, `credential`,
`control_plane` and `unknown` are hard-denied by the host and cannot be enabled
by model output. Native Computer Use approval is
accepted only for the same low-risk operation, exact bundle id and action already
present in the host allowlist; its receipt is attached to the observation.

This route supplies accessibility/DOM text to deepseek-flash. It does not send
screenshots to the model and therefore does not claim pixel-vision support.

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
  "computer_action_review_gateway_state_file": "/private/state/computer-action-review-gateway.json",
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
      "allowed_browser_effects": [],
      "browser_element_grants": [],
      "allowed_apps": [
        "com.apple.finder",
        "com.apple.Preview",
        "com.apple.calculator"
      ],
      "allowed_app_actions": ["observe"],
      "allowed_native_effects": [],
      "native_element_grants": [],
      "action_review": {
        "enabled": true,
        "env_key": "KIN_COMPUTER_ACTION_REVIEW_TOKEN",
        "model": "deepseek-flash",
        "reasoning": "high",
        "timeout_seconds": 60,
        "allowed_categories": ["navigation", "local_reversible"],
        "local_write_hosts": [],
        "local_write_apps": []
      },
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
apps the owner authorized. An optional stable-control hint may include an exact
`native_element_grants` entry such as
`{"action":"click","app_id":"com.example.LocalEditor","expected_text":"Save","effect":"local_edit"}`.
Browser click/text grants use `host` instead of `app_id` and `action` values
`click` or `type`. A hint is included in the independent review context only when
the action/effect/target/exact AX label match; all interactions still require the
review and fresh-state fence. `local_write` additionally requires the category plus an exact host
or app in `local_write_hosts` / `local_write_apps`; the private host must already
have confined that target to the owner's authorized task/files. Review never
creates a new scope.

The private host starts a second fixed-profile gateway with
`startComputerActionReviewGateway({key, lease, onUsage})`. Its state file is
0600 and contains exactly `baseUrl`, `pid` and `startedAt`; the random token lives
only in `KIN_COMPUTER_ACTION_REVIEW_TOKEN`. Each dispatch re-reads the state,
requires a live publisher and loopback endpoint, and passes only the environment
variable name to Codex. A missing/stale reviewer disables all interactions and
reports the reason; it never self-authorizes or prevents the phone chat host
from starting. Browser/native observation and navigation remain separately
described capabilities.

The backend `command` and `args` must point to the installed Computer Use Node
runtime and `@oai/cua-repl` entry point; renaming another adapter is not valid.
Set `CUA_REPL_ENABLED_SURFACES` in the private host process to the surfaces
intentionally deployed. Per-run files contain only the name from `env_vars`,
never its value; inline backend `env` values are rejected. The file-reader config
also omits all UI/backend configuration and always denies its own execution
directory, even if an authorized root contains it. The
owning Codex/Computer Use process must already have macOS Accessibility access
for native apps, and the chosen browser must be available. This route never
grants OS permission itself. At startup the executor waits up to ten seconds for
`kin_ui` and approves only this host-owned MCP server. [DeepSeek's official
Responses documentation](https://api-docs.deepseek.com/guides/responses_api/)
accepts ordinary function tools but rejects a custom `exec`
tool, so this profile keeps code mode disabled and uses a bundled DeepSeek model
catalog. Codex's three host-owned MCP namespace wrappers are flattened at the
exploration gateway into ordinary function names and mapped back on the response;
other namespaces, user-input tools and custom tools are dropped there. A native
CUA elicitation is accepted only when its low-risk
tool, bundle id and action match the private configuration; that approval is
recorded independently of DeepSeek's effect declaration.

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

Isolated validation on 2026-09-19 covered a real new Chrome tab on a local probe
page (AX read, controlled click, changed AX read, close), and a temporary native
AppKit probe (observe, CUA approval, reversible click, changed
AX read, restore). A local Responses stub also drove real codex-cli function MCP
round trips, an independent synthetic action-review endpoint and actual CUA. The
direct transport probes preceded the final all-interactions-review hardening; the
post-hardening adversarial grant/label and full-snapshot fences are covered by
isolated integration tests. Two real DeepSeek end-to-end attempts were stopped
before any tool action by the provider's documented rejection of Codex's custom
`exec` tool. The executor was then changed to disable code mode, and the gateway
was changed to flatten the remaining host-owned MCP namespace wrappers into
ordinary function tools. The post-fix Codex/MCP path passed the isolated stub
round trip; the two-attempt cap meant the post-fix DeepSeek-to-CUA chain was not
claimed as verified. The
independent action-review profile/schema, denial gates and snapshot fences have
unit/integration coverage, but its real-provider call likewise was not reached
in those two attempts. Finder/Preview/Calculator interaction, arbitrary
production sites, file upload, coordinates, pixel vision and external effects
remain unverified or intentionally unavailable as stated above.

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
host rechecks the decision revision, source freshness, a current DS action decision when `semantic_actions` is enabled,
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
existing knowledge wishes retain their meaning.
