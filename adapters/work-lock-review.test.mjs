import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {MobileRouter} from './mobile-router.mjs';
import {WorkLockReview,REVIEW_LIMITS} from './work-lock-review.mjs';
import {REVIEWER_LANES,REVIEWER_PURPOSES} from './mobile-reviewer.mjs';

async function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-work-review-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let time=10000,calls=0;
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'gpt-6-astra',active:false,backgroundTasks:0,handoffTasks:0,queued:0,pendingDeliveries:0};
  const args={file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),classify:async()=>({route:'work',reason:'classifier-unconfirmed'}),switchModel:async model=>({...runtime,model:runtime.model=model}),waitForIdle:async()=>{throw Error('must not wait');},now:()=>time};
  const router=new MobileRouter(args);
  await router.dispatch({id:'input-1',text:'You may explore when idle'},async()=> 'new-turn');
  await router.observe('prompt-end',{stopReason:'end_turn'});
  await router.observe('delivery',{id:'reply-1',state:'accepted',messageId:'receipt-1'});
  time+=3000;
  const evidence={input:{inputs:[{id:'input-1',text:'You may explore when idle'}],cancellableDeferred:[]},receipts:{'reply-1':{state:'accepted',messageId:'receipt-1'}}};
  let decision={disposition:'not_a_task',reason:'Optional autonomy preference, acknowledged in the delivered reply',evidenceIds:['input-1'],remaining:[],discardDraftIds:[]};
  const result=()=>({decision:structuredClone(decision),receipt:{provider:'deepseek',model:'deepseek-flash',reasoning:'high',requestId:'synthetic-request',verifiedAt:'2026-01-01T00:00:00Z'}});
  const reviewArgs={router,file:path.join(root,'reviews.json'),collect:async()=>structuredClone(evidence),review:async()=>{calls++;return result();},now:()=>time};
  return {router,runtime,args,reviewArgs,evidence,result,decision,setDecision:d=>decision=d,review:new WorkLockReview(reviewArgs),calls:()=>calls,advance:n=>time+=n};
}

test('DeepSeek can close an accidental chat lock after verified delivery and resume the same session',async t=>{
  const f=await fixture(t),tid=f.router.currentTask().id;
  const r=await f.review.tick();assert.equal(r.state,'applied');assert.equal(f.router.tasks().length,0);
  assert.equal(f.router.state.tasks[tid].status,'canceled');assert.equal(f.router.state.inputs['input-1'].state,'accepted');
  await f.router.applyPendingMode();assert.equal(f.runtime.model,'deepseek-flash');assert.equal(f.router.sessionId,'synthetic');
  assert.equal(f.review.view().model,'deepseek-flash');assert.equal(f.review.view().reasoning,'high');
});

test('a real finished task uses semantic and delivery evidence, never just end_turn',async t=>{
  const f=await fixture(t);f.decision.disposition='complete';f.decision.reason='Requested result verified in evidence';
  f.evidence.receipts['reply-1']={state:'unconfirmed'};
  assert.equal((await f.review.tick()).state,'waiting');assert.equal(f.router.tasks().length,1);
  f.evidence.receipts['reply-1']={state:'accepted',messageId:'receipt-1'};f.advance(20*60000);
  assert.equal((await f.review.tick()).state,'applied');assert.equal(Object.values(f.router.state.tasks)[0].status,'completed');
});

test('unchanged held work is reassessed every twenty minutes including after restart',async t=>{
  const f=await fixture(t);f.decision.disposition='keep';f.decision.remaining=['A research result is still pending'];
  assert.equal((await f.review.tick()).state,'kept');assert.equal(f.calls(),1);
  f.advance(19*60000);await f.review.tick();assert.equal(f.calls(),1);
  const restored=new WorkLockReview(f.reviewArgs);await restored.tick();assert.equal(f.calls(),1);
  f.advance(60000);await restored.tick();assert.equal(f.calls(),2);assert.equal(f.router.tasks().length,1);
  const events=fs.readFileSync(f.reviewArgs.file+'.events.jsonl','utf8').trim().split('\n').map(JSON.parse);
  assert.equal(events.filter(e=>e.state==='reviewed').length,2);
});

test('long native tools, queued inputs, background work, unknown runtime and handoffs preserve the work provider',async t=>{
  for(const field of ['active','backgroundTasks','handoffTasks','queued','pendingDeliveries']) {
    const f=await fixture(t);f.runtime[field]=1;assert.equal((await f.review.tick()).state,'waiting');assert.equal(f.calls(),0);assert.equal(f.runtime.model,'gpt-6-astra');
  }
  const f=await fixture(t);f.runtime.known=false;await f.review.tick();assert.equal(f.calls(),0);
});

