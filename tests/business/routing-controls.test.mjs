import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,HANDOFF_SOURCE_MAX_HOURS} from '../../adapters/mobile-router.mjs';
import {runtimeReply,displayName} from '../../adapters/mobile-controls.mjs';

function fixture(t,{profiles=null,classify=null}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-routing-controls-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const clock={now:1000};
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0,
    model:'deepseek-flash',modelProvider:'gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off'};
  const switches=[];
  const args={file:path.join(root,'router.json'),sessionId:'synthetic',binding:{conversationId:'conversation',generation:1,nativeSessionId:'synthetic'},profiles,
    inspect:async()=>({...runtime}),classify:async input=>classify?classify(input):{route:'chat',reason:'casual'},
    switchModel:async(model,profile)=>{switches.push(model);Object.assign(runtime,{model,reasoningEffort:profile?.reasoningEffort??runtime.reasoningEffort,serviceTierPreference:profile?.serviceTierPreference??runtime.serviceTierPreference});return {...runtime};},
    waitForIdle:async()=>{throw Error('must not wait');},now:()=>++clock.now};
  return {router:new MobileRouter(args),runtime,clock,switches,args};
}

test('a mode request is credited only to the source its caller names (H1-04)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'owner-said-hi',kind:'owner',text:'hi'},async()=> 'new-turn');
  const request=await f.router.requestMode({commandId:'maintenance-check',mode:'work',reason:'maintenance verification'});
  assert.equal(request.sourceInputId,null,'a maintenance call is never put on the owner\'s latest message');
  const notices=Object.values(f.router.state.notices).filter(n=>n.sourceInputId==='owner-said-hi');
  assert.equal(notices.length,0);
});

test('a desktop hand-off is authorized by an accepted owner input, read from the live ledger, within a day (H3-20)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'owner-asks',kind:'owner',text:'让桌面整理一下报告'},async()=> 'new-turn');
  const eligible=f.router.handoffSource('owner-asks');
  assert.equal(eligible.state,'eligible','accepted a moment ago, it is already in the ledger');
  assert.equal(eligible.maxAgeMs,HANDOFF_SOURCE_MAX_HOURS*3600000);
  await f.router.dispatch({id:'owner-queued',kind:'owner',text:'还有这个'},async()=>({route:'new-turn',queued:true}));
  assert.equal(f.router.handoffSource('owner-queued').reason,'source-not-accepted','queued is not yet delivered to Kin');
  f.router.state.inputs.internal={id:'internal',kind:'work-result',state:'accepted',at:f.clock.now};
  assert.equal(f.router.handoffSource('internal').reason,'source-not-owner');
  assert.equal(f.router.handoffSource('never-seen').reason,'source-not-found');
  f.router.archivedIds.add('long-ago');
  assert.equal(f.router.handoffSource('long-ago').reason,'source-too-old');
  f.clock.now+=25*3600000;
  assert.equal(f.router.handoffSource('owner-asks').reason,'source-too-old');
  f.router.state.config.handoffSourceMaxHours=48;
  assert.equal(f.router.handoffSource('owner-asks').state,'eligible','the limit is the configured one');
});


test('manual choices require the current owner model context and apply only after the turn',async t=>{
 const f=fixture(t);await f.router.dispatch({id:'choice',kind:'owner',text:'用 sol high'},async()=> 'new-turn');
 const request={commandId:'manual',sourceInputId:'choice',mode:'manual',profile:{model:'gpt-6-sol',reasoningEffort:'high'},reason:'owner choice'};
 await assert.rejects(f.router.requestMode(request),/only from the owner/);
 await f.router.requestMode(request,{ownerModelChoice:true});
 assert.equal(f.router.state.requests.manual.state,'pending');assert.equal(f.runtime.model,'deepseek-flash');
 await f.router.applyPendingMode();assert.equal(f.router.state.requests.manual.state,'applied');assert.equal(f.runtime.model,'gpt-6-sol');
});

test('the retired historical classifier cannot manufacture a mode change',async t=>{
 const f=fixture(t,{classify:()=>assert.fail('no historical classifier')});
 await f.router.dispatch({id:'old',kind:'owner',text:'切到 sol high'},async()=> 'new-turn');
 f.clock.now+=25*3600000;const before=f.router.snapshot();
 assert.equal(f.router.reclassifyAcceptedControl,undefined);assert.deepEqual(f.router.snapshot(),before);assert.deepEqual(f.switches,[]);
});

test('configured work profile applies when KIN accepts the task and catalog names remain visible',async t=>{
 const profiles={work:{model:'gpt-6-astra',reasoningEffort:'high',serviceTierPreference:'default'},chat:{model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'}};
 const f=fixture(t,{profiles,classify:()=>assert.fail('no pre-chat classification')});
 await f.router.dispatch({id:'do-work',kind:'owner',text:'please write the report'},async()=> 'new-turn');assert.deepEqual(f.switches,[]);
 await f.router.requestMode({commandId:'accept',mode:'work',sourceInputId:'do-work',reason:'KIN accepts'});await f.router.applyPendingMode();
 assert.deepEqual(f.switches,['gpt-6-astra']);assert.deepEqual((await f.router.readRuntime()).defaults.work,profiles.work);
 assert.deepEqual(f.router.currentTask().inputIds,['do-work']);
 f.router.rememberModelNames([{id:'gpt-6-astra',aliases:['Astra (catalog)']}]);
 assert.ok(runtimeReply({actual:{model:'gpt-6-astra',reasoningEffort:'high',verified:true},tasks:[],mode:'auto'},{names:f.router.modelNames}).includes('Astra (catalog)'));
 assert.equal(displayName('unknown-model'),'unknown-model');
});

test('a failed literal force leaves the original epoch active',async t=>{
 let during=null;const f=fixture(t);
 const router=new MobileRouter({...f.args,forceSwitch:async()=>{during=router.forceInProgress();return {state:'failed',reason:'native-cancel-request-failed'};}});
 Object.assign(f.runtime,{active:true,nativeStatus:'active'});
 await router.dispatch({id:'force-now',kind:'owner',text:'/mode work'},()=>assert.fail('literal command is handled by host'));
 assert.equal(during,true);assert.equal(router.state.requests['owner-mode:force-now'].state,'failed');assert.equal(router.forceInProgress(),false);assert.equal(router.state.executionEpoch,0);
});
