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


def test_a_deferred_fork_is_a_wait_that_keeps_what_it_used(setup):
    """WS4: a fork that timed out is deferred, never failed or retried in the main thread; its
    receipt's usage and fork thread are recorded (PROBE)."""
    mind,_,_=setup
    spent={'model':'gpt-6-sol','native_turn_id':'turn-9','fork_thread_id':'fork-3','usage':{'input_tokens':1200,'output_tokens':40}}
    p=native_provider(mind,lambda request:{'state':'waiting','reason':'fork-timeout','model_invoked':True,'receipt':spent})
    with attempts.collect(p) as calls:
        with pytest.raises(ModelAdmissionWait,match='fork-timeout'):p._native('submit_appraisal',{},'',{},10)
    assert calls[0]['outcome']=='fork-timeout' and calls[0]['request_id']=='turn-9'
    assert calls[0]['usage_status']!='unknown' and calls[0]['detail']=={'fork_thread_id':'fork-3'}


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
    assert record['content'].startswith('Kin 自己的想法')
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


def test_checkpoint_budget_is_fixed_and_does_not_shrink_with_the_window(setup):
    # K3-05, T-22: the checkpoint is for the window after compaction. A full window
    # used to leave it no room at all, so the compaction that needed it never started.
    from kin_mind.context import Compression
    from kin_mind.session_checkpoint import SessionCheckpoint
    mind, _, _ = setup
    checkpoint = SessionCheckpoint(mind)
    binding = {'conversationId':'same','generation':1}
    def snapshot(repeat):
        items = [{'id':str(i),'role':'user' if i%2==0 else 'assistant','text':'完整的聊天内容。'*repeat,'at':'2026-09-21T01:00:0%dZ' % i,'revision':1,'basis':'explicit','delivery':'accepted'} for i in range(8)]
        return {'configVersion':'synthetic-v1','cursors':{},'scope':mind.scope.model_dump(),'shared':{},'sourceRevisions':{},'items':items}
    built = checkpoint.build(snapshot(40),binding,adaptive_budget=True,allow_model=False)
    assert built['complete'] and 2000 < built['budgetPlan']['effective'] <= 8000 and built['budgetPlan']['limit'] == 8000
    assert built['tokens'] <= built['budgetPlan']['effective'] and [i['id'] for i in built['items']] == [str(i) for i in range(8)]
    # Four long exchanges exceed the ceiling: the latest keep their words, the rest wait
    # for a sourced summary instead of pushing the checkpoint past its allowance.
    long = checkpoint.build(snapshot(300),binding,adaptive_budget=True,allow_model=False)
    assert not long['complete'] and long['tokens'] <= 8000 and long['items'][-1]['id'] == '7'
    class Summaries:
        model = 'synthetic-summary'
        def structured(self, name, schema, system, payload, **kwargs):
            entries = [{'item_ids':[i['id']], 'summary':'第%s条的要点。' % i['id']} for i in payload['items']]
            return Compression.model_validate({'entries':entries,'omitted_ids':[]}), {'model':self.model}
    summarized = checkpoint.build(snapshot(300),binding,adaptive_budget=True,provider=Summaries())
    assert summarized['complete'] and summarized['tokens'] <= summarized['budgetPlan']['effective'] <= 8000
    assert summarized['items'][0]['id'].startswith('summary:') and summarized['items'][-1]['text'] == '完整的聊天内容。'*300
    with pytest.raises(TypeError):
        checkpoint.build(snapshot(40),binding,native_capacity=300,adaptive_budget=True,allow_model=False)
    with pytest.raises(ValueError):
        checkpoint.build(snapshot(40),binding,budget=9000,adaptive_budget=True,allow_model=False)


def test_checkpoint_identity_ignores_its_allowance(setup):
    # K3-09: the same content is the same checkpoint, so a prepared candidate is not
    # judged stale and rebuilt because a budget number moved.
    from kin_mind.session_checkpoint import SessionCheckpoint
    mind, _, _ = setup
    checkpoint = SessionCheckpoint(mind)
    items = [{'id':str(i),'role':'user' if i%2==0 else 'assistant','text':'短句。','at':'2026-09-21T01:00:00Z','revision':1,'basis':'explicit','delivery':'accepted'} for i in range(8)]
    snapshot = {'configVersion':'synthetic-v1','cursors':{},'scope':mind.scope.model_dump(),'shared':{},'sourceRevisions':{},'items':items}
    binding = {'conversationId':'same','generation':1}
    small = checkpoint.build(snapshot,binding,budget=2000,adaptive_budget=True,allow_model=False)
    large = checkpoint.build(snapshot,binding,budget=4000,adaptive_budget=True,allow_model=False)
    assert small['budgetPlan'] != large['budgetPlan'] and small['id'] == large['id']
    assert 'budgetPlan' not in small['payload'] and small['payload'] == large['payload']


