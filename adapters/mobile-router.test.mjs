import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,modeCommand,recentConversation,attachmentMetadata,CLASSIFIER_DECISION,NOTICE_SEND_BUDGET,NOTICE_LOOKUP_BUDGET,noticeReceiptClass} from './mobile-router.mjs';
import {compactPrompt,publicMobileRuntime} from './mobile-controls.mjs';

function fixture(t, options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-routing-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=100,classificationCalls=0;
  const runtime={known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'gpt-5.6-sol',modelProvider:'custom-gateway',providerOverride:false,reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0};
  const switched=[];
  const args={file:path.join(root,'state.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),
    classify:async({text})=>{classificationCalls++;const control={'现在是什么模型':'status','切过去给我说一声哦😯':'watch','退出正经模式':'auto','进入正经模式':'work'}[text];return control?{route:'control',control,reason:'synthetic semantic result'}:{route:/code|test/.test(text)?'work':'chat',reason:'synthetic'};},
    switchModel:async(model,profile={model})=>{switched.push(model);const target={reasoningEffort:model==='deepseek-flash'?'high':'medium',serviceTierPreference:model==='deepseek-flash'?'default':'fast',...profile};Object.assign(runtime,{model,reasoningEffort:target.reasoningEffort,serviceTierPreference:target.serviceTierPreference,fastMode:target.serviceTierPreference==='fast'?'on':'off'});return{...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},now:()=>++now,...options};
  const router=new MobileRouter(args);
  return{router,runtime,switched,args,classificationCalls:()=>classificationCalls};
}

const LIVE_MODELS=[
  {id:'deepseek-flash',provider:'openai-15m',providerKind:'gateway',reasoningEfforts:['high'],defaultReasoningEffort:'high',serviceTiers:['default'],defaultServiceTier:'default'},
  {id:'gpt-5.6-sol',provider:'custom-gateway',providerKind:'native',reasoningEfforts:['low','medium','high','xhigh','max'],defaultReasoningEffort:'medium',serviceTiers:[{id:'priority'}],defaultServiceTier:'priority'},
  {id:'gpt-6-astra',aliases:['ASTRA-6'],provider:'custom-gateway',providerKind:'native',reasoningEfforts:['medium','high'],defaultReasoningEffort:'medium',serviceTiers:['default',{id:'priority'}],defaultServiceTier:'default'},
];
function profileFixture(t,{initial={model:'deepseek-flash',provider:'openai-15m',providerKind:'gateway',reasoningEffort:'high',serviceTierPreference:'default'},classify=null,forceSwitch=null}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-profile-routing-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=1000;
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0,
    model:initial.model,modelProvider:initial.provider,providerOverride:initial.providerKind==='gateway',reasoningEffort:initial.reasoningEffort,serviceTierPreference:initial.serviceTierPreference,fastMode:initial.serviceTierPreference==='fast'?'on':'off',serviceTier:null,serviceTierVerified:false};
  const switches=[],classifications=[];
  const args={file:path.join(root,'state.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),modelCatalog:async()=>structuredClone(LIVE_MODELS),
    classify:async input=>{classifications.push(input);return classify?classify(input):{route:'chat',reason:'casual',recall:{mode:'light',query:input.text,reason:'current message'}};},
    switchModel:async(model,profile,context)=>{switches.push({model,profile:structuredClone(profile),context:structuredClone(context)});Object.assign(runtime,{model,modelProvider:profile.provider,providerOverride:profile.providerKind==='gateway',reasoningEffort:profile.reasoningEffort,serviceTierPreference:profile.serviceTierPreference,fastMode:profile.serviceTierPreference==='fast'?'on':'off'});return{...runtime};},
    forceSwitch,waitForIdle:async()=>{throw Error('must not wait');},now:()=>++now};
  return {router:new MobileRouter(args),runtime,args,switches,classifications,now:()=>++now};
}

test('automatic changes notify once per verified transition, including a delayed intermediate model',async t=>{
 const f=fixture(t);f.runtime.model='deepseek-flash';
 await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
 assert.equal(Object.values(f.router.state.notices).filter(n=>n.kind==='model-switched').length,1);
 const first=Object.values(f.router.state.notices)[0];assert.equal(first.target,'gpt-5.6-sol');
 f.runtime.model='deepseek-flash';await f.router.readRuntime();
 const sent=[];
 await f.router.flushNotices({send:async r=>{sent.push(r);return {state:'accepted',messageId:r.id};},lookup:async()=>null});
 assert.equal(sent.length,2);assert.match(sent[0].text,/此前已切换到 GPT/);assert.match(sent[0].text,/现在用的是 DeepSeek/);
 assert.equal(f.router.lastToldModel(),'deepseek-flash','what the owner was told is the model the delivered message named as current');
 assert.equal(f.router.state.recent.some(r=>r.text===sent[0].text),false);
 const restarted=new MobileRouter(f.args);
 await restarted.flushNotices({send:async r=>{sent.push(r);},lookup:async()=>null});assert.equal(sent.length,2);
});

test('explicit switch confirmation shares the automatic transition notice',async t=>{
 const f=fixture(t);
 await f.router.requestMode({commandId:'auto-owner',mode:'auto',notify:true,reason:'owner preference'});
 await f.router.applyPendingMode();
 assert.equal(Object.values(f.router.state.notices).length,1);
 await f.router.prepareModel('deepseek-flash');assert.equal(Object.values(f.router.state.notices).length,1);
});

test('casual and unknown follow-ups preserve completion while execution remains GPT',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'delivered',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  const completion=structuredClone(task.completion),version=task.inputVersion;
  await f.router.dispatch({id:'chat',text:'haha'},async()=> 'steered');
  // The classifier goes down: the follow-up waits as itself — no submission, no
  // task change, no model change (KIN-ITER-20260918-02).
  f.router.classify=async()=>{throw Error('classification timeout');};
  await assert.rejects(f.router.dispatch({id:'unknown',text:'How is the switch?'},async()=>assert.fail('must not submit')),/waiting/);
  assert.equal(task.inputVersion,version);assert.deepEqual(task.completion,completion);
  assert.equal(f.runtime.model,'gpt-5.6-sol');assert.deepEqual(task.contextInputIds,['chat']);
  assert.equal(f.router.state.inputs.unknown.state,'semantic-pending');
  // The late answer says chat: it rides as context on the existing task, and the
  // execution model stays GPT.
  f.router.classify=async()=>({route:'chat',reason:'a question about the switch'});
  f.router.state.semanticPending.unknown.nextAttemptAt=0;
  await f.router.reviewSemanticPending();
  assert.equal(f.router.state.inputs.unknown.state,'selected');
  await f.router.dispatch({id:'unknown',text:'How is the switch?'},async d=>{assert.equal(d.model,'gpt-5.6-sol');return'steered';});
  assert.equal(task.inputVersion,version);assert.deepEqual(task.completion,completion);
  assert.equal(f.runtime.model,'gpt-5.6-sol');assert.deepEqual(task.contextInputIds,['chat','unknown']);
});

test('pre-submit errors can retry once, post-submit ambiguity never replays',async t=>{
  const f=fixture(t);const input={id:'one',text:'write code',submissionProtocol:'host-boundary-v1'};
  await assert.rejects(()=>f.router.dispatch(input,async()=>{throw Error('optional restore failed');}),/before native/);
  assert.equal(f.router.state.inputs.one.state,'failed-before-submit');
  let calls=0;
  await f.router.dispatch(input,async(_,started)=>{started();calls++;return 'new-turn';});
  await f.router.dispatch(input,async()=>assert.fail('duplicate'));
  assert.equal(calls,1);
  await assert.rejects(()=>f.router.dispatch({...input,id:'two'},async(_,started)=>{started();throw Error('lost receipt');}),/reconciliation/);
  await assert.rejects(()=>f.router.dispatch({...input,id:'two'},async()=>assert.fail('ambiguous retry')),/reconciliation/);
});

test('internal completion preserves an owner switch notification across restart',async t=>{
  const f=fixture(t);
  await f.router.requestMode({commandId:'owner-request',mode:'auto',reason:'Switch and tell me',notify:true});
  await f.router.requestMode({commandId:'host-finished',mode:'auto',reason:'Work verified',notify:false});
  const restored=new MobileRouter(f.args);
  await restored.applyPendingMode();
  let sends=0;
  await restored.flushNotices({send:async()=>{sends++;return{state:'accepted',messageId:'notice'};},lookup:async()=>null});
  await restored.flushNotices({send:async()=>assert.fail('replayed'),lookup:async()=>null});
  assert.equal(sends,1);assert.equal(f.runtime.model,'deepseek-flash');assert.equal(restored.sessionId,'synthetic');
});

test('internal outreach defers for work and never switches an active provider',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const before=structuredClone(f.router.currentTask());
  const result=await f.router.dispatch({id:'thought',kind:'proactive',text:'A funny thought'},()=>assert.fail('must not submit'));
  assert.equal(result.route,'deferred');assert.deepEqual(f.router.currentTask(),before);assert.deepEqual(f.switched,[]);
  assert.equal(f.router.snapshot().inputs.thought,undefined);
});

test('an idle spontaneous thought is generated by verified DeepSeek without a work task',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'thought',kind:'proactive',text:'An idle thought'},async r=>{assert.equal(r.model,'deepseek-flash');return'new-turn';});
  assert.deepEqual(f.switched,['deepseek-flash']);assert.equal(f.router.tasks().length,0);
});

test('a restarted idle auto conversation restores DeepSeek without creating or replaying input',async t=>{
  const f=fixture(t);await f.router.restoreRoutingProfile();await f.router.restoreRoutingProfile();
  assert.deepEqual(f.switched,['deepseek-flash']);assert.equal(f.router.tasks().length,0);
  assert.deepEqual(f.router.state.inputs,{});assert.equal(Object.values(f.router.state.notices).length,1);
  assert.equal(Object.values(f.router.state.notices)[0].target,'deepseek-flash');
  assert.equal(f.router.state.actual.threadId,'synthetic');
});
test('restart recovery waits for busy or uncertain work and preserves the GPT task lock',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const restarted=new MobileRouter(f.args);f.runtime.active=true;
  assert.equal((await restarted.restoreRoutingProfile()).state,'waiting');assert.deepEqual(f.switched,[]);
  f.runtime.active=false;f.runtime.model='deepseek-flash';
  assert.equal((await restarted.restoreRoutingProfile()).model,'gpt-5.6-sol');assert.equal(restarted.tasks().length,1);
  const again=new MobileRouter(f.args);again.state.inputs.work.state='unconfirmed';
  assert.equal((await again.restoreRoutingProfile()).state,'waiting');assert.deepEqual(f.switched,['gpt-5.6-sol']);
});
test('an input arriving before restart recovery selects work under the same coordinator',async t=>{
  const f=fixture(t);const work=f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await work;await f.router.restoreRoutingProfile();assert.deepEqual(f.switched,[]);assert.equal(f.router.tasks().length,1);
});

