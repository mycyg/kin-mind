"""What Kin is shown of her own initiative, and what an idle review decided about contacting.

From 2026-09-24 Kin made almost no wishes: every idle review re-rated two drives, nobody asked it
whether to reach out, and nothing it was shown said how long it had been quiet. So a long silence
was invisible to the one who could end it.

**Facts, never a gate.** `facts` counts from the records what an assessment could not see for
itself: how long since a wish of each kind was made, since a contact was sent, since an exploration
and a creation ran; how many idle reviews went by since the last wish; which explorations failed
lately; which contact wishes have waited a day unsent. Hours and counts only, rounded, in the
host's words. No score, no threshold and nothing that asks for a contact: what to do about a
silence stays Kin's judgment.

**The decision an idle review states.** `contact_decision` reads what one committed: a contact
wish it made or took up again, or the move that said why not now. It records; it decides nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta

from .state import timestamp

WISH_KINDS = ("contact", "explore", "create")
# How an exploration's failure is named here: its code, never its words.
CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
# What counts as recent for a failed exploration, and how many are shown.
FAILED_EXPLORATION_DAYS, FAILED_EXPLORATIONS_SHOWN = 7, 5
FAILED_RUNS = ("failed", "timed-out")
# A contact wish unsent this long is shown here and asked about again (actions._due_reviews).
UNSENT_CONTACT_HOURS = 24
UNSENT_SHOWN = 6
LIVE = ("wanted", "waiting", "in_progress")
QUIET_MOVES = ("quiet", "rest")


def _hours(now, value):
    return round(max(0.0, (now - timestamp(value)).total_seconds()) / 3600, 1) if value else None


def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _code(value):
    """A static code the host or an executor wrote, or nothing: never a sentence."""
    return value if isinstance(value, str) and CODE.fullmatch(value) else None


def unsent_contacts(conn, mind, state, at):
    """Contact wishes still standing a day or more after they were made, with no send behind them.
    A send of unknown outcome is being reconciled under its own id and is not one of them."""
    now, held = timestamp(at), mind._unconfirmed_desires(conn)
    found = []
    for desire in state["desires"].values():
        if (desire.get("kind") != "contact" or desire.get("status") not in LIVE or desire.get("delivery")
                or desire["id"] in held or timestamp(desire["expires_at"]) <= now):
            continue
        hours = _hours(now, desire.get("created_at"))
        if hours is not None and hours >= UNSENT_CONTACT_HOURS:
            found.append({"desire_id": desire["id"], "status": desire["status"], "hours_since_made": hours,
                          **({"share": True} if desire.get("exploration_id") else {})})
    return sorted(found, key=lambda entry: (-entry["hours_since_made"], entry["desire_id"]))


def facts(mind, at=None):
    """The initiative facts of this scope as of `at` (the mind's clock by default)."""
    at = at or mind.clock()
    now, scope = timestamp(at), mind.scope.key()
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        made = {}
        wishes = [(d.get("kind"), d.get("created_at")) for d in state["desires"].values()]
        if _table(conn, "mind_desire_archive"):
            # A finished wish moved to the archive was still made when it was made.
            wishes += conn.execute("SELECT kind,json_extract(data,'$.created_at') FROM mind_desire_archive WHERE scope=?",
                                   (scope,)).fetchall()
        for kind, created in wishes:
            if kind in WISH_KINDS and created and (kind not in made or timestamp(created) > timestamp(made[kind])):
                made[kind] = created
        last_wish = max((timestamp(value) for value in made.values()), default=None)
        sent = conn.execute("SELECT json_extract(data,'$.updated_at') FROM mind_contacts WHERE scope=? AND state='accepted' "
                            "ORDER BY json_extract(data,'$.updated_at') DESC LIMIT 1", (scope,)).fetchone()
        # Idle reviews queued after the latest wish of any kind: how many quiet looks went by without one.
        idle = conn.execute("SELECT COUNT(*) FROM mind_action_events WHERE scope=? AND kind='idle-review' AND created_at>?",
                            (scope, last_wish.isoformat() if last_wish else "")).fetchone()[0]
        exploration, failed = None, []
        if _table(conn, "mind_explorations"):
            exploration = conn.execute("SELECT state,created_at FROM mind_explorations WHERE scope=? ORDER BY created_at DESC,id DESC LIMIT 1",
                                       (scope,)).fetchone()
            since = (now - timedelta(days=FAILED_EXPLORATION_DAYS)).isoformat()
            for row in conn.execute("SELECT id,state,created_at,data FROM mind_explorations WHERE scope=? AND state IN (?,?) "
                                    "AND created_at>=? ORDER BY created_at DESC,id DESC LIMIT ?",
                                    (scope, *FAILED_RUNS, since, FAILED_EXPLORATIONS_SHOWN)):
                data = json.loads(row["data"])
                code = next((c for c in map(_code, (data.get("inputs_withheld"), data.get("waiting_reason"), data.get("error"),
                                                    data.get("reason"))) if c), None)
                failed.append({"exploration_id": row["id"], "state": row["state"], "hours_ago": _hours(now, row["created_at"]),
                               "desire_id": data.get("desire_id"), **({"code": code} if code else {})})
        try:
            creation = conn.execute("SELECT state,json_extract(data,'$.started_at') AS started FROM mind_plan_runs WHERE scope=? "
                                    "AND actor='create' ORDER BY json_extract(data,'$.started_at') DESC LIMIT 1", (scope,)).fetchone()
        except sqlite3.OperationalError:
            creation = None
        unsent = unsent_contacts(conn, mind, state, at)
    return {
        "as_of": at,
        "hours_since_last_wish": {kind: _hours(now, made.get(kind)) for kind in WISH_KINDS},
        "hours_since_last_contact_sent": _hours(now, sent[0] if sent else None),
        "last_exploration": {"state": exploration["state"], "hours_ago": _hours(now, exploration["created_at"])} if exploration else None,
        "last_creation": {"state": creation["state"], "hours_ago": _hours(now, creation["started"])} if creation and creation["started"] else None,
        "idle_reviews_since_last_wish": idle,
        "recent_failed_explorations": failed,
        "contact_wishes_unsent_a_day": unsent[:UNSENT_SHOWN],
    }


def contact_decision(state, event_id, proposal):
    """What an idle review committed about contacting: a contact wish it made or took up again
    (`contact`), a move that says why not now (`not-now`), or neither (`missing`). Read from what
    this commit wrote for the wishes, and from the proposal for the move, which is recorded either
    way: in its own table when it was committed, on the queue row with the proposal when it was not."""
    wishes = sorted(d["id"] for d in state["desires"].values()
                    if d.get("event_id") == event_id and d.get("kind") == "contact" and d.get("status") == "wanted")
    move = proposal.next_move
    if wishes:
        return {"state": "contact", "desire_ids": wishes}
    if move and move.move in QUIET_MOVES and move.reason.strip():
        return {"state": "not-now", "move": move.move}
    return {"state": "missing", **({"move": move.move} if move else {})}
