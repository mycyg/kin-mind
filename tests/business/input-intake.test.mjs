// CR-LIFE-02: an owner input is on the router's ledger under its own id from the moment the
// host has verified its source, before anything that can fail prepares it. A failure there is
// recorded as never submitted and reaches the watchdog's retries and, past them, its notice.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {SUBMIT_RETRY_MS,inputInFlight,inputSummary} from '../../adapters/input-ledger.mjs';

const MINUTE=60000;
function fixture(t,options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-intake-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const clock={now:Date.parse('2026-09-25T00:00:00Z')};
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',
    active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const args={file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>clock.now,
    classify:async()=>({route:'chat',reason:'synthetic'}),waitForIdle:async()=>{throw Error('waiting');},...options};
  return {root,clock,runtime,args,router:new MobileRouter(args)};
}
const receivedAt='2026-09-24T23:59:00Z';

test('a verified input is on the ledger before its preparation, and a preparation failure there is retried under its own id (CR-LIFE-02)',async t=>{
  const f=fixture(t);
  const {record,since}=await f.router.received({id:'in-1',kind:'owner',channel:'feishu',receivedAt});
  assert.equal(record.state,'preparing');
  assert.equal(record.channel,'feishu','the channel it came in on is its own fact (CR-LIFE-17)');
  assert.equal(record.firstReceivedAt,Date.parse(receivedAt));
  assert.equal(inputInFlight(f.router.state.inputs['in-1']),true,'while it is prepared it is in flight');
  // The attachment vanished before the prompt was built.
  await f.router.intakeFailed('in-1','intake-ENOENT',{since});
  const failed=f.router.state.inputs['in-1'];
  assert.deepEqual([failed.state,failed.reason,failed.retry.attempts,failed.retry.evidence],['failed-before-submit','intake-ENOENT',1,'not-submitted']);
  assert.equal(inputSummary(failed),'received');
  // The watchdog puts the inbox job back once the retry is due, and never before.
  const requeued=[];const requeue=async id=>{requeued.push(id);return {state:'requeued'};};
  await f.router.watch({requeue,notifyOwner:async()=>assert.fail('nothing to tell yet')});
  assert.deepEqual(requeued,[]);
  f.clock.now+=SUBMIT_RETRY_MS[0];
  await f.router.watch({requeue,notifyOwner:async()=>assert.fail('nothing to tell yet')});
  assert.deepEqual(requeued,['in-1']);
  // The replayed job is taken in again under the same id; routing keeps the retries it used.
  const again=await f.router.received({id:'in-1',kind:'owner',channel:'feishu',receivedAt});
  assert.deepEqual([again.record.state,again.record.retry.attempts],['preparing',1]);
  await f.router.dispatch({id:'in-1',kind:'owner',text:'hi',receivedAt,channel:'feishu'},async()=>'new-turn');
  const routed=f.router.state.inputs['in-1'];
  assert.equal(routed.state,'accepted');assert.equal(routed.route,'chat');assert.equal(routed.intake,undefined,'the routed record replaces the intake one');
  assert.equal(routed.retry.attempts,1);assert.equal(routed.channel,'feishu');
  // Nothing is marked on an input whose submission began, or that is answered.
  await f.router.intakeFailed('in-1','intake-late',{since:again.since});
  assert.equal(f.router.state.inputs['in-1'].state,'accepted');
});

test('an input whose preparation keeps failing is told to the owner as the system, once the retries are used (CR-LIFE-02)',async t=>{
  const f=fixture(t);
  const told=[];
  const notifyOwner=async(kind,id,options)=>{told.push([kind,id,options?.mayStart]);return {state:'accepted',messageId:'notice-1'};};
  for(let attempt=0;attempt<=SUBMIT_RETRY_MS.length;attempt++) {
    const {since}=await f.router.received({id:'in-2',kind:'owner',channel:'wechat',receivedAt});
    f.clock.now+=1000;
    await f.router.intakeFailed('in-2','intake-preparation-failed',{since});
    f.clock.now+=SUBMIT_RETRY_MS.at(-1)+MINUTE;
  }
  const record=f.router.state.inputs['in-2'];
  assert.equal(record.retry.exhausted,true);
  await f.router.watch({requeue:async()=>assert.fail('no retry is left'),notifyOwner});
  assert.deepEqual(told,[['stopped','in-2',true]]);
  assert.equal(inputSummary(f.router.state.inputs['in-2']),'failed-notified');
});