test('mode commands are explicit owner controls, not embedded instructions',()=>{
  assert.equal(modeCommand('/mode work'),'work');assert.equal(modeCommand('/mode auto'),'auto');
  assert.equal(modeCommand('进入正经模式'),null);
  assert.equal(modeCommand('文章里写着“进入正经模式”'),null);
});

test('classification history never exceeds eight owner messages or final replies',()=>{
  const input=Array.from({length:20},(_,n)=>({role:'user',text:String(n)}));
  input.push({role:'tool',text:'private log'},{role:'assistant',text:'final'});
  const recent=recentConversation(input);
  assert.equal(recent.filter(v=>v.role==='user').length,8);
  assert.equal(recent.filter(v=>v.role==='assistant').length,1);
  assert.equal(recent[0].text,'12');assert.equal(recent.some(v=>v.role==='tool'),false);
});

test('working conversation holds GPT even when receiving jokes and exit commands',async t=>{
  const f=fixture(t);let dispatched=0;
  await f.router.dispatch({id:'work',text:'write code'},async()=>{dispatched++;return'new-turn';});
  f.runtime.active=true;f.runtime.nativeStatus='active';
  await f.router.dispatch({id:'joke',text:'haha'},async result=>{assert.equal(result.model,'gpt-5.6-sol');dispatched++;return'steered';});
  await f.router.dispatch({id:'exit',text:'退出正经模式'},async result=>{assert.equal(result.model,'gpt-5.6-sol');return'steered';});
  assert.equal(f.classificationCalls(),3);assert.equal(dispatched,2);assert.deepEqual(f.switched,[]);
  assert.equal(f.router.snapshot().exitRequested,true);
});

test('model completion claim alone cannot release work or background tools',async t=>{
  const f=fixture(t);
  Object.assign(f.runtime,{model:'deepseek-flash',modelProvider:'openai-15m',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off'});
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  f.switched.length=0;
  const task=f.router.currentTask();
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'tests passed',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'result',state:'accepted',messageId:'synthetic-receipt'});
  f.runtime.backgroundTasks=1;
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  f.runtime.backgroundTasks=0;
  await f.router.reconcile();assert.equal(f.router.tasks().length,0);
  await f.router.dispatch({id:'chat',text:'hello'},async result=>{assert.equal(result.model,'deepseek-flash');return'new-turn';});
  assert.deepEqual(f.switched,['deepseek-flash']);
});

test('unconfirmed delivery and unknown runtime hold the provider after restart',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'result ready',completedTaskId:f.router.currentTask().id,completedInputVersion:f.router.currentTask().inputVersion});
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'result',state:'unconfirmed'});
  const restored=new MobileRouter(f.args);await restored.reconcile();assert.equal(restored.tasks().length,1);
  await restored.dispatch({id:'chat',text:'hello'},async r=>{assert.equal(r.model,'gpt-5.6-sol');return'new-turn';});
  assert.equal(f.switched.length,0);
  f.runtime.known=false;await restored.reconcile();assert.equal(restored.tasks().length,1);
});

test('desktop handoff and its pending result keep work locked after the native turn ends',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'desktop result pending',completedTaskId:f.router.currentTask().id,completedInputVersion:f.router.currentTask().inputVersion});
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'ack',state:'accepted',messageId:'ack-id'});
  f.runtime.handoffTasks=1;
  const restored=new MobileRouter(f.args);await restored.reconcile();
  assert.equal(restored.tasks().length,1);await restored.applyPendingMode();
  assert.equal(f.switched.length,0);
  f.runtime.handoffTasks=0;await restored.reconcile();
  assert.equal(restored.tasks().length,0);
});

test('duplicate input never resubmits or reclassifies; uncertain submission is held',async t=>{
  const f=fixture(t);let count=0;const input={id:'one',text:'hello'};
  await f.router.dispatch(input,async()=>{count++;return'new-turn';});
  await f.router.dispatch(input,async()=>{count++;return'new-turn';});
  assert.equal(count,1);assert.equal(f.classificationCalls(),1);
  await assert.rejects(f.router.dispatch({id:'two',text:'hi'},async()=>{throw Error('lost response');}),/reconciliation/);
  await assert.rejects(f.router.dispatch({id:'two',text:'hi'},async()=>{count++;}),/reconciliation/);
  assert.equal(count,1);
});

test('new input invalidates an earlier completion proposal',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'done',completedTaskId:f.router.currentTask().id,completedInputVersion:f.router.currentTask().inputVersion});
  await f.router.dispatch({id:'addition',text:'also add a test'},async()=> 'steered');
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'result',state:'accepted',messageId:'one'});
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
});

