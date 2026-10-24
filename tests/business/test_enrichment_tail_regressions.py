"""Production regressions from the 36-item backlog: independent clocks and usable failures."""
import json
import time
import pytest

from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.companion_view import CompanionView, companion_scope
from kin_mind.host import dispatch
from kin_mind.memory import MemoryAssessment, MemoryNote
from kin_mind import attempts
from test_enrichment_settling import configured, at, assessed, enrichments
from test_derived_erasure import answered, paid
from test_main_session_review import native_provider

pytest_plugins = ('test_kin_mind',)
PEOPLE = ('person:xiaoguang', 'person:yiyi', 'group:family')


def conversation(mind, person):
    return companion_scope(CompanionView(mind.engine, mind.scope, person, 'conversation:' + person))


def event(memory, clock, person, key, ago, kind='owner-message', **extra):
    return memory.ingest({'id': key, 'kind': kind, 'at': at(clock, ago), 'text': key,
                          'recipient_person_id': person, 'conversation_key': 'conversation:' + person, **extra})['source_id']


@pytest.mark.parametrize('active', PEOPLE)
@pytest.mark.parametrize('kind', ['owner-message', 'assistant-message', 'delivery'])
def test_activity_only_moves_its_conversation_and_other_memory_commits_once(setup, active, kind):
    mind, _, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    sources = {}
    for person in PEOPLE:
        with conversation(mind, person):
            sources[person] = []
            for i in range(2):
                sid = event(memory, clock, person, person + str(i), 50 - i)
                sources[person].append(sid)
                assessed(jobs, sid)
    before = enrichments(mind)
    # A runtime event carries its own trusted identity even outside the view of its recipient.
    extra = {'channel': 'feishu', 'delivery_id': 'new-reply', 'bubble_id': 'b', 'state': 'accepted',
             'message_id': 'om-test', 'origin': 'reply'} if kind == 'delivery' else {}
    for ago in [5, 3, 1]:
        if kind == 'delivery': extra.update(delivery_id='reply-' + str(ago), bubble_id='b-' + str(ago))
        event(memory, clock, active, 'new-activity-' + str(ago), ago, kind, **extra)
        after = enrichments(mind)
        for jid, (_, due, data) in after.items():
            assert (due > time.time()) if data['recipient_person_id'] == active else due == before[jid][1]

    class Organize:
        def __init__(self): self.requests = []
        def appraise(self, context):
            ids = [s['id'] for s in context['new_evidence']]
            self.requests.append(ids)
            paid(self)
            return answered(Appraisal(reason='记下这段话', memory=MemoryAssessment(notes=[MemoryNote(
                key='remember', title='聊天片段', content='这段聊天已经整理。', evidence_ids=ids)])))
    provider = Organize()
    for _ in range(2):
        assert jobs.run_one(provider, lane='enrichment')['state'] == 'complete'
    assert jobs.run_one(provider, lane='enrichment')['state'] == 'idle'
    assert len(provider.requests) == 2
    assert {frozenset(ids) for ids in provider.requests} == {frozenset(sources[p]) for p in PEOPLE if p != active}
    rows = enrichments(mind)
    assert sum(state == 'complete' for state, _, _ in rows.values()) == 4
    assert sum(state == 'pending' for state, _, _ in rows.values()) == 2
    # Same store and scope; both batches actually committed notes, not just successful model calls.
    with mind.engine.db.connect() as conn:
        notes = conn.execute("SELECT data FROM records WHERE scope=? AND json_extract(data,'$.attributes.key')='remember'", (mind.scope.key(),)).fetchall()
    assert len(notes) == 2


