import json
import pytest
from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing
from eventmem.core.models import Scope, SourceInput, CompanionDelivery, ContactPolicy
from eventmem.core.contact_tasks import ContactTaskInput
from kin_mind.companions import Companions, CompanionTasks, RecipientPreferences, XIAOGUANG as X, YIYI as Y
from kin_mind.companion_host import dispatch_companion
SCOPE=Scope(persona='duo-synthetic');AT='2026-09-01T05:00:00Z'
@pytest.fixture
def world(tmp_path):
    e=Engine(tmp_path/'memory');m=Companions(e,SCOPE)
    def source(key,person=X,text='一起散步',parents=None):
        sid=e.receive(SourceInput(namespace='kin-owner-input',key=key,scope=SCOPE,text=text,occurred_at=AT,extract=False),derived_from=parents)['id']
        m.annotate(source_id=sid,speaker_person_id=person,conversation_key='chat:'+str(person),command_id=key,evidence_id='synthetic:'+key,participants=[{'person_id':Y,'role':'companion'}])
        return sid
    return e,m,source

def test_times_speaker_participants_and_protected_derivation(world):
    e,m,source=world;s=source('walk');v=m.source_view(s,Y)
    assert v['occurred_at'].startswith('2026-09-01') and v['speaker_person_id']==X and v['participants'][0]['person_id']==Y
    private=source('private',text='私密的事');derived=source('summary',parents=[private])
    assert m.source_view(private,X)
    for sid in [private,derived]:
        assert m.source_view(sid,Y)['speaker_person_id']==X
    old=e.receive(SourceInput(namespace='kin-owner-input',key='old',scope=SCOPE,text='旧事',occurred_at=AT,extract=False))['id']
    p=m.history_preview([old])[0];assert p['speaker_person_id'] is None and p['candidate_speaker']==X
    assert m.source_view(old,Y)['speaker_person_id'] is None
    assert e.source(old)['occurred_at'].startswith('2026-09-01')

def test_private_reply_visible_only_to_original_recipient(world):
    e,m,source=world;s=source('private-y',Y)
    reply=e.receive(SourceInput(namespace='kin-companion-output',key='reply',scope=SCOPE,text='收到',extract=False),derived_from=[s])['id']
    m.annotate(source_id=reply,speaker_person_id='agent:kin',recipient_person_id=Y,conversation_key='chat:'+Y,command_id='reply',evidence_id='om_reply')
    assert m.source_view(reply,Y)
    assert m.source_view(reply,X)['speaker_person_id']=='agent:kin'

def test_preferences_relations_separate_silence_not_dislike(world):
    e,m,source=world;a=source('x',text='我喜欢天文');b=source('y',Y,text='我不喜欢天文')
    m.update_relationship(X,values={'care':75},evidence_ids=[a],expected_revision=0,command_id='rel-x');assert m.relationship(Y)['values']=={}
    prefs=RecipientPreferences(m);prefs.observe(person=X,source_id=a,topic='天文',signal='like',quote='喜欢天文');prefs.observe(person=Y,source_id=b,topic='天文',signal='dislike',quote='不喜欢天文')
    assert prefs.view(X,AT)[0]['score']==4 and prefs.view(Y,AT)[0]['score']==-4
    assert prefs.observe(person=X,source_id=a,topic='游戏',signal='silence')['state']=='no-signal'
    with pytest.raises(Conflict):prefs.observe(person=Y,source_id=a,topic='天文',signal='like',quote='喜欢天文')

