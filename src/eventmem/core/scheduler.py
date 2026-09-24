from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .db import Conflict, Missing, digest, dumps
from .models import ContactPolicy, ScheduleInput, Scope

# `idempotent_channel` is what a policy declares. This table is what its channel has
# shown: whether the last 2xx answer repeated the delivery id, which only a receiver
# that keys its effect by that id does. A delivery whose outcome is unknown is sent
# again on its own only when both hold.
SCHEMA = """CREATE TABLE IF NOT EXISTS outbox_channel_contracts(
 channel TEXT PRIMARY KEY, verified INTEGER NOT NULL, delivery_id TEXT NOT NULL,
 checked_at TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');"""

CLAIM_LEASE = 30  # seconds a claim holds a 'ready'/'retry' row; nothing is sent yet
DISPATCH_LEASE = 30  # seconds a 'sending' row may wait for its receipt
# How long after this occurrence was due a delivery is still worth making: 30 minutes (F4,
# CR-MEM-10). Every row is bounded by it, a row an older build queued too: its due time is found
# from its schedule, or the row is held unsent (CR2-MEM-04). Within it, a row nothing can have
# been sent for — every attempt refused, or the connection never made — is tried again however
# often that takes, and so is one on a declared and verified idempotent channel, whose receiver
# keys its effect by the delivery id. Every dispatch checks it first: past it, a row proven
# unsent is settled as not sent, and one whose outcome is unknown stays under its id for
# reconciliation, never sent late (E2-01, E3-12).
DELIVERY_WINDOW = 30 * 60
RETRY_CAP = 300  # the longest wait between two attempts
HTTP_TIMEOUT = 5
# Bookkeeping, never part of a body: the body the host receives is what it always was.
UNSENT_KEYS = ("attempts", "claim", "dispatch", "gave_up", "due_at", "hold")
# What each delivery state means to a reader that decides whether to say it was done or to
# ask again. Static text: the model reads it through the task tools (E3-13).
MEANINGS = {
    "suggested": "等待确认，尚未发送",
    "ready": "已排队，尚未发送",
    "retry": "尚未确认送达，稍后会自动重试",
    "sending": "正在发送，请求可能已在网络上",
    # A 2xx says the host took the delivery into its own queue, not that anybody received it.
    "sent": "已交给宿主，尚未确认送达",
    "acknowledged": "已确认送达",
    "uncertain": "可能已经发出，结果未知；再次发送前需要核对",
    "canceled": "没有发出",
}


def post(url, body, headers):
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        return client.post(url, content=body, headers=headers)


def echoed(response, delivery_id):
    try:
        answer = response.json()
    except Exception:
        return False
    return isinstance(answer, dict) and delivery_id in (
        answer.get("id"),
        answer.get("delivery_id"),
    )


def unsent(row, data):
    """Positive evidence that no dispatch of this row can have reached the channel.

    Whatever this build did not write itself counts as possibly sent: a 'sending'
    state, an attempt without a recorded refusal, and a lease without its claim (an
    older build had the row in 'sending', and its sweep leaves that lease behind)."""
    if row["state"] not in ("suggested", "ready", "retry", "uncertain"):
        return False
    lease = row["lease_until"]
    if lease is not None and lease != (data.get("claim") or {}).get("lease_until"):
        return False
    dispatch = data.get("dispatch")
    if not dispatch:
        return row["attempts"] == 0 and row["state"] != "uncertain"
    # Every attempt so far was refused. That also holds for a row that ran out of
    # attempts and was parked as 'uncertain' although nothing can have been sent.
    return dispatch.get("refused_through") == row["attempts"]


def withdraw(conn, where, params=()):
    """The one cancel rule, for every path that stops a schedule.

    A row nothing can have been sent for is canceled. A row that is 'sending', or was
    dispatched without a definite refusal, stops retrying and is reported as possibly
    sent, never as canceled. Its open settle still finds it by claim id and decides it
    by the receipt."""
    outcomes = []
    rows = conn.execute(
        "SELECT id,state,attempts,lease_until,data FROM outbox WHERE ("
        + where
        + ") AND state IN ('suggested','ready','retry','sending','uncertain') "
        "ORDER BY available DESC,id",
        params,
    ).fetchall()
    for row in rows:
        data = json.loads(row["data"])
        state = "canceled" if unsent(row, data) else "uncertain"
        if state != row["state"]:
            data.pop("claim", None)
            conn.execute(
                "UPDATE outbox SET state=?,lease_until=NULL,data=? WHERE id=?",
                (state, dumps(data), row["id"]),
            )
        outcomes.append(
            {
                "id": row["id"],
                "outcome": "canceled" if state == "canceled" else "possibly_sent",
            }
        )
    return outcomes