test('new owner input while DeepSeek is judging supersedes its completion decision',async t=>{
  const f=await fixture(t);let resolve;
  f.review.review=()=>new Promise(r=>resolve=r);
  const pending=f.review.tick();while(!resolve)await new Promise(r=>setTimeout(r,0));
  await f.router.dispatch({id:'input-2',text:'Also write the code'},async()=> 'new-turn');
  resolve(f.result());assert.equal((await pending).state,'superseded');assert.equal(f.router.currentTask().inputVersion,2);assert.equal(f.runtime.model,'gpt-6-astra');
});

test('changed source evidence, a new background tool, cannot close work while terminal failures remain reviewable',async t=>{
  const f=await fixture(t);f.review.review=async()=>{f.evidence.input.inputs[0].text='Corrected actual work request';return f.result();};
  assert.equal((await f.review.tick()).state,'superseded');assert.equal(f.router.tasks().length,1);
  const g=await fixture(t);g.review.review=async()=>{g.runtime.backgroundTasks=1;return g.result();};
  assert.equal((await g.review.tick()).state,'waiting');assert.equal(g.router.tasks().length,1);
  const h=await fixture(t);h.router.currentTask().tools.failed={status:'failed'};await h.review.tick();assert.equal(h.calls(),1);
});

test('only a never-submitted superseded chat draft can be discarded with a not_a_task judgment',async t=>{
  const f=await fixture(t);const task=f.router.currentTask();task.deliveries.old={state:'pending',at:100};
  f.evidence.input.cancellableDeferred=[{id:'old',text:'An old acknowledgement'}];f.evidence.receipts.old={state:'not-submitted'};f.decision.discardDraftIds=['old'];
  let canceled=0;f.review.cancelDeferred=async()=>{canceled++;return{state:'canceled-before-send'};};
  assert.equal((await f.review.tick()).state,'applied');assert.equal(canceled,1);assert.equal(task.deliveries.old.state,'canceled-before-send');assert.equal(task.workReview.disposition,'not_a_task');
  const g=await fixture(t);g.router.currentTask().deliveries.old={state:'pending'};g.evidence.input.cancellableDeferred=[{id:'old'}];g.evidence.receipts.old={state:'unconfirmed'};g.decision.discardDraftIds=['old'];
  assert.equal((await g.review.tick()).state,'waiting');assert.equal(g.router.tasks().length,1);
});

test('provider failure retries after the interval and cannot fabricate completion',async t=>{
  const f=await fixture(t);f.review.review=async()=>{throw Error('timeout');};
  assert.equal((await f.review.tick()).state,'failed');assert.equal(f.router.tasks().length,1);
  f.review.review=async()=>f.result();assert.equal((await f.review.tick()).state,'failed');f.advance(20*60000);
  assert.equal((await f.review.tick()).state,'applied');
});

test('missing original input coverage or unverified model holds the task',async t=>{
  const f=await fixture(t);f.decision.evidenceIds=['invented'];const failed=await f.review.tick();assert.equal(failed.state,'failed');assert.equal(f.router.tasks().length,1);assert.match(failed.reason,/unknown-input-reference/);assert.equal(failed.decisionVerified,false);assert.equal(failed.receipt.requestId,'synthetic-request');assert.deepEqual(failed.decision.evidenceIds,['invented']);
  const g=await fixture(t);g.review.review=async()=>({...g.result(),receipt:{provider:'another',model:'deepseek-flash'}});assert.equal((await g.review.tick()).state,'failed');assert.equal(g.router.tasks().length,1);
});

test('assistant completion proposal cannot bypass installed DeepSeek review',async t=>{
  const f=await fixture(t),task=f.router.currentTask();
  task.completion={inputVersion:task.inputVersion,at:task.turnEndedAt-1,summary:'Assistant says done'};
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  f.decision.disposition='keep';f.decision.remaining=['Actual requested result is missing'];
  assert.equal((await f.review.tick()).state,'kept');await f.router.reconcile();assert.equal(f.router.tasks().length,1);
});