@pytest.mark.parametrize("mode", ["text", "voice", "both"])
def test_cross_person_task_generation_cancel_response(world, mode):
    e,m,source=world;s=source('request',text='提醒一一喝水');response=source('response',Y,text='喝过了')
    tasks=CompanionTasks(e,SCOPE,['duo']);tasks.tasks.scheduler.policy(ContactPolicy(id='duo',scope=SCOPE,enabled=True,channel='http://127.0.0.1:9',quiet_start=0,quiet_end=0,max_per_day=100,min_interval_minutes=0,require_confirmation=False))
    route=CompanionDelivery(mode=mode,channel='feishu',person_id=Y,app_id='cli_fake',open_id='ou_y',chat_id='oc_y',conversation_key='feishu:cli_fake:oc_y',binding_revision=1,requester_person_id=X,request_conversation_key='chat:'+X,source_input_id=s)
    c=tasks.create(ContactTaskInput(command_id='task',policy_id='duo',title='喝水',text='小光提醒你喝水',basis=s,due_at='2030-01-01T05:00:00Z',companion_delivery=route),actor=X)
    check={'schedule_id':c['id'],'schedule_generation':0,'companion_delivery':route.model_dump()}
    assert dispatch_companion(e,{'scope':SCOPE.model_dump()},'companion-delivery-check',check)['state']=='current'
    assert tasks.respond(c['id'],actor=Y,source_id=response,status='completed')['actor']==Y
    with pytest.raises(Conflict):tasks.respond(c['id'],actor=X,source_id=s,status='completed')
    tasks.change(c['id'],actor=Y,source_id=response,expected_revision=c['revision'],action='cancel')
    with pytest.raises(Conflict):dispatch_companion(e,{'scope':SCOPE.model_dump()},'companion-delivery-check',check)

from test_event_graph import system, findings

def test_host_context_keeps_real_text_unknown_time_and_speaker(system):
    mind,memory,_,_=system;cfg={'scope':mind.scope.model_dump(),'agent_version':'synthetic'}
    ident={'speaker_person_id':Y,'conversation_key':'feishu:cli_t:oc_y','source_message_id':'om_y','route':{'chat_id':'oc_y'}}
    got=dispatch_companion(mind.engine,cfg,'companion-ingest',{'id':'synthetic-y','text':'我今天在看星星','at':None,'received_at':'2020-01-01T02:00:00Z','identity':ident})
    ctx=dispatch_companion(mind.engine,cfg,'companion-context',{'person_id':Y,'conversation_key':ident['conversation_key'],'query':'星星'})
    assert any(s['text']=='我今天在看星星' and s['occurred_at'] is None and s['speaker_person_id']==Y for s in ctx['sources'])
    with mind.engine.db.connect() as conn:
        assert Companions(mind.engine,mind.scope).visible(conn,got['source_id'],X)

def test_annotation_keeps_only_who_said_it_and_legacy_labels_replay(world):
    """One memory, Kin's judgment: no sharing classification is asked for, stored or shown."""
    e,m,source=world;sid=source('plain',Y,text='今天吃了面')
    with e.db.connect() as conn:
        saved=m.annotation(conn,sid)
    assert saved['speaker_person_id']==Y and not {'classification','sharing'}&set(saved)
    with pytest.raises(TypeError):
        m.annotate(source_id=sid,speaker_person_id=Y,conversation_key='chat:'+Y,classification='private',command_id='label',evidence_id='synthetic')
    with pytest.raises(ValueError):
        dispatch_companion(e,{'scope':SCOPE.model_dump()},'companion-classify',{'source_id':sid,'person_id':Y,'classification':'private','command_id':'c'})
    old=e.receive(SourceInput(namespace='kin-owner-input',key='labelled',scope=SCOPE,text='旧标注',occurred_at=AT,extract=False))['id']
    intent=dict(source_id=old,speaker_person_id=X,conversation_key='chat:x',subject_ids=[],participants=[],basis='explicit',evidence_id='synthetic',recipient_person_id=None)
    with e.db.connect(write=True) as conn:
        legacy={**intent,'classification':'uncertain','sharing':'pending'}
        conn.execute('INSERT INTO mind_companion_changes VALUES(?,?,?)',(SCOPE.key(),'legacy-input',json.dumps({'intent':legacy,'result':{**legacy,'revision':1}})))
    assert m.annotate(source_id=old,speaker_person_id=X,conversation_key='chat:x',command_id='legacy-input',evidence_id='synthetic')['revision']==1


