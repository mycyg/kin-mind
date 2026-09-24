// CR2-INT-01: a proactive contact's physical sends pass the router's one gate. Each bubble takes
// {kind:'contact', id, channel} right before it is marked begun and holds it until its send
// returns; a freeze leaves the bubble in its group, unsent and not submitted, with nothing counted.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {createContactBatch} from '../../adapters/contact-batch.mjs';

function world(t,sender) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-contact-gate-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',reasoningEffort:'high',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const router=new MobileRouter({file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>Date.parse('2026-09-25T00:00:00Z'),
    classify:async()=>({route:'chat',reason:'synthetic'}),switchModel:async()=>({...runtime}),waitForIdle:async()=>{throw Error('waiting');}});
  const journal=new Map(),seen=[];
  const batch=createContactBatch({read:id=>structuredClone(journal.get(id)),write:(id,value)=>journal.set(id,structuredClone(value)),
    send:async request=>{seen.push({id:request.id,inFlight:router.activityList().map(a=>[a.kind,a.id,a.channel])});return sender(request,router);}});
  return {router,runtime,journal,seen,batch,gate:spec=>router.beginActivity(spec)};
}
const accepted=request=>({state:'accepted',messageId:'m-'+request.id});

test('a frozen gate leaves the group unsent and uncounted; after the thaw each send holds the gate until it returns (CR2-INT-01)',async t=>{
  const w=world(t,accepted);
  await w.router.freezeDispatch('release');
  const held=await w.batch({id:'contact-1',bubbles:['第一句','第二句'],channel:'feishu',gate:w.gate});
  assert.deepEqual([held.state,held.reason,held.acceptedBubbles],['pending','dispatch-frozen',0]);
  assert.equal(w.seen.length,0,'nothing reached the channel sender');
  const stored=w.journal.get('contact-1');
  assert.deepEqual(stored.items.map(item=>[item.state,item.submission??null,item.restarts??0]),[['unsent','not-submitted',0],['unsent',null,0]]);
  assert.equal(stored.deliveryStarted,undefined,'no bubble began');
  assert.deepEqual(w.router.activityList(),[],'a refused send holds nothing');
  await w.router.thawDispatch('done');
  const done=await w.batch({id:'contact-1',gate:w.gate});
  assert.deepEqual([done.state,done.reason,done.acceptedBubbles],['accepted',undefined,2]);
  assert.deepEqual(w.seen.map(s=>s.inFlight),stored.items.map(item=>[['contact',item.id,'feishu']]),'each send is in flight, under its own id, while it runs');
  assert.deepEqual(w.router.activityList(),[],'released when it returned');
  assert.equal(w.journal.get('contact-1').heldAtGate,undefined);
});

test('a freeze between two bubbles keeps what was sent and holds the rest; a failed send still releases the gate (CR2-INT-01)',async t=>{
  let first=true;
  const w=world(t,async(request,router)=>{if(first){first=false;await router.freezeDispatch('mid-group');}return accepted(request);});
  const partial=await w.batch({id:'contact-2',bubbles:['一','二'],channel:'wechat',gate:w.gate});
  assert.deepEqual([partial.state,partial.reason,partial.acceptedBubbles],['pending','dispatch-frozen',1]);
  assert.deepEqual(w.journal.get('contact-2').items.map(item=>item.state),['accepted','unsent']);
  assert.deepEqual(w.router.activityList(),[]);
  await w.router.thawDispatch('done');
  assert.equal((await w.batch({id:'contact-2',gate:w.gate})).state,'accepted');
  const failing=world(t,async()=>{throw Error('transport down');});
  const unknown=await failing.batch({id:'contact-3',bubbles:['三'],channel:'feishu',gate:failing.gate});
  assert.equal(unknown.state,'unconfirmed');
  assert.deepEqual(failing.router.activityList(),[],'released in finally');
});
