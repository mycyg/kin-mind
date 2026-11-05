"""Dispatch the existing history operator commands; each owns its preconditions."""


def dispatch(mind, action, request, config=None):
    from .history_status import status, verify
    from .history_compaction import compact, compact_verify, restore

    handlers = {
        'history-status': (status, False),
        'history-verify': (verify, False),
        'history-compact': (compact, True),
        'history-compact-verify': (compact_verify, False),
        'history-restore': (restore, True),
    }
    if action not in handlers:
        raise ValueError('Unknown history operation')
    handler, needs_config = handlers[action]
    args = (mind, config) if needs_config else (mind,)
    return handler(*args, **(request or {}))
