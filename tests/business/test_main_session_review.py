import json
import pytest
from kin_mind.appraisal import Appraisal, Appraisals, NativeReview
from kin_mind import attempts
from kin_mind.model_lanes import ModelAdmissionWait
from kin_mind.context import Contexts
from kin_mind.codex_executor import codex_final_result, run_codex
from test_kin_exploration_codex import fake_codex, codex_kwargs, TOPIC, FINDINGS, THREAD_STARTED

pytest_plugins = ('test_kin_mind',)

def native_provider(mind, exchange):
    p = NativeReview('https://api.deepseek.com', 'gpt-6-astra', timeout=30)
    p.engine = mind.engine
    p.profile = {'model':'gpt-6-astra','modelProvider':'custom','reasoningEffort':'medium','fastMode':'off',
                 'modelContextWindow':128000,'outputReserve':16000,'toolReserve':8000}
    p.input_budget=104000; p.native_call_number=0; p.native_attempt=('one','attempt'); p.exchange=exchange
    return p

def test_main_session_appraisal_commits_once_without_contact_or_diary(setup):
    mind,source,_=setup
    calls=[]
    def exchange(request):
        calls.append(request)
        return {'state':'complete','result':{'reason':'刚读到一个有意思的问题，想先记在心里。','values':{'curiosity':83}},
                'receipt':{'native_turn_id':'turn-1','native_session_id':'same-main','model':'gpt-6-astra','provider':'custom','reasoning':'medium','usage':{'input_tokens':500,'output_tokens':50}}}
    p=native_provider(mind,exchange);jobs=Appraisals(mind)
    job=jobs.enqueue([source('question')],'synthetic-v1')
    out=jobs.run_one(p)
    assert out['state']=='complete',out
    assert mind.read()['dimensions']['curiosity']['value']==83
    assert mind.read()['desires']==[]
    assert len(calls)==1 and calls[0]['profile']==p.profile
    assert '不重复增加成长依据' in calls[0]['system']
    assert '不调用这些提交工具' in calls[0]['system']
    assert '有效的 DS 决策' not in calls[0]['system']
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=0 WHERE id=?",(job['id'],))
    assert jobs.run_one(p)['state']=='complete'
    assert len(calls)==1

def test_preemption_records_unknown_usage_without_exhausting_repairs(setup):
    mind,_,_=setup
    p=native_provider(mind,lambda request:{'state':'waiting','model_invoked':True})
    with attempts.collect(p) as calls:
        with pytest.raises(ModelAdmissionWait):p._native('submit_appraisal',{},'',{},10)
    assert len(calls)==1 and calls[0]['usage_status']=='unknown'
    assert calls[0]['outcome']=='owner-preempted'


def test_committed_diary_is_recallable_once_as_personal_reflection(setup):
    from kin_mind.continuity import ContinuityConfig
    from kin_mind.memory import MemoryContinuity
    from kin_mind.evidence_classes import owner_statement, self_statement
    from eventmem.core.models import RecallRequest
    from eventmem.core.retrieval import candidates
    mind, source, _ = setup
    sid = source('diary-authorisation')
    mind.configure_continuity(ContinuityConfig(command_id='continuity', agent_version='synthetic-v1',
        expected_revision=mind.read()['revision'], evidence_ids=[sid], features={'interpretation':True}, reason='test'))
    def exchange(request):
        return {'state':'complete','result':{'reason':'想记下来', 'understanding':{
            'meaning':'我想象一颗会唱歌的紫色陨石，想以后画下来。', 'topic':'紫色陨石',
            'basis':'internal_thought','confidence':0.8,'importance':70,'evidence_ids':[sid]}},
            'receipt':{'native_turn_id':'diary-turn','native_session_id':'same-main','model':'gpt-6-astra',
                       'provider':'custom','reasoning':'medium','usage':{'input_tokens':50,'output_tokens':30}}}
    jobs=Appraisals(mind);job=jobs.enqueue([sid],'synthetic-v1');out=jobs.run_one(native_provider(mind,exchange))
    assert out['state']=='complete',out
    with mind.engine.db.connect() as conn:
        result=json.loads(conn.execute('SELECT data FROM mind_appraisals WHERE id=?',(job['id'],)).fetchone()[0])['result']
    MemoryContinuity(mind).remember_reflection(result)
    with mind.engine.db.connect() as conn:
        rows=conn.execute("SELECT data FROM records WHERE json_extract(data,'$.attributes.host_event')='diary'").fetchall()
        assert len(rows)==1
        record=json.loads(rows[0][0])
        refs=mind._evidence(conn,[record['id']])
    assert record['confirmation']=='inferred' and record['generated']
    assert record['content'].startswith('小Kin自己琢磨的')
    assert record['attributes']['basis']=='internal_thought'
    assert not owner_statement(record,sources=refs)
    assert self_statement(record,sources=refs), 'a real reflection can support personality growth as Kin own thought'
    docs,_,_=candidates(mind.engine,RecallRequest(scope=mind.scope,query='紫色陨石',scenario='companion'),full_lexical=True)
    assert record['id'] in {doc['id'] for doc in docs}
    assert MemoryContinuity(mind).remember_reflection({'proposal':{}}) is None

def test_native_result_requires_actual_matching_profile_and_turn(setup):
    mind,_,_=setup
    p=native_provider(mind,lambda request:{'state':'complete','result':{},'receipt':{'model':'deepseek-flash','native_turn_id':'old'}})
    with attempts.collect(p) as calls:
        with pytest.raises(RuntimeError,match='native-review-unconfirmed'):p._native('submit_appraisal',{},'',{},10)
    assert calls[0]['outcome']=='native-review-unconfirmed'