test('DeepSeek work escalation waits for its turn before changing provider',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';f.runtime.active=true;f.runtime.nativeStatus='active';
  await assert.rejects(f.router.dispatch({id:'work',text:'write code'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.deepEqual(f.switched,[]);assert.equal(f.router.currentTask().status,'running');
  f.runtime.active=false;f.runtime.nativeStatus='idle';
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  assert.deepEqual(f.switched,['gpt-5.6-sol']);
});

test('a classification failure never manufactures work: the input waits as itself, bounded and visible',async t=>{
  // KIN-ITER-20260918-02: this REVISES the old "classifier timeout fails over to
  // work" semantics. A failed classification creates no task, no GPT switch and no
  // execution grant; the input waits as itself and the failure class is recorded.
  let calls=0;
  const f=fixture(t,{classify:async()=>{calls++;throw Error('classification-timeout');}});
  await assert.rejects(f.router.dispatch({id:'question',text:'unclear'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.equal(f.router.tasks().length,0,'no task is manufactured out of a failure');
  assert.deepEqual(f.switched,[],'and no provider switch');
  const record=f.router.state.inputs.question;
  assert.equal(record.state,'semantic-pending');assert.equal(record.reason,'classification-timeout');
  const entry=f.router.state.semanticPending.question;
  assert.deepEqual([entry.failure.class,entry.attempts,entry.maxAttempts,entry.model],['timeout',2,4,'gpt-5.6-sol']);
  assert.equal(calls,2,'the live dispatch drove the first bounded retry of the same input');
  // The budget is visible and bounded; after it, an explicit failed state.
  for(const _ of [1,2]){f.router.state.semanticPending.question.nextAttemptAt=0;await f.router.reviewSemanticPending();}
  assert.equal(calls,4);
  assert.equal(f.router.state.inputs.question.state,'semantic-failed');
  assert.equal(f.router.state.inputs.question.reason,'classification-exhausted');
  assert.equal(f.router.state.semanticPending.question.state,'failed');
  assert.equal(f.router.tasks().length,0);assert.deepEqual(f.switched,[]);
  // A redrive never executes it and never asks again: it reports the explicit state.
  const outcome=await f.router.dispatch({id:'question',text:'unclear'},async()=>assert.fail('never submitted'));
  assert.equal(outcome.route,'semantic-failed');
  assert.equal(calls,4);
});

test('an idle chat whose classification failed is answered late as chat — never as work',async t=>{
  let failures=2;
  const f=fixture(t,{classify:async()=>{if(failures-->0)throw Error('classification-timeout');return{route:'chat',reason:'late answer'};}});
  await assert.rejects(f.router.dispatch({id:'question',text:'unclear'},async()=>{throw Error('not yet');}),/waiting/);
  assert.equal(f.router.tasks().length,0);assert.deepEqual(f.switched,[]);
  f.router.state.semanticPending.question.nextAttemptAt=0;
  await f.router.reviewSemanticPending();
  assert.deepEqual([f.router.state.inputs.question.state,f.router.state.inputs.question.route],['selected','chat']);
  await f.router.dispatch({id:'question',text:'unclear'},async d=>{assert.equal(d.model,'deepseek-flash');return'new-turn';});
  assert.deepEqual(f.switched,['deepseek-flash'],'chat goes to DeepSeek once known — GPT was never touched');
  assert.equal(f.router.tasks().length,0);
  assert.equal(f.router.state.semanticPending.question.state,'classified');
});

test('a restart with a pending classification keeps the wait durable, bounded and honest',async t=>{
  let calls=0;
  const f=fixture(t,{classify:async()=>{calls++;throw Error('deepseek-http-503');}});
  await assert.rejects(f.router.dispatch({id:'question',text:'unclear'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.equal(f.router.state.semanticPending.question.failure.class,'http');
  // An attempt in flight when the host dies is retried, never concluded.
  f.router.state.semanticPending.question.state='classifying';f.router.save('synthetic-crash');
  const restarted=new MobileRouter(f.args);
  assert.equal(restarted.state.inputs.question.state,'semantic-pending','the wait survives the restart');
  assert.equal(restarted.state.semanticPending.question.state,'retry');
  assert.equal(restarted.tasks().length,0);assert.deepEqual(f.switched,[]);
  // The pump-driven review resumes the SAME input; a late chat answer executes as chat.
  restarted.classify=async()=>({route:'chat',reason:'answered after restart'});
  restarted.state.semanticPending.question.nextAttemptAt=0;
  await restarted.reviewSemanticPending();
  assert.equal(restarted.state.inputs.question.state,'selected');
  await restarted.dispatch({id:'question',text:'unclear'},async d=>{assert.equal(d.model,'deepseek-flash');return'new-turn';});
  assert.equal(restarted.tasks().length,0);
  assert.deepEqual(f.switched,['deepseek-flash']);
});

test('a late classification that arrives after a stop never resurrects work',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  f.router.classify=async()=>{throw Error('classification-timeout');};
  await assert.rejects(f.router.dispatch({id:'more',text:'also add tests'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.equal(f.router.state.inputs.more.state,'semantic-pending');
  // The owner stops the task while the answer is in flight.
  await f.router.dispatch({id:'stop',text:'停止任务'},async()=> 'steered');
  await f.router.reconcile();
  assert.equal(f.router.tasks().length,0);
  // The late answer says "work": it creates nothing and extends nothing.
  f.router.classify=async()=>({route:'work',reason:'late work answer'});
  f.router.state.semanticPending.more.nextAttemptAt=0;
  await f.router.reviewSemanticPending();
  assert.equal(f.router.state.inputs.more.state,'semantic-canceled');
  assert.equal(f.router.state.inputs.more.reason,'superseded-by-cancel');
  assert.equal(f.router.tasks().length,0);assert.deepEqual(f.switched,[]);
});

test('a duplicate of a semantically pending input neither reclassifies nor double-executes',async t=>{
  let calls=0;
  const f=fixture(t,{classify:async()=>{calls++;throw Error('classification-timeout');}});
  await assert.rejects(f.router.dispatch({id:'same',text:'unclear'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.equal(calls,2);
  await assert.rejects(f.router.dispatch({id:'same',text:'unclear'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.equal(calls,2,'a duplicate waits on the same entry; the input is not asked again');
  assert.equal(Object.keys(f.router.state.semanticPending).length,1);
  assert.equal(f.router.tasks().length,0);
});

test('explicit control commands keep their path while the classifier is down',async t=>{
  const f=fixture(t,{classify:async()=>{throw Error('classification-timeout');}});
  await f.router.dispatch({id:'enter',text:'/mode work'},async()=>{throw Error('host control');});
  await f.router.applyPendingMode();
  assert.equal(f.runtime.model,'gpt-5.6-sol');assert.equal(f.router.tasks().length,0);
  assert.equal(Object.keys(f.router.state.semanticPending).length,0,'a command never waits on semantics');
});

test('a late-classified work follow-up joins the existing task without disturbing its lock',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask(),version=task.inputVersion;
  f.router.classify=async()=>{throw Error('classification-timeout');};
  await assert.rejects(f.router.dispatch({id:'followup',text:'also handle errors'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.equal(task.inputVersion,version,'while unclassified, the completion basis is untouched');
  assert.equal(f.runtime.model,'gpt-5.6-sol');
  f.router.classify=async()=>({route:'work',reason:'more work'});
  f.router.state.semanticPending.followup.nextAttemptAt=0;
  await f.router.reviewSemanticPending();
  const record=f.router.state.inputs.followup;
  assert.deepEqual([record.state,record.route],['selected','work']);
  assert.equal(task.inputVersion,version+1,'a real follow-up invalidates the completion basis, as a fresh one would');
  await f.router.dispatch({id:'followup',text:'also handle errors'},async d=>{assert.equal(d.model,'gpt-5.6-sol');return'steered';});
  assert.equal(f.runtime.model,'gpt-5.6-sol');
  assert.deepEqual(f.switched,[],'the existing work never loses its model');
});

test('switch and input acceptance share one mutex',async t=>{
  const f=fixture(t);const releases=[];let count=0;
  f.router.switchModel=async(model,profile={model})=>{await new Promise(resolve=>{releases.push(resolve);});const target={reasoningEffort:model==='deepseek-flash'?'high':'medium',serviceTierPreference:model==='deepseek-flash'?'default':'fast',...profile};Object.assign(f.runtime,{model,reasoningEffort:target.reasoningEffort,serviceTierPreference:target.serviceTierPreference,fastMode:target.serviceTierPreference==='fast'?'on':'off'});return{...f.runtime};};
  const a=f.router.dispatch({id:'chat',text:'hi'},async()=>{count++;return'new-turn';});
  while(!releases.length)await new Promise(resolve=>setImmediate(resolve));
  const b=f.router.dispatch({id:'work',text:'write code'},async()=>{count++;return'new-turn';});
  assert.equal(count,0);releases[0]();await a;
  while(releases.length<2)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(count,1);releases[1]();await b;assert.equal(count,2);
});

test('a legacy interrupted transition stays pending without replaying uncertain input or guessing a profile',async t=>{
  const f=fixture(t);
  f.router.state.transition={state:'switching',to:'deepseek-flash'};
  f.router.state.inputs.uncertain={id:'uncertain',hash:'synthetic',state:'submitting'};
  f.router.save('synthetic-crash');
  const restored=new MobileRouter(f.args);
  await restored.reconcile();
  assert.deepEqual([restored.state.transition.state,restored.state.transition.waitingReason],['unconfirmed','legacy-profile-unavailable']);
  assert.equal(restored.state.inputs.uncertain.state,'unconfirmed');
  await assert.rejects(restored.dispatch({id:'new',text:'hello'},async()=>assert.fail('not submitted under an unknown legacy transition')),/reconciliation/);
});

test('unconfirmed recovery never switches a background task and retries at most once',async t=>{
  const f=fixture(t);f.runtime.profileReady=false;f.runtime.backgroundTasks=1;
  f.router.state.transition={state:'unconfirmed',from:'gpt-5.6-sol',to:'deepseek-flash',
    fromProfile:{model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'},targetProfile:{model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'}};
  let attempts=0;f.router.switchModel=async()=>{attempts++;throw Error('offline');};
  await f.router.reconcile();assert.equal(attempts,0);
  f.runtime.backgroundTasks=0;
  await f.router.reconcile();await f.router.reconcile();assert.equal(attempts,1);
  assert.equal(f.router.state.transition.state,'unconfirmed');
});

test('mode tool returns pending during its own turn and host applies it after tools exit',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';f.runtime.active=true;f.runtime.nativeStatus='active';
  const receipt=await f.router.requestMode({commandId:'enter',mode:'work',reason:'new idea'});
  assert.equal(receipt.state,'pending');
  await f.router.applyPendingMode();assert.equal(f.switched.length,0);
  f.runtime.active=false;f.runtime.nativeStatus='idle';
  await f.router.applyPendingMode();assert.deepEqual(f.switched,['gpt-5.6-sol']);
  assert.equal(f.router.state.requests.enter.state,'applied');
});

test('observations do not stale configuration revision and invalid completion does not mutate mode',async t=>{
  const f=fixture(t);const revision=f.router.state.configRevision;
  await f.router.observe('reply',{final:true,text:'hello'});
  await f.router.requestMode({commandId:'enter',mode:'work',reason:'owner requested',expectedRevision:revision});
  const state=f.router.snapshot();
  await assert.rejects(f.router.requestMode({commandId:'bad',mode:'auto',reason:'done',completedTaskId:'missing'}),/not open/);
  assert.equal(f.router.state.exitRequested,state.exitRequested);
  assert.equal(f.router.state.configRevision,state.configRevision);
});

test('explicit stop releases work only when native execution has stopped',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  f.runtime.active=true;f.runtime.nativeStatus='active';
  await f.router.dispatch({id:'stop',text:'停止任务'},async()=> 'steered');
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  f.runtime.active=false;f.runtime.nativeStatus='idle';
  await f.router.reconcile();assert.equal(f.router.tasks().length,0);
});

test('natural wording is judged by DeepSeek with recent context and the independent work lock',async t=>{
  const seen=[];const f=fixture(t,{classify:async input=>{seen.push(input);return{route:'control',control:'status',reason:'semantic status inquiry'};}});
  await f.router.dispatch({id:'unfamiliar',text:'咦，你的脑袋已经换回刚才那个了没？'},async()=>{throw Error('no model turn for status');});
  assert.equal(seen.length,1);assert.equal(seen[0].text,'咦，你的脑袋已经换回刚才那个了没？');
  assert.equal(seen[0].mode,'auto');assert.equal(seen[0].workHeld,false);assert.equal(f.router.tasks().length,0);
});

test('native compact is not a work task and its command survives memory enrichment',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';
  await f.router.dispatch({id:'compact',text:'/compact'},async decision=>{
    assert.equal(decision.command,'compact');assert.equal(decision.model,'deepseek-flash');
    const pending=compactPrompt({contextToken:'source',prompt:[{type:'text',text:'synthetic memory prefix'},{type:'text',text:'/compact'}]});
    assert.equal(pending.prompt[0].text,'/compact');assert.equal(pending.contextToken,'source');return'new-turn';
  });
  assert.equal(f.classificationCalls(),0);assert.equal(f.router.tasks().length,0);
  await f.router.observeOperation('compact','start');
  await assert.rejects(f.router.dispatch({id:'code',text:'write code'},async()=>{throw Error('must wait');}),/waiting/);
  assert.deepEqual(f.switched,[]);
  await f.router.observeOperation('compact','end',{stopReason:'end_turn'});
  await f.router.dispatch({id:'code',text:'write code'},async()=> 'new-turn');
  assert.deepEqual(f.switched,['gpt-5.6-sol']);
});

test('compact completion returns a standalone chat to automatic routing',async t=>{
  const f=fixture(t);Object.assign(f.runtime,{model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off'});
  await f.router.dispatch({id:'compact',text:'/compact'},async()=> 'new-turn');
  await f.router.observeOperation('compact','start');await f.router.observeOperation('compact','end',{stopReason:'end_turn'});
  const restored=new MobileRouter(f.args);
  await restored.dispatch({id:'hello',text:'haha'},async d=>{assert.equal(d.model,'deepseek-flash');return'new-turn';});
  assert.equal(restored.tasks().length,0);assert.deepEqual(f.switched,[]);
});

test('ambiguous compact after restart remains held and is never replayed',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';
  await f.router.dispatch({id:'compact',text:'/compact'},async()=> 'new-turn');
  const restored=new MobileRouter(f.args);
  assert.equal(restored.state.operations.compact.state,'unconfirmed');
  assert.equal((await restored.dispatch({id:'compact',text:'/compact'},async()=>{throw Error('replay');})).route,'deduplicated');
  await assert.rejects(restored.prepareModel('gpt-5.6-sol'),/Work prevents/);
});

test('state queries and switch watches bypass classification without invalidating work',async t=>{
  const f=fixture(t);Object.assign(f.runtime,{model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off'});
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');f.switched.length=0;
  const task=f.router.currentTask(),version=task.inputVersion;
  await f.router.requestMode({commandId:'done',mode:'auto',reason:'validated',completedTaskId:task.id,completedInputVersion:version});
  f.runtime.active=true;f.runtime.nativeStatus='active';f.runtime.backgroundTasks=1;
  for(const [id,text] of [['status','现在是什么模型'],['watch','切过去给我说一声哦😯'],['exit','退出正经模式']]) {
    assert.equal((await f.router.dispatch({id,text},async()=>{throw Error('runtime controls do not prompt a model');})).route,'host-control');
  }
  assert.equal(task.inputVersion,version);assert.ok(task.completion);assert.equal(f.classificationCalls(),4);
  await f.router.applyPendingMode();assert.deepEqual(f.switched,[]);
  const sent=[];await f.router.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'receipt-'+n.id};},lookup:async()=>null});
  assert.ok(sent.length);assert.equal(Object.keys(task.deliveries).length,0);
  f.runtime.active=false;f.runtime.nativeStatus='idle';f.runtime.backgroundTasks=0;
  await f.router.observe('prompt-end',{stopReason:'end_turn'});await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  await f.router.observe('delivery',{id:'real-result',state:'accepted',messageId:'result'});
  await f.router.reconcile();await f.router.applyPendingMode();assert.deepEqual(f.switched,['deepseek-flash']);
});

test('completion proposals cannot acknowledge an older task input version',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask(),oldVersion=task.inputVersion;
  await f.router.dispatch({id:'change',text:'include more tests'},async()=> 'steered');
  await assert.rejects(f.router.requestMode({commandId:'stale',mode:'auto',reason:'done',completedTaskId:task.id,completedInputVersion:oldVersion}),/version changed/);
  assert.equal(task.completion,undefined);
});

test('switch confirmation is sent once only after native verification, with stable receipts',async t=>{
  const f=fixture(t);f.runtime.active=true;f.runtime.nativeStatus='active';
  await f.router.requestMode({commandId:'exit',mode:'auto',reason:'owner wants chat',notify:true});
  await f.router.applyPendingMode();assert.equal(Object.keys(f.router.state.notices).length,0);
  f.runtime.active=false;f.runtime.nativeStatus='idle';await f.router.applyPendingMode();
  const sent=[];const channel={send:async n=>{assert.equal(f.runtime.model,'deepseek-flash');sent.push(n);return{state:'accepted',messageId:'platform-id'};},lookup:async()=>null};
  await f.router.flushNotices(channel);await f.router.flushNotices(channel);
  await new MobileRouter(f.args).flushNotices(channel);
  assert.equal(sent.length,1);assert.match(sent[0].text,/DeepSeek Flash/);
  assert.equal(Object.values(f.router.state.notices)[0].messageId,'platform-id');
});

test('uncertain notification reconciles its original ID without another send',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{throw Error('no model');});
  let sends=0;
  await f.router.flushNotices({send:async()=>{sends++;return{state:'accepted'};},lookup:async()=>({state:'unconfirmed'})});
  const id=Object.keys(f.router.state.notices)[0];assert.equal(f.router.state.notices[id].state,'unconfirmed');
  const restored=new MobileRouter(f.args);restored.state.notices[id].nextAttemptAt=0;
  await restored.flushNotices({send:async()=>{sends++;throw Error('must not replay');},lookup:async requested=>{assert.equal(requested,id);return{state:'accepted',messageId:'reconciled'};}});
  assert.equal(sends,1);assert.equal(restored.state.notices[id].messageId,'reconciled');
});

test('known pre-send failure retries the same notification ID',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const ids=[];const channel={send:async n=>{ids.push(n.id);if(ids.length===1)throw Error('not submitted');return{state:'accepted',messageId:'id'};},lookup:async()=>({state:'not-started'})};
  await f.router.flushNotices(channel);Object.values(f.router.state.notices)[0].nextAttemptAt=0;await f.router.flushNotices(channel);
  assert.equal(ids.length,2);assert.equal(ids[0],ids[1]);
});

test('concurrent notice drains do not send twice or mark an in-flight send retryable',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  let release,sends=0;const send=async()=>{sends++;await new Promise(r=>{release=r;});return{state:'accepted',messageId:'one'};};
  const first=f.router.flushNotices({send,lookup:async()=>({state:'not-started'})});
  while(!release)await new Promise(r=>setImmediate(r));
  await f.router.flushNotices({send,lookup:async()=>({state:'not-started'})});release();await first;
  assert.equal(sends,1);
});

// ---- KIN-ITER-20260918-03: proven-not-submitted vs submission-unknown ----

test('the receipt vocabulary reads exactly three ways: accepted, rejected, not-submitted, else unknown',()=>{
  assert.equal(noticeReceiptClass({state:'accepted',messageId:'om_1'}),'accepted');
  assert.equal(noticeReceiptClass({state:'accepted'}),'unknown','accepted without a message id is not believed');
  assert.equal(noticeReceiptClass({state:'rejected'}),'rejected');
  assert.equal(noticeReceiptClass({state:'not-started'}),'not-submitted','no outbox record: the send never began');
  assert.equal(noticeReceiptClass({state:'not-submitted'}),'not-submitted');
  assert.equal(noticeReceiptClass({state:'pending',submissionStarted:false}),'not-submitted','a receipt written before the network proves nothing was submitted');
  assert.equal(noticeReceiptClass({state:'pending',submissionStarted:true}),'unknown');
  assert.equal(noticeReceiptClass({state:'unconfirmed'}),'unknown');
  assert.equal(noticeReceiptClass({state:'failed'}),'unknown');
  assert.equal(noticeReceiptClass(null),'unknown');
  assert.equal(noticeReceiptClass({}),'unknown');
});

test('a crash mid-send leaves id and stage durable, and the restart only ever looks up the original id',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  let release;
  const pending=f.router.flushNotices({send:async()=>{await new Promise(r=>{release=r;});return{state:'accepted',messageId:'m'};},lookup:async()=>null});
  while(!release)await new Promise(r=>setImmediate(r));
  const crashed=new MobileRouter(f.args);
  const id=Object.keys(crashed.state.notices)[0];
  assert.deepEqual([crashed.state.notices[id].state,crashed.state.notices[id].stage],['unconfirmed','sending'],
    'a send in flight when the host died comes back as lookup-only, stage intact');
  crashed.state.notices[id].nextAttemptAt=0;
  let sends=0;
  await crashed.flushNotices({send:async()=>{sends++;throw Error('must not resend the ambiguous one');},lookup:async()=>({state:'accepted',messageId:'om_late'})});
  assert.equal(sends,0);
  assert.deepEqual([crashed.state.notices[id].state,crashed.state.notices[id].messageId],['accepted','om_late']);
  release();await pending;
});

test('a failed notice preparation leaves the notice resumable, with its stage recorded',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const id=Object.keys(f.router.state.notices)[0];
  f.router.inspect=async()=>{throw Error('inspect down');};
  await assert.rejects(f.router.flushNotices({send:async()=>assert.fail('not reached'),lookup:async()=>null}),/inspect down/);
  assert.deepEqual([f.router.state.notices[id].state,f.router.state.notices[id].stage],['pending','preparing']);
  f.router.inspect=f.args.inspect;
  const sent=[];
  await f.router.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'m'};},lookup:async()=>null});
  assert.equal(sent.length,1);assert.equal(f.router.state.notices[id].state,'accepted');
});

test('a notice proven not submitted is resent under its own id, beyond the old cap, bounded and visible',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const id=Object.keys(f.router.state.notices)[0];
  let sends=0;
  const channel={send:async()=>{sends++;throw Error('network down before submit');},lookup:async()=>({state:'not-started'})};
  for(let round=0;round<NOTICE_SEND_BUDGET;round++){await f.router.flushNotices(channel);f.router.state.notices[id].nextAttemptAt=0;}
  assert.equal(sends,NOTICE_SEND_BUDGET,'the same id is retried while the ground truth is not-submitted');
  const n=f.router.state.notices[id];
  assert.equal(n.state,'failed','exhaustion is a visible state, not permanent polling');
  assert.equal(n.waitingReason,'send-attempts-exhausted');assert.equal(n.nextAction,'none');
  assert.equal(n.firstFailure.class,'send-not-submitted');
  const before=f.router.state.history.length;
  await f.router.flushNotices(channel);
  assert.equal(sends,NOTICE_SEND_BUDGET,'terminal: no further sends');
  assert.equal(f.router.state.history.length,before,'and nothing more is written');
});

test('a submission whose outcome is unknown is only ever looked up, then stops visibly',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const id=Object.keys(f.router.state.notices)[0];
  let sends=0;
  const channel={send:async()=>{sends++;throw Error('timeout after submit');},lookup:async()=>({state:'unconfirmed',stage:'message-unconfirmed',submissionStarted:true})};
  await f.router.flushNotices(channel);
  assert.equal(sends,1);assert.equal(f.router.state.notices[id].state,'unconfirmed');
  assert.equal(f.router.state.notices[id].nextAction,'lookup-only');
  for(let n=0;n<NOTICE_LOOKUP_BUDGET;n++){f.router.state.notices[id].nextAttemptAt=0;await f.router.flushNotices(channel);}
  assert.equal(sends,1,'an ambiguous submission is never resent');
  assert.equal(f.router.state.notices[id].state,'unconfirmed');
  f.router.state.notices[id].nextAttemptAt=0;await f.router.flushNotices(channel);
  assert.equal(f.router.state.notices[id].state,'unresolved');
  assert.equal(f.router.state.notices[id].waitingReason,'receipt-lookup-exhausted');
  assert.equal(f.router.state.notices[id].nextAction,'none');
});

test('an accepted answer without a message id is not believed; the outbox decides',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const id=Object.keys(f.router.state.notices)[0];
  let sends=0;
  await f.router.flushNotices({send:async()=>{sends++;return{state:'accepted'};},lookup:async()=>({state:'not-started'})});
  assert.equal(f.router.state.notices[id].state,'retry','no message id, no acceptance — and the outbox proves nothing was submitted');
  f.router.state.notices[id].nextAttemptAt=0;
  await f.router.flushNotices({send:async n=>{sends++;assert.equal(n.id,id);return{state:'accepted',messageId:'om_ok'};},lookup:async()=>assert.fail('settled by the send')});
  assert.equal(sends,2);assert.equal(f.router.state.notices[id].state,'accepted');
});