def test_one_finding_can_reach_each_person_once_and_reply_belongs_to_chat(system):
    mind,memory,_,_=system;unit=findings(system)[0];m=Companions(mind.engine,mind.scope)
    for sid in unit['source_ids']:
        m.annotate(source_id=sid,speaker_person_id='agent:kin',conversation_key=None,basis='internal_thought',command_id='kin:'+sid,evidence_id='exploration-test',participants=[{'person_id':'agent:kin','role':'explorer'}])
    cfg={'scope':mind.scope.model_dump(),'agent_version':'synthetic'};ref={'unit_id':unit['id'],'version':1,'mode':'new'}
    for person,chat in [(X,'oc_x'),(Y,'oc_y')]:
        draft='share-'+person
        dispatch_companion(mind.engine,cfg,'companion-share-prepare',{'id':draft,'person_id':person,'references':[ref]})
        memory.ingest({'id':draft,'kind':'delivery','at':mind.clock(),'channel':'feishu','conversation_id':chat,'recipient_person_id':person,
            'delivery_id':draft,'bubble_id':draft,'draft_id':draft,'text':unit['text'],'state':'accepted','message_id':'om_'+chat,'references':[ref]})
        with pytest.raises(Conflict):dispatch_companion(mind.engine,cfg,'companion-share-prepare',{'id':draft+'again','person_id':person,'references':[ref]})
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn,unit['id'],recipient=X)['state']=='shared'
        assert memory.sharing.coverage(conn,unit['id'],recipient=Y)['state']=='shared'
        resolved=memory.sharing.resolve_reply_context(conn,{'channel':'feishu','conversation_id':'oc_y','reply_to_message_id':'om_oc_y'})
        wrong=memory.sharing.resolve_reply_context(conn,{'channel':'feishu','conversation_id':'oc_x','reply_to_message_id':'om_oc_y'})
    assert resolved['state']=='resolved' and wrong['state']=='unresolved'

def test_recipient_theme_choice_balances_familiar_and_new(world):
    from kin_mind.companions import select_sharing_topics
    _,m,source=world;prefs=RecipientPreferences(m)
    for p,topic,signal,quote in [(X,'天文','like','喜欢天文'),(Y,'天文','dislike','不喜欢天文'),(Y,'绘画','like','喜欢绘画')]:
        sid=source(p+topic,p,text=quote);prefs.observe(person=p,source_id=sid,topic=topic,signal=signal,quote=quote)
    candidates=[{'text':'天文新发现'},{'text':'绘画的细节'},{'text':'奇怪的石头'}]
    xs=select_sharing_topics(candidates,prefs.view(X,AT));ys=select_sharing_topics(candidates,prefs.view(Y,AT))
    assert xs[0]['text']=='天文新发现' and ys[0]['text']=='绘画的细节'
    assert all('天文' not in item['text'] for item in ys)
    assert any(item['selection_basis']=='new-topic' for item in xs)

def test_reviewed_legacy_migration_never_rewrites_time_bytes_or_unknown_actor(world):
    e,m,_=world
    old=e.receive(SourceInput(namespace='kin-owner-input',key='legacy-review',scope=SCOPE,text='从前我和一一看灯',occurred_at=AT,extract=False,metadata={'participants':[{'person_id':Y,'role':'companion'}]}))['id']
    unknown=e.receive(SourceInput(namespace='unclassified-import',key='unknown',scope=SCOPE,text='某个人说过的话',occurred_at=AT,extract=False))['id']
    before=e.source(old);blob=e.source(old,content=True).read_bytes()
    result=m.migrate_legacy(source_ids=[old,unknown],command_id='reviewed-batch',evidence_id='fixture-owner-consent')
    assert result['items'][1]['state']=='unknown-speaker'
    assert e.source(old)['occurred_at']==before['occurred_at'] and e.source(old,content=True).read_bytes()==blob
    view=m.source_view(old,Y);assert view['speaker_person_id']==X and view['participants']==[{'person_id':Y,'role':'companion'}]

