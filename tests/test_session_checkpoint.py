from datetime import timedelta

import pytest
from test_memory_continuity import system as memory_system

from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.context import Contexts
from kin_mind.session_advice import SessionAdvice
from kin_mind.session_checkpoint import SessionCheckpoint


@pytest.fixture
def system(tmp_path):
    return memory_system.__wrapped__(tmp_path)


def test_checkpoint_retains_question_and_all_reply_bubbles_and_repeated_user_words(system):
    mind, memory, _, clock = system
    texts = [('owner-message', '云朵需要一个名字牌。'), ('assistant-message', '你想要方形贴纸吗？'), ('owner-message', '要的！'), ('assistant-message', '嗯嗯～'), ('assistant-message', '我给云朵做方形的。'), ('owner-message', '要的！')]
    for index, (kind, text) in enumerate(texts):
        clock[0] += timedelta(seconds=1)
        memory.ingest({'id': str(index), 'kind': kind, 'text': text, 'at': mind.clock()})
    api = SessionCheckpoint(mind)
    snapshot = api.snapshot()
    assert [i['text'] for i in snapshot['items']].count('要的！') == 2
    cp = api.build(snapshot, {'conversationId': 'synthetic', 'generation': 1}, budget=4000)
    assert cp['complete'] and cp['tokens'] <= 4000
    assert [i['text'] for i in cp['items']] == [text for _, text in texts]
    assert api.validate(cp)['valid']
    with mind.engine.db.connect() as conn:
        refs = mind._evidence(conn, [snapshot['items'][0]['sourceId']])
    mind.engine.delete(refs[0]['record_id'])
    assert not api.validate(cp)['valid']
    assert '0' in api.snapshot()['invalidatedSources']


def test_pending_public_journal_is_available_before_semantic_queue_and_receipts_are_separate(system):
    mind, _, _, _ = system
    api = SessionCheckpoint(mind)
    snapshot = api.snapshot([
        {'id': 'q', 'kind': 'owner-message', 'at': mind.clock(), 'text': '那个贴纸呢？'},
        {'id': 'pending', 'kind': 'delivery', 'state': 'unconfirmed', 'at': mind.clock(), 'text': '完成了'},
        {'id': 'warning', 'kind': 'assistant-message', 'at': mind.clock(), 'text': 'Warning: Heads up: Long threads and multiple compactions'},
    ])
    assert [item['id'] for item in snapshot['items']] == ['q']
    assert snapshot['items'][0]['delivery'] == 'not-confirmed-by-this-record'


def test_first_appraisal_after_host_upgrade_does_not_invalidate_its_own_config_snapshot(system):
    mind, _, source, _ = system
    api = SessionCheckpoint(mind, agent_version='host-v2')
    before = api.snapshot()
    jobs = Appraisals(mind, exploration_capabilities={'version': 'host-v2'}, session_context={'id': 'observation', 'binding': {'generation': 1}})
    jobs.enqueue([source('maintenance')], 'host-v2', origin='reflection', stimulus='session-maintenance')

    class Reviewer:
        def appraise(self, context):
            return Appraisal(reason='Keep current thread', session_advice=SessionAdvice(action='keep', reason='No degradation')), {'model': 'deepseek-flash', 'reasoning': 'max'}

    assert jobs.run_one(Reviewer())['state'] == 'complete'
    assert mind.read()['agent_version'] == 'host-v2'
    assert api.snapshot()['configVersion'] == before['configVersion']


def test_overbudget_checkpoint_waits_without_truncating_the_last_exchange(system):
    mind, _, _, _ = system
    api = SessionCheckpoint(mind)
    text = '这一句话必须完整保留。' * 1000
    snapshot = api.snapshot([{'id': 'long', 'kind': 'owner-message', 'at': mind.clock(), 'text': text}])
    cp = api.build(snapshot, {'conversationId': 'synthetic', 'generation': 1}, budget=500)
    assert not cp['complete'] and cp['items'][0]['text'] == text


def test_old_compaction_ack_after_a_new_window_does_not_clear_used_tokens(system):
    mind, _, _, _ = system
    ctx = Contexts(mind)
    for epoch in ['one', 'two']:
        ctx.compact_ack('same', epoch, actual_session='same', completed=True)
    ctx.injection_ack('same', 'two', 'restore-two', 321)
    assert ctx.compact_ack('same', 'one', actual_session='same', completed=True)['state'] == 'already-applied'
    assert ctx.window('same')['used'] == 321
    assert ctx.window('same')['epoch'] == 'two'


def test_session_maintenance_is_prioritized_small_and_does_not_update_affect(system):
    mind, _, source, _ = system
    before = mind.read()
    snapshot = {'id': 'observation', 'binding': {'generation': 1}, 'pressure': {'level': 'elevated'}}
    jobs = Appraisals(mind, session_context=snapshot)
    normal = jobs.enqueue([source('conversation')], 'fixture-v1')
    maintenance = jobs.enqueue([source('maintenance')], 'fixture-v1', origin='reflection', stimulus='session-maintenance')
    class Reviewer:
        def appraise(self, context):
            assert context['stimulus'] == 'session-maintenance'
            assert 'memory_context' not in context and 'dimensions' not in context['state']
            return Appraisal(reason='Compact first', values={'mood': 0}, session_advice=SessionAdvice(action='compact', reason='Window pressure')), {'model': 'deepseek-flash', 'reasoning': 'max'}
    result = jobs.run_one(Reviewer())
    assert result['state'] == 'complete', result
    assert jobs.status(maintenance['id'])['state'] == 'complete'
    assert jobs.status(normal['id'])['state'] == 'pending'
    assert mind.read()['dimensions'] == before['dimensions']
    assert mind.read()['session_advice']['decision']['action'] == 'compact'
    with pytest.raises(ValueError):
        Contexts(mind).injection_ack('s', 'e', 'x', -1)


def test_readonly_session_review_can_finish_during_memory_inference_without_recomputing_it(system):
    mind, _, source, _ = system
    jobs = Appraisals(mind, session_context={'id': 'observation', 'binding': {'generation': 1}})
    work = jobs.enqueue([source('normal interaction')], 'fixture-v1')
    calls = []

    class SessionReviewer:
        def appraise(self, context):
            calls.append('session')
            jobs.enqueue([source('second internal check')], 'fixture-v1', origin='reflection', stimulus='session-maintenance')
            assert jobs.run_one(self)['state'] == 'busy'
            return Appraisal(reason='Keep', session_advice=SessionAdvice(action='keep', reason='Healthy')), {'model': 'deepseek-flash', 'reasoning': 'max'}

    class MemoryReviewer:
        def appraise(self, context):
            calls.append('memory')
            check = jobs.enqueue([source('internal check')], 'fixture-v1', origin='reflection', stimulus='session-maintenance')
            assert jobs.run_one(SessionReviewer())['state'] == 'complete'
            assert jobs.status(check['id'])['state'] == 'complete'
            assert jobs.status(work['id'])['state'] == 'running'
            return Appraisal(reason='No state change'), {'model': 'deepseek-flash', 'reasoning': 'max'}

    assert jobs.run_one(MemoryReviewer())['state'] == 'complete'
    assert calls == ['memory', 'session']
    assert jobs.status(work['id'])['state'] == 'complete'
    assert mind.read()['session_advice']['decision']['action'] == 'keep'
