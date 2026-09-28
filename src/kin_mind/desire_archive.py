"""Which wishes the state document keeps, and where the others go.

The state document is written again on every revision, held whole in memory, projected into every
appraisal and carried into every context the model is shown. On 2026-09-28 it had grown past the two
million characters the host's resident worker takes back from a read, and the proactive contact that
reads it stopped. The wishes were a large part of it: every wish ever made, most long finished, each
carrying copies of what it rested on.

**The rule (the owner's, 2026-09-28).** The document keeps the wishes whose latest activity --
made, updated or settled -- falls within the last `DAYS` days, and of those at most the `KEEP`
newest. Everything else moves to `mind_desire_archive`, finished or not. Both numbers are the
memory settings `desire_retention_days` and `desire_retention_keep`.

**What the rule never moves.** A wish that an execution still holds stays until that execution
ends, whatever its age or rank: a contact attempt still drafting, pending or unconfirmed; an active
plan, or a plan run still running or unconfirmed; an action event that has not settled, including
one left for review; a running exploration; a delivery recorded as partial. Each is a reason the
wish is held, reported by name. A held wish does not take the place of a newer one: the document
holds the newest `KEEP` and every held wish beside them.

**Finished, or let go.** A wish that reached `completed` or `abandoned` moves as it always has. A
wish that had not -- `wanted`, `waiting`, `in_progress` -- is let go (放下): it moves whole, with its
status as it was, which is how the archive marks it (`outcome`), and it no longer drives contact or
exploration, because those read the live document. The difference that matters afterwards is what
each still blocks. A finished wish that moved still blocks a duplicate exactly as before: a second
contact intent for the same sharing decision, the same content on a bootstrap. A wish that was let
go blocks neither, so Kin can raise it again later as a new wish. By identity both stay what they
were: the id a command derived is refused a second wish, and an update of either is refused (a let-go
wish with its own code, `desire-let-go`).

**A move, never a delete.** An archived wish keeps its identifier, plan links, evidence references
and decision receipt, exactly as they were. A reader that misses in the live set looks here.
`desire-unarchive` puts them back; because the state is stored with its keys sorted, a wish that
comes back lands where it was, byte for byte.

**It runs by itself.** While the `desire_archive` flag is on, the rule runs after every committed
assessment (`retain`, from the host's review): a read that finds nothing to move writes nothing, and a
move is one ordinary revision in one write transaction, decided again inside it. It is deferred while
an assessment of the action lane is still running under its lease -- that assessment was shown the
wishes, and taking one away under it would only cost it its commit -- and the next committed
assessment tries again. The explicit `desire-archive` / `desire-unarchive` commands apply the same
rule, and the dry run asks for no flag and writes nothing.

**What is remembered.** Every wish that moves becomes a short memory in Kin's own voice, written by
DeepSeek in the background (`archive_memory`, kind `desire`); the move queues it in the same
transaction and never waits for it. `archive-memory-backfill` queues the wishes archived before this.

**The window.** The appraisal projection shows the live wishes and a tail of `WINDOW` finished ones,
now the most recently active of those the rule keeps, so it never holds a wish the rule would move.
What has moved is counted in the state document, so `desire_window.total` still says how many wishes
exist, and the manifest's drift check reads the same number.

An erase deletes the sources and records and scrubs every mind table that names them, this archive
among them: an archived wish that cited erased material loses its words, and its memory entry, a
derived source, goes with the evidence. The state history is rewritten by its own job.
"""

from __future__ import annotations

import json
from datetime import timedelta, timezone

from eventmem.core.db import Conflict, Missing, digest, dumps

from . import archive_memory
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

