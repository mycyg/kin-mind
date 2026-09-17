import pytest
from eventmem.core.db import Conflict, digest, dumps
from kin_mind.reply_review import CHUNK_BUBBLES, CHUNK_CHARACTERS, RemainderReview, ReplyReview, ReplyReviews
from test_event_graph import system, findings, send


class Review:
    def __init__(self, refs=None, action='allow', decision='ordinary'):
        self.calls=[]; self.requests=[]; self.refs=refs or []; self.action=action; self.decision=decision
    def body(self, context):
        return {'action':self.action,'reason':'Whole reply context',
            'bubbles':[{'draft_id':b['draft_id'],'decision':self.decision,'reason':'Current input and complete body',
                        'references':self.refs} for b in context['bubbles']]}
    def structured(self,name,schema,prompt,context,**kwargs):
        self.calls.append(context)
        # The whole rendered request, so a test can pin what a chunk is actually shown.
        self.requests.append({'name':name,'schema':schema.model_json_schema(),'prompt':prompt,
                              'context':{**context},'options':kwargs})
        return schema.model_validate(self.body(context)),{'model':'deepseek-flash','reasoning':'high'}


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


# --- Stage 3 WP B3: chunked review, remainder re-review, remainder coverage -------------------

PARAGRAPH = 'A synthetic public paragraph about the fictional dataset. '


