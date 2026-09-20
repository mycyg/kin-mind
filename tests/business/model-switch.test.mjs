import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,modeCommand,recentConversation,attachmentMetadata,CLASSIFIER_DECISION,NOTICE_SEND_BUDGET,NOTICE_LOOKUP_BUDGET,noticeReceiptClass} from '../../adapters/mobile-router.mjs';
import {compactPrompt,publicMobileRuntime} from '../../adapters/mobile-controls.mjs';
import {TransportManifests} from '../../adapters/transport-manifest.mjs';
import {createFakeTransport} from './helpers/fake-transport.mjs';

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
  return {root,router:new MobileRouter(args),runtime,args,switches,classifications,now:()=>++now};
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

test('duplicate input never resubmits or reclassifies; uncertain submission is held',async t=>{
  const f=fixture(t);let count=0;const input={id:'one',text:'hello'};
  await f.router.dispatch(input,async()=>{count++;return'new-turn';});
  await f.router.dispatch(input,async()=>{count++;return'new-turn';});
  assert.equal(count,1);assert.equal(f.classificationCalls(),1);
  await assert.rejects(f.router.dispatch({id:'two',text:'hi'},async()=>{throw Error('lost response');}),/reconciliation/);
  await assert.rejects(f.router.dispatch({id:'two',text:'hi'},async()=>{count++;}),/reconciliation/);
  assert.equal(count,1);
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

test('uncertain notification reconciles its original ID without another send',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{throw Error('no model');});
  let sends=0;
  await f.router.flushNotices({send:async()=>{sends++;return{state:'accepted'};},lookup:async()=>({state:'unconfirmed'})});
  const id=Object.keys(f.router.state.notices)[0];assert.equal(f.router.state.notices[id].state,'unconfirmed');
  const restored=new MobileRouter(f.args);restored.state.notices[id].nextAttemptAt=0;
  await restored.flushNotices({send:async()=>{sends++;throw Error('must not replay');},lookup:async requested=>{assert.equal(requested,id);return{state:'accepted',messageId:'reconciled'};}});
  assert.equal(sends,1);assert.equal(restored.state.notices[id].messageId,'reconciled');
});

test('same-model control notices name verified effort and describe Fast only as configuration',async t=>{
  const f=profileFixture(t,{initial:{model:'gpt-5.6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'fast'},
    classify:async input=>({route:'control',control:'manual',profile:{model:'gpt-5.6-sol',reasoningEffort:'high',serviceTierPreference:'fast'},force:true,reason:'same model higher effort',recall:{mode:'light',query:input.text,reason:'control'}})});
  await f.router.dispatch({id:'sol-high',text:'Sol 保持不变，改成 high 和 Fast'},async()=>assert.fail('control is host-owned'));
  const sent=[];await f.router.flushNotices({send:async notice=>{sent.push(notice);return{state:'accepted',messageId:'same-model-notice'};},lookup:async()=>null});
  assert.deepEqual(sent.map(notice=>notice.text),['已切换到 GPT‑5.6 Sol · high · Fast 配置已开启（手动模式）。']);
  assert.doesNotMatch(sent[0].text,/priority|实际.*Fast/);
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

test('a natural manual Astra profile is catalog-validated, announced once and persists through work completion',async t=>{
  const f=profileFixture(t,{classify:async input=>input.text.includes('ASTRA-6')
    ?{route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},force:true,reason:'explicit owner profile',recall:{mode:'light',query:input.text,reason:'current control'}}
    :{route:'work',reason:'substantive work',recall:{mode:'light',query:input.text,reason:'current request'}}});
  const control=await f.router.dispatch({id:'astra',text:'我要切换到ASTRA-6 medium'},async()=>assert.fail('host control is not a native prompt'));
  assert.deepEqual([control.route,control.state,f.router.state.mode,f.runtime.model],['host-control','applied','manual','gpt-6-astra']);
  assert.deepEqual(f.router.state.manualProfile,{provider:'custom-gateway',providerKind:'native',model:'gpt-6-astra',reasoningEffort:'medium',serviceTier:null,serviceTierVerified:false,serviceTierPreference:'default'});
  const sent=[];
  await f.router.flushNotices({send:async notice=>{sent.push(notice);return{state:'accepted',messageId:'manual-ok'};},lookup:async()=>null});
  assert.deepEqual(sent.map(notice=>notice.text),['已切换到 GPT‑6 Astra · medium（手动模式）。']);
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
  assert.equal(task.deliveries['old-delivery'],undefined,'an old fence never overwrites the current delivery ledger');
  assert.equal(Object.values(task.deliveryHistory).some(delivery=>delivery.id==='old-delivery'&&delivery.messageId==='already-sent'&&delivery.lateAfterForce),true,'an already-sent platform receipt remains independent evidence at its old fence');
  await f.router.dispatch({id:'original-work',text:'继续做当前任务'},async()=>assert.fail('the original ID can never be resubmitted'));
  assert.equal(f.router.currentTask().id,taskId);
});