# Off unless a store says otherwise: this decides what the document the model is shown contains,
# and the release before it cannot see an archived wish at all. On, the rule runs by itself.
FLAG = "desire_archive"
# The two history kinds this writes. Both are ordinary revisions.
KIND = "desire-archive"
RESTORE_KIND = "desire-unarchive"
# Where the count of what has moved lives in the state document. Present only once something has.
STATE_KEY = "desire_archive"
# A wish the model has finished with, and one it had not.
TERMINAL = frozenset({"completed", "abandoned"})
LIVE = frozenset({"wanted", "waiting", "in_progress"})
# The outcome an archived wish is marked with: its status says which.
FINISHED, LET_GO = "finished", "let-go"
# The rule's two numbers and the settings that change them.
DAYS, KEEP = 7, 10
DAYS_SETTING, KEEP_SETTING = "desire_retention_days", "desire_retention_keep"
DAYS_RANGE, KEEP_RANGE = (0, 3650), (0, 1000)
# What counts as a wish's activity: when it was made, last changed, settled.
ACTIVITY = ("created_at", "updated_at", "settled_at")
# How many finished wishes the appraisal projection shows: the most recently active of those the
# rule keeps (appraisal.appraisal_context takes it from here).
WINDOW = 8
# Still going: a contact attempt before its receipt, a plan run mid-flight or with an unconfirmed
# send, an exploration at work. An action event settles as `complete`, or as `superseded` when a
# recovery replaces it; anything else is unsettled, including one left for review.
OPEN_CONTACTS = ("drafting", "pending", "unconfirmed")
OPEN_RUNS = ("running", "unconfirmed")
SETTLED_ACTIONS = ("complete", "superseded")
RUNNING = "running"
ACTIVE_PLAN = "active"
# The assessments whose context showed the wishes: every lane but the memory history and the session
# maintenance, which are shown none (manifest.IRRELEVANT, RELEVANT_ONLY).
UNSHOWN_STIMULI = ("memory-backfill", "memory-enrichment", "session-maintenance")
# How many identifiers a report names before it stops listing. Counts are always complete.
SAMPLE = 200
# The codes an automatic run answers with instead of a revision.
DEFERRED, UNCHANGED = "desire-retention-deferred", "desire-retention-unchanged"
# The kind these wishes are remembered as (archive_memory).
MEMORY_KIND = "desire"
# The zone a remembered time is written in when the store names none.
DEFAULT_ZONE = "Asia/Singapore"

# Why a wish stays. Reported by name, with the sentence.
RECENT = "recent"
DELIVERY_OPEN = "delivery-open"
CONTACT_OPEN = "contact-open"
RUN_OPEN = "run-open"
PLAN_ACTIVE = "plan-active"
ACTION_OPEN = "action-open"
EXPLORING = "exploration-running"

REASONS = {
    RECENT: "one of the newest wishes by latest activity, inside the window this run was given",
    DELIVERY_OPEN: "its delivery is recorded as partial, so what arrived is not settled",
    CONTACT_OPEN: "a contact attempt for it is still drafting, pending or unconfirmed",
    RUN_OPEN: "a plan run for it is still running or unconfirmed",
    PLAN_ACTIVE: "an active plan or one of its steps still names it",
    ACTION_OPEN: "an action event about it has not settled",
    EXPLORING: "a running exploration is working on it",
}


def installed(conn):
    """Whether this store has the archive at all."""
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mind_desire_archive'").fetchone())


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN"
        " ('mind_plans','mind_plan_runs','mind_explorations','mind_contacts','mind_action_events')"
    ).fetchall()}


def outcome(status):
    return FINISHED if status in TERMINAL else LET_GO


# --- reading the archive -------------------------------------------------------------------------

def archived(conn, scope, identifier):
    """The wish this identifier names, if it has moved: a copy read from the archive. Finished or
    let go, it is still that wish by identity, and every path that would write one refuses."""
    if not identifier or not installed(conn):
        return None
    row = conn.execute("SELECT data FROM mind_desire_archive WHERE scope=? AND id=?",
                       (scope, identifier)).fetchone()
    return json.loads(row[0]) if row else None


def count(conn, scope):
    if not installed(conn):
        return 0
    return conn.execute("SELECT COUNT(*) FROM mind_desire_archive WHERE scope=?", (scope,)).fetchone()[0]


def let_go(conn, scope, identifier):
    """Whether this identifier names a wish that was let go unfinished."""
    if not identifier or not installed(conn):
        return False
    row = conn.execute("SELECT status FROM mind_desire_archive WHERE scope=? AND id=?", (scope, identifier)).fetchone()
    return bool(row) and row[0] not in TERMINAL