def test_a_failed_native_assessment_waits_and_is_tried_again(setup):
    """K1-01: a main-session assessment that did not complete ran in an ephemeral read-only fork.
    It is retried after a backoff, not set aside at the first failure."""
    mind, source, _ = setup
    calls=[]
    def exchange(request):
        calls.append(request)
        return {'state':'failed','receipt':{}}
    jobs=Appraisals(mind);job=jobs.enqueue([source('native-failure')],'synthetic-v1')
    result=jobs.run_one(native_provider(mind,exchange))
    assert result['state']=='pending' and result['transient_failures']==1 and len(calls)==1
    assert jobs.run_one(native_provider(mind,exchange))['state']=='idle'
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?",(job['id'],))
    assert jobs.run_one(native_provider(mind,exchange))['state']=='pending' and len(calls)==2



def test_a_fork_without_a_finished_turn_waits_uncharged(setup):
    """PROBE: with no finished turn to fork from, the assessment cannot run now. It waits and is
    asked again; it is not a failure, not charged, and not sent into the main thread instead."""
    mind, source, _ = setup
    calls = []
    def exchange(request):
        calls.append(request)
        return {'state': 'failed', 'reason': 'no-completed-turn', 'receipt': {}}
    jobs = Appraisals(mind)
    job = jobs.enqueue([source('fork-too-early')], 'synthetic-v1')
    result = jobs.run_one(native_provider(mind, exchange))
    assert result['state'] != 'needs-repair' and len(calls) == 1
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT state,attempts,data FROM mind_appraisals WHERE id=?", (job['id'],)).fetchone()
    assert row['state'] == 'pending' and row['attempts'] == 0
    assert not json.loads(row['data']).get('transient_failures')

def test_provider_outage_is_retried_for_about_two_hours(setup):
    """K1-08: eight retries at 1, 2, 4, 8, 16, 30, 30 and 30 minutes, then the row is set aside."""
    mind,_,_=setup; jobs=Appraisals(mind); data={'error':'deepseek-http-503'}
    assert [jobs._transient_failure(data) for _ in range(9)]==['pending']*8+['needs-repair']


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


def test_generated_internal_envelopes_do_not_become_public_memory():
    from kin_mind.dialogue import is_public_dialogue
    body = "kin-context:context:invented-id\n共享记忆资料"
    assert not is_public_dialogue({"kind": "delivery", "state": "accepted", "text": body})
    assert not is_public_dialogue({"kind": "assistant-message", "text": "</｜｜DSML｜｜ invoke>"})
    assert is_public_dialogue({"kind": "owner-message", "text": body})
    assert is_public_dialogue({"kind": "assistant-message", "text": "解释 `kin-context:context:id` 是什么。"})


def test_kin_chooses_when_to_think_again_within_a_day(setup):
    """N8: the quiet review is Kin's choice, ten minutes to a day; one past either end is taken
    to that end, never a failed assessment, and the stored 20..120 range no longer narrows it."""
    from datetime import timedelta
    from kin_mind.appraisal import SYSTEM
    from kin_mind.memory import MemoryContinuity
    from kin_mind.state import timestamp
    mind, _, _ = setup
    assert [Appraisal(reason='r', next_review_minutes=m).next_review_minutes for m in (5, 600, 3000)] == [10, 600, 1440]
    assert '10到1440' in SYSTEM
    memory = MemoryContinuity(mind)
    with mind.engine.db.connect(write=True) as conn:
        memory.commit_action(conn, [], 'event-n8', 600, {})
        row = conn.execute('SELECT next_review FROM mind_action_schedule WHERE scope=?', (mind.scope.key(),)).fetchone()
    assert timestamp(row['next_review']) - timestamp(mind.clock()) >= timedelta(minutes=599)


