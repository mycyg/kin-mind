"""The outbox sends with no transaction open and still never reports a false cancel.

Every test injects the clock and the transport, so nothing here touches a network.
Threads meet on events: a test decides when the channel answers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from eventmem.core import Engine, SourceInput
from eventmem.core.api import create_app
from eventmem.core.contact_tasks import ContactTaskInput, ContactTasks
from eventmem.core.models import ContactPolicy, RevisionInput, ScheduleInput, Scope
from eventmem.core.scheduler import (
    CLAIM_LEASE,
    DISPATCH_LEASE,
    MAX_ATTEMPTS,
    Scheduler,
)

CHANNEL = "http://localhost:9944/callback"
START = 1_893_456_000.0  # 2030-01-01T00:00:00Z


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Channel:
    """A scripted receiver. An answer is a status code or an exception to raise, and
    200 once the script runs out. A 2xx answer repeats the delivery id, as a receiver
    that keys its effect by that id does, unless the channel is told not to."""

    def __init__(self, *answers, echo=True):
        self.answers, self.echo, self.requests = list(answers), echo, []

    def __call__(self, url, body, headers):
        self.requests.append({"url": url, "body": body, "headers": dict(headers)})
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        accepted = self.echo and 200 <= answer < 300
        reply = {"delivered": True, "id": json.loads(body)["id"]} if accepted else {}
        return httpx.Response(answer, json=reply)


class Gate(Channel):
    """A receiver that holds every request until the test lets it answer."""

    def __init__(self, *answers, echo=True):
        super().__init__(*answers, echo=echo)
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, url, body, headers):
        self.entered.set()
        assert self.release.wait(20), "the test never released the channel"
        return super().__call__(url, body, headers)


class Outbox:
    def __init__(self, root, channel=None, **policy):
        self.engine = Engine(root)
        self.clock = Clock()
        self.channel = channel or Channel()
        self.scheduler = Scheduler(
            self.engine, clock=self.clock, transport=self.channel
        )
        self.settings = {
            "enabled": True,
            "channel": CHANNEL,
            "quiet_start": 0,
            "quiet_end": 0,
            "require_confirmation": False,
            "max_per_day": 100,
            "min_interval_minutes": 0,
            "idempotent_channel": True,
        } | policy
        self.scheduler.policy(ContactPolicy(**self.settings))
        self.count = 0

    def reminder(self, text="Water the plants"):
        """One due schedule and its 'ready' delivery; nothing has been sent."""
        self.count += 1
        source = self.engine.receive(
            SourceInput(
                namespace="outbox-test", key=str(self.count), text=text, kind="reminder"
            )
        )
        record = self.engine.source(source["id"])["record_ids"][0]
        schedule = self.scheduler.schedule(
            ScheduleInput(
                command_id=f"reminder-{self.count}",
                record_id=record,
                due_at="2020-01-01T00:00:00Z",
            )
        )
        [delivery] = self.scheduler.tick(deliver=False)["created"]
        return record, schedule["id"], delivery

    def row(self, delivery):
        with self.engine.db.connect() as conn:
            found = conn.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery,)
            ).fetchone()
        return dict(found) | {"data": json.loads(found["data"])}

    def schedule(self, sid):
        with self.engine.db.connect() as conn:
            found = conn.execute(
                "SELECT * FROM schedules WHERE id=?", (sid,)
            ).fetchone()
        return dict(found) | {"data": json.loads(found["data"])}

    def verify_channel(self):
        """One accepted delivery whose answer repeated its id verifies the channel."""
        self.reminder("An earlier reminder")
        self.scheduler.deliver_one()
        assert self.contract()["verified"] == 1

    def contract(self):
        with self.engine.db.connect() as conn:
            found = conn.execute(
                "SELECT * FROM outbox_channel_contracts WHERE channel=?", (CHANNEL,)
            ).fetchone()
        return dict(found) if found else None

    def in_flight(self):
        """Run deliver_one on a worker thread and return once its request is out.
        The returned function lets the channel answer and waits for the settle."""
        errors = []

        def work():
            try:
                self.scheduler.deliver_one()
            except Exception as error:  # reported by finish()
                errors.append(error)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        assert self.channel.entered.wait(20), "the request never reached the channel"

        def finish():
            self.channel.release.set()
            thread.join(20)
            assert not thread.is_alive() and not errors, errors

        return finish


@pytest.fixture
def outbox(tmp_path):
    return Outbox(tmp_path / "memory")


def test_a_cancel_between_claim_and_dispatch_sends_nothing_and_cancels_the_row(outbox):
    _, sid, delivery = outbox.reminder()
    claim = outbox.scheduler.claim()
    assert claim["delivery_id"] == delivery
    assert outbox.row(delivery)["state"] == "ready"  # a claim is a lease, not a state
    result = outbox.scheduler.control(sid, "cancel", 2)
    assert result == {
        "id": sid,
        "revision": 3,
        "status": "canceled",
        "deliveries": [{"id": delivery, "outcome": "canceled"}],
        "reconciliation_required": False,
    }
    assert outbox.scheduler.dispatch(claim) is None
    assert outbox.channel.requests == []
    row = outbox.row(delivery)
    assert row["state"] == "canceled" and row["attempts"] == 0
    assert row["lease_until"] is None and "claim" not in row["data"]


@pytest.mark.parametrize(
    "receipt,settled",
    [(200, "sent"), (409, "canceled"), (503, "uncertain")],
)
def test_a_cancel_after_the_dispatch_commit_reports_possibly_sent(
    tmp_path, receipt, settled
):
    outbox = Outbox(tmp_path / "memory", Gate(receipt))
    _, sid, delivery = outbox.reminder()
    finish = outbox.in_flight()
    assert outbox.row(delivery)["state"] == "sending"
    result = outbox.scheduler.control(sid, "cancel", 2)
    assert result["status"] == "canceled"
    assert result["deliveries"] == [{"id": delivery, "outcome": "possibly_sent"}]
    assert result["reconciliation_required"] is True
    # The request is out: the cancel cannot know its fate and must not claim to.
    assert outbox.row(delivery)["state"] == "uncertain"
    finish()
    # The receipt decides: accepted is sent, a definite refusal is truthfully
    # canceled, and an unknown outcome stays uncertain. The schedule stays canceled.
    row = outbox.row(delivery)
    assert row["state"] == settled
    assert [a["outcome"] for a in row["data"]["attempts"]] == [
        {200: "accepted", 409: "refused", 503: "unknown"}[receipt]
    ]
    assert outbox.schedule(sid)["state"] == "canceled"
    assert len(outbox.channel.requests) == 1
    outbox.clock.advance(600)
    outbox.scheduler.tick()
    assert len(outbox.channel.requests) == 1  # nothing is ever sent again


def test_a_writer_commits_while_the_request_is_on_the_network(tmp_path):
    outbox = Outbox(tmp_path / "memory", Gate())
    _, sid, delivery = outbox.reminder()

    def write():
        conn = sqlite3.connect(outbox.engine.db.path, timeout=0.2, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO metrics(name,value,created_at,data) VALUES('writer',1,'2030-01-01T00:00:00+00:00','{}')"
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    # The probe does see a held write transaction: this is what the old send looked
    # like to every other writer for as long as the channel took to answer.
    with outbox.engine.db.connect(write=True), pytest.raises(sqlite3.OperationalError):
        write()
    finish = outbox.in_flight()
    write()
    outbox.engine.receive(
        SourceInput(namespace="outbox-test", key="meanwhile", text="Written meanwhile")
    )
    assert outbox.row(delivery)["state"] == "sending"
    finish()
    assert outbox.row(delivery)["state"] == "sent"
    assert outbox.schedule(sid)["state"] == "complete"
    with outbox.engine.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM metrics WHERE name='writer'").fetchone()[
                0
            ]
            == 1
        )


def test_a_stale_claim_is_fenced_out(outbox):
    _, _, delivery = outbox.reminder()
    stale = outbox.scheduler.claim()
    assert outbox.scheduler.claim() is None  # the lease holds the row
    outbox.clock.advance(CLAIM_LEASE + 2)
    other = Scheduler(outbox.engine, clock=outbox.clock, transport=outbox.channel)
    fresh = other.claim()
    assert fresh["delivery_id"] == delivery and fresh["id"] != stale["id"]
    assert outbox.scheduler.dispatch(stale) is None
    assert outbox.channel.requests == [] and outbox.row(delivery)["state"] == "ready"
    request = other.dispatch(fresh)
    assert request["claim_id"] == fresh["id"]
    assert outbox.scheduler.dispatch(stale) is None  # and not after the dispatch either
    assert other.settle(request, other.send(request)) == "sent"
    assert len(outbox.channel.requests) == 1 and outbox.row(delivery)["attempts"] == 1


def test_a_settle_with_another_claim_id_changes_nothing(outbox):
    outbox.verify_channel()
    _, sid, delivery = outbox.reminder()
    request = outbox.scheduler.dispatch(outbox.scheduler.claim())
    before, contract = outbox.row(delivery), outbox.contract()
    accepted = {"outcome": "accepted", "status": 200, "echoed": True}
    forged = request | {"claim_id": "0" * 32}
    assert outbox.scheduler.settle(forged, accepted) is None
    assert outbox.row(delivery) == before and before["state"] == "sending"
    assert outbox.schedule(sid)["state"] == "queued"
    assert outbox.contract() == contract  # not even the channel evidence moves
    # The same fence stops a receipt that outlived its dispatch. The lease runs out,
    # the verified channel allows a retry, and another worker sends the row again.
    outbox.clock.advance(DISPATCH_LEASE + 1)
    outbox.scheduler.tick(deliver=False)
    assert outbox.row(delivery)["state"] == "retry"
    again = outbox.scheduler.dispatch(outbox.scheduler.claim())
    assert again["claim_id"] != request["claim_id"]
    before = outbox.row(delivery)
    assert outbox.scheduler.settle(request, accepted) is None
    assert outbox.row(delivery) == before and before["state"] == "sending"
    assert outbox.scheduler.settle(again, accepted) == "sent"


def test_a_revise_after_dispatch_retries_the_same_bytes_under_the_same_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EVENTMEM_WEBHOOK_SECRET", "a-test-secret")
    outbox = Outbox(tmp_path / "memory", Channel(200, 503, 200))
    outbox.verify_channel()
    record, sid, delivery = outbox.reminder("The appointment starts at nine.")
    outbox.scheduler.deliver_one()
    assert outbox.row(delivery)["state"] == "retry"  # unknown outcome, verified channel
    outbox.engine.revise(
        record,
        RevisionInput(
            expected_revision=1,
            command_id="late-correction",
            action="correct",
            content="The appointment starts at ten.",
        ),
    )
    outbox.clock.advance(5)
    outbox.scheduler.deliver_one()
    first, second = outbox.channel.requests[1:]
    assert second["body"] == first["body"]
    assert json.loads(second["body"])["text"] == "The appointment starts at nine."
    assert second["headers"] == first["headers"]
    assert second["headers"]["Idempotency-Key"] == delivery
    assert (
        second["headers"]["X-MemoryPalace-Signature"]
        == hmac.new(b"a-test-secret", second["body"], hashlib.sha256).hexdigest()
    )
    row = outbox.row(delivery)
    assert row["state"] == "sent" and row["attempts"] == 2
    frozen = row["data"]["dispatch"]
    assert frozen["body"].encode() == first["body"]
    assert frozen["body_sha256"] == hashlib.sha256(first["body"]).hexdigest()
    assert set(json.loads(first["body"])) == {
        "id",
        "schedule_id",
        "record_id",
        "record_revision",
        "created_at",
        "text",
        "source_ids",
        "scope",
    }
    assert outbox.schedule(sid)["state"] == "complete"


def test_a_revise_before_dispatch_releases_the_claim_and_freezes_the_new_text(outbox):
    record, _, delivery = outbox.reminder("The appointment starts at nine.")
    claim = outbox.scheduler.claim()
    outbox.engine.revise(
        record,
        RevisionInput(
            expected_revision=1,
            command_id="early-correction",
            action="correct",
            content="The appointment starts at ten.",
        ),
    )
    row = outbox.row(delivery)
    assert row["state"] == "ready" and row["lease_until"] is None
    assert "claim" not in row["data"]
    assert outbox.scheduler.dispatch(claim) is None and outbox.channel.requests == []
    outbox.scheduler.deliver_one()  # no lease to wait for: the row is free at once
    [request] = outbox.channel.requests
    sent = json.loads(request["body"])
    assert sent["text"] == "The appointment starts at ten."
    assert sent["record_revision"] == 2
    assert outbox.row(delivery)["state"] == "sent"


def test_a_revise_to_inactive_while_dispatching_does_not_cancel_the_row(tmp_path):
    outbox = Outbox(tmp_path / "memory", Gate())
    record, sid, delivery = outbox.reminder()
    finish = outbox.in_flight()
    outbox.engine.revise(
        record,
        RevisionInput(expected_revision=1, command_id="retract", action="retract"),
    )
    assert outbox.row(delivery)["state"] == "uncertain"
    schedule = outbox.schedule(sid)
    assert schedule["state"] == "canceled" and schedule["data"]["generation"] == 1
    finish()
    assert outbox.row(delivery)["state"] == "sent"  # the receipt, not the revision
    assert outbox.schedule(sid)["state"] == "canceled"


def test_a_revise_to_inactive_cancels_a_row_that_was_only_claimed(outbox):
    record, sid, delivery = outbox.reminder()
    claim = outbox.scheduler.claim()
    outbox.engine.revise(
        record,
        RevisionInput(expected_revision=1, command_id="retract", action="retract"),
    )
    assert outbox.row(delivery)["state"] == "canceled"
    assert outbox.scheduler.dispatch(claim) is None and outbox.channel.requests == []
    assert outbox.schedule(sid)["state"] == "canceled"


@pytest.mark.parametrize(
    "declared,verified,expected",
    [
        (True, True, "retry"),
        (True, False, "uncertain"),  # a declaration alone is not enough
        (False, True, "uncertain"),
    ],
)
def test_an_expired_sending_lease_retries_only_over_a_verified_idempotent_channel(
    tmp_path, declared, verified, expected
):
    outbox = Outbox(
        tmp_path / "memory", Channel(echo=verified), idempotent_channel=declared
    )
    outbox.reminder("An earlier reminder")
    outbox.scheduler.deliver_one()
    assert outbox.contract()["verified"] == int(verified)
    _, sid, delivery = outbox.reminder()
    # The process dies after the dispatch commit: no receipt will ever be settled.
    assert outbox.scheduler.dispatch(outbox.scheduler.claim()) is not None
    outbox.scheduler.tick(deliver=False)
    assert outbox.row(delivery)["state"] == "sending"  # the lease still runs
    outbox.clock.advance(DISPATCH_LEASE + 1)
    outbox.scheduler.tick(deliver=False)
    row = outbox.row(delivery)
    assert row["state"] == expected and row["lease_until"] is None
    # Either way the attempt may have arrived, so a cancel cannot take it back.
    result = outbox.scheduler.control(sid, "cancel", 2)
    assert result["deliveries"] == [{"id": delivery, "outcome": "possibly_sent"}]
    assert outbox.row(delivery)["state"] == "uncertain"


def test_the_last_2xx_answer_decides_whether_the_channel_is_verified(outbox):
    assert outbox.contract() is None
    outbox.verify_channel()
    outbox.channel.echo = False
    _, _, delivery = outbox.reminder()
    outbox.scheduler.deliver_one()
    contract = outbox.contract()
    assert contract["verified"] == 0 and contract["delivery_id"] == delivery
    outbox.channel.echo = True
    outbox.channel.answers.append(503)
    _, _, unknown = outbox.reminder()
    outbox.scheduler.deliver_one()
    assert outbox.row(unknown)["state"] == "uncertain"  # no resend: not verified now
    assert outbox.contract()["delivery_id"] == delivery  # only a 2xx is evidence


@pytest.mark.parametrize(
    "answer",
    [
        503,
        httpx.ReadTimeout("no answer in time"),
        httpx.ConnectError("no connection"),
        RuntimeError("the transport failed"),
    ],
    ids=["503", "timeout", "network", "transport"],
)
@pytest.mark.parametrize("verified", [False, True], ids=["unverified", "verified"])
def test_a_5xx_a_timeout_and_a_network_error_leave_the_outcome_unknown(
    tmp_path, answer, verified
):
    outbox = Outbox(tmp_path / "memory")
    if verified:
        outbox.verify_channel()
    _, sid, delivery = outbox.reminder()
    outbox.channel.answers.append(answer)
    outbox.scheduler.deliver_one()
    row = outbox.row(delivery)
    assert row["state"] == ("retry" if verified else "uncertain")
    assert row["data"]["attempts"][-1]["outcome"] == "unknown"
    result = outbox.scheduler.control(sid, "cancel", 2)
    assert result["deliveries"] == [{"id": delivery, "outcome": "possibly_sent"}]
    assert result["reconciliation_required"] is True
    assert outbox.row(delivery)["state"] == "uncertain"
    outbox.clock.advance(600)
    outbox.scheduler.tick()
    assert len(outbox.channel.requests) == 1 + int(verified)  # never sent again


@pytest.mark.parametrize("declared", [True, False])
def test_a_4xx_is_a_definite_refusal(tmp_path, declared):
    # Nothing was sent, so another try can duplicate nothing: it needs neither the
    # declaration nor a verified channel, and a cancel may truthfully cancel the row.
    outbox = Outbox(tmp_path / "memory", Channel(409), idempotent_channel=declared)
    _, sid, delivery = outbox.reminder()
    outbox.scheduler.deliver_one()
    row = outbox.row(delivery)
    assert row["state"] == "retry" and row["attempts"] == 1
    assert row["data"]["attempts"] == [
        {
            "at": "2030-01-01T00:00:00.000000+00:00",
            "status": "retry",
            "outcome": "refused",
            "http": 409,
        }
    ]
    assert row["data"]["dispatch"]["refused_through"] == 1
    result = outbox.scheduler.control(sid, "cancel", 2)
    assert result["deliveries"] == [{"id": delivery, "outcome": "canceled"}]
    assert result["reconciliation_required"] is False
    assert outbox.row(delivery)["state"] == "canceled"


def test_one_unknown_attempt_outweighs_every_refusal(tmp_path):
    outbox = Outbox(tmp_path / "memory")
    outbox.verify_channel()
    _, sid, delivery = outbox.reminder()
    outbox.channel.answers.extend([409, 503, 409])
    for _ in range(3):
        outbox.scheduler.deliver_one()
        outbox.clock.advance(60)
    row = outbox.row(delivery)
    assert row["state"] == "retry" and row["attempts"] == 3
    assert [a["outcome"] for a in row["data"]["attempts"]] == [
        "refused",
        "unknown",
        "refused",
    ]
    result = outbox.scheduler.control(sid, "cancel", 2)
    assert result["deliveries"] == [{"id": delivery, "outcome": "possibly_sent"}]
    assert outbox.row(delivery)["state"] == "uncertain"


def test_refused_deliveries_stop_at_the_attempt_cap(tmp_path):
    outbox = Outbox(tmp_path / "memory", Channel(*[409] * (MAX_ATTEMPTS + 2)))
    _, sid, delivery = outbox.reminder()
    for _ in range(MAX_ATTEMPTS + 2):
        outbox.scheduler.deliver_one()
        outbox.clock.advance(600)
    row = outbox.row(delivery)
    assert row["state"] == "uncertain" and row["attempts"] == MAX_ATTEMPTS
    assert len(outbox.channel.requests) == MAX_ATTEMPTS
    assert [a["status"] for a in row["data"]["attempts"]] == ["retry"] * 4 + [
        "uncertain"
    ]
    assert outbox.schedule(sid)["state"] == "queued"  # as before: visible, not lost
    # Every attempt was refused, so a cancel need not raise a false alarm either.
    result = outbox.scheduler.control(sid, "cancel", 2)
    assert result["deliveries"] == [{"id": delivery, "outcome": "canceled"}]
    assert result["reconciliation_required"] is False
    assert outbox.row(delivery)["state"] == "canceled"


def test_claiming_and_dispatching_leave_the_schedule_revision_alone(outbox):
    _, sid, delivery = outbox.reminder()
    before = outbox.schedule(sid)
    assert before["revision"] == 2 and before["state"] == "queued"
    claim = outbox.scheduler.claim()
    assert claim["generation"] == 0 and claim["owner"] == outbox.scheduler.owner
    row = outbox.row(delivery)
    assert row["data"]["claim"] == {
        k: v for k, v in claim.items() if k != "delivery_id"
    }
    assert row["lease_until"] == claim["lease_until"] == START + 1 + CLAIM_LEASE
    assert outbox.schedule(sid) == before
    request = outbox.scheduler.dispatch(claim)
    row = outbox.row(delivery)
    assert row["state"] == "sending" and row["attempts"] == 1
    assert row["lease_until"] == START + DISPATCH_LEASE
    assert "claim" not in row["data"]
    assert row["data"]["dispatch"]["claim_id"] == claim["id"]
    assert row["data"]["dispatch"]["generation"] == 0
    assert outbox.schedule(sid) == before
    outbox.scheduler.settle(request, outbox.scheduler.send(request))
    after = outbox.schedule(sid)
    assert after["state"] == "complete" and after["revision"] == 3


def test_every_stop_or_restart_opens_a_new_generation(outbox):
    _, sid, delivery = outbox.reminder()
    claim = outbox.scheduler.claim()
    paused = outbox.scheduler.control(sid, "pause", 2)
    assert paused["deliveries"] == [{"id": delivery, "outcome": "canceled"}]
    assert outbox.schedule(sid)["data"]["generation"] == 1
    resumed = outbox.scheduler.control(sid, "resume", 3)
    assert resumed["deliveries"] == [] and resumed["reconciliation_required"] is False
    assert outbox.schedule(sid)["data"]["generation"] == 2
    [again] = outbox.scheduler.tick(deliver=False)["created"]
    assert again != delivery and outbox.schedule(sid)["state"] == "queued"
    # The schedule is queued again, but the old claim belongs to a closed generation.
    assert outbox.scheduler.dispatch(claim) is None and outbox.channel.requests == []
    canceled = outbox.scheduler.control(sid, "cancel", 5)
    assert canceled["deliveries"] == [{"id": again, "outcome": "canceled"}]
    assert outbox.schedule(sid)["data"]["generation"] == 3
    confirmed = outbox.scheduler.control(sid, "confirm", 6)
    assert confirmed == {"id": sid, "revision": 7, "status": "canceled"}


def test_dispatch_runs_the_policy_checks_again(outbox):
    _, _, delivery = outbox.reminder()
    claim = outbox.scheduler.claim()
    outbox.scheduler.policy(ContactPolicy(**(outbox.settings | {"enabled": False})))
    assert outbox.scheduler.dispatch(claim) is None
    row = outbox.row(delivery)
    assert row["state"] == "suggested" and row["lease_until"] is None
    with outbox.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE outbox SET state='ready' WHERE id=?", (delivery,))
    # Quiet all day: the row is put back and tried again a minute later.
    quiet = outbox.settings | {"quiet_start": 0, "quiet_end": 23}
    outbox.scheduler.policy(ContactPolicy(**quiet))
    assert outbox.scheduler.dispatch(outbox.scheduler.claim()) is None
    row = outbox.row(delivery)
    assert row["state"] == "ready" and row["available"] == START + 60
    assert row["lease_until"] is None and "claim" not in row["data"]
    assert outbox.channel.requests == [] and row["attempts"] == 0


def test_a_send_in_flight_counts_toward_the_policy_limits(tmp_path):
    outbox = Outbox(tmp_path / "memory", Gate(), min_interval_minutes=30)
    _, _, first = outbox.reminder()
    _, _, second = outbox.reminder()
    finish = outbox.in_flight()
    other = Scheduler(outbox.engine, clock=outbox.clock, transport=outbox.channel)
    assert other.dispatch(other.claim()) is None
    assert outbox.row(second)["state"] == "ready"
    assert outbox.row(second)["available"] == START + 60
    finish()
    assert outbox.row(first)["state"] == "sent" and len(outbox.channel.requests) == 1


def test_an_acknowledgment_may_arrive_before_the_receipt(tmp_path):
    outbox = Outbox(tmp_path / "memory", Gate(503))
    _, sid, delivery = outbox.reminder()
    finish = outbox.in_flight()
    assert outbox.scheduler.acknowledge(delivery) == {
        "id": delivery,
        "status": "acknowledged",
    }
    assert outbox.schedule(sid)["state"] == "complete"
    finish()
    row = outbox.row(delivery)
    assert row["state"] == "acknowledged" and row["data"]["attempts"] == []


def test_a_resend_never_moves_to_another_channel(outbox):
    outbox.verify_channel()
    _, _, delivery = outbox.reminder()
    outbox.channel.answers.append(503)
    outbox.scheduler.deliver_one()
    assert outbox.row(delivery)["state"] == "retry"
    elsewhere = "http://localhost:9955/callback"
    outbox.scheduler.policy(ContactPolicy(**(outbox.settings | {"channel": elsewhere})))
    with outbox.engine.db.connect(write=True) as conn:
        conn.execute(
            "INSERT INTO outbox_channel_contracts(channel,verified,delivery_id,checked_at) VALUES(?,1,'delivery_other','2030-01-01T00:00:00+00:00')",
            (elsewhere,),
        )
    outbox.clock.advance(5)
    outbox.scheduler.deliver_one()
    # The first channel may have delivered it; the second has never seen its id.
    assert outbox.row(delivery)["state"] == "uncertain"
    assert [r["url"] for r in outbox.channel.requests] == [CHANNEL, CHANNEL]


def test_a_row_an_older_build_tried_is_never_reported_canceled(outbox):
    # An older build settled one attempt as 'retry' and left no dispatch record.
    _, settled_sid, settled = outbox.reminder()
    # It crashed inside another send: its sweep returned the row and kept the lease.
    _, swept_sid, swept = outbox.reminder()
    with outbox.engine.db.connect(write=True) as conn:
        conn.execute(
            "UPDATE outbox SET state='retry',attempts=1 WHERE id=?", (settled,)
        )
        conn.execute(
            "UPDATE outbox SET state='retry',lease_until=? WHERE id=?",
            (START - 5, swept),
        )
    # Claiming the swept row writes a new lease. What the old one said must survive.
    outbox.clock.advance(1)
    claim = outbox.scheduler.claim()
    assert claim["delivery_id"] in (settled, swept)
    for sid, delivery in ((settled_sid, settled), (swept_sid, swept)):
        result = outbox.scheduler.control(sid, "cancel", 2)
        assert result["deliveries"] == [{"id": delivery, "outcome": "possibly_sent"}]
        assert outbox.row(delivery)["state"] == "uncertain"
    assert outbox.scheduler.dispatch(claim) is None and outbox.channel.requests == []


def test_an_older_build_may_take_a_claimed_row_without_a_second_send(outbox):
    _, _, delivery = outbox.reminder()
    claim = outbox.scheduler.claim()
    # The older deliver_one ignores leases and claims by moving the row to 'sending'.
    with outbox.engine.db.connect(write=True) as conn:
        conn.execute(
            "UPDATE outbox SET state='sending',lease_until=? WHERE id=?",
            (START + 30, delivery),
        )
    assert outbox.scheduler.dispatch(claim) is None and outbox.channel.requests == []
    assert outbox.row(delivery)["state"] == "sending"


def test_the_cancel_result_passes_through_contact_tasks_and_the_api(tmp_path):
    outbox = Outbox(tmp_path / "memory", Gate(), id="reminders")
    tasks = ContactTasks(outbox.engine, Scope(), ["reminders"])
    tasks.scheduler = outbox.scheduler
    created = tasks.create(
        ContactTaskInput(
            command_id="appointment",
            policy_id="reminders",
            title="Appointment",
            text="The appointment starts at nine.",
            basis="The user asked for an appointment reminder.",
            due_at="2020-01-01T09:00:00+08:00",
        )
    )
    [delivery] = outbox.scheduler.tick(deliver=False)["created"]
    finish = outbox.in_flight()
    app = create_app(
        engine=outbox.engine, token="test-credential", workers=False, mcp_enabled=False
    )
    headers = {"Authorization": "Bearer test-credential"}
    with TestClient(app) as client:
        listed = client.get("/v1/contact/outbox", headers=headers).json()["items"]
        assert [(item["id"], item["state"], item["phase"]) for item in listed] == [
            (delivery, "sending", "dispatching")
        ]
        [item] = tasks.list()["items"]
        assert item["deliveries"] == [{"id": delivery, "state": "sending"}]
        result = tasks.manage(created["id"], item["revision"], "cancel")
        assert result["status"] == "canceled"
        assert result["deliveries"] == [{"id": delivery, "outcome": "possibly_sent"}]
        assert result["reconciliation_required"] is True
        finish()
        [item] = tasks.list()["items"]
        assert item["state"] == "canceled"
        assert item["deliveries"] == [{"id": delivery, "state": "sent"}]
        [listed] = client.get("/v1/contact/outbox", headers=headers).json()["items"]
        assert "phase" not in listed and listed["state"] == "sent"


def test_a_restore_keeps_a_dispatched_row_uncertain_and_a_claimed_row_deliverable(
    tmp_path,
):
    from eventmem.core.transfer import backup, restore

    outbox = Outbox(tmp_path / "memory")
    _, _, dispatched = outbox.reminder()
    assert outbox.scheduler.dispatch(outbox.scheduler.claim()) is not None
    _, _, claimed = outbox.reminder()
    assert outbox.scheduler.claim()["delivery_id"] == claimed
    backup(outbox.engine, tmp_path / "backup.tar.gz")
    restore(tmp_path / "backup.tar.gz", tmp_path / "restored")
    restored = Outbox(tmp_path / "restored")
    # The request of the dispatched row may have arrived: it needs reconciliation.
    assert restored.row(dispatched)["state"] == "uncertain"
    # Nothing was sent for a claim. Its lease runs out and the row is delivered once.
    assert restored.row(claimed)["state"] == "ready"
    restored.scheduler.deliver_one()
    assert restored.channel.requests == []
    restored.clock.advance(CLAIM_LEASE + 2)
    restored.scheduler.deliver_one()
    assert restored.row(claimed)["state"] == "sent"
    assert len(restored.channel.requests) == 1


@pytest.mark.parametrize("minutes,state", [(0, "sent"), (30, "ready")])
def test_a_slightly_later_stamp_on_another_send_is_a_distance_not_a_violation(
    tmp_path, minutes, state
):
    # Workers no longer run one after another, so their stamps may interleave.
    outbox = Outbox(tmp_path / "memory", min_interval_minutes=minutes)
    _, _, first = outbox.reminder()
    _, _, second = outbox.reminder()
    outbox.scheduler.deliver_one(stamp="2030-01-01T00:00:05.000000+00:00")
    outbox.scheduler.deliver_one(stamp="2030-01-01T00:00:00.000000+00:00")
    assert outbox.row(first)["state"] == "sent"
    assert outbox.row(second)["state"] == state
