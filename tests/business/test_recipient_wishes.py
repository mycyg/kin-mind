"""One affect state and memory scope, independent recipient execution ledgers."""
from datetime import timedelta
import pytest
from eventmem.core.db import Missing
from kin_mind.companion_view import CompanionView, companion_scope
from kin_mind.companions import XIAOGUANG as X, YIYI as Y
from kin_mind.memory import MemoryContinuity
from kin_mind.plans import AutonomousPlans
from kin_mind.appraisal import Appraisals
from kin_mind import recipient, expression_intent
from kin_mind.state import DesireChange
from test_kin_mind import setup, wish, event

def view(mind,p):
    return companion_scope(CompanionView(mind.engine,mind.scope,p,'synthetic:'+p))

def test_same_wish_key_independent_claim_settle_and_shared_emotion(setup):
    mind,source,clock=setup
    wish(mind,source,'same-key');x=mind.read()['desires'][0]
    with view(mind,Y):
        wish(mind,source,'same-key');y=mind.read()['desires'][0]
        assert x['id']!=y['id'] and y['recipient_person_id']==Y
        ya=mind.claim_contact(owner_epoch='y-1')
    xa=mind.claim_contact(owner_epoch='x-1')
    assert xa['desire_ids']==[x['id']] and ya['desire_ids']==[y['id']]
    with pytest.raises(Missing):mind.settle_contact(attempt_id=ya['id'],state='canceled')
    mind.settle_contact(attempt_id=xa['id'],state='canceled',decision={'action':'wait','reason':'wait for X','condition':'owner_reply'})
    with view(mind,Y):
        mind.settle_contact(attempt_id=ya['id'],state='canceled',decision={'action':'wait','reason':'wait for Y','condition':'owner_reply'})
    mind.reconsider_contacts(owner_epoch='x-2')
    with view(mind,Y):
        assert mind.read()['desires'][0]['status']=='waiting'
        assert mind.reconsider_contacts(owner_epoch='y-1')['resumed']==[]
    mind.record(event(mind,source,'mood-change',{'mood':25}))
    xstate=mind.read()
    with view(mind,Y):
        ystate=mind.read()
        assert ystate['dimensions']==xstate['dimensions']
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mind_state').fetchone()[0]==1
        assert len(mind._load(conn)['desires'])==2

def test_same_plan_key_and_schedules_survive_restart_separately(setup):
    mind,source,clock=setup;memory=MemoryContinuity(mind);memory.configure({'semantic_actions':True,'autonomous_plans':True,'operational_lanes':True,'idle':True,'semantic':True})
    evidence=source('shared-plan-basis')
    def create():
        return AutonomousPlans(mind).manage({'action':'create','command_id':'same-plan','key':'same-plan','goal':'check in','motivation':'care','reason':'source','evidence_ids':[evidence],'steps':[{'id':'contact','actor':'contact','goal':'ask how the day went','completion':'accepted'}]})
    x=create()
    with view(mind,Y):
        y=create();assert y['id']!=x['id'];assert [p['id'] for p in AutonomousPlans(mind).read()['plans']]==[y['id']]
        with pytest.raises(Missing):AutonomousPlans(mind).read(x['id'])
        with mind.engine.db.connect(write=True) as conn:memory.commit_action(conn,[],'y-event',123,{})
    with mind.engine.db.connect(write=True) as conn:
        memory.commit_action(conn,[],'x-event',30,{})
        xc=recipient.cursor(conn,mind.scope.key(),'mind_action_schedule')
    with view(mind,Y):
        with mind.engine.db.connect() as conn:yc=recipient.cursor(conn,mind.scope.key(),'mind_action_schedule')
        assert yc['next_review']!=xc['next_review']
        assert yc['minutes']==123
    assert [p['id'] for p in AutonomousPlans(mind).read()['plans']]==[x['id']]

