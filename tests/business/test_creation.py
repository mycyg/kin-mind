import json

from pathlib import Path

import pytest

from eventmem.core.db import Conflict

from kin_mind.creation import accept_result

from kin_mind.memory import fingerprint_file

from test_autonomous_plans import env, create, decide

def ready(env,tmp_path):
    mind,plans,*_=env
    p=decide(env,create(env));run=plans.claim('create','worker')['run']
    root=tmp_path/'creator';root.mkdir();file=root/'result.json';file.write_text('{"total":10}')
    result={'state':'produced','summary':'Total is 10','remaining':[], 'verification':['Parsed JSON'],
        'artifacts':[fingerprint_file(file)], 'receipt':{'model':'gpt-6-astra','run_id':run['id'],'thread_id':'isolated-native','exit_code':0,'workspace':str(root)}}
    config={'creation_directory':str(root),'creation_model':'gpt-6-astra','agent_version':'planning-v1'}
    return mind,plans,run,result,config

class Review:
    def structured(self,name,schema,system,context,**kwargs):
        assert context['verified_artifacts'][0]['inspection']['excerpt']=='{"total":10}'
        return schema.model_validate({'complete':True,'reason':'The inspected content meets the numeric criterion','artifact_hashes':[context['verified_artifacts'][0]['sha256']]}),{'provider':'deepseek','reasoning':'high'}

def test_creator_requires_content_review_and_same_manifest_retries(env,tmp_path):
    mind,plans,run,result,config=ready(env,tmp_path)
    request={'run_id':run['id'],'owner':'worker','fence':1,'result':result}
    settled=accept_result(mind,config,request,Review())
    assert settled['state']=='completed' and settled['result']['verified']
    assert accept_result(mind,config,request,Review())==settled

def test_unconfigured_creator_model_defaults_to_sol_medium_executor(env,tmp_path):
    mind,_,run,result,config=ready(env,tmp_path)
    config.pop('creation_model')
    result['receipt']['model']='gpt-6-sol'
    settled=accept_result(mind,config,{'run_id':run['id'],'owner':'worker','fence':1,'result':result},Review())
    assert settled['state']=='completed'

def test_changed_file_or_wrong_native_model_never_completes(env,tmp_path):
    mind,plans,run,result,config=ready(env,tmp_path)
    request={'run_id':run['id'],'owner':'worker','fence':1,'result':result}
    result['receipt']['model']='unverified-model'
    with pytest.raises(Conflict):accept_result(mind,config,request,Review())
    result['receipt']['model']='gpt-6-astra';Path(result['artifacts'][0]['path']).write_text('changed')
    with pytest.raises(Conflict):accept_result(mind,config,request,Review())
    assert plans.read(identifier=run['plan_id'])['plans'][0]['steps'][0]['state']=='running'

def test_review_timeout_keeps_checkpoint_and_releases_executor(env,tmp_path):
    mind,plans,run,result,config=ready(env,tmp_path)
    class Timeout:
        def structured(self,*args,**kwargs):raise TimeoutError()
    settled=accept_result(mind,config,{'run_id':run['id'],'owner':'worker','fence':1,'result':result},Timeout())
    assert settled['state']=='interrupted' and settled['result']['artifacts']
    assert settled['result']['review_receipt']['usage_status']=='unknown'
    assert plans.read(identifier=run['plan_id'])['plans'][0]['steps'][0]['state']=='waiting'

def test_downstream_delivery_and_executor_notes_do_not_block_current_step(env,tmp_path):
    mind,plans,run,result,config=ready(env,tmp_path)
    result['remaining']=['Deliver later when appropriate', 'Owner response is still pending']
    class Scoped(Review):
        def structured(self,name,schema,system,context,**kwargs):
            assert 'step_remaining' in system and '后续' in system
            decision,receipt=super().structured(name,schema,system,context,**kwargs)
            return decision.model_copy(update={'downstream':context['result']['remaining']}),receipt
    settled=accept_result(mind,config,{'run_id':run['id'],'owner':'worker','fence':1,'result':result},Scoped())
    assert settled['state']=='completed'
    assert settled['result']['completion_review']['downstream']==result['remaining']

def test_required_verification_gap_is_resumable_and_visible_in_compact_plan(env,tmp_path):
    from kin_mind.decision_context import compact_plan
    mind,plans,run,result,config=ready(env,tmp_path)
    class Gaps(Review):
        def structured(self,*args,**kwargs):
            decision,receipt=super().structured(*args,**kwargs)
            return decision.model_copy(update={'complete':False,'step_remaining':['Required rendering did not finish']}),receipt
    settled=accept_result(mind,config,{'run_id':run['id'],'owner':'worker','fence':1,'result':result},Gaps())
    assert not settled['result']['verified'] and settled['result']['phase']=='needs_verification'
    plan=compact_plan(plans.read(identifier=run['plan_id'])['plans'][0])
    receipt=plan['steps'][0]['receipts'][-1]
    assert receipt['verification_gaps']==['Required rendering did not finish']
    assert receipt['resume_action']=='inspect-checkpoint-and-run-missing-checks'
    assert receipt['checkpoint']==result['receipt']['workspace']

def test_artifact_change_during_review_is_not_current_verified_evidence(env,tmp_path):
    mind,plans,run,result,config=ready(env,tmp_path)
    class Change(Review):
        def structured(self,*args,**kwargs):
            decision,receipt=super().structured(*args,**kwargs)
            Path(result['artifacts'][0]['path']).write_text('{"total":999}')
            return decision,receipt
    with pytest.raises(Conflict,match='during completion review'):
        accept_result(mind,config,{'run_id':run['id'],'owner':'worker','fence':1,'result':result},Change())
    assert plans.read(identifier=run['plan_id'])['plans'][0]['steps'][0]['state']=='running'

def test_retry_after_artifact_ingestion_reuses_original_observation(env,tmp_path,monkeypatch):
    from kin_mind.memory import MemoryContinuity
    mind,plans,run,result,config=ready(env,tmp_path)
    original=MemoryContinuity.ingest
    failed=False
    def interrupted(self,event):
        nonlocal failed
        if event['kind']=='task-result' and not failed:
            failed=True
            raise TimeoutError('simulated worker interruption after artifact commit')
        return original(self,event)
    monkeypatch.setattr(MemoryContinuity,'ingest',interrupted)
    request={'run_id':run['id'],'owner':'worker','fence':1,'result':result}
    with pytest.raises(TimeoutError):accept_result(mind,config,request,Review())
    from datetime import timedelta
    env[3][0]+=timedelta(seconds=3)
    settled=accept_result(mind,config,request,Review())
    assert settled['state']=='completed'
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_runtime_events WHERE kind='artifact-created'").fetchone()[0]==1