def test_replacement_and_time_edit_are_sourced_idempotent_and_generation_fenced(world):
    e,m,source=world;sid=source('remind-request',text='提醒一一喝水');edit=source('remind-edit',text='改成休息一下');reply=source('reply-y',Y,text='收到')
    tasks=CompanionTasks(e,SCOPE,['duo']);tasks.tasks.scheduler.policy(ContactPolicy(id='duo',scope=SCOPE,enabled=True,channel='http://127.0.0.1:9',quiet_start=0,quiet_end=0,max_per_day=100,min_interval_minutes=0,require_confirmation=False))
    route=CompanionDelivery(channel='feishu',person_id=Y,app_id='cli_fake',open_id='ou_y',chat_id='oc_y',conversation_key='feishu:cli_fake:oc_y',binding_revision=1,requester_person_id=X,request_conversation_key='chat:'+X,source_input_id=sid)
    task=tasks.create(ContactTaskInput(command_id='replace-base',policy_id='duo',title='提醒',text='喝水',basis=sid,due_at='2030-01-01T05:00:00Z',companion_delivery=route),actor=X)
    args=dict(actor=X,source_id=edit,expected_revision=task['revision'],command_id='content-edit',text='休息一下')
    changed=tasks.replace(task['id'],**args);assert tasks.replace(task['id'],**args)['id']==changed['id']
    assert changed['replaces_task_id']==task['id'] and changed['changed_by']==X
    with pytest.raises(Conflict):tasks.replace(changed['id'],actor=Y,source_id=reply,expected_revision=changed['revision'],command_id='wrong-editor',text='别的事')
    deferred=tasks.change(changed['id'],actor=X,source_id=edit,expected_revision=changed['revision'],action='snooze',due_at='2030-01-01T06:00:00Z',command_id='defer')
    assert tasks.change(changed['id'],actor=X,source_id=edit,expected_revision=changed['revision'],action='snooze',due_at='2030-01-01T06:00:00Z',command_id='defer')==deferred
    with e.db.connect(write=True) as conn:
        row=conn.execute('SELECT data FROM schedules WHERE id=?',(changed['id'],)).fetchone();import json;data=json.loads(row[0]);conn.execute("UPDATE schedules SET state='complete' WHERE id=?",(changed['id'],))
    req={'schedule_id':changed['id'],'schedule_generation':data['generation'],'companion_delivery':data['companion_delivery']}
    assert dispatch_companion(e,{'scope':SCOPE.model_dump()},'companion-delivery-check',req)['state']=='current'
    with pytest.raises(Conflict):dispatch_companion(e,{'scope':SCOPE.model_dump()},'companion-delivery-check',{**req,'schedule_generation':0})


def test_raw_delivery_journal_preserves_recipient_and_source_dependencies(system):
    import json
    mind,memory,_,_=system;m=Companions(mind.engine,mind.scope)
    def deliver(key,**extra):
        receipt=memory.ingest({'id':key,'kind':'delivery','at':mind.clock(),'channel':'feishu',
            'conversation_id':'oc_y','conversation_key':'feishu:cli_t:oc_y','recipient_person_id':Y,
            'delivery_id':key,'bubble_id':key,'text':'只给一一的话','state':'accepted','message_id':'om_'+key,**extra})
        with mind.engine.db.connect() as conn:
            return json.loads(conn.execute('SELECT data FROM mind_runtime_events WHERE id=?',(receipt['id'],)).fetchone()[0])['source_id']
    sid=deliver('y-private-outgoing')
    view=m.source_view(sid,Y)
    assert view['speaker_person_id']=='agent:kin' and 'conversation_key' not in view
    assert m.source_view(sid,X)['speaker_person_id']=='agent:kin'
    source=mind.engine.receive(SourceInput(namespace='kin-owner-input',key='shared-then-private',scope=mind.scope,text='后来收回分享',extract=False))['id']
    labels=dict(source_id=source,speaker_person_id=X,conversation_key='chat:x',evidence_id='synthetic')
    m.annotate(**labels,command_id='share-it')
    derived=deliver('y-derived',input_source_ids=[source]);assert m.source_view(derived,Y)
    m.annotate(**labels,command_id='protect-it',expected_revision=1)
    assert m.source_view(derived,Y)['speaker_person_id']=='agent:kin'