def reconciliation(outcomes):
    outcomes = sorted(outcomes, key=lambda o: o["outcome"] != "possibly_sent")
    return {
        "deliveries": outcomes[:20],
        "reconciliation_required": any(
            o["outcome"] == "possibly_sent" for o in outcomes
        ),
    }


def phase(row):
    """Derived for readers, never stored: the request of a dispatched 'sending' row is
    on the network, or was lost with its process, and no transaction is held for it."""
    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    dispatched = (data.get("dispatch") or {}).get("claim_id")
    return "dispatching" if row["state"] == "sending" and dispatched else None


def describe(row):
    """A delivery as a reader should take it: its state, its derived phase, whether anything
    can have reached the channel, and what that means in words (E3-13).

    "Never sent" is the dispatcher's own judgement, `unsent()`, so a reader is never told less
    than the dispatcher knows: an older row with attempts and no refusal on record stays
    "possibly sent" and is reconciled under its id (CR-MEM-04). A canceled row was canceled only
    when nothing could have been sent. Readers select `lease_until` with the row."""
    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    keys = row.keys()
    attempts = row["attempts"] if "attempts" in keys else 0
    never_sent = row["state"] == "canceled" or unsent(
        {"state": row["state"], "attempts": attempts,
         "lease_until": row["lease_until"] if "lease_until" in keys else None}, data)
    found = {"id": row["id"], "state": row["state"], "phase": phase(row), "attempts": attempts,
             "never_sent": bool(never_sent), "meaning": MEANINGS.get(row["state"], row["state"])}
    if data.get("gave_up"):
        found["meaning"] = ("没有发出：到截止时间仍未能发送，不再发送"
                            if data["gave_up"].get("reason") == "past-deadline"
                            else "没有发出：投递时段内渠道一直拒收或连接不上，已停止重试")
        found["gave_up_at"] = data["gave_up"].get("at")
    elif held(data) == "due-time-unknown" and row["state"] in ("suggested", "uncertain"):
        found["meaning"] = ("没有发出：确认不了这次提醒的到期时间，不会自动发送，等待核对" if never_sent
                            else "可能已经发出；确认不了这次提醒的到期时间，不会再发送，需按原编号核对")
    elif row["state"] == "uncertain" and never_sent:
        found["meaning"] = "没有发出（每次都被拒收），已停止自动重试"
    return found


def deadline_moment(data):
    """The last moment this occurrence is still worth delivering: its due time plus the window,
    in UTC, to the millisecond (CR-MEM-10). None when the row names no due time it can be read
    from: a row an older build queued names none, and its creation is not one — an overdue
    schedule is queued late — so the scheduler finds it from the schedule first, or holds the row
    (`Scheduler._due`, CR2-MEM-04).

    The one deadline there is: the dispatcher judges expiry by it, and the host is handed it as
    `deadlineAt` to check before each send of its own (CR2-INT-07). Milliseconds, because that is
    what the host's clock reads, so the two can never disagree about a moment."""
    try:
        moment = (datetime.fromisoformat(data["due_at"]) + timedelta(seconds=DELIVERY_WINDOW)).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return moment.replace(microsecond=moment.microsecond - moment.microsecond % 1000)


def deadline(data):
    """The deadline as a POSIX time, for the clock to be compared with."""
    moment = deadline_moment(data)
    return None if moment is None else moment.timestamp()


def deadline_at(data):
    """The deadline as the host receives it: ISO 8601, UTC, milliseconds and a `Z`, the form
    JavaScript's Date writes and reads without loss."""
    moment = deadline_moment(data)
    return None if moment is None else moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def window_open(data, now):
    """Whether a delivery is still inside the window in which it is worth making. Without a
    deadline it is not: nothing extends the time a reminder may be sent in (CR2-MEM-04)."""
    until = deadline(data)
    return until is not None and now < until


def held(data):
    """Why a delivery waits for someone rather than for the dispatcher, if it does."""
    return (data.get("hold") or {}).get("reason")


