"""Identity/visibility projections in the existing memory database, never a second memory.

Historical sources and their timestamps are immutable. A reviewed annotation only adds
who spoke, who an event concerns, and which audience may recall it. Unknown stays unknown.
"""
from __future__ import annotations
import json
from copy import deepcopy
from eventmem.core.db import Conflict, Missing, dumps
from eventmem.core.models import Scope

XIAOGUANG, YIYI = 'person:xiaoguang', 'person:yiyi'
PEOPLE = {XIAOGUANG, YIYI}
FAMILY = 'group:family'
PROTECTED = {'private', 'intimate', 'health', 'financial', 'uncertain'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS mind_companion_sources(
 scope TEXT NOT NULL,source_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,source_id));
CREATE TABLE IF NOT EXISTS mind_companion_relations(
 scope TEXT NOT NULL,person_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,person_id));
CREATE TABLE IF NOT EXISTS mind_companion_changes(
 scope TEXT NOT NULL,command_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,command_id));
'''

class Companions:
    def __init__(self, engine, scope: Scope):
        self.engine, self.scope = engine, scope
        with engine.db.connect() as conn:
            conn.executescript(SCHEMA)

    def annotate(self, *, source_id, speaker_person_id, conversation_key, classification,
                 command_id, evidence_id, expected_revision=0, subject_ids=(), participants=(),
                 sharing='private', basis='explicit', recipient_person_id=None):
        if speaker_person_id not in PEOPLE | {'agent:kin', None}:
            raise ValueError('Unknown speaker')
        if recipient_person_id is not None and recipient_person_id not in PEOPLE | {FAMILY}: raise ValueError('Unknown recipient')
        if classification not in PROTECTED | {'ordinary'} or sharing not in {'private', 'ordinary-shared', 'pending'}:
            raise ValueError('Explicit content classification and sharing choice required')
        if not command_id or not evidence_id or basis not in {'explicit','observed','inferred','internal_thought'}:
            raise ValueError('Annotation provenance required')
        if any(not isinstance(p, dict) or not p.get('person_id') or not p.get('role') for p in participants):
            raise ValueError('Participants need independent identities and roles')
        data = dict(source_id=source_id, speaker_person_id=speaker_person_id, conversation_key=conversation_key,
                    classification=classification, sharing=sharing, subject_ids=list(subject_ids),
                    participants=list(participants), basis=basis, evidence_id=evidence_id,recipient_person_id=recipient_person_id)
        with self.engine.db.connect(write=True) as conn:
            previous=conn.execute('SELECT data FROM mind_companion_changes WHERE scope=? AND command_id=?',
                                  (self.scope.key(),command_id)).fetchone()
            if previous:
                old=json.loads(previous[0])
                if old['intent'] != data: raise Conflict('Annotation command reused with different intent')
                return old['result']
            source=conn.execute('SELECT scope,deleted FROM sources WHERE id=?',(source_id,)).fetchone()
            if not source or source['deleted'] or source['scope']!=self.scope.key(): raise Missing(source_id)
            prior=conn.execute('SELECT revision FROM mind_companion_sources WHERE scope=? AND source_id=?',
                               (self.scope.key(),source_id)).fetchone()
            if (prior[0] if prior else 0)!=expected_revision: raise Conflict('Annotation revision changed')
            result={**data,'revision':expected_revision+1}
            conn.execute('INSERT OR REPLACE INTO mind_companion_sources VALUES(?,?,?,?)',
                         (self.scope.key(),source_id,result['revision'],dumps(result)))
            conn.execute('INSERT INTO mind_companion_changes VALUES(?,?,?)',
                         (self.scope.key(),command_id,dumps({'intent':data,'result':result})))
            return result

    def annotation(self, conn, sid):
        row=conn.execute('SELECT data FROM mind_companion_sources WHERE scope=? AND source_id=?',
                         (self.scope.key(),sid)).fetchone()
        return json.loads(row[0]) if row else None

    def visible(self, conn, sid, audience, seen=None, memo=None):
        # A source DAG can reach the same ancestor through many paths.
        # Memo belongs to this one read, so deletion/reclassification is fresh next time.
        memo={} if memo is None else memo
        if sid in memo:return memo[sid]
        result=self._visible(conn,sid,audience,seen,memo)
        memo[sid]=result
        return result

    def _visible(self, conn, sid, audience, seen, memo):
        if audience not in PEOPLE | {FAMILY}: return False
        seen=set() if seen is None else set(seen)
        if sid in seen: return False
        seen.add(sid)
        row=conn.execute('SELECT scope,namespace,data,deleted FROM sources WHERE id=?',(sid,)).fetchone()
        if not row or row['deleted'] or row['scope']!=self.scope.key(): return False
        # KIN has one memory across conversations. Subject, provenance and any
        # explicit confidentiality annotation are facts for her judgment; they
        # do not recursively turn a derived experience into an inaccessible one.
        return True

    def source_view(self, source_id, audience):
        with self.engine.db.connect() as conn:
            if not self.visible(conn,source_id,audience): raise Missing('Source unavailable to this conversation')
            row=conn.execute('SELECT data,occurred_at,received_at FROM sources WHERE id=?',(source_id,)).fetchone()
            data=json.loads(row['data']); a=self.annotation(conn,source_id)
            return {'source_id':source_id,'text':self.engine.source(source_id,content=True).read_bytes()[:24000].decode('utf-8',errors='replace') if data.get('media_type','text/plain').startswith(('text/','application/json')) else '[附件来源：'+data.get('media_type','unknown')+']','occurred_at':data.get('metadata',{}).get('original_occurred_at',row['occurred_at']),
                    'time_basis':data.get('metadata',{}).get('time_basis','source'),
                    'conversation_kind':data.get('metadata',{}).get('conversation_kind','autonomous' if a and a['speaker_person_id']=='agent:kin' and not a.get('conversation_key') else 'p2p' if a and a.get('conversation_key') else 'unknown'),
                    'chat_id':data.get('metadata',{}).get('chat_id'),
                    'received_at':row['received_at'], 'speaker_person_id':a['speaker_person_id'] if a else data.get('metadata',{}).get('speaker_person_id'),
                    'conversation_key':a['conversation_key'] if a else data.get('metadata',{}).get('conversation_key'),
                    'subject_ids':a['subject_ids'] if a else [],'participants':a['participants'] if a else [],
                    'classification':a['classification'] if a else None,'sharing':a['sharing'] if a else None,
                    'basis':a['basis'] if a else 'unknown','knowledge_basis':data.get('metadata',{}).get('knowledge_basis'),
                    'external_sources':data.get('metadata',{}).get('sources',[]),'observation_ids':data.get('metadata',{}).get('observation_ids',[]),'perspective':data.get('metadata',{}).get('perspective')}

    def history_preview(self, source_ids):
        """No writes, no guessed timestamps/actors. Caller supplies reviewed classifications."""
        with self.engine.db.connect() as conn:
            result=[]
            for sid in source_ids:
                row=conn.execute('SELECT namespace,data,occurred_at,received_at FROM sources WHERE id=? AND scope=? AND deleted=0',
                                 (sid,self.scope.key())).fetchone()
                if not row: raise Missing(sid)
                data=json.loads(row['data']);a=self.annotation(conn,sid)
                legacy_owner=row['namespace']=='kin-owner-input' or row['namespace']=='kin-runtime' and data.get('metadata',{}).get('host_event')=='owner-message'
                legacy_kin=row['namespace'] in {'kin-exploration','kin-exploration-observation'} or row['namespace']=='host:codex' and data.get('metadata',{}).get('host_event')=='tool'
                result.append({'source_id':sid,'occurred_at':row['occurred_at'],'received_at':row['received_at'],
                    'speaker_person_id':a['speaker_person_id'] if a else data.get('metadata',{}).get('speaker_person_id'),
                    'candidate_speaker':XIAOGUANG if legacy_owner else 'agent:kin' if legacy_kin else None,
                    'candidate_basis':'legacy-bound-owner-namespace' if legacy_owner else 'legacy-kin-operation-namespace' if legacy_kin else 'unknown',
                    'annotation':a,'original_metadata':deepcopy(data.get('metadata',{}))})
            return result

    def migrate_legacy(self, *, source_ids, classifications, command_id, evidence_id):
        """Reviewed labels only; immutable originals retain their exact timestamps and bytes."""
        preview=self.history_preview(source_ids);results=[]
        for item in preview:
            if item['annotation']:
                results.append(item['annotation']);continue
            if item['candidate_speaker'] not in {XIAOGUANG,'agent:kin'}:
                results.append({'source_id':item['source_id'],'state':'unknown-speaker'});continue
            meta=item['original_metadata']
            results.append(self.annotate(source_id=item['source_id'],speaker_person_id=item['candidate_speaker'],
                conversation_key=meta.get('conversation_key'),classification=classifications.get(item['source_id'],'uncertain'),
                sharing='ordinary-shared',participants=meta.get('participants',[]),subject_ids=meta.get('subject_ids',[]),
                command_id=command_id+':'+item['source_id'],evidence_id=evidence_id,basis='observed'))
        return {'items':results}

    def relationship(self, person):
        if person==FAMILY:return {'person_id':FAMILY,'revision':0,'values':{},'evidence_ids':[]}
        if person not in PEOPLE: raise ValueError('Unknown relationship')
        with self.engine.db.connect() as conn:
            row=conn.execute('SELECT data FROM mind_companion_relations WHERE scope=? AND person_id=?',
                             (self.scope.key(),person)).fetchone()
            return json.loads(row[0]) if row else {'person_id':person,'revision':0,'values':{},'evidence_ids':[]}

    def update_relationship(self, person, *, values, evidence_ids, expected_revision, command_id):
        if person not in PEOPLE or not evidence_ids: raise ValueError('Sourced relationship required')
        allowed={'closeness','security','longing','care','grievance','initiative'}
        if set(values)-allowed or any(type(v) is not int or not 0<=v<=100 for v in values.values()):
            raise ValueError('Invalid relationship values')
        intent=dict(person_id=person,values=values,evidence_ids=evidence_ids,expected_revision=expected_revision)
        with self.engine.db.connect(write=True) as conn:
            old=conn.execute('SELECT data FROM mind_companion_changes WHERE scope=? AND command_id=?',(self.scope.key(),command_id)).fetchone()
            if old:
                prior=json.loads(old[0])
                if prior['intent']!=intent: raise Conflict('Relationship command changed')
                return prior['result']
            for sid in evidence_ids:
                if not self.visible(conn,sid,person): raise Missing('Relationship evidence unavailable')
            row=conn.execute('SELECT data FROM mind_companion_relations WHERE scope=? AND person_id=?',(self.scope.key(),person)).fetchone()
            prior=json.loads(row[0]) if row else {'revision':0,'values':{}}
            if prior['revision']!=expected_revision: raise Conflict('Relationship revision changed')
            result={'person_id':person,'revision':expected_revision+1,'values':{**prior['values'],**values},'evidence_ids':evidence_ids}
            conn.execute('INSERT OR REPLACE INTO mind_companion_relations VALUES(?,?,?,?)',(self.scope.key(),person,result['revision'],dumps(result)))
            conn.execute('INSERT INTO mind_companion_changes VALUES(?,?,?)',(self.scope.key(),command_id,dumps({'intent':intent,'result':result})))
            return result

class CompanionTasks:
    """Cross-person tasks use ContactTasks/Scheduler and the original delivery ledger."""
    def __init__(self, engine, scope, policy_ids):
        from eventmem.core.contact_tasks import ContactTasks
        self.tasks=ContactTasks(engine,scope,policy_ids,namespace='kin-companion-tasks')
        self.memory=Companions(engine,scope)
        self.engine,self.scope=engine,scope
        with engine.db.connect() as conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS mind_companion_responses(
              scope TEXT NOT NULL,source_id TEXT NOT NULL,task_id TEXT NOT NULL,data TEXT NOT NULL,
              PRIMARY KEY(scope,source_id));''')

    def _speaker(self, source_id, person):
        with self.engine.db.connect() as conn:
            a=self.memory.annotation(conn,source_id)
            if not a or a['speaker_person_id']!=person: raise Conflict('Action requires the actual speaker source')
            return a

    def create(self, task, *, actor):
        route=task.companion_delivery
        if not route or route.requester_person_id!=actor: raise Conflict('Requester mismatch')
        a=self._speaker(route.source_input_id,actor)
        if a['conversation_key']!=route.request_conversation_key: raise Conflict('Request conversation mismatch')
        return self.tasks.create(task)

    def change(self, task_id, *, actor, source_id, expected_revision, action, due_at=None, command_id=None):
        self._speaker(source_id,actor)
        command_id=command_id or f'companion-change:{source_id}:{task_id}:{action}'
        context={'task_id':task_id,'command_id':command_id,'actor':actor,'source_id':source_id,'action':action,'expected_revision':expected_revision,'due_at':due_at}
        with self.engine.db.connect() as conn:
            prior=conn.execute('SELECT data FROM mind_companion_changes WHERE scope=? AND command_id=?',(self.scope.key(),command_id)).fetchone()
            if prior:
                old=json.loads(prior[0])
                if old['intent']!=context:raise Conflict('Task change command reused')
                return old['result']
            row,_=self.tasks._owned(conn,task_id);route=json.loads(row['data']).get('companion_delivery')
            if not route: raise Missing('Not a companion task')
            if actor!=route['requester_person_id'] and not (actor==route['person_id'] and action in {'cancel','pause'}):
                raise Conflict('Only requester edits; recipient may stop reminders')
        result=self.tasks.scheduler.control(task_id,action,expected_revision,due_at=due_at,change_context=context)
        # Preserve who changed it independently of scheduler's transport acknowledgment.
        with self.engine.db.connect(write=True) as conn:
            key=command_id
            conn.execute('INSERT OR IGNORE INTO mind_companion_changes VALUES(?,?,?)',
                (self.scope.key(),key,dumps({'intent':context,'result':result})))
        return result

    def replace(self, task_id, *, actor, source_id, expected_revision, command_id, text, due_at=None):
        from eventmem.core.contact_tasks import ContactTaskInput
        from eventmem.core.models import CompanionDelivery
        a=self._speaker(source_id,actor)
        with self.engine.db.connect() as conn:
            row,record=self.tasks._owned(conn,task_id);data=json.loads(row['data']);route=data.get('companion_delivery')
            if not route or route['requester_person_id']!=actor:raise Conflict('Only requester may replace reminder')
            route={**route,'source_input_id':source_id,'request_conversation_key':a['conversation_key']}
            replacement=ContactTaskInput(command_id=command_id+':replacement',policy_id=row['policy_id'],title=record['title'],
                text=text,basis=source_id,due_at=due_at or row['due_at'],recurrence=data['recurrence'],
                companion_delivery=CompanionDelivery.model_validate(route))
        stopped=self.change(task_id,actor=actor,source_id=source_id,expected_revision=expected_revision,action='cancel',command_id=command_id+':stop')
        if stopped.get('reconciliation_required'):raise Conflict('Original reminder delivery needs reconciliation before replacement')
        created=self.tasks.create(replacement,metadata={'replaces_task_id':task_id,'changed_by':actor,'source_input_id':source_id})
        return {**created,'replaces_task_id':task_id,'changed_by':actor}

    def respond(self, task_id, *, actor, source_id, status):
        if status not in {'replied','acknowledged','completed','declined'}: raise ValueError('Human response required')
        self._speaker(source_id,actor)
        with self.engine.db.connect(write=True) as conn:
            row,_=self.tasks._owned(conn,task_id);route=json.loads(row['data']).get('companion_delivery')
            if not route or actor!=route['person_id']: raise Conflict('Only recipient can acknowledge or complete')
            result={'task_id':task_id,'actor':actor,'source_id':source_id,'status':status}
            old=conn.execute('SELECT data FROM mind_companion_responses WHERE scope=? AND source_id=?',(self.scope.key(),source_id)).fetchone()
            if old and json.loads(old[0])!=result: raise Conflict('Response already belongs to another task')
            conn.execute('INSERT OR IGNORE INTO mind_companion_responses VALUES(?,?,?,?)',(self.scope.key(),source_id,task_id,dumps(result)))
            return result