def test_self_reminder_body_cannot_leak_through_unannotated_task_source(world):
    e,m,source=world;sid=source('y-self',Y,text='提醒我写私密日记')
    tasks=CompanionTasks(e,SCOPE,['duo']);tasks.tasks.scheduler.policy(ContactPolicy(id='duo',scope=SCOPE,enabled=True,channel='http://127.0.0.1:9',quiet_start=0,quiet_end=0,max_per_day=100,min_interval_minutes=0,require_confirmation=False))
    route=CompanionDelivery(channel='feishu',person_id=Y,app_id='cli_fake',open_id='ou_y',chat_id='oc_y',conversation_key='feishu:cli_fake:oc_y',binding_revision=1,requester_person_id=Y,request_conversation_key='chat:'+Y,source_input_id=sid)
    tasks.create(ContactTaskInput(command_id='self-private',policy_id='duo',title='提醒',text='写日记',basis=sid,due_at='2030-01-01T05:00:00Z',companion_delivery=route),actor=Y)
    task_source=tasks.tasks.list()['items'][0]['source_ids'][0]
    assert m.source_view(task_source,Y)['speaker_person_id']==Y
    assert m.source_view(task_source,X)['speaker_person_id']==Y

def test_old_kin_exploration_is_own_action_not_yiyi_dialogue(world):
    e,m,_=world
    sid=e.receive(SourceInput(namespace='kin-exploration',key='old-self',scope=SCOPE,text='KIN自己读到合成影评，评论不是核实事实',occurred_at=AT,extract=False))['id']
    before=e.source(sid);assert m.history_preview([sid])[0]['candidate_speaker']=='agent:kin'
    m.migrate_legacy(source_ids=[sid],command_id='old-self',evidence_id='fixture-consent')
    view=m.source_view(sid,Y)
    assert view['speaker_person_id']=='agent:kin' and view['conversation_kind']=='autonomous' and 'conversation_key' not in view
    assert e.source(sid)['occurred_at']==before['occurred_at']

def test_contact_preferences_do_not_change_other_person(world):
    from kin_mind.companions import contact_preference
    e,m,source=world;sid=source('pref-y',Y)
    request={'command_id':'quiet-y','expected_revision':0,'wait_for_reply':False,'evidence_ids':[sid],'reason':'本人明确允许'}
    result=contact_preference(m,Y,request)
    assert contact_preference(m,Y,request)==result and contact_preference(m,X)['revision']==0
    with pytest.raises(Conflict):contact_preference(m,X,{**request,'command_id':'wrong-x'})

