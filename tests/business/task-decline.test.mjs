import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {WorkLockReview} from '../../adapters/work-lock-review.mjs';

async function fixture(t,{manual=false}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-task-decline-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let tick=1000,switches=0;
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'gpt-6-sol',modelProvider:'custom-gateway',providerOverride:false,reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on',
    active:false,queued:0,backgroundTasks:0,pendingDeliveries:0,handoffTasks:0};
  const options={file:path.join(root,'router.json'),sessionId:'synthetic',now:()=>++tick,inspect:async()=>({...runtime}),
    classify:async({text})=>({route:text==='chat'?'chat':'work',reason:'synthetic'}),
    switchModel:async()=>{switches++;return {...runtime};},waitForIdle:async()=>assert.fail('unexpected wait')};
  const router=new MobileRouter(options);
  if(manual){router.state.mode='manual';router.state.manualProfile={provider:'custom-gateway',providerKind:'native',model:'gpt-6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'};}
  await router.dispatch({id:'original-work',kind:'owner',text:'work'},async()=> 'new-turn');
  const task=router.currentTask();
  await router.observe('prompt-start',{taskId:task.id,inputVersion:task.inputVersion,turnFence:router.state.executionEpoch});
  const command={mode:'auto',taskOutcome:'declined',completedTaskId:task.id,completedInputVersion:task.inputVersion,sourceInputId:'original-work',
    reason:'I did the initial check and do not want to do the rest',commandId:'decline-'+task.id};
  return {router,runtime,task,command,options,switches:()=>switches,now:()=>++tick};
}

test('decline is bound to the current task and version, with an idempotent distinct receipt',async t=>{
  const f=await fixture(t);
  for(const bad of [
    {...f.command,taskOutcome:'unknown'},
    {...f.command,mode:'work'},
    {...f.command,completedTaskId:undefined},
    {...f.command,completedInputVersion:undefined},
    {...f.command,completedInputVersion:f.task.inputVersion+1},
  ])await assert.rejects(()=>f.router.requestMode(bad));
  const receipt=await f.router.requestMode(f.command);
  assert.deepEqual([receipt.state,receipt.taskOutcome,receipt.taskId,receipt.inputVersion],['pending','declined',f.task.id,1]);
  assert.equal(f.task.completion.outcome,'declined');
  assert.deepEqual(await f.router.requestMode(f.command),receipt);
  assert.equal(f.task.status,'running');
  const view=await f.router.readRuntime();
  assert.equal(view.tasks[0].taskOutcome,'declined');
});

test('native end, tools and accepted refusal delivery settle only this task as canceled',async t=>{
  const f=await fixture(t,{manual:true});f.router.workReviewerEnabled=true;
  await f.router.requestMode(f.command);
  await f.router.observe('tool',{taskId:f.task.id,inputVersion:1,turnFence:0,id:'tool',status:'in_progress'});
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  await f.router.reconcile();assert.equal(f.task.status,'running');
  await f.router.observe('tool',{taskId:f.task.id,inputVersion:1,turnFence:0,id:'tool',status:'completed'});
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'original-work',id:'reply',state:'unknown'});
  await f.router.reconcile();assert.equal(f.task.status,'running');
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'original-work',id:'reply',state:'accepted',messageId:'platform-reply'});
  await f.router.reconcile();await f.router.applyPendingMode();
  assert.equal(f.task.status,'canceled');assert.equal(f.task.completedAt,undefined);
  assert.equal(f.task.completion.outcome,'declined');
  assert.equal(f.router.state.mode,'manual');assert.equal(f.router.state.manualProfile.model,'gpt-6-sol');
  assert.equal(f.router.state.inputs['original-work'].state,'accepted');
  assert.equal(f.task.deliveries.reply.messageId,'platform-reply');
});

test('a new work input supersedes the old declined version without canceling it',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  await f.router.dispatch({id:'more-work',kind:'owner',text:'work'},async()=> 'new-turn');
  assert.equal(f.task.inputVersion,2);assert.deepEqual(f.task.inputIds,['original-work','more-work']);
  assert.equal(f.task.completion,undefined);assert.equal(f.router.state.requests[f.command.commandId].state,'superseded');
  assert.equal(f.router.state.exitRequested,false);
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  await f.router.reconcile();assert.equal(f.task.status,'running');
  assert.equal(f.router.state.inputs['more-work'].state,'accepted');
});

test('a known failed earlier delivery does not prevent an accepted refusal from closing the task',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,id:'earlier-failed',state:'rejected'});
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'original-work',id:'refusal',state:'accepted',messageId:'accepted-refusal'});
  await f.router.reconcile();
  assert.equal(f.task.status,'canceled');
  assert.equal(f.task.deliveries['earlier-failed'].state,'rejected');
  assert.equal(f.task.deliveries.refusal.messageId,'accepted-refusal');
});