def test_startup_recalculates_old_cross_conversation_deadlines_without_replaying_backoff(setup):
    mind, _, clock = setup
    memory = configured(mind, enrichment_settle_minutes=20)
    jobs = Appraisals(mind)
    for person in PEOPLE:
        with conversation(mind, person):
            assessed(jobs, event(memory, clock, person, person, 45))
    event(memory, clock, PEOPLE[0], 'current-x', 1)
    # Persist the actual old-release defect, then use the normal host startup path.
    future = time.time() + 1200
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=? WHERE state='pending'", (future,))
        for state in ['pending', 'running', 'needs-repair']:
            data = {'stimulus': 'memory-enrichment', 'recipient_person_id': PEOPLE[2]}
            if state != 'pending': data['settle'] = True
            conn.execute('INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,?,?)',
                         ('protected-' + state, mind.scope.key(), state, future, json.dumps(data)))
    config = {'root': str(mind.engine.db.root), 'scope': mind.scope.model_dump(), 'agent_version': 'synthetic-v1'}
    assert dispatch(config, 'recover', {})['state'] == 'recovered'
    rows = enrichments(mind)
    for jid, (state, due, data) in rows.items():
        if jid.startswith('protected-'): assert due == future and state == jid.removeprefix('protected-')
        else: assert (due > time.time()) == (data['recipient_person_id'] == PEOPLE[0])
    assert dispatch(config, 'recover', {})['enrichment_rescheduled'] == 0


@pytest.mark.parametrize('stage', ['started', 'not-started'])
def test_schema_and_repair_errors_survive_queue_ledger_and_next_model_request(setup, stage):
    mind, source, _ = setup
    jobs = Appraisals(mind)
    job = jobs.enqueue([source('error-feedback')], 'synthetic-v1')
    def failing(request):
        if request['name'] == 'submit_appraisal':
            return {'state': 'complete', 'result': {'reason': '记下来了', 'values': {'curiosity': 'high'}},
                    'receipt': {'native_turn_id': 'main', 'model': 'gpt-6-astra', 'usage': {'input_tokens': 500, 'output_tokens': 50}}}
        return {'state': 'waiting', 'reason': 'fork-failed', 'stage': stage, 'detail': 'provider-error',
                'failure': {'code': 'provider-error', 'message': 'HTTP 502: upstream stream closed at output[0]; token=secret-value'},
                'receipt': {'native_turn_id': 'repair', 'fork_thread_id': 'repair-fork', 'model': 'gpt-6-astra'}}
    assert jobs.run_one(native_provider(mind, failing))['state'] == 'pending'
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute('SELECT data FROM mind_appraisals WHERE id=?', (job['id'],)).fetchone()[0])
        conn.execute('UPDATE mind_appraisals SET available=0 WHERE id=?', (job['id'],))
    receipt = data['failed_call_receipt']
    assert receipt['schema_errors'][0]['loc'] == ['values', 'curiosity']
    assert 'HTTP 502' in receipt['schema_repair']['failure']['message']
    assert 'secret-value' not in json.dumps(receipt)
    [entry] = attempts.read(mind.engine, mind.scope.key(), job_id=job['id'])['attempts']
    assert entry['calls'][0]['detail']['schema_errors'] == receipt['schema_errors']
    assert entry['failed_call_receipt'] == receipt
    if stage == 'started': assert entry['calls'][1]['detail']['failure'] == receipt['schema_repair']['failure']
    else: assert len(entry['calls']) == 1, 'a repair that never started records no model call'
    assert jobs.status(job['id'])['failed_call_receipt'] == receipt
    calls = []
    def corrected(request):
        calls.append(request)
        feedback = request['context']['previous_attempt']['failed_call_receipt']
        assert feedback['schema_errors'][0]['loc'] == ['values', 'curiosity']
        assert 'upstream stream closed' in feedback['schema_repair']['failure']['message']
        return {'state': 'complete', 'result': {'reason': '数值改为整数', 'values': {'curiosity': 81}},
                'receipt': {'native_turn_id': 'retry', 'model': 'gpt-6-astra', 'usage': {'input_tokens': 500, 'output_tokens': 50}}}
    assert jobs.run_one(native_provider(mind, corrected))['state'] == 'complete'
    assert mind.read()['dimensions']['curiosity']['value'] == 81
    assert len(calls) == 1 and jobs.run_one(native_provider(mind, corrected))['state'] == 'idle'
