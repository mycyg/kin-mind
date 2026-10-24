import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,modeCommand,recentConversation,attachmentMetadata,CLASSIFIER_DECISION,NOTICE_SEND_BUDGET,NOTICE_LOOKUP_BUDGET,noticeReceiptClass} from '../../adapters/mobile-router.mjs';
import {compactPrompt,publicMobileRuntime} from '../../adapters/mobile-controls.mjs';
import {TransportManifests} from '../../adapters/transport-manifest.mjs';
import {switchCodexModel} from '../../adapters/codex-models.mjs';
import {ReplyGuard} from '../../adapters/reply-guard.mjs';
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
  {id:'k3',aliases:['Kimi K3','Kimi'],provider:'openai-15m',providerKind:'gateway',reasoningEfforts:['high'],defaultReasoningEffort:'high',serviceTiers:['default'],defaultServiceTier:'default'},
  {id:'deepseek-flash',provider:'openai-15m',providerKind:'gateway',reasoningEfforts:['high'],defaultReasoningEffort:'high',serviceTiers:['default'],defaultServiceTier:'default'},
  {id:'gpt-6-sol',provider:'custom-gateway',providerKind:'native',reasoningEfforts:['low','medium','high','xhigh','max'],defaultReasoningEffort:'medium',serviceTiers:[{id:'priority'}],defaultServiceTier:'priority'},
  {id:'gpt-6.1-sol',provider:'custom-gateway',providerKind:'native',reasoningEfforts:['low','medium','high','xhigh','max'],defaultReasoningEffort:'medium',serviceTiers:[{id:'priority'}],defaultServiceTier:'priority'},
  {id:'gpt-6-astra',aliases:['GPT‑6 Astra','ASTRA-6'],provider:'custom-gateway',providerKind:'native',reasoningEfforts:['medium','high'],defaultReasoningEffort:'medium',serviceTiers:['default',{id:'priority'}],defaultServiceTier:'default'},
];
function profileFixture(t,{initial={model:'deepseek-flash',provider:'openai-15m',providerKind:'gateway',reasoningEffort:'high',serviceTierPreference:'default'},classify=null,forceSwitch=null,preflightProfile=null}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-profile-routing-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=1000;
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0,
    model:initial.model,modelProvider:initial.provider,providerOverride:initial.providerKind==='gateway',reasoningEffort:initial.reasoningEffort,serviceTierPreference:initial.serviceTierPreference,fastMode:initial.serviceTierPreference==='fast'?'on':'off',serviceTier:null,serviceTierVerified:false};
  const switches=[],classifications=[];
  const args={file:path.join(root,'state.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),modelCatalog:async()=>structuredClone(LIVE_MODELS),
    classify:async input=>{classifications.push(input);return classify?classify(input):{route:'chat',reason:'casual',recall:{mode:'light',query:input.text,reason:'current message'}};},
    switchModel:async(model,profile,context)=>{switches.push({model,profile:structuredClone(profile),context:structuredClone(context)});Object.assign(runtime,{model,modelProvider:profile.provider,providerOverride:profile.providerKind==='gateway',reasoningEffort:profile.reasoningEffort,serviceTierPreference:profile.serviceTierPreference,fastMode:profile.serviceTierPreference==='fast'?'on':'off'});return{...runtime};},
    preflightProfile,forceSwitch,waitForIdle:async()=>{throw Error('must not wait');},now:()=>++now};
  return {root,router:new MobileRouter(args),runtime,args,switches,classifications,now:()=>++now};
}



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
  f.runtime.nativeStatus='systemError';f.runtime.active=true;await restored.applyPendingMode();assert.equal(restored.state.executionEpoch,4);
  restored.state.requests['owner-mode:later']={commandId:'owner-mode:later',mode:'manual',state:'failed'};
  assert.equal(restored.unfencedForce()?.commandId,request.commandId,'a failed later choice does not erase the old interruption');
  restored.state.requests[request.commandId].result.sessionId='other-session';
  assert.equal(restored.unfencedForce(),null,'a different native session cannot release the old epoch');
  restored.state.requests[request.commandId].result.sessionId='synthetic';
  f.runtime.nativeStatus='idle';f.runtime.active=false;await restored.applyPendingMode();
  assert.equal(restored.state.executionEpoch,5);
  assert.equal(restored.state.requests[request.commandId].result.forceBoundaryId,restored.state.forceBoundaries[0].id);
  await restored.applyPendingMode();assert.equal(restored.state.forceBoundaries.length,1);
  delete restored.state.requests[request.commandId].forceBoundary;
  restored.state.requests[request.commandId].forceState='unconfirmed';
  assert.equal(restored.unfencedForce(),null,'a later boundary prevents duplicate advancement');
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
  // N4: the words never reached the owner, so they are owed to Kin's next turn; the group is filed once handed.
  assert.deepEqual(manifests.read('group-old').tail_owed.items,['draft-old']);
  assert.equal(manifests.isLive('group-old'),true);
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
  const guard=new ReplyGuard({directory:root,manifestDirectory:directory,clock,role:'service',lease:{heartbeat:false},
    receipt:async()=>null,emit:async event=>events.push(event),call:async(action,input)=>{if(action==='share-cancel')shares.push(input.draft_id);}});
  const resume=()=>guard.resumeDue({guard:async()=>assert.fail('no guard decision'),send:async()=>assert.fail('no transport send')});
  assert.equal((await resume()).checked,1);
  assert.deepEqual([events.map(event=>event.state),shares],[['canceled'],['draft-old']]);
  assert.equal((await resume()).checked,0,'repeated pumps do not duplicate side effects');
  assert.deepEqual(guard.tail.owed().map(o=>o.unsent),[['原回复']],'the unsent words wait for Kin\'s next turn');
  await guard.tail.handed({groups:['group-old'],inputId:'next-input'});
  assert.equal(guard.manifests.isLive('group-old'),false);
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


