import json
import time
from types import SimpleNamespace

import pytest
from eventmem.core.db import Conflict, dumps
from kin_mind.appraisal import Appraisals
from kin_mind.recovery import recover_history
from test_operational_recovery import Provider

pytest_plugins = ('test_memory_continuity',)


@pytest.mark.parametrize('reason', ['capacity', 'foreground'])
def test_admission_waits_do_not_quarantine_and_resume_with_same_id(system, reason):
    mind, memory, source, _ = system
    memory.configure({'operational_lanes': True})
    jobs = Appraisals(mind)
    job = jobs.enqueue([source('history')], 'fixture-v1', stimulus='memory-enrichment')
    provider = Provider(); provider.engine = mind.engine
    with mind.engine.db.connect(write=True) as conn:
        if reason == 'capacity':
            for n in range(2):
                conn.execute('INSERT INTO mind_model_leases VALUES(?,?,?,?)', (str(n), 'background', time.time()+3600, '{}'))
        else:
            conn.execute('INSERT INTO mind_foreground_leases VALUES(?,?,?)', (mind.scope.key(), 'active-user', time.time()+3600))
    for _ in range(3):
        result = jobs.run_one(provider, lane='enrichment')
        assert result['id'] == job['id'] and result['state'] == 'pending'
        assert result['attempts'] == 0 and result['waiting_reason'].startswith('deepseek-')
        with mind.engine.db.connect(write=True) as conn:
            assert conn.execute('SELECT available FROM mind_appraisals WHERE id=?', (job['id'],)).fetchone()[0] > time.time()
            conn.execute('UPDATE mind_appraisals SET available=0 WHERE id=?', (job['id'],))
    assert provider.calls == [] and result['admission_waits'] == 3
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('DELETE FROM mind_model_leases')
        conn.execute('DELETE FROM mind_foreground_leases')
    completed = Appraisals(mind).run_one(provider, lane='enrichment')
    assert completed['state'] == 'complete' and completed['attempts'] == 1
    assert 'waiting_reason' not in completed and len(provider.calls) == 1


def test_recovery_only_admission_failures_and_no_old_model_seed(system):
    mind, memory, source, _ = system
    memory.configure({'operational_lanes': True})
    jobs = Appraisals(mind)
    job = jobs.enqueue([source('history')], 'fixture-v1', stimulus='memory-enrichment')
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute('SELECT data FROM mind_appraisals WHERE id=?', (job['id'],)).fetchone()[0])
        data.update(error='Missing', proposed_result={'invalid':'old'}, receipt={'model':'deepseek-flash'})
        conn.execute("UPDATE mind_appraisals SET state='needs-repair',attempts=2,data=? WHERE id=?", (dumps(data),job['id']))
    args=dict(job_ids=[job['id']], command_id='approved', source='owner approval', workers_stopped=True, admission_only=True)
    with pytest.raises(Conflict):recover_history(mind, **args)
    with mind.engine.db.connect(write=True) as conn:
        data['error']='deepseek-background-capacity'
        conn.execute('UPDATE mind_appraisals SET data=? WHERE id=?',(dumps(data),job['id']))
    recovered=recover_history(mind, **args)
    assert recover_history(mind, **args)==recovered
    with mind.engine.db.connect() as conn:
        row=conn.execute('SELECT * FROM mind_appraisals WHERE id=?',(job['id'],)).fetchone()
    saved=json.loads(row['data'])
    assert row['state']=='pending' and saved['seed_rejected']
    assert saved['recovery_history'][0]['receipt']['model']=='deepseek-flash'
    assert 'proposed_result' not in saved


def test_capacity_is_configurable_database_wide_and_foreground_still_has_priority(system):
    from kin_mind.model_runtime import configure_capacity, model_slot, ModelAdmissionWait
    mind,*_=system
    configure_capacity(mind.engine,4)
    provider=SimpleNamespace(engine=mind.engine,background=True)
    with mind.engine.db.connect(write=True) as conn:
        for n in range(3):conn.execute('INSERT INTO mind_model_leases VALUES(?,?,?,?)',(str(n),'background',time.time()+3600,'{}'))
    with model_slot(provider,'fourth'):
        with mind.engine.db.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM mind_model_leases').fetchone()[0]==4
    configure_capacity(mind.engine,3)
    with pytest.raises(ModelAdmissionWait):
        with model_slot(provider,'blocked'):pass
    configure_capacity(mind.engine,4)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('INSERT INTO mind_foreground_leases VALUES(?,?,?)',(mind.scope.key(),'user',time.time()+3600))
    with pytest.raises(ModelAdmissionWait,match='foreground'):
        with model_slot(provider,'foreground-first'):pass
    with model_slot(SimpleNamespace(engine=mind.engine,background=False),'foreground'):pass
    with pytest.raises(ValueError):configure_capacity(mind.engine,99)
