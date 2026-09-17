import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter} from './mobile-router.mjs';
import {WorkLockReview} from './work-lock-review.mjs';
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
  assert.equal(new Set(Object.values(REVIEWER_PURPOSES)).size,3);
  assert.equal(new Set(Object.values(REVIEWER_LANES)).size,3);
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
