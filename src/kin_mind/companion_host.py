"""Host-only companion operations. No model/network execution in these handlers."""
import json
from eventmem.core.models import Scope, SourceInput, RecallRequest, CompanionDelivery
from eventmem.core.contact_tasks import ContactTaskInput
from eventmem.core.db import Conflict, Missing, named_in
from .companions import Companions, CompanionTasks, RecipientPreferences, select_sharing_topics, XIAOGUANG, YIYI, FAMILY

def dispatch_companion(engine, config, action, request):
    scope=Scope.model_validate(config['scope']);memory=Companions(engine,scope)
    policies=config.get('companion_policy_ids') or ['kin-wechat-reminders']
    if action=='companion-targets':
        return {person:{"route":route,**dispatch_companion(engine,config,'companion-context',
            {'person_id':person,'conversation_key':route['conversation_key'],'agenda_only':True})}
            for person,route in request['routes'].items()}
    if action=='companion-host-call':
        from .companion_view import CompanionView
        from .host import _dispatch
        person=request['person_id'];conversation=request['conversation_key']
        view=CompanionView(engine,scope,person,conversation)
        operation=request['action'];body=dict(request.get('request') or {})
        if operation.startswith('companion-'):raise ValueError('Nested companion dispatch unavailable')
        if operation=='private-note-save':body.update(person_id=person,conversation_key=conversation)
        if operation in {'sharing-preflight','share-references','share-history'}:body['recipient']=person
        for identifier in named_in(body):
            view.require(identifier)
        scoped_config={**config,'session_id':request.get('session_id') or conversation}
        scoped_config.pop('session_registry_file',None)
        if request.get('shared_session'):scoped_config['main_session_review']=True
        from .companion_view import companion_scope
        with companion_scope(view):
            result=_dispatch(scoped_config,operation,body)
        if operation=='memory-context':
            result['companion_context']=dispatch_companion(engine,config,'companion-context',{'person_id':person,'conversation_key':conversation,'query':body.get('query','')})
            return result
        if operation=='session-snapshot':
            # Shared facts may be recalled, but another person's dialogue is never this window's turns.
            own=[]
            with engine.db.connect() as conn:
                for item in result.get('items',[]):
                    sid=item.get('sourceId');annotation=memory.annotation(conn,sid) if sid else None
                    row=conn.execute('SELECT data FROM sources WHERE id=? AND scope=?',(sid,scope.key())).fetchone() if sid else None
                    meta=json.loads(row[0]).get('metadata',{}) if row else {}
                    origin=(annotation or {}).get('conversation_key') or meta.get('conversation_key')
                    if not request.get('shared_session') and origin!=conversation and not (person==XIAOGUANG and not origin and meta.get('recipient_person_id',XIAOGUANG)==XIAOGUANG):continue
                    if sid and not memory.visible(conn,sid,person):continue
                    item.update(speaker_person_id=(annotation or {}).get('speaker_person_id') or meta.get('speaker_person_id') or (XIAOGUANG if item.get('role')=='user' else 'agent:kin'),conversation_kind=meta.get('conversation_kind','p2p' if person!=FAMILY else 'group'))
                    own.append(item)
            result['items']=own
            ids={i['id'] for i in own};result['cursors']['public']=[i for i in result['cursors'].get('public',[]) if i['id'] in ids]
            result['sourceRevisions']={k:v for k,v in result.get('sourceRevisions',{}).items() if k in ids}
        if operation in {'read','state-overview'} and isinstance(result,dict):
            result=view.filter(result)
            if person!=XIAOGUANG:
                from .drives import without_intimacy
                result=without_intimacy(result)
            result['relationship']=memory.relationship(person)
            return result
        from .host import MODEL_FACING
        from .model_view import for_model
        filtered=view.filter(result) if operation in MODEL_FACING or operation in {'private-notes','session-snapshot','session-checkpoint'} else result
        if operation == 'claim' and result.get('state') == 'drafting' and filtered.get('state') != 'drafting':
            # Claim is already durable. A protected view must not hide its handle
            # and leave the recipient's only active slot orphaned indefinitely.
            from .state import Mind
            with companion_scope(view):
                Mind(engine, scope).settle_contact(attempt_id=result['id'], state='canceled',
                    reason='contact-source-protected', decision={'action':'wait',
                    'reason':'The offered sources are unavailable in this recipient view',
                    'condition':'new_evidence'})
            return {'state':'waiting', 'reason':'contact-source-protected'}
        return for_model(filtered) if operation in MODEL_FACING else filtered
    if action=='companion-autonomy':
        from .companion_view import CompanionView, companion_scope
        from .state import Mind, DesireChange
        from .plans import AutonomousPlans
        person,conversation=request['person_id'],request['conversation_key']
        view=CompanionView(engine,scope,person,conversation)
        body=dict(request['change'])
        # The intention is KIN's. Its human initiating source remains provenance,
        # not permission to carry that source's private content to another audience.
        for identifier in named_in(body):view.require(identifier)
        with companion_scope(view):
            mind=Mind(engine,scope)
            if request['kind']=='wish':
                body['agent_version']=config['agent_version']
                return mind.manage_desire(DesireChange.model_validate(body))
            if request['kind']=='plan':return AutonomousPlans(mind).manage(body)
        raise ValueError('Unknown autonomous intention')
    if action=='companion-preference':return RecipientPreferences(memory).observe(**request)
    if action=='companion-ingest':
        identity=request['identity'];speaker=identity['speaker_person_id']
        at=request.get('at');received=request['received_at']
        source=engine.receive(SourceInput(namespace='kin-companion-input',key=request['id'],scope=scope,
            session=identity['conversation_key'],text=request['text'],occurred_at=at or received,authority='explicit',extract=False,
            metadata={'speaker_person_id':speaker,'conversation_key':identity['conversation_key'],'role':'user','host_event':'message','recipient_person_id':identity['route'].get('person_id',speaker),
                      'conversation_kind':identity.get('chat_type','p2p'),'chat_id':identity['route']['chat_id'],'audience_person_ids':identity.get('audience_person_ids',[]),
                      'channel':'feishu','original_occurred_at':at,'time_basis':'platform' if at else 'receipt-only'}))
        memory.annotate(source_id=source['id'],speaker_person_id=speaker,conversation_key=identity['conversation_key'],
            recipient_person_id=FAMILY if identity['route'].get('person_id',speaker)==FAMILY else None,command_id='input:'+request['id'],evidence_id=identity['source_message_id'])
        # Existing runtime ledger / event graph, with a human actor distinct from event participants.
        from .state import Mind
        from .memory import MemoryContinuity
        mind=Mind(engine,scope)
        MemoryContinuity(mind).ingest({'id':request['id'],'kind':'owner-message','at':at or received,'source_id':source['id'],
            'text':request['text'],'session':identity['conversation_key'],'conversation_key':identity['conversation_key'],
            'recipient_person_id':identity['route'].get('person_id',speaker),'actor':speaker,'speaker_person_id':speaker,'channel':'feishu','conversation_id':identity['route']['chat_id'],
            'message_id':identity['source_message_id'],**{k:v for k,v in request.get('reply_context',{}).items() if k in {'reply_to_message_id','root_message_id','thread_id'}},'received_at':received,'time_basis':'platform' if at else 'receipt-only'})
        if identity.get('chat_type','p2p')=='p2p':
            from .appraisal import Appraisals
            from .companion_view import CompanionView, companion_scope
            with companion_scope(CompanionView(engine,scope,speaker,identity['conversation_key'])):
                Appraisals(mind).enqueue([source['id']],config['agent_version'])
        return {'source_id':source['id']}
    if action=='companion-context':
        person=request['person_id'];conversation=request['conversation_key'];ids=[]
        if person not in {XIAOGUANG,YIYI,FAMILY}:raise ValueError('Unknown person')
        with engine.db.connect() as conn:
            recent=conn.execute('SELECT s.id FROM sources s JOIN mind_companion_sources a ON a.source_id=s.id AND a.scope=s.scope '
                "WHERE s.scope=? AND s.deleted=0 AND json_extract(a.data,'$.conversation_key')=? ORDER BY s.received_at DESC LIMIT 12",
                (scope.key(),conversation)).fetchall()
            ids.extend(r[0] for r in recent)
        if request.get('query'):
            found=engine.recall(RecallRequest(query=request['query'],scope=scope,budget=1800,limit=12),allow_model=False,record=False,original=True)
            for item in found.get('items',[]):ids.extend(item.get('source_ids',[]))
        excluded=set(request.get('exclude_source_ids',[]))
        # Reuse only originals acknowledged in the current native window.
        from .context import Contexts
        from .state import Mind
        ctx=Contexts(Mind(engine,scope))
        with engine.db.connect() as conn:
            window=ctx.window(config.get('session_id',conversation),conn)
            deliveries=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_context_deliveries'").fetchone()
            for row in (conn.execute("SELECT data FROM mind_context_deliveries WHERE scope=? AND session=? AND epoch=? AND state='accepted'",
                                    (scope.key(),config.get('session_id',conversation),window['epoch'])) if deliveries else []):
                for item in json.loads(row[0]).get('items',[]):
                    if item.get('depth')=='original' and item.get('id','').startswith('src_') and window['seen'].get(item['id'])==item.get('revision'):
                        excluded.add(item['id'])
        views=[];remaining=24000
        for sid in ([] if request.get('agenda_only') else dict.fromkeys(ids)):
            if sid in excluded:continue
            try:
                view=memory.source_view(sid,person)
                allowance=min(6000,remaining)
                view['truncated']=len(view['text'])>allowance
                view['text']=view['text'][:allowance];remaining-=len(view['text'])
                views.append(view)
                if remaining<=0:break
            except Missing:pass
        views.sort(key=lambda v:(v.get('occurred_at') or v.get('received_at') or '',v['source_id']))
        quotes=[]
        references=request.get('reply_contexts') or [{'input_id':request.get('input_id'),**(request.get('reply_context') or {})}]
        for reference in references:
            target=reference.get('reply_to_message_id')
            if not target:continue
            with engine.db.connect() as conn:
                row=conn.execute("SELECT data FROM mind_runtime_events WHERE scope=? AND json_extract(data,'$.message_id')=? ORDER BY seq DESC LIMIT 1",(scope.key(),target)).fetchone()
            event=json.loads(row[0]) if row else {}
            try:
                view=memory.source_view(event['source_id'],person) if event.get('source_id') else None
            except Missing:view=None
            quotes.append({'input_id':reference.get('input_id'),'id':target,**({'source_id':view['source_id'],'speaker_person_id':view['speaker_person_id'],
                     'text_excerpt':view['text'][:2000],
                     'occurred_at':view['occurred_at']} if view else {'state':'unavailable'})})
        from eventmem.core.models import now
        tasks=[]
        if policies:
            all_tasks=CompanionTasks(engine,scope,policies).tasks.list(100)['items']
            with engine.db.connect() as conn:
                for task in all_tasks:
                    route=json.loads(conn.execute('SELECT data FROM schedules WHERE id=?',(task['id'],)).fetchone()[0]).get('companion_delivery')
                    if route and (person in {route['person_id'],route['requester_person_id']} or person==FAMILY and route['request_conversation_key']==conversation):
                        task['people']={'requester':route['requester_person_id'],'recipient':route['person_id']}
                        task['human_responses']=[json.loads(r[0]) for r in conn.execute('SELECT data FROM mind_companion_responses WHERE scope=? AND task_id=?',(scope.key(),task['id']))]
                        tasks.append(task)
        preferences=RecipientPreferences(memory).view(person,now()) if person!=FAMILY else []
        agenda={}
        if request.get('agenda_only'):
            from .companion_view import CompanionView, companion_scope
            from .state import Mind
            from .plans import AutonomousPlans
            with companion_scope(CompanionView(engine,scope,person,conversation)):
                mind=Mind(engine,scope)
                status=mind.read()
                agenda={'wishes':status.get('desires',[]),'plans':AutonomousPlans(mind).read(limit=20)['plans'],
                        'interaction_timing':status.get('interaction_timing',{})}
        return {**agenda,**({'quoted_messages':quotes} if quotes else {}),'person_id':person,'sources':views,'relationship':memory.relationship(person),
                'contact_preferences':__import__('kin_mind.companions',fromlist=['contact_preference']).contact_preference(memory,person) if person!=FAMILY else {},'preferences':preferences,'tasks':tasks,'kin_interests':kin_interests(engine,scope,memory,person),'sharing_candidates':select_sharing_topics(sharing_candidates(engine,scope,memory,person),preferences)}
    if action=='companion-output':
        identity=request['identity']
        source=engine.receive(SourceInput(namespace='kin-companion-output',key=request['id'],scope=scope,session=identity['conversation_key'],
            text=request['text'],occurred_at=request['at'],authority='model',extract=False,metadata={'role':'assistant','speaker_person_id':'agent:kin','conversation_key':identity['conversation_key']}),derived_from=list(dict.fromkeys([sid for sid in [request.get('source_input_id'),*request.get('referenced_source_ids',[])] if sid])))
        memory.annotate(source_id=source['id'],speaker_person_id='agent:kin',conversation_key=identity['conversation_key'],
            recipient_person_id=identity['speaker_person_id'],command_id='output:'+request['id'],evidence_id=request['receipt']['messageId'])
        return {'source_id':source['id']}
    if action=='companion-share-prepare':
        from .state import Mind
        from .sharing import ShareLedger
        ledger=ShareLedger(Mind(engine,scope));person=request['person_id'];refs=request['references']
        with engine.db.connect() as conn:
            for raw in refs:
                ref,_=ledger.valid_reference(conn,raw)
                if ledger.coverage(conn,ref.unit_id,recipient=person)['state'] in {'shared','unconfirmed'}:raise Conflict('Already shared or awaiting receipt')
                held=conn.execute("SELECT draft_id,state FROM mind_share_reservations WHERE scope=? AND recipient=? AND unit_id=? AND version=?",(scope.key(),person,ref.unit_id,ref.version)).fetchone()
                if held and held['draft_id']!=request['id'] and held['state']!='canceled':raise Conflict('Original share still reserved')
        return ledger.prepare({'draft_id':request['id'],'recipient':person,'references':refs})
    if action=='companion-history-migrate':return memory.migrate_legacy(**request)
    if action=='companion-history-preview':return memory.history_preview(request['source_ids'])
    if action=='companion-annotate':return memory.annotate(**request)
    if action=='companion-relationship':return memory.update_relationship(**request)
    if action=='companion-delivery-check':
        with engine.db.connect() as conn:
            row=conn.execute('SELECT revision,state,data FROM schedules WHERE id=?',(request['schedule_id'],)).fetchone()
            if not row or json.loads(row['data']).get('generation',0)!=request['schedule_generation'] or row['state'] in {'canceled','paused'}:raise Conflict('Companion reminder changed')
            if json.loads(row['data']).get('companion_delivery')!=request['companion_delivery']:raise Conflict('Companion reminder route changed')
        return {'state':'current'}
    tasks=CompanionTasks(engine,scope,policies)
    if action=='companion-task-create':
        r=request['route'];route=CompanionDelivery(**{k:v for k,v in r.items() if k!='schema'},requester_person_id=request['actor'],
            request_conversation_key=request['request_conversation_key'],source_input_id=request['source_input_id'],mode=request.get('mode','text'))
        if not policies:raise ValueError('Companion reminder policies pending configuration')
        return tasks.create(ContactTaskInput(command_id=request['command_id'],policy_id=policies[0],title=request['title'],text=request['text'],
            basis=request['basis'],due_at=request['due_at'],recurrence=request['recurrence'],companion_delivery=route),actor=request['actor'])
    if action=='companion-task-change':return tasks.change(**request)
    if action=='companion-task-replace':return tasks.replace(**request)
    if action=='companion-task-respond':return tasks.respond(**request)
    raise ValueError('Unknown companion operation')


