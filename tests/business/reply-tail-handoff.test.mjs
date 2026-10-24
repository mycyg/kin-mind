import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {TransportManifests} from '../../adapters/transport-manifest.mjs';
import {ReplyTail} from '../../adapters/reply-tail.mjs';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {inputSummary} from '../../adapters/input-ledger.mjs';

async function fixture(t,{failAnswer=false}={}) {
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-tail-handoff-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 let now=100000,fail=failAnswer;const events=[],sends=[],receipts=new Map();
 const clock=()=>now;
 const router=new MobileRouter({file:path.join(root,'router.json'),sessionId:'synthetic',now:clock,inspect:async()=>({known:true,nativeStatus:'idle',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0})});
 router.state.inputs.old={id:'old',kind:'owner',state:'accepted',at:now,turnStartedAt:now,turnEndedAt:now+1,delivered:4};
 router.state.inputs.next={id:'next',kind:'owner',state:'accepted',at:now+10,turnStartedAt:now+10,turnEndedAt:now+11};
 const options={directory:path.join(root,'manifests'),clock,sleep:async()=>{},receipt:id=>receipts.get(id),retry:{baseMs:10},
  onAnswered:async e=>{if(fail)throw Error('synthetic ledger unavailable');events.push(e);await router.observe(e.state==='accepted'?'reply-complete':'reply-choice',e);}};
 let store=new TransportManifests(options);let tail=new ReplyTail({manifests:store,clock});
 const entries=Array.from({length:6},(_,i)=>({request:{reply_id:'old',draft_id:'draft-'+i,text:'synthetic '+i},delivery:{id:'bubble-'+i,memoryBatchId:'reply-synthetic',kind:'reply',text:'synthetic '+i}}));
 const m=store.createDraft({entries,ownerEpoch:'synthetic'});
 const transport={receipt:id=>receipts.get(id),send:async d=>{sends.push(d.id);const r={state:'accepted',messageId:'mock-'+d.id};receipts.set(d.id,r);now++;return r;}};
 const result=await store.run(m.group_id,{review:async()=>({state:'ready',checked:entries.map(e=>e.request)}),transport,guard:async()=>sends.length<4?'send':{action:'interrupt',reason:'new-owner-input',inputId:'next'}});
 assert.equal(result.manifest.state,'interrupted');await tail.after(result);
 assert.deepEqual(store.read(m.group_id).bubbles.map(b=>b.state),['accepted','accepted','accepted','accepted','canceled','canceled']);
 return {router,events,sends,receipts,group:m.group_id,get store(){return store;},get tail(){return tail;},advance:ms=>now+=ms,allowAnswer:()=>fail=false,
  restart:()=>{store=new TransportManifests(options);tail=new ReplyTail({manifests:store,clock});},transport};
}

test('accepted owner handoff settles old input, preserves receipts and leaves successor failure reportable',async t=>{
 const f=await fixture(t);assert.equal(f.store.answerDue(f.store.read(f.group)),null,'owed is not handed');
 assert.equal(f.events.length,0);assert.equal(f.tail.owed().length,1);
 await f.tail.handed({groups:[f.group],inputId:'next'});
 assert.equal(f.router.state.inputs.old.answer.state,'merged');assert.equal(f.router.state.inputs.old.answer.mergedInto,'next');
 assert.equal(f.router.state.inputs.old.delivered,4,'never label the two unsent words as delivered');
 assert.equal(inputSummary(f.router.state.inputs.old),'answered');
 f.restart();await f.tail.handed({groups:[f.group],inputId:'next'});await f.store.run(f.group,{transport:f.transport});
 assert.equal(f.events.length,1);assert.equal(f.sends.length,4,'accepted prefix and canceled tail are never replayed');
 await f.router.observe('reply-complete',{inputId:'next'});f.advance(11*60000);
 const notices=[];await f.router.watch({notifyOwner:async(k,id)=>{notices.push([k,id]);return {state:'accepted'};}});assert.deepEqual(notices,[]);
 // A separate next-input failure still gets its own failure notice.
 delete f.router.state.inputs.next.answer;delete f.router.state.inputs.next.settledAt;f.router.state.inputs.next.nativeFailure={at:100000};
 await f.router.watch({notifyOwner:async(k,id)=>{notices.push([k,id]);return {state:'accepted'};}});
 assert.deepEqual(notices,[['stopped','next']]);
});

test('ledger failure after handoff survives restart and retries only its durable answer side effect',async t=>{
 const f=await fixture(t,{failAnswer:true});await f.tail.handed({groups:[f.group],inputId:'next'});
 assert.equal(f.router.state.inputs.old.answer,undefined);assert.equal(f.store.isLive(f.group),true);
 f.restart();f.allowAnswer();f.advance(100);await f.store.resumeDue({transport:f.transport});
 assert.equal(f.router.state.inputs.old.answer.state,'merged');assert.equal(f.sends.length,4);assert.equal(f.events.length,1);
});

test('failure, unknown submission, work, and missing/same-input handoff cannot be disguised as merged',async t=>{
 const f=await fixture(t);await f.tail.handed({groups:[f.group],inputId:'next'});
 const base=f.store.read(f.group);delete base.answered;
 for(const edit of [m=>delete m.tail_handed,m=>m.tail_handed.input_id='old',m=>m.tail_handed.input_id=null,m=>m.answered_input_ids=['old','next'],m=>m.work=true,m=>m.reason='transport-unavailable',m=>m.bubbles[4].state='undeliverable',m=>m.bubbles[4].fragments[0].state='unknown',m=>m.bubbles[4].fragments[0].attempts=1,m=>m.bubbles[4].fragments[0].receipt={state:'rejected'},m=>m.tail_owed={items:['remaining']}]) {
  const m=structuredClone(base);edit(m);assert.equal(f.store.answerDue(m),null);
 }
});

test('a genuinely unsent fragment retries under its original ID without replaying the accepted prefix',async t=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-tail-retry-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 let now=1000,offline=true;const calls=[],receipts=new Map(),answers=[];
 const store=new TransportManifests({directory:root,clock:()=>now,sleep:async()=>{},retry:{baseMs:10},onAnswered:e=>answers.push(e)});
 const entries=['prefix','tail'].map(id=>({request:{reply_id:'retry-owner',text:id},delivery:{id,memoryBatchId:'retry-group',kind:'reply',text:id}}));
 const m=store.createDraft({entries,ownerEpoch:1});
 const transport={receipt:id=>receipts.get(id),send:async d=>{calls.push(d.id);if(d.id.includes('tail')&&offline)return {state:'not-submitted',submissionStarted:false};const r={state:'accepted',messageId:'mock-'+d.id};receipts.set(d.id,r);return r;}};
 await store.run(m.group_id,{review:async()=>({state:'ready',checked:entries.map(e=>e.request)}),transport});
 assert.equal(answers.length,0);assert.equal(store.read(m.group_id).bubbles[1].fragments[0].state,'unsent');
 offline=false;now+=100;await store.resumeDue({transport});
 assert.deepEqual(calls,['kin-frag-prefix-0','kin-frag-tail-0','kin-frag-tail-0']);
 assert.equal(answers[0].state,'accepted');assert.equal(store.read(m.group_id).state,'accepted');
});
