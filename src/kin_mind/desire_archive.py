"""Where a finished wish goes when the state document no longer has to carry it.

The state document holds every wish that was ever made. A hundred and twenty of them, over half
the document by bytes, and all but three already finished — and the whole document is written
again on every revision, held whole in memory, projected into every appraisal and carried into
every context the model is shown. Patches make each revision cheaper to store; they do not make
the document smaller. This does.

**A move, never a delete.** An archived wish keeps its identifier, its plan links, its evidence
references and its decision receipt, exactly as they were, in `mind_desire_archive`. Nothing is
summarised and nothing is dropped. A reader that misses in the live set looks here, so what the
archive holds still blocks a duplicate intent, still refuses an update with the same refusal the
live wish would have given, and still stops a plan synchronisation from making the wish again.
`desire-unarchive` puts them back, and because the state is stored with its keys sorted, a wish
that comes back lands where it was, byte for byte.

**Time is never a reason.** A long-running plan is not finished because it is old. Only a wish
that has already reached `completed` or `abandoned` can move at all; age is a floor under that
decision, never a cause of it. An expired `wanted` or `waiting` wish is not touched — expiry is
not a verdict, and the wish is still the model's to settle. Neither is a wish whose execution or
delivery is still open: a contact attempt still drafting, an unconfirmed send, a plan run still
running, an action event that has not settled, a running exploration, or a partial delivery. Each
of those is a reason the wish is held, and every reason a wish is held is reported by name.

**The window stays the window.** The appraisal projection shows the active wishes and a tail of
finished ones; that tail is kept live so the projection is the projection it was. What has moved
is counted in the state document, so `desire_window.total` still says how many wishes exist rather
than how many are left in the document, and the manifest's drift check reads the same number.

Off by default, behind `desire_archive`, and even on it moves nothing without an explicit command.
The dry run needs neither: it reads, reports what would move and why everything else stays, and
writes nothing at all, which is what makes it safe to run against a copy of a live store.

One known gap travels with the move, and it is the one already recorded against the state
document: an erase deletes the sources and the records and does not reach `mind_state` or the
history rows, both of which hold the wish text. `mind_desire_archive` is a third place with the
same property, for the same wishes, and whoever closes that gap has to name this table beside
those two.
"""

from __future__ import annotations

import json
from datetime import timedelta

from eventmem.core.db import Conflict, Missing, digest, dumps

from .state import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_desire_archive(
 scope TEXT NOT NULL,id TEXT NOT NULL,status TEXT NOT NULL,kind TEXT NOT NULL,
 exploration_id TEXT,sharing_revision INTEGER,plan_id TEXT,
 archived_at TEXT NOT NULL,archived_revision INTEGER NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,id));
CREATE INDEX IF NOT EXISTS mind_desire_archive_intent
 ON mind_desire_archive(scope,exploration_id,sharing_revision);