test('a rejected notice is terminal: never resent, never polled',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const id=Object.keys(f.router.state.notices)[0];
  let sends=0,lookups=0;
  await f.router.flushNotices({send:async()=>{sends++;throw Error('platform refused');},lookup:async()=>{lookups++;return{state:'rejected',stage:'platform-rejected'};}});
  assert.equal(f.router.state.notices[id].state,'rejected');
  assert.equal(f.router.state.notices[id].waitingReason,'platform-rejected');
  f.router.state.notices[id].nextAttemptAt=0;
  await f.router.flushNotices({send:async()=>{sends++;},lookup:async()=>{lookups++;return null;}});
  assert.deepEqual([sends,lookups],[1,1],'terminal: no resend, no re-check');
});

test('an unconfirmed notice whose record proves never-submitted is resumed under the same id',async t=>{
  // The production stuck notices: unconfirmed, attempts past the old cap, outbox
  // file missing. Ground truth wins: not-submitted, so the SAME id is sent again.
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const id=Object.keys(f.router.state.notices)[0];
  let sends=0;
  await f.router.flushNotices({send:async()=>{sends++;throw Error('lost');},lookup:async()=>({state:'unconfirmed'})});
  f.router.state.notices[id].attempts=3;f.router.state.notices[id].nextAttemptAt=0;
  await f.router.flushNotices({send:async()=>{sends++;throw Error('must not resend while unknown');},lookup:async()=>({state:'not-started'})});
  assert.equal(sends,1);assert.equal(f.router.state.notices[id].state,'retry','not-submitted ground truth resumes the send, past the old cap');
  assert.equal(f.router.state.notices[id].nextAction,'resend-same-id');
  f.router.state.notices[id].nextAttemptAt=0;
  await f.router.flushNotices({send:async n=>{sends++;assert.equal(n.id,id);return{state:'accepted',messageId:'om_settled'};},lookup:async()=>null});
  assert.deepEqual([f.router.state.notices[id].state,f.router.state.notices[id].messageId],['accepted','om_settled']);
  assert.equal(sends,2);
});

test('a settle that changes nothing writes nothing',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  await f.router.flushNotices({send:async()=>({state:'accepted',messageId:'m'}),lookup:async()=>null});
  const before=f.router.state.history.filter(e=>e.kind==='notice-settled').length;
  await f.router.flushNotices({send:async()=>assert.fail('terminal'),lookup:async()=>assert.fail('terminal')});
  assert.equal(f.router.state.history.filter(e=>e.kind==='notice-settled').length,before);
});

test('a stale mode confirmation is superseded and never sent',async t=>{
  const f=fixture(t);Object.assign(f.runtime,{model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off'});
  await f.router.requestMode({commandId:'exit',mode:'auto',reason:'owner wants chat',notify:true});
  await f.router.applyPendingMode();
  const stale=Object.values(f.router.state.notices).find(n=>n.kind==='mode-applied');
  assert.ok(stale);assert.equal(stale.state,'pending');
  // The model moved on before the confirmation was ever sent.
  await f.router.requestMode({commandId:'back',mode:'work',reason:'owner wants work'});
  await f.router.applyPendingMode();
  assert.equal(f.runtime.model,'gpt-5.6-sol');
  const sent=[];
  await f.router.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'m-'+n.id};},lookup:async()=>null});
  assert.equal(f.router.state.notices[stale.id].state,'superseded');
  assert.equal(sent.some(n=>n.id===stale.id),false,'the stale confirmation was never sent');
});