def test_appraisal_queue_and_legacy_defaults_do_not_cross(setup):
    mind,source,clock=setup;basis=source('one-source');jobs=Appraisals(mind)
    x=jobs.enqueue([basis],'synthetic-v1')
    with view(mind,Y):
        y=jobs.enqueue([basis],'synthetic-v1');assert x['id']!=y['id']
        assert [j['id'] for j in jobs.status()]==[y['id']]
    assert [j['id'] for j in jobs.status()]==[x['id']]
    wish(mind,source,'legacy')
    with mind.engine.db.connect(write=True) as conn:
        state=mind._load(conn);d=next(iter(state['desires'].values()));at=d['created_at'];d.pop('recipient_person_id');mind._save(conn,state)
    with view(mind,Y):assert mind.read()['desires']==[]
    assert mind.read()['desires'][0]['created_at']==at

def test_actual_appraisal_claims_only_own_queue_and_emits_own_wish(setup):
    from kin_mind.appraisal import Appraisal, Wish
    from test_kin_mind import FakeReviewer
    mind,source,clock=setup;jobs=Appraisals(mind);basis=source('same-assessment')
    x=jobs.enqueue([basis],'synthetic-v1')
    with view(mind,Y):
        y=jobs.enqueue([basis],'synthetic-v1')
        result=jobs.run_one(FakeReviewer(Appraisal(values={'mood':40},reason='synthetic mood',wishes=[Wish(kind='contact',content='Ask Y about her day',topic='day',strength=90,ttl_hours=24,completion='accepted') ])))
        assert result['state']=='complete'
        assert len(mind.read()['desires'])==1
        assert mind.read()['desires'][0]['recipient_person_id']==Y
    assert jobs.status(x['id'])['state']=='pending'
    assert mind.read()['desires']==[]
    assert mind.read()['dimensions']['mood']['value']==40

def test_contact_words_intents_and_last_interaction_do_not_override_other_person(setup):
    from types import SimpleNamespace
    from eventmem.core.models import SourceInput
    from kin_mind.appraisal import ExpressionIntent
    mind,source,clock=setup
    def preferences(label):
        mind.configure_contact_frequency({'expected_revision':mind.read()['revision'],'command_id':'freq-'+label,'agent_version':'v1','reason':'explicit','evidence_ids':[source('frequency-'+label)]})
        with mind.engine.db.connect(write=True) as conn:
            expression_intent.commit_intent(SimpleNamespace(mind=mind,conn=conn,state=mind._load(conn),sources=[],value=ExpressionIntent(stance=label),event_id='intent-'+label,job_id='job-'+label,receipt={},version='v1',stimulus='synthetic'))
    preferences('X')
    with view(mind,Y):preferences('Y')
    before=mind.clock();clock[0]+=timedelta(minutes=1)
    for person,at in [(X,before),(Y,mind.clock())]:
        mind.engine.receive(SourceInput(namespace='kin-companion-input',key='timing-'+person,scope=mind.scope,text='synthetic',authority='explicit',occurred_at=at,extract=False,metadata={'recipient_person_id':person,'speaker_person_id':person}))
    for person,label,at in [(X,'X',before),(Y,'Y',mind.clock())]:
        with view(mind,person):
            assert mind.read()['interaction_timing']['last_owner_message_at']==at
            with mind.engine.db.connect() as conn:
                assert recipient.preferences(mind._load(conn))['contact_frequency']['text']=='frequency-'+label
                assert expression_intent.current(conn,mind,mind.clock())['stance']==label

