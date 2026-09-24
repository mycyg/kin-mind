// CR2-MIND-01: the mind's own model work on its store (an appraisal, a memory enrichment, a memory
// warm-up), each in a process of its own, passes the router's one gate. A freeze refuses it and the
// drain lists it until it is released; it holds no turn, no model switch and no dispatch.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,ACTIVITY_KINDS} from '../../adapters/mobile-router.mjs';

const STORE=['appraisal','enrichment','memory-prep'];

test('the mind\'s store work is refused by a freeze and counted by the drain, and holds nothing else (CR2-MIND-01)',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-mind-workers-gate-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',reasoningEffort:'high',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const router=new MobileRouter({file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>Date.parse('2026-09-25T00:00:00Z'),
    classify:async()=>({route:'chat',reason:'synthetic'}),switchModel:async()=>({...runtime}),waitForIdle:async()=>{throw Error('waiting');}});
  for(const kind of [...STORE,'contact'])assert.ok(ACTIVITY_KINDS.includes(kind),kind);
  await router.freezeDispatch('release');
  for(const kind of STORE)assert.deepEqual(router.beginActivity({kind,id:kind}),{ok:false,reason:'frozen'});
  assert.deepEqual(router.activityList(),[],'nothing refused is in flight');
  await router.thawDispatch('done');
  const held=STORE.map(kind=>router.beginActivity({kind,id:kind}));
  assert.deepEqual(router.activityList().map(a=>[a.kind,a.id]),STORE.map(kind=>[kind,kind]),'the drain sees each until it is released');
  for(const flags of [{},{owner:true},{internal:true},{assessment:true,internal:true}])
    assert.equal(router.busy(runtime,flags),false,'no turn, switch or dispatch waits for it: '+JSON.stringify(flags));
  // Beside them the other kinds hold what they held before.
  const creation=router.beginActivity({kind:'creation',id:'c1'}),contact=router.beginActivity({kind:'contact',id:'b1',channel:'feishu'});
  assert.equal(router.busy(runtime,{owner:true}),true,'a send in flight holds the owner\'s dispatch');
  contact.release();
  assert.deepEqual([router.busy(runtime),router.busy(runtime,{owner:true}),router.busy(runtime,{internal:true})],[true,false,false]);
  creation.release();
  for(const one of held)assert.equal(one.release(),true);
  assert.deepEqual(router.activityList(),[]);
});