// ---- KIN-ITER-20260918-03: what became of every transition's notification ----

test('a rebind with no model change is recorded as such, and generates no notice',async t=>{
  const f=fixture(t);
  f.router.state.transition={state:'unconfirmed',from:'gpt-5.6-sol',to:'deepseek-flash'};
  f.router.save('synthetic-crash');
  const restored=new MobileRouter(f.args);
  await restored.reconcile();
  assert.equal(Object.keys(restored.state.notices).length,0);
  assert.deepEqual(restored.state.transition.notification,{state:'not-generated',reason:'rebind-no-model-change'});
});

test('every real switch records what became of its owner notification',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const notice=Object.values(f.router.state.notices).find(n=>n.kind==='model-switched');
  assert.equal(notice.state,'pending');
  assert.deepEqual(f.router.state.transition.notification,{state:'queued',noticeId:notice.id});
});

test('a suppressed restart notice is accounted as not sent, with its cause',async t=>{
  const f=fixture(t);
  await told(f,'deepseek-flash');
  f.runtime.model='gpt-5.6-sol';                     // the native runtime came back on its own default
  const restarted=new MobileRouter(f.args);
  await restarted.restoreRoutingProfile();
  const notice=Object.values(restarted.state.notices).find(n=>n.kind==='model-switched');
  assert.equal(notice.state,'suppressed');
  assert.deepEqual(restarted.state.transition.notification,{state:'suppressed',reason:'restart-restored-known-model',noticeId:notice.id});
  assert.equal(restarted.lastToldModel(),'deepseek-flash','a suppressed notice never moves what the owner was told');
  assert.equal(notice.acceptedAt,undefined);assert.equal(notice.messageId,undefined,'never delivered, never counted');
});

test('historical transitions are distinguished from current runtime and every host switch is recorded',async t=>{
  const f=fixture(t);f.router.state.transition={state:'applied',from:'gpt-5.6-sol',to:'deepseek-flash'};
  const before=publicMobileRuntime(f.router.state,f.runtime,'synthetic');
  assert.equal(before.actual.model,'gpt-5.6-sol');assert.equal(before.lastTransition.matchesCurrentModel,false);
  await f.router.prepareModel('deepseek-flash');
  const view=await f.router.readRuntime();assert.equal(view.actual.model,'deepseek-flash');
  assert.equal(view.lastTransition.source,'probe');assert.equal(view.lastTransition.matchesCurrentModel,true);
  f.runtime.model='gpt-5.6-sol';const changed=await f.router.readRuntime();
  assert.equal(changed.lastTransition.source,'runtime-observation');assert.equal(changed.lastTransition.to,'gpt-5.6-sol');
});

test('chat in an explicitly selected work mode does not invent a work task',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'enter',text:'进入正经模式'},async()=>{throw Error('host control');});
  await f.router.applyPendingMode();
  await f.router.dispatch({id:'joke',text:'haha'},async d=>{assert.equal(d.model,'gpt-5.6-sol');return'new-turn';});
  assert.equal(f.router.state.mode,'work');assert.equal(f.router.tasks().length,0);
  await f.router.dispatch({id:'exit',text:'退出正经模式'},async()=>{throw Error('host control');});
  await f.router.applyPendingMode();assert.equal(f.runtime.model,'deepseek-flash');
});

test('a notification in flight holds provider changes until its send settles',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  let release;const sending=f.router.flushNotices({send:async()=>{await new Promise(r=>{release=r;});return{state:'accepted',messageId:'notice'};},lookup:async()=>null});
  while(!release)await new Promise(r=>setImmediate(r));
  await f.router.requestMode({commandId:'exit',mode:'auto',reason:'owner exits'});
  await f.router.applyPendingMode();assert.deepEqual(f.switched,[]);
  release();await sending;await f.router.applyPendingMode();assert.deepEqual(f.switched,['deepseek-flash']);
});

// ---- durable state file: one damaged revision never costs the conversation ----

test('a damaged state file falls back to the revision the writer kept, and the bytes are quarantined',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const file=f.args.file,directory=path.dirname(file);
  assert.equal(fs.existsSync(file+'.prev'),true,'the writer keeps the revision it replaces');
  fs.writeFileSync(file,'{"schema":1,"tasks"');
  const restored=new MobileRouter(f.args);
  assert.equal(restored.tasks().length,1,'the open task is still there');
  assert.equal(restored.state.recovery.reason,'restored-from-previous-revision');
  assert.equal(restored.state.recovery.from,'previous');
  const kept=fs.readdirSync(path.join(directory,'quarantine'));
  assert.deepEqual([kept.length,restored.state.recovery.quarantined],[1,kept]);
  assert.deepEqual((await restored.readRuntime()).recovery,restored.state.recovery,'and status can show it');
  assert.equal(fs.readFileSync(path.join(directory,'quarantine',kept[0]),'utf8'),'{"schema":1,"tasks"','the damaged bytes are kept, never deleted');
  assert.deepEqual(new MobileRouter(f.args).state.recovery,restored.state.recovery,'the fact stays readable after a healthy restart');
});

test('with no readable revision left the router does not invent a fresh history',async t=>{
  const f=fixture(t);Object.assign(f.runtime,{model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off'});
  await f.router.dispatch({id:'one',text:'hello'},async()=> 'new-turn');
  const file=f.args.file;
  fs.writeFileSync(file,'not json');fs.writeFileSync(file+'.prev','[');
  const restored=new MobileRouter(f.args);
  assert.equal(restored.state.recovery.reason,'state-file-unreadable');
  assert.equal(restored.state.recovery.quarantined.length,2);
  assert.equal(restored.state.recovery.restoredInputs,1);
  assert.equal(restored.state.inputs.one.state,'unconfirmed','what was accepted comes back from the journal');
  await assert.rejects(restored.dispatch({id:'one',text:'hello'},async()=>assert.fail('replayed')),/reconciliation/);
  assert.equal((await restored.restoreRoutingProfile()).state,'waiting','no provider is switched under work nobody can see');
  assert.deepEqual(f.switched,[]);
  const untouched=new MobileRouter({...f.args,file:path.join(path.dirname(file),'never-written.json')});
  assert.equal(untouched.state.recovery,undefined,'a missing file is still a fresh start');
});

test('two writers never share a temporary name, and an interrupted write leaves the file loadable',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const second=new MobileRouter(f.args),temporaries=[],rename=fs.renameSync;
  fs.renameSync=(from,to)=>{temporaries.push(from);return rename(from,to);};
  try {f.router.save('one');second.save('two');} finally {fs.renameSync=rename;}
  assert.ok(temporaries.length>=4);
  assert.equal(new Set(temporaries).size,temporaries.length,'same process, same second, different names');
  fs.renameSync=(from,to)=>{if(to===f.args.file)throw Object.assign(Error('interrupted'),{code:'EIO'});return rename(from,to);};
  try {assert.throws(()=>f.router.save('interrupted'));} finally {fs.renameSync=rename;}
  fs.writeFileSync(f.args.file+'.999.abcdef.tmp','half a revision');
  const after=new MobileRouter(f.args);
  assert.equal(after.tasks().length,1);assert.equal(after.state.recovery,undefined);
  assert.deepEqual(fs.readdirSync(path.dirname(f.args.file)).filter(n=>n.endsWith('.tmp')),['state.json.999.abcdef.tmp'],'a write cleans up after itself');
});

// ---- a restart that changes nothing the owner can see says nothing ----

async function told(f,model) {
  f.runtime.model=model;
  await f.router.dispatch({id:'told-'+model,text:'现在是什么模型'},async()=>{});
  await f.router.flushNotices({send:async()=>({state:'accepted',messageId:'told-'+model}),lookup:async()=>null});
  assert.equal(f.router.lastToldModel(),model);
}

test('a restart that restores the model the owner was last told about settles without a message',async t=>{
  const f=fixture(t);
  await told(f,'deepseek-flash');
  f.runtime.model='gpt-5.6-sol';                     // the native runtime came back on its own default
  const restarted=new MobileRouter(f.args);
  assert.equal((await restarted.restoreRoutingProfile()).model,'deepseek-flash');
  const notice=Object.values(restarted.state.notices).find(n=>n.kind==='model-switched');
  assert.deepEqual([notice.state,notice.reason,notice.text],['suppressed','restart-restored-known-model',undefined]);
  await restarted.flushNotices({send:async()=>assert.fail('a suppressed notice is never sent'),lookup:async()=>assert.fail('and never reconciled')});
  const view=await restarted.readRuntime();
  assert.ok(view.notifications.some(n=>n.state==='suppressed'&&n.reason==='restart-restored-known-model'),'still visible in status');
  await restarted.dispatch({id:'ask',text:'现在是什么模型'},async()=>{});
  const sent=[];
  await restarted.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'answer'};},lookup:async()=>null});
  assert.equal(sent.length,1);assert.match(sent[0].text,/DeepSeek Flash/,'an explicit question is still answered');
});

test('a restart that restores a different model tells the owner, exactly as before',async t=>{
  const f=fixture(t);
  await told(f,'deepseek-flash');
  // The mode moved on without the owner hearing about it, so the restored model is news.
  await f.router.requestMode({commandId:'host-work',mode:'work',reason:'Verified work is under way',notify:false});
  await f.router.applyPendingMode();
  const restarted=new MobileRouter(f.args);
  assert.equal((await restarted.restoreRoutingProfile()).model,'gpt-5.6-sol');
  const notice=Object.values(restarted.state.notices).find(n=>n.kind==='model-switched'&&n.target==='gpt-5.6-sol');
  assert.equal(notice.state,'pending');assert.match(notice.text,/已切换到 GPT/);
  const sent=[];
  await restarted.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'switched'};},lookup:async()=>null});
  await restarted.flushNotices({send:async()=>assert.fail('sent twice'),lookup:async()=>null});
  assert.equal(sent.length,1);
});

test('a restart in the middle of a real switch still tells the owner once',async t=>{
  const f=fixture(t);
  await told(f,'deepseek-flash');
  f.router.state.transition={id:'switch-synthetic',state:'switching',from:'deepseek-flash',to:'gpt-5.6-sol',source:'input',sourceId:'told-deepseek-flash'};
  f.router.save('synthetic-crash');
  f.runtime.model='gpt-5.6-sol';
  const restarted=new MobileRouter(f.args);
  assert.equal(restarted.state.transition.state,'unconfirmed');
  await restarted.reconcile();
  const switches=Object.values(restarted.state.notices).filter(n=>n.kind==='model-switched');
  assert.equal(switches.length,1);assert.equal(switches[0].state,'pending');
  const sent=[];
  await restarted.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'switched'};},lookup:async()=>null});
  await restarted.flushNotices({send:async()=>assert.fail('sent twice'),lookup:async()=>null});
  assert.equal(sent.length,1);assert.equal(restarted.lastToldModel(),'gpt-5.6-sol');
});

