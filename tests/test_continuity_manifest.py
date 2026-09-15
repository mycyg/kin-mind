import json
from datetime import timedelta

import pytest
from test_memory_continuity import system as memory_system

from eventmem.core.db import Conflict
from eventmem.core.models import RevisionInput
from kin_mind.context import Contexts
from kin_mind.context_delivery import ContextDelivery
from kin_mind.continuity import ConcernChange, ContinuityConfig
from kin_mind.continuity_manifest import ContinuityManifest
from kin_mind.session_checkpoint import SessionCheckpoint


@pytest.fixture
def system(tmp_path):
    values = memory_system.__wrapped__(tmp_path)
    values[1].configure({'manifests': True, 'manifest_restore': True, 'context_receipts': True,
                         'graph': True, 'graph_recall': True, 'sharing': True, 'continuity_overviews': True})
    return values


def binding():
    return {'conversationId': 'synthetic-conversation', 'generation': 1, 'threadId': 'thread', 'nativeSessionId': 'root'}


def old_work(system):
    mind, memory, _, clock = system
    result = memory.ingest({'id': 'created', 'kind': 'artifact-created', 'at': mind.clock(), 'task_id': 'old-task',
                           'artifact': {'name': 'cloud.zip', 'sha256': 'b' * 64, 'members_sha256': 'c' * 64}})
    for n in range(30):
        clock[0] += timedelta(seconds=1)
        memory.ingest({'id': str(n), 'kind': 'owner-message', 'at': mind.clock(), 'text': 'Unrelated ordinary conversation'})
    return result


def test_old_work_identity_and_task_are_selected_beyond_recent_messages(system):
    mind, memory, _, clock = system
    created = old_work(system)
    clock[0] += timedelta(seconds=1)
    memory.ingest({'id': 'question', 'kind': 'owner-message', 'at': mind.clock(), 'text': 'Who made cloud.zip?'})
    api = SessionCheckpoint(mind)
    snap = api.snapshot(tasks=[{'id': 'old-task'}])
    assert 'created' not in [i['id'] for i in snap['items']]
    item = next(i for i in snap['linked']['items'] if i['id'] == created['work_id'])
    assert item['facts']['created_by'] == 'Kin'
    cp = api.build(snap, binding(), budget=4000, allow_model=False)
    assert cp['complete'], cp['coverage']
    assert 'Kin' in cp['payload']['memoryContext'] and 'cloud.zip' in cp['payload']['memoryContext']
    assert api.validate(cp)['valid']
    assert ContinuityManifest(mind).read(cp['id'])['watermarks']['semantic_seq'] < snap['watermarks']['durable_event_seq']
    # An unrelated later event doesn't invalidate the evidence already used.
    memory.ingest({'id': 'unrelated', 'kind': 'owner-message', 'at': mind.clock(), 'text': 'hello'})
    assert api.validate(cp)['valid']
    with mind.engine.db.connect() as conn:
        rid = mind._evidence(conn, [created['source_id']])[0]['record_id']
    mind.engine.delete(rid)
    assert not api.validate(cp)['valid']


def test_pending_outbox_coverage_participates_before_memory_ingestion(system):
    mind, memory, _, _ = system
    work = old_work(system)
    with mind.engine.db.connect(write=True) as conn:
        unit = memory.sharing.units(conn, work['work_id'], ['Cloud files contain synthetic stars.'], [work['source_id']], owner_kind='work')[0]
    pending = [{'id': 'fresh-receipt', 'kind': 'delivery', 'at': mind.clock(), 'state': 'accepted', 'message_id': 'platform-1',
                'text': unit['text'], 'references': [{'unit_id': unit['id'], 'version': 1}]}]
    select = ContinuityManifest(mind).select('Cloud files', pending=pending)
    found = next(i for i in select['items'] if i['id'] == unit['id'])
    assert found['facts']['share_coverage']['state'] == 'shared'
    assert found['facts']['share_coverage']['basis'] == 'pending-host-receipt'
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn, unit['id'])['state'] == 'unshared'
    pending[0]['state'] = 'unconfirmed'
    found = next(i for i in ContinuityManifest(mind).select('Cloud files', pending=pending)['items'] if i['id'] == unit['id'])
    assert found['facts']['share_coverage']['state'] == 'unshared'


