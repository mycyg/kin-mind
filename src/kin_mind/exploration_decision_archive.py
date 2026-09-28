"""Where a settled exploration decision goes when the state document no longer has to carry it.

Every exploration result Kin decided on keeps its decision in `state.exploration_decisions`, for
ever, and each one carries a full reference for everything it was written from — the result, every
page the run read, the wish's own evidence — with a copy of each source's metadata. Forty-three
decisions were 856 thousand characters of a document that is written again on every revision. The
state view shows only the twelve newest; nothing else reads the rest from the document unless a
wish, a contact or a reconsideration still names it. This moves the rest out.

**A move, never a delete.** An archived decision keeps its identifier, its revision, its evidence
references and its receipt, exactly as they were, in `mind_exploration_decision_archive`. The
document keeps a count and the revision of every decision that moved (`STATE_KEY`), which is all
a reader inside a commit transaction needs to tell a decision that moved from one never taken.
`exploration-decision-unarchive` puts them back, and because the state is stored with its keys
sorted, a decision that comes back lands where it was, byte for byte.

**What stays, and why.** Every decision still in the document is held for named reasons:
- `window`: one of the decisions the state view (`exploration_decisions.decision_view`) shows. They
  stay so the view, the appraisal projection built from it and the manifest of what was shown are
  the ones they were.
- `recent`: decided within the last `DAYS` days, and one of the `KEEP` newest such (the owner's
  rule for wishes, 2026-09-28, applied here).
- `wish-open`: a wish still in the document that is not finished links this result. Contact
  readiness and every update of that wish read the decision (`require_share`).
- `reconsider-open`: a deferred decision whose waiting wish still waits on its reconsideration
  condition.
- `share-open`: a share decision whose contact intent has not been made yet, live or archived.
- `contact-open`: a contact attempt drafting, pending or unconfirmed offers a wish that links it.
- `exploration-running`: the exploration it is about has not settled.
- `undated`: it carries no time to judge it by.

**A reader that misses in the document looks here.** A proposal that decides again on a result
whose decision moved is judged against that decision, exactly as it would have been in the
document: the same decision again changes nothing and leaves it here, a clock event still cannot
reopen it, and a reconsideration from new evidence brings it back into the document (`revive`) and
writes its next revision there. `require_share` reads it here, so a result already shared still
refuses a second contact intent. The dedupe of the appraisal's wishes and the manifest's drift
check read its revision from the document's count (`revision_of`).

**Memory.** What moves is handed, inside the transaction that moves it, to `memory_hook` as items
of kind `exploration-decision` (`_item`): what was decided and why, the exploration's topic, target
and outcome, the evidence ids, and the ids of the result's sources. `load_archived` gives back the
whole decision by its id. The result itself never lived in the document: it is the source the
exploration wrote, read by id like any other memory (`read_memory`, `source_evidence`).

**Erasure.** An erase reaches this table the way it reaches every mind table with a `data` column
(`erasure._tables` -> `erasure._plain`): an archived decision that cites erased material loses its
words and its erased references exactly as a live one does, in the same transaction as the delete,
and the document's count holds identifiers and revisions only. So the gap `desire_archive`
describes is not reopened here. What `memory_hook` stores is its own table's business.

**Switch.** `exploration_decision_archive`, off by default. With it on, `auto` runs from the review
minute: it reads the flag and counts the decisions with one query, does nothing while the view
could still show them all, surveys at most once an hour, and moves what the survey names as an
ordinary revision of its own. The dry run needs no flag and writes nothing, so it can be read
against a copy of a live store; the restore is never gated, because it is what runs before a
rollback to a release that cannot see this table.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta

from eventmem.core.db import Conflict, Missing, digest, dumps

from . import archive_memory
from .exploration_decisions import VIEW, newest
from .state import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_exploration_decision_archive(
 scope TEXT NOT NULL,id TEXT NOT NULL,decision TEXT NOT NULL,revision INTEGER NOT NULL,
 updated_at TEXT NOT NULL,archived_at TEXT NOT NULL,archived_revision INTEGER NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,id));
"""

