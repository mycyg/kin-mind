import test from 'node:test';
import assert from 'node:assert/strict';
import {MindLoop} from '../../adapters/owner-host.mjs';
import {createContactBatch} from '../../adapters/contact-batch.mjs';
function fixture(overrides={}) {
 const events=[];let epoch='owner-1';let eligible=true;let busy=false;
 const loop=new MindLoop({call:async(action,req)=>{events.push([action,req]);if(action==='candidate')return{eligible:true};if(action==='claim')return{id:'stable-id',state:'drafting'};if(action==='check')return{eligible:true};return req;},eligibility:()=>({eligible}),ownerEpoch:()=>epoch,isBusy:()=>busy,draft:async()=> 'A sourced hello',send:async()=>({state:'accepted',messageId:'server-id'}),...overrides});
 // These tests exercise one contact tick; durable queue behavior is tested with SQLite.
 loop.review=async()=>{};
 return{loop,events,change:()=>{epoch='owner-2';},quiet:()=>{eligible=false;},busy:()=>{busy=true;}};
}
test('under four hours is allowed by threshold; accepted receipt settles',async()=>{const{loop,events}=fixture();await loop.tick();assert.equal(events.at(-1)[1].state,'accepted');assert.equal(events.filter(x=>x[0]==='claim').length,1);});
test('initial partial acceptance is preserved in the durable settlement',async()=>{const{loop,events}=fixture({send:async()=>({state:'accepted',messageId:'server-id',messageIds:['server-id'],partial:true,canceledBubbles:1})});await loop.tick();const settled=events.at(-1)[1];assert.equal(settled.state,'accepted');assert.equal(settled.partial,true);assert.equal(settled.canceled_bubbles,1);});
test('quiet hours and waiting owner never draft',async()=>{const{loop,events,quiet}=fixture();quiet();await loop.tick();assert.equal(events.length,0);});
test('new message invalidates a draft',async()=>{let f;f=fixture({draft:async()=>{f.change();return'outdated';},send:async()=>assert.fail('must not send')});await f.loop.tick();assert.equal(f.events.at(-1)[1].state,'canceled');});
test('missing message ID is unconfirmed',async()=>{const{loop,events}=fixture({send:async()=>({state:'accepted'})});await loop.tick();assert.equal(events.at(-1)[1].state,'unconfirmed');});
test('timeout never invents another send ID',async()=>{const{loop,events}=fixture({send:async()=>{throw Error('timeout');}});await loop.tick();assert.equal(events.at(-1)[1].state,'unconfirmed');assert.equal(events.filter(x=>x[0]==='claim').length,1);});
test('concurrent ticks share one draft',async()=>{let release;const gate=new Promise(r=>release=r);const{loop,events}=fixture({draft:async()=>{await gate;return'hello';}});const one=loop.tick();await new Promise(r=>setImmediate(r));await loop.tick();release();await one;assert.equal(events.filter(x=>x[0]==='claim').length,1);});

test('empty legacy draft requires evidence without sending',async()=>{const{loop,events}=fixture({draft:async()=>null,send:async()=>assert.fail('must not send')});await loop.tick();assert.equal(events.at(-1)[1].decision.condition,'new_evidence');});

