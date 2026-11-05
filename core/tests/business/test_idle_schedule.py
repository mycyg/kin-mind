"""The action schedule behind the quiet review: one stored deadline, and nothing else.

A committed assessment used to rewrite the deadline every time, and the idle review was held by a
second, longer interval after its last success. A later plan review could then postpone
the saved deadline again. Here the stored
cursor is the only clock: an idle review consumes it and writes the one it chose, anything else
leaves an earlier stored deadline (an overdue one too) as it is, and an earlier time may replace a
later one.
"""
import json
from datetime import timedelta

import pytest

from kin_mind.appraisal import Appraisals
from kin_mind.memory import MemoryContinuity
from kin_mind.recipient import cursor, save_cursor
from test_initiative import RECEIPT, world
from test_main_session_review import native_provider

pytest_plugins = ('test_kin_mind',)


def at(clock, minutes):
    return (clock[0] + timedelta(minutes=minutes)).isoformat()


def read_schedule(mind):
    with mind.engine.db.connect() as conn:
        row = cursor(conn, mind.scope.key(), 'mind_action_schedule')
    return row['next_review'], json.loads(row['data'] or '{}')


def write_schedule(mind, next_at, data):
    MemoryContinuity(mind)
    with mind.engine.db.connect(write=True) as conn:
        save_cursor(conn, mind.scope.key(), 'mind_action_schedule', next_at=next_at, data=data)


def commit(mind, minutes, *, idle_review, event_id='synthetic-event'):
    with mind.engine.db.connect(write=True) as conn:
        MemoryContinuity(mind).commit_action(conn, [], event_id, minutes, RECEIPT, idle_review=idle_review)


def enqueue(mind, source, key, stimulus=None):
    job = Appraisals(mind).enqueue([source(key)], 'synthetic-v1', stimulus=stimulus)['id']
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job,))
        assert conn.execute("SELECT state FROM mind_appraisals WHERE id=?", (job,)).fetchone()['state'] == 'pending'
    return job


def provider(mind, frames):
    def exchange(request):
        frames.append(request)
        return {'state': 'complete', 'result': {'reason': '先安静一会儿', 'next_review_minutes': 360}, 'receipt': RECEIPT}
    return native_provider(mind, exchange)


# --- The memory's own scheduling ----------------------------------------------------------------

@pytest.mark.parametrize('saved', [1, 5, 10, 45, 60, 3000])
def test_a_review_is_accepted_the_moment_its_saved_minutes_have_passed(setup, saved):
    """At exactly the saved ten, forty-five or sixty minutes the review is due, and not before."""
    mind, source, clock = setup
    memory, actions = world(setup)
    write_schedule(mind, at(clock, saved), {'last_success': clock[0].isoformat(), 'minutes': saved})
    clock[0] += timedelta(minutes=saved - 1)
    assert memory.queue_idle(actions) is None, 'still before its deadline'
    clock[0] += timedelta(minutes=1)
    assert memory.queue_idle(actions)
    assert read_schedule(mind)[0] == at(clock, 0), 'queuing reads the cursor: it is where it was'


def test_a_tick_before_the_deadline_queues_nothing(setup):
    mind, source, clock = setup
    memory, actions = world(setup)
    write_schedule(mind, at(clock, 60), {'last_success': (clock[0] - timedelta(hours=40)).isoformat(), 'minutes': 60})
    clock[0] += timedelta(minutes=59)
    assert memory.queue_idle(actions) is None


def test_two_ticks_at_the_same_due_time_queue_the_same_review_once(setup):
    """Its identity is its key, kind and occurrence: a repeated tick asks for no second review."""
    mind, source, clock = setup
    memory, actions = world(setup)
    write_schedule(mind, at(clock, 10), {'last_success': clock[0].isoformat(), 'minutes': 10})
    clock[0] += timedelta(minutes=10)
    first = memory.queue_idle(actions)
    assert first and memory.queue_idle(actions) == first
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT id,data FROM mind_action_events WHERE kind='idle-review'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]['data'])['due_at'] == at(clock, 0)


def test_the_due_idle_review_consumes_the_deadline_and_writes_its_own(setup):
    mind, source, clock = setup
    write_schedule(mind, at(clock, 60), {'event_id': 'earlier', 'minutes': 60, 'receipt': {'model': 'earlier'},
                                         'last_success': (clock[0] - timedelta(hours=2)).isoformat()})
    commit(mind, 30, idle_review=True)
    next_at, data = read_schedule(mind)
    assert next_at == at(clock, 30)
    assert data['event_id'] == 'synthetic-event' and data['minutes'] == 30
    assert data['last_success'] == clock[0].isoformat() and 'last_commit' not in data