def test_open_concern_is_independent_of_recent_window_and_resolving_invalidates(system):
    mind, _, source, _ = system
    mind.configure_continuity(ContinuityConfig(command_id='enable', agent_version='fixture-v1', expected_revision=mind.read()['revision'], evidence_ids=[source('enable')], features={'concerns': True}, reason='Synthetic configuration'))
    mind.manage_concern(ConcernChange(command_id='pending', agent_version='fixture-v1', expected_revision=mind.read()['revision'], evidence_ids=[source('wait')],
        action='create', key='cloud', kind='shared_plan', content='Wait for the cloud sketch before printing.', topic='cloud', intensity=80, basis='explicit', confidence=1, reason='An unfinished plan'))
    selection = ContinuityManifest(mind).select('cloud')
    item = next(i for i in selection['items'] if i.get('state_dependency'))
    assert 'before printing' in item['text'] and Contexts(mind)._current(item)
    mind.manage_concern(ConcernChange(command_id='resolved', agent_version='fixture-v1', expected_revision=mind.read()['revision'], evidence_ids=[source('result')],
        action='resolve', concern_id=item['id'], reason='Sketch received'))
    assert not Contexts(mind)._current(item)
    assert item['id'] not in [i['id'] for i in ContinuityManifest(mind).select('cloud')['items']]


def prepare(system, *, event='event', session='thread', epoch='initial', text='The file is not delivered yet.'):
    mind, _, source, _ = system
    ctx = Contexts(mind)
    sid = source(event, text)
    with mind.engine.db.connect() as conn:
        rid = mind._evidence(conn, [sid])[0]['record_id']
    item = ctx.record_item(mind.engine.get(rid))
    api = ContextDelivery(ctx)
    value = api.prepare(session, epoch, event, text, [item], budget=2000)
    return api, value, item


def ack(api, value):
    return api.acknowledge(value['session'], value['epoch'], value['id'], actual_session=value['session'],
        marker=value['marker'], text_hash=value['text_hash'], verified=True)


def test_preparation_does_not_claim_injection_or_reading_and_ack_is_idempotent(system):
    api, value, item = prepare(system)
    assert api.ctx.window('thread')['used'] == 0
    assert item['id'] not in api.ctx.window('thread')['seen']
    assert api.begin('thread', 'initial', value['id'])['state'] == 'sending'
    recovered = ContextDelivery(Contexts(system[0]))
    assert recovered.pending('thread')[0]['id'] == value['id']
    assert ack(recovered, value)['state'] == 'accepted'
    assert ack(recovered, value)['state'] == 'accepted'
    assert recovered.ctx.window('thread')['used'] == value['tokens']
    assert recovered.ctx.window('thread')['seen'][item['id']] == item['revision']


def test_changed_source_before_injection_is_stale_without_counting(system):
    api, value, item = prepare(system)
    system[0].engine.revise(item['id'], RevisionInput(expected_revision=1, command_id='correct', action='correct', content='Delivery is now accepted.'))
    assert api.begin('thread', 'initial', value['id'])['state'] == 'stale'
    assert api.ctx.window('thread')['used'] == 0


def test_receipt_after_correction_counts_bytes_but_not_invalidated_fact(system):
    api, value, item = prepare(system)
    api.begin('thread', 'initial', value['id'])
    system[0].engine.delete(item['id'])
    assert ack(api, value)['needs_review']
    assert api.ctx.window('thread')['used'] == value['tokens']
    assert item['id'] not in api.ctx.window('thread')['seen']


def test_receipt_requires_same_native_session_and_exact_public_text_hash(system):
    api, value, _ = prepare(system)
    with pytest.raises(Conflict):
        api.acknowledge('thread', 'initial', value['id'], actual_session='other', marker=value['marker'], text_hash=value['text_hash'], verified=True)
    with pytest.raises(Conflict):
        api.acknowledge('thread', 'initial', value['id'], actual_session='thread', marker=value['marker'], text_hash='wrong', verified=True)
    assert api.ctx.window('thread')['used'] == 0


