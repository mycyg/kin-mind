import pytest
from eventmem.core.db import Conflict
from kin_mind.reply_review import ReplyReviews
from test_event_graph import system, findings, send


class Review:
    def __init__(self, refs=None, action='allow', decision='ordinary'):
        self.calls=[]; self.refs=refs or []; self.action=action; self.decision=decision
    def structured(self,name,schema,prompt,context,**kwargs):
        self.calls.append(context)
        return schema.model_validate({'action':self.action,'reason':'Whole reply context',
            'bubbles':[{'draft_id':b['draft_id'],'decision':self.decision,'reason':'Current input and complete body',
                        'references':self.refs} for b in context['bubbles']]}),{'model':'deepseek-flash','reasoning':'high'}


def request(memory, mind, text='Please repeat the joke'):
    memory.ingest({'id':'input','kind':'owner-message','at':mind.clock(),'text':text})
    return {'entries':[{'draft_id':f'd{i}','reply_id':'input','text':t} for i,t in enumerate(['Here it is:', 'The entire copyable body.','Goodnight'])]}


def test_one_whole_decision_includes_actual_input_and_reuses_frozen_evidence(system):
    mind,memory,*_=system; req=request(memory,mind); provider=Review(); api=ReplyReviews(memory.sharing)
    first=api.preflight(req,provider)
    assert first['state']=='ready' and len(provider.calls)==1
    assert provider.calls[0]['current_inputs'][0]['text']=='Please repeat the joke'
    assert [x['text'] for x in first['checked']]==[x['text'] for x in req['entries']]
    reused = api.preflight({**req,'frozen':True})
    assert reused['reused'] and reused['receipt']['usage_status']=='reused'
    assert reused['receipt']['usage']=={} and reused['receipt']['elapsed_ms']==0
    memory.ingest({'id':'new-input','kind':'owner-message','at':mind.clock(),'text':'Another question'})
    assert api.preflight({**req,'frozen':True})['state']=='pending'
    assert len(provider.calls)==1


def test_one_duplicate_cannot_mark_whole_reply_delivered_or_silent(system):
    mind,memory,*_=system; req=request(memory,mind)
    held=ReplyReviews(memory.sharing).preflight(req,Review(action='hold',decision='duplicate'))
    assert held['state']=='pending' and len(held['checked'])==3
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mind_share_coverage').fetchone()[0]==0


def test_shared_fact_requires_supported_continuation_but_multiple_bubbles_can_reference_it(system):
    mind,memory,*_=system; unit=findings(system)[0];send(system,unit)
    req=request(memory,mind)
    for entry in req['entries']:
        entry['references']=[{'unit_id':unit['id'],'version':1,'mode':'new'}]
    api=ReplyReviews(memory.sharing)
    assert api.preflight(req,Review(refs=req['entries'][0]['references'],decision='new'))['state']=='pending'
    refs=[{'unit_id':unit['id'],'version':1,'mode':'retelling','reason':'The current user asked to hear it again'}]
    assert api.preflight(req,Review(refs=refs,decision='continuation'))['state']=='ready'


def test_changed_input_during_model_call_never_commits(system):
    mind,memory,*_=system;req=request(memory,mind)
    class Changed(Review):
        def structured(self,*args,**kwargs):
            result=super().structured(*args,**kwargs)
            memory.ingest({'id':'new-input','kind':'owner-message','at':mind.clock(),'text':'Stop'})
            return result
    with pytest.raises(Conflict,match='changed during review'):
        ReplyReviews(memory.sharing).preflight(req,Changed())


def test_missing_or_reordered_bubbles_and_changed_frozen_body_are_rejected(system):
    mind,memory,*_=system;req=request(memory,mind);api=ReplyReviews(memory.sharing)
    class Incomplete(Review):
        def structured(self,*args,**kwargs):
            result,receipt=super().structured(*args,**kwargs)
            return result.model_copy(update={'bubbles':result.bubbles[:1]}),receipt
    with pytest.raises(Conflict,match='omitted'):api.preflight(req,Incomplete())
    api.preflight(req,Review())
    req['entries'][1]['text']='Short replacement'
    with pytest.raises(Conflict,match='body changed'):api.preflight(req,Review())


def test_schema_repair_is_model_led_and_preserves_failed_call_usage(system):
    mind,memory,*_=system;req=request(memory,mind);api=ReplyReviews(memory.sharing)
    class Repair(Review):
        timeout=120
        def structured(self,name,schema,prompt,context,**kwargs):
            if not self.calls:
                self.calls.append(context)
                self.failure_receipt={'usage':{'output_tokens':17},'outcome':'schema-invalid'}
                schema.model_validate({'action':'allow','reason':'First','bubbles':[{'draft_id':'d0','decision':'ordinary','reason':'one','references':[{'unit_id':'f','version':1,'mode':'continuation'}]}]})
            assert context['validation_feedback'][0]['type']=='literal_error'
            assert self.timeout<120
            return super().structured(name,schema,prompt,context,**kwargs)
    provider=Repair();result=api.preflight(req,provider)
    assert result['state']=='ready' and len(provider.calls)==2
    assert result['receipt']['schema_repair']['rejected_call']['usage']['output_tokens']==17
    assert provider.timeout==120


def test_a_changed_reply_body_is_typed_as_frozen_content_not_as_a_changed_command(system):
    """Stage 2 WP3: a reply body is a content freeze, not a command. Both freezes now carry
    a code of their own, so stage 3's continuation groups can act on it instead of the host
    turning every one of them into an endless pending retry."""
    from kin_mind.conflicts import classify
    mind,memory,*_=system;req=request(memory,mind);api=ReplyReviews(memory.sharing)
    api.preflight(req,Review())
    req['entries'][0]['text']='A different body'
    with pytest.raises(Conflict) as frozen:api.preflight(req,Review())
    registration={'reply_id':'turn-one','bubbles':[{'text':'A public sentence','references':[]}]}
    memory.sharing.register(registration)
    with pytest.raises(Conflict) as registered:
        memory.sharing.register({**registration,'bubbles':[{'text':'Other body','references':[]}]})
    for error in (frozen.value,registered.value):
        assert classify(error) == ('runtime','reply-content-changed','block')