test('temporary defer preserves a declared wake condition',async()=>{const{loop,events}=fixture({draft:async()=>({action:'wait',condition:'time',retry_after_seconds:1800,reason:'Revisit later'}),send:async()=>assert.fail('must not send')});await loop.tick();assert.equal(events.at(-1)[1].decision.retry_after_seconds,1800);assert.equal(events[0][0],'reconsider');});
test('invalid draft is a recoverable execution failure with a redacted stage',async()=>{const{loop,events}=fixture({draft:async()=>{throw Error('invalid JSON');},send:async()=>assert.fail('must not send')});await loop.tick();assert.equal(events.at(-1)[1].reason,'draft-failed');assert.deepEqual(events.at(-1)[1].failure,{category:'unknown',stage:'contact-draft-execution',code:'contact-draft-execution-failed',retry_condition:'backoff'});});
test('a structured format failure is distinct from execution failure',async()=>{const{loop,events}=fixture({draft:async()=>{throw Error('contact-draft-invalid-result');}});await loop.tick();assert.deepEqual(events.at(-1)[1].failure,{category:'model-output',stage:'contact-draft-format',code:'contact-draft-invalid-result',retry_condition:'deepseek-decision'});});
test('an empty send decision is recorded as empty model output',async()=>{const{loop,events}=fixture({draft:async()=>({action:'send',text:'  '}),send:async()=>assert.fail('must not send')});await loop.tick();assert.equal(events.at(-1)[1].failure.stage,'contact-draft-output');assert.equal(events.at(-1)[1].failure.code,'contact-draft-empty-output');});
test('a pre-model source change does not count as a draft failure',async()=>{const error=Error('private detail');error.failure={category:'source-changed',stage:'contact-draft-source',code:'contact-context-source-changed',retry_condition:'source-change',model_invoked:false};const{loop,events}=fixture({draft:async()=>{throw error;}});await loop.tick();assert.equal(events.at(-1)[1].reason,'draft-source-changed');assert.equal(events.at(-1)[1].failure.model_invoked,false);});
test('draft failures retain nested accounting receipts but never provider prose',async()=>{const error=Error('private provider body');error.failure={category:'model-output',stage:'contact-draft-format',code:'deepseek-incomplete-or-unverified',retry_condition:'deepseek-decision',model_invoked:true,model_receipt:{provider:'deepseek',outcome:'incomplete',prompt:'secret',chunks:[{request_id:'chunk-1',usage:{input_tokens:2},body:'secret'}],schema_repair:{attempts:1,rejected_call:{request_id:'repair-1',outcome:'rejected',prompt:'secret'}}}};const{loop,events}=fixture({draft:async()=>{throw error;}});await loop.tick();const receipt=events.at(-1)[1].failure.model_receipt;assert.equal(receipt.chunks[0].request_id,'chunk-1');assert.equal(receipt.schema_repair.rejected_call.request_id,'repair-1');assert.ok(!JSON.stringify(events.at(-1)[1]).includes('secret'));assert.ok(!JSON.stringify(events.at(-1)[1]).includes('private provider body'));});
test('new owner message also invalidates a discard decision',async()=>{let f;f=fixture({draft:async()=>{f.change();return{action:'abandon',reason:'outdated'};}});await f.loop.tick();assert.equal(f.events.at(-1)[1].decision,undefined);});
test('new owner input during a failed draft does not defer the old wish',async()=>{let f;f=fixture({draft:async()=>{f.change();throw Error('canceled');}});await f.loop.tick();assert.equal(f.events.at(-1)[1].reason,'Draft or delivery conditions changed');});
test('routine blocking reasons are observable',async()=>{let status;const{loop}=fixture({call:async action=>action==='candidate'?{eligible:false,reason:'no-actionable-desire'}:{},recordStatus:s=>status=s});await loop.tick();assert.equal(status.contact.reason,'no-actionable-desire');assert.ok(status.contact.checkedAt);});

test('the explicit contact-draft host context remains active for the complete draft promise',async()=>{
 let active,release;const gate=new Promise(resolve=>release=resolve),seen=[];
 const{loop}=fixture({withHostContext:async(context,run)=>{active=context;seen.push(['open',structuredClone(context)]);try{return await run();}finally{seen.push(['close',active.operation_id]);active=null;}},
   draft:async()=>{assert.equal(active.kind,'contact-draft');assert.equal(active.lane,'background');await gate;return'hello';}});
 const pending=loop.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(active.operation_id,'stable-id');release();await pending;
 assert.deepEqual(seen.map(event=>event[0]),['open','close']);assert.equal(active,null);
});

