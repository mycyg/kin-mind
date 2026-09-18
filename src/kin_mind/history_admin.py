"""Where a `history-*` operator command registers itself, so that adding one is never another
branch in the host's dispatch.

Stage 5 lands the history work in several packages that each ship a command or two — status,
verification, compaction, restore — and they land at different times against the same file. A
dispatch that named every one of them would be one file each package has to edit, which is one
merge conflict each package has to resolve, in the module where a mistake is least visible. Here a
package appends its module to `PROVIDERS` and decorates its handler, and the host keeps the single
line it already has.

Nothing here decides whether a command may run. That belongs to the command: these are operator
actions, and what they have to prove first — a quiescent host, a writable archive, a lease that is
still fresh — is the command's own subject and not a registry's.
"""

from __future__ import annotations

from importlib import import_module

# Modules that register a command when they are imported. A package appends its own; the import is
# deferred to the call so that a host which never runs a history command never pays for them.
PROVIDERS: tuple[str, ...] = ()

COMMANDS: dict[str, object] = {}


def command(name):
    """Register a handler as the `history-*` action of that name."""
    if not name.startswith("history-"):
        raise RuntimeError("A history command's name starts with history-")

    def register(handler):
        COMMANDS[name] = handler
        return handler

    return register


def dispatch(mind, action, request):
    for module in PROVIDERS:
        import_module(module)
    handler = COMMANDS.get(action)
    if handler is None:
        raise ValueError("Unknown history operation")
    return handler(mind, **(request or {}))
