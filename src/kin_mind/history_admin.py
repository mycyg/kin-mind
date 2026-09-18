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
PROVIDERS: tuple[str, ...] = ("kin_mind.history_status", "kin_mind.history_compaction")

COMMANDS: dict[str, object] = {}
# The commands that are handed the host's own configuration as well as the mind. Only the ones that
# have to look outside the database for their answer: where the host keeps its pid files and its
# status file is not something the store knows, and a command that proves nothing is running cannot
# be told to trust the request for it.
NEEDS_CONFIG: set[str] = set()


def command(name, *, config=False):
    """Register a handler as the `history-*` action of that name."""
    if not name.startswith("history-"):
        raise RuntimeError("A history command's name starts with history-")

    def register(handler):
        COMMANDS[name] = handler
        if config:
            NEEDS_CONFIG.add(name)
        return handler

    return register


def dispatch(mind, action, request, config=None):
    for module in PROVIDERS:
        import_module(module)
    handler = COMMANDS.get(action)
    if handler is None:
        raise ValueError("Unknown history operation")
    if action in NEEDS_CONFIG:
        # Positionally, so a request can never supply it: what the host is running is the host's own
        # account of itself, and a caller that could pass its own would be back to asserting that
        # the workers are stopped.
        return handler(mind, config, **(request or {}))
    return handler(mind, **(request or {}))
