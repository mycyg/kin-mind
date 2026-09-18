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

Nothing here judges. A dependent is `needs_review` when the ledger no longer carries the revision
it was written against — that is all the host knows and all it claims. What to do about it belongs
to the reader: a wish stops being ready, a step asks for a review, an intent falls back to the
lookup, and the next appraisal is shown the trait as it now is.
"""

from __future__ import annotations

import json

from eventmem.core.db import Missing, digest

from .state import timestamp

# What may rest on a trait. A kind is the reader, not the writer: each one knows what to do when
# the material under it moved, and none of them learns it from here.
KINDS = ("desire", "decision", "intent", "hypothesis")
VALID, NEEDS_REVIEW = "valid", "needs_review"
# What a wish says while it waits because the trait under it moved. Static host text: the reason a
# reader is given, in the existing `waiting` state rather than a state of its own.
WAITING_REASON = "The trait this wish rests on has moved; it waits until an appraisal judges it again"
# Wishes this settles. A wish already waiting, running or finished is not one that looks ready.
SETTLES = ("wanted",)


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


def trait_moved(conn, scope, trait_id, revision, at, *, ended):
    """The ledger wrote this trait. Whatever rested on a revision it no longer has needs review.

    Called from the one place that writes a trait, so no path can move a trait without the things
    that rested on it hearing about it."""
    if not installed(conn):
        return 0
    return conn.execute(
        "UPDATE mind_trait_dependents SET state=?,at=? WHERE scope=? AND trait_id=? AND state=? AND (? OR trait_revision<>?)",
        (NEEDS_REVIEW, at, scope, trait_id, VALID, 1 if ended else 0, revision)).rowcount


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
    """Every trait this wish was committed on, still in force at the revision it was committed
    against. Read from the ledger itself: the dependents table says what to look at, never what
    the answer is."""
    refs = desire.get("trait_revisions") or {}
    if not refs:
        return True
    from .traits import EFFECTIVE, Traits
    from .traits import installed as ledger_installed
    if not ledger_installed(conn):
        return False
    ledger = Traits(mind)
    for identifier, revision in refs.items():
        try:
            trait = ledger.get(conn, identifier)
        except Missing:
            return False
        if trait["revision"] != revision or trait["status"] not in EFFECTIVE:
            return False
    return True


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


def pending(conn, scope, *, kind=None, limit=40):
    """What a moved trait left for review, for an operator who wants to see it. Identifiers only."""
    if not installed(conn):
        return []
    where = "scope=? AND state=?" + (" AND kind=?" if kind else "")
    args = (scope, NEEDS_REVIEW) + ((kind,) if kind else ())
    return [{"trait_id": row["trait_id"], "trait_revision": row["trait_revision"], "kind": row["kind"],
             "dependent_id": row["dependent_id"], "at": row["at"]} for row in conn.execute(
        "SELECT * FROM mind_trait_dependents WHERE " + where + " ORDER BY at DESC,trait_id,dependent_id LIMIT ?",
        (*args, max(1, min(200, limit)))).fetchall()]


# --- the host's own route -----------------------------------------------------------------------

def settle_wishes(mind, *, apply=False):
    """Move the wishes a moved trait left not ready into the existing `waiting` state.

    They must not silently stay ready, and they must not silently disappear either. `_desire_ready`
    already refuses them, but only the new code knows why: to an older release such a wish looks
    ready, so this is what runs before a rollback. Idempotent, and a dry run writes nothing.
    """
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        found = sorted(desire["id"] for desire in state["desires"].values()
                       if desire.get("trait_revisions") and desire["status"] in SETTLES
                       and not links_fresh(conn, mind, desire))
    if not found or not apply:
        return {"state": "dry-run" if found else "settled", "settled": [], "scope": scope}

    def wait(conn, current, event_id):
        at, moved = mind.clock(), []
        for identifier in found:
            desire = current["desires"].get(identifier)
            if not desire or desire["status"] not in SETTLES or links_fresh(conn, mind, desire):
                continue
            desire.update(status="waiting", revision=desire["revision"] + 1, updated_at=at,
                          event_id=event_id, reason=WAITING_REASON,
                          # The field a reader of a waiting wish already looks at, whatever its kind.
                          contact_wait={"condition": "new_evidence", "reason": WAITING_REASON, "since": at})
            moved.append(identifier)
        mind._retarget(conn, current, at)
        return {"settled": moved}
    payload = {"command_id": "trait-wish-review:" + digest(found)[:32],
               "agent_version": state["agent_version"], "expected_revision": state["revision"]}
    return {"state": "settled", "scope": scope, **mind._mutate(payload, "trait-wish-review", wait)}


def review_view(mind):
    """What the host action reports: the wishes a moved trait left not ready, and everything else
    the dependents table is holding open. Read only."""
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        wishes = [{"id": desire["id"], "status": desire["status"], "kind": desire["kind"],
                   "trait_revisions": desire["trait_revisions"],
                   "expired": timestamp(desire["expires_at"]) <= timestamp(mind.clock())}
                  for desire in state["desires"].values()
                  if desire.get("trait_revisions") and not links_fresh(conn, mind, desire)]
        return {"wishes": sorted(wishes, key=lambda w: w["id"]), "dependents": pending(conn, scope)}
