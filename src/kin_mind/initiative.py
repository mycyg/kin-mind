"""What Kin is shown of her own initiative, and what an idle review decided about contacting.

From 2026-09-24 Kin made almost no wishes: every idle review re-rated two drives, nobody asked it
whether to reach out, and nothing it was shown said how long it had been quiet. So a long silence
was invisible to the one who could end it.

**Facts, never a gate.** `facts` counts from the records what an assessment could not see for
itself: how long since a wish of each kind was made, since a contact was sent, since an exploration
and a creation ran; how many idle reviews went by since the last wish; which explorations failed
lately; which contact wishes have waited a day unsent; which sealed letters and diaries opened in
the last day (`sealed_opened`, with `sealed_entries` on). Hours and counts only, rounded, in the
host's words. No score, no threshold and nothing that asks for a contact: what to do about a
silence stays Kin's judgment.

**Anniversaries (那年今日, 2026-10-01).** With `anniversaries` on, the facts also name the shared
moments whose anniversary is today (`anniversaries_today`): a week, a hundred days, one, three or six
months, or whole years to the local day (Asia/Singapore). A moment is chosen conservatively, from
what is already recorded and nothing new: an owner chat message -- 小光's own words, explicit, role
user, a message -- that an appraisal's understanding cited when it rated what happened importance 70
or more as stated or inferred meaning (never Kin's internal thought). Every source that understanding
cited must still be current: a delete or a correction of any of it, or words an erase took out, and
it is no moment. One moment a day at most, the most important; at most three shown; the ones 小光
asked not to be brought up are left out (`not_raised`). Dates and a few words of Kin's own label,
nothing that asks for a contact: whether to mention one, and when, stays Kin's.

**The decision an idle review states.** `contact_decision` reads what one committed: a contact
wish it made or took up again, or the move that said why not now. It records; it decides nothing.
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .autonomy_schema import enabled
from .state import host_wait, timestamp

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
# Anniversaries (`anniversaries`): what makes a shared moment, and how much of it is shown.
ANNIVERSARIES = "anniversaries"
MOMENT_IMPORTANCE, MOMENT_BASES = 70, ("explicit", "inferred")
ANNIVERSARIES_SHOWN, TOPIC_CHARS = 3, 40
ZONE = "Asia/Singapore"
ERASED = "[已删除]"
# NEEDS 小光 OK: what a request whose facts carry anniversaries is told about them.
ANNIVERSARY_PROMPT = ("\nautonomy_context.initiative_facts.anniversaries_today 是今天正好满一周、一百天、一个月、三个月、半年或整年的共同时刻"
                      "（milestone 写满了多久，例如 1-week、100-days、1-month、3-months、6-months、2-years）：moment_id 是那天小光说的话，"
                      "date 是那一天，topic 是你当时给这件事起的标题。它们和其他 initiative_facts 一样只是日期上的事实，不是提醒、任务或联系的理由；"
                      "要不要提、什么时候提、怎么提，由你结合眼下的心情和最近的聊天决定，不提也完全可以。需要细节时可以用只读记忆工具读 moment_id。")


def _hours(now, value):
    return round(max(0.0, (now - timestamp(value)).total_seconds()) / 3600, 1) if value else None


def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _code(value):
    """A static code the host or an executor wrote, or nothing: never a sentence."""
    return value if isinstance(value, str) and CODE.fullmatch(value) else None


def unsent_contacts(conn, mind, state, at):
    """Contact wishes still standing a day or more after they were made, with no send behind them.
    A send of unknown outcome is being reconciled under its own id and is not one of them. One that
    waits because the host held it -- its draft failed or never started -- says so (`wait_decided_by`):
    that wait is no choice of Kin's (state.py HOST_WAIT_REASONS)."""
    now, held = timestamp(at), mind._unconfirmed_desires(conn)
    found = []
    for desire in state["desires"].values():
        if (desire.get("kind") != "contact" or desire.get("status") not in LIVE or desire.get("delivery")
                or desire["id"] in held or timestamp(desire["expires_at"]) <= now):
            continue
        hours = _hours(now, desire.get("created_at"))
        if hours is not None and hours >= UNSENT_CONTACT_HOURS:
            found.append({"desire_id": desire["id"], "status": desire["status"], "hours_since_made": hours,
                          **({"share": True} if desire.get("exploration_id") else {}),
                          **({"wait_decided_by": "host"} if desire["status"] == "waiting" and host_wait(desire.get("contact_wait")) else {})})
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
        # A letter or a sealed diary whose day came lately: the entry, never its words (sealed.py).
        from . import sealed
        opened = sealed.opened(conn, scope, at) if sealed.enabled(conn, scope) else []
        anniversaries = anniversaries_today(conn, mind, at) if enabled(conn, scope, ANNIVERSARIES) else None
    return {
        "as_of": at,
        "hours_since_last_wish": {kind: _hours(now, made.get(kind)) for kind in WISH_KINDS},
        "hours_since_last_contact_sent": _hours(now, sent[0] if sent else None),
        "last_exploration": {"state": exploration["state"], "hours_ago": _hours(now, exploration["created_at"])} if exploration else None,
        "last_creation": {"state": creation["state"], "hours_ago": _hours(now, creation["started"])} if creation and creation["started"] else None,
        "idle_reviews_since_last_wish": idle,
        "recent_failed_explorations": failed,
        "contact_wishes_unsent_a_day": unsent[:UNSENT_SHOWN],
        **({"sealed_opened": opened} if opened else {}),
        # Only with the switch on: off, the facts are what they always were, key for key.
        **({"anniversaries_today": anniversaries} if anniversaries is not None else {}),
    }