TABLE = "mind_exploration_decision_archive"
FLAG = "exploration_decision_archive"
# The two history kinds this writes: ordinary revisions whose request names what moved.
KIND = "exploration-decision-archive"
RESTORE_KIND = "exploration-decision-unarchive"
# What the memory hook is told the items are.
MEMORY_KIND = "exploration-decision"
# The document's own record of what has moved: a count and every moved decision's revision.
# Present only once something has, so a store that never archives keeps the document it had.
STATE_KEY = "exploration_decision_archive"
# The owner's rule: the last seven days, at most ten of them.
DAYS = 7
KEEP = 10
# Unfinished wishes: still the model's to settle, expired or not.
LIVE_WISH = frozenset({"wanted", "waiting", "in_progress"})
OPEN_CONTACTS = ("drafting", "pending", "unconfirmed")
RUNNING = "running"
# How many identifiers a report names before it stops listing. Counts are always complete.
SAMPLE = 200
# The result's own summary, as the outcome a memory item describes, is cut here.
SUMMARY_CHARS = 1200

WINDOW = "window"
RECENT = "recent"
UNDATED = "undated"
WISH_OPEN = "wish-open"
RECONSIDER_OPEN = "reconsider-open"
SHARE_OPEN = "share-open"
CONTACT_OPEN = "contact-open"
EXPLORING = "exploration-running"

REASONS = {
    WINDOW: "one of the decisions the state view shows, so the view and its manifest stay as they were",
    RECENT: "decided within the days this run was given, and one of the newest it keeps",
    UNDATED: "carries no time to judge it by",
    WISH_OPEN: "a wish still in the document and not finished links this result",
    RECONSIDER_OPEN: "a deferred decision whose waiting wish still waits on its reconsideration condition",
    SHARE_OPEN: "a share decision whose contact intent has not been made yet",
    CONTACT_OPEN: "a contact attempt drafting, pending or unconfirmed offers a wish that links this result",
    EXPLORING: "the exploration it is about is still running",
}

# Where archived decisions are handed to become memory entries: called as
# `memory_hook(mind, conn, MEMORY_KIND, items)` inside the transaction that moves them, so an item
# exists exactly when its decision has moved. Nothing until the integration names one.
memory_hook = None


def installed(conn):
    """Whether this store has the archive. A reader that finds it absent answers as before."""
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone())


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# --- reading the archive -------------------------------------------------------------------------

def archived(conn, scope, identifier):
    """The decision this exploration has, if it has moved: a copy, as it was stored."""
    if not identifier or not installed(conn):
        return None
    row = conn.execute(f"SELECT data FROM {TABLE} WHERE scope=? AND id=?", (scope, identifier)).fetchone()
    return json.loads(row[0]) if row else None


def load_archived(mind, identifier):
    """The whole archived decision by its exploration id, for the memory registry; None when it is
    not archived."""
    with mind.engine.db.connect() as conn:
        return archived(conn, mind.scope.key(), identifier)


def count(conn, scope):
    if not installed(conn):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE scope=?", (scope,)).fetchone()[0]


def revision_of(state, identifier):
    """The revision of this exploration's decision wherever it now lives: the document's own, else
    the one the document records for it having moved, else None. Read from the document alone, so
    a commit transaction can ask it of the state it holds."""
    live = (state.get("exploration_decisions") or {}).get(identifier)
    if live is not None:
        return live.get("revision")
    return ((state.get(STATE_KEY) or {}).get("revisions") or {}).get(identifier)


# --- what holds a decision -----------------------------------------------------------------------

