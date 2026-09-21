import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {WorkLockReview,mergeDecisions} from '../../adapters/work-lock-review.mjs';

async function fixture(t,{disposition='resume',waiting=false}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-work-chat-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=10000,reviewCalls=0;
  const runtime={known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'gpt-5.6-sol',modelProvider:'custom-gateway',reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on',
    providerOverride:false,active:false,queued:0,backgroundTasks:0,pendingDeliveries:0};
  const router=new MobileRouter({file:path.join(root,'router.json'),sessionId:'synthetic',now:()=>++now,
    inspect:async()=>({...runtime}),classify:async({text})=>({route:text==='work'?'work':'chat',reason:'synthetic'}),
    switchModel:async()=>assert.fail('chat must preserve work model'),waitForIdle:async()=>assert.fail('unexpected wait')});
  await router.dispatch({id:'original',kind:'owner',text:'work'},async()=> 'new-turn');
  await router.observe('prompt-start');runtime.active=true;runtime.nativeStatus='active';
  let dispatch;
  await router.dispatch({id:'chat',kind:'owner',text:'想你啦'},async d=>{dispatch=d;return 'steered';});
  runtime.active=false;runtime.nativeStatus='idle';await router.observe('prompt-end',{stopReason:'end_turn'});now+=3000;
  const task=router.currentTask();
  const args={router,file:path.join(root,'review.json'),now:()=>++now,
    collect:async()=>({input:{inputs:[{id:'original',text:'work'},{id:'chat',text:'想你啦'}],outputs:[],waiting},receipts:{}}),
    review:async input=>{reviewCalls++;return {decision:{disposition,reason:'synthetic semantic decision',evidenceIds:['original','chat'],remaining:['完成原作品'],discardDraftIds:[]},
      receipt:{provider:'deepseek',model:'deepseek-flash',reasoning:'high',requestId:'isolated-review'}};}};
  const review=new WorkLockReview(args);
  return {router,task,runtime,review,args,dispatch,reviewCalls:()=>reviewCalls};
}

test('chat preserves semantic intent and work version, then one existing review resumes original task',async t=>{
  const f=await fixture(t);
  assert.equal(f.dispatch.intent,'chat');assert.equal(f.dispatch.taskId,f.task.id);
  assert.deepEqual(f.task.inputIds,['original']);assert.deepEqual(f.task.contextInputIds,['chat']);
  assert.equal(f.task.inputVersion,1);
  const result=await f.review.tick();assert.equal(result.state,'applied');
  assert.deepEqual(f.task.handoff,{id:'work-chat:chat',state:'pending',text:'刚才的插话已经接过。继续原任务尚未完成的部分：完成原作品。先使用已有进度、文件与工具结果，已发送内容不重复发送；如有新的暂停或取消要求，按新要求处理。'});
  assert.equal(f.router.tasks().length,1);assert.equal(f.task.status,'running');assert.equal(f.reviewCalls(),1);
  const restarted=new WorkLockReview(f.args);await restarted.tick();assert.equal(f.reviewCalls(),1,'pending handoff never requests another decision');
});

test('accepted continuation and later turn completion cannot continue the same chat again',async t=>{
  const f=await fixture(t);await f.review.tick();f.task.handoff.state='accepted';
  await f.router.dispatch({id:'handoff:work-chat:chat',kind:'handoff',text:'continue'},async()=> 'new-turn');
  const previous=f.reviewCalls();
  // The model cannot request resume without a new chat candidate.
  await f.router.observe('prompt-start');await f.router.observe('prompt-end',{stopReason:'end_turn'});
  const result=await f.review.tick();assert.notEqual(result.decision?.disposition,'resume');assert.equal(f.reviewCalls(),previous);
});

test('semantic waiting keeps work pending without a handoff',async t=>{
  const f=await fixture(t,{disposition:'keep',waiting:true});
  assert.equal((await f.review.tick()).state,'kept');assert.equal(f.task.handoff,undefined);
});

test('active tools, new input versions and cancellation prevent a chat continuation',async t=>{
  for(const block of ['tool','cancel','active']) {
    const f=await fixture(t);
    if(block==='tool')f.task.tools.pending={status:'in_progress',inputVersion:1,turnFence:0};
    if(block==='cancel')f.task.cancelRequested=true;
    if(block==='active')f.runtime.active=true;
    assert.equal((await f.review.tick()).state,'waiting');assert.equal(f.task.handoff,undefined);assert.equal(f.reviewCalls(),0);
  }
  const f=await fixture(t);const original=f.args.review;
  f.review.review=async input=>{const r=await original(input);f.task.inputVersion++;return r;};
  assert.equal((await f.review.tick()).state,'superseded');assert.equal(f.task.handoff,undefined);
});

test('chunked work reviews retain waiting over resume and resume over completion',()=>{
  const d=disposition=>({disposition,reason:disposition,evidenceIds:['original'],remaining:[],discardDraftIds:[]});
  assert.equal(mergeDecisions([d('resume'),d('keep')]).disposition,'keep');
  assert.equal(mergeDecisions([d('complete'),d('resume')]).disposition,'resume');
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


test('a changed delivery receipt prevents resuming on a stale work review',async t=>{
  const f=await fixture(t);let reads=0;
  f.review.collect=async()=>({input:{inputs:[{id:'original'},{id:'chat'}]},receipts:{delivery:{state:++reads===1?'accepted':'unconfirmed'}}});
  const result=await f.review.tick();
  assert.equal(result.state,'superseded');assert.equal(result.reason,'evidence-changed-during-review');
  assert.equal(f.task.handoff,undefined);
});
