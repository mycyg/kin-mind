"""One real appraisal transaction keeps request ownership through plans and actions."""
from datetime import timedelta
import json
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.autonomy_models import PlanChange, ActionDecision
from kin_mind.memory import MemoryContinuity
from kin_mind.plans import AutonomousPlans
from kin_mind import recipient
from test_kin_mind import setup

X='person:xiaoguang';Y='person:yiyi';G='group:family'
class Reviewer:
    def __init__(self, proposal): self.proposal=proposal;self.context=None
    def appraise(self, context):
        self.context=context
        return self.proposal, {'provider':'deepseek','model':'synthetic','reasoning':'high'}

def arrange(setup):
    mind,source,clock=setup
    MemoryContinuity(mind).configure({'records':True,'semantic':True,'semantic_actions':True,'autonomous_plans':True,'operational_lanes':True})
    mind.unified_targets={p:{'route':{'conversation_key':'chat:'+p}} for p in (X,Y,G)}
    return mind,source,clock

def plan(sid, person, key='same-goal'):
    return PlanChange(action='create',key=key,recipient_person_id=person,goal='整理她给出的清单',motivation='完成她明确交代的事',reason='已给出清单与请求',evidence_ids=[sid],steps=[{'id':'do','actor':'create','goal':'整理清单','completion':'交付实际成果'}])

def decision(sid, person, key='same-goal',rev=1):
    return ActionDecision(plan_id=key,recipient_person_id=person,step_id='do',expected_revision=rev,action='wait',reason='等待补充清单附件',evidence_ids=[sid])

def test_same_key_two_recipients_create_and_decide_in_one_appraisal(setup):
    mind,source,clock=arrange(setup);sid=source('clear-two-requests')
    jobs=Appraisals(mind);job=jobs.enqueue([sid],'synthetic-v1')
    reviewer=Reviewer(Appraisal(reason='分别记下两人的请求',plan_changes=[plan(sid,p) for p in (Y,G)],action_decisions=[decision(sid,p) for p in (Y,G)]))
    result=jobs.run_one(reviewer)
    assert result['state']=='complete',result
    assert not result.get('rejected_sections'),result
    made=[]
    for person in (Y,G):
        with recipient.target_scope(mind,person):
            p=AutonomousPlans(mind).read()['plans'][0];made.append(p['id'])
            assert p['recipient_person_id']==person and p['steps'][0]['state']=='waiting'
            assert p['steps'][0]['decision']['action']=='wait'
    assert len(set(made))==2
    assert AutonomousPlans(mind).read()['plans']==[]
    assert recipient.current()==X


def test_existing_other_plan_by_id_keeps_owner_and_fresh_execution_view(setup):
    mind,source,clock=arrange(setup);sid=source('y-request');plans=AutonomousPlans(mind)
    original=plans.manage({'command_id':'y-create',**plan(sid,Y).model_dump()})
    Appraisals(mind).enqueue([sid],'synthetic-v1')
    reviewer=Reviewer(Appraisal(reason='复评一一的原请求',action_decisions=[decision(sid,None,key=original['id'])]))
    result=Appraisals(mind).run_one(reviewer);assert result['state']=='complete',result
    assert not result.get('rejected_sections'),result
    shown=reviewer.context['recipient_targets'][Y]['plans'][0]
    assert shown['recipient_person_id']==Y and shown['steps'][0]['state']=='pending'
    assert shown['steps'][0]['execution']['plan_run'] is None
    with recipient.target_scope(mind,Y):
        saved=plans.read(original['id'])['plans'][0]
        assert saved['steps'][0]['state']=='waiting'
    assert AutonomousPlans(mind).read()['plans']==[]


def test_secondary_followup_keeps_origin_recipient(setup,monkeypatch):
    import kin_mind.appraisal as module
    mind,source,clock=arrange(setup);sid=source('y-followup')
    # Exercise the production follow-up insertion without depending on one
    # semantic section's refusal rules, which are unrelated to its ownership.
    monkeypatch.setattr(module,'asks_again',lambda *args:True)
    with recipient.target_scope(mind,Y):
        jobs=Appraisals(mind);parent=jobs.enqueue([sid],'synthetic-v1')
        result=jobs.run_one(Reviewer(Appraisal(reason='保留原对象')))
        assert result['state']=='complete',result
        with mind.engine.db.connect() as conn:
            row=conn.execute("SELECT data FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=? AND json_extract(data,'$.stimulus')='held-sections'",(parent['id'],)).fetchone()
            assert row and json.loads(row[0])['recipient_person_id']==Y
