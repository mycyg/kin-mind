"""What rested on a trait, and what has to be looked at again once that trait moves.

A trait is not a fact about the world. It is what a shared history formed, and one owner sentence
can revise or end it. So anything the host committed *because* of a trait has to say which trait
and at which revision, or a correction would leave that thing standing on nothing while every
projection still reads it as current.

`mind_trait_dependents` is that record, and this module is its writer and its readers:

- a **wish** the appraisal's own move said it was pursuing on a trait's account, which also keeps
  the map itself as `trait_revisions` so `_desire_ready` needs no second table to read;
- a **decision** about a plan step, which asks for a plan review once and only once;
- the **intent** for the next few replies, and the **hypothesis** a behavior check rests on, each
  of which already re-reads the ledger for itself and is recorded here so one place answers
  "what did this trait hold up".

Nothing here judges. A dependent is `needs_review` when the ledger no longer carries the same meaning
as the effective revision it was written against — that is all the host knows and all it claims. What to do about it belongs
to the reader: a wish stops being ready, a step asks for a review, an intent falls back to the
lookup, and the next appraisal is shown the trait as it now is.
"""

from __future__ import annotations

import json

from eventmem.core.db import Missing


# What may rest on a trait. A kind is the reader, not the writer: each one knows what to do when
# the material under it moved, and none of them learns it from here.
KINDS = ("desire", "decision", "intent", "hypothesis")
VALID, NEEDS_REVIEW = "valid", "needs_review"