test('a failure the dispatch already recorded is not counted again, and a freeze refusal counts nothing (CR-LIFE-02)',async t=>{
  const f=fixture(t);
  const {since}=await f.router.received({id:'in-3',kind:'owner',channel:'feishu',receivedAt});
  // The host's own boundary marks the submission itself; this one fails before it does.
  await assert.rejects(f.router.dispatch({id:'in-3',kind:'owner',text:'hi',receivedAt,submissionProtocol:'host-boundary-v1'},async()=>{throw Error('session could not be restored');}),
    error=>error.code==='input-not-submitted');
  assert.deepEqual([f.router.state.inputs['in-3'].state,f.router.state.inputs['in-3'].retry.attempts],['failed-before-submit',1]);
  await f.router.intakeFailed('in-3','intake-preparation-failed',{since});
  assert.equal(f.router.state.inputs['in-3'].retry.attempts,1,'the same failure, counted once');

  // A freeze that begins between the intake and the dispatch takes nothing: the inbox keeps
  // the job, no attempt is counted, and the input no longer holds a drain.
  await f.router.received({id:'in-4',kind:'owner',channel:'feishu',receivedAt});
  await f.router.freezeDispatch('synthetic migration');
  await assert.rejects(f.router.dispatch({id:'in-4',kind:'owner',text:'hi',receivedAt},async()=>assert.fail('frozen')),error=>error.code==='dispatch-frozen');
  const held=f.router.state.inputs['in-4'];
  assert.deepEqual([held.state,held.reason,held.retry.attempts],['failed-before-submit','dispatch-frozen',0]);
  assert.equal(inputInFlight(held),false);
  const requeued=[];
  await f.router.watch({requeue:async id=>{requeued.push(id);return {state:'pending'};},notifyOwner:async()=>assert.fail('nothing to tell')});
  assert.deepEqual(requeued,[],'nothing is requeued while frozen');
  await f.router.thawDispatch();
  await f.router.watch({requeue:async id=>{requeued.push(id);return {state:'pending'};},notifyOwner:async()=>assert.fail('nothing to tell')});
  assert.deepEqual(requeued,['in-4'],'after the thaw the watchdog finds the job still waiting in the inbox');
  assert.equal(f.router.state.inputs['in-4'].retry.exhausted,undefined);
});

test('an input cut off by a restart while it was prepared is proven unsubmitted and goes to the same retry (CR-LIFE-02)',async t=>{
  const f=fixture(t);
  await f.router.received({id:'in-5',kind:'owner',channel:'wechat',receivedAt});
  const restarted=new MobileRouter(f.args);
  const record=restarted.state.inputs['in-5'];
  assert.deepEqual([record.state,record.reason,record.retry.attempts],['failed-before-submit','restart-before-submission',0]);
  const requeued=[];
  await restarted.watch({requeue:async id=>{requeued.push(id);return {state:'requeued'};},notifyOwner:async()=>assert.fail('nothing to tell')});
  assert.deepEqual(requeued,['in-5']);
  // An input left preparing past the review interval expires into the same retry.
  const {since}=await restarted.received({id:'in-6',kind:'owner',channel:'feishu',receivedAt});
  f.clock.now+=21*MINUTE;
  await restarted.watch({requeue:async()=>({state:'processing'}),notifyOwner:async()=>assert.fail('nothing to tell')});
  assert.deepEqual([restarted.state.inputs['in-6'].state,restarted.state.inputs['in-6'].reason],['failed-before-submit','input-preparation-timeout']);
  assert.ok(since<f.clock.now);
});