class RecipientPreferences:
    """Recipient projection alongside Traits, using its source evidence and recency curve.
    KIN's own Traits rows are never overwritten by a recipient's feedback.
    """
    def __init__(self, memory):
        self.memory=memory;self.engine=memory.engine;self.scope=memory.scope
        with self.engine.db.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS mind_recipient_preferences(scope TEXT,person_id TEXT,source_id TEXT,topic TEXT,data TEXT,PRIMARY KEY(scope,person_id,source_id,topic))')

    def observe(self, *, person, source_id, topic, signal, quote='', share_id=None):
        if person not in PEOPLE or signal not in {'like','dislike','engaged','silence'}:raise ValueError('Invalid feedback')
        if not isinstance(topic,str) or not 1<=len(topic.strip())<=120:raise ValueError('Bounded topic required')
        with self.engine.db.connect(write=True) as conn:
            a=self.memory.annotation(conn,source_id)
            if not a or a['speaker_person_id']!=person:raise Conflict('Feedback belongs to its speaker')
            row=conn.execute('SELECT data,occurred_at FROM sources WHERE id=? AND scope=? AND deleted=0',(source_id,self.scope.key())).fetchone()
            if not row:raise Missing(source_id)
            if signal=='silence':return {'state':'no-signal'}
            if signal in {'like','dislike'} and (not quote or quote not in self.engine.source(source_id,content=True).read_text(encoding='utf-8')):raise Conflict('Explicit preference needs original words')
            if share_id:
                share=conn.execute("SELECT data FROM mind_memory_nodes WHERE scope=? AND id=? AND kind='share'",(self.scope.key(),share_id)).fetchone()
                if not share or json.loads(share[0]).get('recipient_person_id')!=person:raise Conflict('Feedback share belongs to another recipient')
            data={'person_id':person,'source_id':source_id,'topic':topic,'signal':signal,'quote':quote,'share_id':share_id,'at':row['occurred_at'],'basis':'explicit' if signal in {'like','dislike'} else 'inferred'}
            old=conn.execute('SELECT data FROM mind_recipient_preferences WHERE scope=? AND person_id=? AND source_id=? AND topic=?',(self.scope.key(),person,source_id,topic)).fetchone()
            if old and json.loads(old[0])!=data:raise Conflict('Feedback evidence reused differently')
            conn.execute('INSERT OR IGNORE INTO mind_recipient_preferences VALUES(?,?,?,?,?)',(self.scope.key(),person,source_id,topic,dumps(data)))
            return data

    def view(self, person, at):
        from .traits import support_strength
        result={}
        with self.engine.db.connect() as conn:
            for row in conn.execute('SELECT data FROM mind_recipient_preferences WHERE scope=? AND person_id=? ORDER BY json_extract(data,\'$.at\')',(self.scope.key(),person)):
                d=json.loads(row[0])
                if not self.memory.visible(conn,d['source_id'],person):continue
                topic=result.setdefault(d['topic'],{'topic':d['topic'],'score':0,'evidence':[],'basis':'inferred'})
                strength=support_strength([{'polarity':'support','at':d['at']}],at)
                # Explicit statements dominate weak engagement. Silence has no row.
                if d['signal'] in {'like','dislike'}:topic.update(score=(4 if d['signal']=='like' else -4)*strength,basis='explicit')
                elif topic['basis']!='explicit':topic['score']=min(1,topic['score']+0.25*strength)
                topic['evidence'].append(d)
        return sorted(result.values(),key=lambda d:-d['score'])


