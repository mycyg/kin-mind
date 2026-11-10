"""Three dated things wake one review each (actions._due_reviews): a sealed letter that opened, a
concern whose time to come up again arrived (`timed_concerns`), a day that is an anniversary of a
shared moment (`anniversaries`). Each wakes once for what came due, and nothing while its setting is
off. And the calendar page reads the anniversaries between two days (`anniversaries`).

Synthetic replays only: an injected clock, sources through the engine, no model and no network."""
import json
from datetime import datetime, timedelta

from kin_mind import initiative, sealed
from kin_mind.actions import ActionEvents
from kin_mind.continuity import ConcernProposal
from kin_mind.host import RESIDENT_ACTIONS, dispatch
from kin_mind.memory import MemoryContinuity
from test_companion_continuity import concerns_on, create, propose
from test_initiative import world
from test_inner_life import moment
from test_model_view import config_for

pytest_plugins = ("test_kin_mind",)


def woken(mind, field):
    """The idle reviews a dated thing woke, by the field its event names."""
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT state,data FROM mind_action_events WHERE kind='idle-review' ORDER BY created_at,id").fetchall()
    return [{"state": row["state"], **json.loads(row["data"])} for row in rows if field in json.loads(row["data"])]


def test_a_sealed_letter_that_opened_wakes_one_review_that_reads_it(setup):
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"sealed_entries": True})
    clock[0] -= timedelta(days=2)
    sealed.seal_letter(mind, "给 Kin 的信：synthetic words", (sealed.today(mind.clock()) + timedelta(days=1)).isoformat(), "letter-1")
    clock[0] += timedelta(days=2)
    # Off, the letter still opens on its day, and nothing is woken for it.
    memory.configure({"sealed_entries": False})
    minute = lambda: dispatch(config_for(mind), "review-due", {})
    minute()
    minute()
    assert sealed.entries(mind)["items"][0]["state"] == "opened" and woken(mind, "sealed_kind") == []
    memory.configure({"sealed_entries": True})
    for _ in range(3):
        minute()
    [wake] = woken(mind, "sealed_kind")
    record = sealed.root_id(sealed.entries(mind)["items"][0]["source_id"])
    assert wake["sealed_kind"] == "letter" and wake["evidence_ids"] == [record] and wake["state"] != "needs-review"
    with mind.engine.db.connect() as conn:
        jobs = [json.loads(row[0]) for row in conn.execute("SELECT data FROM mind_appraisals").fetchall()]
    assert [(job["stimulus"], record in job["evidence_ids"]) for job in jobs] == [("idle-review", True)]


def test_a_concern_wakes_one_review_when_its_time_comes_and_again_for_a_new_time(setup):
    mind, source, clock = setup
    concerns_on(mind, source)
    now = datetime.fromisoformat(mind.clock())
    exam, evidence = create(mind, source, clock, "exam", surface_after=(now + timedelta(hours=1)).isoformat())
    actions = ActionEvents(mind)
    actions.crossings()
    assert woken(mind, "concern_id") == [], "before its time nothing is woken"
    MemoryContinuity(mind).configure({"timed_concerns": False})
    clock[0] += timedelta(hours=2)
    actions.crossings()
    assert woken(mind, "concern_id") == [], "off, nothing is woken"
    MemoryContinuity(mind).configure({"timed_concerns": True})
    actions.crossings()
    actions.crossings()
    [wake] = woken(mind, "concern_id")
    assert wake["concern_id"] == exam and datetime.fromisoformat(wake["surface_after"]) == now + timedelta(hours=1)
    assert wake["evidence_ids"] == {c["id"]: c for c in mind.read()["concerns"]}[exam]["evidence_ids"]
    # A later time Kin gives it is a time of its own, asked about once when it comes.
    propose(mind, "move-exam", ConcernProposal(action="update", concern_id=exam, reason="明早再问",
                                               surface_after=(now + timedelta(hours=5)).isoformat()), evidence)
    actions.crossings()
    assert len(woken(mind, "concern_id")) == 1
    clock[0] += timedelta(hours=4)
    actions.crossings()
    actions.crossings()
    assert [datetime.fromisoformat(w["surface_after"]) for w in woken(mind, "concern_id")] == [
        now + timedelta(hours=1), now + timedelta(hours=5)]


def test_an_anniversary_wakes_one_review_on_its_day(setup):
    mind, source, clock = setup
    # Away from Singapore midnight, so the minute below is on the anniversary's own day.
    clock[0] = (clock[0] + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=1)
    moment(mind, source, clock, "first-clock")
    clock[0] += timedelta(days=7)
    memory, actions = world(setup)
    actions.crossings()
    assert woken(mind, "day") == [], "off, nothing is woken"
    memory.configure({"anniversaries": True})
    actions.crossings()
    actions.crossings()
    [wake] = woken(mind, "day")
    assert wake["day"] == initiative.local_day(mind.clock()).isoformat()
    assert "not a reminder, task or reason to contact" in wake["reason"]
    clock[0] += timedelta(days=1)
    actions.crossings()
    assert len(woken(mind, "day")) == 1, "the next day is no anniversary"


def test_the_calendar_reads_the_anniversaries_between_two_days(setup):
    mind, source, clock = setup
    day = initiative.local_day(mind.clock())
    moment(mind, source, clock, "first-clock")
    assert "anniversaries" in RESIDENT_ACTIONS

    def between(start, end):
        return dispatch(config_for(mind), "anniversaries", {"from": start.isoformat(), "to": end.isoformat()})["items"]

    assert between(day, day + timedelta(days=92)) == [], "off, nothing"
    MemoryContinuity(mind).configure({"anniversaries": True})
    # At most 92 days: a hundred days later is past what one request covers.
    first = between(day + timedelta(days=1), day + timedelta(days=200))
    assert [(item["title"], item["years"]) for item in first] == [
        ("第一次一起拆钟 · 满一周", None), ("第一次一起拆钟 · 满一个月", None), ("第一次一起拆钟 · 满三个月", None)]
    assert first[0]["date"] == (day + timedelta(days=7)).isoformat()
    hundred = day + timedelta(days=100)
    assert between(hundred, hundred + timedelta(days=1)) == [{"date": hundred.isoformat(), "title": "第一次一起拆钟 · 满一百天", "years": None}]
    year = initiative._months_later(day, 12)
    assert between(year - timedelta(days=3), year + timedelta(days=3)) == [{"date": year.isoformat(), "title": "第一次一起拆钟 · 1 周年", "years": 1}]
