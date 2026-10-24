import json
import pytest
from test_companions import world, X, Y, SCOPE
from eventmem.core.db import Missing
from kin_mind.companion_view import CompanionView, companion_scope
from kin_mind.companions import FAMILY

@pytest.mark.parametrize('person',[X,Y,FAMILY])
def test_shared_memory_keeps_content_attribution_and_explicit_annotations(world,person):
    e,m,source=world
    sid=source('private-origin',X,classification='private',text='一段私密经历',sharing='private')
    view=CompanionView(e,SCOPE,person,'chat:'+person)
    original=e.source(sid)
    value=m.source_view(sid,person)
    assert value['text']=='一段私密经历'
    assert value['classification']=='private' and value['sharing']=='private'
    assert value['speaker_person_id']==X
    assert value['occurred_at']==original['occurred_at']
    assert view.filter({'items':[value]})=={'items':[value]}
    with e.db.connect() as conn:
        facts=view.provenance([sid],conn)[0]
        assert facts['classification']=='private' and facts['speaker_person_id']==X
    assert e.source(sid)==original

def test_source_ancestry_and_ids_in_prose_do_not_veto_an_independent_experience(world):
    e,m,source=world
    first=source('y-internal',Y,classification='private')
    derived=source('own-exploration',parents=[first],text='刚想到一个小事')
    view=CompanionView(e,SCOPE,X,'chat:'+X)
    value={'id':derived,'text':'引用到 '+first,'dependencies':[{'id':first}]}
    assert view.filter(value)==value
    assert m.source_view(derived,X)['text']=='刚想到一个小事'

def test_deleted_or_foreign_scope_sources_remain_unavailable(world):
    from eventmem.core.models import SourceInput,Scope
    e,m,source=world;sid=source('deleted')
    other=e.receive(SourceInput(namespace='synthetic',key='foreign',scope=Scope(persona='other'),text='别的存储域',extract=False))['id']
    with e.db.connect(write=True) as conn:conn.execute('UPDATE sources SET deleted=1 WHERE id=?',(sid,))
    for person in (X,Y,FAMILY):
        for target in (sid,other):
            with pytest.raises(Missing):m.source_view(target,person)

def test_legacy_notes_and_shared_affect_preserve_their_original_facts(world):
    e,m,source=world;sid=source('cause',classification='intimate')
    value={'notes':[{'text':'旧札记','occurred_at':None}],
           'dimensions':{'mood':{'value':71,'text':'想起那件事','evidence_ids':[sid]}}}
    assert CompanionView(e,SCOPE,Y,'chat:'+Y).filter(value)==value

def test_context_pack_does_not_drop_a_source_for_its_original_recipient(world):
    from kin_mind.state import Mind
    from kin_mind.context import Contexts
    e,m,source=world;first=source('one');second=source('two',Y,classification='private')
    with companion_scope(CompanionView(e,SCOPE,Y,'chat:'+Y)):
        packed=Contexts(Mind(e,SCOPE)).pack([{'id':first,'revision':1,'text':'FIRST_CONTEXT','basis':'observed'},
          {'id':second,'revision':1,'text':'SECOND_CONTEXT','basis':'observed'}],'',2000,allow_model=False)
    assert 'FIRST_CONTEXT' in json.dumps(packed) and 'SECOND_CONTEXT' in json.dumps(packed)

def test_ready_contact_handle_survives_shared_source_projection(world,monkeypatch):
    from kin_mind.companion_host import dispatch_companion
    from kin_mind.state import Mind
    from kin_mind import host
    e,m,source=world;sid=source('origin',classification='private')
    draft={'id':'synthetic-contact','state':'drafting','recipient_person_id':Y,'text':'想说一句','source_ids':[sid]}
    monkeypatch.setattr(host,'_dispatch',lambda *args:draft)
    monkeypatch.setattr(Mind,'settle_contact',lambda *args,**kwargs:pytest.fail('shared memory must not cancel a contact'))
    assert dispatch_companion(e,{'scope':SCOPE.model_dump()},'companion-host-call',
      {'person_id':Y,'conversation_key':'chat:'+Y,'action':'claim','request':{}})==draft