def select_sharing_topics(candidates, preferences, limit=6):
    """A small recipient projection; one fresh topic alongside familiar interests.
    Scores are suggestions, not psychological facts or changes to KIN's own Traits.
    """
    known={p['topic'].casefold():p['score'] for p in preferences}
    familiar=[];novel=[]
    for item in candidates:
        haystack=' '.join([item.get('text',''),*item.get('topic_tags',[])]).casefold()
        matches=[score for topic,score in known.items() if topic in haystack]
        score=sum(matches)
        if any(value<0 for value in matches):continue
        found={**item,'preference_score':score,'selection_basis':'recipient-feedback' if matches else 'new-topic'}
        (familiar if score>0 else novel).append(found)
    familiar.sort(key=lambda x:-x['preference_score'])
    if familiar and novel:return familiar[:max(1,limit-1)]+novel[:1]
    return (familiar or novel)[:limit]


def contact_preference(memory, person, request=None):
    """Recipient pacing in the existing sourced change ledger; no global affect rewrite."""
    if person not in PEOPLE:raise ValueError('Unknown contact person')
    state_key='contact-preference:'+person
    with memory.engine.db.connect(write=bool(request)) as conn:
        row=conn.execute('SELECT data FROM mind_companion_changes WHERE scope=? AND command_id=?',(memory.scope.key(),state_key)).fetchone()
        current=json.loads(row[0]) if row else {'person_id':person,'revision':0}
        if request is None:return current
        key=state_key+':'+request['command_id'];prior=conn.execute('SELECT data FROM mind_companion_changes WHERE scope=? AND command_id=?',(memory.scope.key(),key)).fetchone()
        if prior:
            saved=json.loads(prior[0])
            if saved['intent']!=request:raise Conflict('Contact command changed')
            return current
        if type(request.get('wait_for_reply')) is not bool or request['expected_revision']!=current['revision']:raise Conflict('Contact preference revision changed')
        if not request.get('evidence_ids'):raise ValueError('Contact preference needs original speaker evidence')
        for sid in request['evidence_ids']:
            annotation=memory.annotation(conn,sid)
            if not annotation or annotation['speaker_person_id']!=person:raise Conflict('Contact preference belongs to another speaker')
        result={'person_id':person,'revision':current['revision']+1,'wait_for_reply':request['wait_for_reply'],'evidence_ids':request['evidence_ids'],'reason':request['reason']}
        conn.execute('INSERT OR REPLACE INTO mind_companion_changes VALUES(?,?,?)',(memory.scope.key(),state_key,dumps(result)))
        conn.execute('INSERT INTO mind_companion_changes VALUES(?,?,?)',(memory.scope.key(),key,dumps({'intent':request,'result':result})))
        return result