def _months_later(day, months):
    """`day` that many months later; a day the month does not have is its last (31 Jan -> 28 Feb)."""
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def milestone(moment, today):
    """How long ago `moment` (a local date) was, when `today` is one of the anniversaries kept: a week,
    a hundred days, one, three or six months, or whole years. None on every other day."""
    if today <= moment:
        return None
    days = (today - moment).days
    if days == 7:
        return "1-week"
    if days == 100:
        return "100-days"
    for months in (1, 3, 6):
        if _months_later(moment, months) == today:
            return f"{months}-month" + ("s" if months > 1 else "")
    years = today.year - moment.year
    if years >= 1 and _months_later(moment, 12 * years) == today:
        return f"{years}-year" + ("s" if years > 1 else "")
    return None


def not_raised(conn, mind, ids):
    """Of `ids` (a moment's message and the sources it was understood from), the ones 小光 asked Kin
    not to bring up on its own ("不主动提起", recall_admission's quiet marks, while
    `recall_quiet_marks` is on): an id marked itself, or a source with a record that is marked or held
    by a marked graph node. Off, or with nothing marked, nothing is left out."""
    from .recall_admission import QuietMarks, stored_settings
    if not stored_settings(conn, mind.scope)["recall_quiet_marks"]:
        return frozenset()
    quiet = QuietMarks(mind).active(conn)
    if not quiet:
        return frozenset()
    found = set()
    for identifier in ids:
        if identifier in quiet or any(row[0] in quiet for row in conn.execute(
                "SELECT e.record_id FROM evidence e JOIN records r ON r.id=e.record_id "
                "WHERE e.source_id=? AND r.deleted=0", (identifier,))):
            found.add(identifier)
    return frozenset(found)


def _owner_message(ref):
    metadata = ref.get("metadata") or {}
    return (ref.get("authority") == "explicit" and metadata.get("role") == "user"
            and metadata.get("host_event") == "message")


def moments(conn, mind):
    """The shared moments the history holds, one per local day: {moment_id, date, topic, importance,
    cited}. See the module docstring for the rule."""
    from eventmem.core.db import Conflict, Missing
    zone, best = ZoneInfo(ZONE), {}
    rows = conn.execute("SELECT id,json_extract(data,'$.request.understanding') AS understanding FROM mind_events "
                        "WHERE scope=? AND kind='affect' AND json_extract(data,'$.request.understanding.basis') IN (?,?) "
                        "AND CAST(json_extract(data,'$.request.understanding.importance') AS INTEGER)>=? ORDER BY revision",
                        (mind.scope.key(), *MOMENT_BASES, MOMENT_IMPORTANCE)).fetchall()
    for row in rows:
        try:
            understanding = json.loads(row["understanding"])
        except (TypeError, ValueError):
            continue
        cited = [i for i in understanding.get("evidence_ids") or [] if isinstance(i, str)]
        topic = understanding.get("topic")
        if not cited or not isinstance(topic, str) or ERASED in topic or ERASED in str(understanding.get("meaning")):
            continue
        try:
            refs = mind._evidence(conn, cited)
        except (Missing, Conflict):
            continue
        if not refs or not mind._fresh(conn, refs):
            continue
        anchors = sorted((r for r in refs if _owner_message(r)), key=lambda r: (r["occurred_at"], r["source_id"]))
        if not anchors:
            continue
        day = timestamp(anchors[0]["occurred_at"]).astimezone(zone).date()
        importance = understanding.get("importance")
        moment = {"moment_id": anchors[0]["source_id"], "date": day.isoformat(), "topic": topic.strip()[:TOPIC_CHARS],
                  "importance": importance, "cited": sorted({*cited, *(r["source_id"] for r in refs)})}
        if day not in best or importance > best[day]["importance"]:
            best[day] = moment
    return [best[day] for day in sorted(best)]


def anniversaries_today(conn, mind, at):
    """Today's anniversaries of shared moments, oldest moment first, at most ANNIVERSARIES_SHOWN."""
    today = timestamp(at).astimezone(ZoneInfo(ZONE)).date()
    found = []
    for moment in moments(conn, mind):
        reached = milestone(date.fromisoformat(moment["date"]), today)
        if reached and not not_raised(conn, mind, {moment["moment_id"], *moment["cited"]}):
            found.append({"moment_id": moment["moment_id"], "date": moment["date"], "milestone": reached,
                          "topic": moment["topic"]})
    return found[:ANNIVERSARIES_SHOWN]


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
