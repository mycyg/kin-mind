import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {workEvidence} from '../../adapters/work-review-evidence.mjs';
import {WorkLockReview,taskFingerprint} from '../../adapters/work-lock-review.mjs';

const HOUR=3600000;
async function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-work-chat-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const clock={now:10000};let summaries=0;const told=[];
  const runtime={known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'gpt-6.1-sol',modelProvider:'custom-gateway',reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on',
    providerOverride:false,active:false,queued:0,backgroundTasks:0,pendingDeliveries:0};
  const router=new MobileRouter({file:path.join(root,'router.json'),sessionId:'synthetic',now:()=>++clock.now,
    inspect:async()=>({...runtime}),classify:async({text})=>({route:text==='work'?'work':'chat',reason:'synthetic'}),
    switchModel:async()=>assert.fail('chat must preserve work model'),waitForIdle:async()=>assert.fail('unexpected wait')});
  await router.dispatch({id:'original',kind:'owner',text:'work'},async()=> 'new-turn');
  const task=router.currentTask();
  await router.requestMode({taskOutcome:'accepted',mode:'work',commandId:'accept',completedTaskId:task.id,completedInputVersion:1,reason:'I will do it'});
  await router.observe('prompt-start',{taskId:task.id,inputVersion:1,turnFence:0,inputIds:['original']});runtime.active=true;runtime.nativeStatus='active';
  let dispatch;
  await router.dispatch({id:'chat',kind:'owner',text:'想你啦'},async d=>{dispatch=d;return 'steered';});
  runtime.active=false;runtime.nativeStatus='idle';await router.observe('prompt-end',{taskId:task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  const args={router,file:path.join(root,'review.json'),now:()=>++clock.now,idleMs:HOUR,
    collect:async()=>({input:{inputs:[{id:'original',text:'work'},{id:'chat',text:'想你啦'}],outputs:[],unsentDrafts:[{id:'draft',text:'unsent'}]},facts:{unsentDraftIds:['draft'],undelivered:[]}}),
    summarize:async()=>{summaries++;return {summary:{summary:'Asked for a work item; nothing delivered yet.',delivered:[],open:['the work item'],unsent:['one reply draft'],evidenceIds:['original']},
      receipt:{model:'deepseek-flash',requestId:'isolated-summary'}};},
    deliver:async taskId=>{told.push(taskId);return {state:'accepted'};}};
  const review=new WorkLockReview(args);
  return {router,task,runtime,review,args,dispatch,clock,told,summaries:()=>summaries};
}

test('chat during accepted work stays context on the task and never changes its version',async t=>{
  const f=await fixture(t);
  assert.equal(f.dispatch.intent,'chat');assert.equal(f.dispatch.taskId,f.task.id);
  assert.deepEqual(f.task.inputIds,['original']);assert.deepEqual(f.task.contextInputIds,['chat']);
  assert.equal(f.task.inputVersion,1);assert.equal(f.task.status,'running');
});

test('an idle undeclared task gets a facts summary for Kin; the lock and drafts stay hers (N1-N3)',async t=>{
  const f=await fixture(t);
  assert.equal((await f.review.tick()).reason,'task-recently-active');assert.equal(f.summaries(),0);
  f.clock.now+=HOUR+1;
  const told=await f.review.tick();
  assert.deepEqual([told.state,f.summaries(),f.told.length],['told',1,1]);
  assert.equal(f.task.status,'running','a summary never releases or keeps the lock by itself');
  assert.equal(f.task.handoff,undefined,'and never asks Kin to continue');
  assert.deepEqual(f.task.workSummary.unsentDraftIds,['draft'],'unsent drafts are facts, not withdrawn');
  assert.equal(f.router.workFacts()[0].workSummary.summary,'Asked for a work item; nothing delivered yet.');
  // Unchanged facts are not summarized again before the doubled interval.
  f.clock.now+=HOUR+1;assert.equal((await f.review.tick()).state,'told');assert.equal(f.summaries(),1);
  f.clock.now+=HOUR;const again=await f.review.tick();
  assert.deepEqual([again.state,again.repeats,f.summaries(),f.told.length],['told',1,2,2]);
  assert.ok(again.retryAt-again.checkedAt>=4*HOUR-10,'each repeat waits twice as long');
  const restarted=new WorkLockReview(f.args);await restarted.tick();assert.equal(f.summaries(),2);
});

test('no summary for a proposal, a declared outcome, a stopping task or a busy session',async t=>{
  for(const block of ['proposed','declared','cancel','active']) {
    const f=await fixture(t);f.clock.now+=2*HOUR;
    if(block==='proposed')f.task.status='proposed';
    if(block==='declared')f.task.completion={outcome:'partial',inputVersion:1,at:f.clock.now};
    if(block==='cancel')f.task.cancelRequested=true;
    if(block==='active')f.runtime.active=true;
    const result=await f.review.tick();
    assert.equal(result.state,'waiting',block);assert.equal(f.summaries(),0);assert.equal(f.task.workSummary,undefined);
  }
});

test('a task that changes while it is being summarized records nothing',async t=>{
  const f=await fixture(t);f.clock.now+=2*HOUR;
  const original=f.review.summarize;
  f.review.summarize=async input=>{const r=await original(input);f.task.deliveries.late={state:'accepted',messageId:'late',at:f.clock.now};return r;};
  const before=taskFingerprint(f.router,f.task);
  assert.equal((await f.review.tick()).state,'superseded');
  assert.notEqual(taskFingerprint(f.router,f.task),before);assert.equal(f.task.workSummary,undefined);assert.equal(f.told.length,0);
});

test('confirmed native cancellation archives old active tools without inventing their outcome',async t=>{
  const f=await fixture(t);f.task.executionEpoch=1;f.router.state.executionEpoch=1;
  const old={status:'in_progress',inputVersion:1,turnFence:0};f.task.tools.original=old;
  f.task.tools.current={status:'in_progress',inputVersion:1,turnFence:1};
  const boundary={id:'boundary',taskId:f.task.id,fromEpoch:0,toEpoch:1,receipt:{state:'interrupted',cancel:{cancelledTurn:true},runtime:{known:true,sessionId:'synthetic',nativeStatus:'idle',active:false,backgroundTasks:0}}};
  f.router.state.forceBoundaries.push(boundary);
  assert.equal(f.router.archiveInterruptedTools(),true);
  assert.equal(f.task.tools.original,undefined);assert.ok(f.task.tools.current);
  assert.equal(f.task.toolHistory['original:fence:0'].status,'in_progress','external outcome stays unknown');
  assert.equal(f.task.toolHistory['original:fence:0'].fencedBy,'boundary');
  assert.equal(f.router.archiveInterruptedTools(),false);
  f.task.tools.unknown={...old};boundary.receipt.runtime.backgroundTasks=1;
  assert.equal(f.router.archiveInterruptedTools(),false);assert.ok(f.task.tools.unknown);
});

test('evidence is read by id: owner words quoted, host inputs named, unsent drafts reported, gaps named',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-work-evidence-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  fs.mkdirSync(path.join(root,'outbox'));
  for(const source of [{id:'owner',senderId:'bound-owner'},{id:'wechat',wechatMessage:{from_user_id:'bound-owner'}}])
    fs.writeFileSync(path.join(root,source.id+'.json'),JSON.stringify({...source,canonicalSessionId:'synthetic',text:'deliver work'}));
  fs.writeFileSync(path.join(root,'outbox','sent.json'),JSON.stringify({id:'sent',state:'accepted',messageId:'m1',text:'here it is'}));
  // A stray unreadable record elsewhere in the outbox is never read, so it cannot fail this task (AD2-19).
  fs.writeFileSync(path.join(root,'outbox','broken.json'),'{');
  const inputs=[{id:'owner',kind:'owner',state:'accepted'},{id:'wechat',kind:'owner',state:'accepted'},
    {id:'handoff:resume',kind:'handoff',state:'accepted'},{id:'gone',kind:'owner',state:'accepted'}];
  const manifests={findBubble:id=>id==='draft'?{manifest:{state:'held'},bubble:{state:'unsent',text:'not sent yet',request:{reply_id:'owner'}}}:null};
  const evidence=workEvidence({sessionId:'synthetic',inputDirectory:root,outboxDirectory:path.join(root,'outbox'),manifests,lastReply:async()=>({text:'final',status:'completed'})});
  const snapshot={task:{id:'task',status:'running',inputVersion:1,inputIds:['owner','wechat','handoff:resume','gone'],contextInputIds:[],
    deliveries:{sent:{state:'accepted',messageId:'m1'},draft:{state:'deferred'},lost:{state:'unconfirmed'}},tools:{a:{status:'completed'}}},inputs};
  const result=await evidence.collect(snapshot);
  assert.deepEqual(result.input.inputs.filter(i=>i.text).map(i=>i.id),['owner','wechat']);
  assert.equal(result.input.inputs.find(i=>i.id==='handoff:resume').text,undefined);
  assert.equal(result.input.inputs.find(i=>i.id==='gone').missing,true);
  assert.deepEqual(result.input.outputs.map(o=>[o.id,o.state]),[['sent','accepted'],['lost','unconfirmed']]);
  assert.deepEqual(result.facts.unsentDraftIds,['draft']);assert.equal(result.input.lastPublicReply.text,'final');
});

