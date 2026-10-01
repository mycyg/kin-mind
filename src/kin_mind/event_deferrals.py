"""Deferred event routes come back (`deferred_routes`, off by default).

An append or a correction whose binding the host could not verify is committed as `defer`, and so
is a route the assessment deferred itself. Before this, the result went only into
`mind_event_routes`, which nothing reads but the route's own replay: the material was never looked
at again, and an experience that continued was left in pieces.

With the switch on, such a route also leaves a row in `mind_event_deferrals`: the target event it
named (if any), the members it would have placed with the sources each rests on, the route's
evidence, a static reason code, how often it has been looked at again and when it may be next. The
row holds ids, codes and times only -- no words.

* **Shown again.** A non-operational memory context (`MemoryContinuity.semantic_context`) carries at
  most `SHOWN` deferrals that are due, oldest first, as `pending_deferrals`: each member's current
  records as short excerpts of the experience read, and the target event among the graph
  candidates, so an append can cite its identity evidence as any other.
* **Settled.** A later route that places a member -- create, append, correct or link -- uses it
  up; a deferral with nothing left goes (`event_deferrals_settled`).
* **Let go.** Every commit of an assessment that was shown a deferral counts one look at it. After
  `RECONSIDER_LIMIT` looks without being placed it goes (`event_deferrals_dropped`); so does one
  older than `MAX_AGE_DAYS`, which nothing could show.
* **Erased.** A row that names an erased source or record goes with it, in the delete's transaction
  (`kin_mind.erasure`). It keeps no evidence references, so the slimming migration has nothing of it.

Off, nothing is written or shown, and a memory context has no `pending_deferrals` key."""
from __future__ import annotations

import json
from datetime import timedelta

from eventmem.core.db import Conflict, Missing, digest, dumps

from .state import timestamp

SWITCH = "deferred_routes"
TABLE = "mind_event_deferrals"
# How many one memory context carries, how often each is looked at again before it is let go, how
# long between two looks, and how long one that cannot be shown any more is kept.
SHOWN = 4
RECONSIDER_LIMIT = 2
RETRY_MINUTES = 60
MAX_AGE_DAYS = 14
# Per member: the records shown, and how long each excerpt may be.
MEMBER_RECORDS = 2
EXCERPT_TOKENS = 250
# A later route of one of these actions places what it names.
PLACED = frozenset({"create", "append", "correct", "link"})
# Why a route was deferred: the host could not verify an append or correction's binding, it named
# no current event, or the assessment chose to defer.
BINDING_UNVERIFIED, TARGET_MISSING, ASSESSMENT_DEFERRED = "binding-unverified", "target-missing", "assessment-deferred"


def enabled(conn, scope):
    from .lifecycle import configured
    return configured(conn, scope, SWITCH)


def _metric(conn, name, value, at, data):
    conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)", (name, value, at, dumps(data)))


def after_route(conn, scope, route, action, target, members, evidence, appraisal_id, command_id, at):
    """What a committed route does to the deferrals: a `defer` keeps its material, a route that
    places members uses up what any deferral holds of them. Inside the route's own savepoint."""
    if action == "defer":
        _record(conn, scope, route, target, members, evidence, appraisal_id, command_id, at)
    elif action in PLACED:
        _settle(conn, scope, members, at)


def _record(conn, scope, route, target, members, evidence, appraisal_id, command_id, at):
    held = {member["id"]: sorted(set(member.get("source_ids") or ())) for member in members}
    if not held:
        return None
    identifier = "defer_" + digest([scope, sorted(held)])[:32]
    reason = ASSESSMENT_DEFERRED if route.action == "defer" else BINDING_UNVERIFIED if target else TARGET_MISSING
    data = {"members": held, "evidence_ids": sorted({ref["record_id"] for ref in evidence}),
            "source_ids": sorted({ref["source_id"] for ref in evidence} | {s for sources in held.values() for s in sources}),
            "appraisal_id": appraisal_id, "command_id": command_id, "requested_action": route.action}
    # The same material deferred again keeps its count and its next look: being deferred once more
    # is what a look that placed nothing ends in.
    conn.execute(f"INSERT INTO {TABLE}(scope,id,event_id,reason,attempts,next_at,created_at,data) VALUES(?,?,?,?,0,?,?,?) "
                 "ON CONFLICT(scope,id) DO UPDATE SET event_id=excluded.event_id,reason=excluded.reason,data=excluded.data",
                 (scope, identifier, target["id"] if target else None, reason, at, at, dumps(data)))
    return identifier