test('an unavailable model catalog is a wait, never a failure of the owner input (AD1-02)',async t=>{
  let unavailable=2,waits=0;
  const f=fixture(t,{resolveProfile:async profile=>{if(unavailable-->0)throw Error('Canonical model provider unavailable');return structuredClone(profile);},waitForIdle:async()=>{waits++;}});
  let submitted=0;
  const result=await f.router.dispatch({id:'profile-wait',kind:'owner',text:'hello',submissionProtocol:'host-boundary-v1'},async(_,started)=>{await started();submitted++;return 'new-turn';});
  assert.deepEqual([result.route,submitted,waits],['new-turn',1,2]);
  assert.equal(f.router.state.inputs['profile-wait'].state,'accepted');
});

test('a preparation failure before submission keeps a bounded retry of the same id',async t=>{
  const f=fixture(t,{resolveProfile:async()=>{throw Error('profile rejected');}});
  const error=await f.router.dispatch({id:'profile-failed',kind:'owner',text:'hello',submissionProtocol:'host-boundary-v1'},async()=>assert.fail('no submission')).catch(e=>e);
  assert.match(error.message,/profile rejected/);assert.equal(error.code,'input-not-submitted');assert.equal(error.inputId,'profile-failed');
  const input=f.router.state.inputs['profile-failed'];
  assert.deepEqual([input.state,input.submissionStartedAt,input.reason,input.retry.attempts,input.retry.evidence],['failed-before-submit',undefined,'profile rejected',1,'not-submitted']);
  assert.ok(input.retry.nextAt>input.retry.lastFailureAt);assert.equal(f.router.inflight.size,0);
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
 assert.equal(view.defaults.work.model,'gpt-6.1-sol');assert.equal(view.defaults.work.reasoningEffort,'medium');
 assert.equal(view.defaults.chat.model,'deepseek-flash');assert.ok(view.models.some(m=>m.id==='gpt-6-sol'));
 assert.equal(view.actual.model,'gpt-5.6-sol');assert.deepEqual(f.router.state.manualProfile,chosen);
});