test('a legacy transition with neither live endpoint stays pending instead of inventing a previous profile',async t=>{
  const f=fixture(t);f.runtime.model='gpt-6-astra';
  f.router.state.transition={id:'legacy-switch',state:'switching',from:'deepseek-flash',to:'gpt-5.6-sol',source:'input',sourceId:'old-input'};
  f.router.save('legacy-crash');
  const restarted=new MobileRouter(f.args);await restarted.reconcile();
  assert.deepEqual([restarted.state.transition.state,restarted.state.transition.waitingReason,f.runtime.model],['unconfirmed','legacy-profile-unavailable','gpt-6-astra']);
  assert.deepEqual(f.switched,[],'no default effort, provider or service tier is guessed for the legacy profile');
});

// ---- reply tail port: the unsent rest of an interrupted reply rides on the routing call ----
function tailPort(answers={}) {
  const calls=[];
  const method=name=>async detail=>{calls.push([name,detail]);if(answers[name] instanceof Error)throw answers[name];return typeof answers[name]==='function'?answers[name](detail):answers[name]??null;};
  return {calls,pending:method('pending'),decided:method('decided'),missed:method('missed'),stopped:method('stopped')};
}
const synthetic={reason:'new-owner-input',sent:[{text:'The first bubble.',receipt:{messageId:'om_1',acceptedAt:null}}],unconfirmed:[],unsent:[{text:'The second bubble.'}],decisions:['continue','rewrite_remainder','supersede']};

test('with no interrupted reply the classifier is asked exactly what it was always asked',async t=>{
  const seen=[],classify=async input=>{seen.push(input);return {route:'chat',reason:'synthetic',tail:{decision:'supersede',reason:'nobody asked'}};};
  const port=tailPort(),plain=fixture(t,{classify}),ported=fixture(t,{classify,replyTail:port});
  const before=await plain.router.select({id:'one',text:'hello'}),after=await ported.router.select({id:'one',text:'hello'});
  assert.deepEqual(Object.keys(seen[1]),['text','clock','recent','task','mode','workHeld','availableModels','timeoutMs']);
  assert.deepEqual(Object.keys(seen[1]),Object.keys(seen[0]));
  assert.deepEqual(JSON.stringify({...seen[1],clock:null}),JSON.stringify({...seen[0],clock:null}));
  assert.deepEqual(port.calls,[['pending',{id:'one',text:'hello'}]],'the port hears about the message and is asked nothing else');
  assert.deepEqual([after.tail,before.tail,Object.keys(after)],[undefined,undefined,Object.keys(before)],'an unsolicited tail is never acted on');
});

test('an interrupted reply rides on the routing call, and what DeepSeek decided reaches the tail port with the input that caused it',async t=>{
  const seen=[],port=tailPort({pending:{key:'group-a:0123456789abcdef',reply:synthetic},decided:{state:'recorded',groupId:'group-a',intentId:'tail-1'}});
  const f=fixture(t,{replyTail:port,classify:async input=>{seen.push(input);return {route:'chat',reason:'synthetic',tail:{decision:'rewrite_remainder',reason:'Fold it in',receipt:{requestId:'request-1'}}};}});
  const record=await f.router.select({id:'two',text:'wait, one more thing'});
  assert.deepEqual(seen[0].interruptedReply,synthetic);assert.equal(Object.keys(seen[0]).at(-1),'interruptedReply');
  assert.deepEqual(port.calls.map(c=>c[0]),['pending','decided']);
  assert.deepEqual(port.calls[1][1],{inputId:'two',key:'group-a:0123456789abcdef',tail:{decision:'rewrite_remainder',reason:'Fold it in',receipt:{requestId:'request-1'}}});
  assert.deepEqual([record.route,record.tail],['chat',{carrier:'classify',decision:'rewrite_remainder',state:'recorded',groupId:'group-a',intentId:'tail-1'}]);
  assert.deepEqual(f.router.state.inputs.two.tail,record.tail,'the routing record says which decision rode on it');
  await f.router.select({id:'two',text:'wait, one more thing'});
  assert.equal(port.calls.length,2,'a duplicate input asks nobody again');
});

test('a classifier that times out, or answers without a tail, leaves the remainder to its next carrier',async t=>{
  const offer={key:'group-a:0123456789abcdef',reply:synthetic};
  const timeout=tailPort({pending:offer,missed:detail=>({state:'missed',reason:detail.reason})});
  const f=fixture(t,{replyTail:timeout,classify:async()=>{throw Error('classification-timeout');}});
  const record=await f.router.select({id:'three',text:'unclear'});
  // KIN-ITER-20260918-02: a failed classification routes nothing; the input waits as itself.
  assert.deepEqual([record.state,record.route,record.reason,record.tail],['semantic-pending',null,'classification-timeout',{carrier:'classify',state:'missed',reason:'classifier-unconfirmed'}]);
  assert.deepEqual(timeout.calls.at(-1),['missed',{inputId:'three',key:offer.key,reason:'classifier-unconfirmed'}]);
  const silent=tailPort({pending:offer});
  const g=fixture(t,{replyTail:silent,classify:async()=>({route:'chat',reason:'synthetic'})});
  assert.deepEqual((await g.router.select({id:'four',text:'haha'})).tail,{carrier:'classify',state:'missed'});
  assert.deepEqual(silent.calls.at(-1),['missed',{inputId:'four',key:offer.key,reason:'no-tail-decision'}]);
});

// ---- classify intents: stop, file sending, owner words and attachment metadata ----
const intentClassifier=answer=>{const seen=[];return {seen,classify:async input=>{seen.push(input);
  return {route:/code|test/.test(input.text)?'work':'chat',reason:'synthetic',recall:{mode:'light',query:'q',reason:'r'},...(typeof answer==='function'?answer(input):answer)};}};};

test('with intents off an attachment is still work, and the classifier is asked exactly what it always was',async t=>{
  const {seen,classify}=intentClassifier({stop:'current_task',file_send:{requested:true,channel:'wechat',file_ref:'r'}});
  const f=fixture(t,{classify});
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const record=await f.router.select({id:'file',text:'look at this',attachments:[{kind:'image',name:'photo.jpg'}]});
  assert.deepEqual([record.route,record.reason,record.fileSend,record.stop],['work','work-lock: work-input',undefined,undefined]);
  await f.router.select({id:'plain',text:'hello'});
  assert.deepEqual(Object.keys(seen[0]),['text','clock','recent','task','mode','workHeld','availableModels','timeoutMs'],'only the live model catalog is added; no intent was asked for');
  assert.equal(f.router.currentTask().cancelRequested,undefined,'an intent nobody asked for cannot stop a task');
});

test('with intents on an attachment is classified, and only its metadata is ever sent',async t=>{
  const {seen,classify}=intentClassifier({route:'chat',reason:'a photo to look at'});
  const f=fixture(t,{classify,classifyIntents:true});
  const record=await f.router.select({id:'file',text:'look at this',attachments:[
    {kind:'image',mimeType:'image/jpeg',name:'photo.jpg',bytes:12345,path:'/private/inbox/photo.jpg',data:'BASE64',encrypt_query_param:'secret'},
    {name:'note.txt',bytes:-1,kind:'unheard-of'}]});
  assert.deepEqual([record.route,record.reason],['chat','a photo to look at'],'the model decides chat or work with the attachments in view');
  assert.deepEqual(seen[0].attachments,[{kind:'image',mimeType:'image/jpeg',name:'photo.jpg',bytes:12345},{kind:'other',name:'note.txt'}]);
  const request=JSON.stringify(seen[0]);
  for(const leaked of ['path','data','encrypt_query_param','/private/inbox','BASE64'])assert.ok(!request.includes(leaked),'no path and no content ever reaches the classifier: '+leaked);
  assert.equal(seen[0].intents,true);
  // Metadata only, whatever the caller passes: the request is built from four fields.
  assert.deepEqual(attachmentMetadata([{kind:'file',mimeType:'x/y'.padEnd(200,'z'),name:'a'.repeat(200),bytes:7,url:'https://example.test/x'}]),
    [{kind:'file',mimeType:('x/y'.padEnd(200,'z')).slice(0,100),name:'a'.repeat(120),bytes:7}]);
  assert.deepEqual(attachmentMetadata(null),[]);
  assert.equal(attachmentMetadata(Array.from({length:40},()=>({kind:'file'}))).length,24);
});

test('a natural-language stop is the literal command only for the owner with a task open, and only when the classifier answered',async t=>{
  const {seen,classify}=intentClassifier({stop:'current_task'});
  const f=fixture(t,{classify,classifyIntents:true});
  const idle=await f.router.select({id:'idle',text:'stop what you are doing'});
  assert.deepEqual([idle.route,idle.stop],['chat',undefined],'with no task open there is nothing to stop');
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  const stopped=await f.router.select({id:'stop-it',text:'ok, that is enough for now'});
  assert.deepEqual(stopped.stop,{requested:'current_task',decisionSource:CLASSIFIER_DECISION,taskIds:[task.id]});
  assert.equal(task.cancelRequested,true,'the same flag the literal command sets');
  assert.equal(f.router.state.inputs['stop-it'].stop.decisionSource,'deepseek-input-classification');
  await f.router.reconcile();
  assert.equal(f.router.state.tasks[task.id].status,'canceled');
  // Nobody but the owner is asked for intents, so nobody but the owner can stop a task.
  const g=fixture(t,{classify,classifyIntents:true});
  await g.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const other=await g.router.select({id:'other',kind:'notice',text:'stop the task'});
  assert.deepEqual([other.stop,seen.at(-1).intents],[undefined,undefined]);
  assert.equal(g.router.currentTask().cancelRequested,undefined);
});

test('a literal stop still needs no model, and an unavailable classifier cannot stop anything',async t=>{
  const f=fixture(t,{classifyIntents:true,classify:async input=>{
    if(input.text==='write code')return{route:'work',reason:'synthetic'};
    throw Error('the classifier is never asked for a literal stop');}});
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  const before=f.classificationCalls();
  const stop=await f.router.select({id:'stop',text:'停止任务'});
  assert.deepEqual([stop.reason,task.cancelRequested,f.classificationCalls()],['owner-stop-command',true,before]);
  // The same words in ordinary language, with no classifier to read them: nothing
  // happens — the input waits as itself and the running task keeps its model.
  const g=fixture(t,{classifyIntents:true,classify:async input=>{
    if(input.text==='write code')return{route:'work',reason:'synthetic'};
    throw Error('classification-timeout');}});
  await g.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const unread=await g.router.select({id:'unread',text:'ok, that is enough for now'});
  assert.deepEqual([unread.state,unread.route,unread.reason,unread.stop],['semantic-pending',null,'classification-timeout',undefined]);
  assert.equal(g.router.currentTask().cancelRequested,undefined);
  assert.equal(g.router.currentTask().status,'running');assert.equal(g.runtime.model,'gpt-5.6-sol');
});

