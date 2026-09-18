"""Shared authenticity predicates: which material may stand for what.

One definition for every reader, built on the read policy rather than beside it.

- The **owner's own statement** is a record the policy calls plain experience, written by the
  owner and confirmed as said. A turn labelled a configuration request is still an experience
  and is still recalled; it says what the owner asked for, not what Kin has grown into, so it
  never supports a claim about Kin. Setup and persona texts carry `explicit` authority too,
  which is why authority alone was never the test.
- **Verified behaviour** is an execution receipt the host resolved. Who wrote the text decides
  nothing: an exploration result is model-authored, so its text is only a self-statement, while
  the receipt of that same exploration is behaviour.
- **Kin's own statement** is where model-authored material ends. It is admitted in no other class.
- An **internal event** is evidence of nothing, in any class.
- An **episode** is the host's unit of "a separate time this happened": the execution it belongs
  to, else the interaction window it falls in, else its root. The window is the one `rhythm`
  already draws; there is no second definition of how far apart two messages must be. Callers
  may merge episodes and may never split one.

Pure functions over what the caller already holds: nothing here reads or writes the database.
"""

from __future__ import annotations

from eventmem.core.db import digest
from eventmem.core.read_policy import NON_EXPERIENCE, REQUEST_LABEL, host_envelope

from .rhythm import stamp

# Sources the host writes back for its own events, with model authority.
INTERNAL_NAMESPACE = "mind-internal-event"
# What the host can resolve into proof that something was actually done, and the field of each
# that says it finished. An artifact on its own is not here: its task result is what verifies it.
EXECUTION_RECEIPTS = ("plan-run", "task-result", "delivery", "exploration")


def never_evidence(source):
    """An internal event: Kin's own bookkeeping, written back as a source. Never evidence."""
    if not isinstance(source, dict):
        return True
    namespace = source.get("namespace") or ""
    return namespace == INTERNAL_NAMESPACE or namespace.startswith(INTERNAL_NAMESPACE + ":")


def owner_statement(record, policy=None, *, sources=()):
    """Did the owner say this, in person? `sources` are the cited source rows when the caller has
    them. Without a policy (the classification is switched off) the local checks still hold."""
    if not isinstance(record, dict) or any(never_evidence(s) for s in sources):
        return False
    attributes = record.get("attributes") or {}
    if attributes.get("self_knowledge") or host_envelope(record.get("content") or ""):
        # A self-claim and a host block stored as an owner turn are not the owner speaking.
        return False
    if policy is not None:
        found = policy.classify(record)
        # Plain experience only: a configuration request is an experience with a label.
        if found.kind != "experience" or found.label is not None:
            return False
    elif attributes.get("origin_kind") in NON_EXPERIENCE | {REQUEST_LABEL}:
        # The stamp the engine wrote when the source arrived, which is all there is to read here.
        return False
    return (attributes.get("role") == "user" and not record.get("generated")
            and record.get("confirmation") == "explicit")


def self_statement(record, *, sources=()):
    """Kin's own words: model-authored material, and the only class it may ever be."""
    if not isinstance(record, dict) or any(never_evidence(s) for s in sources):
        return False
    if (record.get("attributes") or {}).get("role") == "user":
        return False
    return bool(record.get("generated")) or any((s or {}).get("authority") == "model" for s in sources)


def verified_behavior(receipt):
    """Something Kin actually did, as the host resolved it. The caller names what it resolved;
    a source, a record or a bare artifact is not an execution and never passes here."""
    if not isinstance(receipt, dict) or receipt.get("kind") not in EXECUTION_RECEIPTS:
        return False
    kind = receipt["kind"]
    if kind == "plan-run":
        return receipt.get("state") == "completed" and bool((receipt.get("result") or {}).get("verified"))
    if kind == "task-result":
        return receipt.get("verified") is True
    if kind == "delivery":
        return receipt.get("state") == "accepted" and bool(receipt.get("message_id"))
    return receipt.get("state") == "complete"


def window_of(windows, at):
    """The interaction window an utterance falls in, from `rhythm.interaction_windows`. Its
    join of 30 minutes is reused as it stands; this is not a second threshold."""
    moment = stamp(at)
    return next((w for w in windows if stamp(w["start"]) <= moment <= stamp(w["end"])), None)


def root_key(*, text=None, window=None, source_id=None):
    """What two ingestions of one utterance share. The same words inside the same window are one
    origin however many namespaces carried them; without a window the source is its own root."""
    if text is not None and window:
        return "root_" + digest(["utterance", " ".join(text.split()).casefold(), window["start"]])[:32]
    if source_id:
        return "root_" + digest(["source", str(source_id)])[:32]
    raise ValueError("A root needs an utterance inside a window or a source")


def episode_key(*, execution_id=None, window=None, root=None):
    """A separate time something happened, in the host's terms, in order of preference."""
    if execution_id:
        return "ep_" + digest(["execution", str(execution_id)])[:32]
    if window:
        # The start, so that a window growing as the conversation continues stays one episode.
        return "ep_" + digest(["window", window["start"]])[:32]
    if root:
        return "ep_" + digest(["root", str(root)])[:32]
    raise ValueError("An episode needs an execution, a window or a root")
