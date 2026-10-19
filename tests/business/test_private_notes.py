import json
import pytest
from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput, RecallRequest, now
from eventmem.core.db import Conflict
from kin_mind.state import Mind
from kin_mind.memory import MemoryContinuity
from kin_mind.private_notes import PrivateNotes


def setup(tmp_path):
    mind = Mind(Engine(tmp_path/'memory'), Scope(persona='note-test'), clock=now)
    source = mind.engine.receive(SourceInput(namespace='kin-owner-input',key='input-1',scope=mind.scope,text='今天下雨了',occurred_at=mind.clock(),extract=False))
    MemoryContinuity(mind)
    return mind, source


def test_saved_once_restart_time_message_and_private_search(tmp_path):
    mind, source=setup(tmp_path);notes=PrivateNotes(mind)
    req={'id':'note-1','group_id':'reply-1','input_id':'input-1','text':'雨声让我有点想她，也许她正在忙。','reply_text':'一起听雨吧。'}
    assert notes.save(req)['state']=='saved'
    notes.save(req)
    with pytest.raises(Conflict):notes.save({**req,'text':'后来改写'})
    again=PrivateNotes(Mind(Engine(mind.engine.db.root),mind.scope,clock=mind.clock))
    result=again.read(input_id='input-1',since='2026-10-17T00:00:00Z')
    assert len(result['notes'])==1 and result['notes'][0]['text']==req['text']
    assert result['notes'][0]['evidence_ids']==[source['id']]
    assert again.read(query='雨声',semantic=False)['notes'][0]['id']=='note-1'
    assert again.recent()['notes'][0]['delivery_state']=='not-confirmed'
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone()
        assert not conn.execute("SELECT 1 FROM records WHERE data LIKE '%雨声让我%'").fetchone()
    # Delete reuses the source dependency and erasure transaction rather than retaining a private copy.
    from kin_mind.erasure import _recall_rows
    with mind.engine.db.connect(write=True) as conn:_recall_rows(conn,{source['id']},{source['id']})
    assert again.read()['notes']==[]


def test_semantic_paraphrase_and_scope(tmp_path,monkeypatch):
    mind,_=setup(tmp_path);notes=PrivateNotes(mind)
    notes.save({'id':'one','group_id':'a','text':'想靠近她一点。'})
    notes.save({'id':'two','group_id':'b','text':'想研究一下代码。'})
    from kin_mind import recall_admission
    def embed(engine,scope,entries,deadline):
        return [[1,0]]+[[1,0] if '靠近' in t else [0,1] for _,t in entries[1:]],len(entries)
    monkeypatch.setattr(recall_admission,'embed',embed)
    assert notes.read(query='依恋')['notes'][0]['id']=='one'
    assert notes.read(query='依恋')['recall']=='local-semantic'
    other=PrivateNotes(Mind(mind.engine,Scope(persona='other'),clock=mind.clock))
    assert other.read()['notes']==[]


def test_capacity_is_bounded_without_rewriting_notes(tmp_path,monkeypatch):
    mind,_=setup(tmp_path);notes=PrivateNotes(mind)
    from kin_mind import private_notes
    monkeypatch.setattr(private_notes,'MAX_NOTES',2)
    for i in range(3):notes.save({'id':str(i),'group_id':str(i),'text':'此刻很平静。','at':f'2026-10-17T12:0{i}:00Z'})
    assert [e['id'] for e in notes.read()['notes']]==['2','1']


def test_historical_guess_keeps_later_context_and_expires_from_recent(tmp_path):
    mind,_=setup(tmp_path);notes=PrivateNotes(mind)
    notes.save({'id':'guess','group_id':'a','text':'也许她不开心。','at':'2020-01-01T10:00:00Z'})
    notes.save({'id':'update','group_id':'b','text':'她说刚才在打游戏，原来我猜错了。','at':'2020-01-01T10:01:00Z'})
    old=notes.read(identifier='guess')['notes'][0]
    assert old['text']=='也许她不开心。'
    assert old['later_notes'][0]['id']=='update'
    assert notes.recent()['notes']==[]