def holds(conn, mind, state, at, *, days=DAYS, keep=KEEP):
    """Every reason each decision in the document has for staying, by exploration id. A decision
    with no reasons may move. Read once per run: a handful of queries, whatever the count."""
    from .desire_archive import archived as archived_wish
    from .desire_archive import intent as archived_intent

    scope = mind.scope.key()
    decisions = state.get("exploration_decisions") or {}
    # Compared as moments rather than as text: the stamps are written at several precisions.
    floor = timestamp(at) - timedelta(days=max(0, days))
    ordered = [entry["exploration_id"] for entry in newest(state)]
    window = set(ordered[:VIEW])
    dated = [i for i in ordered if decisions[i].get("updated_at")]
    recent = set([i for i in dated if timestamp(decisions[i]["updated_at"]) > floor][:max(0, keep)])
    unfinished, intents = {}, set()
    for desire in (state.get("desires") or {}).values():
        linked = desire.get("exploration_id")
        if not linked:
            continue
        intents.add((linked, desire.get("sharing_revision")))
        if desire.get("status") in LIVE_WISH:
            unfinished.setdefault(linked, set()).add(desire["status"])
    contacted = set()
    if _table(conn, "mind_contacts"):
        for (data,) in conn.execute("SELECT data FROM mind_contacts WHERE scope=? AND state IN ("
                                    + ",".join("?" * len(OPEN_CONTACTS)) + ")", (scope, *OPEN_CONTACTS)):
            attempt = json.loads(data)
            for identifier in attempt.get("desire_ids") or [attempt.get("desire_id")]:
                desire = (state.get("desires") or {}).get(identifier) or archived_wish(conn, scope, identifier)
                if desire and desire.get("exploration_id"):
                    contacted.add(desire["exploration_id"])
    running = set()
    if _table(conn, "mind_explorations"):
        running = {row[0] for row in conn.execute(
            "SELECT id FROM mind_explorations WHERE scope=? AND state=?", (scope, RUNNING))}
    reasons = {}
    for identifier, decision in decisions.items():
        found = set()
        if identifier in window:
            found.add(WINDOW)
        if not decision.get("updated_at"):
            found.add(UNDATED)
        elif identifier in recent:
            found.add(RECENT)
        statuses = unfinished.get(identifier, set())
        if statuses:
            found.add(WISH_OPEN)
            if decision.get("decision") == "defer" and "waiting" in statuses:
                found.add(RECONSIDER_OPEN)
        if (decision.get("decision") == "share" and (identifier, decision.get("revision")) not in intents
                and not archived_intent(conn, scope, identifier, decision.get("revision"))):
            found.add(SHARE_OPEN)
        if identifier in contacted:
            found.add(CONTACT_OPEN)
        if identifier in running:
            found.add(EXPLORING)
        reasons[identifier] = found
    return reasons


def survey(conn, mind, state, at, *, days=DAYS, keep=KEEP):
    """What would move and what holds everything else. Read only; the apply decides again inside
    its own transaction and moves only what it still names."""
    reasons = holds(conn, mind, state, at, days=days, keep=keep)
    moving = sorted(identifier for identifier, found in reasons.items() if not found)
    holding = {identifier: sorted(found) for identifier, found in reasons.items() if found}
    return moving, holding


# --- what the memory is handed -------------------------------------------------------------------

def _item(conn, mind, state, decision):
    """One archived decision as a memory item: what was decided and why, what the exploration was
    and how it ended, and the ids a reader follows to the full records. No evidence metadata."""
    from .desire_archive import archived as archived_wish

    scope, identifier = mind.scope.key(), decision["exploration_id"]
    run = conn.execute("SELECT state,data FROM mind_explorations WHERE scope=? AND id=?",
                       (scope, identifier)).fetchone() if _table(conn, "mind_explorations") else None
    data = json.loads(run[1]) if run else {}
    wish = None
    if isinstance(data.get("desire_id"), str):
        wish = (state.get("desires") or {}).get(data["desire_id"]) or archived_wish(conn, scope, data["desire_id"])
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    summary = result.get("summary")
    exploration = {"id": identifier, "topic": (wish or {}).get("topic"),
                   "target": data.get("exploration_target") or (wish or {}).get("exploration_target"),
                   "state": run[0] if run else None, "partial": data.get("partial"),
                   "summary": summary[:SUMMARY_CHARS] if isinstance(summary, str) else None}
    refs = [identifier, data.get("source_id"), *(data.get("observation_ids") or [])]
    from eventmem.core.engine import root_id
    # A reconsidered decision can cite only the new thought. Its memory also copies
    # the result and the wish's topic, so retain those deletion dependencies too.
    dependencies = [ref["record_id"] for ref in [*(decision.get("evidence") or []),
                                                 *((wish or {}).get("evidence") or [])]
                    if isinstance(ref, dict) and isinstance(ref.get("record_id"), str)]
    dependencies.extend(root_id(sid) for sid in refs[1:] if isinstance(sid, str) and sid.startswith("src_"))
    return {
        "id": identifier, "revision": decision.get("revision"),
        "summary_input": {"decision": decision.get("decision"), "reason": decision.get("reason"),
                          "reconsider_when": decision.get("reconsider_when"),
                          "decided_at": decision.get("updated_at"), "exploration": exploration},
        "evidence_ids": list(dict.fromkeys(dependencies)),
        "occurred_at": decision.get("updated_at"),
        "refs": list(dict.fromkeys(i for i in refs if isinstance(i, str) and i)),
    }