test('a persisted wholly-unsent needs-review batch is released without inventing an abandon decision',async()=>{
 const events=[];const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=> 'owner-1',isBusy:()=>false,draft:async()=>assert.fail('must not draft'),send:async()=>assert.fail('must not send'),
   resume:async()=>({state:'needs-review',safeToRelease:true,acceptedBubbles:0,failure:{category:'contract',stage:'contact-review-contract',code:'contact-review-response-mismatch',retry_condition:'repair-input'}}),
   call:async(action,request)=>{events.push([action,request]);if(action==='candidate')return{eligible:false,reason:'attempt-in-progress',state:'pending',attempt_id:'old',owner_epoch:'owner-1'};return request;}});
 loop.review=async()=>{};await loop.tick();const settled=events.at(-1)[1];
 assert.equal(settled.state,'canceled');assert.equal(settled.aborted_before_send,true);assert.equal(settled.reason,'contact-review-failed');assert.equal(settled.decision,undefined);assert.equal(settled.failure.category,'contract');
});

test('needs-review without proof of a wholly unsent batch remains reconciliation-only',async()=>{
 const events=[];const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=> 'owner-1',isBusy:()=>false,
   resume:async()=>({state:'needs-review',acceptedBubbles:0,failure:{category:'unknown',stage:'contact-review-model',code:'unclear',retry_condition:'backoff'}}),
   call:async(action,request)=>{events.push([action,request]);if(action==='candidate')return{eligible:false,reason:'attempt-in-progress',state:'pending',attempt_id:'old',owner_epoch:'owner-1'};return request;}});
 loop.review=async()=>{};await loop.tick();assert.equal(events.at(-1)[1].state,'unconfirmed');assert.equal(events.at(-1)[1].failure.retry_condition,'reconcile');
});

test('a superseded wholly-unsent attempt releases for DS review instead of host abandonment',async()=>{
 const events=[];const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=> 'owner-2',isBusy:()=>false,
   resume:async()=>({state:'canceled',safeToRelease:true,acceptedBubbles:0}),
   call:async(action,request)=>{events.push([action,request]);if(action==='candidate')return{eligible:false,reason:'attempt-in-progress',state:'pending',attempt_id:'old',owner_epoch:'owner-1'};return request;}});
 loop.review=async()=>{};await loop.tick();const settled=events.at(-1)[1];
 assert.equal(settled.state,'canceled');assert.equal(settled.aborted_before_send,true);assert.equal(settled.reason,'contact-source-changed');
 assert.equal(settled.decision,undefined);assert.equal(settled.failure.category,'source-changed');
});

test('only the semantic decision carried by a safe batch can retire a wish',async()=>{
 const semantic={action:'abandon',reason:'The group repeats something already shared'};
 const{loop,events}=fixture({send:async()=>({state:'canceled',safeToRelease:true,acceptedBubbles:0,decision:semantic})});
 await loop.tick();const settled=events.at(-1)[1];assert.equal(settled.state,'canceled');assert.deepEqual(settled.decision,semantic);
 assert.equal(settled.reason,undefined);
});

test('a malformed review verdict remains pre-send and releases the owner slot for DS review',async()=>{
 const journal=new Map();let transports=0;
 const send=createContactBatch({read:id=>structuredClone(journal.get(id)),write:(id,value)=>journal.set(id,structuredClone(value)),
   preflight:async()=>null,send:async()=>{transports++;}});
 const{loop,events}=fixture({send});await loop.tick();const settled=events.at(-1)[1];
 assert.equal(settled.state,'canceled');assert.equal(settled.aborted_before_send,true);
 assert.equal(settled.reason,'contact-review-failed');assert.equal(settled.failure.code,'contact-review-verdict-invalid');
 assert.equal(settled.decision,undefined);assert.equal(transports,0);
});