def test_contact_waits_reach_three_days_and_are_clamped_not_refused():
    """N9: a wait of up to 72 hours is kept; one past either end is taken to that end."""
    from kin_mind.appraisal import WishUpdate
    from kin_mind.state import ContactDecision
    assert ContactDecision(action='wait', reason='r', condition='time', retry_after_seconds=10**6).retry_after_seconds == 259200
    update = lambda seconds: WishUpdate(desire_id='d', action='wait', reason='r', wait_condition='time',
                                        retry_after_seconds=seconds).retry_after_seconds
    assert [update(100), update(172800), update(10**7)] == [300, 172800, 259200]


def idle_world(setup):
    """Operational lanes with an action policy; the bootstrap review is taken as done."""
    from datetime import timedelta
    from kin_mind.actions import ActionEvents
    from kin_mind.memory import MemoryContinuity
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "idle": True, "operational_lanes": True})
    actions = ActionEvents(mind)
    actions.configure({"command_id": "policy", "agent_version": "synthetic-v1", "expected_revision": mind.read()["revision"],
                       "evidence_ids": [source("policy")], "reason": "The owner allowed autonomous review"})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_action_events SET state='complete' WHERE kind='bootstrap'")
    clock[0] += timedelta(minutes=30)
    assert memory.queue_idle(actions)
    return mind, memory, actions, clock


def test_an_idle_review_set_aside_closes_its_event_and_schedules_the_next(setup):
    """K4-19 and K1-01: a job that ends needs-repair closes its event instead of holding a drain
    slot as `queued`, and the idle clock moves on however the idle review ended."""
    from kin_mind.state import timestamp
    mind, memory, actions, clock = idle_world(setup)
    jobs = Appraisals(mind)
    actions.drain(jobs)
    with mind.engine.db.connect(write=True) as conn:
        event = conn.execute("SELECT id,data FROM mind_action_events WHERE kind='idle-review'").fetchone()
        job_id = json.loads(event["data"])["job_id"]
        conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE id=?", (job_id,))
    actions.drain(jobs)
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM mind_action_events WHERE id=?", (event["id"],)).fetchone()[0] == "needs-review"
        data = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()[0])
    jobs._reschedule_idle(data, memory.settings())
    assert memory.due() is None
    with mind.engine.db.connect() as conn:
        after = conn.execute("SELECT next_review FROM mind_action_schedule WHERE scope=?", (mind.scope.key(),)).fetchone()[0]
    assert timestamp(after) > timestamp(mind.clock())


def test_a_timer_only_review_makes_no_enrichment_call(setup):
    """K1-07: an idle review whose only evidence is the host's own timer event has nothing to
    organise, so no enrichment appraisal is queued after it."""
    from test_kin_mind import FakeReviewer
    mind, memory, actions, clock = idle_world(setup)
    jobs = Appraisals(mind)
    actions.drain(jobs)
    reviewer = FakeReviewer(Appraisal(reason="Nothing new, rest a while", next_review_minutes=240))
    assert jobs.run_one(reviewer, lane="action")["state"] == "complete"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_appraisals WHERE id LIKE 'enrich_%'").fetchone()[0] == 0
    assert memory.due() is None


def test_the_assessment_budget_leaves_out_what_the_main_session_holds(setup):
    mind, _, _ = setup
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,data) VALUES('models',?)",
                     (json.dumps({"summary": {"endpoint": "https://api.deepseek.com", "api_key_env": "SYNTHETIC_KEY"}}),))
    profile = {'model':'gpt-6-astra','modelProvider':'custom','reasoningEffort':'medium','fastMode':'off',
               'modelContextWindow':128000,'outputReserve':16000,'toolReserve':8000}
    full = NativeReview.from_engine(mind.engine, profile=profile, exchange=lambda r: r)
    held = NativeReview.from_engine(mind.engine, profile=profile, exchange=lambda r: r, used_tokens=60000)
    assert full.input_budget == 104000 and held.input_budget == 44000


def test_the_assessment_frame_keeps_contract_context_and_schema_apart(setup):
    """The request for `_kin/assess`: the standing contract (instructions and fixed definitions),
    the dynamic context, and the schema object, which is never pasted into the input."""
    mind, source, _ = setup
    frames = []
    def exchange(request):
        frames.append(request)
        return {'state':'complete','result':{'reason':'Nothing new.'},
                'receipt':{'native_turn_id':'turn-1','native_session_id':'same-main','model':'gpt-6-astra','provider':'custom','reasoning':'medium','usage':{}}}
    jobs = Appraisals(mind)
    jobs.enqueue([source('frame')], 'synthetic-v1')
    assert jobs.run_one(native_provider(mind, exchange))['state'] == 'complete'
    frame = frames[0]
    assert isinstance(frame['schema'], dict) and frame['schema'].get('properties')
    assert '维度定义' in frame['contract'] and 'definitions' not in frame['context']
    assert 'next_review_minutes' not in frame['contract'].split('维度定义')[1]
    assert frame['system'] == frame['contract']