test('replacement settlement is atomic with every delivery proof',async t=>{
  const f=await fixture(t),task=f.router.currentTask();
  f.decision.disposition='complete';
  task.deliveries.upload={state:'unconfirmed'};
  task.deliveries.other={state:'unconfirmed'};
  f.evidence.receipts.upload={state:'not-submitted',fulfilledBy:[{messageId:'replacement',verified:true}]};
  f.evidence.receipts.other={state:'unconfirmed'};
  assert.equal((await f.review.tick()).reason,'delivery-unconfirmed');
  assert.equal(task.deliveries.upload.state,'unconfirmed');
  assert.equal(task.deliveries.upload.fulfilledBy,undefined);
  f.evidence.receipts.other={state:'not-submitted',fulfilledBy:[{messageId:'another-replacement',verified:true}]};
  f.advance(20*60000);
  assert.equal((await f.review.tick()).state,'applied');
  assert.equal(task.deliveries.upload.state,'not-submitted');
  assert.equal(task.deliveries.upload.fulfilledBy[0].messageId,'replacement');
});

test('a completed step whose delivery is unconfirmed keeps the work lock, and no other verdict stands in for it',async t=>{
  const f=await fixture(t);
  f.decision.disposition='complete';f.decision.reason='The requested step itself is finished';
  f.evidence.receipts['reply-1']={state:'unconfirmed'};
  const held=await f.review.tick();
  assert.equal(held.state,'waiting');
  assert.equal(held.reason,'delivery-unconfirmed');
  assert.equal(f.router.tasks().length,1,'a finished step is not a finished obligation');
  // The attempt is typed by its own lane and purpose. A step verdict, a plan verdict
  // and an owner-work verdict are three separate records, never one reused judgment.
  assert.equal(held.lane,REVIEWER_LANES.reviewWork);
  assert.equal(held.purpose,REVIEWER_PURPOSES.reviewWork);
  // Every entry point is accounted under a purpose of its own. The reply-tail decision is the routing
  // call's own question asked alone, so it shares that call's lane and nothing else.
  assert.equal(new Set(Object.values(REVIEWER_PURPOSES)).size,Object.keys(REVIEWER_PURPOSES).length);
  assert.equal(new Set(['classify','reviewWork','audit'].map(entry=>REVIEWER_LANES[entry])).size,3);
  assert.equal(REVIEWER_LANES.tail,REVIEWER_LANES.classify);
  // Confirmed delivery is judged by a new review, not by replaying the earlier one.
  f.evidence.receipts['reply-1']={state:'accepted',messageId:'receipt-1'};f.advance(20*60000);
  assert.equal((await f.review.tick()).state,'applied');
  assert.equal(f.calls(),2);
});

test('a refused user-work lane holds the lock and is never mistaken for a verdict',async t=>{
  const f=await fixture(t);
  const review=new WorkLockReview({...f.reviewArgs,review:async()=>{
    throw Object.assign(Error('model-lane-degraded'),{leaseSkipped:true,lease:{lane:'user-work',leaseState:'degraded',leaseReason:'lease-service-unreachable'}});}});
  const result=await review.tick();
  assert.equal(result.state,'waiting');
  assert.equal(result.reason,'work-review-lane-unavailable');
  assert.equal(result.decision,undefined);
  assert.equal(result.retryAt-result.checkedAt,5*60000);
  assert.equal(f.router.tasks().length,1);
});

// ---- no size ever leaves the lock held with no review able to run ----

test('an ordinary review asks for exactly what it always asked for',async t=>{
  const f=await fixture(t);let request;
  f.review.review=async input=>{request=input;return f.result();};
  assert.equal((await f.review.tick()).state,'applied');
  assert.deepEqual(request,f.evidence.input);
  assert.equal(request.review_chunk,undefined,'nothing is added to the small case');
  assert.equal(createHash('sha256').update(JSON.stringify(request)).digest('hex'),
    '7f22459b345820ed4c07de3637b01c0b8d6311d4d4cd5d8c12f4c7f6269d9eea','byte for byte the request of the previous release');
});

function oversized(f,{count=60,chars=9000}={}) {
  f.evidence.input.inputs=[f.evidence.input.inputs[0],...Array.from({length:count},(_,n)=>({id:'context-'+n,text:'c'.repeat(chars)}))];
  f.evidence.input.outputs=[{id:'reply-1',text:'o'.repeat(chars),receivedByServer:true}];
}

