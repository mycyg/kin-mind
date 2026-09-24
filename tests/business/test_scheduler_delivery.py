"""Reminder delivery when the host refuses for a while or restarts (T-11: E2-01, E3-12), what the
task list says about each delivery (E3-13, CR-MEM-04), a schedule whose policy is gone (E2-02),
and the deadline of each occurrence, its due time plus 30 minutes, checked before every dispatch
(CR-MEM-10) and handed to the host, signed, as the very moment the dispatcher judges by
(CR2-INT-07); for a row an older build queued too, whose due time is found from its schedule or
the row is held (CR2-MEM-04)."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from eventmem.core import Engine
from eventmem.core.contact_tasks import ContactTasks
from eventmem.core.models import ContactPolicy, ScheduleInput, Scope, SourceInput
from eventmem.core.scheduler import DELIVERY_WINDOW, Scheduler, window_open

SCOPE = Scope(persona="synthetic-reminders")
START = datetime(2026, 9, 23, 1, tzinfo=timezone.utc)


class Answer:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body or {}

    def json(self):
        return self.body


class Channel:
    """A host that answers from a script: an exception class, or a status code."""

    def __init__(self, *script, then=200, clock=None):
        self.script, self.then, self.calls, self.times, self.clock = list(script), then, [], [], clock

    def __call__(self, url, body, headers):
        self.calls.append(json.loads(body)["id"])
        if self.clock:
            self.times.append(self.clock[0])
        step = self.script.pop(0) if self.script else self.then
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("scripted")
        return Answer(step, {"id": headers["Idempotency-Key"]} if step < 300 else {})


@pytest.fixture
def world(tmp_path):
    engine = Engine(tmp_path / "db")
    clock = [START.timestamp()]
    made = {}

    def scheduler(channel, *, idempotent=False, recurrence="daily", key="walk"):
        channel.clock = clock
        policy = ContactPolicy(id="reminders", scope=SCOPE, enabled=True, channel="http://127.0.0.1:9/deliver",
                               quiet_start=0, quiet_end=0, max_per_day=100, min_interval_minutes=0,
                               require_confirmation=False, idempotent_channel=idempotent)
        found = Scheduler(engine, clock=lambda: clock[0], transport=channel)
        found.policy(policy)
        sid = engine.receive(SourceInput(namespace="contact-tasks", key=key, scope=SCOPE, kind="reminder",
                                         title="Walk", text="Time for the evening walk", authority="explicit",
                                         extract=False))["id"]
        rid = engine.source(sid)["record_ids"][0]
        made[key] = found.schedule(ScheduleInput(command_id=key, policy_id="reminders", record_id=rid,
                                                 due_at=START.isoformat(), recurrence=recurrence))["id"]
        return found, made[key]

    def run(found, seconds, step=301):
        end = clock[0] + seconds
        while clock[0] < end:
            found.tick()
            clock[0] += step

    return engine, clock, scheduler, run


def outbox(engine, schedule_id):
    with engine.db.connect() as conn:
        return [dict(row) | {"data": json.loads(row["data"])} for row in conn.execute(
            "SELECT id,state,attempts,data FROM outbox WHERE schedule_id=? ORDER BY available", (schedule_id,))]


def schedule(engine, schedule_id):
    with engine.db.connect() as conn:
        return dict(conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())


def test_a_restarting_or_busy_host_is_retried_until_it_takes_the_reminder(world):
    engine, clock, scheduler, run = world
    channel = Channel(httpx.ConnectError, httpx.ConnectError, 409, 409, httpx.ConnectTimeout, 409, 409)
    found, sid = scheduler(channel)
    run(found, DELIVERY_WINDOW, step=60)  # the host ticks once a minute
    rows = outbox(engine, sid)
    assert rows[0]["state"] == "sent" and rows[0]["attempts"] == 8  # the window bounds it, not a count
    assert len(set(channel.calls)) == 1  # the same delivery each time, never a second one
    # The daily reminder moved on to its next occurrence.
    current = schedule(engine, sid)
    assert current["state"] == "scheduled" and current["due_at"] > START.isoformat()


def test_a_reminder_never_sent_within_its_window_is_given_up_on_and_says_so(world):
    engine, clock, scheduler, run = world
    channel = Channel(then=409)
    found, sid = scheduler(channel, recurrence="none")
    run(found, DELIVERY_WINDOW + 1800)
    rows = outbox(engine, sid)
    assert rows[0]["state"] == "canceled" and rows[0]["data"]["gave_up"]["reason"] == "past-deadline"
    # Nothing went out after this occurrence's deadline, its due time plus 30 minutes.
    assert all(at <= START.timestamp() + DELIVERY_WINDOW for at in channel.times)
    assert schedule(engine, sid)["state"] == "queued"
    calls = len(channel.calls)
    run(found, 3600)
    assert len(channel.calls) == calls  # nothing more is tried once it gave up
    tasks = ContactTasks(engine, SCOPE, ["reminders"])
    delivery = tasks.list()["items"][0]["deliveries"][0]
    assert delivery["never_sent"] and delivery["state"] == "canceled" and "没有发出" in delivery["meaning"]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name='reminder_undelivered'").fetchone()[0] == 1


def test_a_possibly_sent_reminder_is_not_sent_again_but_the_next_occurrence_comes(world):
    engine, clock, scheduler, run = world
    channel = Channel(httpx.ReadTimeout)
    found, sid = scheduler(channel)
    run(found, 1800)
    rows = outbox(engine, sid)
    assert [row["state"] for row in rows] == ["uncertain"] and len(channel.calls) == 1
    current = schedule(engine, sid)
    assert current["state"] == "scheduled" and current["due_at"] > START.isoformat()
    delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"][0]
    assert not delivery["never_sent"] and "核对" in delivery["meaning"]
    # The next day's occurrence is delivered as its own delivery.
    clock[0] = datetime.fromisoformat(current["due_at"]).timestamp()
    run(found, 600)
    assert [row["state"] for row in outbox(engine, sid)] == ["uncertain", "sent"]


def test_a_verified_idempotent_channel_is_retried_past_five_unknown_outcomes(world):
    engine, clock, scheduler, run = world
    channel = Channel(503, 503, 503, 503, 503, 503, 503)
    found, sid = scheduler(channel, idempotent=True)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO outbox_channel_contracts(channel,verified,delivery_id,checked_at) VALUES(?,1,'x','t')",
                     ("http://127.0.0.1:9/deliver",))
    run(found, DELIVERY_WINDOW, step=60)
    rows = outbox(engine, sid)
    assert rows[0]["state"] == "sent" and rows[0]["attempts"] == 8


def test_a_schedule_whose_policy_is_gone_pauses_and_the_others_still_run(world):
    engine, clock, scheduler, run = world
    found, sid = scheduler(Channel(), key="first")
    _, other = scheduler(Channel(), key="second")
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE schedules SET policy_id='gone' WHERE id=?", (sid,))
    run(found, 600)
    assert schedule(engine, sid)["state"] == "paused"
    assert json.loads(schedule(engine, sid)["data"])["paused_reason"] == "policy-unavailable"
    assert [row["state"] for row in outbox(engine, other)] == ["sent"]


def test_a_reminder_left_queued_past_its_deadline_is_never_sent_late(world):
    """A service that was down: on its return the row is past its occurrence's deadline. It is
    settled as not sent without a request, and the daily reminder moves on (CR-MEM-10)."""
    engine, clock, scheduler, run = world
    channel = Channel()
    found, sid = scheduler(channel)
    found.tick(deliver=False)
    clock[0] += DELIVERY_WINDOW + 600
    run(found, 600)
    assert channel.calls == []
    rows = outbox(engine, sid)
    assert rows[0]["state"] == "canceled" and rows[0]["data"]["gave_up"]["reason"] == "past-deadline"
    current = schedule(engine, sid)
    assert current["state"] == "scheduled" and current["due_at"] > START.isoformat()
    delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"][0]
    assert delivery["never_sent"] and "截止时间" in delivery["meaning"]


def test_an_unknown_outcome_past_the_deadline_is_kept_for_reconciliation(world):
    """Possibly sent and past the deadline: kept under its id, never sent again (CR-MEM-10)."""
    engine, clock, scheduler, run = world
    channel = Channel(then=503)
    found, sid = scheduler(channel, idempotent=True)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO outbox_channel_contracts(channel,verified,delivery_id,checked_at) VALUES(?,1,'x','t')",
                     ("http://127.0.0.1:9/deliver",))
    run(found, DELIVERY_WINDOW + 1800, step=60)
    rows = outbox(engine, sid)
    assert rows[0]["state"] == "uncertain" and "gave_up" not in rows[0]["data"]
    assert all(at <= START.timestamp() + DELIVERY_WINDOW for at in channel.times)
    assert len(set(channel.calls)) == 1  # one delivery id throughout, to reconcile by
    delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"]
    assert not next(d for d in delivery if d["id"] == rows[0]["id"])["never_sent"]


def test_an_older_rows_unknown_outcome_is_not_reported_as_never_sent(world):
    """A row an older build left 'uncertain' after attempts, with no refusal on record, may have
    reached the channel: the task list says so, as the dispatcher treats it (CR-MEM-04)."""
    engine, clock, scheduler, run = world
    found, sid = scheduler(Channel())
    found.tick(deliver=False)
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE outbox SET state='uncertain',attempts=2,data=json_remove(data,'$.dispatch') WHERE schedule_id=?",
                     (sid,))
    delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"][0]
    assert delivery["state"] == "uncertain" and not delivery["never_sent"]
    assert delivery["meaning"] == "可能已经发出，结果未知；再次发送前需要核对"


def test_the_host_is_handed_the_deadline_the_dispatcher_judges_by_under_the_signature(world, monkeypatch):
    """The host answers 202 and sends later, from its own queue: the deadline goes with the body,
    signed, as the same moment the dispatcher stops at, so a host held by a release freeze does not
    send it after the occurrence has ended (CR2-INT-07)."""
    engine, clock, scheduler, run = world
    monkeypatch.setenv("EVENTMEM_WEBHOOK_SECRET", "synthetic-secret")
    sent = []

    class Recording(Channel):
        def __call__(self, url, body, headers):
            sent.append((body, dict(headers)))
            return super().__call__(url, body, headers)

    found, sid = scheduler(Recording(409, then=202))  # refused once, then taken into the host's queue
    run(found, 180, step=60)
    assert len(sent) == 2
    expected = (START + timedelta(seconds=DELIVERY_WINDOW)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert expected == "2026-09-23T01:30:00.000Z"
    bodies = [json.loads(body) for body, _ in sent]
    assert all(body["deadlineAt"] == expected for body in bodies)
    # The retry repeats the very bytes, and the signature covers the whole body, deadline included.
    assert sent[0][0] == sent[1][0]
    for body, headers in sent:
        assert headers["X-MemoryPalace-Signature"] == hmac.new(b"synthetic-secret", body, hashlib.sha256).hexdigest()
    # One moment, not two: the dispatcher's own check closes exactly at the deadline it handed over.
    row = outbox(engine, sid)[0]
    moment = datetime.fromisoformat(expected).timestamp()
    assert window_open(row["data"], moment - 0.001) and not window_open(row["data"], moment)
    # And a 2xx reads as what it is: handed to the host, not delivered.
    assert row["state"] == "sent"
    delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"][0]
    assert delivery["meaning"] == "已交给宿主，尚未确认送达" and "已送达" not in delivery["meaning"]


def test_a_deadline_is_the_due_time_plus_the_window_in_utc_to_the_millisecond():
    from eventmem.core.scheduler import deadline, deadline_at

    local = {"due_at": "2026-09-23T09:15:30.123456+08:00"}
    assert deadline_at(local) == "2026-09-23T01:45:30.123Z"
    assert deadline(local) == datetime(2026, 9, 23, 1, 45, 30, 123000, tzinfo=timezone.utc).timestamp()
    # When it was queued is not when it was due: a row naming no due time has no deadline of its
    # own, and nothing is inside a window it cannot read (CR2-MEM-04).
    assert deadline_at({"created_at": "2026-09-23T01:00:00+00:00"}) is None
    assert deadline_at({}) is None and deadline({"due_at": "not a time"}) is None
    assert not window_open({"created_at": "2026-09-23T01:00:00+00:00"}, START.timestamp())


def older_row(engine, clock, found, sid, *, queued_after, **columns):
    """What an older build left: the occurrence due at START, queued `queued_after` seconds late
    (a service that was down, a tick that ran late), its row naming no due time of its own."""
    clock[0] = START.timestamp() + queued_after
    found.tick(deliver=False)
    sets = "".join(f",{name}=?" for name in columns)
    with engine.db.connect(write=True) as conn:
        conn.execute(f"UPDATE outbox SET data=json_remove(data,'$.due_at'){sets} WHERE schedule_id=?",
                     (*columns.values(), sid))
    row = outbox(engine, sid)[0]
    assert "due_at" not in row["data"] and row["data"]["created_at"] > START.isoformat()
    return row["id"]


def test_an_older_row_queued_late_is_held_to_its_occurrences_deadline_not_its_queueing(world):
    """Due 09:00, queued 09:25 by an older build: its deadline is 09:30, found from the schedule
    and frozen into the row, never 09:55 from when it was queued. At 09:40 nothing is sent, and the
    daily reminder moves on (CR2-MEM-04)."""
    engine, clock, scheduler, run = world
    channel = Channel()
    found, sid = scheduler(channel)
    delivery = older_row(engine, clock, found, sid, queued_after=25 * 60)
    clock[0] = START.timestamp() + 40 * 60
    found.tick()
    assert channel.calls == []
    row = outbox(engine, sid)[0]
    assert row["id"] == delivery and row["state"] == "canceled"
    assert row["data"]["gave_up"]["reason"] == "past-deadline" and datetime.fromisoformat(row["data"]["due_at"]) == START
    current = schedule(engine, sid)
    assert current["state"] == "scheduled" and current["due_at"] > START.isoformat()


def test_an_older_row_sent_in_its_window_hands_the_host_the_occurrences_deadline(world):
    """Queued ten minutes late and sent at once: the host is told 09:30, the moment the dispatcher
    judges by, and not a deadline counted from the queueing (CR2-MEM-04 with CR2-INT-07)."""
    engine, clock, scheduler, run = world
    sent = []

    class Recording(Channel):
        def __call__(self, url, body, headers):
            sent.append(json.loads(body))
            return super().__call__(url, body, headers)

    found, sid = scheduler(Recording())
    older_row(engine, clock, found, sid, queued_after=10 * 60)
    clock[0] = START.timestamp() + 12 * 60
    found.tick()
    assert [body["deadlineAt"] for body in sent] == ["2026-09-23T01:30:00.000Z"]
    assert "due_at" not in sent[0] and "hold" not in sent[0]
    row = outbox(engine, sid)[0]
    assert row["state"] == "sent" and datetime.fromisoformat(row["data"]["due_at"]) == START


def test_an_older_rows_lapsed_send_is_not_sent_again_past_its_occurrences_deadline(world):
    """An older build's send whose lease lapsed, on a verified idempotent channel: at 09:40 it is
    past the occurrence's deadline, so it stays possibly sent under its id and is not sent again,
    however late it had been queued (CR2-MEM-04)."""
    engine, clock, scheduler, run = world
    channel = Channel()
    found, sid = scheduler(channel, idempotent=True)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO outbox_channel_contracts(channel,verified,delivery_id,checked_at) VALUES(?,1,'x','t')",
                     ("http://127.0.0.1:9/deliver",))
    older_row(engine, clock, found, sid, queued_after=25 * 60, state="sending", attempts=1,
              lease_until=START.timestamp() + 26 * 60)
    clock[0] = START.timestamp() + 40 * 60
    run(found, 600, step=60)
    assert channel.calls == []
    rows = outbox(engine, sid)
    assert rows[0]["state"] == "uncertain" and datetime.fromisoformat(rows[0]["data"]["due_at"]) == START
    delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"]
    assert not next(d for d in delivery if d["id"] == rows[0]["id"])["never_sent"]
    assert schedule(engine, sid)["due_at"] > START.isoformat()  # the daily reminder moved on


def test_an_older_row_whose_due_time_cannot_be_confirmed_is_held_unsent(world):
    """The schedule no longer holds the occurrence the row was made for, so the row's due time
    cannot be confirmed. Nothing is sent and no deadline is made up for it: the row waits, unsent,
    for someone to reconcile it, and says so (CR2-MEM-04)."""
    engine, clock, scheduler, run = world
    channel = Channel()
    found, sid = scheduler(channel)
    delivery = older_row(engine, clock, found, sid, queued_after=25 * 60)
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE schedules SET due_at=? WHERE id=?", ((START + timedelta(minutes=10)).isoformat(), sid))
    clock[0] = START.timestamp() + 26 * 60
    run(found, 3600, step=60)
    assert channel.calls == []
    row = outbox(engine, sid)[0]
    assert row["id"] == delivery and row["state"] == "suggested"
    assert row["data"]["hold"]["reason"] == "due-time-unknown" and "due_at" not in row["data"]
    assert schedule(engine, sid)["state"] == "queued"  # nothing decided on the occurrence's behalf
    found_delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"][0]
    assert found_delivery["never_sent"]
    assert found_delivery["meaning"] == "没有发出：确认不了这次提醒的到期时间，不会自动发送，等待核对"
    # A confirmation does not send it either: it is held again at its next dispatch.
    current = schedule(engine, sid)
    found.control(sid, "confirm", current["revision"])
    run(found, 600, step=60)
    assert channel.calls == [] and outbox(engine, sid)[0]["state"] == "suggested"
    # Canceled, it is simply not sent, and no longer said to wait for anyone.
    outcome = found.control(sid, "cancel", schedule(engine, sid)["revision"])
    assert outcome["deliveries"] == [{"id": delivery, "outcome": "canceled"}]
    found_delivery = ContactTasks(engine, SCOPE, ["reminders"]).list()["items"][0]["deliveries"][0]
    assert found_delivery["meaning"] == "没有发出" and channel.calls == []
