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