def test_an_idle_assessment_is_told_it_may_start_something(setup):
    """K1-05: the contract no longer claims the host raises longing over time, the closing line
    invites a new thought instead of "leave it empty and move the review", and the install-time
    provider label is not shown as the model deciding."""
    mind, source, _ = setup
    frames = []
    def exchange(request):
        frames.append(request)
        return {'state':'complete','result':{'reason':'Nothing new.'},
                'receipt':{'native_turn_id':'turn-1','native_session_id':'same-main','model':'gpt-6-astra','provider':'custom','reasoning':'medium','usage':{}}}
    jobs = Appraisals(mind)
    jobs.enqueue([source('idle')], 'synthetic-v1')
    assert jobs.run_one(native_provider(mind, exchange))['state'] == 'complete'
    contract = frames[0]['contract']
    assert '自主起念的机会' in contract and '更新复核时间即可' not in contract
    assert '确定性公式' not in contract and '不会让想念或主动随时间自己上升' in contract
    policy = frames[0]['context'].get('state', {}).get('action_policy') or {}
    assert 'provider' not in policy and 'reasoning' not in policy

def test_evidence_the_fork_read_with_its_tools_may_be_cited(setup):
    """K1-16: an id the request did not supply is accepted when the turn's tool receipts show a
    completed read and the record already existed; without a tool read it is not."""
    mind, source, _ = setup
    earlier = source('read-by-tool')
    jobs = Appraisals(mind)
    class Proposal:
        def model_dump(self):
            return {'understanding': {'evidence_ids': [earlier]}}
    from datetime import datetime, timedelta, timezone
    started = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    with_tools = {'native_receipt': {'tool_calls': [{'name': 'read_memory', 'ok': True}]}}
    fetched = jobs._tool_fetched(Proposal(), with_tools, {}, started)
    assert any(ref['source_id'] == earlier for ref in fetched.values())
    assert jobs._tool_fetched(Proposal(), {'native_receipt': {'tool_calls': []}}, {}, started) == {}
    before = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert jobs._tool_fetched(Proposal(), with_tools, {}, before) == {}


def test_the_decision_runtime_is_read_from_the_committed_schedule(setup):
    """K1-11: the latest decision's runtime comes from its one schedule row, not a sort of every appraisal."""
    from test_kin_mind import FakeReviewer
    mind, memory, actions, clock = idle_world(setup)
    jobs = Appraisals(mind)
    actions.drain(jobs)
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Rest a while", next_review_minutes=240)), lane="action")["state"] == "complete"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_action_schedule SET data=json_set(data,'$.receipt.model','schedule-marker') WHERE scope=?",
                     (mind.scope.key(),))
        recent = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM mind_action_events WHERE scope=? ORDER BY created_at DESC,id DESC LIMIT 8",
                              (mind.scope.key(),)).fetchall()
    assert mind.read()["decision_runtime"]["model"] == "schedule-marker"
    assert "mind_action_event_recent" in " ".join(str(r[-1]) for r in recent)


def test_sixty_quiet_minutes_hold_as_many_assessments_as_kin_asked_for(setup):
    """T-02: an hour of ticks with no owner message assesses only when Kin's own next review comes
    due (never more often than the ten-minute floor), each with a bounded input."""
    from datetime import timedelta
    from test_kin_mind import FakeReviewer
    mind, memory, actions, clock = idle_world(setup)
    jobs = Appraisals(mind)
    sizes = []

    class Measuring(FakeReviewer):
        def appraise(self, context):
            sizes.append(len(json.dumps(context, ensure_ascii=False, default=str)))
            return super().appraise(context)

    reviewer = Measuring(Appraisal(reason="Nothing new; look again in a while", next_review_minutes=10))
    for _minute in range(60):
        memory.queue_idle(actions)
        actions.drain(jobs)
        jobs.run_one(reviewer, lane="action")
        clock[0] += timedelta(minutes=1)
    assert 1 <= reviewer.calls <= 7
    assert max(sizes) < 40000