def intent(conn, scope, exploration_id, sharing_revision, *, exclude=None):
    """Whether a finished archived wish already carries the contact intent for this sharing decision.

    A sharing decision may have one contact intent, and the live check reads every wish whatever its
    status: a finished intent that moved must keep blocking, or the next appraisal would make a second
    one and send the owner the same thing twice. One that was let go before it was ever sent does not
    block: the decision may be raised again."""
    if not exploration_id or sharing_revision is None or not installed(conn):
        return False
    row = conn.execute(
        "SELECT id FROM mind_desire_archive WHERE scope=? AND exploration_id=? AND sharing_revision=?"
        " AND status IN ('completed','abandoned')"
        + (" AND id<>?" if exclude else "") + " LIMIT 1",
        (scope, exploration_id, sharing_revision) + ((exclude,) if exclude else ()),
    ).fetchone()
    return bool(row)


def contents(conn, scope):
    """What the finished archived wishes asked for, as text, for the one dedupe that reads finished
    wishes by content: a bootstrap appraisal must not make again a wish it already made. A wish let
    go unfinished is not among them: it may be made again."""
    if not installed(conn):
        return set()
    return {row[0] for row in conn.execute(
        "SELECT json_extract(data,'$.content') FROM mind_desire_archive WHERE scope=?"
        " AND status IN ('completed','abandoned')", (scope,)).fetchall() if row[0] is not None}


# --- the rule ------------------------------------------------------------------------------------