test('a summary failure persists its cause and waits for its retry time',async t=>{
  const f=await fixture(t);f.clock.now+=2*HOUR;
  f.review.collect=async()=>{throw Error('evidence unavailable');};
  const failed=await f.review.tick();assert.equal(failed.state,'failed');
  assert.equal(f.review.view().reason,'evidence unavailable');assert.ok(failed.retryAt>failed.checkedAt);
  let reads=0;const restarted=new WorkLockReview({...f.args,collect:async()=>{reads++;throw Error('again');}});
  await restarted.tick();assert.equal(reads,0);assert.equal(f.summaries(),0);
});

test('the watchdog expires only orphaned unsubmitted inputs, even without a task',async t=>{
  const f=await fixture(t);f.task.status='completed';
  for(const [id,state] of [['orphan','selected'],['live','selected'],['uncertain','unconfirmed'],['submitted','selected'],['fresh','selected']])
    f.router.state.inputs[id]={id,kind:'owner',state,at:id==='fresh'?f.router.now():-21*60000,...(id==='submitted'?{submissionStartedAt:1}:{})};
  f.router.save('test-orphans');
  const restarted=new MobileRouter({...f.router,file:f.router.file});restarted.inflight.set('live',Promise.resolve());
  await restarted.watch();
  assert.equal(restarted.state.inputs.orphan.state,'failed-before-submit');
  assert.equal(restarted.state.inputs.orphan.reason,'input-preparation-timeout');
  assert.equal(restarted.state.inputs.live.state,'selected');
  assert.equal(restarted.state.inputs.uncertain.state,'unconfirmed');
  assert.equal(restarted.state.inputs.submitted.state,'selected');
  assert.equal(restarted.state.inputs.fresh.state,'selected');
});


test('an invalid summary returns only existing task facts to Kin, without deciding or retrying the draft',async t=>{
  const f=await fixture(t);f.clock.now+=2*HOUR;
  f.review.summarize=async()=>{throw Error('deepseek-invalid-work-summary');};
  const result=await f.review.tick();
  assert.equal(result.state,'told');assert.equal(result.factsOnly,true);
  assert.equal(f.told.length,1);assert.equal(f.task.status,'running');
  assert.equal(f.task.workSummary,undefined);assert.equal(f.task.completion,undefined);
  await f.review.tick();assert.equal(f.told.length,1,'no immediate repeated internal turn');
});

test('an invalid summary cannot return stale facts after owner cancellation or task change',async t=>{
  for(const change of ['cancel','input']) {
    const f=await fixture(t);f.clock.now+=2*HOUR;
    f.review.summarize=async()=>{if(change==='cancel')f.task.cancelRequested=true;else f.task.inputVersion++;throw Error('deepseek-invalid-work-summary');};
    assert.equal((await f.review.tick()).state,'superseded');assert.equal(f.told.length,0);
  }
});