"""

# Off unless a store says otherwise, like the patch format and unlike every other stage-5 flag:
# this one decides what the document the model is shown contains, so it is turned on as its own
# decision, once a dry run has been read.
FLAG = "desire_archive"
# The two history kinds this writes. Both are ordinary revisions: the move is a change of state
# like any other, and the row says which wishes moved.
KIND = "desire-archive"
RESTORE_KIND = "desire-unarchive"
# Where the count of what has moved lives in the state document. Present only once something has,
# so a store that never archives anything carries the document it always did, key for key.
STATE_KEY = "desire_archive"
# A wish the model has finished with. No other status can be archived, whatever its age.
TERMINAL = frozenset({"completed", "abandoned"})
# A wish that is still the model's to settle. Expiry is not one of these: an expired wish is still
# `wanted`, and a reader that treated the clock as a verdict would be deciding on its own.
LIVE = frozenset({"wanted", "waiting", "in_progress"})
# How many finished wishes the appraisal projection still shows, from the end of the document.
# Kept live so the projection is unchanged by the move. Must equal the tail that appraisal.py
# takes, and it is a tail of the document's own order, which is its identifiers sorted — not the
# newest, whatever the order reads like.
WINDOW = 8
# The floor under a finished wish, in days. Not a reason to archive: a wish that has not been
# settled is not archived at any age, and this only keeps a recent settlement in the document
# while it is still likely to be discussed.
DAYS = 30
# Still going: a contact attempt before its receipt, a plan run mid-flight or with an unconfirmed
# send, an exploration at work. An action event settles as `complete`, or as `superseded` when a
# recovery replaces it; anything else is unsettled, including one left for review.
OPEN_CONTACTS = ("drafting", "pending", "unconfirmed")
OPEN_RUNS = ("running", "unconfirmed")
SETTLED_ACTIONS = ("complete", "superseded")
RUNNING = "running"
ACTIVE_PLAN = "active"
# How many identifiers a report names before it stops listing. Counts are always complete.
SAMPLE = 200

# Why a wish stays. Reported by name, with the sentence, because a report that says a wish was
# held without saying what holds it cannot be acted on.
LIVE_WISH = "live"
UNDATED = "undated"
RECENT = "recent"
WINDOW_HELD = "window"
DELIVERY_OPEN = "delivery-open"
CONTACT_OPEN = "contact-open"
RUN_OPEN = "run-open"
PLAN_ACTIVE = "plan-active"
ACTION_OPEN = "action-open"
EXPLORING = "exploration-running"

REASONS = {
    LIVE_WISH: "still the model's own to settle: wanted, waiting or in progress, expired or not",
    UNDATED: "carries no timestamp to judge its age by",
    RECENT: "settled more recently than the floor this run was given",
    WINDOW_HELD: "one of the finished wishes the appraisal projection still shows",
    DELIVERY_OPEN: "its delivery is recorded as partial, so what arrived is not settled",
    CONTACT_OPEN: "a contact attempt for it is still drafting, pending or unconfirmed",
    RUN_OPEN: "a plan run for it is still running or unconfirmed",
    PLAN_ACTIVE: "an active plan or one of its steps still names it",
    ACTION_OPEN: "an action event about it has not settled",
    EXPLORING: "a running exploration is working on it",
}


def installed(conn):
    """Whether this store has the archive at all. Every `Mind` creates it, and a reader that finds
    it absent answers as it did before this module existed rather than failing."""
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mind_desire_archive'").fetchone())


def _tables(conn):
    """Which of the tables a reference could live in this store has. A bare mind has the contacts
    and the action events; the plans and the explorations arrive with their own layers."""
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN"
        " ('mind_plans','mind_plan_runs','mind_explorations','mind_contacts','mind_action_events')"
    ).fetchall()}


# --- reading the archive -------------------------------------------------------------------------

def archived(conn, scope, identifier):
    """The wish this identifier names, if it has moved. A copy read from the archive: the callers
    are readers, and the wishes here are finished, so every path that would write one refuses
    before it gets this far."""
    if not identifier or not installed(conn):
        return None
    row = conn.execute("SELECT data FROM mind_desire_archive WHERE scope=? AND id=?",
                       (scope, identifier)).fetchone()
    return json.loads(row[0]) if row else None


def count(conn, scope):
    if not installed(conn):
        return 0
    return conn.execute("SELECT COUNT(*) FROM mind_desire_archive WHERE scope=?", (scope,)).fetchone()[0]


def intent(conn, scope, exploration_id, sharing_revision, *, exclude=None):
    """Whether an archived wish already carries the contact intent for this sharing decision.

    The one question that must survive the move. A sharing decision may have one contact intent,
    and the live check reads every wish whatever its status — so a finished wish that moved would
    stop blocking, and the next appraisal would make a second intent for a decision that already
    had one. That is a duplicate message to the owner, which is the one outcome this whole package
    must not buy with its saving."""
    if not exploration_id or sharing_revision is None or not installed(conn):
        return False
    row = conn.execute(
        "SELECT id FROM mind_desire_archive WHERE scope=? AND exploration_id=? AND sharing_revision=?"
        + (" AND id<>?" if exclude else "") + " LIMIT 1",
        (scope, exploration_id, sharing_revision) + ((exclude,) if exclude else ()),
    ).fetchone()
    return bool(row)


def contents(conn, scope):
    """What the archived wishes asked for, as text, for the one dedupe that reads finished wishes
    by their content: a bootstrap appraisal, which is allowed to see a wish it already made and
    must not make it again because that wish has moved."""
    if not installed(conn):
        return set()
    return {row[0] for row in conn.execute(
        "SELECT json_extract(data,'$.content') FROM mind_desire_archive WHERE scope=?",
        (scope,)).fetchall() if row[0] is not None}


# --- what holds a wish ---------------------------------------------------------------------------

def _references(conn, scope):
    """Every wish identifier something still open names, and which reason names it.

    Read once per run rather than once per wish: a hundred and twenty wishes against five tables
    is five queries here and six hundred the other way."""
    found, tables = {}, _tables(conn)

    def mark(identifier, reason):
        if identifier:
            found.setdefault(identifier, set()).add(reason)

    if "mind_contacts" in tables:
        for row in conn.execute(
            "SELECT json_extract(data,'$.desire_id') FROM mind_contacts WHERE scope=? AND state IN"
            " (" + ",".join("?" * len(OPEN_CONTACTS)) + ")", (scope, *OPEN_CONTACTS)):
            mark(row[0], CONTACT_OPEN)
    if "mind_action_events" in tables:
        for row in conn.execute(
            "SELECT json_extract(data,'$.desire_id') FROM mind_action_events WHERE scope=? AND state"
            " NOT IN (" + ",".join("?" * len(SETTLED_ACTIONS)) + ")", (scope, *SETTLED_ACTIONS)):
            mark(row[0], ACTION_OPEN)
    if "mind_explorations" in tables:
        for row in conn.execute(
            "SELECT json_extract(data,'$.desire_id') FROM mind_explorations WHERE scope=? AND state=?",
            (scope, RUNNING)):
            mark(row[0], EXPLORING)
    if "mind_plans" in tables:
        open_steps = {}
        if "mind_plan_runs" in tables:
            for row in conn.execute(
                "SELECT plan_id,step_id FROM mind_plan_runs WHERE scope=? AND state IN"
                " (" + ",".join("?" * len(OPEN_RUNS)) + ")", (scope, *OPEN_RUNS)):
                open_steps.setdefault(row[0], set()).add(row[1])
        for row in conn.execute("SELECT id,status,data FROM mind_plans WHERE scope=?", (scope,)):
            if row[1] != ACTIVE_PLAN and row[0] not in open_steps:
                continue
            plan = json.loads(row[2])
            steps = plan.get("steps") or []
            if row[1] == ACTIVE_PLAN:
                mark(plan.get("desire_id"), PLAN_ACTIVE)
                for step in steps:
                    mark(step.get("desire_id"), PLAN_ACTIVE)
            for step in steps:
                if step.get("id") in open_steps.get(row[0], ()):
                    mark(step.get("desire_id"), RUN_OPEN)
                    # A single-step plan carries the wish on the plan itself.
                    mark(plan.get("desire_id"), RUN_OPEN)
    return found


def _window(state, at):
    """The identifiers of the finished wishes the appraisal projection still shows.

    The same tail that projection takes, computed the same way: whatever is not an unexpired live
    wish, in the document's own order, last `WINDOW` of them. The order is the identifiers sorted,
    because that is how the document is stored and read back, so this is a stable set rather than
    the newest wishes — matching the projection is the point, not what the tail is made of."""
    finished = [identifier for identifier, desire in state["desires"].items()
                if not (desire["status"] in LIVE
                        and timestamp(desire["expires_at"]) > timestamp(at))]
    return set(finished[-WINDOW:])


def holds(conn, mind, state, at, *, days=DAYS):
    """Every reason each wish in the document has for staying, by identifier.

    A wish with no reasons is one that may move. `live` is decided on its own and stops there:
    what the model has not settled is never examined further, let alone moved."""
    # Compared as moments rather than as text: the recorded timestamps are written by several
    # clocks at several precisions, and `2026-01-01T00:00:00Z` sorts after `2026-01-01T00:00:00.5`
    # as a string while being the earlier of the two.
    floor = timestamp(at) - timedelta(days=max(0, days))
    references, window = _references(conn, mind.scope.key()), _window(state, at)
    reasons = {}
    for identifier, desire in state["desires"].items():
        found = set()
        if desire["status"] not in TERMINAL:
            reasons[identifier] = {LIVE_WISH}
            continue
        settled = desire.get("updated_at") or desire.get("created_at")
        if not settled:
            found.add(UNDATED)
        elif timestamp(settled) > floor:
            found.add(RECENT)
        if identifier in window:
            found.add(WINDOW_HELD)
        delivery = desire.get("delivery") or {}
        if delivery and delivery.get("state") != "accepted":
            found.add(DELIVERY_OPEN)
        found |= references.get(identifier, set())
        reasons[identifier] = found
    return reasons


def survey(conn, mind, state, at, *, days=DAYS):
    """What would move and what holds everything else. Read only, and the whole of the decision:
    the apply re-runs this inside its own transaction and moves only what it still names."""
    reasons = holds(conn, mind, state, at, days=days)
    moving = sorted(identifier for identifier, found in reasons.items() if not found)
    holding = {identifier: sorted(found) for identifier, found in reasons.items() if found}
    return moving, holding


# --- moving them ---------------------------------------------------------------------------------

def _store(conn, scope, state, identifier, desire, at):
    """One wish, into the archive, whole. The columns are what a reader has to find it by; `data`
    is the wish itself, unchanged, which is what makes the move reversible.

    `archived_revision` is the revision this move is making. A mutation runs before its own
    revision is taken — the number is bumped once the change is in the document — so the revision
    whose history row records this move is the one after the state being written."""
    conn.execute(
        "INSERT INTO mind_desire_archive VALUES(?,?,?,?,?,?,?,?,?,?)",
        (scope, identifier, desire["status"], desire["kind"], desire.get("exploration_id"),
         desire.get("sharing_revision"), desire.get("plan_id"), at, state["revision"] + 1,
         dumps(desire)),
    )


def _counted(conn, scope, state):
    """Keep the state document's own count of what has moved in step with the table, inside the
    same transaction that moved it. The projection and the manifest both read this number rather
    than the table, so a rebuilt revision carries the count it had, and a document that has never
    archived anything does not carry the key at all."""
    found = count(conn, scope)
    if found:
        state[STATE_KEY] = {"count": found}
    else:
        state.pop(STATE_KEY, None)
    return found


def _report(mind, document, moving, holding, *, sample=SAMPLE, **extra):
    counts = {}
    for found in holding.values():
        for reason in found:
            counts[reason] = counts.get(reason, 0) + 1
    return {"scope": mind.scope.key(), "flag": FLAG, "revision": document["revision"],
            "desires": len(document["desires"]),
            "would_archive": moving[:sample], "would_archive_count": len(moving),
            "holding": {k: holding[k] for k in sorted(holding)[:sample]},
            "holding_count": len(holding), "holding_reasons": counts,
            "reasons": {k: REASONS[k] for k in sorted(counts)}, **extra}


def archive(mind, *, apply=False, days=DAYS, limit=None, sample=SAMPLE):
    """Move every finished wish that nothing is still holding into the archive.

    The dry run asks for no flag and writes nothing at all — not a row, not a revision — so it can
    be read against a copy of a live store before anything is decided. An apply asks for the flag,
    re-decides inside its own write transaction, and records the move as an ordinary revision with
    its own history row, so what moved and when is in the history like every other change."""
    scope, at, days = mind.scope.key(), mind.clock(), max(0, int(days))
    with mind.engine.db.connect() as conn:
        from .autonomy_schema import enabled
        state = mind._load(conn)
        moving, holding = survey(conn, mind, state, at, days=days)
        allowed, stored = enabled(conn, scope, FLAG), count(conn, scope)
    if limit is not None:
        moving = moving[:max(0, int(limit))]
    if apply and not allowed:
        raise Conflict("Wish archiving is switched off for this scope",
                       kind="runtime", code="desire-archive-disabled")
    if not moving:
        # An apply that finds nothing to move is not a failure. It is the ordinary answer on a
        # store where everything finished is recent, still held, or already here.
        return _report(mind, state, moving, holding, sample=sample, enabled=allowed, days=days,
                       archived=stored, state="archived" if apply else "dry-run",
                       **({"moved": [], "moved_count": 0, "event_id": None} if apply else {}))
    if not apply:
        return _report(mind, state, moving, holding, sample=sample, state="dry-run",
                       enabled=allowed, days=days, archived=stored)

    def move(conn, current, event_id):
        # Decided again here, against the state this transaction holds: between the survey and
        # this write another command may have reopened a plan or started a send, and a wish that
        # something now holds is a wish that stays.
        again, _ = survey(conn, mind, current, mind.clock(), days=days)
        chosen = [identifier for identifier in moving if identifier in set(again)]
        for identifier in chosen:
            _store(conn, scope, current, identifier, current["desires"].pop(identifier), mind.clock())
        _counted(conn, scope, current)
        return {"archived": chosen}

    # The revision is part of the command's identity as well as its precondition. Two runs that
    # named the same wishes from the same revision are the same command and the second is answered
    # from the first — but a run that reached a new revision, even one that moved nothing because
    # something had just taken hold of every wish it named, is a new command and is tried again.
    payload = {"command_id": KIND + ":" + digest([moving, state["revision"]])[:32],
               "agent_version": state["agent_version"],
               "expected_revision": state["revision"], "desire_ids": moving}
    result = mind._mutate(payload, KIND, move)
    with mind.engine.db.connect() as conn:
        current = mind._load(conn)
        remaining, holding = survey(conn, mind, current, mind.clock(), days=days)
        return _report(mind, current, remaining, holding, sample=sample, state="archived",
                       enabled=True, days=days, archived=count(conn, scope),
                       moved=result["archived"][:sample], moved_count=len(result["archived"]),
                       event_id=result["event_id"])


def restore(mind, *, apply=False, ids=None, sample=SAMPLE):
    """Put archived wishes back into the state document.

    Never gated by the flag: this is what runs before a rollback, and a release that cannot see
    the archive must find every wish where it has always looked. With no identifiers it restores
    all of them, which is what that rollback asks for. The wishes come back exactly as they were
    stored — the document sorts its keys, so a full round trip gives back the document it had."""
    scope = mind.scope.key()
    ids = [ids] if isinstance(ids, str) else ids
    wanted = sorted(dict.fromkeys(ids)) if ids else None
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        rows = conn.execute("SELECT id,data FROM mind_desire_archive WHERE scope=? ORDER BY id",
                            (scope,)).fetchall()
    held = {row["id"]: row["data"] for row in rows}
    unknown = sorted(set(wanted) - set(held)) if wanted else []
    found = [identifier for identifier in (wanted or sorted(held)) if identifier in held]
    live = [identifier for identifier in found if identifier in state["desires"]]
    if live:
        # Two copies of one wish is the one state this module may never produce, and a restore
        # that quietly overwrote the live one would be exactly that, silently.
        raise Conflict("A wish cannot be restored over the live one that already carries its id",
                       kind="runtime", code="desire-archive-live", target=live[0])
    report = {"scope": scope, "archived": len(held), "unknown": unknown[:sample],
              "unknown_count": len(unknown)}
    if not found and unknown:
        raise Missing("No archived wish carries that id", kind="runtime", target=unknown[0])
    if not apply or not found:
        # Nothing archived and an apply is the idempotent answer, not a failure: it is what a
        # second `desire-unarchive` before a rollback finds, and it has to read as finished.
        return {**report, "state": "restored" if apply else "dry-run",
                "would_restore": found[:sample], "would_restore_count": len(found),
                "restored_count": 0, "revision": state["revision"]}

    def put_back(conn, current, event_id):
        back = []
        for identifier in found:
            if identifier in current["desires"]:
                continue
            row = conn.execute("SELECT data FROM mind_desire_archive WHERE scope=? AND id=?",
                               (scope, identifier)).fetchone()
            if not row:
                continue
            current["desires"][identifier] = json.loads(row[0])
            # The wish is in the document again before its row goes, in the one transaction: what
            # leaves the archive is only ever what the state now carries.
            conn.execute("DELETE FROM mind_desire_archive WHERE scope=? AND id=?", (scope, identifier))
            back.append(identifier)
        # Stored sorted whatever happens here, so the in-memory document is put in the order it
        # will be read back in rather than in the order the wishes were put back.
        current["desires"] = {k: current["desires"][k] for k in sorted(current["desires"])}
        _counted(conn, scope, current)
        return {"restored": back}

    payload = {"command_id": RESTORE_KIND + ":" + digest([found, state["revision"]])[:32],
               "agent_version": state["agent_version"], "expected_revision": state["revision"],
               "desire_ids": found}
    result = mind._mutate(payload, RESTORE_KIND, put_back)
    with mind.engine.db.connect() as conn:
        return {**report, "state": "restored", "restored": result["restored"][:sample],
                "restored_count": len(result["restored"]), "archived": count(conn, scope),
                "revision": result["revision"], "event_id": result["event_id"]}