def _moment(value):
    try:
        moment = timestamp(value)
    except (TypeError, ValueError, AttributeError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def activity(desire):
    """The wish's latest activity: the latest of when it was made, changed and settled. None for a
    wish that carries no time at all, which ranks after every dated one."""
    moments = [found for found in (_moment(desire.get(key)) for key in ACTIVITY if isinstance(desire.get(key), str))
               if found is not None]
    return max(moments) if moments else None


def window(conn, scope, days=None, keep=None):
    """The rule's numbers: as asked, else as configured, else the owner's 7 days and 10 wishes."""
    from .autonomy_schema import settings
    stored = settings(conn, scope)
    days = stored.get(DAYS_SETTING, DAYS) if days is None else days
    keep = stored.get(KEEP_SETTING, KEEP) if keep is None else keep
    if type(days) is not int or not DAYS_RANGE[0] <= days <= DAYS_RANGE[1]:
        raise ValueError("A retention window is a whole number of days")
    if type(keep) is not int or not KEEP_RANGE[0] <= keep <= KEEP_RANGE[1]:
        raise ValueError("A retention cap is a whole number of wishes")
    return days, keep


def _references(conn, scope):
    """Every wish identifier something still open names, and which reason names it. Read once per
    run: a hundred wishes against five tables is five queries here."""
    found, tables = {}, _tables(conn)

    def mark(identifier, reason):
        if identifier:
            found.setdefault(identifier, set()).add(reason)

    if "mind_contacts" in tables:
        for row in conn.execute(
            "SELECT data FROM mind_contacts WHERE scope=? AND state IN"
            " (" + ",".join("?" * len(OPEN_CONTACTS)) + ")", (scope, *OPEN_CONTACTS)):
            # An attempt offers every ready wish (N11); each one it offered is still open.
            attempt = json.loads(row[0])
            for identifier in attempt.get("desire_ids") or [attempt.get("desire_id")]:
                mark(identifier, CONTACT_OPEN)
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


def survey(conn, mind, state, at, *, days=DAYS, keep=KEEP):
    """The whole decision, read only: `kept` (the newest `keep` inside `days`, newest first),
    `holding` (identifier -> the reasons an open execution holds it), and `moving` (everything else,
    sorted). The apply runs this again inside its own transaction and moves only what it still names."""
    now = _moment(at)
    floor = now - timedelta(days=max(0, days))
    moments = {identifier: activity(desire) for identifier, desire in state["desires"].items()}
    recent = sorted((identifier for identifier, moment in moments.items() if moment is not None and moment >= floor),
                    key=lambda identifier: (moments[identifier], identifier), reverse=True)[:max(0, keep)]
    chosen = set(recent)
    references = _references(conn, mind.scope.key())
    holding, moving = {}, []
    for identifier, desire in state["desires"].items():
        if identifier in chosen:
            continue
        found = set(references.get(identifier, ()))
        delivery = desire.get("delivery") or {}
        if delivery and delivery.get("state") != "accepted":
            found.add(DELIVERY_OPEN)
        if found:
            holding[identifier] = sorted(found)
        else:
            moving.append(identifier)
    return {"kept": recent, "holding": holding, "moving": sorted(moving)}


def _assessing(conn, scope, now=None):
    """The assessments still running under a fresh lease whose context showed the wishes."""
    import time
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_appraisals'").fetchone():
        return []
    return [row[0] for row in conn.execute(
        "SELECT id FROM mind_appraisals WHERE scope=? AND state='running' AND lease>?"
        " AND COALESCE(json_extract(data,'$.stimulus'),'') NOT IN (" + ",".join("?" * len(UNSHOWN_STIMULI)) + ")",
        (scope, time.time() if now is None else now, *UNSHOWN_STIMULI))]


# --- what is remembered of a wish ------------------------------------------------------------------

def _zone(state):
    from zoneinfo import ZoneInfo
    zone = (((state or {}).get("profile") or {}).get("contact") or {}).get("timezone") or DEFAULT_ZONE
    try:
        return ZoneInfo(zone)
    except (ValueError, KeyError):
        return ZoneInfo(DEFAULT_ZONE)


def _local(value, zone):
    moment = _moment(value) if isinstance(value, str) else value
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M") if moment else None


def memory_item(desire, state=None):
    """What the memory of a wish may be written from, and nothing else: its topic and content, its
    kind and status, how it ended, its completion condition, its latest reason and its times. Never
    its evidence references, which carry copies of what they were read from."""
    zone = _zone(state)
    status = desire.get("status")
    ended = {"completed": "completed", "abandoned": "abandoned"}.get(status, "let_go_unfinished")
    latest = activity(desire)
    evidence = [ref for ref in desire.get("evidence") or [] if isinstance(ref, dict)]
    return {
        "id": desire["id"], "revision": desire.get("revision") if type(desire.get("revision")) is int else 1,
        "summary_input": {"topic": desire.get("topic"), "content": desire.get("content"), "wish_kind": desire.get("kind"),
                          "status": status, "outcome": ended, "completion": desire.get("completion"),
                          "reason": desire.get("reason"), "created_at": _local(desire.get("created_at"), zone),
                          "last_activity_at": _local(latest, zone), "expires_at": _local(desire.get("expires_at"), zone),
                          "timezone": str(zone)},
        "evidence_ids": [{k: ref[k] for k in ("source_id", "record_id") if isinstance(ref.get(k), str)} for ref in evidence],
        "occurred_at": latest.isoformat() if latest else desire.get("created_at"),
        "refs": [ref["record_id"] for ref in evidence if isinstance(ref.get("record_id"), str)][:archive_memory.REFS_LIMIT],
    }


# --- moving them ---------------------------------------------------------------------------------

def _store(conn, scope, state, identifier, desire, at):
    """One wish, into the archive, whole. The columns are what a reader finds it by -- `status` among
    them, which is also its outcome; `data` is the wish itself, unchanged, which makes the move
    reversible. `archived_revision` is the revision this move is making. In a document whose
    references are kept slim (evidence_refs), the archived wish's are kept the same way."""
    from .evidence_refs import marked, slimmed
    conn.execute(
        "INSERT INTO mind_desire_archive VALUES(?,?,?,?,?,?,?,?,?,?)",
        (scope, identifier, desire["status"], desire["kind"], desire.get("exploration_id"),
         desire.get("sharing_revision"), desire.get("plan_id"), at, state["revision"] + 1,
         dumps(slimmed(desire) if marked(state) else desire)),
    )


def _counted(conn, scope, state):
    """The state document's own count of what has moved, kept in step with the table inside the same
    transaction. Absent while nothing has moved."""
    found = count(conn, scope)
    if found:
        state[STATE_KEY] = {"count": found}
    else:
        state.pop(STATE_KEY, None)
    return found


def _report(mind, document, found, *, sample=SAMPLE, **extra):
    moving, holding, kept = found["moving"], found["holding"], found["kept"]
    letting = [identifier for identifier in moving if document["desires"][identifier].get("status") not in TERMINAL]
    counts = {}
    for reasons in holding.values():
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    named = set(counts) | ({RECENT} if kept else set())
    return {"scope": mind.scope.key(), "flag": FLAG, "revision": document["revision"],
            "desires": len(document["desires"]),
            "would_archive": moving[:sample], "would_archive_count": len(moving),
            "would_finish_count": len(moving) - len(letting),
            "would_let_go": letting[:sample], "would_let_go_count": len(letting),
            "kept": kept[:sample], "kept_count": len(kept),
            "holding": {k: holding[k] for k in sorted(holding)[:sample]},
            "holding_count": len(holding), "holding_reasons": counts,
            "reasons": {k: REASONS[k] for k in sorted(named)}, **extra}


def archive(mind, *, apply=False, days=None, keep=None, limit=None, sample=SAMPLE, automatic=False):
    """Apply the rule: move every wish it does not keep and nothing holds into the archive.

    The dry run asks for no flag and writes nothing at all -- not a row, not a revision -- so it can
    be read against a copy of a live store. An apply asks for the flag, decides again inside its own
    write transaction against whatever revision it finds there, and records the move as an ordinary
    revision with its own history row; the memory of each wish is queued in the same transaction. A
    run that finds nothing left to move there writes nothing. `automatic` is the host's own run after
    an assessment: it also defers while an assessment that was shown the wishes is still running."""
    scope, at = mind.scope.key(), mind.clock()
    with mind.engine.db.connect() as conn:
        from .autonomy_schema import enabled
        days, keep = window(conn, scope, days, keep)
        state = mind._load(conn)
        found = survey(conn, mind, state, at, days=days, keep=keep)
        allowed, stored = enabled(conn, scope, FLAG), count(conn, scope)
    moving = found["moving"] if limit is None else found["moving"][:max(0, int(limit))]
    if apply and not allowed:
        raise Conflict("Wish archiving is switched off for this scope",
                       kind="runtime", code="desire-archive-disabled")
    settings = {"enabled": allowed, "days": days, "keep": keep, "archived": stored}
    nothing = {"moved": [], "moved_count": 0, "let_go": [], "let_go_count": 0, "event_id": None, "memory_queued": 0}
    if not moving:
        # Not a failure: the ordinary answer on a store where everything is kept, held or moved.
        return _report(mind, state, found, sample=sample, state="archived" if apply else "dry-run",
                       **settings, **(nothing if apply else {}))
    if not apply:
        return _report(mind, state, found, sample=sample, state="dry-run", **settings)

    def move(conn, current, event_id):
        if automatic:
            busy = _assessing(conn, scope)
            if busy:
                raise Conflict("An assessment that was shown the wishes is still running",
                               kind="runtime", code=DEFERRED, actual=len(busy))
        # Decided again against the state this transaction holds: between the survey and this write
        # another command may have made a wish, reopened a plan or started a send.
        again = set(survey(conn, mind, current, mind.clock(), days=days, keep=keep)["moving"])
        chosen = [identifier for identifier in moving if identifier in again]
        if not chosen:
            raise Conflict("Nothing the rule would move is left to move", kind="runtime", code=UNCHANGED)
        moved_at, letting, items = mind.clock(), [], []
        for identifier in chosen:
            desire = current["desires"].pop(identifier)
            _store(conn, scope, current, identifier, desire, moved_at)
            if desire["status"] not in TERMINAL:
                letting.append(identifier)
            items.append(memory_item(desire, current))
        queued = archive_memory.enqueue(mind, conn, MEMORY_KIND, items)
        _counted(conn, scope, current)
        return {"archived": chosen, "let_go": letting, "memory_queued": queued}

    # The revision is part of the command's identity: two runs that named the same wishes from the
    # same revision are one command, and the second is answered from the first.
    payload = {"command_id": KIND + ":" + digest([moving, state["revision"], days, keep])[:32],
               "agent_version": state["agent_version"],
               "expected_revision": state["revision"], "desire_ids": moving}
    try:
        # Any revision is a revision to decide on: `move` decides again inside the transaction.
        result = mind._mutate(payload, KIND, move, rebase=lambda conn, current: True)
    except Conflict as error:
        if getattr(error, "code", None) not in {DEFERRED, UNCHANGED}:
            raise
        return _report(mind, state, found, sample=sample, **settings, **nothing,
                       state="deferred" if error.code == DEFERRED else "archived",
                       **({"reason": DEFERRED} if error.code == DEFERRED else {}))
    with mind.engine.db.connect() as conn:
        current = mind._load(conn)
        remaining = survey(conn, mind, current, mind.clock(), days=days, keep=keep)
        return _report(mind, current, remaining, sample=sample, state="archived",
                       enabled=True, days=days, keep=keep, archived=count(conn, scope),
                       moved=result["archived"][:sample], moved_count=len(result["archived"]),
                       let_go=result.get("let_go", [])[:sample], let_go_count=len(result.get("let_go", [])),
                       memory_queued=result.get("memory_queued", 0), event_id=result["event_id"])


def retain(mind):
    """The rule, run by the host after a committed assessment while the flag is on. Answers with
    counts only, and never raises: a failure here is reported and tried again after the next one."""
    from .autonomy_schema import enabled
    from .conflicts import classify
    try:
        with mind.engine.db.connect() as conn:
            if not installed(conn) or not enabled(conn, mind.scope.key(), FLAG):
                return {"state": "disabled"}
        report = archive(mind, apply=True, sample=0, automatic=True)
    except Exception as error:  # noqa: BLE001 - the review goes on; the next commit tries again
        found = classify(error)
        return {"state": "failed", "error": type(error).__name__, **({"code": found.code} if found.code else {})}
    state = report["state"]
    if state == "archived" and not report.get("moved_count"):
        state = "idle"
    return {"state": state, **{key: report.get(key) for key in (
        "moved_count", "let_go_count", "kept_count", "holding_count", "memory_queued", "revision", "event_id",
        "days", "keep") if report.get(key) is not None}}


def restore(mind, *, apply=False, ids=None, sample=SAMPLE):
    """Put archived wishes back into the state document, finished or let go.

    Never gated by the flag: this is what runs before a rollback, and a release that cannot see the
    archive must find every wish where it has always looked. With no identifiers it restores all of
    them. The wishes come back exactly as they were stored. Their memory entries are no longer offered
    as memories of an archived wish, and one not yet written is not written."""
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
        # Two copies of one wish is the one state this module may never produce.
        raise Conflict("A wish cannot be restored over the live one that already carries its id",
                       kind="runtime", code="desire-archive-live", target=live[0])
    report = {"scope": scope, "archived": len(held), "unknown": unknown[:sample],
              "unknown_count": len(unknown)}
    if not found and unknown:
        raise Missing("No archived wish carries that id", kind="runtime", target=unknown[0])
    if not apply or not found:
        # Nothing archived and an apply is the idempotent answer, not a failure.
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
            # The wish is in the document again before its row goes, in the one transaction.
            conn.execute("DELETE FROM mind_desire_archive WHERE scope=? AND id=?", (scope, identifier))
            back.append(identifier)
        current["desires"] = {k: current["desires"][k] for k in sorted(current["desires"])}
        archive_memory.restored(mind, conn, MEMORY_KIND, back)
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


# --- the archive as a memory kind -------------------------------------------------------------------

MEMORY_INSTRUCTION = ("愿望（kind=desire）：写你当时想要什么、为什么想要、最后怎么样了——完成了（completed）、"
                      "放弃了（abandoned），还是没做完就放下了（let_go_unfinished）——以及大约是什么时候。"
                      "wish_kind 是愿望的种类：contact 联系、explore 探索、create 创作。")


def _load(mind, conn, identifier):
    """The full archived wish, for a read: every field as the document held it, its evidence as the
    store ids it names (the copies of the sources' metadata those references carry are left out)."""
    row = conn.execute("SELECT status,archived_at,archived_revision,data FROM mind_desire_archive WHERE scope=? AND id=?",
                       (mind.scope.key(), identifier)).fetchone()
    if not row:
        return None
    desire = json.loads(row["data"])
    desire["evidence"] = [{k: ref[k] for k in ("source_id", "record_id", "revision", "namespace", "occurred_at", "erased")
                           if k in ref} for ref in desire.get("evidence") or [] if isinstance(ref, dict)]
    return {"outcome": outcome(row["status"]), "archived_at": row["archived_at"],
            "archived_revision": row["archived_revision"], "record": desire}


def _backfill(mind, conn):
    state = mind._load(conn)
    return [memory_item(json.loads(row[0]), state) for row in conn.execute(
        "SELECT data FROM mind_desire_archive WHERE scope=? ORDER BY id", (mind.scope.key(),))]


archive_memory.register(MEMORY_KIND, label="愿望", instruction=MEMORY_INSTRUCTION, loader=_load, backfill=_backfill)