def test_y_exploration_runs_as_kin_and_keeps_y_wish_origin_without_consuming_x(setup,tmp_path):
    from kin_mind.exploration import Explorations
    from kin_mind.companions import Companions
    mind,source,clock=setup
    MemoryContinuity(mind).configure({'records':True,'semantic':True,'operational_lanes':True})
    wish(mind,source,'x-explore',kind='explore',content='X question',completion='sourced answer')
    xid=mind.read()['desires'][0]['id']
    with view(mind,Y):
        basis=source('y-question')
        wish(mind,source,'y-explore',kind='explore',content='Y question',completion='sourced answer',evidence_ids=[basis])
        yid=mind.read()['desires'][0]['id']
        def run(*args,**kwargs):
            return {'state':'complete','partial':False,'attempt':1,'result':{'summary':'Synthetic finding','findings':['Synthetic observation'],'sources':[{'url':'memory://'+basis,'title':'Synthetic source'}],'open_questions':[],'suggested_share':None}}
        result=Explorations(mind).run('synthetic',str(tmp_path/'explore'),'v1',runner=run)
        assert result['state']=='complete'
        assert next(d for d in mind.read()['desires'] if d['id']==yid)['status']=='completed'
        with mind.engine.db.connect() as conn:
            annotation=Companions(mind.engine,mind.scope).annotation(conn,result['source_id'])
        assert annotation['speaker_person_id']=='agent:kin'
        assert annotation['recipient_person_id']==Y
    assert next(d for d in mind.read()['desires'] if d['id']==xid)['status']=='wanted'

def test_shared_native_can_choose_group_wish_and_time_plan_without_new_message_or_send(setup):
    from kin_mind.companion_host import dispatch_companion
    from kin_mind.companions import Companions
    from datetime import datetime, timezone
    mind,source,clock=setup;group='group:family';config={'scope':mind.scope.model_dump(),'agent_version':'synthetic-v1'}
    expiry=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    request={'person_id':group,'conversation_key':'chat:family','kind':'wish','origin_source_id':'synthetic-origin',
        'change':{'command_id':'kin-chooses-family','action':'create','expected_revision':mind.read()['revision'],'subjective':True,'content':'想在家里聊一个奇怪的发现','topic':'有趣新话题','kind':'contact','strength':75,'expires_at':expiry,'completion':'群里实际收到'}}
    result=dispatch_companion(mind.engine,config,'companion-autonomy',request)
    with view(mind,group):
        desires=mind.read()['desires'];assert len(desires)==1 and desires[0]['recipient_person_id']==group
    assert not mind.read()['desires']
    secret=source('private-secret')
    Companions(mind.engine,mind.scope).annotate(source_id=secret,speaker_person_id=X,conversation_key='chat:x',command_id='private-tag',evidence_id='private-tag')
    request['change'].update(command_id='forbidden-group-basis',expected_revision=mind.read()['revision'],evidence_ids=[secret])
    dispatch_companion(mind.engine,config,'companion-autonomy',request)
    shown=Companions(mind.engine,mind.scope).source_view(secret,group)
    assert 'classification' not in shown and shown['speaker_person_id']==X
    assert 'conversation_key' not in shown and shown['conversation_kind']=='p2p'

def test_one_appraisal_can_choose_two_recipients_without_duplicate_content_suppression(setup):
    from kin_mind.appraisal import Appraisal, Wish
    from test_kin_mind import FakeReviewer
    mind,source,clock=setup
    mind.unified_targets={Y:{'route':{'conversation_key':'chat:y'}},'group:family':{'route':{'conversation_key':'chat:family'}}}
    Appraisals(mind).enqueue([source('unified-heartbeat')],'synthetic-v1')
    proposal=Appraisal(values={'mood':63},reason='synthetic',wishes=[Wish(kind='contact',content='同一好玩的发现',topic='day',strength=90,ttl_hours=24,completion='accepted',recipient_person_id=p,evidence_ids=[]) for p in (Y,'group:family')])
    result=Appraisals(mind).run_one(FakeReviewer(proposal))
    assert result['state']=='complete', result
    for person in (Y,'group:family'):
        with view(mind,person):
            state=mind.read();assert state['dimensions']['mood']['value']==63
            assert len(state['desires'])==1 and state['desires'][0]['recipient_person_id']==person
    assert not mind.read()['desires']