test('classifier recent conversation projects only model-visible dialogue fields',()=>{
 assert.deepEqual(recentConversation([{role:'user',text:'synthetic',at:1,conversation_id:'private-index',platform_message_id:'private-message'}]),[{role:'user',text:'synthetic',at:1}]);
});


test('an already configured gateway is not restarted for a fresh conversation profile',async()=>{
 const runtime={known:true,sessionId:'fresh',threadId:'fresh',nativeSessionId:'fresh',nativeStatus:'idle',model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,providerBaseUrl:'http://127.0.0.1:9',reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',active:false,backgroundTasks:0};
 const calls=[];const connection={extMethod:async name=>{calls.push(name);if(name==='_kin/runtime')return {...runtime};throw Error('a fresh thread has no durable rollout for provider restart');},setSessionConfigOption:async({configId,value})=>{runtime[({model:'model',reasoning_effort:'reasoningEffort','fast-mode':'fastMode'})[configId]]=value;}};
 await switchCodexModel({connection,sessionId:'fresh',profile:{provider:'custom-gateway',providerKind:'gateway',model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'},gateway:{baseUrl:runtime.providerBaseUrl,token:'test-only'}});
 assert.equal(calls.includes('providers/set'),false);
});


test('peer inherits primary manual model before restore, follows changes, and preserves its own thread and inputs',async t=>{
 const f=profileFixture(t),manual={provider:'openai-15m',providerKind:'gateway',model:'k3',reasoningEffort:'high',serviceTierPreference:'default'};
 let selected={mode:'manual',profile:manual};
 const peer=new MobileRouter({...f.args,primaryModelSettings:()=>selected});
 peer.state.inputs['group-event']={id:'group-event',state:'settled',kind:'owner',text:'synthetic family event'};
 const originalThread=peer.state.sessionId;
 assert.equal(await peer.refreshPrimaryModelSelection(),true);await peer.restoreRoutingProfile();
 assert.equal(f.runtime.model,'k3');assert.equal(peer.state.mode,'manual');
 assert.equal(peer.state.sessionId,originalThread);assert.equal(peer.state.inputs['group-event'].text,'synthetic family event');
 assert.ok(Object.values(peer.state.notices).every(n=>n.state==='suppressed'),'inherited changes do not broadcast startup model notices');
 const rev=peer.state.revision;assert.equal(await peer.refreshPrimaryModelSelection(),false);assert.equal(peer.state.revision,rev);
 selected={mode:'manual',profile:{...manual,model:'deepseek-flash'}};
 f.runtime.active=true;await peer.refreshPrimaryModelSelection();assert.equal((await peer.restoreRoutingProfile()).state,'waiting');assert.equal(f.runtime.model,'k3','do not interrupt an active reply');
 f.runtime.active=false;await peer.restoreRoutingProfile();assert.equal(f.runtime.model,'deepseek-flash');
 const restarted=new MobileRouter({...f.args,primaryModelSettings:()=>selected});await restarted.refreshPrimaryModelSelection();await restarted.restoreRoutingProfile();
 assert.equal(restarted.state.inputs['group-event'].text,'synthetic family event');
 assert.equal(restarted.state.sessionId,originalThread);
});

test('primary auto setting restores peer auto routing without copying primary work or conversations',async t=>{
 const f=profileFixture(t);let selected={mode:'manual',profile:{provider:'openai-15m',providerKind:'gateway',model:'k3',reasoningEffort:'high',serviceTierPreference:'default'}};
 const peer=new MobileRouter({...f.args,primaryModelSettings:()=>selected});await peer.refreshPrimaryModelSelection();await peer.restoreRoutingProfile();
 selected={mode:'auto',profile:{...selected.profile,model:'deepseek-flash'}};await peer.refreshPrimaryModelSelection();await peer.restoreRoutingProfile();
 assert.equal(peer.state.mode,'auto');assert.equal(f.runtime.model,'deepseek-flash');assert.equal(peer.state.manualProfile,undefined);
 assert.equal(peer.desiredProfile(f.runtime,{route:'work'}).model,peer.profiles.work.model);assert.deepEqual(peer.state.tasks,{});assert.deepEqual(peer.state.inputs,{});
});

test('peer control cannot create a divergent manual override or force interrupt the primary setting',async t=>{
 const f=profileFixture(t),selected={mode:'manual',profile:{provider:'openai-15m',providerKind:'gateway',model:'k3',reasoningEffort:'high',serviceTierPreference:'default'}};
 const peer=new MobileRouter({...f.args,primaryModelSettings:()=>selected});await peer.refreshPrimaryModelSelection();await peer.restoreRoutingProfile();
 const request={commandId:'synthetic-group-choice',state:'pending',mode:'manual',profile:{...selected.profile,model:'deepseek-flash'},sourceInputId:'group-control',force:true,notify:true};
 peer.state.requests[request.commandId]=request;await peer.applyModeRequest(request,f.runtime);
 assert.equal(request.state,'failed');assert.equal(request.failureReason,'primary-model-setting-owned');assert.equal(f.runtime.model,'k3');assert.equal(f.switches.length,1);
 await assert.rejects(peer.requestMode({commandId:'untrusted-manual',mode:'manual',profile:selected.profile,reason:'untrusted'}),/only from the owner/);
});

test('one native router keeps different recipients tasks and return destinations distinct',async t=>{
 const f=fixture(t),x={id:'x-work',text:'x work',kind:'owner',recipient_person_id:'person:xiaoguang',conversation_key:'private-x'},y={id:'y-work',text:'y work',kind:'owner',recipient_person_id:'person:yiyi',conversation_key:'private-y'};
 const tx=f.router.addTask(x),ty=f.router.addTask(y);
 assert.notEqual(tx.id,ty.id);assert.deepEqual(tx.inputIds,['x-work']);assert.deepEqual(ty.inputIds,['y-work']);
 assert.equal(f.router.currentTask(x).id,tx.id);assert.equal(f.router.currentTask(y).id,ty.id);
 const locked=f.router.applyRouteLock({id:'family-chat',text:'hello',kind:'owner',recipient_person_id:'group:family',conversation_key:'group-1'},{decision:'chat',intent:'chat',reason:'casual'});
 assert.equal(locked.task,null);assert.ok(!tx.contextInputIds?.includes('family-chat'));assert.ok(!ty.contextInputIds?.includes('family-chat'));
});

// Ordinary input is interpreted in KIN's turn. These cases invoke the same requestMode
// boundary used by the host tool after it supplies the current owner's identity.
async function ownerInput(f,id='choice',text='切换模型') {
 return f.router.dispatch({id,kind:'owner',text},async()=> 'new-turn');
}
async function choose(f,{id='choice',commandId='choose',profile={model:'k3'},force=false}={}) {
 if(!f.router.state.inputs[id])await ownerInput(f,id);
 return f.router.requestMode({commandId,sourceInputId:id,mode:'manual',profile,reason:'owner chose this model',notify:true,force},{ownerModelChoice:true});
}
async function takeWork(f,id='job') {
 if(!f.router.state.inputs[id])await ownerInput(f,id,'写一份报告');
 await f.router.requestMode({commandId:'take-'+id,sourceInputId:id,mode:'work',reason:'KIN accepts the work'});
 return f.router.currentTask();
}

test('ordinary requests, including model names and work, enter KIN once without a pre-chat classifier',async t=>{
 const f=profileFixture(t,{classify:()=>assert.fail('retired pre-chat classifier')});const prompts=[];
 for(const [id,text]of [['a','现在是什么模型'],['b','切换到 Kimi high'],['c','写代码'],['d','不用回复我，随便看看']])
  await f.router.dispatch({id,kind:'owner',text},async d=>{prompts.push(d);return 'new-turn';});
 assert.equal(prompts.length,4);assert.equal(f.classifications.length,0);assert.deepEqual(f.switches,[]);assert.equal(f.router.tasks().length,0);
 await f.router.dispatch({id:'b',kind:'owner',text:'切换到 Kimi high'},()=>assert.fail('duplicate native submission'));
 assert.equal(f.router.state.inputs.b.state,'accepted');assert.deepEqual(f.router.state.semanticPending,{});
});

test('a classifier outage cannot manufacture work or stop ordinary conversation',async t=>{
 const f=fixture(t,{classify:async()=>{throw Error('provider-http-503');}});let submitted=0;
 await f.router.dispatch({id:'hello',kind:'owner',text:'想随便说两句'},async()=>{submitted++;return 'new-turn';});
 assert.equal(submitted,1);assert.equal(f.router.tasks().length,0);assert.deepEqual(f.router.state.semanticPending,{});
});

for(const model of ['k3','gpt-6-astra'])test('main-model manual choice applies '+model+' once and persists across restart and later chat',async t=>{
 const f=profileFixture(t);await choose(f,{profile:{model}});await f.router.applyPendingMode();
 assert.equal(f.runtime.model,model);assert.equal(f.router.state.requests.choose.state,'applied');assert.equal(f.router.state.mode,'manual');
 const receipt=f.router.state.requests.choose;assert.equal(receipt.force,false);assert.equal(f.switches.length,1);
 const profile=structuredClone(f.router.state.manualProfile);const restored=new MobileRouter(f.args);f.router=restored;
 await ownerInput(f,'next','刚才说到哪儿啦');assert.equal(restored.state.inputs.next.model,model);assert.deepEqual(restored.state.manualProfile,profile);assert.equal(f.switches.length,1);
 const notices=[];await restored.flushNotices({send:async n=>{notices.push(n);return {state:'accepted',messageId:'sent-'+n.id};},lookup:async()=>null});
 assert.ok(notices.some(n=>n.sourceInputId==='choice'));assert.ok(notices.every(n=>n.text.length>0));
 await restored.flushNotices({send:()=>assert.fail('duplicate notice'),lookup:async()=>null});
});

test('a mode tool request waits for the active turn and only the latest pending model is applied',async t=>{
 const f=profileFixture(t);await ownerInput(f);f.runtime.active=true;f.runtime.nativeStatus='active';
 await choose(f,{commandId:'first',profile:{model:'k3'}});await f.router.applyPendingMode();assert.deepEqual(f.switches,[]);
 await choose(f,{commandId:'second',profile:{model:'gpt-6-astra'}});assert.equal(f.router.state.requests.first.state,'superseded');
 f.runtime.active=false;f.runtime.nativeStatus='idle';await f.router.applyPendingMode();
 assert.deepEqual(f.switches.map(s=>s.model),['gpt-6-astra']);assert.equal(f.router.state.requests.second.state,'applied');assert.equal(f.router.state.executionEpoch,0);
});

test('accepting and completing work preserves the explicitly selected manual profile',async t=>{
 const f=profileFixture(t);await choose(f);await f.router.applyPendingMode();const profile=structuredClone(f.router.state.manualProfile);
 const task=await takeWork(f);await f.router.applyPendingMode();assert.equal(task.status,'running');assert.deepEqual(task.inputIds,['job']);
 assert.equal(f.runtime.model,'k3');assert.equal(f.router.state.mode,'manual');assert.equal(f.switches.length,1);
 await f.router.observe('prompt-start',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,inputIds:['job']});
 await f.router.requestMode({commandId:'done',mode:'auto',completedTaskId:task.id,completedInputVersion:task.inputVersion,sourceInputId:'job',reason:'finished'});
 await f.router.observe('delivery',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,id:'report',state:'accepted',messageId:'m-report'});
 await f.router.observe('prompt-end',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,stopReason:'end_turn'});
 await f.router.reconcile();await f.router.applyPendingMode();assert.equal(task.status,'completed');assert.deepEqual(f.router.state.manualProfile,profile);assert.equal(f.runtime.model,'k3');assert.equal(f.switches.length,1);
});

test('accepting work in auto mode captures and later restores the full previous profile',async t=>{
 const initial={model:'gpt-6-astra',provider:'custom-gateway',providerKind:'native',reasoningEffort:'high',serviceTierPreference:'default'};
 const f=profileFixture(t,{initial});f.router.state.autoIdleProfile={...initial};const task=await takeWork(f);await f.router.applyPendingMode();
 assert.equal(f.runtime.model,'gpt-6.1-sol');assert.equal(f.router.state.autoReturnProfile.model,'gpt-6-astra');
 await f.router.observe('prompt-start',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,inputIds:['job']});
 await f.router.requestMode({commandId:'done',mode:'auto',completedTaskId:task.id,completedInputVersion:task.inputVersion,sourceInputId:'job',reason:'finished'});
 await f.router.observe('delivery',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,id:'report',state:'accepted',messageId:'m-report'});
 await f.router.observe('prompt-end',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,stopReason:'end_turn'});
 await f.router.reconcile();await f.router.applyPendingMode();assert.equal(task.status,'completed');
 assert.deepEqual([f.runtime.model,f.runtime.reasoningEffort,f.runtime.serviceTierPreference],['gpt-6-astra','high','default']);
});

