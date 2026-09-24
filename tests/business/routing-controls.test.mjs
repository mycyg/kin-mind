import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {MobileRouter,HANDOFF_SOURCE_MAX_HOURS} from '../../adapters/mobile-router.mjs';
import {runtimeReply,displayName} from '../../adapters/mobile-controls.mjs';

const sha=value=>createHash('sha256').update(String(value)).digest('hex');
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

test('a manual profile comes only from the owner or a verified reclassification (H1-04)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'owner-said-hi',kind:'owner',text:'hi'},async()=> 'new-turn');
  assert.throws(()=>f.router.recordModeRequest({commandId:'pin-manual',mode:'manual',reason:'model asked',profile:{model:'gpt-6-sol',reasoningEffort:'high'}}),/only from the owner/);
  assert.throws(()=>f.router.recordModeRequest({commandId:'pin-manual-2',mode:'manual',reason:'model asked',sourceInputId:'owner-said-hi',profile:{model:'gpt-6-sol',reasoningEffort:'high'}}),/only from the owner/,
    'naming an owner message is not the owner asking');
  assert.equal(f.router.state.requests['pin-manual'],undefined);assert.equal(f.router.state.configRevision,0,'a refused request changes nothing');
  // The owner's own words still pin a profile.
  const g=fixture(t,{classify:async()=>({route:'control',control:'manual',profile:{model:'gpt-6-sol',reasoningEffort:'high',serviceTierPreference:'fast'},force:false,reason:'owner choice'})});
  await g.router.dispatch({id:'owner-manual',kind:'owner',text:'用 sol high'},async()=>assert.fail('a control is not a prompt'));
  assert.equal(g.router.state.requests['owner-mode:owner-manual'].mode,'manual');
});

test('a reclassification is fenced on configuration and later owner inputs, not on bookkeeping (H1-07)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'switch-please',kind:'owner',text:'切到 sol high'},async()=> 'new-turn');
  const source=f.router.state.inputs['switch-please'];
  const basis=f.router.reclassificationBasis('switch-please');
  // Tool and delivery bookkeeping save the state; they do not move the basis.
  await f.router.observe('tool',{id:'bookkeeping',status:'completed'});
  await f.router.observe('delivery',{id:'reply',state:'accepted',messageId:'m1',sourceInputId:'switch-please'});
  assert.equal(f.router.reclassificationBasis('switch-please'),basis);
  const evidence={id:'evidence',version:1,sourceSha256:sha('source'),acceptanceSha256:sha('acceptance'),ownerBindingSha256:sha('binding'),
    actualSessionId:'synthetic',conversationId:'conversation',generation:1};
  const decision={route:'control',control:'work',force:false,reason:'the owner asked for the work profile'};
  const result=await f.router.reclassifyAcceptedControl({commandId:'recover-1',sourceInputId:'switch-please',sourceHash:source.hash,expectedRevision:basis,evidence,decision});
  assert.equal(result.receipt.state,'recorded');assert.equal(result.request.mode,'work');
  // A later owner message moves the basis for another source.
  const g=fixture(t);
  await g.router.dispatch({id:'switch-please',kind:'owner',text:'切到 sol high'},async()=> 'new-turn');
  const before=g.router.reclassificationBasis('switch-please');
  await g.router.dispatch({id:'never-mind',kind:'owner',text:'算了不用换'},async()=> 'new-turn');
  assert.notEqual(g.router.reclassificationBasis('switch-please'),before);
  await assert.rejects(g.router.reclassifyAcceptedControl({commandId:'recover-2',sourceInputId:'switch-please',sourceHash:g.router.state.inputs['switch-please'].hash,
    expectedRevision:before,evidence,decision}),/revision changed/);
});

test('an old owner message cannot be reclassified into a control (H1-07)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'old-switch',kind:'owner',text:'切到 sol high'},async()=> 'new-turn');
  f.clock.now+=25*3600000;
  assert.throws(()=>f.router.assertReclassifiable(f.router.state.inputs['old-switch']),/too old/);
  const evidence={id:'evidence',version:1,sourceSha256:sha('source'),acceptanceSha256:sha('acceptance'),ownerBindingSha256:sha('binding'),
    actualSessionId:'synthetic',conversationId:'conversation',generation:1};
  await assert.rejects(f.router.reclassifyAcceptedControl({commandId:'recover-old',sourceInputId:'old-switch',sourceHash:f.router.state.inputs['old-switch'].hash,
    expectedRevision:f.router.reclassificationBasis('old-switch'),evidence,decision:{route:'control',control:'work',force:false,reason:'late'}}),/too old/);
});

test('the host\'s configured profiles route work and chat; display names come from the catalog (AD1-15)',async t=>{
  const profiles={work:{model:'gpt-6-astra',reasoningEffort:'high',serviceTierPreference:'default'},chat:{model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'}};
  const f=fixture(t,{profiles,classify:async()=>({route:'work',reason:'a real request'})});
  await f.router.dispatch({id:'do-work',kind:'owner',text:'please write the report'},async()=> 'new-turn');
  assert.deepEqual(f.switches,['gpt-6-astra']);
  assert.deepEqual((await f.router.readRuntime()).defaults.work,profiles.work);
  f.router.rememberModelNames([{id:'gpt-6-astra',aliases:['Astra (catalog)']}]);
  assert.equal(runtimeReply({actual:{model:'gpt-6-astra',reasoningEffort:'high',verified:true},tasks:[],mode:'auto'},{names:f.router.modelNames}).includes('Astra (catalog)'),true);
  assert.equal(displayName('unknown-model'),'unknown-model');
});

test('a failed force leaves no force in progress, so nothing keeps its epoch suppressed (H1-01)',async t=>{
  let inProgress=null;
  const f=fixture(t,{classify:async()=>({route:'control',control:'work',force:true,reason:'owner forces the work profile'})});
  f.args.forceSwitch=null;
  const router=new MobileRouter({...f.args,forceSwitch:async()=>{inProgress=router.forceInProgress();return {state:'failed',reason:'native-cancel-request-failed'};}});
  Object.assign(f.runtime,{active:true,nativeStatus:'active'});
  await router.dispatch({id:'force-now',kind:'owner',text:'立刻切到工作模式'},async()=>assert.fail('a control is not a prompt'));
  assert.equal(inProgress,true,'while the host is interrupting, the force is in progress');
  assert.equal(router.state.requests['owner-mode:force-now'].state,'failed');
  assert.equal(router.forceInProgress(),false);
  assert.equal(router.state.executionEpoch,0,'the epoch never advanced');
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