def test_old_epoch_receipt_does_not_count_or_hide_sources_in_new_window(system):
    api, value, item = prepare(system)
    api.begin('thread', 'initial', value['id'])
    api.ctx.compact_ack('thread', 'new-epoch', actual_session='thread', completed=True)
    assert ack(api, value)['historical']
    assert api.ctx.window('thread')['used'] == 0 and item['id'] not in api.ctx.window('thread')['seen']


def test_other_context_waits_for_uncertain_append(system):
    api, value, _ = prepare(system)
    api.begin('thread', 'initial', value['id'])
    api.uncertain('thread', 'initial', value['id'])
    _, other, _ = prepare(system, event='other')
    assert api.begin('thread', 'initial', other['id'])['state'] == 'waiting'
    ack(api, value)
    assert api.begin('thread', 'initial', other['id'])['state'] == 'sending'


def test_ack_and_window_accounting_roll_back_together(system, monkeypatch):
    api, value, _ = prepare(system)
    original = api.ctx._save_window
    def interrupted(*args):
        original(*args)
        raise RuntimeError('Synthetic transaction interruption')
    monkeypatch.setattr(api.ctx, '_save_window', interrupted)
    with pytest.raises(RuntimeError):
        ack(api, value)
    fresh = ContextDelivery(Contexts(system[0]))
    assert fresh.ctx.window('thread')['used'] == 0
    assert ack(fresh, value)['state'] == 'accepted'
    assert fresh.ctx.window('thread')['used'] == value['tokens']


def test_normal_prepared_background_uses_budget_without_a_model_or_early_count(system):
    mind, _, _, _ = system
    ctx = Contexts(mind)
    result = ctx.build('hello', session='thread', event_id='hello', receipt_mode=True, allow_model=False, host_overhead=80)
    assert result['injection']['tokens'] <= result['budget']
    assert ctx.window('thread')['used'] == 0
    api = ContextDelivery(ctx)
    assert ack(api, result['injection'])['state'] == 'accepted'
    assert ctx.window('thread')['used'] == result['injection']['tokens']
    reread = ctx.build('hello', session='thread', event_id='second', receipt_mode=True, allow_model=False)
    assert 'affect' not in reread['covered_ids']
    explicit = ctx.build('hello', purpose='read', session='thread', allow_model=False)
    assert 'affect' in explicit['covered_ids']


def test_content_overview_can_reuse_text_while_delivery_facts_change(system):
    mind, _, _, _ = system
    ctx = Contexts(mind)
    item = {'id': 'synthetic-finding', 'revision': 1, 'text': 'An unchanged cloud finding. ' * 100, 'facts': {'share_coverage': {'state': 'unshared'}}}
    key = ctx._overview_key(item)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('INSERT INTO mind_context_cache VALUES(?,?,?,?)', (key,mind.scope.key(),json.dumps({'text':'A cloud finding.', 'source':item}),mind.clock()))
    changed = {**item, 'revision': 2, 'facts': {'share_coverage': {'state': 'shared'}}}
    warmed = ctx._overview(changed)
    assert warmed['cached_summary'] and warmed['facts']['share_coverage']['state'] == 'shared'
    assert ctx._overview_key({**changed, 'text': 'A corrected finding.'}) != key


def test_unrelated_append_does_not_change_manifest_identity(system):
    mind, _, _, _ = system
    api = SessionCheckpoint(mind)
    snap = api.snapshot([{'id': 'q', 'kind': 'owner-message', 'at': mind.clock(), 'text': 'Want a cloud name?'},
                         {'id': 'a', 'kind': 'assistant-message', 'at': mind.clock(), 'text': 'Yes.'}])
    first = api.build(snap, binding(), budget=2000, allow_model=False)
    assert first['complete']
    snap['watermarks']['semantic_seq'] += 2
    second = api.build(snap, binding(), budget=2000, allow_model=False)
    assert second['id'] == first['id']


