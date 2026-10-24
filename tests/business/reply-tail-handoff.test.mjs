import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {TransportManifests} from '../../adapters/transport-manifest.mjs';
import {ReplyTail} from '../../adapters/reply-tail.mjs';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {inputSummary,inputSettled} from '../../adapters/input-ledger.mjs';

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
 assert.equal(f.router.state.inputs.old.answer,undefined,'native acceptance alone does not settle the old input');
 assert.equal(f.router.state.inputs.old.replyHandoff.inputId,'next');
 await f.router.observe('reply-complete',{inputId:'next'});
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
 assert.equal(f.router.state.inputs.old.answer,undefined);
 await f.router.observe('reply-complete',{inputId:'next'});
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


async function formatHandoff(t) {
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-format-handoff-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 let now=1000;const notices=[],events=[];
 const args={file:path.join(root,'router.json'),sessionId:'synthetic',now:()=>now};let router=new MobileRouter(args);
 for(const id of ['old','next'])router.state.inputs[id]={id,kind:'owner',intent:'chat',state:'accepted',at:now,turnStartedAt:now,turnEndedAt:now+1};
 router.save('synthetic-inputs');
 const store=new TransportManifests({directory:path.join(root,'manifests'),clock:()=>now,sleep:async()=>{},
  onAnswered:async event=>{events.push(event);return router.observe(event.state==='accepted'?'reply-complete':'reply-choice',event);}});
 const tail=new ReplyTail({manifests:store,clock:()=>now});
 const entry={request:{reply_id:'old',draft_id:'failed-draft',text:'format withheld'},delivery:{id:'failed-bubble',memoryBatchId:'failed-group',kind:'reply',text:'format withheld'}};
 const group=store.createDraft({entries:[entry],ownerEpoch:1});
 const result=await store.run(group.group_id,{review:async()=>({state:'refused',reason:'private-note-bubbles-invalid'}),transport:{send:()=>assert.fail('invalid output must never reach a transport')}});
 await tail.after(result);assert.equal(tail.owed().length,1);
 await tail.handed({groups:[group.group_id],inputId:'next'});
 assert.equal(router.state.inputs.old.answer,undefined);
 const notifyOwner=async(kind,id)=>{notices.push([kind,id]);return {state:'accepted',messageId:'notice-'+notices.length};};
 const replyOutcomes=[{groupId:group.group_id,inputIds:['old'],state:'format-failed'}];
 return {get router(){return router;},store,events,notices,advance:()=>now+=11*60000,
  restart(){router=new MobileRouter(args);},watch:()=>router.watch({notifyOwner,replyOutcomes})};
}

test('format failure handed to a later successful chat settles only after that answer and emits no delayed old notice',async t=>{
 const f=await formatHandoff(t);f.restart();assert.equal(f.router.state.inputs.old.answer,undefined);
 await f.router.observe('reply-complete',{inputId:'next'});
 f.advance();await f.watch();await f.watch();assert.deepEqual(f.notices,[],'a successful successor does not get followed by an obsolete failure notice');
 assert.equal(f.router.state.inputs.old.answer?.state,'merged');assert.equal(f.router.state.inputs.old.answer.mergedInto,'next');
 assert.equal(f.router.state.inputs.old.delivered,undefined,'merged never invents platform delivery');
 assert.equal(f.store.read('failed-group').bubbles[0].fragments.length,0);
});

test('failed successor reports the handed obligation once with a receipt traceable to both inputs',async t=>{
 const f=await formatHandoff(t);f.router.state.inputs.next.nativeFailure={at:1001};f.advance();await f.watch();
 assert.deepEqual(f.notices,[['stopped','next']]);
 assert.equal(f.router.state.inputs.old.answer,undefined);
 assert.equal(f.router.state.inputs.old.ownerNotice.messageId,f.router.state.inputs.next.ownerNotice.messageId);
 f.restart();f.advance();await f.watch();assert.equal(f.notices.length,1);
});

