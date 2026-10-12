"""Compatibility for callers from before command receipts used stable ids alone.

No payload fingerprint is computed, stored or compared. Existing fingerprint side tables
are legacy data and do not decide whether a command may run. Revision preconditions are
checked by each business operation at its first effect, inside its write transaction.
"""


def stamp(family, scope, payload, *, enabled=True, supersedes=None):
    """Legacy call shape; content has no role in command identity."""
    return None


def stored(conn, stamp, key):
    """Legacy fingerprints are never used as receipt or validation evidence."""
    return None


def unchanged(conn, stamp, key, *, legacy=None):
    """A caller that already found its stable command id returns its stored receipt."""
    return True


def record(conn, stamp, key, at):
    """The operation's own command table holds its receipt; no second table is written."""
    return None


def revision_id(key, stamp):
    """Compatibility only: a new effect needs its own caller supplied command id."""
    return key
