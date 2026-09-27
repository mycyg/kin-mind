import test from 'node:test';
import assert from 'node:assert/strict';
import {MindLoop,interactionView,stateContext,INTERACTION_LIMITS} from '../../adapters/owner-host.mjs';
import {createContactBatch} from '../../adapters/contact-batch.mjs';
import {parseContactDraft} from '../../adapters/contact-draft.mjs';
function fixture(overrides={}) {
 const events=[];let epoch='owner-1';let eligible=true;let busy=false;
 const loop=new MindLoop({call:async(action,req)=>{events.push([action,req]);if(action==='candidate')return{eligible:true};if(action==='claim')return{id:'stable-id',state:'drafting'};if(action==='check')return{eligible:true};return req;},eligibility:()=>({eligible}),ownerEpoch:()=>epoch,isBusy:()=>busy,draft:async()=>({action:'send',text:'A sourced hello'}),send:async()=>({state:'accepted',messageId:'server-id'}),...overrides});
 // These tests exercise one contact tick; durable queue behavior is tested with SQLite.
 loop.review=async()=>{};
 return{loop,events,change:()=>{epoch='owner-2';},quiet:()=>{eligible=false;},busy:()=>{busy=true;}};
}
test('an eligible wish is drafted once and an accepted receipt settles',async()=>{const{loop,events}=fixture();await loop.tick();assert.equal(events.at(-1)[1].state,'accepted');assert.equal(events.filter(x=>x[0]==='claim').length,1);});
test('initial partial acceptance is preserved in the durable settlement',async()=>{const{loop,events}=fixture({send:async()=>({state:'accepted',messageId:'server-id',messageIds:['server-id'],partial:true,canceledBubbles:1})});await loop.tick();const settled=events.at(-1)[1];assert.equal(settled.state,'accepted');assert.equal(settled.partial,true);assert.equal(settled.canceled_bubbles,1);});
test('an ineligible moment never drafts',async()=>{const{loop,events,quiet}=fixture();quiet();await loop.tick();assert.equal(events.length,0);});
test('new message invalidates a draft',async()=>{let f;f=fixture({draft:async()=>{f.change();return{action:'send',text:'outdated'};},send:async()=>assert.fail('must not send')});await f.loop.tick();assert.equal(f.events.at(-1)[1].state,'canceled');});
test('a send the owner\'s contact rules skip at the last moment went nowhere: canceled as never sent, never left unconfirmed to be checked for a batch that never was',async()=>{
 const{loop,events}=fixture({send:async()=>({state:'skipped',eligible:false,reason:'waiting-for-reply',channel:'feishu'})});
 await loop.tick();
 const [action,settled]=events.at(-1);
 assert.deepEqual([action,settled.attempt_id,settled.state,settled.aborted_before_send,settled.reason],['settle','stable-id','canceled',true,'Delivery conditions changed before sending']);
 assert.equal(events.some(([name,request])=>name==='settle'&&request.state==='unconfirmed'),false);
 // The owner's context moved on meanwhile: said as such.
 let f;f=fixture({send:async()=>{f.change();return{state:'skipped',eligible:false,reason:'waiting-for-reply'};}});
 await f.loop.tick();
 assert.deepEqual([f.events.at(-1)[1].state,f.events.at(-1)[1].aborted_before_send,f.events.at(-1)[1].reason],['canceled',true,'contact-source-changed']);
 // Something that says skipped and names a message is no skip: it is reconciled as any receipt without proof.
 const named=fixture({send:async()=>({state:'skipped',messageId:'server-id'})});
 await named.loop.tick();
 assert.equal(named.events.at(-1)[1].state,'unconfirmed');
});
test('missing message ID is unconfirmed',async()=>{const{loop,events}=fixture({send:async()=>({state:'accepted'})});await loop.tick();assert.equal(events.at(-1)[1].state,'unconfirmed');});
test('timeout never invents another send ID',async()=>{const{loop,events}=fixture({send:async()=>{throw Error('timeout');}});await loop.tick();assert.equal(events.at(-1)[1].state,'unconfirmed');assert.equal(events.filter(x=>x[0]==='claim').length,1);});
test('concurrent ticks share one draft',async()=>{let release;const gate=new Promise(r=>release=r);const{loop,events}=fixture({draft:async()=>{await gate;return{action:'send',text:'hello'};}});const one=loop.tick();await new Promise(r=>setImmediate(r));await loop.tick();release();await one;assert.equal(events.filter(x=>x[0]==='claim').length,1);});

