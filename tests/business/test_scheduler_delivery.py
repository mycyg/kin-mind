"""Reminder delivery when the host refuses for a while or restarts (T-11: E2-01, E3-12), what the
task list says about each delivery (E3-13), and a schedule whose policy is gone (E2-02)."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from eventmem.core import Engine
from eventmem.core.contact_tasks import ContactTasks
from eventmem.core.models import ContactPolicy, ScheduleInput, Scope, SourceInput
from eventmem.core.scheduler import DELIVERY_WINDOW, MAX_ATTEMPTS, Scheduler

SCOPE = Scope(persona="synthetic-reminders")
START = datetime(2026, 9, 23, 1, tzinfo=timezone.utc)


class Answer:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body or {}

    def json(self):
        return self.body


class Channel:
    """A host that answers from a script: an exception class, or a status code."""

    def __init__(self, *script, then=200):
        self.script, self.then, self.calls = list(script), then, []

    def __call__(self, url, body, headers):
        self.calls.append(json.loads(body)["id"])
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
    run(found, 3 * 3600)
    rows = outbox(engine, sid)
    assert rows[0]["state"] == "sent" and rows[0]["attempts"] == 8 > MAX_ATTEMPTS
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
    assert rows[0]["state"] == "canceled" and rows[0]["data"]["gave_up"]["reason"] == "undelivered-within-window"
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
    run(found, 4 * 3600)
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