def test_an_operation_keeps_a_later_deadline_and_its_data(setup):
    """The reported sequence: a due review asks for ten minutes, then a 360-minute plan review follows."""
    mind, source, clock = setup
    write_schedule(mind, at(clock, 10), {'event_id': 'idle', 'minutes': 10,
                                         'last_success': (clock[0] - timedelta(hours=20)).isoformat()})
    commit(mind, 360, idle_review=False, event_id='plan')
    next_at, data = read_schedule(mind)
    assert next_at == at(clock, 10), 'still the ten minutes the idle review chose'
    assert data['event_id'] == 'idle' and data['minutes'] == 10
    assert data['last_success'] == (clock[0] - timedelta(hours=20)).isoformat()
    assert set(data) == {'event_id', 'minutes', 'last_success'}


def test_an_operation_keeps_an_overdue_deadline(setup):
    mind, source, clock = setup
    write_schedule(mind, at(clock, -90), {'event_id': 'idle', 'minutes': 30,
                                          'last_success': (clock[0] - timedelta(hours=3)).isoformat()})
    commit(mind, 1440, idle_review=False)
    next_at, data = read_schedule(mind)
    assert next_at == at(clock, -90), 'nothing pushes an overdue deadline out'
    assert data['event_id'] == 'idle' and data['minutes'] == 30
    assert set(data) == {'event_id', 'minutes', 'last_success'}


def test_an_operation_may_choose_an_earlier_deadline(setup):
    mind, source, clock = setup
    write_schedule(mind, at(clock, 360), {'event_id': 'plan', 'minutes': 360, 'last_success': clock[0].isoformat()})
    commit(mind, 15, idle_review=False)
    next_at, data = read_schedule(mind)
    assert next_at == at(clock, 15)
    assert data['event_id'] == 'synthetic-event' and data['minutes'] == 15


def test_an_operation_with_no_schedule_creates_one(setup):
    mind, source, clock = setup
    MemoryContinuity(mind)
    with mind.engine.db.connect() as conn:
        assert cursor(conn, mind.scope.key(), 'mind_action_schedule') is None
    commit(mind, 45, idle_review=False)
    next_at, data = read_schedule(mind)
    assert next_at == at(clock, 45) and data['minutes'] == 45 and data['event_id'] == 'synthetic-event'


# --- Through a real assessment ------------------------------------------------------------------

def test_an_idle_review_leaves_the_deadline_it_chose_against_a_later_plan(setup):
    """A due idle review, alone: it consumes the stored deadline and writes the six hours it chose,
    which a plan review arriving afterwards leaves standing. The reverse order is the other pair."""
    mind, source, clock = setup
    memory, actions = world(setup)
    jobs = Appraisals(mind)
    write_schedule(mind, at(clock, -10), {'event_id': 'earlier', 'minutes': 60,
                                          'last_success': (clock[0] - timedelta(hours=25)).isoformat()})
    clock[0] += timedelta(minutes=10)
    assert memory.queue_idle(actions)
    actions.drain(jobs)
    assert jobs.run_one(provider(mind, []))['state'] == 'complete'
    idle_next, idle_data = read_schedule(mind)
    assert idle_next == at(clock, 360) and idle_data['minutes'] == 360
    assert idle_data['event_id'] and idle_data['last_success'] == clock[0].isoformat()
    assert memory.queue_idle(actions) is None, 'not due again: its own six hours stand'
    # A plan review an hour later is judged without moving that deadline.
    clock[0] += timedelta(hours=1)
    plan = enqueue(mind, source, 'plan-after', 'plan-review')
    frames = []
    result = Appraisals(mind).run_one(provider(mind, frames), job_id=plan)
    assert result['state'] == 'complete', result
    plan_next, plan_data = read_schedule(mind)
    assert plan_next == idle_next, 'the plan did not move the deadline the idle review chose'
    assert {k: plan_data[k] for k in idle_data} == idle_data, 'and its data is intact'