def long_reply(memory, mind, count, characters, *, refs=None, prefix='d'):
    """A reply the stage-2 code refused outright: past 64 bubbles or past 180,000 characters."""
    memory.ingest({'id':'long-input','kind':'owner-message','at':mind.clock(),'text':'Please explain all of it'})
    body = (PARAGRAPH * (characters // len(PARAGRAPH) + 1))[:characters]
    return {'entries':[{'draft_id':f'{prefix}{i}','reply_id':'long-input','text':f'Part {i}. '+body,
                        **({'references':refs} if refs else {})} for i in range(count)]}


def rows(mind, table):
    with mind.engine.db.connect() as conn:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def test_a_reply_past_the_old_character_limit_is_one_group_reviewed_in_chunks(system):
    """D4: 250,000 characters used to raise for ever; now the host reviews them as one group,
    chunk by chunk, and settles the reservations once at the end."""
    mind,memory,*_=system; req=long_reply(memory,mind,5,50000); provider=Review()
    result=ReplyReviews(memory.sharing).preflight(req,provider)
    assert [c['chunk']['draft_ids'] for c in provider.calls]==[['d0','d1'],['d2','d3'],['d4']]
    assert [c['chunk']['index'] for c in provider.calls]==[0,1,2]
    assert {c['chunk']['count'] for c in provider.calls}=={3}
    # Every chunk sees the whole reply's outline and the same union of references.
    for call in provider.calls:
        assert [b['draft_id'] for b in call['reply_outline']]==[e['draft_id'] for e in req['entries']]
        assert call['findings']==provider.calls[0]['findings']
        assert sum(len(b['text']) for b in call['bubbles'])<=CHUNK_CHARACTERS+len(req['entries'][0]['text'])
    assert result['state']=='ready'
    assert [c['draft_id'] for c in result['checked']]==[e['draft_id'] for e in req['entries']]
    assert [b['draft_id'] for b in result['semantic']['bubbles']]==[e['draft_id'] for e in req['entries']]
    assert len(result['receipt']['chunks'])==3
    # One merged row in today's shape; the per-chunk rows are the new table's.
    assert rows(mind,'mind_reply_reviews')==1 and rows(mind,'mind_reply_review_chunks')==3


def test_a_reply_over_sixty_four_bubbles_is_one_group_with_order_and_coverage_enforced(system):
    """D4: over 64 bubbles the group is chunked; a chunk that drops or reorders its bubbles is
    refused, so no partial coverage can ever reach the merged row."""
    mind,memory,*_=system; req=long_reply(memory,mind,70,40); api=ReplyReviews(memory.sharing)
    provider=Review(); result=api.preflight(req,provider)
    assert [len(c['bubbles']) for c in provider.calls]==[CHUNK_BUBBLES,70-CHUNK_BUBBLES]
    assert [c['draft_id'] for c in result['checked']]==[e['draft_id'] for e in req['entries']]
    assert len(result['semantic']['chunks'])==2 and result['state']=='ready'

    class Skipping(Review):
        def body(self,context):
            value=super().body(context)
            return {**value,'bubbles':value['bubbles'][1:]} if context['chunk']['index'] else value

    class Reordering(Review):
        def body(self,context):
            value=super().body(context)
            return {**value,'bubbles':list(reversed(value['bubbles']))} if context['chunk']['index'] else value

    other=long_reply(memory,mind,70,41,prefix='later-')
    key=digest([e['draft_id'] for e in other['entries']])
    for provider in (Skipping(),Reordering()):
        with pytest.raises(Conflict,match='omitted or reordered'):
            api.preflight(other,provider)
        assert rows(mind,'mind_reply_reviews')==1
        with mind.engine.db.connect() as conn:
            # Only the chunk that covered its bubbles in order was kept.
            assert [r[0] for r in conn.execute('SELECT chunk FROM mind_reply_review_chunks WHERE review_id=?',(key,))]==[0]


def test_each_chunk_carries_the_decisions_of_the_chunks_before_it(system):
    mind,memory,*_=system; req=long_reply(memory,mind,100,40); provider=Review()
    ReplyReviews(memory.sharing).preflight(req,provider)
    assert provider.calls[0]['earlier_decisions']==[]
    for index,call in enumerate(provider.calls[1:],start=1):
        assert [c['chunk'] for c in call['earlier_decisions']]==list(range(index))
        seen=[b['draft_id'] for c in call['earlier_decisions'] for b in c['bubbles']]
        assert seen==[e['draft_id'] for e in req['entries'][:len(seen)]]
        assert call['earlier_decisions'][0]['bubbles'][0]['decision']=='ordinary'


def test_a_failed_chunk_settles_nothing_and_finished_chunks_are_never_paid_for_twice(system):
    """A chunk failure must leave no half-settled reservation, and the crash between chunks
    resumes from what is already stored."""
    mind,memory,*_=system; unit=findings(system)[0]
    ref=[{'unit_id':unit['id'],'version':1,'mode':'new'}]
    req=long_reply(memory,mind,70,40,refs=ref); api=ReplyReviews(memory.sharing)

    class Crashing(Review):
        def structured(self,name,schema,prompt,context,**kwargs):
            if len(self.calls)==1:
                raise RuntimeError('deepseek-timeout')
            return super().structured(name,schema,prompt,context,**kwargs)

    crashing=Crashing(refs=ref)
    with pytest.raises(RuntimeError):
        api.preflight(req,crashing)
    assert len(crashing.calls)==1 and rows(mind,'mind_reply_review_chunks')==1
    assert rows(mind,'mind_reply_reviews')==0 and rows(mind,'mind_share_reservations')==0
    resumed=Review(refs=ref)
    result=ReplyReviews(memory.sharing).preflight(req,resumed)
    # Only the second chunk is bought again; the first is read back from its stored row.
    assert [c['chunk']['index'] for c in resumed.calls]==[1]
    assert result['state']=='ready' and len(result['checked'])==70
    assert rows(mind,'mind_share_reservations')==1
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT draft_id,state FROM mind_share_reservations').fetchone()['draft_id']=='d69'


def test_a_review_that_runs_out_of_time_reports_its_progress_instead_of_being_killed(system):
    """The host's own call gives up at 300 seconds. A review that cannot finish stops on a
    chunk boundary, so the next resume pays only for the chunks that are left."""
    mind,memory,*_=system; req=long_reply(memory,mind,70,40); api=ReplyReviews(memory.sharing)
    api.budget=-1; provider=Review()
    stopped=api.preflight(req,provider)
    assert stopped=={'state':'pending','reason':'reply-review-chunks-incomplete',
                     'review_id':digest([e['draft_id'] for e in req['entries']]),
                     'chunks':{'reviewed':1,'total':2}}
    assert len(provider.calls)==1 and rows(mind,'mind_reply_reviews')==0
    api.budget=150; resumed=Review()
    assert api.preflight(req,resumed)['state']=='ready'
    assert [c['chunk']['index'] for c in resumed.calls]==[1]


def test_a_reply_beyond_the_review_capacity_is_reported_not_raised(system, monkeypatch):
    mind,memory,*_=system; req=long_reply(memory,mind,70,40); provider=Review()
    monkeypatch.setattr('kin_mind.reply_review.MAX_CHUNKS',1)
    assert ReplyReviews(memory.sharing).preflight(req,provider)['reason']=='reply-exceeds-review-capacity'
    assert provider.calls==[]


def test_every_chunk_receipt_is_kept_and_unknown_usage_is_never_zero(system):
    merged=ReplyReviews.merged_receipt([{'model':'deepseek-flash','usage':{'output_tokens':7},'elapsed_ms':10},
                                        {'model':'deepseek-flash','usage':{'output_tokens':5},'elapsed_ms':20}])
    assert merged['usage']=={'output_tokens':12} and merged['usage_status']=='reported'
    assert merged['elapsed_ms']==30 and len(merged['chunks'])==2
    unknown=ReplyReviews.merged_receipt([{'model':'deepseek-flash','usage':{'output_tokens':7}},
                                         {'model':'deepseek-flash','usage':{},'outcome':'usage-missing'}])
    assert 'usage' not in unknown and unknown['usage_status']=='unknown'
    assert [r.get('usage') for r in unknown['chunks']]==[{'output_tokens':7},{}]


def sent_bubble(entry,state='accepted'):
    return {'id':'transport-'+entry['draft_id'],'draft_id':entry['draft_id'],'text':entry['text'],
            'state':state,'references':[],'message_id':'receipt-'+entry['draft_id'],'at':'2026-09-14T04:00:00+00:00'}


def test_a_frozen_group_whose_evidence_changed_re_reviews_only_its_unsent_remainder(system):
    """D3: reply_review used to answer `frozen-reply-evidence-changed` for ever. The sent
    bubbles stay exactly as they were sent; only the remainder is judged again."""
    mind,memory,*_=system; req=request(memory,mind); api=ReplyReviews(memory.sharing)
    first=api.preflight(req,Review())
    assert first['state']=='ready'
    # A bubble whose receipt is still unknown was handed to the transport too: it is history
    # for this review exactly like the accepted one, never text to judge again.
    outbox=[sent_bubble(req['entries'][0]),sent_bubble(req['entries'][1],state='unconfirmed')]
    memory.ingest({'id':'new-input','kind':'owner-message','at':mind.clock(),'text':'Another question'})
    frozen={**req,'frozen':True,'outbox':outbox}
    # Without a model the host is told to call again with one, instead of being blocked.
    assert api.preflight(frozen)=={'state':'pending','reason':'frozen-remainder-review-required',
                                   'review_id':first['review_id']}
    provider=Review(); released=api.preflight(frozen,provider)
    assert [b['draft_id'] for b in provider.calls[0]['bubbles']]==['d2']
    assert [b['draft_id'] for b in provider.calls[0]['sent_bubbles']]==['d0','d1']
    assert released['state']=='ready' and released['semantic']['sent']==['d0','d1']
    assert released['checked'][:2]==first['checked'][:2]
    assert [c['draft_id'] for c in released['checked']]==['d0','d1','d2']
    assert len(provider.calls)==1
    # Nothing moved since, so the next resume costs nothing at all.
    again=api.preflight(frozen,provider)
    assert again['reused'] and len(provider.calls)==1


def test_a_frozen_remainder_that_needs_rewriting_is_held_for_a_new_group(system):
    mind,memory,*_=system; req=request(memory,mind); api=ReplyReviews(memory.sharing)
    first=api.preflight(req,Review())
    memory.ingest({'id':'new-input','kind':'owner-message','at':mind.clock(),'text':'Another question'})
    frozen={**req,'frozen':True,'outbox':[sent_bubble(req['entries'][0])]}

    class Rewriting(Review):
        def body(self,context):
            value=super().body(context)
            value['bubbles'][0]['public_text']='A different remainder'
            return {**value,'action':'revise'}

    held=api.preflight(frozen,Rewriting(action='revise'))
    assert held['state']=='pending' and held['reason']=='frozen-remainder-needs-new-group'
    assert [c['text'] for c in held['checked']]==[e['text'] for e in req['entries']]
    assert held['checked'][0]==first['checked'][0]


def test_covers_remainder_settles_an_earlier_tail_and_an_uncovered_item_stays_owed(system):
    """The settlement carrier for WP B2: what the new group covers hands the old bubble's
    reservation over; what it does not cover keeps holding its content."""
    mind,memory,*_=system; unit=findings(system)[0]
    ref={'unit_id':unit['id'],'version':1,'mode':'new'}
    assert memory.sharing.preflight({'draft_id':'old-d1','text':unit['text'],'references':[ref]})['state']=='ready'
    req=request(memory,mind)
    for entry in req['entries']:
        entry['references']=[ref]
    api=ReplyReviews(memory.sharing)
    # The interrupted group still holds the finding, so without a settlement the new reply waits.
    assert api.preflight(req,Review(refs=[ref]))['reason']=='content-reserved-by-another-draft'
    remainder={'reply_id':'earlier-group','items':[{'id':'old-d1','text':unit['text'],'references':[ref]},
                                                   {'id':'old-d2','text':'The part nobody said again','references':[]}]}

    class Covering(Review):
        def body(self,context):
            assert context['interrupted_remainder']==[
                {'id':'old-d1','text':unit['text'],'text_complete':True,'references':[ref]},
                {'id':'old-d2','text':'The part nobody said again','text_complete':True,'references':[]}]
            return {**super().body(context),'covers_remainder':[
                {'item_id':'old-d1','covered_by':'d0','reason':'Said again in full'},
                {'item_id':'not-in-the-remainder','covered_by':'d0','reason':'Invented item'}]}

    provider=Covering(refs=[ref])
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_reply_reviews")
    settled=api.preflight({**req,'remainder':remainder},provider)
    assert provider.requests[0]['schema']==RemainderReview.model_json_schema()
    assert settled['state']=='ready'
    assert settled['covers_remainder']==[{'item_id':'old-d1','covered_by':'d0','reason':'Said again in full'}]
    assert settled['remainder_owed']==['old-d2']
    with mind.engine.db.connect() as conn:
        held=conn.execute('SELECT draft_id,state FROM mind_share_reservations').fetchone()
    # The interrupted bubble no longer holds it; the new group's last bubble does.
    assert (held['draft_id'],held['state'])==('d2','prepared')


def test_without_a_remainder_the_rendered_request_keeps_todays_bytes(system):
    """Ordinary chat pays nothing for stage 3: same tool, same schema, same prompt, same body."""
    mind,memory,*_=system; req=request(memory,mind); api=ReplyReviews(memory.sharing)
    chunked=Review(); api.preflight(req,chunked)
    memory.configure({'chunked_reply_review':False})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('DELETE FROM mind_reply_reviews')
    legacy=Review(); api.preflight(req,legacy)
    assert len(chunked.requests)==len(legacy.requests)==1
    assert dumps(chunked.requests[0])==dumps(legacy.requests[0])
    assert 'covers_remainder' not in ReplyReview.model_json_schema()['properties']
    assert {'reply_outline','chunk','earlier_decisions','interrupted_remainder','sent_bubbles'} \
        .isdisjoint(chunked.requests[0]['context'])


def test_the_switch_off_restores_the_stage_two_limits_and_the_permanent_frozen_block(system):
    mind,memory,*_=system; api=ReplyReviews(memory.sharing)
    bubbles=long_reply(memory,mind,70,40)
    characters=long_reply(memory,mind,5,50000)
    ready=api.preflight(request(memory,mind),Review())
    memory.ingest({'id':'new-input','kind':'owner-message','at':mind.clock(),'text':'Another question'})
    memory.configure({'chunked_reply_review':False})
    with pytest.raises(ValueError,match='public bubbles and stable draft IDs'):
        api.preflight(bubbles,Review())
    with pytest.raises(ValueError,match='unique and text bounded'):
        api.preflight(characters,Review())
    frozen={**request(memory,mind),'frozen':True,'outbox':[sent_bubble({'draft_id':'d0','text':'Here it is:'})]}
    assert api.preflight(frozen,Review())=={'state':'pending','reason':'frozen-reply-evidence-changed',
                                            'review_id':ready['review_id']}
    memory.configure({'chunked_reply_review':True})
    assert api.preflight(bubbles,Review())['state']=='ready'
    assert api.preflight(frozen,Review())['state']=='ready'
