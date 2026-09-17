"""Command identity, effective-payload fingerprint and commit precondition, kept apart.

Three different questions used to share a single digest of the whole request:

* **which command is this?** — the caller's command id, which keys the durable receipt;
* **what would it write?** — the *effective payload*: every field that is written or takes
  part in validation;
* **may it take effect right now?** — the *precondition*: the compare-and-swap revisions,
  checked at the first effect and again whenever a revision takes effect.

Mixing them makes a retry that only refreshed its `expected_revision` look like a different
command, and would let a genuinely rewritten proposal pass as the same one. A fingerprint
answers only the second question.

Canonicalization is deliberately minimal: stable JSON (`dumps` already sorts object keys)
plus the collections the host itself reduces to a sorted set before writing them. Quotes,
code, paths and original text stay byte for byte, because the host verifies a quote by
literal substring match against the source: `x²` is not `x2`, and `a  b` is not `a b`.

The fingerprint is versioned and lives in its own side table, never in a column of an
existing one. A host that is rolled back ignores the table and compares exactly the legacy
digests it compares today; a row written before this table existed has no side row, so it
keeps being compared by its legacy digest as well.
"""

from __future__ import annotations

from typing import NamedTuple

from .db import digest, dumps

# Bumped only when the same payload would produce a different fingerprint. Rows written
# under an older version are compared by their legacy digest instead of being declared
# changed, so no committed command loses its identity (the lesson of commit ed14e98).
FINGERPRINT_VERSION = "payload-v1"

TABLE = "mind_command_fingerprints"
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS mind_command_fingerprints("
    "family TEXT NOT NULL,scope TEXT NOT NULL,id TEXT NOT NULL,fingerprint TEXT NOT NULL,"
    "fingerprint_version TEXT NOT NULL,precondition TEXT NOT NULL,supersedes TEXT,"
    "at TEXT NOT NULL,PRIMARY KEY(family,scope,id))"
)


class Family(NamedTuple):
    """One command family: what its payload writes, what only gates it, what names it."""

    #: "module:Model" of every pydantic model this family's payload is validated by. A test
    #: compares these fields against `fields`, so a new model field fails the build until it
    #: is classified. A family whose payload is a plain dict declares no model.
    models: tuple
    #: The effective payload. Declared for the test; `effective()` does not rely on it, so a
    #: field nobody declared is still fingerprinted rather than silently ignored.
    fields: tuple
    #: Compare-and-swap expectations. Never identity: they are checked at every effect.
    precondition: tuple
    #: Already part of the command id, so repeating them inside the fingerprint says nothing.
    identity: tuple
    #: Collections the host itself reduces to a sorted, deduplicated set before it writes
    #: anything derived from them (every one of these reaches `Mind._evidence`, which does
    #: `sorted(set(ids))`). Order in any other list is written down, so it is significant.
    unordered: frozenset


_EVIDENCE = frozenset({"evidence_ids"})

FINGERPRINT_FIELDS = {
    # state.Mind._mutate: affect, evolution, desires and the explicit policy updates. The
    # policy updates hand it a plain dict, so their own keys are declared here as well.
    "mind-state": Family(
        models=("kin_mind.state:AffectiveEvent", "kin_mind.state:DesireChange"),
        fields=("action", "agent_version", "completion", "concern_ids", "content", "desire_id",
                "evidence_ids", "evolution", "exploration_id", "exploration_target", "expires_at",
                "kind", "motivations", "origin", "quiet_start_hour", "reason",
                "retry_after_seconds", "rhythm", "strength", "style", "topic", "understanding",
                "values", "wait_condition", "wait_for_reply"),
        precondition=("expected_revision",), identity=("command_id",), unordered=_EVIDENCE),
    # lifecycle.EventLifecycle.apply_routes. title, quote and reason are part of what this
    # command writes and of what `_binding` verifies, so they belong to its identity of content.
    "event-route": Family(
        models=("kin_mind.lifecycle:EventRoute",),
        fields=("action", "binding", "evidence_ids", "event_id", "identity", "key", "member_ids",
                "quote", "reason", "thread_id", "title"),
        precondition=("expected_revision", "expected_thread_revision"), identity=(),
        unordered=_EVIDENCE),
    "habit": Family(
        models=("kin_mind.habits:HabitProposal",),
        fields=("evidence_ids", "preferences", "reason"),
        precondition=("expected_revision",), identity=(), unordered=_EVIDENCE),
    # plans.manage validates the change without the command id, so nothing is left to exclude.
    "plan-change": Family(
        models=("kin_mind.autonomy_models:PlanChange",),
        fields=("action", "desire_id", "evidence_ids", "goal", "id", "key", "motivation",
                "next_review_at", "reason", "steps"),
        precondition=("expected_revision",), identity=(), unordered=_EVIDENCE),
    "procedure": Family(
        models=("kin_mind.autonomy_models:ProcedureCandidate",),
        fields=("applicable_when", "counterexamples", "environment", "evidence_ids", "id", "key",
                "reason", "result_ids", "steps", "success_criteria", "title", "tools"),
        precondition=("expected_revision",), identity=(), unordered=_EVIDENCE),
    # graph.EventGraph.revise takes a plain request dict, so its fields are declared here and
    # checked against the keys the function actually reads.
    "graph-revision": Family(
        models=(),
        fields=("action", "changes", "evidence_ids", "id", "member_ids", "previous_command_id",
                "reason", "target_id", "title"),
        precondition=("expected_revision", "target_revision"), identity=("command_id",),
        unordered=_EVIDENCE),
}