test('a new chat dispatch keeps its input and does not revoke an already delivered refusal',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'original-work',id:'refusal',state:'accepted',messageId:'accepted-refusal'});
  const result=await f.router.dispatch({id:'new-chat',kind:'owner',text:'chat'},async (decision,markSubmitted)=>{
    assert.equal(decision.taskId??null,null);
    markSubmitted();
    return 'new-turn';
  });
  await f.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0});
  assert.equal(result.route,'new-turn');
  assert.equal(f.task.status,'canceled');
  assert.equal(f.router.state.inputs['new-chat'].state,'accepted');
  assert.equal(f.router.state.inputs['new-chat'].taskId,undefined);
});

test('a pending refusal survives a new chat and settles only after its own bubble is accepted',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  const originalEnd=f.task.completion.turnEndedAt;
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'original-work',id:'refusal',state:'deferred'});
  f.runtime.pendingDeliveries=1;
  const result=await f.router.dispatch({id:'new-chat',kind:'owner',text:'chat'},async (_decision,markSubmitted)=>{markSubmitted();return 'new-turn';});
  assert.equal(result.route,'new-turn');
  assert.equal(f.router.state.inputs['new-chat'].state,'accepted');
  await f.router.observe('prompt-start',{taskId:f.task.id,inputVersion:1,turnFence:0});
  assert.equal(f.task.completion.outcome,'declined');
  assert.equal(f.task.completion.turnEndedAt,originalEnd);
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'new-chat',id:'chat',state:'accepted',messageId:'accepted-chat'});
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  f.runtime.pendingDeliveries=0;
  await f.router.reconcile();assert.equal(f.task.status,'running');
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'original-work',id:'refusal',state:'accepted',messageId:'accepted-refusal'});
  await f.router.reconcile();
  assert.equal(f.task.status,'canceled');
  assert.equal(f.router.state.inputs['new-chat'].state,'accepted');
});

test('a new chat reply cannot stand in for a refusal that was never delivered',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  await f.router.dispatch({id:'new-chat',kind:'owner',text:'chat'},async (_decision,markSubmitted)=>{markSubmitted();return 'new-turn';});
  await f.router.observe('prompt-start',{taskId:f.task.id,inputVersion:1,turnFence:0});
  await f.router.observe('delivery',{taskId:f.task.id,inputVersion:1,turnFence:0,sourceInputId:'new-chat',id:'chat',state:'accepted',messageId:'accepted-chat'});
  await f.router.observe('prompt-end',{taskId:f.task.id,inputVersion:1,turnFence:0,stopReason:'end_turn'});
  await f.router.reconcile();
  assert.equal(f.task.status,'running');
  assert.equal(f.task.completion.outcome,'declined');
  assert.equal(f.router.state.inputs['new-chat'].state,'accepted');
});

test('a later owner mode request keeps its flags when new work invalidates an old decline',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  await f.router.requestMode({mode:'auto',commandId:'later-auto',reason:'owner requested automatic routing'});
  assert.equal(f.router.state.requests[f.command.commandId].state,'superseded');
  await f.router.dispatch({id:'more-work',kind:'owner',text:'work'},async()=> 'new-turn');
  assert.equal(f.router.state.requests['later-auto'].state,'pending');
  assert.equal(f.router.state.requestedMode,'auto');
  assert.equal(f.router.state.exitRequested,true);
});

test('an arriving chat stays an independent input while a declined work turn waits',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  const result=await f.router.dispatch({id:'new-chat',kind:'owner',text:'chat'},async()=> 'new-turn');
  assert.equal(result.route,'new-turn');assert.equal(f.router.state.inputs['new-chat'].state,'accepted');
  assert.equal(f.task.inputVersion,1);assert.deepEqual(f.task.contextInputIds,['new-chat']);
  assert.equal(f.task.status,'running');
});

test('existing work review waits for host decline settlement without declaring completion',async t=>{
  const f=await fixture(t);await f.router.requestMode(f.command);
  let reviews=0;
  const review=new WorkLockReview({router:f.router,file:path.join(path.dirname(f.options.file),'review.json'),
    now:f.now,collect:async()=>assert.fail('decline does not need a semantic work verdict'),
    review:async()=>{reviews++;throw Error('unexpected semantic review');}});
  const result=await review.tick();
  assert.equal(result.state,'waiting');assert.equal(result.reason,'assistant-decline-awaiting-host-settlement');
  assert.equal(reviews,0);assert.equal(f.task.status,'running');
});