test('a draft without a decision is a format failure, never a host-made wait (AD2-17)',async()=>{const{loop,events}=fixture({draft:async()=>null,send:async()=>assert.fail('must not send')});await loop.tick();const settled=events.at(-1)[1];assert.equal(settled.state,'canceled');assert.equal(settled.decision,undefined);assert.equal(settled.failure.category,'model-output');});

test('temporary defer preserves a declared wake condition',async()=>{const{loop,events}=fixture({draft:async()=>({action:'wait',condition:'time',retry_after_seconds:1800,reason:'Revisit later'}),send:async()=>assert.fail('must not send')});await loop.tick();assert.equal(events.at(-1)[1].decision.retry_after_seconds,1800);assert.equal(events[0][0],'reconsider');});
test('invalid draft is a recoverable execution failure with a redacted stage',async()=>{const{loop,events}=fixture({draft:async()=>{throw Error('invalid JSON');},send:async()=>assert.fail('must not send')});await loop.tick();assert.equal(events.at(-1)[1].reason,'draft-failed');assert.deepEqual(events.at(-1)[1].failure,{category:'unknown',stage:'contact-draft-execution',code:'contact-draft-execution-failed',retry_condition:'backoff'});});
test('a structured format failure is distinct from execution failure',async()=>{const{loop,events}=fixture({draft:async()=>{throw Error('contact-draft-invalid-result');}});await loop.tick();assert.deepEqual(events.at(-1)[1].failure,{category:'model-output',stage:'contact-draft-format',code:'contact-draft-invalid-result',retry_condition:'deepseek-decision'});});
test('an empty send decision is recorded as empty model output',async()=>{const{loop,events}=fixture({draft:async()=>({action:'send',text:'  '}),send:async()=>assert.fail('must not send')});await loop.tick();assert.equal(events.at(-1)[1].failure.stage,'contact-draft-output');assert.equal(events.at(-1)[1].failure.code,'contact-draft-empty-output');});
test('a pre-model source change does not count as a draft failure',async()=>{const error=Error('private detail');error.failure={category:'source-changed',stage:'contact-draft-source',code:'contact-context-source-changed',retry_condition:'source-change',model_invoked:false};const{loop,events}=fixture({draft:async()=>{throw error;}});await loop.tick();assert.equal(events.at(-1)[1].reason,'draft-source-changed');assert.equal(events.at(-1)[1].failure.model_invoked,false);});
test('a draft that never started only waits; one that started or cannot be placed is a drafting failure (CR2-INT-06)',async()=>{
 const settleFor=async failure=>{const error=Error('private detail');error.failure=failure;const{loop,events}=fixture({draft:async()=>{throw error;},send:async()=>assert.fail('must not send')});await loop.tick();return events.at(-1)[1];};
 // WS4 (h:abd70aa): a draft fork the ACP says never started ran no model.
 const unmade=await settleFor({category:'model-unavailable',stage:'contact-draft-model',code:'fork-draft-failed',retry_condition:'backoff',model_invoked:false});
 assert.deepEqual([unmade.state,unmade.reason,unmade.failure.model_invoked],['canceled','draft-not-started',false]);
 // No mind worker could run, or a release freeze refused the fork before it began (CR-MIND-01).
 assert.equal((await settleFor({category:'model-unavailable',stage:'mind-worker',code:'mind-worker-unavailable',retry_condition:'backoff',model_invoked:false})).reason,'draft-not-started');
 assert.equal((await settleFor({category:'source-changed',stage:'contact-draft-source',code:'dispatch-frozen',retry_condition:'source-change',model_invoked:false})).reason,'draft-not-started');
 // A fork that started, or one nobody can place (unknown: null, dropped), still counts.
 assert.equal((await settleFor({category:'model-unavailable',stage:'contact-draft-model',code:'fork-draft-failed',retry_condition:'backoff',model_invoked:true})).reason,'draft-failed');
 const unknown=await settleFor({category:'model-unavailable',stage:'contact-draft-model',code:'fork-draft-timeout',retry_condition:'backoff',model_invoked:null});
 assert.deepEqual([unknown.reason,'model_invoked' in unknown.failure],['draft-failed',false]);
 // A source that really moved before the model is still that, and a model output fault is a failure.
 assert.equal((await settleFor({category:'source-changed',stage:'contact-draft-source',code:'contact-runtime-mismatch',retry_condition:'source-change',model_invoked:false})).reason,'draft-source-changed');
 assert.equal((await settleFor({category:'model-output',stage:'contact-draft-output',code:'contact-draft-empty-output',retry_condition:'deepseek-decision',model_invoked:false})).reason,'draft-failed');
});
test('the rules between two contacts hold a new contact, never the rest of one under way (CR2-INT-01)',async()=>{
 let rule='waiting-for-reply',inProgress=true;const asked=[],events=[],resumed=[];
 const eligibility=within=>{asked.push(within??null);return within?.started||rule===null?{eligible:true}:{eligible:false,reason:rule};};
 const loop=new MindLoop({call:async(action,req)=>{events.push(action);
   if(action==='candidate')return inProgress?{eligible:false,reason:'attempt-in-progress',state:'pending',attempt_id:'a1'}:{eligible:true};
   if(action==='claim')return {id:'a2',state:'drafting'};if(action==='check')return {eligible:true};return req;},
  eligibility,ownerEpoch:()=>'e1',isBusy:()=>false,draft:async()=>({action:'send',text:'下一次联系'}),
  send:async request=>{asked.length=0;await request.guard({contact:'a2',started:false});await request.guard({contact:'a2',started:true});return {state:'accepted',messageId:'m2'};},
  resume:async candidate=>{resumed.push(candidate.attempt_id);return {state:'accepted',messageId:'m1'};}});
 loop.review=async()=>{};
 // Waiting for a reply, or inside the gap: the contact already under way is taken up and finished...
 for(const reason of ['waiting-for-reply','recent-conversation']) {
  rule=reason;events.length=0;
  await loop.tick();
  assert.equal(resumed.at(-1),'a1',reason);
  assert.equal(events.includes('reconsider'),false,'no wish is woken while a new contact must wait');
 }
 // ...and with nothing under way, the next contact waits and nothing is claimed.
 inProgress=false;events.length=0;
 const waiting=await loop.tick();
 assert.deepEqual([waiting.state,waiting.reason],['waiting','recent-conversation']);
 assert.equal(events.includes('claim'),false);
 // The switch and the quiet hours hold everything, the rest of a contact included.
 rule='quiet-hours';inProgress=true;events.length=0;const before=resumed.length;
 assert.equal((await loop.tick()).reason,'quiet-hours');
 assert.deepEqual([events,resumed.length],[[],before]);
 // A new contact's send guard asks the rules about the contact itself.
 rule=null;inProgress=false;
 await loop.tick();
 assert.deepEqual(asked,[{contact:'a2',started:false},{contact:'a2',started:true}]);
});
test('draft failures retain nested accounting receipts but never provider prose',async()=>{const error=Error('private provider body');error.failure={category:'model-output',stage:'contact-draft-format',code:'deepseek-incomplete-or-unverified',retry_condition:'deepseek-decision',model_invoked:true,model_receipt:{provider:'deepseek',outcome:'incomplete',prompt:'secret',chunks:[{request_id:'chunk-1',usage:{input_tokens:2},body:'secret'}],schema_repair:{attempts:1,rejected_call:{request_id:'repair-1',outcome:'rejected',prompt:'secret'}}}};const{loop,events}=fixture({draft:async()=>{throw error;}});await loop.tick();const receipt=events.at(-1)[1].failure.model_receipt;assert.equal(receipt.chunks[0].request_id,'chunk-1');assert.equal(receipt.schema_repair.rejected_call.request_id,'repair-1');assert.ok(!JSON.stringify(events.at(-1)[1]).includes('secret'));assert.ok(!JSON.stringify(events.at(-1)[1]).includes('private provider body'));});
test('new owner message also invalidates a discard decision',async()=>{let f;f=fixture({draft:async()=>{f.change();return{action:'abandon',reason:'outdated'};}});await f.loop.tick();assert.equal(f.events.at(-1)[1].decision,undefined);});
test('new owner input during a failed draft does not defer the old wish',async()=>{let f;f=fixture({draft:async()=>{f.change();throw Error('canceled');}});await f.loop.tick();assert.equal(f.events.at(-1)[1].reason,'Draft or delivery conditions changed');});
test('routine blocking reasons are observable',async()=>{let status;const{loop}=fixture({call:async action=>action==='candidate'?{eligible:false,reason:'no-actionable-desire'}:{},recordStatus:s=>status=s});await loop.tick();assert.equal(status.contact.reason,'no-actionable-desire');assert.ok(status.contact.checkedAt);});