class Scheduler:
    def __init__(self, engine, *, clock=None, transport=None):
        self.engine = engine
        self.clock = clock or time.time
        self.transport = transport or post
        self.owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self.last_automatic = float("-inf")
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA)

    def _stamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(
            timespec="microseconds"
        )

    @staticmethod
    def _due(conn, delivery_id, schedule_id, data):
        """This occurrence's due time, written into `data` when it had to be found. A row this
        build queued carries it. One an older build queued does not, and its creation time is not
        it: an overdue schedule is queued late, and 09:00 queued at 09:25 would otherwise be sent
        until 09:55. It is the due time the row's own id was made from, which the schedule still
        holds, under the same generation, while this occurrence is its current one. Anything else
        is not confirmed and answers None: the row is held, not given a deadline of its own
        (CR2-MEM-04)."""
        if data.get("due_at"):
            return data["due_at"]
        schedule = conn.execute("SELECT due_at,data FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if schedule is None:
            return None
        generation = json.loads(schedule["data"]).get("generation", 0)
        if delivery_id != "delivery_" + digest([schedule_id, schedule["due_at"], generation])[:32]:
            return None
        data["due_at"] = schedule["due_at"]
        return data["due_at"]

    def _policy(self, conn, policy_id):
        row = conn.execute(
            "SELECT data FROM policies WHERE id=?", (policy_id,)
        ).fetchone()
        try:
            return ContactPolicy.model_validate_json(row[0]) if row else None
        except ValueError:
            # A policy row that no longer validates is no policy: its schedules pause
            # instead of failing every tick of the loop that serves all the others.
            return None

    def _verified(self, conn, channel):
        row = conn.execute(
            "SELECT verified FROM outbox_channel_contracts WHERE channel=?",
            (channel,),
        ).fetchone()
        return bool(row and row[0])

    def policy(self, policy: ContactPolicy):
        with self.engine.db.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO policies VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (policy.id, policy.model_dump_json()),
            )
        return policy.model_dump()

    def schedule(self, request: ScheduleInput):
        with self.engine.db.connect(write=True) as conn:

            def run():
                record = self.engine._get(conn, request.record_id)
                row = conn.execute(
                    "SELECT data FROM policies WHERE id=?", (request.policy_id,)
                ).fetchone()
                if row:
                    policy = ContactPolicy.model_validate_json(row[0])
                    if policy.scope.model_dump() != record["scope"]:
                        raise Conflict("Contact policy and memory scopes differ")
                else:
                    policy = ContactPolicy(
                        id=request.policy_id, scope=Scope(**record["scope"])
                    )
                    conn.execute(
                        "INSERT INTO policies VALUES(?,?)",
                        (policy.id, policy.model_dump_json()),
                    )
                sid = "schedule_" + digest(request.command_id)[:32]
                data = request.model_dump() | {"record_revision": record["revision"]}
                conn.execute(
                    "INSERT INTO schedules VALUES(?,?,?,?,?,?,?)",
                    (
                        sid,
                        request.policy_id,
                        request.record_id,
                        request.due_at,
                        "scheduled",
                        1,
                        dumps(data),
                    ),
                )
                return {
                    "id": sid,
                    "revision": 1,
                    "status": "scheduled",
                    "source_ids": record["source_ids"],
                }

            return self.engine.command(
                conn, "schedule:" + request.command_id, request.model_dump(), run
            )

    def control(self, sid, action, expected_revision, due_at=None):
        from .models import utc

        if action not in {"cancel", "pause", "resume", "snooze", "confirm"}:
            raise ValueError("Unknown schedule action")
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
            if not row:
                raise Missing(sid)
            if row["revision"] != expected_revision:
                raise Conflict("Schedule revision changed")
            data = json.loads(row["data"])
            if action == "confirm":
                data["confirmed"] = True
                conn.execute(
                    "UPDATE outbox SET state='ready' WHERE schedule_id=? AND state='suggested'",
                    (sid,),
                )
                state = row["state"]
            else:
                state = {
                    "cancel": "canceled",
                    "pause": "paused",
                    "resume": "scheduled",
                    "snooze": "scheduled",
                }[action]
                outcomes = withdraw(conn, "schedule_id=?", (sid,))
                # Every stop or restart opens a new generation, so a claim taken
                # before it can never dispatch after it.
                data["generation"] = data.get("generation", 0) + 1
            if action in {"resume", "snooze"}:
                data.pop("confirmed", None)
            if action == "snooze" and not due_at:
                raise ValueError("Snooze requires due_at")
            due = utc(due_at) if due_at else row["due_at"]
            conn.execute(
                "UPDATE schedules SET state=?,revision=revision+1,due_at=?,data=? WHERE id=?",
                (state, due, dumps(data), sid),
            )
            result = {"id": sid, "revision": expected_revision + 1, "status": state}
            return result if action == "confirm" else result | reconciliation(outcomes)

    def _eligible(self, conn, schedule, policy, stamp):
        if schedule["state"] in ("canceled", "paused", "complete"):
            return False, "schedule_inactive"
        try:
            record = self.engine._get(conn, schedule["record_id"])
        except Missing:
            return False, "memory_deleted"
        if (
            record["scope"] != policy.scope.model_dump()
            or record["status"] != "active"
            or record["attributes"].get("completed")
            or record["attributes"].get("canceled")
        ):
            return False, "memory_inactive"
        if record["valid_until"] and record["valid_until"] <= stamp:
            return False, "memory_expired"
        data = json.loads(schedule["data"])
        if (
            data["trigger"] not in policy.triggers
            or record["kind"] not in policy.allowed_kinds
        ):
            return False, "outside_policy"
        return record, None

    def automatic_greetings(self, stamp):
        # Only a user-enabled greeting policy creates autonomous suggestions.
        # Daily source and command identities survive restart and cancellation.
        if time.monotonic() - self.last_automatic < 60:
            return
        self.last_automatic = time.monotonic()
        with self.engine.db.connect() as conn:
            policies = [
                ContactPolicy.model_validate_json(r[0])
                for r in conn.execute("SELECT data FROM policies")
            ]
        from .models import SourceInput

        for policy in policies:
            if (
                not policy.enabled
                or "greeting" not in policy.triggers
                or "reminder" not in policy.allowed_kinds
            ):
                continue
            local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(policy.timezone))
            key = f"greeting:{policy.id}:{local.date().isoformat()}"
            with self.engine.db.connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM commands WHERE id=?", ("schedule:" + key,)
                ).fetchone():
                    continue
            source = self.engine.receive(
                SourceInput(
                    namespace="scheduled-greeting",
                    key=key,
                    scope=policy.scope,
                    kind="reminder",
                    title="主动问候",
                    text=policy.greeting_text,
                    authority="model",
                    metadata={"trigger": "greeting", "policy_id": policy.id},
                )
            )
            rid = self.engine.source(source["id"])["record_ids"][0]
            due = local.replace(
                hour=policy.quiet_end, minute=0, second=0, microsecond=0
            )
            self.schedule(
                ScheduleInput(
                    command_id=key,
                    policy_id=policy.id,
                    record_id=rid,
                    due_at=due.isoformat(),
                    trigger="greeting",
                )
            )

    def tick(self, *, deliver=True, stamp=None):
        stamp = stamp or self._stamp()
        self.automatic_greetings(stamp)
        created = []
        with self.engine.db.connect(write=True) as conn:
            # A send that outlived its lease has an unknown outcome. It goes out again
            # on its own only over a declared and verified idempotent channel.
            stale = conn.execute(
                "SELECT o.id,o.attempts,o.data,o.schedule_id,s.policy_id FROM outbox o LEFT JOIN schedules s ON s.id=o.schedule_id WHERE o.state='sending' AND o.lease_until<?",
                (self.clock(),),
            ).fetchall()
            for row in stale:
                policy = self._policy(conn, row["policy_id"])
                data = json.loads(row["data"])
                known = self._due(conn, row["id"], row["schedule_id"], data)
                retry = (
                    policy
                    and policy.channel
                    and policy.idempotent_channel
                    and known
                    and window_open(data, self.clock())
                    and self._verified(conn, policy.channel)
                )
                if not known:
                    data["hold"] = {"at": stamp, "reason": "due-time-unknown"}
                changed = conn.execute(
                    "UPDATE outbox SET state=?,lease_until=NULL,data=? WHERE id=? AND state='sending'",
                    ("retry" if retry else "uncertain", dumps(data), row["id"]),
                ).rowcount
                if changed and not retry and policy:
                    # Possibly sent and not to be sent again: this occurrence is over, and a
                    # recurring reminder goes on to its next one.
                    schedule = conn.execute("SELECT * FROM schedules WHERE id=?", (row["schedule_id"],)).fetchone()
                    if schedule:
                        self._complete_schedule(conn, schedule, policy, stamp, row["id"], delivered=False)
            for schedule in conn.execute(
                "SELECT * FROM schedules WHERE state='scheduled' AND due_at<=? ORDER BY due_at LIMIT 100",
                (stamp,),
            ).fetchall():
                policy = self._policy(conn, schedule["policy_id"])
                if policy is None:
                    conn.execute(
                        "UPDATE schedules SET state='paused',revision=revision+1,"
                        "data=json_set(data,'$.paused_reason','policy-unavailable') WHERE id=?",
                        (schedule["id"],),
                    )
                    continue
                record, reason = self._eligible(conn, schedule, policy, stamp)
                if not record:
                    conn.execute(
                        "UPDATE schedules SET state='canceled',revision=revision+1 WHERE id=?",
                        (schedule["id"],),
                    )
                    continue
                delivery_id = (
                    "delivery_"
                    + digest(
                        [
                            schedule["id"],
                            schedule["due_at"],
                            json.loads(schedule["data"]).get("generation", 0),
                        ]
                    )[:32]
                )
                data = {
                    "id": delivery_id,
                    "schedule_id": schedule["id"],
                    "record_id": record["id"],
                    "record_revision": record["revision"],
                    "created_at": stamp,
                    "due_at": schedule["due_at"],
                    "text": record["content"],
                    "source_ids": record["source_ids"],
                    "scope": record["scope"],
                    "attempts": [],
                }
                confirmed = json.loads(schedule["data"]).get("confirmed")
                state = (
                    "ready"
                    if policy.enabled
                    and policy.channel
                    and (not policy.require_confirmation or confirmed)
                    else "suggested"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO outbox(id,schedule_id,state,available,data) VALUES(?,?,?,?,?)",
                    (delivery_id, schedule["id"], state, self.clock(), dumps(data)),
                )
                conn.execute(
                    "UPDATE schedules SET state='queued',revision=revision+1 WHERE id=?",
                    (schedule["id"],),
                )
                created.append(delivery_id)
        if deliver:
            self.deliver_one(stamp)
        return {"created": created}

    def deliver_one(self, stamp=None):
        # Three short write transactions with the network call between the last two
        # and inside none of them. A cancel is serialized against the dispatch commit:
        # once it returns, no send can start for that schedule, and a send that had
        # already started is reported as possibly sent instead of canceled.
        stamp = stamp or self._stamp()
        claim = self.claim()
        request = claim and self.dispatch(claim, stamp)
        if request:
            self.settle(request, self.send(request), stamp)

    def claim(self):
        """Lease one due row. A claim is a token and a lease on a 'ready' or 'retry'
        row, not a state: nothing has gone on the network, so an expired claim, another
        worker or an older build may take the row over without risking a second send.
        The schedule and its revision are not touched."""
        at = self.clock()
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute(
                "SELECT o.id,o.state,o.attempts,o.lease_until,o.data,s.data AS config FROM outbox o LEFT JOIN schedules s ON s.id=o.schedule_id "
                "WHERE o.state IN ('ready','retry') AND o.available<=? AND (o.lease_until IS NULL OR o.lease_until<?) ORDER BY o.available LIMIT 1",
                (at, at),
            ).fetchone()
            if not row:
                return None
            data = json.loads(row["data"])
            if not unsent(row, data):
                # Whatever made this row possibly sent stays on it. The new lease
                # replaces an older build's lease, the only trace of its attempt.
                data["dispatch"] = (data.get("dispatch") or {}) | {
                    "refused_through": -1
                }
            claim = {
                "id": uuid.uuid4().hex,
                "owner": self.owner,
                "generation": json.loads(row["config"] or "{}").get("generation", 0),
                "at": self._stamp(),
                # Whole seconds: SQL and Python must agree on this number exactly.
                "lease_until": int(at) + 1 + CLAIM_LEASE,
            }
            conn.execute(
                "UPDATE outbox SET lease_until=?,data=? WHERE id=?",
                (claim["lease_until"], dumps(data | {"claim": claim}), row["id"]),
            )
        return claim | {"delivery_id": row["id"]}

    def dispatch(self, claim, stamp=None):
        """Turn a claim into a send, or into nothing. Every check of the old send path
        runs again, then one guarded UPDATE moves the row to 'sending' with its frozen
        body. No row updated means no request: the caller gets None."""
        stamp = stamp or self._stamp()
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE id=? AND state IN ('ready','retry') AND json_extract(data,'$.claim.id')=?",
                (claim["delivery_id"], claim["id"]),
            ).fetchone()
            if not row:
                return None  # canceled, released or taken over since the claim
            data = json.loads(row["data"])
            clean = unsent(row, data)
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE id=?", (row["schedule_id"],)
            ).fetchone()
            config = json.loads(schedule["data"]) if schedule else {}
            policy = schedule and self._policy(conn, schedule["policy_id"])
            record = None
            if (
                policy
                and schedule["state"] == "queued"
                and config.get("generation", 0) == claim["generation"]
            ):
                record, reason = self._eligible(conn, schedule, policy, stamp)
            if not record:
                withdraw(conn, "id=?", (row["id"],))
                return None
            data.pop("claim", None)

            def release(state, available=None):
                conn.execute(
                    "UPDATE outbox SET state=?,lease_until=NULL,available=?,data=? WHERE id=?",
                    (
                        state,
                        row["available"] if available is None else available,
                        dumps(data),
                        row["id"],
                    ),
                )

            if not self._due(conn, row["id"], row["schedule_id"], data):
                # No due time this row can be held to: nothing is sent and nothing is decided on
                # its behalf. Proven unsent it waits, unsent, for someone to confirm; possibly sent
                # it stays under its id to be reconciled (CR2-MEM-04).
                data["hold"] = {"at": stamp, "reason": "due-time-unknown"}
                return release("suggested" if clean else "uncertain")
            if not window_open(data, self.clock()):
                # Past this occurrence's deadline nothing is sent (CR-MEM-10). Proven unsent, it
                # is settled as not sent; possibly sent, it stays under its id to be reconciled.
                # Either way the occurrence is over, and a recurring reminder moves on.
                if clean:
                    data["gave_up"] = {"at": stamp, "reason": "past-deadline"}
                    conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES('reminder_undelivered',1,?,?)",
                                 (stamp, dumps({"attempts": row["attempts"], "reason": "past-deadline"})))
                release("canceled" if clean else "uncertain")
                self._complete_schedule(conn, schedule, policy, stamp, row["id"], delivered=False)
                return None
            if (
                not policy.enabled
                or not policy.channel
                or (policy.require_confirmation and not config.get("confirmed"))
            ):
                return release("suggested")
            local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(policy.timezone))
            hour = local.hour
            quiet = (
                policy.quiet_start <= hour < policy.quiet_end
                if policy.quiet_start < policy.quiet_end
                else hour >= policy.quiet_start or hour < policy.quiet_end
                if policy.quiet_start != policy.quiet_end
                else False
            )
            # A send in flight counts like a sent one: its request left the process
            # and the transaction that used to serialize the limits is gone.
            prior = conn.execute(
                "SELECT o.state,o.data FROM outbox o JOIN schedules s ON s.id=o.schedule_id WHERE s.policy_id=? AND (o.state IN ('sent','acknowledged') OR (o.state='sending' AND o.lease_until>=?)) ORDER BY o.available DESC LIMIT 200",
                (policy.id, self.clock()),
            ).fetchall()
            sent = []
            for other in prior:
                past = json.loads(other["data"])
                at = (
                    (past.get("dispatch") or {}).get("at")
                    if other["state"] == "sending"
                    else past.get("sent_at")
                )
                if at:
                    sent.append(datetime.fromisoformat(at))
            today_count = sum(
                t.astimezone(ZoneInfo(policy.timezone)).date() == local.date()
                for t in sent
            )
            # Workers no longer run one after another, so the latest send may carry a
            # stamp slightly after this one. Distance is what the interval measures.
            interval = (
                bool(sent)
                and abs((local - max(sent)).total_seconds())
                < policy.min_interval_minutes * 60
            )
            if quiet or today_count >= policy.max_per_day or interval:
                return release("ready", self.clock() + 60)
            dispatch = data.get("dispatch") or {}
            channel = digest(policy.channel)[:32]
            body = dispatch.get("body")
            intact = body is not None and hashlib.sha256(
                body.encode()
            ).hexdigest() == dispatch.get("body_sha256")
            if not clean and not (
                policy.idempotent_channel
                and self._verified(conn, policy.channel)
                and dispatch.get("channel") in (None, channel)
                and (body is None or intact)
            ):
                # Possibly sent before: another send needs a declared and verified
                # idempotent channel, the same channel, and the same bytes.
                return release("uncertain")
            if not intact:
                # The first dispatch freezes what the record says now. Every later one
                # repeats these bytes under the same Idempotency-Key, whatever the
                # record has become since.
                data.update(text=record["content"], record_revision=record["revision"])
                # The deadline travels with the body it bounds, under the signature: the host
                # holds a delivery it took in its own queue and sends it later, so it has to know
                # when this occurrence stops being worth sending (CR2-INT-07).
                until = deadline_at(data)
                if until:
                    data["deadlineAt"] = until
                body = dumps({k: v for k, v in data.items() if k not in UNSENT_KEYS})
            raw = body.encode()
            data["dispatch"] = dispatch | {
                "claim_id": claim["id"],
                "generation": claim["generation"],
                "at": stamp,
                "body": body,
                "body_sha256": hashlib.sha256(raw).hexdigest(),
                "channel": channel,
            }
            updated = conn.execute(
                "UPDATE outbox SET state='sending',attempts=attempts+1,lease_until=?,data=? "
                "WHERE id=? AND state IN ('ready','retry') AND json_extract(data,'$.claim.id')=? "
                "AND EXISTS(SELECT 1 FROM schedules s WHERE s.id=outbox.schedule_id AND s.state='queued' "
                "AND COALESCE(json_extract(s.data,'$.generation'),0)=?)",
                (
                    self.clock() + DISPATCH_LEASE,
                    dumps(data),
                    row["id"],
                    claim["id"],
                    claim["generation"],
                ),
            ).rowcount
            if updated != 1:
                return None
        headers = {"Content-Type": "application/json", "Idempotency-Key": row["id"]}
        secret = os.environ.get("EVENTMEM_WEBHOOK_SECRET")
        if secret:
            headers["X-MemoryPalace-Signature"] = hmac.new(
                secret.encode(), raw, hashlib.sha256
            ).hexdigest()
        return {
            "id": row["id"],
            "claim_id": claim["id"],
            "channel": policy.channel,
            "body": raw,
            "headers": headers,
        }

    def send(self, request):
        """The network call, with no transaction open: writers and cancels commit while
        the channel answers. A 4xx is a definite refusal, and so is a connection that was
        never made — the host restarting, its port closed — because no byte of the request
        left. A 5xx, a read timeout or a connection lost mid-request leaves the outcome
        unknown."""
        try:
            response = self.transport(
                request["channel"], request["body"], request["headers"]
            )
            status = response.status_code
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return {"outcome": "refused", "status": None, "echoed": False}
        except Exception:
            return {"outcome": "unknown", "status": None, "echoed": False}
        if 200 <= status < 300:
            return {
                "outcome": "accepted",
                "status": status,
                "echoed": echoed(response, request["id"]),
            }
        outcome = "refused" if 400 <= status < 500 else "unknown"
        return {"outcome": outcome, "status": status, "echoed": False}

    def settle(self, request, result, stamp=None):
        """Land a receipt on the dispatch that asked for it, and on nothing else. A row
        dispatched again, acknowledged or deleted since no longer matches the claim id
        and stays as it is. A row a cancel or the sweep parked as 'uncertain' while the
        request was out still matches, and is decided by the receipt."""
        stamp = stamp or self._stamp()
        guard = "id=? AND json_extract(data,'$.dispatch.claim_id')=? AND state IN ('sending','uncertain')"
        key = (request["id"], request["claim_id"])
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM outbox WHERE " + guard, key).fetchone()
            if not row:
                return None
            data = json.loads(row["data"])
            dispatch, attempts = data["dispatch"], row["attempts"]
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE id=?", (row["schedule_id"],)
            ).fetchone()
            policy = schedule and self._policy(conn, schedule["policy_id"])
            if result["outcome"] == "accepted":
                state = "sent"
                data["sent_at"] = stamp
                conn.execute(
                    "INSERT INTO outbox_channel_contracts(channel,verified,delivery_id,checked_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(channel) DO UPDATE SET verified=excluded.verified,delivery_id=excluded.delivery_id,checked_at=excluded.checked_at",
                    (request["channel"], int(result["echoed"]), row["id"], stamp),
                )
            else:
                if (
                    result["outcome"] == "refused"
                    and dispatch.get("refused_through", 0) == attempts - 1
                ):
                    dispatch["refused_through"] = attempts
                clean = dispatch.get("refused_through") == attempts
                wanted = (
                    schedule is not None
                    and schedule["state"] == "queued"
                    and json.loads(schedule["data"]).get("generation", 0)
                    == dispatch.get("generation")
                )
                resend = clean or (
                    policy
                    and policy.idempotent_channel
                    and self._verified(conn, request["channel"])
                )
                if not wanted:
                    state = "canceled" if clean else "uncertain"
                elif not self._due(conn, row["id"], row["schedule_id"], data):
                    data["hold"] = {"at": stamp, "reason": "due-time-unknown"}
                    state = "suggested" if clean else "uncertain"
                elif resend and window_open(data, self.clock()):
                    state = "retry"
                elif clean:
                    # Never sent, and its window has closed: given up on, and saying so,
                    # rather than parked as possibly sent.
                    state = "canceled"
                    data["gave_up"] = {"at": stamp, "reason": "undelivered-within-window"}
                else:
                    state = "uncertain"
            data.setdefault("attempts", []).append(
                {
                    "at": stamp,
                    "status": state,
                    "outcome": result["outcome"],
                    "http": result["status"],
                }
            )
            conn.execute(
                "UPDATE outbox SET state=?,data=?,lease_until=NULL,available=? WHERE "
                + guard,
                (state, dumps(data), self.clock() + min(RETRY_CAP, 2 ** min(attempts, 9)), *key),
            )
            if state == "sent" and policy:
                self._complete_schedule(conn, schedule, policy, stamp, row["id"])
            elif state in ("uncertain", "canceled") and policy and schedule is not None:
                # This occurrence is over without a delivery; a recurring reminder still goes
                # on to its next one instead of staying queued for good.
                self._complete_schedule(conn, schedule, policy, stamp, row["id"], delivered=False)
            if data.get("gave_up"):
                conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES('reminder_undelivered',1,?,?)",
                             (stamp, dumps({"attempts": attempts})))
        return state

    def _complete_schedule(self, conn, schedule, policy, stamp, delivery_id, *, delivered=True):
        """The occurrence `delivery_id` is over. Delivered, a one-time schedule is complete;
        not delivered, it stays queued with its delivery's outcome for someone to act on. A
        recurring schedule moves on to its next occurrence either way."""
        config = json.loads(schedule["data"])
        current_id = (
            "delivery_"
            + digest([schedule["id"], schedule["due_at"], config.get("generation", 0)])[
                :32
            ]
        )
        # Late acknowledgments must not advance a resumed or canceled occurrence.
        if schedule["state"] != "queued" or current_id != delivery_id:
            return
        if config["recurrence"] == "none":
            if delivered:
                conn.execute(
                    "UPDATE schedules SET state='complete',revision=revision+1 WHERE id=?",
                    (schedule["id"],),
                )
            return
        local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(policy.timezone))
        due = datetime.fromisoformat(schedule["due_at"]).astimezone(
            ZoneInfo(policy.timezone)
        )
        while due <= local:
            if config["recurrence"] == "yearly":
                due = due.replace(
                    year=due.year + 1,
                    day=min(due.day, calendar.monthrange(due.year + 1, due.month)[1]),
                )
            else:
                due += timedelta(days=1 if config["recurrence"] == "daily" else 7)
        config.pop("confirmed", None)
        conn.execute(
            "UPDATE schedules SET state='scheduled',due_at=?,data=?,revision=revision+1 WHERE id=?",
            (
                due.astimezone(timezone.utc).isoformat(timespec="microseconds"),
                dumps(config),
                schedule["id"],
            ),
        )

    def acknowledge(self, delivery_id):
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            if not row:
                raise Missing(delivery_id)
            # The request of a dispatched row is out while no transaction is held, so
            # its acknowledgment may arrive before its receipt. The late settle then
            # finds no 'sending' row and changes nothing.
            attempted = row["state"] in {"sent", "uncertain", "acknowledged"}
            if not attempted and not phase(row):
                raise Conflict("Delivery has not been attempted")
            if row["state"] == "acknowledged":
                return {"id": delivery_id, "status": "acknowledged"}
            data = json.loads(row["data"])
            data.setdefault("sent_at", self._stamp())
            conn.execute(
                "UPDATE outbox SET state='acknowledged',lease_until=NULL,data=? WHERE id=?",
                (dumps(data), delivery_id),
            )
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE id=?", (row["schedule_id"],)
            ).fetchone()
            policy = ContactPolicy.model_validate_json(
                conn.execute(
                    "SELECT data FROM policies WHERE id=?", (schedule["policy_id"],)
                ).fetchone()[0]
            )
            self._complete_schedule(conn, schedule, policy, self._stamp(), delivery_id)
        return {"id": delivery_id, "status": "acknowledged"}