test('a file the owner asked for is recorded as states and IDs; a malformed intent is dropped without failing the route',async t=>{
  const sha='b'.repeat(64);
  let answer={file_send:{requested:true,channel:'wechat',file_ref:'report.pdf',candidate_sha256:sha},recall:{mode:'deep',query:'q',reason:'r',owner_words:['captain','kin']}};
  const {classify}=intentClassifier(()=>answer);
  const f=fixture(t,{classify,classifyIntents:true});
  const asked=await f.router.select({id:'send-it',text:'send me the report on WeChat'});
  const {at,...recorded}=asked.fileSend;
  assert.deepEqual(recorded,{requested:true,channel:'wechat',fileRef:'report.pdf',candidateSha256:sha,decisionSource:CLASSIFIER_DECISION});
  assert.equal(typeof at,'number');
  assert.deepEqual(f.router.state.inputs['send-it'].fileSend,asked.fileSend);
  assert.deepEqual(asked.recall.owner_words,['captain','kin']);
  assert.equal(asked.recall.decisionSource,CLASSIFIER_DECISION);
  answer={file_send:{requested:true,channel:'carrier-pigeon',file_ref:'x'.repeat(400)},recall:{mode:'light',query:'q',reason:'r',owner_words:Array.from({length:40},()=>'w'.repeat(200))}};
  const dropped=await f.router.select({id:'nonsense',text:'anything'});
  assert.deepEqual([dropped.route,dropped.fileSend,dropped.recall.owner_words],['chat',undefined,undefined],'the route stands; the unusable intent is gone');
  assert.ok(!JSON.stringify(f.router.state.inputs.nonsense).includes('w'.repeat(30)));
});

test('a failed classification is asked again of the same input, with a longer wait — and nothing more',async t=>{
  // KIN-ITER-20260918-02: the retry is uniform and bounded; no trigger-word
  // heuristic gates it, and a retry by itself never grants anything.
  const waits=[];let fail=true;
  const f=fixture(t,{classifyIntents:true,
    classify:async input=>{waits.push(input.timeoutMs);if(fail){fail=false;throw Error('classification-timeout');}
      return {route:'chat',reason:'a file the owner asked for',recall:{mode:'light',query:'q',reason:'r'},file_send:{requested:true,channel:'wechat',file_ref:'report.pdf'}};}});
  await f.router.dispatch({id:'ask',text:'send me that file'},async d=>{assert.equal(d.model,'deepseek-flash');return'new-turn';});
  assert.deepEqual(waits,[15000,30000],'twice, the second time with a longer wait');
  assert.equal(f.router.state.history.some(e=>e.kind==='semantic-retry'&&e.id==='ask'),true);
  const asked=f.router.state.inputs.ask;
  assert.deepEqual([asked.state,asked.route,asked.fileSend.channel],['accepted','chat','wechat']);
  assert.equal(f.router.state.semanticPending.ask.state,'classified');
  assert.equal(f.router.tasks().length,0,'a chat answer never invents work');
  // A message that keeps failing is never routed by the failure itself.
  f.router.classify=async input=>{waits.push(input.timeoutMs);throw Error('classification-timeout');};
  await assert.rejects(f.router.dispatch({id:'plain',text:'hello there'},async()=>{throw Error('must not submit');}),/waiting/);
  const plain=f.router.state.inputs.plain;
  assert.deepEqual([plain.state,plain.route,plain.reason],['semantic-pending',null,'classification-timeout']);
  assert.equal(plain.fileSend,undefined,'a retry can never grant anything');
  assert.equal(f.router.tasks().length,0);
  assert.equal(f.runtime.model,'deepseek-flash','and the settled chat model is not disturbed');
});

test('attachments and commands are never classified; a literal stop needs no model; other inputs never touch the tail port',async t=>{
  const port=tailPort({pending:{key:'group-a:0123456789abcdef',reply:synthetic},stopped:{state:'recorded'}}),f=fixture(t,{replyTail:port});
  await f.router.select({id:'file',text:'look at this',attachments:[{name:'photo'}]});
  await f.router.select({id:'mode',text:'/mode work'});
  await f.router.select({id:'compact',text:'/compact'});
  const stop=await f.router.select({id:'stop',text:'停止任务'});
  assert.deepEqual(port.calls,[['missed',{inputId:'file',reason:'not-classified'}],['missed',{inputId:'mode',reason:'not-classified'}],['missed',{inputId:'compact',reason:'not-classified'}],['stopped',{inputId:'stop'}]]);
  assert.deepEqual([f.classificationCalls(),stop.reason,stop.tail],[0,'owner-stop-command',{carrier:'owner-stop',state:'recorded'}]);
  for(const input of [{id:'thought',kind:'proactive',text:'An idle thought'},{id:'handoff:1',kind:'handoff',text:'continue'},{id:'result',kind:'work-result',text:'停止任务'}])await f.router.select(input);
  assert.equal(port.calls.length,4);
});

test('a tail port that fails never decides routing',async t=>{
  const broken=Error('tail unavailable'),port=tailPort({pending:broken,missed:broken,stopped:broken,decided:broken});
  const f=fixture(t,{replyTail:port});
  assert.deepEqual([(await f.router.select({id:'one',text:'hello'})).route,(await f.router.select({id:'stop',text:'/停'})).reason],['chat','owner-stop-command']);
  await f.router.dispatch({id:'two',text:'write code'},async()=> 'new-turn');
  assert.equal(f.router.currentTask().status,'running');
});