def test_scoped_share_coverage_and_reference_registration_follow_current_person(system):
    from kin_mind.companion_view import CompanionView,companion_scope
    mind,memory,_,_=system;unit=findings(system)[0];m=Companions(mind.engine,mind.scope)
    for sid in unit['source_ids']:
        m.annotate(source_id=sid,speaker_person_id='agent:kin',conversation_key=None,command_id='scope-share:'+sid,evidence_id='synthetic')
    ref={'unit_id':unit['id'],'version':1,'mode':'new'}
    # A real legacy owner receipt counts for X, never for the newly met Y.
    memory.ingest({'id':'legacy-shared','kind':'delivery','at':mind.clock(),'channel':'feishu','delivery_id':'legacy-shared','bubble_id':'legacy-shared','text':unit['text'],'state':'accepted','message_id':'om_fake_legacy','references':[ref]})
    for person,expected in [(X,'shared'),(Y,'unshared')]:
        with companion_scope(CompanionView(mind.engine,mind.scope,person,'chat:'+person)):
            with mind.engine.db.connect() as conn:assert memory.sharing.coverage(conn,unit['id'])['state']==expected
    with companion_scope(CompanionView(mind.engine,mind.scope,Y,'chat:'+Y)):
        result=memory.sharing.register({'reply_id':'y-reply','recipient':X,'draft_id':'y-draft','bubbles':[{'text':unit['text'],'references':[ref]}]})
        assert result['recipient_person_id']==Y
        with mind.engine.db.connect() as conn:
            assert conn.execute('SELECT recipient FROM mind_share_reservations WHERE draft_id=?',('y-draft',)).fetchone()[0]==Y
    with companion_scope(CompanionView(mind.engine,mind.scope,X,'chat:'+X)):
        with pytest.raises(Conflict):memory.sharing.register({'reply_id':'y-reply','bubbles':[{'text':'overwrite'}]})

def test_same_event_in_two_private_chats_and_family_keeps_source_time_speaker_and_privacy(system):
    from kin_mind.companions import FAMILY as G
    from kin_mind.companion_view import CompanionView
    mind,memory,_,_=system;cfg={'scope':mind.scope.model_dump(),'agent_version':'synthetic'};m=Companions(mind.engine,mind.scope)
    rows=[]
    for key,person,kind,chat in [('x',X,'p2p','oc_x'),('y',Y,'p2p','oc_y'),('family',Y,'group','oc_family')]:
        identity={'speaker_person_id':person,'conversation_key':'feishu:cli_test:'+chat,'source_message_id':'om_'+key,'route':{'person_id':G if kind=='group' else person,'chat_id':chat},'chat_type':kind,'audience_person_ids':[X,Y] if kind=='group' else []}
        at='2026-10-22T0'+str(len(rows))+':00:00Z'
        row=dispatch_companion(mind.engine,cfg,'companion-ingest',{'id':'same-event-'+key,'text':'说起同一次散步：'+key,'at':at,'received_at':at,'identity':identity})
        rows.append((row['source_id'],at,person,kind,chat))
    for sid,at,person,kind,chat in rows:
        view=m.source_view(sid,person)
        assert view['speaker_person_id']==person and view['conversation_kind']==kind and 'chat_id' not in view
        assert view['occurred_at'].startswith(at[:-1]) and not {'classification','sharing'}&set(view)
    assert m.source_view(rows[0][0],G) and m.source_view(rows[2][0],G)
    # Ordinary experience is one KIN memory even when its ingestion workflow is
    # still pending; the original speaker/time remain unchanged.
    assert m.source_view(rows[1][0],G)['speaker_person_id']==Y
    secret=mind.engine.receive(SourceInput(namespace='kin-owner-input',key='family-secret',scope=mind.scope,text='私密内容',extract=False))['id']
    m.annotate(source_id=secret,speaker_person_id=X,conversation_key='feishu:cli_test:oc_x',command_id='family-secret',evidence_id='fixture')
    assert m.source_view(secret,G)['speaker_person_id']==X
    context=dispatch_companion(mind.engine,cfg,'companion-context',{'person_id':G,'conversation_key':'feishu:cli_test:oc_family'})
    assert [s['source_id'] for s in context['sources']]==[rows[2][0]]
    assert CompanionView(mind.engine,mind.scope,G,'feishu:cli_test:oc_family').filter({'id':secret,'text':'私密内容'})['text']=='私密内容'