test('the explicit contact-draft host context remains active for the complete draft promise',async()=>{
 let active,release;const gate=new Promise(resolve=>release=resolve),seen=[];
 const{loop}=fixture({withHostContext:async(context,run)=>{active=context;seen.push(['open',structuredClone(context)]);try{return await run();}finally{seen.push(['close',active.operation_id]);active=null;}},
   draft:async()=>{assert.equal(active.kind,'contact-draft');assert.equal(active.lane,'background');await gate;return{action:'send',text:'hello'};}});
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

test('no reviewer word retires a wish: a safe canceled group goes back to Kin (WS8 docs pass, AD2-16)',async()=>{
 // A receipt that still carried an old whole-group verdict is read like any other canceled group.
 const verdict={action:'abandon',reason:'The group repeats something already shared'};
 const{loop,events}=fixture({send:async()=>({state:'canceled',safeToRelease:true,acceptedBubbles:0,decision:verdict})});
 await loop.tick();const settled=events.at(-1)[1];assert.equal(settled.state,'canceled');assert.equal(settled.decision,undefined);
 assert.equal(settled.reason,'contact-review-failed');assert.equal(settled.failure.code,'contact-canceled-without-semantic-decision');
});

test('the group check refuses nothing and holds nothing: an old refusal verdict or a file not ready goes back to Kin at once (WS8 docs pass)',async()=>{
 const run=async({preflight,verifyFile,files=[]})=>{
   const journal=new Map();let transports=0;
   const send=createContactBatch({read:id=>structuredClone(journal.get(id)),write:(id,value)=>journal.set(id,structuredClone(value)),
     preflight,verifyFile,send:async()=>{transports++;return {state:'accepted',messageId:'m'};}});
   return {result:await send({id:'group-9',text:'Hello',files,channel:'feishu'}),journal,transports:()=>transports};
 };
 for(const state of ['duplicate','silent','merged']) {
   const {result,journal,transports}=await run({preflight:async()=>({state,reason:'synthetic'})});
   assert.equal(result.state,'needs-review',state);assert.equal(result.safeToRelease,true);assert.equal(result.decision,undefined);
   assert.equal(result.failure.code,'contact-review-verdict-invalid');assert.equal(transports(),0);
   assert.equal(journal.get('group-9').items.every(item=>item.state==='unsent'),true,'nothing is canceled on a verdict');
 }
 const file={path:'/synthetic/artifact.txt',bytes:3,sha256:'a'.repeat(64)};
 const {result,journal,transports}=await run({preflight:async()=>assert.fail('the group is not checked before its files'),
   verifyFile:async()=>({state:'pending',reason:'still-writing'}),files:[file]});
 assert.equal(result.state,'needs-review');assert.equal(result.failure.category,'source-changed');assert.equal(result.failure.code,'still-writing');
 assert.equal(transports(),0);
 const stored=journal.get('group-9');assert.equal(stored.reviewFailures,undefined);assert.equal(stored.reviewNotBefore,undefined);
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
   const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=> 'owner-1',isBusy:()=>false,draft:async()=>({action:'send',text:'One'}),send:batch,
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
   draft:async()=>({action:'send',text:'A current thought'}),send:async()=>{sends++;return{state:'accepted',messageId:'receipt'};},
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
test('a new owner message does not stop background exploration (N7); closing does, and says it is a shutdown (OPS-04)',async()=>{
 const stops=[];const{loop}=fixture({stopExploration:reason=>{stops.push(reason);}});
 await loop.ingest({id:'owner-hi',text:'hi'});assert.deepEqual(stops,[]);
 loop.close();assert.deepEqual(stops,['host-shutdown']);
});
test('a contact wait of up to three days is kept; one past either end is taken to that end (N9)',()=>{
 const wait=seconds=>parseContactDraft(JSON.stringify({action:'wait',condition:'time',retry_after_seconds:seconds,reason:'Later'})).retry_after_seconds;
 assert.deepEqual([wait(60),wait(172800),wait(432000)],[300,172800,259200]);
 assert.throws(()=>parseContactDraft(JSON.stringify({text:'An object without an action'})),/contact-draft-invalid-result/);
 // A strict output schema always carries desire_ids; empty means every offered wish.
 assert.equal(parseContactDraft(JSON.stringify({action:'send',bubbles:[{text:'Hi',references:[]}],desire_ids:[],reason:null,condition:null,retry_after_seconds:null})).desire_ids,undefined);
 assert.deepEqual(parseContactDraft(JSON.stringify({action:'send',bubbles:['Hi'],desire_ids:['d-1']})).desire_ids,['d-1']);
});
test('Kin names the wishes her draft is for; check and pending carry them with the text (N11)',async()=>{
 const{loop,events}=fixture({draft:async()=>({action:'send',text:'The tea was lovely',desire_ids:['d-tea']})});
 await loop.tick();
 const check=events.find(([action])=>action==='check')[1];
 assert.deepEqual(check.desire_ids,['d-tea']);assert.equal(check.text,'The tea was lovely');
 const pending=events.find(([action,request])=>action==='settle'&&request.state==='pending')[1];
 assert.deepEqual(pending.desire_ids,['d-tea']);assert.equal(pending.text,'The tea was lovely');
});
test('a draft that names no wish acts on the wishes handed to Kin that round, never on one she was not shown (CR-MIND-04)',async()=>{
 const handed=fixture({draft:async()=>({action:'send',text:'Both of these',handed_ids:['d-a','d-b']})});
 await handed.loop.tick();
 const check=handed.events.find(([action])=>action==='check')[1],pending=handed.events.find(([action,request])=>action==='settle'&&request.state==='pending')[1];
 assert.deepEqual(check.desire_ids,['d-a','d-b']);assert.deepEqual(pending.desire_ids,['d-a','d-b']);
 assert.ok(handed.events.every(([,request])=>!('handed_ids' in (request??{}))),'the host list is not part of her decision');
 // Her own choice among them still wins, and a wait settles the handed set with its decision.
 const named=fixture({draft:async()=>({action:'send',text:'Just this',desire_ids:['d-b'],handed_ids:['d-a','d-b']})});
 await named.loop.tick();
 assert.deepEqual(named.events.find(([action])=>action==='check')[1].desire_ids,['d-b']);
 const waited=fixture({draft:async()=>({action:'wait',condition:'time',retry_after_seconds:600,reason:'Later',handed_ids:['d-a']}),send:async()=>assert.fail('must not send')});
 await waited.loop.tick();
 const settled=waited.events.at(-1)[1];
 assert.deepEqual(settled.desire_ids,['d-a']);assert.equal(settled.decision.handed_ids,undefined);
});
test('each settlement of a draft carries what it was shown and what its fork read, never as her decision (CL6D-MM-01)',async()=>{
 const shown=['mem_'+'a'.repeat(32)],receipt={tool_calls:[{name:'memorypalace.read',ok:true,ids:[{id:'mem_'+'b'.repeat(32),revision:1}]}],truncated:true};
 const reads=request=>[request.shown_ids,request.draft_receipt];
 // A wait: the settlement that writes her reason carries both, and her decision neither.
 const waited=fixture({draft:async()=>({action:'wait',condition:'time',retry_after_seconds:600,reason:'Later',handed_ids:['d-a'],shown_ids:shown,receipt}),
   send:async()=>assert.fail('must not send')});
 await waited.loop.tick();
 const decided=waited.events.at(-1)[1];
 assert.equal(decided.reason,'draft-decision');assert.deepEqual(reads(decided),[shown,receipt]);
 assert.equal('shown_ids' in decided.decision||'receipt' in decided.decision,false,'the host\'s record is not part of her decision');
 // A send: the pending that holds her text carries both; the settlements after the send need nothing more.
 const sent=fixture({draft:async()=>({action:'send',text:'Hi',shown_ids:shown,receipt})});
 await sent.loop.tick();
 const settlements=sent.events.filter(([action])=>action==='settle').map(([,request])=>request);
 assert.deepEqual(settlements.map(request=>request.state),['pending','accepted']);
 assert.deepEqual(reads(settlements[0]),[shown,receipt]);assert.equal(settlements[0].receipt,undefined);
 // Every other settlement of the same draft carries them too.
 for(const [check,draft,reason] of [[{eligible:false,reason:'repeats-unconfirmed-send'},{action:'send',text:'Again'},'repeats-unconfirmed-send'],
   [{eligible:false,reason:'desire-not-offered'},{action:'send',text:'Hi',desire_ids:['d-x']},'draft-failed'],
   [{eligible:false,reason:'candidate-changed'},{action:'send',text:'Hi'},'Draft or delivery conditions changed'],
   [{eligible:true},{action:'send',text:'  '},'draft-failed']]) {
  const events=[];
  const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=>'owner-1',isBusy:()=>false,send:async()=>assert.fail('must not send'),
   draft:async()=>({...draft,shown_ids:shown,receipt}),call:async(action,request)=>{events.push([action,request]);
    if(action==='candidate')return {eligible:true};if(action==='claim')return {id:'a1',state:'drafting'};if(action==='check')return check;return request;}});
  loop.review=async()=>{};await loop.tick();
  const settled=events.at(-1)[1];
  assert.equal(settled.reason,reason);assert.deepEqual(reads(settled),[shown,receipt],reason);
 }
});
test('a pending the store canceled, because something the draft had was deleted, sends nothing (CL6D-MM-01)',async()=>{
 let sends=0;const events=[];
 const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=>'owner-1',isBusy:()=>false,
  draft:async()=>({action:'send',text:'Hi',shown_ids:[],receipt:{tool_calls:[]}}),send:async()=>{sends++;return {state:'accepted',messageId:'m-1'};},
  call:async(action,request)=>{events.push([action,request]);
   if(action==='candidate')return {eligible:true};if(action==='claim')return {id:'a1',state:'drafting'};if(action==='check')return {eligible:true};
   if(action==='settle'&&request.state==='pending')return {id:'a1',state:'canceled',reason:'draft-sources-deleted'};return request;}});
 loop.review=async()=>{};
 const result=await loop.tick();
 assert.equal(sends,0);assert.deepEqual([result.state,result.reason],['canceled','draft-sources-deleted']);
 assert.deepEqual(events.filter(([action])=>action==='settle').map(([,request])=>request.state),['pending'],'nothing more to settle');
});
test('what a copied field was written from is the store\'s, not words rendered for Kin (CL6D-MM-01)',()=>{
 const ids=['mem_'+'c'.repeat(32)];
 const view=interactionView({dimensions:{},desires:[{id:'w1',kind:'contact',status:'waiting',topic:'t',content:'c',expires_at:'2999-01-01T00:00:00Z',
   contact_wait:{condition:'owner_reply',reason:'等她回来',reason_evidence_ids:ids}}],
  contact_unconfirmed:[{attempt_id:'a1',desire_ids:['w1'],excerpt:'晚安',since:'2026-09-25T00:00:00Z',excerpt_evidence_ids:ids}]});
 assert.deepEqual(view.desires[0].contact_wait,{condition:'owner_reply',reason:'等她回来'});
 assert.deepEqual(view.contact_unconfirmed,[{attempt_id:'a1',desire_ids:['w1'],excerpt:'晚安',since:'2026-09-25T00:00:00Z'}]);
});
test('a send of unknown outcome is checked again under its own id and only then does the tick go on (AD2-14)',async()=>{
 const events=[];const loop=new MindLoop({eligibility:()=>({eligible:true}),ownerEpoch:()=> 'owner-1',isBusy:()=>false,draft:async()=>assert.fail('nothing ready'),send:async()=>assert.fail('must not send'),
   resume:async candidate=>{events.push(['resume',candidate.attempt_id]);return{state:'unconfirmed',reason:'receipt-unavailable'};},
   call:async(action,request)=>{events.push([action,request]);if(action==='candidate')return{eligible:false,reason:'no-actionable-desire',reconcile:[{attempt_id:'old',owner_epoch:'owner-1'}]};return request;}});
 loop.review=async()=>{};await loop.tick();
 assert.deepEqual(events.find(([action])=>action==='resume'),['resume','old']);
 const settled=events.at(-1)[1];assert.equal(settled.attempt_id,'old');assert.equal(settled.state,'unconfirmed');assert.equal(settled.reason,'receipt-still-unknown');
});
test('items whose sources moved are shown and marked 待复核; the bounds are wider (N12, N15)',()=>{
 const desires=Array.from({length:20},(_,i)=>({id:'d'+i,kind:'contact',status:'wanted',topic:'t',content:'c'+i,needs_review:i===19,expires_at:'2999-01-01T00:00:00Z'}));
 const state={continuity:{needs_review:true},expression:{guidance:['a','b','c','d']},appraisal_summary:{understanding:{meaning:'m',needs_review:true}},
   selected_concerns:Array.from({length:8},(_,i)=>({id:'c'+i,needs_review:i===0})),rhythm:{phase:'awake',needs_review:false},desires,dimensions:{mood:{value:50,needs_review:true}}};
 const view=interactionView(state);
 assert.equal(view.desires.length,INTERACTION_LIMITS.desires);assert.equal(view.desires.at(-1).review,'待复核');
 assert.equal(view.concerns.length,INTERACTION_LIMITS.concerns);assert.equal(view.concerns[0].review,'待复核');
 assert.equal(view.understanding.review,'待复核');assert.equal(view.expression.review,'待复核');assert.equal(view.dimensions.mood.review,'待复核');
 const text=stateContext({state:{...state,dimensions:{mood:{value:50}}}});
 assert.ok(!text.includes('工作质量'));assert.ok(text.includes('拒绝、忙与停止要求优先'));
});
test('the derived layers ride beside the scores, in words and a pulse, and say they are derived (emotion v2b)',()=>{
 const layers={version:'affect-layers-v1',basis:'derived',undertone:{status:'tracking',tau_hours:24,text:'踏实',dimensions:['contentment'],leaning:{contentment:68}},
   feeling:{text:'有点酸',dimensions:['jealousy']},lingering:{text:'那点小醋意还没散',dimension:'jealousy',direction:'up',since:'2026-09-27T01:00:00+00:00',strength:9.3},
   vitals:{heart_rate_bpm:77,breaths_per_min:15,basis:'derived',status:'current'}};
 const state={continuity:{activation:'active'},dimensions:{jealousy:{value:40,undertone:{value:18.4,status:'tracking'},basis:'event_inferred'},mood:{value:65,basis:'role_default'}},affect_layers:layers};
 const view=interactionView(state);
 assert.deepEqual(view.affect_layers,{basis:'derived',undertone:{status:'tracking',text:'踏实',leaning:{contentment:68}},feeling:'有点酸',lingering:'那点小醋意还没散',
   vitals:{heart_rate_bpm:77,breaths_per_min:15,basis:'derived',status:'current'}});
 assert.equal(view.dimensions.jealousy.undertone,18.4);assert.equal('undertone' in view.dimensions.mood,false);
 assert.equal(interactionView({...state,affect_layers:{...layers,lingering:null}}).affect_layers.lingering,null);
 assert.equal(interactionView({...state,continuity:{activation:'shadow'}}).affect_layers,undefined,'shadow shows nothing new');
 assert.equal(interactionView({continuity:{activation:'active'},dimensions:{}}).affect_layers,undefined,'an older core sends none');
 const text=stateContext({state});
 assert.ok(text.includes('由宿主从已提交的分数本地推导')&&text.includes('"feeling":"有点酸"'));
 assert.ok(!/(src|mem)_/.test(JSON.stringify(view.affect_layers)));
});
test('the minute loop runs one contact tick a minute (AD2-15)',async t=>{
 t.mock.timers.enable({apis:['setInterval']});
 const events=[];
 const loop=new MindLoop({call:async action=>{events.push(action);return action==='review'?{state:'idle'}:{eligible:false,reason:'no-actionable-desire'};},
  eligibility:()=>({eligible:true}),ownerEpoch:()=>'owner-1',isBusy:()=>false,draft:async()=>assert.fail('no draft'),send:async()=>assert.fail('no send')});
 loop.start();
 t.mock.timers.tick(60000);
 for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(events.filter(action=>action==='review').length,1);
 assert.equal(events.filter(action=>action==='candidate').length,1);
 loop.close();
});
test('a group the local check does not release goes back to Kin at once, with no semantic hold (AD2-16)',async()=>{
 const journal=new Map();let transports=0,checks=0;
 const send=createContactBatch({read:id=>structuredClone(journal.get(id)),write:(id,value)=>journal.set(id,structuredClone(value)),
   preflight:async()=>{checks++;return {state:'pending',reason:'repair'};},send:async()=>{transports++;}});
 const result=await send({id:'group-1',text:'One\n\nTwo',channel:'wechat'});
 assert.equal(result.state,'needs-review');assert.equal(checks,1);assert.equal(transports,0);
 assert.equal(journal.get('group-1').failure.category,'contract');assert.equal(journal.get('group-1').reviewNotBefore,undefined);
});
