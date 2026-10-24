import json

from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.memory import MemoryAssessment, MemoryNote
from test_event_graph import system
from test_derived_erasure import answered, paid


def test_person_id_failure_reaches_next_model_with_field_and_target(system):
    mind, memory, source, _ = system
    sid = source('reference-input', '小光喜欢看星星。')
    jobs = Appraisals(mind)
    job = jobs.enqueue([sid], 'fixture-v1', origin='reflection', stimulus='memory-enrichment')
    seen = []

    class Model:
        def appraise(self, context):
            seen.append(context)
            paid(self)
            return answered(Appraisal(reason='整理记录', memory=MemoryAssessment(notes=[
                MemoryNote(key='stars', title='小光的兴趣', content='小光喜欢看星星。',
                           evidence_ids=[sid], about_ids=['person:xiaoguang'] if len(seen) == 1 else [])])))

    model = Model()
    first = jobs.run_one(model, job_id=job['id'])
    assert first['state'] != 'complete'
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute('SELECT data FROM mind_appraisals WHERE id=?', (job['id'],)).fetchone()[0])
        assert data['error_detail']['field'] == 'memory.notes[0].about_ids[0]'
        assert data['error_detail']['target'] == 'person:xiaoguang'
        assert data['missing_reference'] == 'person:xiaoguang'
        conn.execute('UPDATE mind_appraisals SET available=0 WHERE id=?', (job['id'],))
        assert conn.execute("SELECT COUNT(*) FROM records WHERE json_extract(data,'$.attributes.key')='stars'").fetchone()[0] == 0
    result = jobs.run_one(model, job_id=job['id'])
    assert result['state'] == 'complete', result
    assert seen[1]['previous_attempt']['target'] == 'person:xiaoguang'
    assert seen[1]['previous_attempt']['field'] == 'memory.notes[0].about_ids[0]'
    assert '身份 ID 不作记忆链接' in seen[1]['previous_attempt']['hint']
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records WHERE json_extract(data,'$.attributes.key')='stars'").fetchone()[0] == 1
    assert '人物' in MemoryNote.model_json_schema()['properties']['about_ids']['description']