def test_resident_worker_saves_and_reads_notes(tmp_path):
    import io
    from kin_mind.host import serve
    frames=[{'id':'1','action':'private-note-save','args':{'id':'n','group_id':'g','text':'有点期待。'}},
            {'id':'2','action':'private-notes','args':{'identifier':'n'}}]
    out=io.StringIO()
    serve({'root':str(tmp_path/'resident'),'scope':{'persona':'note-test'}},io.StringIO(''.join(json.dumps(f)+'\n' for f in frames)),out)
    results=[json.loads(line) for line in out.getvalue().splitlines()]
    assert all(r['ok'] for r in results),results
    assert results[1]['result']['notes'][0]['text']=='有点期待。'


def test_receipts_keep_partial_delivery_distinct_from_note_and_reply(tmp_path):
    mind,_=setup(tmp_path);notes=PrivateNotes(mind)
    notes.save({'id':'n','group_id':'reply-g','text':'有点期待。','reply_text':'第一句\n第二句'})
    memory=MemoryContinuity(mind);memory.configure({'records':True})
    for index,state in enumerate(['accepted','unconfirmed']):
        memory.ingest({'id':f'evt-{index}','kind':'delivery','at':mind.clock(),'channel':'feishu','delivery_id':'reply-g',
                       'bubble_id':f'b-{index}','expected_bubbles':2,'text':['第一句','第二句'][index],
                       'state':state,**({'message_id':'om-1'} if state=='accepted' else {})})
    restored=PrivateNotes(Mind(Engine(mind.engine.db.root),mind.scope,clock=mind.clock))
    entry=restored.read(group_id='reply-g')['notes'][0]
    assert entry['text']=='有点期待。'
    assert entry['delivery'][0]['state']=='partial'
    assert {b['state'] for b in entry['delivery'][0]['bubbles']}=={'accepted','unconfirmed'}


def test_real_outputs_survive_restart(tmp_path):
    import os
    from pathlib import Path
    fixtures=os.environ.get('KIN_TEST_NOTE_FIXTURES')
    if not fixtures:pytest.skip('live model fixtures not supplied')
    mind,_=setup(tmp_path);notes=PrivateNotes(mind)
    for model in ['k3','deepseek-flash','gpt-6.1-sol']:
        data=json.loads((Path(fixtures)/('note-'+model)/'answer.txt').read_text())
        notes.save({'id':model,'group_id':'g-'+model,'input_id':'input-1','text':data['private_note'],'reply_text':'\n'.join(data['bubbles']),'model':model})
    restarted=PrivateNotes(Mind(Engine(mind.engine.db.root),mind.scope,clock=mind.clock))
    for model in ['k3','deepseek-flash','gpt-6.1-sol']:
        original=json.loads((Path(fixtures)/('note-'+model)/'answer.txt').read_text())
        assert restarted.read(identifier=model)['notes'][0]['text']==original['private_note']


def test_appraisal_sees_only_private_role_material_and_erasure_roots(tmp_path):
    mind, source = setup(tmp_path)
    notes = PrivateNotes(mind)
    notes.save({'id':'n','group_id':'g','input_id':'input-1','text':'我想安静一会儿。','reply_text':'公开答复'})
    context = notes.appraisal_context()
    note = context['notes'][0]
    assert note['basis'] == 'role_note' and note['visibility'] == 'kin_only'
    assert note['evidence_ids'] == [source['id']]
    assert 'reply_text' not in note and 'delivery' not in note and 'later_notes' not in note
    from kin_mind.erasure import shown_ids, _recall_rows
    with mind.engine.db.connect() as conn:
        assert source['id'] in shown_ids(conn, context)
    with mind.engine.db.connect(write=True) as conn:
        _recall_rows(conn, {source['id']}, {source['id']})
    assert notes.appraisal_context()['notes'] == []