def canonical(value, unordered=False):
    """Stable JSON shape only. Business strings are never folded.

    `dumps` already sorts object keys, so the only thing left to normalize is a collection
    the host treats as a set. No NFKC, no case folding, no whitespace collapsing: the same
    bytes the host validates and writes are the bytes that are fingerprinted.
    """
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        members = [canonical(item) for item in value]
        if not unordered:
            return members
        unique = {dumps(member): member for member in members}
        return [unique[key] for key in sorted(unique)]
    return value


def effective(family, payload):
    """The part of a payload that is written or validated: everything but the precondition
    and the fields the command id already carries. Taking the complement, rather than the
    declared list, means an undeclared field is included instead of being forgotten."""
    spec = FINGERPRINT_FIELDS[family]
    skipped = set(spec.precondition) | set(spec.identity)
    return {key: canonical(value, key in spec.unordered)
            for key, value in payload.items() if key not in skipped}


def preconditions(family, payload):
    """The compare-and-swap expectations, stored beside the fingerprint but never in it."""
    spec = FINGERPRINT_FIELDS[family]
    return dumps({key: payload.get(key) for key in spec.precondition})


class Stamp(NamedTuple):
    """One command's three parts, ready to compare and to store."""

    family: str
    scope: str
    fingerprint: str
    version: str
    precondition: str
    supersedes: str | None = None


def stamp(family, scope, payload, *, enabled=True, supersedes=None):
    """Fingerprint a payload, or return None when the host switch is off.

    `enabled` is the caller's `idempotency_fingerprint` setting. This module sits below
    kin_mind, so the switch is read by the caller that owns a scope and handed down; None
    then restores the legacy digest comparison everywhere, with nothing else to turn off.
    """
    if not enabled:
        return None
    body = effective(family, payload)
    return Stamp(family, scope, digest([FINGERPRINT_VERSION, family, body]),
                 FINGERPRINT_VERSION, preconditions(family, payload), supersedes)


def stored(conn, stamp, key):
    """The side row of a command, or None when it predates this table."""
    if stamp is None or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    return conn.execute(
        "SELECT * FROM mind_command_fingerprints WHERE family=? AND scope=? AND id=?",
        (stamp.family, stamp.scope, key)).fetchone()


def unchanged(conn, stamp, key, *, legacy=None):
    """Whether a stored command carries the same effective payload as this one.

    A side row decides by fingerprint, which is why a refreshed `expected_revision` returns
    the original receipt. Without one the command predates this table and the legacy digests
    decide, exactly as they do today; a family that never stored a digest (`legacy` is None)
    keeps its previous behavior of trusting the command id alone.
    """
    row = stored(conn, stamp, key)
    if row is not None and row["fingerprint_version"] == stamp.version:
        return row["fingerprint"] == stamp.fingerprint
    return legacy is None or legacy[0] == legacy[1]


def record(conn, stamp, key, at):
    """Store the fingerprint and precondition of a command that just took effect."""
    if stamp is None:
        return
    conn.execute(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO mind_command_fingerprints VALUES(?,?,?,?,?,?,?,?)",
        (stamp.family, stamp.scope, key, stamp.fingerprint, stamp.version,
         stamp.precondition, stamp.supersedes, at))


def revision_id(key, stamp):
    """The command id of an explicit revision of `key`.

    A revision is a command of its own, named after the exact content it puts in effect, so
    re-applying that same content finds the revision instead of creating another one. The
    original command, its receipt and its undo record are never rewritten.
    """
    return "rev_" + digest([key, stamp.fingerprint])[:32]