def test_last_question_and_short_answer_survive_manifest_compression(system):
    mind, _, _, _ = system
    api = SessionCheckpoint(mind)
    pending = [{'id':'q','kind':'assistant-message','at':'2026-09-14T03:00:00Z','text':'要给云朵做一张方形贴纸吗？'},
               {'id':'u','kind':'owner-message','at':'2026-09-14T03:00:01Z','text':'要的！'}]
    cp = api.build(api.snapshot(pending), binding(), budget=2000, allow_model=False)
    assert cp['complete'] and cp['tokens'] <= 1905
    assert '方形贴纸' in json.dumps(cp['payload'], ensure_ascii=False)
    assert '要的！' in json.dumps(cp['payload'], ensure_ascii=False)


def test_manifest_budget_failure_retains_original_evidence_and_has_no_model_call(system):
    mind, _, _, _ = system
    api = SessionCheckpoint(mind)
    text = 'Cloud observations with a condition, not a delivery. ' * 600
    cp = api.build(api.snapshot([{'id':'long','kind':'owner-message','at':mind.clock(),'text':text}]), binding(), budget=500, allow_model=False)
    assert not cp['complete']
    assert cp['items'][0]['text'] == text
    assert not cp['metrics']['model_requested']


def test_pending_creation_keeps_real_author_and_invalidates_after_source_correction(system):
    mind, memory, _, _ = system
    event = {'id': 'unmerged-create', 'kind': 'artifact-created', 'at': mind.clock(), 'task_id': 'task-cloud',
             'actor': 'Kin', 'artifact': {'name': 'cloud.zip', 'sha256': 'a' * 64}}
    item = next(i for i in ContinuityManifest(mind).select('cloud.zip', pending=[event])['items'] if i['id'] == 'journal:unmerged-create')
    assert 'Kin' in item['text'] and 'a' * 64 in item['text']
    receipt = memory.ingest(event)
    assert Contexts(mind)._current(item)
    with mind.engine.db.connect() as conn:
        rid = mind._evidence(conn, [receipt['source_id']])[0]['record_id']
    mind.engine.delete(rid)
    assert not Contexts(mind)._current(item)


def test_critical_units_are_not_silently_cut_at_four(system):
    mind, _, _, _ = system
    api = SessionCheckpoint(mind)
    snap = api.snapshot([{'id':'q','kind':'owner-message','at':mind.clock(),'text':'Continue the unfinished task.'}])
    items = [{'id':str(i), 'revision':1, 'basis':'observed', 'text':'Condition '+str(i), 'priority':0} for i in range(5)]
    snap['linked'] = {'items':items,'critical_ids':[i['id'] for i in items]}
    cp = api.build(snap, binding(), budget=4000, allow_model=False)
    assert cp['complete']
    assert all(i['id'] in cp['memoryCoverage']['covered_ids'] for i in items)
    snap['linked']['critical_ids'].append('unloaded-critical-source')
    cp = api.build(snap, binding(), budget=4000, allow_model=False)
    assert not cp['complete'] and cp['criticalMissing'] == ['unloaded-critical-source']


def test_cached_compression_reports_zero_new_model_requests(system):
    from kin_mind.context import Compression
    class Provider:
        calls = 0
        def structured(self, name, schema, prompt, payload, **kwargs):
            self.calls += 1
            return Compression(entries=[{'item_ids':payload['allowed_item_ids'], 'summary':'The file remains undelivered.'}], omitted_ids=[]), {'model':'synthetic','reasoning':'max'}
    ctx, provider = Contexts(system[0]), Provider()
    items = [{'id':'a','revision':1,'text':'The file has not been delivered. '*200,'basis':'observed'}]
    first = ctx.pack(items, 'delivery status', 400, provider=provider, require_all=True)
    second = ctx.pack(items, 'delivery status', 400, provider=provider, require_all=True)
    assert first['model_requests'] == 1 and not first['omitted_ids']
    assert second['cache_hit'] and second['model_requests'] == 0 and provider.calls == 1


def test_checkpoint_from_an_older_host_configuration_requires_refresh(system):
    mind, _, _, _ = system
    old = SessionCheckpoint(mind, agent_version='host-old')
    snapshot = old.snapshot([{'id':'q','kind':'owner-message','at':mind.clock(),'text':'Hello'}])
    checkpoint = old.build(snapshot,binding(),budget=2000,allow_model=False)
    assert old.validate(checkpoint)['valid']
    assert not SessionCheckpoint(mind,agent_version='host-new').validate(checkpoint)['valid']