def _hand_over(mind, conn, items, inject):
    hook = inject if inject is not None else memory_hook
    if hook is not None and items:
        hook(mind, conn, MEMORY_KIND, items)


# --- moving them ---------------------------------------------------------------------------------

def _store(conn, scope, state, identifier, decision, at):
    """One decision into the archive, whole. `archived_revision` is the revision this move makes: a
    mutation runs before its own revision is taken. In a document whose references are kept slim
    (evidence_refs), the archived decision's are kept the same way."""
    from .evidence_refs import marked, slimmed
    conn.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?)",
                 (scope, identifier, decision.get("decision") or "", decision.get("revision") or 0,
                  decision.get("updated_at") or "", at, state["revision"] + 1,
                  dumps(slimmed(decision) if marked(state) else decision)))


def _counted(conn, scope, state):
    """The document's record of what has moved, kept in step with the table inside the transaction
    that moved it. A document that has never archived anything does not carry the key at all."""
    rows = conn.execute(f"SELECT id,revision FROM {TABLE} WHERE scope=? ORDER BY id", (scope,)).fetchall() \
        if installed(conn) else []
    if rows:
        state[STATE_KEY] = {"count": len(rows), "revisions": {row[0]: row[1] for row in rows}}
    else:
        state.pop(STATE_KEY, None)
    return len(rows)


def revive(mind, conn, state, identifier):
    """The decision this exploration has, in the document: moved back from the archive, inside the
    caller's transaction, when it had moved. For a writer that is about to rest something on it —
    a reconsideration writing its next revision, a wish taking its revision — so the one decision
    is never in two places. The document's own dict is changed in place, in sorted order, because
    its caller may be holding it."""
    decisions = state.setdefault("exploration_decisions", {})
    if identifier in decisions:
        return decisions[identifier]
    scope = mind.scope.key()
    found = archived(conn, scope, identifier)
    if found is None:
        return None
    decisions[identifier] = found
    for key in sorted(decisions):
        decisions[key] = decisions.pop(key)
    conn.execute(f"DELETE FROM {TABLE} WHERE scope=? AND id=?", (scope, identifier))
    _counted(conn, scope, state)
    return decisions[identifier]


def _report(mind, document, moving, holding, *, sample=SAMPLE, **extra):
    counts = {}
    for found in holding.values():
        for reason in found:
            counts[reason] = counts.get(reason, 0) + 1
    return {"scope": mind.scope.key(), "flag": FLAG, "revision": document["revision"],
            "decisions": len(document.get("exploration_decisions") or {}),
            "would_archive": moving[:sample], "would_archive_count": len(moving),
            "holding": {k: holding[k] for k in sorted(holding)[:sample]},
            "holding_count": len(holding), "holding_reasons": counts,
            "reasons": {k: REASONS[k] for k in sorted(counts)}, **extra}


def _sizes(state, moving):
    """What the document is, and what it would be without what moves, in stored characters."""
    before = len(dumps(state))
    after = dict(state, exploration_decisions={k: v for k, v in (state.get("exploration_decisions") or {}).items()
                                               if k not in set(moving)})
    revisions = {**((state.get(STATE_KEY) or {}).get("revisions") or {}),
                 **{k: state["exploration_decisions"][k].get("revision") for k in moving}}
    if revisions:
        after[STATE_KEY] = {"count": len(revisions), "revisions": dict(sorted(revisions.items()))}
    return {"document_chars": before, "document_chars_after": len(dumps(after)),
            "decision_chars": len(dumps(state.get("exploration_decisions") or {})),
            "decision_chars_after": len(dumps(after["exploration_decisions"]))}


