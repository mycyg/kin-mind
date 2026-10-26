"""A source-based view of the SAME store for one trusted conversation.

Conversation identity selects scheduling and relationship facts. Memory itself is
shared; this view preserves attribution without imposing semantic sharing decisions.
"""
import json
from eventmem.core.db import NAMED, named_in
from contextvars import ContextVar
from contextlib import contextmanager
ACTIVE_VIEW=ContextVar('kin_companion_view',default=None)
@contextmanager
def companion_scope(view):
    token=ACTIVE_VIEW.set(view)
    try:yield
    finally:ACTIVE_VIEW.reset(token)
from .companions import Companions, XIAOGUANG, YIYI, FAMILY

PROSE={'text','content','summary','excerpt','body','topic','title','description','reply_text','rendered_text','prompt','goal','note','answer','rendered','markdown','memoryContext'}

class CompanionView:
    def __init__(self, engine, scope, person, conversation):
        if person not in {XIAOGUANG,YIYI,FAMILY} or not conversation:raise ValueError('companion-view-identity-required')
        self.engine,self.scope,self.person,self.conversation=engine,scope,person,conversation
        self.memory=Companions(engine,scope)

    def visible_id(self, identifier, conn, seen=None):
        # Context pages keep the same source/record identity plus a segment cursor.
        named=NAMED.match(identifier)
        if named and identifier[len(named.group()):].startswith((':segment:', '@')):identifier=named.group()
        seen=set(seen or ())
        if identifier in seen:return False
        seen.add(identifier)
        if identifier.startswith('src_'):return self.memory.visible(conn,identifier,self.person)
        if identifier.startswith('mem_'):
            rows=conn.execute('SELECT source_id FROM evidence WHERE record_id=?',(identifier,)).fetchall()
            return bool(rows) and all(self.memory.visible(conn,r[0],self.person) for r in rows)
        # Non-source operational IDs are checked by their owning store. Scanning
        # every mind table here both duplicated that work and inferred privacy
        # from unrelated IDs appearing in ordinary prose.
        return None

    def require(self, identifier):
        with self.engine.db.connect() as conn:
            if self.visible_id(identifier,conn) is False:raise PermissionError('Memory belongs to another private conversation')

    def provenance(self, source_ids, conn):
        """Keep speaker and subject distinct; absent attribution stays unknown."""
        result=[]
        for sid in source_ids:
            if not self.memory.visible(conn,sid,self.person):continue
            row=conn.execute('SELECT data,occurred_at,received_at FROM sources WHERE id=? AND scope=? AND deleted=0',(sid,self.scope.key())).fetchone()
            if not row:continue
            metadata=json.loads(row['data']).get('metadata',{})
            annotation=self.memory.annotation(conn,sid) or {}
            result.append({'source_id':sid,
                'speaker_person_id':annotation.get('speaker_person_id',metadata.get('speaker_person_id')),
                'subject_ids':annotation.get('subject_ids',[]),
                'occurred_at':metadata.get('original_occurred_at',row['occurred_at']),
                'received_at':row['received_at'],
                'basis':annotation.get('basis','unknown')})
        return result

    def filter(self, value):
        """Present shared memory unchanged; attribution remains in each record."""
        if hasattr(value, 'model_dump'):
            return value.model_dump(mode='json')
        if isinstance(value, list):
            return [self.filter(item) for item in value]
        if isinstance(value, dict):
            return {key: self.filter(item) for key, item in value.items()}
        return value
