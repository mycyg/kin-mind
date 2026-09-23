import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,modeCommand,recentConversation,attachmentMetadata,CLASSIFIER_DECISION,NOTICE_SEND_BUDGET,NOTICE_LOOKUP_BUDGET,noticeReceiptClass} from '../../adapters/mobile-router.mjs';
import {compactPrompt,publicMobileRuntime} from '../../adapters/mobile-controls.mjs';
import {TransportManifests} from '../../adapters/transport-manifest.mjs';
import {ReplyGuard} from '../../adapters/reply-guard.mjs';
import {replyProgress} from '../../adapters/reply-progress.mjs';
import {createFakeTransport} from './helpers/fake-transport.mjs';

function fixture(t, options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-routing-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=100,classificationCalls=0;
  const runtime={known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'gpt-6-sol',modelProvider:'custom-gateway',providerOverride:false,reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0};
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
  {id:'gpt-6-sol',provider:'custom-gateway',providerKind:'native',reasoningEfforts:['low','medium','high','xhigh','max'],defaultReasoningEffort:'medium',serviceTiers:[{id:'priority'}],defaultServiceTier:'priority'},
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
 const first=Object.values(f.router.state.notices)[0];assert.equal(first.target,'gpt-6-sol');
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
  assert.deepEqual([entry.failure.class,entry.attempts,entry.maxAttempts,entry.model],['timeout',2,4,'gpt-6-sol']);
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
  const f=profileFixture(t,{initial:{model:'gpt-6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'fast'},
    classify:async input=>({route:'control',control:'manual',profile:{model:'gpt-6-sol',reasoningEffort:'high',serviceTierPreference:'fast'},force:true,reason:'same model higher effort',recall:{mode:'light',query:input.text,reason:'control'}})});
  await f.router.dispatch({id:'sol-high',text:'Sol 保持不变，改成 high 和 Fast'},async()=>assert.fail('control is host-owned'));
  const sent=[];await f.router.flushNotices({send:async notice=>{sent.push(notice);return{state:'accepted',messageId:'same-model-notice'};},lookup:async()=>null});
  assert.deepEqual(sent.map(notice=>notice.text),['已切换到 GPT‑6 Sol · high · Fast 配置已开启（手动模式）。']);
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
  assert.deepEqual([f.router.state.tasks[first.id].status,f.router.state.tasks[second.id].status,f.runtime.model],['completed','running','gpt-6-sol']);
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
  const f=profileFixture(t,{initial:{model:'gpt-6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'fast'},
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

test('an unconfirmed force request completes its original fence when the same native session becomes idle',async t=>{
  let forceCalls=0;
  const f=profileFixture(t,{classify:async()=>({route:'control',control:'manual',profile:{model:'gpt-6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'},force:true,reason:'owner model change'}),
    forceSwitch:async()=>{forceCalls++;return {state:'unconfirmed',reason:'native-not-idle'};}});
  f.router.state.executionEpoch=4;f.runtime.nativeStatus='systemError';
  const first=await f.router.dispatch({id:'force-after-system-error',kind:'owner',text:'切到 Sol medium'},async()=>assert.fail('control does not submit'));
  const request=f.router.state.requests['owner-mode:force-after-system-error'];
  assert.deepEqual([first.state,request.forceState,f.router.state.executionEpoch,forceCalls],['pending','unconfirmed',4,1]);
  await f.router.applyPendingMode();
  assert.equal(f.router.state.forceBoundaries.length,0,'an unknown native turn cannot be fenced');
  f.runtime.nativeStatus='idle';
  await f.router.applyPendingMode();
  assert.deepEqual([request.state,request.forceState,f.router.state.executionEpoch,forceCalls],['applied','confirmed',5,1]);
  assert.equal(request.forceBoundary.receipt.state,'idle');
  assert.equal(request.forceBoundary.receipt.reconciled,true);
  assert.equal(f.switches.length,1);
  await f.router.applyPendingMode();
  assert.equal(f.router.state.forceBoundaries.length,1,'repeated pumps do not create another boundary');
  assert.equal(f.switches.length,1);
});

test('an already applied unconfirmed force is fenced once only while its original mode remains current',async t=>{
  const f=profileFixture(t,{initial:{model:'gpt-6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'fast'},
    forceSwitch:async()=>assert.fail('an applied request must not interrupt again')});
  f.router.state.executionEpoch=4;f.router.state.mode='manual';
  f.router.state.manualProfile={provider:'custom-gateway',providerKind:'native',model:'gpt-6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'};
  f.router.state.inputs.original={id:'original',kind:'owner',hash:'source-hash',nativeThreadId:'synthetic'};
  const request={commandId:'owner-mode:original',sourceInputId:'original',sourceHash:'source-hash',mode:'manual',force:true,state:'applied',forceState:'unconfirmed',forceStartedAt:f.now(),
    result:{mode:'manual',sessionId:'synthetic',provider:'custom-gateway',model:'gpt-6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'}};
  f.router.state.requests[request.commandId]=request;f.router.save('fixture-applied-without-force-fence');
  const restored=new MobileRouter(f.args);
  f.runtime.nativeStatus='systemError';await restored.applyPendingMode();assert.equal(restored.state.executionEpoch,4);
  restored.state.requests['owner-mode:later']={commandId:'owner-mode:later',mode:'manual',state:'failed'};
  assert.equal(restored.unfencedForce()?.commandId,request.commandId,'a failed later choice does not erase the old interruption');
  restored.state.requests[request.commandId].result.sessionId='other-session';
  assert.equal(restored.unfencedForce(),null,'a different native session cannot release the old epoch');
  restored.state.requests[request.commandId].result.sessionId='synthetic';
  f.runtime.nativeStatus='idle';await restored.applyPendingMode();
  assert.equal(restored.state.executionEpoch,5);
  assert.equal(restored.state.requests[request.commandId].result.forceBoundaryId,restored.state.forceBoundaries[0].id);
  await restored.applyPendingMode();assert.equal(restored.state.forceBoundaries.length,1);
  delete restored.state.requests[request.commandId].forceBoundary;
  restored.state.requests[request.commandId].forceState='unconfirmed';
  assert.equal(restored.unfencedForce(),null,'a later boundary prevents duplicate advancement');
});

test('a later deferred mode uses the earlier confirmed interruption fence without reviving the old mode',async t=>{
  let forces=0;
  const f=profileFixture(t,{classify:async input=>({route:'control',control:'manual',profile:{model:input.text==='first'?'gpt-6-sol':'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:input.text==='first'?'fast':'default'},force:input.text==='first',reason:'owner model choice'}),
    forceSwitch:async()=>{forces++;return {state:'unconfirmed',reason:'native-not-idle'};}});
  f.router.state.executionEpoch=4;f.runtime.nativeStatus='systemError';
  await f.router.dispatch({id:'first',kind:'owner',text:'first'},async()=>assert.fail('control'));
  await f.router.dispatch({id:'second',kind:'owner',text:'second'},async()=>assert.fail('control'));
  const first=f.router.state.requests['owner-mode:first'],second=f.router.state.requests['owner-mode:second'];
  assert.deepEqual([first.state,second.state,f.router.state.executionEpoch,forces],['superseded','pending',4,1]);
  f.runtime.nativeStatus='idle';await f.router.applyPendingMode();
  assert.equal(first.state,'superseded');assert.equal(first.forceState,'confirmed');
  assert.equal(f.router.state.forceBoundaries[0].commandId,first.commandId);
  assert.deepEqual([second.state,f.runtime.model,f.router.state.executionEpoch],['applied','gpt-6-astra',5]);
  assert.deepEqual(f.switches.map(item=>item.model),['gpt-6-astra'],'the older Sol choice is never switched or announced');
});

test('a turn fenced before transport submission retires its manifest without replaying, while an unknown send stays blocked',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-switch-receipt-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const emitted=[],canceled=[];
  const manifests=new TransportManifests({directory:path.join(root,'manifests'),clock:()=>Date.parse('2026-09-23T10:00:00Z'),sleep:async()=>{},lease:{heartbeat:false},
    receipt:async()=>null,emit:async event=>emitted.push(event),cancelShare:async id=>canceled.push(id)});
  const make=id=>manifests.createDraft({ownerEpoch:'epoch',entries:[{request:{draft_id:'draft-'+id,reply_id:'input-'+id,text:'旧回复'},
    delivery:{id:'bubble-'+id,memoryBatchId:'group-'+id,text:'旧回复',turnFence:4}}]});
  const ready=async()=>({state:'ready',checked:[{}]});
  make('fenced');let refused=0;
  const stopped=await manifests.run('group-fenced',{review:ready,transport:{receipt:async()=>null,send:async()=>{refused++;return {state:'not-submitted',submissionStarted:false,reason:'turn-superseded-before-send'};}}});
  assert.equal(stopped.manifest.state,'retired');
  assert.deepEqual([stopped.manifest.bubbles[0].state,stopped.manifest.bubbles[0].fragments[0].state],['canceled','canceled']);
  assert.equal(stopped.manifest.bubbles[0].fragments[0].receipt.state,'not-submitted');
  assert.equal(emitted[0].state,'canceled');assert.deepEqual(canceled,['draft-fenced']);
  await manifests.run('group-fenced',{transport:async()=>assert.fail('old epoch must not retry')});
  assert.equal(refused,1);
  make('unknown');
  const unknown=await manifests.run('group-unknown',{review:ready,transport:{receipt:async()=>null,send:async()=>({state:'blocked',reason:'transport-outcome-unknown'})}});
  assert.equal(unknown.manifest.state,'blocked-unknown');
  assert.equal(unknown.manifest.bubbles[0].fragments[0].state,'unknown');
  assert.equal(emitted.at(-1).state,'unconfirmed','an unknown send is never reported as canceled');
});

test('operator can retire only a proven before-send refusal with no external receipt or accepted fragment',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-switch-operator-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let external=null;const emitted=[],canceled=[];
  const manifests=new TransportManifests({directory:path.join(root,'manifests'),clock:()=>Date.parse('2026-09-23T10:00:00Z'),sleep:async()=>{},lease:{heartbeat:false},
    receipt:async()=>external,emit:async event=>emitted.push(event),cancelShare:async id=>canceled.push(id)});
  manifests.createDraft({ownerEpoch:'epoch',entries:[{request:{draft_id:'draft-old',reply_id:'input-old',text:'原回复'},
    delivery:{id:'bubble-old',memoryBatchId:'group-old',text:'原回复',turnFence:4}}]});
  const first=await manifests.run('group-old',{review:async()=>({state:'ready',checked:[{}]}),transport:async()=>({state:'blocked',reason:'turn-superseded-before-send'})});
  const fragmentId=first.manifest.bubbles[0].fragments[0].transport_id;
  assert.equal(first.manifest.state,'blocked-unknown');
  external={state:'unconfirmed'};
  await assert.rejects(manifests.resolve('group-old',fragmentId,{outcome:'not-submitted'}),/External receipt/);
  assert.equal(manifests.read('group-old').state,'blocked-unknown');
  external=null;
  const settled=await manifests.resolve('group-old',fragmentId,{outcome:'not-submitted'});
  assert.equal(settled.manifest.state,'retired');
  assert.equal(settled.manifest.bubbles[0].fragments[0].receipt.source,'operator');
  assert.deepEqual([emitted.at(-1).state,canceled[0]],['canceled','draft-old']);
  assert.equal(manifests.isLive('group-old'),false);
});

test('service resume finishes CLI-retired before-send refusal without sending or failure notice',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-switch-cli-resume-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const directory=path.join(root,'manifests'),clock=()=>Date.parse('2026-09-23T10:00:00Z');
  const cli=new TransportManifests({directory,clock,role:'cli',receipt:async()=>null,lease:{heartbeat:false}});
  cli.createDraft({ownerEpoch:'epoch',entries:[{request:{draft_id:'draft-old',reply_id:'input-old',text:'原回复'},
    delivery:{id:'bubble-old',memoryBatchId:'group-old',text:'原回复',turnFence:4}}]});
  const first=await cli.run('group-old',{review:async()=>({state:'ready',checked:[{}]}),transport:async()=>({state:'blocked',reason:'turn-superseded-before-send'})});
  const fragmentId=first.manifest.bubbles[0].fragments[0].transport_id;
  await cli.resolve('group-old',fragmentId,{outcome:'not-submitted'});
  assert.equal(cli.isLive('group-old'),true,'the CLI has no memory or share cancellation sink');
  const events=[],shares=[];
  const guard=new ReplyGuard({directory:root,manifestDirectory:directory,clock,role:'service',lease:{heartbeat:false},replyTailDecision:false,
    receipt:async()=>null,emit:async event=>events.push(event),call:async(action,input)=>{if(action==='share-cancel')shares.push(input.draft_id);},
    notifyFailure:async()=>assert.fail('a canceled reply has no failure notice')});
  const resume=()=>guard.resumeDue({guard:async()=>assert.fail('no guard decision'),send:async()=>assert.fail('no transport send')});
  assert.equal((await resume()).checked,1);
  assert.deepEqual([events.map(event=>event.state),shares],[['canceled'],['draft-old']]);
  assert.equal(guard.manifests.isLive('group-old'),false);
  assert.equal((await resume()).checked,0,'repeated pumps do not duplicate side effects');
});

test('approved cancellation resolves only the matching input without claiming a sent or silent reply',()=>{
  const at='2026-09-23T11:20:00Z',previous={awaitingReplyInputId:'old-input',awaitingReplySince:'2026-09-23T10:15:00Z',
    replyDisposition:'unconfirmed',replyWaitingReason:'delivery-outcome-unknown',lastResolvedInputId:'earlier-input',lastReplyAt:'earlier-reply'};
  const detail={inputId:'old-input',groupId:'old-group',groupState:'retired',state:'canceled',
    reason:'turn-superseded-before-send',approvalSource:'KIN-ITER-20260923-02-v1'};
  const canceled={...previous,...replyProgress(previous,'reply-canceled',detail,at)};
  assert.deepEqual([canceled.replyDisposition,canceled.replyWaitingReason,canceled.awaitingReplySince,canceled.lastResolvedInputId,canceled.lastResolvedAt],
    ['canceled','turn-superseded-before-send',null,'old-input',at]);
  assert.equal(canceled.replyGroups['old-group'].approvalSource,detail.approvalSource);
  assert.equal(canceled.lastReplyAt,previous.lastReplyAt,'cancellation is not a sent reply');
  const newer={...previous,awaitingReplyInputId:'new-input'};
  const late={...newer,...replyProgress(newer,'reply-canceled',detail,at)};
  assert.deepEqual([late.awaitingReplyInputId,late.awaitingReplySince,late.replyDisposition,late.lastResolvedInputId],
    ['new-input',newer.awaitingReplySince,'unconfirmed','earlier-input']);
  assert.equal(late.replyGroups['old-group'].state,'retired','the old group outcome remains visible');
  const unapproved={...previous,...replyProgress(previous,'reply-canceled',{...detail,approvalSource:null},at)};
  assert.equal(unapproved.awaitingReplySince,previous.awaitingReplySince,'a cancellation needs its approval source');
});

test('quiet main assessment retains the actual manual profile and never classifies or notifies',async t=>{
  const f=profileFixture(t,{initial:{model:'gpt-6-astra',provider:'custom-gateway',providerKind:'native',reasoningEffort:'high',serviceTierPreference:'default'}});
  f.router.state.mode='manual';f.router.state.manualProfile={model:'gpt-6-astra',provider:'custom-gateway',providerKind:'native',reasoningEffort:'high',serviceTierPreference:'default'};
  let submitted=0;
  const result=await f.router.dispatch({id:'assessment:one',text:'空闲时想想最近的心情，也可以什么都不做。',kind:'assessment'},async()=>{submitted++;return 'new-turn';});
  assert.equal(result.route,'new-turn');assert.equal(submitted,1);
  assert.equal(f.switches.length,0);assert.equal(f.classifications.length,0);
  assert.equal(Object.keys(f.router.state.notices).length,0);assert.equal(f.router.tasks().length,0);
  f.runtime.active=true;
  assert.equal((await f.router.dispatch({id:'assessment:two',text:'下一次',kind:'assessment'},async()=>assert.fail('must yield'))).route,'deferred');
});

test('a mixed work and explicit model request switches first and submits the work exactly once',async t=>{
 const f=profileFixture(t,{classify:async()=>({route:'work',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},force:true,reason:'owner selected model and commissioned a document'})});
 f.router.state.mode='manual';f.router.state.manualProfile={model:'deepseek-flash',provider:'openai-15m',providerKind:'gateway',reasoningEffort:'high',serviceTierPreference:'default'};
 let submissions=0;
 const input={id:'mixed-work',kind:'owner',text:'Switch to ASTRA medium and finish the document'};
 const result=await f.router.dispatch(input,async decision=>{submissions++;assert.equal(f.runtime.model,'gpt-6-astra');assert.equal(decision.profile.reasoningEffort,'medium');assert.ok(decision.taskId);return 'new-turn';});
 assert.equal(result.route,'new-turn');assert.equal(f.router.state.requests['owner-mode:mixed-work'].state,'applied');
 assert.equal(f.router.currentTask().inputVersion,1);assert.equal(f.router.state.mode,'manual');
 const again=await f.router.dispatch(input,()=>assert.fail('duplicate work'));assert.equal(again.route,'deduplicated');assert.equal(submissions,1);
});

test('mixed owner control interrupts active work but preserves task identity and old side effects',async t=>{
 let f,forces=0;
 f=profileFixture(t,{classify:async()=>({route:'work',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},force:true,reason:'explicit owner choice'}),forceSwitch:async()=>{forces++;Object.assign(f.runtime,{active:false,nativeStatus:'idle',backgroundTasks:0});return {state:'interrupted'};}});
 const task=f.router.addTask({id:'old',text:'previous work'});task.deliveries.old={state:'accepted',messageId:'already-delivered'};
 Object.assign(f.runtime,{active:true,nativeStatus:'active',backgroundTasks:1,pendingDeliveries:1});
 await f.router.dispatch({id:'mixed-force',kind:'owner',text:'Switch and add figures'},async()=>{assert.equal(f.runtime.model,'gpt-6-astra');return 'new-turn';});
 assert.equal(forces,1);assert.equal(f.router.currentTask().id,task.id);assert.equal(task.deliveries.old.messageId,'already-delivered');assert.equal(task.inputVersion,2);
});

test('failed mixed control never runs work under the previous model',async t=>{
 const f=profileFixture(t,{classify:async()=>({route:'work',control:'manual',profile:{model:'__unsupported__',reasoningEffort:'medium',serviceTierPreference:'default'},reason:'unsupported exact choice'})});
 const result=await f.router.dispatch({id:'unsupported-work',kind:'owner',text:'Use unavailable model to work'},()=>assert.fail('must not submit on old model'));
 assert.equal(result.route,'control-failed');assert.equal(f.runtime.model,'deepseek-flash');assert.equal(f.router.state.inputs['unsupported-work'].failureStage,'model-control');
});

test('explicit deferred mixed control without prior work does not wait on its own new task',async t=>{
 const f=profileFixture(t,{classify:async()=>({route:'work',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},force:false,reason:'switch at idle then work'})});
 await f.router.dispatch({id:'deferred-new-work',kind:'owner',text:'After old work ends, switch and start the next document'},async()=> 'new-turn');
 assert.equal(f.router.state.requests['owner-mode:deferred-new-work'].state,'applied');assert.equal(f.router.tasks().length,1);
});

test('control-only interruption schedules the existing handoff after verified model application',async t=>{
 let f;f=profileFixture(t,{classify:async()=>({route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'},reason:'owner profile choice'}),forceSwitch:async()=>{Object.assign(f.runtime,{active:false,nativeStatus:'idle'});return {state:'interrupted'};}});
 const task=f.router.addTask({id:'original',text:'finish original deliverable'});Object.assign(f.runtime,{active:true,nativeStatus:'active'});
 await f.router.dispatch({id:'control-only',kind:'owner',text:'Switch to ASTRA medium'},()=>assert.fail('control is not owner work'));
 assert.equal(task.handoff.state,'pending');assert.equal(task.handoff.id,'owner-mode:control-only');assert.equal(f.router.state.requests[task.handoff.id].state,'applied');assert.equal(task.inputVersion,1);
});

test('repeated manual profile keeps active work, tools and pending delivery intact, including restart',async t=>{
  let interruptions=0;
  const profile={model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'default'};
  const f=profileFixture(t,{classify:async input=>input.text==='work'
    ?{route:'work',reason:'new work'}
    :{route:input.text==='mixed'?'work':'control',control:'manual',profile,force:true,reason:'same selected profile'},
    forceSwitch:async()=>{interruptions++;return{state:'interrupted'};}});
  await f.router.dispatch({id:'pin',text:'pin'},async()=>assert.fail('control'));
  await f.router.dispatch({id:'work',text:'work'},async()=> 'new-turn');
  const task=f.router.currentTask(),version=task.inputVersion,epoch=f.router.state.executionEpoch;
  task.tools.tool={status:'in_progress',turnFence:epoch};
  task.deliveries.file={state:'unconfirmed',inputVersion:version};
  f.runtime.active=true;f.runtime.backgroundTasks=1;f.runtime.pendingDeliveries=1;f.runtime.nativeStatus='running';
  const notices=structuredClone(f.router.state.notices),tools=structuredClone(task.tools),deliveries=structuredClone(task.deliveries);
  await f.router.dispatch({id:'same-control',text:'pin'},async()=>assert.fail('control'));
  assert.equal(f.router.state.requests['owner-mode:same-control'].state,'applied');
  assert.equal(interruptions,0);assert.equal(f.switches.length,1);assert.equal(f.router.state.executionEpoch,epoch);
  assert.deepEqual(f.router.state.notices,notices);assert.deepEqual(task.tools,tools);assert.deepEqual(task.deliveries,deliveries);
  assert.equal(task.inputVersion,version);assert.equal(task.handoff,undefined);
  const restarted=new MobileRouter(f.args);
  await restarted.dispatch({id:'same-control',text:'pin'},async()=>assert.fail('duplicate control'));
  let submitted;
  await restarted.dispatch({id:'mixed-repeat',text:'mixed'},async detail=>{submitted=detail;return'steer';});
  assert.equal(submitted.taskId,task.id);assert.equal(submitted.inputVersion,version+1);
  assert.equal(interruptions,0);assert.equal(f.switches.length,1);assert.deepEqual(restarted.state.notices,JSON.parse(JSON.stringify(notices)));
});

test('manual content and task completion retain the full profile without choosing another model',async t=>{
  const f=profileFixture(t,{classify:async input=>input.text==='pin'
    ?{route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:'high',serviceTierPreference:'default'},reason:'owner choice'}
    :{route:input.text==='chat'?'chat':'work',reason:'content only'}});
  await f.router.dispatch({id:'pin',text:'pin'},async()=>assert.fail('control'));
  for(const text of ['work','chat','progress'])await f.router.dispatch({id:text,text},async detail=>{assert.equal(detail.model,'gpt-6-astra');assert.equal(detail.profile.reasoningEffort,'high');return'new-turn';});
  assert.equal(f.classifications.length,4,'one existing content/recall call per owner message');
  for(const call of f.classifications.slice(1))assert.deepEqual([call.mode,call.currentProfile.model,call.currentProfile.reasoningEffort,call.currentProfile.serviceTierPreference],['manual','gpt-6-astra','high','default']);
  const task=f.router.currentTask(),notices=structuredClone(f.router.state.notices);
  await f.router.requestMode({commandId:'task-complete',mode:'auto',completedTaskId:task.id,completedInputVersion:task.inputVersion,reason:'task finished',notify:false});
  await f.router.reconcile();await f.router.applyPendingMode();
  assert.ok(task.completion);assert.equal(f.router.state.mode,'manual');assert.equal(f.router.state.requestedMode,null);
  assert.equal(f.switches.length,1);assert.deepEqual(f.router.state.notices,notices);
});

test('an actual effort change still interrupts once and notifies after profile verification',async t=>{
  let interruptions=0;
  const f=profileFixture(t,{classify:async input=>({route:'control',control:'manual',profile:{model:'gpt-6-astra',reasoningEffort:input.text==='pin'?'medium':'high',serviceTierPreference:'default'},force:true,reason:'fresh owner request'}),
    forceSwitch:async()=>{interruptions++;f.runtime.active=false;f.runtime.nativeStatus='idle';return{state:'interrupted'};}});
  await f.router.dispatch({id:'pin',text:'pin'},async()=>assert.fail('control'));
  f.runtime.active=true;f.runtime.nativeStatus='running';
  await f.router.dispatch({id:'higher',text:'higher'},async()=>assert.fail('control'));
  assert.equal(interruptions,1);assert.equal(f.runtime.reasoningEffort,'high');assert.equal(f.switches.length,2);
  assert.equal(Object.values(f.router.state.notices).length,2);
});

test('a historical canceled task cannot discard another task input after a classification retry or restart',async t=>{
  let calls=0;
  const f=fixture(t,{classify:async()=>{if(++calls===1)throw Error('classification-timeout');return{route:'work',reason:'append a conclusion'};}});
  const task=f.router.addTask({id:'current-work',text:'edit current article'});
  f.router.state.tasks.old={...structuredClone(task),id:'old',status:'canceled',cancelRequested:true,canceledAt:1};
  const input={id:'conclusion',text:'append the conclusion'};
  await f.router.select(input);
  const restarted=new MobileRouter(f.args);
  await restarted.reviewSemanticPending();
  assert.equal(restarted.state.inputs.conclusion.state,'selected');
  assert.equal(restarted.state.inputs.conclusion.taskId,task.id);
  let submitted=0;
  await restarted.dispatch(input,async detail=>{submitted++;assert.equal(detail.taskId,task.id);return'steer';});
  await restarted.dispatch(input,async()=>assert.fail('duplicate'));
  assert.equal(submitted,1);assert.equal(calls,2);assert.equal(restarted.state.tasks.old.status,'canceled');
});

test('a real stop of the captured task still retires late work classification',async t=>{
  let calls=0;
  const f=fixture(t,{classify:async()=>{if(++calls===1)throw Error('classification-timeout');return{route:'work',reason:'late work'};}});
  const task=f.router.addTask({id:'current-work',text:'edit current article'});
  await f.router.select({id:'pending',text:'append a conclusion'});
  task.cancelRequested=true;
  await f.router.reviewSemanticPending();
  assert.equal(f.router.state.inputs.pending.state,'semantic-canceled');
  assert.equal(f.router.state.inputs.pending.reason,'superseded-by-cancel');
  assert.equal(f.router.state.tasks[task.id].inputVersion,1);
});

test('new independent work can retry after a prior task was canceled',async t=>{
  let calls=0;
  const f=fixture(t,{classify:async()=>{if(++calls===1)throw Error('classification-timeout');return{route:'work',reason:'independent new work'};}});
  const old=f.router.addTask({id:'old-work',text:'old task'});old.cancelRequested=true;old.status='canceled';old.canceledAt=1;
  await f.router.select({id:'new-work',text:'start the new article'});
  await f.router.reviewSemanticPending();
  assert.equal(f.router.state.inputs['new-work'].state,'selected');
  assert.notEqual(f.router.state.inputs['new-work'].taskId,old.id);
  assert.equal(old.status,'canceled');assert.equal(old.cancelRequested,true);
});


test('profile resolution failure settles the input before any native submission',async t=>{
  const f=fixture(t,{resolveProfile:async()=>{throw Error('Canonical model provider unavailable');}});
  await assert.rejects(f.router.dispatch({id:'profile-failed',kind:'owner',text:'hello',submissionProtocol:'host-boundary-v1'},async()=>assert.fail('no submission')),/Canonical model provider unavailable/);
  const input=f.router.state.inputs['profile-failed'];
  assert.equal(input.state,'failed-before-submit');assert.equal(input.submissionStartedAt,undefined);
  assert.equal(input.reason,'Canonical model provider unavailable');assert.equal(f.router.inflight.size,0);
  assert.equal(new MobileRouter(f.args).state.inputs['profile-failed'].state,'failed-before-submit');
});

test('a terminal native error allows a fresh same-profile assessment, preserving all execution locks',async t=>{
  const f=fixture(t);f.runtime.nativeStatus='systemError';
  assert.equal(f.router.busy(f.runtime),true,'general work/switch boundary is unchanged');
  let submissions=0;
  const result=await f.router.dispatch({id:'assessment:recovery',kind:'assessment',text:'internal review'},async detail=>{submissions++;assert.equal(detail.model,f.runtime.model);return 'new-turn';});
  assert.equal(result.route,'new-turn');assert.equal(submissions,1);assert.equal(f.switched.length,0);
  for(const key of ['active','backgroundTasks','queued','pendingDeliveries']) {
    f.runtime[key]=1;
    assert.equal((await f.router.dispatch({id:'assessment:'+key,kind:'assessment',text:'wait'},()=>assert.fail('held'))).route,'deferred');
    f.runtime[key]=0;
  }
  f.runtime.nativeStatus='unknown';
  assert.equal((await f.router.dispatch({id:'assessment:unknown',kind:'assessment',text:'wait'},()=>assert.fail('unknown'))).route,'deferred');
});


test('runtime lists supplier capabilities and new defaults while preserving an older manual choice',async t=>{
 const f=profileFixture(t,{initial:{model:'gpt-6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'medium',serviceTierPreference:'fast'}});
 const chosen={model:'gpt-5.6-sol',provider:'custom-gateway',providerKind:'native',reasoningEffort:'high',serviceTierPreference:'default'};
 f.router.state.mode='manual';f.router.state.manualProfile=chosen;Object.assign(f.runtime,{model:chosen.model,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off'});
 const view=await f.router.readRuntime();
 assert.equal(view.defaults.work.model,'gpt-6-sol');assert.equal(view.defaults.work.reasoningEffort,'medium');
 assert.equal(view.defaults.chat.model,'deepseek-flash');assert.ok(view.models.some(m=>m.id==='gpt-6-sol'));
 assert.equal(view.actual.model,'gpt-5.6-sol');assert.deepEqual(f.router.state.manualProfile,chosen);
});