def archive(mind, *, apply=False, days=DAYS, keep=KEEP, limit=None, sample=SAMPLE, inject=None, measure=True):
    """Move every exploration decision nothing holds any more into the archive.

    The dry run asks for no flag and writes nothing at all. An apply asks for the flag, decides
    again inside its own write transaction, hands what moves to the memory hook in that same
    transaction, and records the move as an ordinary revision with its own history row."""
    scope, at = mind.scope.key(), mind.clock()
    days, keep = max(0, int(days)), max(0, int(keep))
    with mind.engine.db.connect() as conn:
        from .autonomy_schema import enabled
        state = mind._load(conn)
        moving, holding = survey(conn, mind, state, at, days=days, keep=keep)
        allowed, stored = enabled(conn, scope, FLAG), count(conn, scope)
    if limit is not None:
        moving = moving[:max(0, int(limit))]
    extra = {"enabled": allowed, "days": days, "keep": keep, "archived": stored,
             **(_sizes(state, moving) if measure else {})}
    if apply and not allowed:
        raise Conflict("Exploration decision archiving is switched off for this scope",
                       kind="runtime", code="exploration-decision-archive-disabled")
    if not moving:
        # Nothing to move is the ordinary answer on a store where everything is recent or held.
        return _report(mind, state, moving, holding, sample=sample, state="archived" if apply else "dry-run",
                       **extra, **({"moved": [], "moved_count": 0, "event_id": None} if apply else {}))
    if not apply:
        return _report(mind, state, moving, holding, sample=sample, state="dry-run", **extra)

    def move(conn, current, event_id):
        # Decided again against the state this transaction holds: a wish may have taken one up since.
        again, _ = survey(conn, mind, current, mind.clock(), days=days, keep=keep)
        chosen = [identifier for identifier in moving if identifier in set(again)]
        items = [_item(conn, mind, current, current["exploration_decisions"][identifier]) for identifier in chosen]
        for identifier in chosen:
            _store(conn, scope, current, identifier, current["exploration_decisions"].pop(identifier), mind.clock())
        _counted(conn, scope, current)
        _hand_over(mind, conn, items, inject)
        return {"archived": chosen}

    # The revision is part of the command's identity: the same names from the same revision are the
    # same command, answered from the first; a later revision is a new one.
    payload = {"command_id": KIND + ":" + digest([moving, state["revision"]])[:32],
               "agent_version": state["agent_version"], "expected_revision": state["revision"],
               "exploration_ids": moving}
    result = mind._mutate(payload, KIND, move)
    with mind.engine.db.connect() as conn:
        current = mind._load(conn)
        remaining, holding = survey(conn, mind, current, mind.clock(), days=days, keep=keep)
        return _report(mind, current, remaining, holding, sample=sample, state="archived",
                       enabled=True, days=days, keep=keep, archived=count(conn, scope),
                       **({"document_chars": len(dumps(current))} if measure else {}),
                       moved=result["archived"][:sample], moved_count=len(result["archived"]),
                       event_id=result["event_id"])


def restore(mind, *, apply=False, ids=None, sample=SAMPLE):
    """Put archived decisions back into the state document, all of them when no ids are named.

    Never gated by the flag: this is what runs before a rollback. A decision comes back exactly as
    it was stored, and the document sorts its keys, so a full round trip gives back the document it
    had apart from its revision and time."""
    scope = mind.scope.key()
    ids = [ids] if isinstance(ids, str) else ids
    wanted = sorted(dict.fromkeys(ids)) if ids else None
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        rows = conn.execute(f"SELECT id FROM {TABLE} WHERE scope=? ORDER BY id", (scope,)).fetchall() \
            if installed(conn) else []
    held = [row[0] for row in rows]
    unknown = sorted(set(wanted) - set(held)) if wanted else []
    found = [identifier for identifier in (wanted or held) if identifier in set(held)]
    live = [identifier for identifier in found if identifier in (state.get("exploration_decisions") or {})]
    if live:
        # Two copies of one decision is the state this module may never produce.
        raise Conflict("An exploration decision cannot be restored over the live one that carries its id",
                       kind="runtime", code="exploration-decision-archive-live", target=live[0])
    report = {"scope": scope, "archived": len(held), "unknown": unknown[:sample], "unknown_count": len(unknown)}
    if not found and unknown:
        raise Missing("No archived exploration decision carries that id", kind="runtime", target=unknown[0])
    if not apply or not found:
        # Nothing left to put back is the idempotent answer of a second restore, not a failure.
        return {**report, "state": "restored" if apply else "dry-run", "would_restore": found[:sample],
                "would_restore_count": len(found), "restored_count": 0, "revision": state["revision"]}

    def put_back(conn, current, event_id):
        back = []
        decisions = current.setdefault("exploration_decisions", {})
        for identifier in found:
            if identifier in decisions:
                continue
            row = conn.execute(f"SELECT data FROM {TABLE} WHERE scope=? AND id=?", (scope, identifier)).fetchone()
            if not row:
                continue
            decisions[identifier] = json.loads(row[0])
            # In the document before its row goes, in the one transaction.
            conn.execute(f"DELETE FROM {TABLE} WHERE scope=? AND id=?", (scope, identifier))
            back.append(identifier)
        current["exploration_decisions"] = {k: decisions[k] for k in sorted(decisions)}
        archive_memory.restored(mind, conn, MEMORY_KIND, back)
        _counted(conn, scope, current)
        return {"restored": back}

    payload = {"command_id": RESTORE_KIND + ":" + digest([found, state["revision"]])[:32],
               "agent_version": state["agent_version"], "expected_revision": state["revision"],
               "exploration_ids": found}
    result = mind._mutate(payload, RESTORE_KIND, put_back)
    with mind.engine.db.connect() as conn:
        return {**report, "state": "restored", "restored": result["restored"][:sample],
                "restored_count": len(result["restored"]), "archived": count(conn, scope),
                "revision": result["revision"], "event_id": result["event_id"]}


