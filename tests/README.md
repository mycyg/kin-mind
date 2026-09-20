# Business regressions

The public suite follows the product's normal paths. It uses temporary stores and local transports.

| Flow | Coverage |
| --- | --- |
| Chat | Plain replies, text repair, quiet choices, visible failures |
| Model switch | Manual profiles, automatic return, notification receipts |
| Memory | Correction, scope, recall, evidence and event continuity |
| Autonomous action | Plans, owner participation, exploration, creation and learning |
| Delivery | Accepted/unknown results, resumable handoffs, SDK transactions |
| Startup and recovery | Shared-session recovery, historical state formats, client startup |

Run `uv run pytest -q tests/business` and `node --test tests/business/*.test.mjs` after a relevant change. Client checks live in `tests/clients` and run through each client's package script. CI runs affected components once; the second Python version is a release check.

Extended fault injection and historical replay are optional local acceptance work. Earlier public cases remain available in Git history. Real conversations, credentials and frozen private replay inputs are never stored here.