test('evidence past the single-review limits is reviewed in bounded chunks, not refused for ever',async t=>{
  const f=await fixture(t),seen=[];
  oversized(f);
  assert.ok(JSON.stringify(f.evidence.input).length>REVIEW_LIMITS.bytes);
  f.review.review=async input=>{seen.push(input);return f.result();};
  const applied=await f.review.tick();
  assert.equal(applied.state,'applied','the work lock is released on the merits');
  assert.ok(seen.length>1&&seen.length<=REVIEW_LIMITS.maxChunks,'bounded number of steps');
  assert.deepEqual(seen.map(c=>c.review_chunk.index),seen.map((_,n)=>n+1));
  assert.equal(seen[0].review_chunk.count,seen.length);
  assert.equal(applied.chunks,seen.length);assert.equal(applied.receipt.chunks,seen.length);
  // Every chunk knows the whole shape, and each one after the first knows what was decided before it.
  assert.equal(seen[0].review_chunk.outline.inputs.length,61);
  assert.deepEqual(seen[0].review_chunk.decisions,[]);
  assert.equal(seen[1].review_chunk.decisions[0].disposition,'not_a_task');
  assert.equal(seen.at(-1).review_chunk.decisions.length,seen.length-1);
  // Nothing is dropped in silence: an excerpted body says how long the original was.
  const excerpted=seen.flatMap(c=>c.inputs).filter(i=>i.text_excerpted);
  assert.equal(excerpted.length,60);
  assert.ok(excerpted.every(i=>i.text_excerpted.chars===9000&&i.text.length<=REVIEW_LIMITS.excerpt+8));
  assert.deepEqual(seen.flatMap(c=>c.inputs).map(i=>i.id),f.evidence.input.inputs.map(i=>i.id),'the host keeps global order and coverage');
  assert.equal(f.router.tasks().length,0);
});

test('one chunk that says the work is unfinished keeps the lock',async t=>{
  const f=await fixture(t);let answered=0;
  oversized(f);
  f.review.review=async()=>{
    answered++;
    return answered===2?{...f.result(),decision:{disposition:'keep',reason:'A requested result is still missing',evidenceIds:['input-1'],remaining:['the requested result'],discardDraftIds:[]}}:f.result();
  };
  const kept=await f.review.tick();
  assert.equal(kept.state,'kept');
  assert.equal(kept.decision.disposition,'keep');
  assert.deepEqual(kept.decision.remaining,['the requested result']);
  assert.equal(f.router.tasks().length,1);
  assert.ok(answered>2,'the remaining chunks are still reviewed');
});

test('a chunk with a malformed answer is refused exactly as an unchunked one is',async t=>{
  const f=await fixture(t);
  oversized(f);
  f.review.review=async input=>input.review_chunk.index===2?{...f.result(),decision:{disposition:'complete',reason:'',evidenceIds:[],remaining:[],discardDraftIds:[]}}:f.result();
  const refused=await f.review.tick();
  assert.equal(refused.state,'failed');
  assert.match(refused.reason,/invalid-decision-shape/);
  assert.equal(f.router.tasks().length,1);
});

test('a bubble the host retired is not an unconfirmed delivery and stops holding the lock',async t=>{
  const f=await fixture(t),task=f.router.currentTask();
  f.decision.disposition='complete';f.decision.reason='The requested result reached the owner';
  task.deliveries.retired={state:'pending',at:100};
  f.evidence.receipts.retired={state:'unconfirmed'};
  const held=await f.review.tick();
  assert.deepEqual([held.state,held.reason],['waiting','delivery-unconfirmed'],'an unexplained delivery still holds it');
  f.evidence.receipts.retired={state:'retired',reason:'input-or-session-superseded',source:'transport-manifest'};
  f.advance(20*60000);
  const applied=await f.review.tick();
  assert.equal(applied.state,'applied');
  assert.equal(task.deliveries.retired.state,'retired');
  assert.equal(task.deliveries.retired.reason,'input-or-session-superseded');
  assert.deepEqual(task.workReview.retiredDeliveryIds,['retired']);
  assert.equal(f.router.tasks().length,0);
  // A task whose every bubble was retired has no delivery evidence at all, and keeps the lock.
  const g=await fixture(t);
  g.decision.disposition='complete';g.decision.reason='The requested result reached the owner';
  g.router.currentTask().deliveries['reply-1']={state:'pending'};
  g.evidence.receipts['reply-1']={state:'retired',reason:'input-or-session-superseded'};
  const empty=await g.review.tick();
  assert.deepEqual([empty.state,empty.reason],['waiting','no-delivery-evidence']);
  assert.equal(g.router.tasks().length,1);
});
