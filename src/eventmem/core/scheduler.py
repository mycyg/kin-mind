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
MAX_ATTEMPTS = 5
HTTP_TIMEOUT = 5
UNSENT_KEYS = ("attempts", "claim", "dispatch")  # bookkeeping, never part of a body


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

    def _policy(self, conn, policy_id):
        row = conn.execute(
            "SELECT data FROM policies WHERE id=?", (policy_id,)
        ).fetchone()
        return ContactPolicy.model_validate_json(row[0]) if row else None

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
                "SELECT o.id,o.attempts,s.policy_id FROM outbox o LEFT JOIN schedules s ON s.id=o.schedule_id WHERE o.state='sending' AND o.lease_until<?",
                (self.clock(),),
            ).fetchall()
            for row in stale:
                policy = self._policy(conn, row["policy_id"])
                retry = (
                    policy
                    and policy.channel
                    and policy.idempotent_channel
                    and row["attempts"] < MAX_ATTEMPTS
                    and self._verified(conn, policy.channel)
                )
                conn.execute(
                    "UPDATE outbox SET state=?,lease_until=NULL WHERE id=? AND state='sending'",
                    ("retry" if retry else "uncertain", row["id"]),
                )
            for schedule in conn.execute(
                "SELECT * FROM schedules WHERE state='scheduled' AND due_at<=? ORDER BY due_at LIMIT 100",
                (stamp,),
            ).fetchall():
                row = conn.execute(
                    "SELECT data FROM policies WHERE id=?", (schedule["policy_id"],)
                ).fetchone()
                policy = ContactPolicy.model_validate_json(row[0])
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
        the channel answers. A 4xx is a definite refusal. A 5xx, a timeout or a network
        error leaves the outcome unknown."""
        try:
            response = self.transport(
                request["channel"], request["body"], request["headers"]
            )
            status = response.status_code
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
                elif resend and attempts < MAX_ATTEMPTS:
                    state = "retry"
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
                (state, dumps(data), self.clock() + min(300, 2**attempts), *key),
            )
            if state == "sent" and policy:
                self._complete_schedule(conn, schedule, policy, stamp, row["id"])
        return state

    def _complete_schedule(self, conn, schedule, policy, stamp, delivery_id):
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