def sharing_candidates(engine,scope,memory,person):
    from .state import Mind
    from .sharing import ShareLedger
    ledger=ShareLedger(Mind(engine,scope));result=[]
    with engine.db.connect() as conn:
        rows=conn.execute("SELECT data FROM mind_graph_nodes WHERE scope=? AND kind='finding' AND state='active' ORDER BY occurred_at DESC LIMIT 80",(scope.key(),)).fetchall()
        for row in rows:
            unit=json.loads(row[0]);sources=unit.get('source_ids',[])
            if not sources or not all(memory.visible(conn,s,person) for s in sources):continue
            if ledger.coverage(conn,unit['id'],recipient=person)['state']!='unshared':continue
            result.append({'text':unit.get('text',''),'at':unit.get('occurred_at'),'basis':unit.get('basis','unknown'),
                           'topic_tags':unit.get('topic_tags',[]),'source_ids':sources,'knowledge_basis':'model_report','perspective':'source_report_not_verified_fact','reference':{'unit_id':unit['id'],'version':unit.get('content_version',1),'mode':'new'}})
            if len(result)>=12:break
    return result


def kin_interests(engine,scope,memory,person):
    from .traits import installed
    found=[]
    with engine.db.connect() as conn:
        if not installed(conn):return found
        for row in conn.execute("SELECT data FROM mind_traits WHERE scope=? AND status IN ('candidate','established')",(scope.key(),)):
            t=json.loads(row[0])
            if t.get('basis')!='self_preference' or t.get('category') not in {'interests','aesthetics','humor'}:continue
            evidence=t.get('evidence',[])
            if evidence and not all(memory.visible(conn,e.get('source_id'),person) for e in evidence):continue
            found.append({'id':t['id'],'text':t.get('text',''),'basis':'kin_subjective_preference','evidence_ids':[e['source_id'] for e in evidence if e.get('source_id')]})
    return found[:12]
