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