def test_context_keeps_latest_correction_and_excludes_current(world):
    e,m,source=world
    ids=[source('budget-'+str(i),text=('old'+str(i))*1800) for i in range(8)]
    correction=source('correction',text='更正：家里有五只猫。')
    current=source('current',text='那现在几只？')
    cfg={'scope':SCOPE.model_dump(),'agent_version':'test','session_id':'synthetic-native','companion_policy_ids':[]}
    value=dispatch_companion(e,cfg,'companion-context',{'person_id':X,'conversation_key':'chat:'+X,'exclude_source_ids':[current]})
    assert correction in [v['source_id'] for v in value['sources']]
    assert current not in [v['source_id'] for v in value['sources']]
    assert sum(len(v['text']) for v in value['sources'])<=24000
    assert len(value['sources'])<len(ids)+2


def test_context_quote_resolves_original_speaker_and_time(world):
    import json
    from kin_mind.memory import MemoryContinuity
    from kin_mind.state import Mind
    e,m,source=world
    original=source('quoted',person=Y,text='猫是五只。')
    continuity=MemoryContinuity(Mind(e,SCOPE));continuity.configure({'records':True})
    continuity.ingest({'id':'quoted-event','historical':True,'kind':'owner-message','at':AT,'source_id':original,'text':'猫是五只。','message_id':'om_quoted','actor':Y,'speaker_person_id':Y,'conversation_key':'chat:'+Y})
    cfg={'scope':SCOPE.model_dump(),'agent_version':'test','session_id':'quote-native','companion_policy_ids':[]}
    value=dispatch_companion(e,cfg,'companion-context',{'person_id':X,'conversation_key':'chat:'+X,'reply_context':{'reply_to_message_id':'om_quoted'}})
    assert value['quoted_messages'][0]['speaker_person_id']==Y
    assert value['quoted_messages'][0]['text_excerpt']=='猫是五只。'
    assert value['quoted_messages'][0]['occurred_at'].startswith('2026-09-01')


def test_source_dag_visits_shared_ancestors_once_per_read(world):
    e,m,source=world
    ids=[source('dag0'),source('dag1')]
    for i in range(2,18):ids.append(source('dag'+str(i),parents=ids[-2:]))
    with e.db.connect() as conn:
        queries=[];conn.set_trace_callback(queries.append)
        assert m.visible(conn,ids[-1],X)
        assert len(queries)<200
        conn.set_trace_callback(None)
    with e.db.connect(write=True) as conn:conn.execute('UPDATE sources SET deleted=1 WHERE id=?',(ids[0],))
    with e.db.connect() as conn:
        assert m.visible(conn,ids[-1],X)
        assert not m.visible(conn,ids[0],X)


def test_host_copies_learn_which_sources_were_deleted(tmp_path):
    from kin_mind.host import dispatch
    e=Engine(tmp_path/'memory')
    kept=e.receive(SourceInput(namespace='kin-companion-input',key='kept',scope=SCOPE,text='合成：留着',occurred_at=AT,extract=False))['id']
    gone=e.receive(SourceInput(namespace='kin-companion-input',key='gone',scope=SCOPE,text='合成：删掉',occurred_at=AT,extract=False))['id']
    e.receive(SourceInput(namespace='kin-owner-input',key='owner-kept',scope=SCOPE,text='合成：图一',occurred_at=AT,extract=False))
    owner=e.receive(SourceInput(namespace='kin-owner-input',key='owner-gone',scope=SCOPE,text='合成：图二',occurred_at=AT,extract=False))['id']
    e.delete(gone);e.delete(owner)
    config={'root':str(tmp_path/'memory'),'scope':SCOPE.model_dump(),'agent_version':'test'}
    got=dispatch(config,'deleted-sources',{'source_ids':[kept,gone],'owner_input_ids':['owner-kept','owner-gone','never-stored']})
    assert got=={'deleted':sorted([gone,'owner-gone'])}