test('same manual profile retains running work and accepted effects without interrupting or restarting',async t=>{
 const f=profileFixture(t,{forceSwitch:()=>assert.fail('no interruption')});await choose(f);await f.router.applyPendingMode();const task=await takeWork(f);await f.router.applyPendingMode();
 task.tools.t={status:'in_progress'};task.deliveries.sent={state:'accepted',messageId:'old-platform-id'};const prior=structuredClone(task);
 f.runtime.active=true;f.runtime.nativeStatus='active';await choose(f,{commandId:'same'});await f.router.applyPendingMode();
 assert.equal(f.router.state.requests.same.state,'applied');assert.deepEqual(task,prior);assert.equal(f.switches.length,1);assert.equal(f.router.state.executionEpoch,0);
});

test('a same-model effort change waits for idle and verifies the actual effort before announcing',async t=>{
 const f=profileFixture(t);await choose(f,{profile:{model:'gpt-6-sol',reasoningEffort:'medium',serviceTierPreference:'priority'}});await f.router.applyPendingMode();
 f.runtime.active=true;await choose(f,{commandId:'high',profile:{model:'gpt-6-sol',reasoningEffort:'high',serviceTierPreference:'priority'}});await f.router.applyPendingMode();assert.equal(f.runtime.reasoningEffort,'medium');
 f.runtime.active=false;await f.router.applyPendingMode();assert.equal(f.runtime.reasoningEffort,'high');assert.equal(f.router.state.requests.high.state,'applied');assert.equal(f.switches.length,2);
});