test('repeated proven pre-submit failures release for DS review while unknown delivery never replays',async()=>{
 const makeLoop=({receipt,id})=>{
   const journal=new Map(),sent=[],settlements=[];let state=null;
   const batch=createContactBatch({read:key=>structuredClone(journal.get(key)),write:(key,value)=>journal.set(key,structuredClone(value)),
     maxFailures:2,receipt,send:async request=>{sent.push(request.id);throw Error('synthetic interruption');}});
   const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=> 'owner-1',isBusy:()=>false,draft:async()=> 'One',send:batch,
     resume:()=>batch({id}),call:async(action,request)=>{
       if(action==='reconsider')return{};
       if(action==='candidate')return ['pending','unconfirmed'].includes(state)
         ?{eligible:false,reason:'attempt-in-progress',state,attempt_id:id,owner_epoch:'owner-1'}:{eligible:true};
       if(action==='claim'){state='drafting';return{id,state:'drafting'};}
       if(action==='check')return{eligible:true};
       if(action==='settle'){state=request.state;settlements.push(request);return request;}
       return{};
     }});
   loop.review=async()=>{};return{loop,sent,settlements,getState:()=>state};
 };
 const proven=makeLoop({id:'proven-never-started',receipt:()=>({state:'not-submitted',submissionStarted:false})});
 for(let pass=0;pass<8&&proven.getState()!=='canceled';pass++)await proven.loop.tick();
 const released=proven.settlements.at(-1);assert.equal(released.state,'canceled');assert.equal(released.aborted_before_send,true);
 assert.equal(released.reason,'contact-review-failed');assert.equal(released.failure.code,'transport-never-started-exhausted');
 assert.equal(released.decision,undefined);assert.equal(new Set(proven.sent).size,1);assert.equal(proven.sent.length,3);

 const unknown=makeLoop({id:'unknown-delivery',receipt:()=>null});await unknown.loop.tick();await unknown.loop.tick();await unknown.loop.tick();
 assert.equal(unknown.getState(),'unconfirmed');assert.equal(unknown.sent.length,1);
 assert.ok(unknown.settlements.every(value=>value.aborted_before_send!==true));
});

for(const boundary of ['owner-input','busy','quiet','closed','write-error']) {
 test(`a ${boundary} after durable pending never becomes an unknown send`,async()=>{
  let state,epoch='owner-1',busy=false,eligible=true,first=true,sends=0,claims=0;
  const loop=new MindLoop({eligibility:()=>({eligible}),ownerEpoch:()=>epoch,isBusy:()=>busy,
   draft:async()=> 'A current thought',send:async()=>{sends++;return{state:'accepted',messageId:'receipt'};},
   resume:async()=>assert.fail('a proven unsent attempt must not require transport reconciliation'),
   call:async(action,request)=>{
    if(action==='candidate')return ['pending','unconfirmed'].includes(state)
     ?{eligible:false,reason:'attempt-in-progress',state,attempt_id:'attempt-'+claims}:{eligible:true};
    if(action==='claim'){claims++;state='drafting';return{id:'attempt-'+claims,state};}
    if(action==='check')return{eligible:true};
    if(action==='settle'){
     if(state==='pending'&&request.state==='canceled')assert.equal(request.aborted_before_send,true);
     state=request.state;
     if(state==='pending'&&first){first=false;
      if(boundary==='owner-input')epoch='owner-2';
      if(boundary==='busy')busy=true;
      if(boundary==='quiet')eligible=false;
      if(boundary==='closed')loop.closed=true;
      if(boundary==='write-error')throw Error('write completed but response failed');
     }
     return request;
    }
    return{};
   }});
  loop.review=async()=>{};
  const result=await loop.tick();
  assert.equal(result.state,'canceled');assert.equal(result.aborted_before_send,true);assert.equal(sends,0);
  if(boundary==='owner-input')assert.equal(result.reason,'contact-source-changed');
  busy=false;eligible=true;loop.closed=false;
  assert.equal((await loop.tick()).state,'accepted');assert.equal(claims,2);assert.equal(sends,1);
 });
}