def _settle(conn, scope, members, at):
    used_ids = {member["id"] for member in members}
    used_sources = {source for member in members for source in member.get("source_ids") or ()}
    settled = 0
    for row in conn.execute(f"SELECT id,data FROM {TABLE} WHERE scope=?", (scope,)).fetchall():
        data = json.loads(row["data"])
        # A member is placed by its own id, or when every source it rests on now is.
        left = {member: sources for member, sources in data["members"].items()
                if member not in used_ids and not (sources and set(sources) <= used_sources)}
        if len(left) == len(data["members"]):
            continue
        if left:
            conn.execute(f"UPDATE {TABLE} SET data=? WHERE scope=? AND id=?", (dumps({**data, "members": left}), scope, row["id"]))
        else:
            conn.execute(f"DELETE FROM {TABLE} WHERE scope=? AND id=?", (scope, row["id"]))
            settled += 1
    if settled:
        _metric(conn, "event_deferrals_settled", settled, at, {})


def pending(memory, conn, policy, query, at):
    """The deferrals due to be looked at again, oldest first, at most `SHOWN`, each with what its
    members are now as the experience read `policy` sees them. One with nothing left to show is
    passed over; `MAX_AGE_DAYS` ends it."""
    rows = conn.execute(f"SELECT id,event_id,reason,attempts,data FROM {TABLE} WHERE scope=? AND julianday(next_at)<=julianday(?) "
                        "ORDER BY julianday(created_at),id", (memory.scope.key(), at)).fetchall()
    from .adaptive_recall import evidence_excerpt
    shown = []
    for row in rows:
        if len(shown) >= SHOWN:
            break
        member_ids = list(json.loads(row["data"])["members"])
        excerpts = {}
        for member_id in member_ids:
            try:
                record_ids = memory._record_ids(conn, member_id)
            except (Missing, Conflict):
                continue
            for record_id in record_ids[:MEMBER_RECORDS]:
                if record_id in excerpts:
                    continue
                try:
                    if not memory.mind._fresh(conn, memory.mind._evidence(conn, [record_id])):
                        continue
                    record = memory.engine._get(conn, record_id)
                except (Missing, Conflict):
                    continue
                if not policy.visible(record):
                    continue
                text, partial = evidence_excerpt(record["content"], query, budget=EXCERPT_TOKENS)
                excerpts[record_id] = {"id": record_id, "revision": record["revision"], "basis": policy.basis(record),
                                       "occurred_at": record["valid_from"], "text": text, "excerpt_only": partial}
        if not excerpts:
            continue
        shown.append({"id": row["id"], "event_id": row["event_id"], "member_ids": member_ids,
                      "members": list(excerpts.values()), "reason": row["reason"], "reconsidered": row["attempts"]})
    return shown


def seen(conn, scope, identifiers, at):
    """At the commit of an assessment that was shown `identifiers`: one more look at each that is
    still deferred, and the last one lets it go. Whatever is older than `MAX_AGE_DAYS` goes too."""
    dropped = 0
    following = (timestamp(at) + timedelta(minutes=RETRY_MINUTES)).isoformat()
    for identifier in dict.fromkeys(identifiers):
        row = conn.execute(f"SELECT attempts FROM {TABLE} WHERE scope=? AND id=?", (scope, identifier)).fetchone()
        if not row:
            continue
        if row["attempts"] + 1 >= RECONSIDER_LIMIT:
            conn.execute(f"DELETE FROM {TABLE} WHERE scope=? AND id=?", (scope, identifier))
            dropped += 1
        else:
            conn.execute(f"UPDATE {TABLE} SET attempts=attempts+1,next_at=? WHERE scope=? AND id=?", (following, scope, identifier))
    cutoff = (timestamp(at) - timedelta(days=MAX_AGE_DAYS)).isoformat()
    expired = conn.execute(f"DELETE FROM {TABLE} WHERE scope=? AND julianday(created_at)<julianday(?)", (scope, cutoff)).rowcount
    if dropped or expired:
        _metric(conn, "event_deferrals_dropped", dropped + expired, at, {"reconsidered": dropped, "expired": expired})
    return dropped + expired


def forget(conn, ids, *, write=True):
    """The rows that name any of `ids` -- an erased source or record -- go whole: what a deferral
    keeps is only for placing that material, and an erase leaves nothing of it to place."""
    from .erasure import mentions
    rows = mentions(conn, TABLE, ids, "rowid AS key")
    if write:
        conn.executemany(f"DELETE FROM {TABLE} WHERE rowid=?", [(row["key"],) for row in rows])
    return len(rows)