test('unsupported model or effort fails before any profile change or interruption',async t=>{
 for(const profile of [{model:'missing'},{model:'k3',reasoningEffort:'ultra'}]){
  const f=profileFixture(t,{forceSwitch:()=>assert.fail('unsupported choice must not interrupt')});await choose(f,{profile});await f.router.applyPendingMode();
  assert.equal(f.router.state.requests.choose.state,'failed');assert.equal(f.runtime.model,'deepseek-flash');assert.deepEqual(f.switches,[]);assert.equal(f.router.state.executionEpoch,0);
 }
});

test('provider history refusal leaves the original model, session and delivered effects intact',async t=>{
 const f=profileFixture(t,{preflightProfile:async()=>{throw Object.assign(Error('incompatible history'),{code:'KIMI_HISTORY_INCOMPATIBLE',beforeMutation:true});}});
 await choose(f);await f.router.applyPendingMode();assert.equal(f.router.state.requests.choose.state,'failed');assert.equal(f.runtime.model,'deepseek-flash');assert.equal(f.router.sessionId,'synthetic');assert.deepEqual(f.switches,[]);
});

test('a failed switch acknowledgement is kept uncertain and is not announced as successful',async t=>{
 const f=profileFixture(t);await choose(f);f.router.switchModel=async()=>{throw Error('connection lost after switch request');};
 await f.router.applyPendingMode();assert.equal(f.router.state.requests.choose.state,'pending');assert.equal(f.router.state.transition.state,'unconfirmed');
 assert.equal(f.router.state.requests.choose.waitingReason,'switch-unconfirmed');assert.equal(f.router.state.mode,'auto');
 assert.equal(Object.values(f.router.state.notices).some(n=>n.kind==='model-switched'&&n.target==='k3'),false);
});