test('unknown successor stays unknown and a canceled successor leaves the original obligation reportable',async t=>{
 const f=await formatHandoff(t);f.router.state.inputs.next.nativeFailure={at:1001};f.router.state.inputs.next.uncertainDeliveries=['original-fragment'];
 f.advance();await f.watch();assert.deepEqual(f.notices,[['unknown','next']]);assert.equal(f.router.state.inputs.old.answer,undefined);
 assert.equal(f.router.state.inputs.old.ownerNotice.kind,'unknown');
 const g=await formatHandoff(t);g.router.state.inputs.next.canceledBy='owner-stop';g.advance();await g.watch();
 assert.deepEqual(g.notices,[['stopped','old']]);assert.equal(g.router.state.inputs.old.answer,undefined);
});

test('handoff cannot merge explicit work or cross-audience inputs, and cycles cannot hide an obligation',async t=>{
 const f=await formatHandoff(t);const r=f.router;
 r.state.inputs.task={id:'task',kind:'owner',intent:'work',state:'accepted'};
 r.state.inputs.other={id:'other',kind:'owner',state:'accepted',conversation_key:'other'};
 r.state.inputs.next.conversation_key='same';
 for(const id of ['task','other'])await r.observe('reply-choice',{inputId:id,state:'merged',mergedInto:'next',handoff:true});
 assert.equal(r.state.inputs.task.replyHandoff,undefined);assert.equal(r.state.inputs.other.replyHandoff,undefined);
 await r.observe('reply-choice',{inputId:'next',state:'merged',mergedInto:'old',handoff:true});
 assert.equal(r.state.inputs.next.replyHandoff,undefined);
});


test('partial prefix remains partial when its handed successor fails; the shared notice is sent once',async t=>{
 const f=await fixture(t);await f.tail.handed({groups:[f.group],inputId:'next'});
 assert.equal(f.router.state.inputs.old.answer,undefined);
 f.router.state.inputs.next.nativeFailure={at:100001};f.advance(11*60000);
 const notices=[],notifyOwner=async(kind,id)=>{notices.push([kind,id]);return {state:'accepted',messageId:'partial-notice'};};
 await f.router.watch({notifyOwner});assert.deepEqual(notices,[['partial','next']]);
 assert.equal(f.router.state.inputs.old.ownerNotice.messageId,'partial-notice');assert.equal(f.router.state.inputs.old.delivered,4);
 f.advance(11*60000);await f.router.watch({notifyOwner});assert.equal(notices.length,1);assert.equal(f.sends.length,4);
});

test('a third successful chat settles the whole causal handoff chain across a restart',async t=>{
 const f=await formatHandoff(t);f.router.state.inputs.third={id:'third',kind:'owner',state:'accepted',intent:'chat',at:1000};
 await f.router.observe('reply-choice',{state:'merged',inputId:'next',mergedInto:'third',handoff:true,groupId:'next-group'});
 assert.equal(f.router.state.inputs.old.answer,undefined);f.restart();
 await f.router.observe('reply-choice',{state:'silent',inputId:'third'});
 assert.equal(f.router.state.inputs.next.answer.mergedInto,'third');assert.equal(f.router.state.inputs.old.answer.mergedInto,'next');
 f.advance();await f.watch();assert.deepEqual(f.notices,[]);
});


test('a successor notice settles an earlier rejected or unknown notice without losing its receipt history',async t=>{
 for(const state of ['rejected','unknown','not-submitted']) {
  const f=await formatHandoff(t),old=f.router.state.inputs.old;
  old.ownerNotice={id:'earlier-notice',kind:'stopped',state,lastReceipt:state,attempts:1};
  f.router.state.inputs.next.nativeFailure={at:1001};f.advance();await f.watch();
  assert.deepEqual(f.notices,[['stopped','next']]);
  assert.equal(old.ownerNotice.state,'accepted');assert.equal(inputSummary(old),'failed-notified');assert.equal(inputSettled(old),true);
  assert.equal(old.replyHandoff.previousNotice.id,'earlier-notice');assert.equal(old.replyHandoff.previousNotice.state,state);
  f.restart();f.advance();await f.watch();assert.equal(f.notices.length,1);
 }
});