# --- the automatic run ---------------------------------------------------------------------------

# The hour of the last survey, by store and scope. In the resident worker's memory: a restart
# surveys once more, which is the whole cost of forgetting it.
_SURVEYED = {}


def auto(mind, *, inject=None):
    """The review minute's call. Off unless the flag is on; then one query counts the decisions and
    nothing more is done while the state view could still show every one of them. Past that, one
    survey an hour, and a move when it names something. Never raises: a run that cannot go ahead
    says why and the next minute tries again."""
    scope = mind.scope.key()
    try:
        with mind.engine.db.connect() as conn:
            if not installed(conn):
                return {"state": "absent"}
            from .autonomy_schema import enabled
            if not enabled(conn, scope, FLAG):
                return {"state": "disabled"}
            row = conn.execute("SELECT (SELECT COUNT(*) FROM json_each(data,'$.exploration_decisions'))"
                               " FROM mind_state WHERE scope=?", (scope,)).fetchone()
        live = row[0] if row else 0
        if live <= VIEW:
            return {"state": "idle", "decisions": live}
        key, hour = (str(mind.engine.db.path), scope), mind.clock()[:13]
        if _SURVEYED.get(key) == hour:
            return {"state": "idle", "decisions": live, "surveyed": hour}
        report = archive(mind, apply=True, inject=inject, measure=False, sample=0)
        _SURVEYED[key] = hour
        return {"state": "archived" if report["moved_count"] else "idle", "decisions": report["decisions"],
                "moved_count": report["moved_count"], "revision": report["revision"]}
    except Conflict as error:
        # Another writer moved the revision between the survey and the move.
        return {"state": "deferred", "code": getattr(error, "code", None)}
    except Exception as error:  # noqa: BLE001 -- the minute's other bookkeeping must go on
        print(f"kin mind: exploration decision archive did not run: {type(error).__name__}", file=sys.stderr)
        return {"state": "failed", "error": type(error).__name__}


# --- memory --------------------------------------------------------------------------------------

# What an entry of this kind says (archive_memory): the owner asked on 2026-09-28 that exploration
# decisions and results leave the document as short memories the main session can still recall,
# with the full record one read away (`read_archived_record`).
MEMORY_INSTRUCTION = ("探索决定（kind=exploration-decision）：写你对这次探索结果作了什么决定——分享给对方（share）、"
                      "先放着等条件满足再看（defer），还是自己留着不分享（keep）——为什么，这次探索的是什么、"
                      "查到了什么，以及大约是什么时候。")


def _load(mind, conn, identifier):
    """The full archived decision, for a read: every field as the document held it, its evidence as
    the store ids it names (the copies of the sources' metadata those references carry are left out)."""
    decision = archived(conn, mind.scope.key(), identifier)
    if decision is None:
        return None
    decision["evidence"] = [{k: ref[k] for k in ("source_id", "record_id", "revision", "namespace", "occurred_at", "erased")
                             if k in ref} for ref in decision.get("evidence") or [] if isinstance(ref, dict)]
    return {"outcome": decision.get("decision"), "record": decision}


def _backfill(mind, conn):
    """Every decision already archived, in `enqueue`'s shape."""
    if not installed(conn):
        return []
    state = mind._load(conn)
    return [_item(conn, mind, state, json.loads(row[0])) for row in conn.execute(
        f"SELECT data FROM {TABLE} WHERE scope=? ORDER BY id", (mind.scope.key(),))]


archive_memory.register(MEMORY_KIND, label="探索决定", instruction=MEMORY_INSTRUCTION, loader=_load, backfill=_backfill)
# Moved decisions become memory entries, queued in the transaction that moves them.
memory_hook = archive_memory.enqueue