test('a temporary handoff failure retries the original request after its delay',async t=>{
 const f=profileFixture(t);await choose(f);const original=f.router.switchModel;let calls=0;
 f.router.switchModel=async(...args)=>{if(++calls===1)throw Object.assign(Error('busy handoff'),{code:'HANDOFF_NOT_COMMITTED'});return original(...args);};
 await f.router.applyPendingMode();const r=f.router.state.requests.choose;assert.equal(r.state,'pending');assert.equal(r.waitingReason,'HANDOFF_NOT_COMMITTED');assert.ok(r.retryAt>r.failedAt);
 await f.router.applyPendingMode();assert.equal(calls,1);r.retryAt=0;await f.router.applyPendingMode();assert.equal(calls,2);assert.equal(r.state,'applied');assert.equal(f.runtime.model,'k3');
});

test('unknown notification reconciles the same platform ID after restart without resending',async t=>{
 const f=profileFixture(t);await choose(f);await f.router.applyPendingMode();let sends=0;
 await f.router.flushNotices({send:async()=>{sends++;return {state:'unconfirmed'};},lookup:async()=>({state:'unconfirmed'})});
 const notices=Object.values(f.router.state.notices).filter(n=>n.state==='unconfirmed');assert.ok(notices.length>0);const sent=sends;
 const restarted=new MobileRouter(f.args);for(const n of Object.values(restarted.state.notices))n.nextAttemptAt=0;
 await restarted.flushNotices({send:()=>assert.fail('never resend unknown outcome'),lookup:async id=>({state:'accepted',messageId:'reconciled-'+id})});
 assert.equal(sends,sent);assert.ok(Object.values(restarted.state.notices).every(n=>n.state==='accepted'));
});

test('a frozen model tool request stays pending and applies under its original ID after thaw',async t=>{
 const f=profileFixture(t);await ownerInput(f);await f.router.freezeDispatch('release');await choose(f);await f.router.applyPendingMode();assert.deepEqual(f.switches,[]);assert.equal(f.router.state.requests.choose.state,'pending');
 await f.router.thawDispatch('released');await f.router.applyPendingMode();assert.equal(f.router.state.requests.choose.state,'applied');assert.equal(f.switches.length,1);
});

test('a literal owner work command can replace a manual pin while task acceptance cannot',async t=>{
 const f=profileFixture(t);await choose(f);await f.router.applyPendingMode();
 const out=await f.router.dispatch({id:'literal-work',kind:'owner',text:'/mode work'},()=>assert.fail('literal control is handled by host'));
 assert.equal(out.route,'host-control');assert.equal(f.router.state.mode,'work');assert.equal(f.runtime.model,'gpt-6.1-sol');
});