test('lightweight web-and-image entertainment stays a DeepSeek chat by semantic decision',async t=>{
  const f=profileFixture(t,{classify:async input=>{
    assert.match(input.text,/猫猫 meme/);return {route:'chat',reason:'small playful outcome',recall:{mode:'light',query:input.text,reason:'current request'}};
  }});
  let submitted;
  const result=await f.router.dispatch({id:'meme',text:'帮我去网上找一个猫猫 meme，再发表情包给我'},async detail=>{submitted=detail;return'new-turn';});
  assert.deepEqual([result.route,result.model,f.router.tasks().length],['new-turn','deepseek-flash',0]);
  assert.deepEqual(submitted.profile,{provider:'openai-15m',providerKind:'gateway',model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'});
  assert.equal(f.switches.length,0,'web and image capability alone never escalates the route');
});

test('automatic work and creative production use Sol medium with a Fast preference, never Astra by default',async t=>{
  const f=profileFixture(t,{classify:async input=>({route:'work',reason:'substantive creative product',recall:{mode:'light',query:input.text,reason:'current request'}})});
  let submitted;
  await f.router.dispatch({id:'story',text:'写一部长篇互动故事并整理设定集'},async detail=>{submitted=detail;return'new-turn';});
  assert.deepEqual(submitted.profile,{provider:'custom-gateway',providerKind:'native',model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'});
  assert.deepEqual(f.switches.map(call=>call.model),['gpt-5.6-sol']);
  const view=await f.router.readRuntime();
  assert.deepEqual([view.actual.serviceTierPreference,view.actual.serviceTier,view.actual.serviceTierVerified],['fast',null,false],'Fast is a configured preference, not a fabricated actual response tier');
});

test('automatic work waits when the previous full provider or service-tier preference is unknown',async t=>{
  const f=profileFixture(t,{classify:async input=>({route:'work',reason:'substantive work',recall:{mode:'light',query:input.text,reason:'current request'}})});
  delete f.runtime.modelProvider;delete f.runtime.providerOverride;delete f.runtime.serviceTierPreference;delete f.runtime.fastMode;
  await assert.rejects(f.router.dispatch({id:'unknown-return',text:'写代码'},async()=>assert.fail('work cannot start without a restorable previous profile')),/Automatic return profile unverified/);
  assert.equal(f.switches.length,0);assert.equal(f.router.state.autoReturnProfile,undefined);
});

test('a natural manual Astra profile is catalog-validated, announced once and persists through work completion',async t=>{
  const f=profileFixture(t,{classify:async input=>input.text.includes('ASTRA-6')
    ?{route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},force:true,reason:'explicit owner profile',recall:{mode:'light',query:input.text,reason:'current control'}}
    :{route:'work',reason:'substantive work',recall:{mode:'light',query:input.text,reason:'current request'}}});
  const control=await f.router.dispatch({id:'astra',text:'我要切换到ASTRA-6 medium'},async()=>assert.fail('host control is not a native prompt'));
  assert.deepEqual([control.route,control.state,f.router.state.mode,f.runtime.model],['host-control','applied','manual','gpt-6-astra']);
  assert.deepEqual(f.router.state.manualProfile,{provider:'custom-gateway',providerKind:'native',model:'gpt-6-astra',reasoningEffort:'medium',serviceTier:null,serviceTierVerified:false,serviceTierPreference:'default'});
  const sent=[];
  await f.router.flushNotices({send:async notice=>{sent.push(notice);return{state:'accepted',messageId:'manual-ok'};},lookup:async()=>null});
  assert.deepEqual(sent.map(notice=>notice.text),['已切换到 GPT‑6 Astra（手动模式）。']);
  let work;
  await f.router.dispatch({id:'work-after-manual',text:'现在写代码并交付'},async detail=>{work=detail;return'new-turn';});
  assert.equal(work.model,'gpt-6-astra');assert.equal(f.switches.length,1,'automatic work routing cannot override the manual pin');
  const task=f.router.currentTask();
  task.completion={inputVersion:task.inputVersion,at:f.now(),summary:'done'};
  await f.router.observe('prompt-end',{taskId:task.id,stopReason:'end_turn',turnFence:task.executionEpoch});
  await f.router.observe('delivery',{taskId:task.id,id:'manual-delivery',state:'accepted',messageId:'m-manual',inputVersion:task.inputVersion,turnFence:task.executionEpoch});
  await f.router.reconcile();
  assert.deepEqual([f.router.tasks().length,f.router.state.mode,f.runtime.model],[0,'manual','gpt-6-astra']);
  assert.equal(Object.values(f.router.state.requests).some(request=>request.state==='pending'&&request.mode==='auto'),false);
});

test('unsupported manual models or efforts fail before interruption and never silently map to a nearby profile',async t=>{
  let forceCalls=0;
  const f=profileFixture(t,{classify:async input=>({route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'max',serviceTierPreference:'default'},force:true,reason:'explicit owner profile',recall:{mode:'light',query:input.text,reason:'current control'}}),forceSwitch:async()=>{forceCalls++;return{state:'idle'};}});
  f.runtime.active=true;
  const result=await f.router.dispatch({id:'bad-effort',text:'切到 ASTRA-6 max'},async()=>assert.fail('host control is not submitted'));
  assert.deepEqual([result.route,result.state,f.runtime.model,forceCalls,f.switches.length],['host-control','failed','deepseek-flash',0,0]);
  const sent=[];await f.router.flushNotices({send:async notice=>{sent.push(notice);return{state:'accepted',messageId:'unsupported'};},lookup:async()=>null});
  assert.equal(sent.length,1);assert.match(sent[0].text,/没有切换/);assert.doesNotMatch(sent[0].text,/已切换到/);
});

test('confirmed owner force bypasses stale coordinator processing, fences late output and preserves ledgers',async t=>{
  let forceCall;
  const f=profileFixture(t,{initial:{model:'gpt-5.6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'fast'},
    classify:async input=>input.text.includes('ASTRA')
      ?{route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},force:true,reason:'explicit immediate owner switch',recall:{mode:'light',query:input.text,reason:'current control'}}
      :{route:'work',reason:'substantive work',recall:{mode:'light',query:input.text,reason:'current request'}},
    forceSwitch:async request=>{forceCall=request;return{state:'interrupted',nativeTurnId:'turn-old'};}});
  await f.router.dispatch({id:'original-work',text:'继续做当前任务'},async()=> 'new-turn');
  const task=f.router.currentTask(),taskId=task.id,inputVersion=task.inputVersion;
  f.router.state.operations.old={inputId:'original-work',kind:'tool',state:'running'};
  f.router.state.notices.old={id:'old',kind:'status',state:'sending',stage:'sending'};
  f.runtime.active=true;f.runtime.backgroundTasks=1;f.runtime.pendingDeliveries=1;f.runtime.nativeStatus='idle';
  const result=await f.router.dispatch({id:'force-astra',text:'现在切到 ASTRA-6 medium'},async()=>assert.fail('control does not enter the native prompt'));
  assert.deepEqual([result.route,result.state,f.runtime.model,f.router.state.executionEpoch],['host-control','applied','gpt-6-astra',1]);
  assert.deepEqual([forceCall.fromEpoch,forceCall.toEpoch,forceCall.sourceInputId],[0,1,'force-astra']);
  assert.equal(f.switches.at(-1).context.forceBoundary.receipt.state,'interrupted','the switch receives the confirmed hot-switch proof');
  assert.deepEqual([f.router.currentTask().id,f.router.currentTask().inputVersion,f.router.currentTask().continuationRequired],[taskId,inputVersion,true]);
  assert.equal(f.router.state.operations.old.state,'fenced-unconfirmed');
  assert.equal(f.router.state.notices.old.state,'sending','the already-started send ledger is preserved, never replayed');
  await f.router.observe('prompt-end',{taskId,stopReason:'end_turn',turnFence:0,inputVersion});
  await f.router.observe('tool',{taskId,id:'old-tool',status:'completed',turnFence:0,inputVersion});
  await f.router.observe('delivery',{taskId,id:'old-delivery',state:'accepted',messageId:'already-sent',turnFence:0,inputVersion});
  assert.equal(task.stopReason,undefined,'the canceled turn cannot become a completed turn');
  assert.equal(task.deliveries['old-delivery'].lateAfterForce,true,'an already-sent platform receipt remains evidence at its old fence');
  await f.router.dispatch({id:'original-work',text:'继续做当前任务'},async()=>assert.fail('the original ID can never be resubmitted'));
  assert.equal(f.router.currentTask().id,taskId);
});

test('an idle force with only an old delivery pending preserves the completion proposal and never reruns finished work',async t=>{
  const f=profileFixture(t,{initial:{model:'gpt-5.6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'fast'},
    classify:async input=>input.text.includes('ASTRA')
      ?{route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},reason:'explicit owner switch',recall:{mode:'light',query:input.text,reason:'current control'}}
      :{route:'work',reason:'substantive work',recall:{mode:'light',query:input.text,reason:'current request'}},forceSwitch:async()=>({state:'idle'})});
  await f.router.dispatch({id:'finished-work',text:'生成猫图'},async()=> 'new-turn');
  const task=f.router.currentTask(),version=task.inputVersion;
  await f.router.observe('prompt-start',{taskId:task.id,inputVersion:version,turnFence:0});
  task.completion={inputVersion:version,turnFence:0,at:f.now(),summary:'cat image generated'};
  await f.router.observe('delivery',{taskId:task.id,id:'cat-delivery',state:'pending',inputVersion:version,turnFence:0});
  await f.router.observe('prompt-end',{taskId:task.id,stopReason:'end_turn',inputVersion:version,turnFence:0});
  f.runtime.pendingDeliveries=1;
  await f.router.dispatch({id:'idle-force',text:'切到 ASTRA-6 medium'},async()=>assert.fail('control is host-owned'));
  assert.deepEqual([f.router.state.executionEpoch,task.executionEpoch,task.continuationRequired,task.completion.state],[1,0,false,undefined]);
  assert.equal(task.completionHistory,undefined,'an idle boundary did not cancel or demote the finished turn');
  f.runtime.pendingDeliveries=0;
  await f.router.observe('delivery',{taskId:task.id,id:'cat-delivery',state:'accepted',messageId:'cat-sent',inputVersion:version,turnFence:0});
  await f.router.reconcile();
  assert.deepEqual([task.status,f.router.tasks().length,f.runtime.model],['completed',0,'gpt-6-astra']);
  assert.equal(f.switches.filter(call=>call.model==='gpt-5.6-sol').length,0,'finished work was not executed again');
});

test('an unconfirmed or failed force emits one honest control receipt and never a success claim',async t=>{
  for(const outcome of [{state:'pending'},{state:'failed',reason:'cancel rejected'}]) {
    const f=profileFixture(t,{classify:async input=>({route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},force:true,reason:'explicit immediate owner switch',recall:{mode:'light',query:input.text,reason:'current control'}}),forceSwitch:async()=>outcome});
    f.runtime.active=true;
    const result=await f.router.dispatch({id:'force-'+outcome.state,text:'现在切到 ASTRA-6 medium'},async()=>assert.fail('control is host-owned'));
    assert.equal(result.state,outcome.state==='failed'?'failed':'pending');assert.equal(f.runtime.model,'deepseek-flash');
    const sent=[];const transport={send:async notice=>{sent.push(notice);return{state:'accepted',messageId:'m-'+outcome.state};},lookup:async()=>null};
    await f.router.flushNotices(transport);await f.router.flushNotices({...transport,send:async()=>assert.fail('control receipt sent twice')});
    assert.equal(sent.length,1);assert.doesNotMatch(sent[0].text,/已切换到/);
    assert.match(sent[0].text,outcome.state==='failed'?/没有切换/:/切换正在处理/);
    assert.equal(Object.values(f.router.state.notices).some(notice=>notice.kind==='model-switched'||notice.kind==='mode-applied'),false);
  }
});

test('forced auto with live work exits a manual pin immediately, uses the work profile, then restores chat only after full settlement',async t=>{
  const f=profileFixture(t,{initial:{model:'gpt-6-astra',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'default'},
    classify:async input=>input.text.includes('自动')
      ?{route:'control',control:'auto',force:true,reason:'explicit immediate automatic mode',recall:{mode:'light',query:input.text,reason:'current control'}}
      :{route:'work',reason:'substantive work',recall:{mode:'light',query:input.text,reason:'current request'}},forceSwitch:async()=>({state:'interrupted'})});
  f.router.state.mode='manual';f.router.state.manualProfile={provider:'custom-gateway',providerKind:'native',model:'gpt-6-astra',reasoningEffort:'medium',serviceTier:null,serviceTierVerified:false,serviceTierPreference:'default'};f.router.save('synthetic-manual');
  await f.router.dispatch({id:'manual-work',text:'完成这个任务'},async()=> 'new-turn');
  const task=f.router.currentTask(),version=task.inputVersion;
  f.runtime.active=true;
  const control=await f.router.dispatch({id:'force-auto',text:'现在恢复自动模式'},async()=>assert.fail('control is host-owned'));
  assert.deepEqual([control.state,f.router.state.mode,f.runtime.model,f.router.currentTask().id],['applied','auto','gpt-5.6-sol',task.id]);
  assert.equal(f.router.state.manualProfile,undefined);assert.equal(task.continuationRequired,true);
  assert.deepEqual(f.router.state.autoReturnProfile,{provider:'openai-15m',providerKind:'gateway',model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'},'explicit auto defines the full chat profile as the eventual return profile');
  f.runtime.active=false;f.runtime.backgroundTasks=0;f.runtime.pendingDeliveries=0;
  await f.router.observe('prompt-start',{taskId:task.id,inputVersion:version,turnFence:1});
  await f.router.requestMode({commandId:'finish-after-force',mode:'auto',reason:'continued result is ready',completedTaskId:task.id,completedInputVersion:version,notify:false});
  await f.router.observe('tool',{taskId:task.id,id:'continued-tool',status:'pending',inputVersion:version,turnFence:1});
  await f.router.observe('delivery',{taskId:task.id,id:'continued-delivery',state:'pending',inputVersion:version,turnFence:1});
  await f.router.observe('prompt-end',{taskId:task.id,stopReason:'end_turn',inputVersion:version,turnFence:1});
  assert.equal((await f.router.reconcile()).state,'work-held');assert.equal(f.runtime.model,'gpt-5.6-sol');
  await f.router.observe('tool',{taskId:task.id,id:'continued-tool',status:'completed',inputVersion:version,turnFence:1});
  assert.equal((await f.router.reconcile()).state,'work-held','tool settlement alone cannot release the task');
  await f.router.observe('delivery',{taskId:task.id,id:'continued-delivery',state:'accepted',messageId:'delivered',inputVersion:version,turnFence:1});
  assert.equal((await f.router.reconcile()).state,'idle');assert.equal(f.runtime.model,'gpt-5.6-sol','completion records the restore request but does not claim it already happened');
  await f.router.applyPendingMode();
  assert.deepEqual([f.runtime.model,f.router.state.mode,f.router.tasks().length],['deepseek-flash','auto',0]);
});

test('automatic work restores the exact prior full profile only after every open task settles',async t=>{
  const f=profileFixture(t,{initial:{model:'gpt-6-astra',provider:'custom-gateway',providerKind:'native',reasoningEffort:'high',serviceTierPreference:'default'},classify:async input=>({route:'work',reason:'substantive work',recall:{mode:'light',query:input.text,reason:'current request'}})});
  await f.router.dispatch({id:'multi-work',text:'做两个独立步骤'},async()=> 'new-turn');
  const first=f.router.currentTask(),firstReturn=structuredClone(f.router.state.autoReturnProfile);
  const second={...structuredClone(first),id:'work-second',inputIds:['second'],inputVersion:1,summary:'second step',deliveries:{},tools:{},completion:null,createdAt:f.now()};
  f.router.state.tasks[second.id]=second;f.router.save('synthetic-second-task');
  for(const task of [first,second]){task.completion={inputVersion:task.inputVersion,at:f.now(),summary:'done'};task.stopReason='end_turn';task.turnEndedAt=f.now();task.deliveries['delivery-'+task.id]={state:'accepted',messageId:'m-'+task.id,inputVersion:task.inputVersion,turnFence:task.executionEpoch,at:f.now()};}
  second.deliveries['delivery-'+second.id].state='pending';
  await f.router.reconcile();
  assert.deepEqual([f.router.state.tasks[first.id].status,f.router.state.tasks[second.id].status,f.runtime.model],['completed','running','gpt-5.6-sol']);
  second.deliveries['delivery-'+second.id].state='accepted';second.deliveries['delivery-'+second.id].messageId='m-second';second.deliveries['delivery-'+second.id].at=f.now();
  await f.router.reconcile();await f.router.applyPendingMode();
  assert.deepEqual(firstReturn,{provider:'custom-gateway',providerKind:'native',model:'gpt-6-astra',reasoningEffort:'high',serviceTier:null,serviceTierVerified:false,serviceTierPreference:'default'});
  assert.deepEqual([f.runtime.model,f.runtime.reasoningEffort,f.runtime.serviceTierPreference],['gpt-6-astra','high','default']);
});