def test_a_plan_review_alone_keeps_the_due_deadline(setup):
    """Only a plan review: the overdue deadline it would otherwise push to 360 minutes stays."""
    mind, source, clock = setup
    memory, actions = world(setup)
    last_success = (clock[0] - timedelta(hours=25)).isoformat()
    write_schedule(mind, at(clock, -10), {'event_id': 'earlier', 'minutes': 60, 'last_success': last_success})
    expected = read_schedule(mind)
    clock[0] += timedelta(minutes=10)
    job = enqueue(mind, source, 'plan-only', 'plan-review')
    result = Appraisals(mind).run_one(provider(mind, []), job_id=job)
    assert result['state'] == 'complete', result
    next_at, data = read_schedule(mind)
    assert (next_at, data) == expected
    assert data['event_id'] == 'earlier' and data['minutes'] == 60 and data['last_success'] == last_success
    # The plan did not move it: the quiet review is still due at the next tick.
    assert memory.queue_idle(actions)


def test_an_assessment_of_an_owner_message_leaves_an_earlier_deadline(setup):
    mind, source, clock = setup
    memory, actions = world(setup)
    write_schedule(mind, at(clock, 45), {'event_id': 'earlier', 'minutes': 45, 'last_success': clock[0].isoformat()})
    job = enqueue(mind, source, 'owner-message')
    result = Appraisals(mind).run_one(provider(mind, []), job_id=job)
    assert result['state'] == 'complete', result
    assert read_schedule(mind)[0] == at(clock, 45)


def test_seven_fresh_assessments_run_within_one_hour_and_none_moves_the_deadline(setup):
    """The removed cap answered `hourly-limit` at the seventh; now every fresh assessment runs."""
    mind, source, clock = setup
    world(setup)
    jobs = Appraisals(mind)
    write_schedule(mind, at(clock, 45), {'event_id': 'earlier', 'minutes': 45, 'last_success': clock[0].isoformat()})
    started = clock[0]
    expected = read_schedule(mind)
    for index in range(7):
        enqueue(mind, source, 'fresh-%d' % index)
        # The generic run: no job is named, and the queue itself answers with the row it judged.
        assert jobs.runnable('action') is True
        result = jobs.run_one(native_provider(mind, lambda request: {
            'state': 'complete', 'result': {'reason': '想起一件事', 'next_review_minutes': 600}, 'receipt': RECEIPT}))
        assert result['state'] == 'complete', result
        assert read_schedule(mind) == expected
        clock[0] += timedelta(minutes=1)
    assert clock[0] - started < timedelta(hours=1)
    with mind.engine.db.connect() as conn:
        made = conn.execute("SELECT COUNT(*) FROM mind_appraisals WHERE state='complete'").fetchone()[0]
    assert made == 7



def test_merged_idle_and_plan_review_consumes_the_due_deadline(setup):
    mind, source, clock = setup
    memory, actions = world(setup)
    write_schedule(mind, at(clock, -1), {'event_id': 'earlier', 'minutes': 5})
    plan = enqueue(mind, source, 'merged-plan', 'plan-review')
    assert memory.queue_idle(actions)
    jobs = Appraisals(mind)
    actions.drain(jobs)
    result = jobs.run_one(provider(mind, []), job_id=plan)
    assert result['state'] == 'complete', result
    assert read_schedule(mind)[0] == at(clock, 360)
    assert memory.queue_idle(actions) is None
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT state,data FROM mind_appraisals WHERE json_extract(data,'$.stimulus')='idle-review'").fetchall()
        assert rows and all(row['state'] == 'complete' for row in rows)


def test_a_recipient_with_no_stored_cursor_gets_a_real_initial_deadline(setup):
    from kin_mind.recipient import target_scope
    mind, _, clock = setup
    MemoryContinuity(mind)
    with target_scope(mind, 'person:person_b'):
        commit(mind, 30, idle_review=False)
        assert read_schedule(mind)[0] == at(clock, 30)


def test_legacy_semantic_commit_advances_sources_without_postponing_review(setup):
    from kin_mind.memory import MemoryAssessment
    mind, _, clock = setup
    memory = MemoryContinuity(mind)
    original = {'minutes': 5, 'event_id': 'prior'}
    with mind.engine.db.connect(write=True) as conn:
        save_cursor(conn, mind.scope.key(), 'mind_semantic_cursor', seq=1, next_at=at(clock, 5), data=original)
        memory.apply_assessment(conn, MemoryAssessment(), [], 'new-event', 2, 360, {})
        row = cursor(conn, mind.scope.key(), 'mind_semantic_cursor')
        assert row['seq'] == 2 and row['next_review'] == at(clock, 5)
        assert json.loads(row['data']) == original
        memory.apply_assessment(conn, MemoryAssessment(), [], 'idle-event', 3, 30, {}, idle_review=True)
        row = cursor(conn, mind.scope.key(), 'mind_semantic_cursor')
        assert row['seq'] == 3 and row['next_review'] == at(clock, 30)
