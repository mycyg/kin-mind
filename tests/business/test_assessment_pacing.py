"""Assessment pacing (feature 3): an hourly limit per lane, the quiet review's floor, and the rows an
outage set aside going back by themselves once an appraisal commits again."""
import json
from datetime import timedelta

from kin_mind.appraisal import HOURLY_ATTEMPT_LIMIT, MAX_TRANSIENT_FAILURES, Appraisals
from kin_mind.memory import QUIET_REVIEW_MINUTES
from kin_mind.recovery import AUTO_RESUMES
from test_main_session_review import native_provider
from test_initiative import world

pytest_plugins = ('test_kin_mind',)

COMPLETE = {'state': 'complete', 'result': {'reason': '想起一件小事。', 'values': {'curiosity': 70}},
            'receipt': {'native_turn_id': 'turn', 'model': 'gpt-6-astra', 'provider': 'custom', 'reasoning': 'medium',
                        'usage': {'input_tokens': 100, 'output_tokens': 10}}}
FAILED = {'state': 'waiting', 'reason': 'fork-failed', 'model_invoked': True, 'started': True,
          'receipt': {'model': 'gpt-6-astra', 'native_turn_id': 't', 'fork_thread_id': 'f', 'usage': None}}


def due_now(mind, job_id):
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job_id,))


def state_of(mind, job_id):
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT state,data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()
    return row['state'], json.loads(row['data'])


def set_aside(mind, jobs, job_id):
    for _ in range(MAX_TRANSIENT_FAILURES + 1):
        due_now(mind, job_id)
        jobs.run_one(native_provider(mind, lambda request: FAILED))
    assert state_of(mind, job_id)[0] == 'needs-repair'


def test_rows_an_outage_set_aside_go_back_after_the_next_commit_a_bounded_number_of_times(setup):
    mind, source, _ = setup
    jobs = Appraisals(mind)
    outage = jobs.enqueue([source('during-outage')], 'synthetic-v1')['id']
    set_aside(mind, jobs, outage)
    for round_ in range(AUTO_RESUMES + 1):
        fresh = jobs.enqueue([source('after-outage-%d' % round_)], 'synthetic-v1')['id']
        due_now(mind, fresh)
        assert jobs.run_one(native_provider(mind, lambda request: COMPLETE), job_id=fresh)['state'] == 'complete'
        state, data = state_of(mind, outage)
        if round_ == AUTO_RESUMES:
            assert state == 'needs-repair', 'given back at most AUTO_RESUMES times'
            break
        assert state == 'pending' and 'repair_reason' not in data and 'transient_failures' not in data
        assert [h['command_id'] for h in data['recovery_history']].count('provider-recovered') == round_ + 1
        set_aside(mind, jobs, outage)


def test_the_hourly_limit_holds_the_lane_but_not_a_session_review(setup):
    mind, source, clock = setup
    jobs = Appraisals(mind)
    for index in range(HOURLY_ATTEMPT_LIMIT):
        job = jobs.enqueue([source('busy-%d' % index)], 'synthetic-v1')['id']
        due_now(mind, job)
        assert jobs.run_one(native_provider(mind, lambda request: COMPLETE), job_id=job)['state'] == 'complete'
    waiting = jobs.enqueue([source('held')], 'synthetic-v1')['id']
    due_now(mind, waiting)
    assert jobs.runnable('action') is False
    assert jobs.run_one(native_provider(mind, lambda request: COMPLETE)) == {'state': 'idle', 'reason': 'hourly-limit'}
    review = jobs.enqueue_maintenance('snapshot-1', 'synthetic-v1')['id']
    due_now(mind, review)
    assert jobs.runnable('action') is True
    clock[0] += timedelta(minutes=61)
    assert jobs.run_one(native_provider(mind, lambda request: COMPLETE), job_id=waiting)['state'] == 'complete'


def test_with_nothing_new_the_quiet_review_waits_for_its_floor(setup):
    mind, source, clock = setup
    memory, actions = world(setup)
    clock[0] += timedelta(minutes=30)
    assert memory.queue_idle(actions)
    with mind.engine.db.connect(write=True) as conn:
        # As a committed assessment leaves the clock: Kin's next look in 10 minutes, the commit now.
        conn.execute("UPDATE mind_action_schedule SET next_review=?,data=?", ((clock[0] + timedelta(minutes=10)).isoformat(),
                     json.dumps({'last_success': clock[0].isoformat(), 'minutes': 10})))
    clock[0] += timedelta(minutes=11)
    assert memory.queue_idle(actions) is None
    clock[0] += timedelta(minutes=QUIET_REVIEW_MINUTES - 11)
    assert memory.queue_idle(actions)