def test_activity_sharing_defer_affects_only_target_and_source_labels_remain_facts(setup):
    from types import SimpleNamespace
    from test_exploration_decision_archive import explored
    from kin_mind.exploration_decisions import SharingDecision, apply_decisions, decision_key
    from kin_mind.companions import Companions
    mind,source,clock=setup;sid,_=explored(mind,clock,'shared-finding')
    m=Companions(mind.engine,mind.scope)
    m.annotate(source_id=sid,speaker_person_id='agent:kin',conversation_key='kin:explore',command_id='tag-shared',evidence_id='synthetic')
    ev=SimpleNamespace(evidence_ids=[sid],command_id='share',agent_version='v1')
    with mind.engine.db.connect(write=True) as conn:
        state=mind._load(conn)
        for p in (X,Y):
            state['desires'][p]={'recipient_person_id':p,'exploration_id':'shared-finding','status':'wanted','revision':1}
        apply_decisions(mind,conn,state,[SharingDecision(exploration_id='shared-finding',recipient_person_id=p,decision='share') for p in (X,Y)],ev,{},'exploration-result')
        ev.command_id='defer-y'
        apply_decisions(mind,conn,state,[SharingDecision(exploration_id='shared-finding',recipient_person_id=Y,decision='defer',reconsider_when='later')],ev,{},'idle-review')
        assert state['desires'][X]['status']=='wanted' and state['desires'][Y]['status']=='waiting'
        assert state['exploration_decisions'][decision_key('shared-finding',X)]['decision']=='share'
        assert state['exploration_decisions'][decision_key('shared-finding',Y)]['decision']=='defer'
    m.annotate(source_id=sid,speaker_person_id='agent:kin',conversation_key='kin:explore',command_id='tag-private',evidence_id='synthetic',expected_revision=1)
    with mind.engine.db.connect(write=True) as conn:
        state=mind._load(conn)
        apply_decisions(mind,conn,state,[SharingDecision(exploration_id='shared-finding',recipient_person_id='group:family',decision='share')],ev,{},'idle-review')
        assert state['exploration_decisions'][decision_key('shared-finding','group:family')]['decision']=='share'
    assert m.source_view(sid,'group:family')['speaker_person_id']=='agent:kin'

def test_unified_minute_queues_one_idle_wake_but_ticks_all_recipient_plans(setup,monkeypatch,tmp_path):
    from kin_mind.host import dispatch
    from kin_mind.actions import ActionEvents
    mind,source,clock=setup;idle=[];plans=[];crossings=[]
    monkeypatch.setattr(MemoryContinuity,'queue_idle',lambda *a:idle.append(recipient.current()))
    monkeypatch.setattr(AutonomousPlans,'tick',lambda *a:plans.append(recipient.current()))
    monkeypatch.setattr(ActionEvents,'crossings',lambda *a:crossings.append(recipient.current()))
    config={'root':str(tmp_path),'scope':mind.scope.model_dump(),'agent_version':'synthetic-v1','companion':{'shared_session':True},'exploration_stop_file':str(tmp_path/'stop'),'exploration_directory':str(tmp_path/'jobs')}
    for person in (X,Y,'group:family'):
        with view(mind,person):dispatch(config,'review-due',{})
    assert idle==[X] and crossings==[X]
    assert plans==[X,Y,'group:family']

def test_one_snapshot_has_separate_agendas_and_preserves_shared_affect(setup):
    from kin_mind.companion_host import dispatch_companion
    mind,source,clock=setup
    wish(mind,source,'x-next',content='X question')
    with view(mind,Y):wish(mind,source,'y-next',content='Y question')
    routes={p:{'conversation_key':'chat:'+p} for p in (X,Y,'group:family')}
    result=dispatch_companion(mind.engine,{'scope':mind.scope.model_dump()},'companion-targets',{'routes':routes})
    assert [x['content'] for x in result[X]['wishes']]==['X question']
    assert [x['content'] for x in result[Y]['wishes']]==['Y question']
    assert not result['group:family']['wishes']
    assert all(not x['sources'] for x in result.values())