def installed(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_trait_dependents'").fetchone())


# --- writing the line ---------------------------------------------------------------------------

def record(conn, scope, kind, dependent_id, trait_revisions, *, dependent_revision=None, at):
    """One row per trait this dependent was committed on, at the revision it had.

    A dependent is written whole: the rows of an earlier commit of the same thing are replaced, so
    a decision that no longer rests on a trait is not held by the one before it."""
    if kind not in KINDS:
        raise RuntimeError("Unknown trait dependent kind")
    if not installed(conn):
        # No trait was ever written in this store, so nothing can be resting on one. A revision here
        # could only have come from the ledger, and the ledger creates this table with itself.
        return
    conn.execute("DELETE FROM mind_trait_dependents WHERE scope=? AND kind=? AND dependent_id=?",
                 (scope, kind, dependent_id))
    for identifier, revision in sorted(trait_revisions.items()):
        conn.execute("INSERT OR REPLACE INTO mind_trait_dependents VALUES(?,?,?,?,?,?,?,?)",
                     (scope, identifier, revision, kind, dependent_id, dependent_revision, VALID, at))


def clear_kind(conn, scope, kind):
    """Forget every dependent of one kind in this scope. For the kinds a scope holds one of at a
    time: the intent in force is one row, not one row per intent there has ever been."""
    if not installed(conn):
        return 0
    return conn.execute("DELETE FROM mind_trait_dependents WHERE scope=? AND kind=?", (scope, kind)).rowcount


def same_meaning_since(conn, trait, revision):
    """Whether `trait` is still EFFECTIVE and has kept the exact same text/meaning since `revision`."""
    from .traits import EFFECTIVE
    if trait.get("status") not in EFFECTIVE:
        return False
    if type(revision) is not int or revision < 1 or type(trait.get("revision")) is not int:
        return False
    if trait.get("revision") == revision:
        return True
    if trait["revision"] < revision:
        return False
    rows = conn.execute(
        "SELECT revision,data FROM mind_trait_history WHERE id=? AND revision>=? AND revision<=? ORDER BY revision",
        (trait["id"], revision, trait["revision"]),
    ).fetchall()
    if [row[0] for row in rows] != list(range(revision, trait["revision"] + 1)):
        return False
    target_text = (trait.get("text") or "").strip()
    for rev, raw in rows:
        snap = json.loads(raw)
        if snap.get("id") != trait["id"] or snap.get("revision") != rev:
            return False
        if snap.get("status") not in EFFECTIVE:
            return False
        if (snap.get("text") or "").strip() != target_text:
            return False
        if rev > revision and snap.get("action") == "revise":
            return False
    return True


def trait_moved(conn, scope, trait_id, revision, at, *, ended, reinforced=False):
    """The ledger wrote this trait. Everything that rested on an earlier revision is now stale
    unless this write merely reinforced/established the exact same trait text without revising it."""
    if not installed(conn):
        return 0
    if reinforced and not ended:
        conn.execute(
            "UPDATE mind_trait_dependents SET trait_revision=?,at=? WHERE scope=? AND trait_id=? AND state=? AND trait_revision=?",
            (revision, at, scope, trait_id, VALID, revision - 1),
        )
        return 0
    if ended:
        return conn.execute(
            "UPDATE mind_trait_dependents SET state=?,at=? WHERE scope=? AND trait_id=? AND state=?",
            (NEEDS_REVIEW, at, scope, trait_id, VALID)).rowcount
    return conn.execute(
        "UPDATE mind_trait_dependents SET state=?,at=? WHERE scope=? AND trait_id=? AND trait_revision<>? AND state=?",
        (NEEDS_REVIEW, at, scope, trait_id, revision, VALID)).rowcount


def attach(commit, bound, grounds):
    """The move this appraisal stated named a wish or a step, and said a trait is why. Record it.

    The move itself orders nothing and readies nothing; this is the one thing about it that outlives
    its own row, because what rested on a trait has to be findable from the trait."""
    revisions = {ground["ref"]: ground["revision"] for ground in grounds if ground["kind"] == "trait"}
    if not revisions:
        return
    conn, mind = commit.conn, commit.mind
    at, scope = mind.clock(), mind.scope.key()
    wish = (bound.get("wish") or {}).get("id")
    if wish:
        desire = (commit.state.get("desires") or {}).get(wish)
        if desire is not None:
            # The wish keeps the map, the way it already keeps the concern revisions it was linked
            # under. It is not a new state: the revision it rests on is what "because of" means.
            desire["trait_revisions"] = {**(desire.get("trait_revisions") or {}), **revisions}
            record(conn, scope, "desire", wish, desire["trait_revisions"],
                   dependent_revision=desire.get("revision"), at=at)
    step = bound.get("step") or {}
    decision = _decision_id(conn, scope, step.get("plan_id"), step.get("id"))
    if decision:
        # Against the decision in force when the move was made, so a later decision — which cited
        # whatever it cited — is never held back by this one.
        record(conn, scope, "decision", step["plan_id"] + "/" + step["id"], revisions,
               dependent_revision=decision, at=at)


def _decision_id(conn, scope, plan_id, step_id):
    """The decision this step carries right now, read after the decisions of this same commit."""
    if not plan_id or not step_id:
        return None
    row = conn.execute("SELECT data FROM mind_plans WHERE scope=? AND id=?", (scope, plan_id)).fetchone()
    if not row:
        return None
    step = next((s for s in json.loads(row[0]).get("steps", []) if s["id"] == step_id), None)
    return ((step or {}).get("decision") or {}).get("id")


# --- reading it back ----------------------------------------------------------------------------

def links_fresh(conn, mind, desire):
    """Every trait this wish was committed on is still in force with the same meaning in the ledger."""
    refs = desire.get("trait_revisions") or {}
    if not refs:
        return True
    from .traits import Traits
    from .traits import installed as ledger_installed
    if not ledger_installed(conn):
        return False
    ledger = Traits(mind)
    for identifier, revision in refs.items():
        try:
            trait = ledger.get(conn, identifier)
        except Missing:
            return False
        if not same_meaning_since(conn, trait, revision):
            return False
    return True


def current_revisions(conn, mind, revisions):
    """The traits this map names as they stand now: their current revision where still in force,
    and without those that are gone or no longer effective (K1-19)."""
    from .traits import EFFECTIVE, Traits
    from .traits import installed as ledger_installed
    if not revisions or not ledger_installed(conn):
        return {}
    ledger, current = Traits(mind), {}
    for identifier in revisions:
        try:
            trait = ledger.get(conn, identifier)
        except Missing:
            continue
        if trait["status"] in EFFECTIVE:
            current[identifier] = trait["revision"]
    return current


def decision_needs_review(conn, scope, plan_id, step, decision):
    """Whether the trait this step's current decision was taken on has moved since.

    One row read, keyed by the decision itself. A step whose decision has been taken again since is
    not this row's dependent, and a step that never rested on a trait has no row at all."""
    identifier = (decision or {}).get("id")
    if not identifier or not installed(conn):
        return False
    return bool(conn.execute(
        "SELECT 1 FROM mind_trait_dependents WHERE scope=? AND kind='decision' AND dependent_id=?"
        " AND dependent_revision=? AND state=? LIMIT 1",
        (scope, plan_id + "/" + step["id"], identifier, NEEDS_REVIEW)).fetchone())
