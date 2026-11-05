from kin_mind.appraisal import Appraisal, NativeReview, MaintenanceAssessment
from kin_mind.strict_schema import strict_schema, decode
from kin_mind.profile import DIMENSIONS
from pydantic import ValidationError

def test_dimension_keys_survive_native_schema():
    schema=Appraisal.model_json_schema()
    native=strict_schema(schema)['properties']['values']['anyOf'][0]
    assert native['items']['properties']['key']['enum']==list(DIMENSIONS)
    assert '100' in native['items']['properties']['value']['description']
    for values in ({'invented':50},{'mood':101}):
        try:Appraisal(values=values)
        except ValidationError as e:assert e.errors()[0]['type']!='value_error'
        else:raise AssertionError('invalid score accepted')

def test_maintenance_only_offers_committed_fields():
    review=object.__new__(NativeReview)
    context={'stimulus':'session-maintenance'}
    schema=review._schema(context)
    assert set(schema['properties'])=={'reason','session_advice'}
    assert schema['required']==['session_advice']
    text=review._system(context,None)
    assert 'recipient_targets' not in text
    assert 'wishes' not in text

def test_shared_prompt_defaults_and_edits(tmp_path):
    from eventmem.core.prompts import project_prompt, PromptStore
    assert project_prompt('provider/summary','default one',tmp_path)=='default one'
    store=PromptStore(tmp_path/'prompts')
    assert project_prompt('provider/summary','default two',tmp_path)=='default two'
    assert store.read('provider/summary')['text']=='default two'
    store.write('provider/summary','KIN own wording')
    assert project_prompt('provider/summary','code changed again',tmp_path)=='KIN own wording'
    assert len(store.list())==1

def test_rejected_maintenance_is_not_completed(setup, monkeypatch):
    from kin_mind import appraisal
    from kin_mind.session_advice import SessionAdvice
    from eventmem.core.db import Conflict
    mind, source, _ = setup
    jobs=appraisal.Appraisals(mind, session_context={'id':'snapshot','binding':{'generation':1},'evidence':[],'recent':[]})
    job=jobs.enqueue([source('pressure')],'synthetic-v1',origin='reflection',stimulus='session-maintenance')
    def reject(*args, **kwargs):
        raise Conflict('Session finding needs an exact owner source',code='finding-source-mismatch')
    monkeypatch.setattr(appraisal,'advice_record',reject)
    result=jobs.run_one(FakeReviewer(Appraisal(session_advice=SessionAdvice(action='keep',reason='Current state'))))
    assert result['state']!='complete'
    assert jobs.status(job['id'])['state']!='complete'

def test_a_review_refused_twice_for_one_snapshot_settles_on_a_fixed_fallback_alone_or_merged(setup, monkeypatch):
    """Item 14: alone or merged into a batch, a session review the host refuses is retried, and at the
    limit for one snapshot settles on keep (defer for an unverified compaction) instead of being paid for again."""
    import json
    from kin_mind import appraisal
    from kin_mind.session_advice import SessionAdvice, AdviceRejected
    mind, source, _ = setup
    real = appraisal.advice_record
    def reject(proposal, *args, **kwargs):
        if proposal.reason.startswith('host-fallback'):
            return real(proposal, *args, **kwargs)
        raise AdviceRejected('finding-source-mismatch', 'Session finding needs an exact owner source')
    monkeypatch.setattr(appraisal, 'advice_record', reject)
    context = {'id': 'snapshot', 'binding': {'generation': 1}, 'evidence': [], 'recent': []}
    for merged, proposed, fallback in ((False, 'keep', 'keep'), (True, 'compact', 'defer')):
        jobs = appraisal.Appraisals(mind, session_context=context)
        review = jobs.enqueue([source('pressure-%s' % merged)], 'synthetic-v1', origin='reflection', stimulus='session-maintenance')
        with mind.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (review['id'],)).fetchone()
            conn.execute("UPDATE mind_appraisals SET data=? WHERE id=?", (json.dumps({**json.loads(row[0]), 'session_snapshot_id': 'snapshot'}), review['id']))
        if merged:
            jobs.enqueue([source('quiet-%s' % merged)], 'synthetic-v1', origin='reflection', stimulus='idle-review')
        reviewer = FakeReviewer(Appraisal(session_advice=SessionAdvice(action=proposed, reason='Current state')))
        def run():
            with mind.engine.db.connect(write=True) as conn:
                conn.execute("UPDATE mind_appraisals SET available=0 WHERE state='pending'")
            return jobs.run_one(reviewer)
        assert run()['state'] == 'pending'
        assert jobs.status(review['id'])['state'] != 'complete'
        if merged:
            with mind.engine.db.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM mind_appraisals WHERE json_extract(data,'$.batch_ids') IS NOT NULL "
                                    "AND json_extract(data,'$.stimulus')='internal-batch'").fetchone()[0] == 1, 'the review was merged'
        assert run()['state'] == 'complete', merged
        assert jobs.status(review['id'])['state'] == 'complete'
        with mind.engine.db.connect() as conn:
            advice = json.loads(conn.execute("SELECT data FROM mind_session_advice").fetchone()[0])
        assert advice['decision']['action'] == fallback and advice['decision']['reason'] == 'host-fallback:finding-source-mismatch'

from test_kin_mind import setup, FakeReviewer

def test_review_accepts_the_user_source_it_displayed():
    from kin_mind.session_advice import with_dialogue, advice_record, SessionAdvice, SessionFinding, repair_input, AdviceRejected
    context={'id':'snapshot','binding':{'generation':1},'recent':[], 'evidence':[]}
    dialogue=[{'id':'earlier-input','role':'user','text':'这个回答重复了。'}]
    proposal=SessionAdvice(action='keep',reason='Review feedback',findings=[SessionFinding(sourceId='earlier-input',kind='context-degradation',quote='回答重复了',reason='User feedback')])
    try:advice_record(proposal,context,{},'event')
    except AdviceRejected:pass
    else:raise AssertionError('unseen source accepted')
    shown=with_dialogue(context,dialogue)
    assert advice_record(proposal,shown,{},'event')['decision']['findings']
    assert repair_input(proposal,shown,AdviceRejected('finding-source-mismatch','source'))['recent'][0]['id']=='earlier-input'
    assert context['recent']==[]
