import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,HOT_LIMITS} from '../../adapters/mobile-router.mjs';

const LIMITS={inputs:6,internal:2};

function fixture(t,options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-router-hot-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const clock={now:Date.parse('2026-09-01T00:00:00Z')};
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',
    active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const args={file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>++clock.now,
    classify:async({text})=>({route:/work/.test(text)?'work':'chat',reason:'synthetic'}),
    switchModel:async(model,profile={model})=>{Object.assign(runtime,{model,reasoningEffort:profile.reasoningEffort??runtime.reasoningEffort,serviceTierPreference:profile.serviceTierPreference??runtime.serviceTierPreference});return {...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},...options};
  return {root,clock,runtime,args,router:new MobileRouter(args)};
}
async function answeredChat(router,id) {
  await router.dispatch({id,kind:'owner',text:'hello '+id},async()=> 'new-turn');
  await router.observe('prompt-start',{taskId:null,inputVersion:null,inputIds:[id]});
  await router.observe('prompt-end',{taskId:null,inputVersion:null,stopReason:'end_turn'});
  await router.observe('reply-complete',{inputId:id});
}

test('a turn with no task is ordinary: never a late event, saved only when something was learned (AD1-07)',async t=>{
  const f=fixture(t);
  await answeredChat(f.router,'chat-1');
  assert.equal((f.router.state.lateEvents??[]).length,0,'a chat turn is not a late event');
  assert.equal(f.router.state.inputs['chat-1'].turnEndedAt>0,true,'its facts are still recorded on the input');
  const revision=f.router.state.revision;
  await f.router.observe('tool',{taskId:null,inputVersion:null,id:'lookup',status:'completed'});
  await f.router.observe('prompt-end',{taskId:null,inputVersion:null,stopReason:'end_turn'});
  assert.equal(f.router.state.revision,revision,'nothing new, nothing written');
});

test('settled records beyond the hot tail move to the monthly archive, and a replay of one is refused (AD1-08)',async t=>{
  const f=fixture(t,{hotLimits:LIMITS});
  assert.equal(HOT_LIMITS.inputs,200,'the default keeps the last 200 settled owner inputs');
  const count=LIMITS.inputs+3;
  for(let i=0;i<count;i++)await answeredChat(f.router,'m'+i);
  await f.router.reconcile();
  const owner=Object.values(f.router.state.inputs).filter(r=>(r.kind??'owner')==='owner');
  assert.equal(owner.length,LIMITS.inputs);
  assert.equal(f.router.state.inputs.m0,undefined);assert.ok(f.router.state.inputs['m'+(count-1)]);
  const archive=path.join(f.root,'archive','router-2026-09.jsonl');
  const lines=fs.readFileSync(archive,'utf8').trim().split('\n').map(line=>JSON.parse(line));
  assert.deepEqual(lines.filter(l=>l.kind==='input').map(l=>l.id),['m0','m1','m2'],'moved, oldest first, never deleted');
  assert.equal(f.router.summary().archived,3);
  assert.equal((await f.router.dispatch({id:'m0',kind:'owner',text:'hello m0'},async()=>assert.fail('an archived input is never re-run'))).route,'deduplicated');
  // The state file is compact, and a restart still knows the archived ids.
  assert.equal(fs.readFileSync(f.args.file,'utf8').includes('\n  '),false);
  const restarted=new MobileRouter(f.args);
  assert.equal(restarted.archivedIds.has('m1'),true);
  assert.equal(restarted.unsettledInputs({received:[{id:'m2',at:1}]}).length,0,'an archived id in the inbox is not unsettled');
});

test('an open task keeps its inputs and a bounded history (AD1-08)',async t=>{
  const f=fixture(t,{hotLimits:LIMITS});
  await f.router.dispatch({id:'work-request',kind:'owner',text:'work please'},async()=> 'new-turn');
  const task=f.router.currentTask();
  for(let i=0;i<LIMITS.inputs+2;i++)await answeredChat(f.router,'c'+i);
  await f.router.reconcile();
  assert.ok(f.router.state.inputs['work-request'],'an input an open task names stays hot');
  for(let i=0;i<80;i++)await f.router.observe('tool',{taskId:task.id,inputVersion:task.inputVersion,turnFence:task.executionEpoch+1,id:'late-'+i,status:'completed'});
  assert.ok(Object.keys(f.router.state.tasks[task.id].toolHistory).length<=64);
});