def test_only_new_parts_of_frozen_input_reset_compression_stalls(setup):
    mind,_,_=setup
    jobs=Appraisals(mind);data={'error':'deepseek-evidence-compression-pending'}
    jobs._compression_wait(data,1)
    assert data['compression_stalls']==0
    # Foreign cache work cannot affect the progress supplied by this attempt.
    jobs._compression_wait(data,0)
    assert data['compression_stalls']==1

def test_native_context_pack_has_no_32000_cap(setup):
    mind,_,_=setup
    result=Contexts(mind).pack([{'id':'one','revision':1,'text':'完整资料。'*18000,'basis':'explicit'}],'',100000,allow_model=False)
    assert result['covered_ids']==['one'] and not result['omitted_ids']

def test_result_accepts_long_prose_but_not_two_json_objects():
    from kin_mind.exploration import Findings
    from kin_mind.exploration import AssistanceHint
    result={**FINDINGS,'summary':'一个完整的发现。'*1800}
    assert Findings.model_validate(result).summary==result['summary']
    assert AssistanceHint(action='一项有依据的行动。'*200,reason='缘由',completion='实际结果')
    assert codex_final_result(json.dumps(result)+'\n'+json.dumps(result)) is None

def test_exploration_text_repair_does_not_rerun_tools_and_citations_still_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "isolated-synthetic")
    body=THREAD_STARTED+"open(last,'w').write('{broken json')\nprint(json.dumps({'type':'turn.completed','usage':{'input_tokens':9,'output_tokens':4}}))\n"
    executable=fake_codex(tmp_path/'fake',body);calls=[]
    def repair(text,ledger,timeout):
        calls.append((text,timeout))
        return FINDINGS,{'model':'deepseek-flash','usage':None,'usage_status':'unknown'}
    out=run_codex(executable,TOPIC,tmp_path/'run',repair=repair,**codex_kwargs())
    assert out['state']=='complete',out
    assert len(calls)==1 and 0<calls[0][1]<=60
    def bad_repair(*args,**kwargs):return {**FINDINGS,'sources':[{'url':'https://unread.invalid','title':'unread'}]},{}
    out=run_codex(executable,TOPIC,tmp_path/'unbacked',repair=bad_repair,**codex_kwargs())
    assert out['state']=='failed' and out['reason']=='unbacked-citation'


def test_checkpoint_uses_native_room_and_waits_when_the_window_is_full(setup):
    from kin_mind.session_checkpoint import SessionCheckpoint
    mind, _, _ = setup
    checkpoint = SessionCheckpoint(mind)
    items = [{'id':str(i),'role':'user' if i%2==0 else 'assistant','text':'完整的聊天内容。'*300,'at':'2026-09-21T01:00:00Z','revision':1,'basis':'explicit','delivery':'accepted'} for i in range(8)]
    snapshot = {'configVersion':'synthetic-v1','cursors':{},'scope':mind.scope.model_dump(),'shared':{},'sourceRevisions':{},'items':items}
    binding = {'conversationId':'same','generation':1}
    ample = checkpoint.build(snapshot,binding,native_capacity=50000,adaptive_budget=True,allow_model=False)
    assert ample['complete'] and ample['budgetPlan']['effective']>2000
    full = checkpoint.build(snapshot,binding,native_capacity=300,adaptive_budget=True,allow_model=False)
    assert not full['complete'] and full['budgetPlan']['limit']==300


def test_native_failure_is_not_replayed_by_the_appraisal_queue(setup):
    mind, source, _ = setup
    calls=[]
    def exchange(request):
        calls.append(request)
        return {'state':'failed','receipt':{}}
    jobs=Appraisals(mind);jobs.enqueue([source('native-failure')],'synthetic-v1')
    result=jobs.run_one(native_provider(mind,exchange))
    assert result['state']=='needs-repair' and len(calls)==1
    assert jobs.run_one(native_provider(mind,exchange))['state']=='idle'
    assert len(calls)==1


def test_provider_outage_gets_only_three_queue_retries(setup):
    mind,_,_=setup; jobs=Appraisals(mind); data={'error':'deepseek-http-503'}
    assert [jobs._transient_failure(data) for _ in range(4)]==['pending','pending','pending','needs-repair']


def test_distinct_reflections_can_support_growth_but_rereading_one_cannot(setup):
    from types import SimpleNamespace
    from kin_mind.traits import Traits
    from eventmem.core.db import Conflict
    mind,_,_=setup; ledger=Traits(mind)
    first={'episode_key':'reflection-one','class':'self_statement'}
    second={'episode_key':'reflection-two','class':'self_statement'}
    ledger._lookup=lambda conn,refs,trait,cache: [first,second]
    decision=SimpleNamespace(basis='inference',episodes=[SimpleNamespace(ref='one'),SimpleNamespace(ref='two')])
    ledger._established(None,{'id':'trait'},decision,[first,second],{})
    ledger._lookup=lambda conn,refs,trait,cache: [first,first]
    with pytest.raises(Conflict):ledger._established(None,{'id':'trait'},decision,[first,first],{})


def test_uncertain_background_keeps_its_identity_without_blocking_fresh_context(setup):
    from kin_mind.context_delivery import ContextDelivery
    mind,_,_=setup;contexts=Contexts(mind);delivery=ContextDelivery(contexts)
    epoch=contexts.window('same-main')['epoch']
    old=delivery.prepare('same-main',epoch,'owner-one','earlier optional background',[])
    delivery.begin('same-main',epoch,old['id']);delivery.uncertain('same-main',epoch,old['id'])
    new=delivery.prepare('same-main',epoch,'owner-two','current optional background',[])
    assert delivery.begin('same-main',epoch,new['id'])['state']=='sending'
    assert delivery.begin('same-main',epoch,old['id'])['state']=='unconfirmed'
