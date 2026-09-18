import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ReplyGuard} from './reply-guard.mjs';
import {MobileRouter} from './mobile-router.mjs';
import {replyProgress} from './reply-progress.mjs';
import {FORCED_TAIL_DECISIONS,TAIL_DECISIONS,TAIL_INTENT_STATES} from './reply-tail.mjs';
import {createFakeTransport} from './testing/fake-transport.mjs';
import {writeJsonAtomic} from './atomic-json.mjs';

/** One temp world per test: a reply guard over fake Python actions, a fake transport with its own
 * receipts, an injected clock and owner epoch. Nothing here can reach a phone or a model. */
function world(t,{wholeReplyReview=true,...options}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-tail-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const state={now:1_000_000,epoch:'epoch-1',verdict:null,decide:null,cancelFailures:0},canceled=[],preflights=[],events=[],tails=[],asked=[],woken=[];
  const clock=()=>state.now,transport=createFakeTransport({directory:path.join(root,'outbox'),clock});
  const call=async(action,request)=>{
    if(action==='reply-status')return {action:'reply'};
    if(action==='share-cancel'){if(state.cancelFailures-->0)throw Error('Mind worker failed');canceled.push(request.draft_id);return {state:'canceled'};}
    assert.equal(action,'share-preflight-group');preflights.push(request);
    return state.verdict?.(request)??{state:'ready',review_id:'review-'+preflights.length,checked:request.entries.map(e=>({text:e.text,references:[]}))};
  };
  const open=(extra={})=>new ReplyGuard({call,directory:path.join(root,'share-checks'),clock,wholeReplyReview,sleep:async()=>{},lease:{heartbeat:false},
    emit:async event=>{events.push(event);},onTail:detail=>tails.push(detail),onWake:id=>woken.push(id),ownerEpoch:()=>state.epoch,
    decideTail:async view=>{asked.push(view);return state.decide?.(view)??null;},...options,...extra});
  // What the host's resume guard does for an ordinary reply: a changed owner epoch means a newer message arrived.
  const hostGuard=async view=>view.ownerEpoch===state.epoch?'send':'cancel';
  return {root,state,clock,transport,platform:transport.platform,canceled,preflights,events,tails,asked,woken,open,hostGuard,guard:open()};
}
const group=(batch,texts,{replyId='input-1',work=false,taskId}={})=>texts.map((text,i)=>({request:{draft_id:`${batch}-d${i}`,reply_id:replyId,text,...(work?{work:true}:{})},
  delivery:{id:`${batch}-m${i}`,text,kind:'reply',draftId:`${batch}-d${i}`,memoryBatchId:batch,expectedBubbles:texts.length,...(taskId?{taskId}:{})}}));
const covering=(batch,coverage)=>request=>({state:'ready',review_id:'review-'+batch,checked:request.entries.map(e=>({text:e.text,references:[]})),
  ...(request.remainder?{covers_remainder:request.remainder.items.filter(i=>coverage[i.id]).map(i=>({item_id:i.id,covered_by:coverage[i.id],reason:'Said again in the new reply'})),
    remainder_owed:request.remainder.items.filter(i=>!coverage[i.id]).map(i=>i.id)}:{})});
const once=values=>Object.fromEntries([...new Set(values)].map(v=>[v,values.filter(x=>x===v).length]));

/** The first `after` bubbles go out; then a newer owner message arrives and the group stops at the next bubble boundary. */
async function interrupted(w,guard,batch,texts,options={}) {
  let submits=0,armed=true;
  w.platform.onSubmit(()=>{if(armed&&++submits===(options.after??1)){armed=false;w.state.now+=1000;w.state.epoch='epoch-after-'+batch;}});
  const result=await guard.replyGroup(group(batch,texts,options),'epoch-1',{guard:w.hostGuard,send:w.transport});
  assert.equal(result.groupState,'interrupted');armed=false;
  return result;
}
const decide=(w,guard,inputId,decision)=>{
  w.state.now+=1000;
  const offer=guard.tail.pending({id:inputId,text:'A newer owner message'});
  assert.ok(offer,'an interrupted group with nothing unknown is offered to the routing call');
  return guard.tail.decided({inputId,key:offer.key,tail:{decision,reason:'A short public judgment',receipt:{provider:'deepseek',model:'deepseek-flash',requestId:'request-'+inputId}}}).then(async told=>{await guard.tail.idleNow();return {offer,told};});
};

test('a newer owner message cancels nothing: the group is interrupted at the next bubble boundary and waits for a decision',async t=>{
  const w=world(t),result=await interrupted(w,w.guard,'reply-a',['第一句。','第二句。','第三句。']);
  assert.deepEqual(result.entries.map(e=>e.state),['accepted','unsent','unsent']);assert.equal(result.reason,'input-or-session-superseded');
  assert.deepEqual(w.canceled,[],'no reservation is released because a message arrived');assert.deepEqual(w.events.map(e=>e.state),['accepted']);
  assert.equal((await w.guard.resumeDue({guard:w.hostGuard,send:w.transport})).checked,0,'and nothing resumes it until somebody decides');
  assert.deepEqual(w.guard.tail.status().awaiting,['reply-a']);assert.ok(!JSON.stringify(w.guard.tail.status()).includes('第二句'),'status carries no message text');
  // The message that arrives while a bubble is in flight cannot carry the decision: what that bubble did is not known yet.
  const v=world(t);let offered='unset';
  v.platform.onSubmit(()=>{if(offered==='unset'){v.state.now+=1000;offered=v.guard.tail.pending({id:'input-2',text:'等一下'});}});
  const live=await v.guard.replyGroup(group('reply-b',['第一句。','第二句。']),'epoch-1',{guard:async()=>'send',send:v.transport});
  assert.equal(offered,null);assert.deepEqual([live.groupState,live.reason,...live.entries.map(e=>e.state)],['interrupted','new-owner-input','accepted','unsent']);
  assert.equal(v.guard.manifests.read('reply-b').interrupted.by,'input-2');
});

// 'next-draft': the next group's draft was written (it already names the old group), and the process died before the old group was told.
for(const step of ['recorded','applied','next-draft','linked'])test(`a crash right after ${step==='next-draft'?'the next group\'s draft is written':'the intent is '+step}: recovery rolls forward, every draft is cancelled exactly once, the link is there`,async t=>{
  let crashed=step==='next-draft';
  const w=world(t),crashing=w.open({tailHooks:{afterStep:name=>{if(name===step&&!crashed){crashed=true;throw Error('Simulated crash after '+name);}}}});
  await interrupted(w,crashing,'reply-a',['第一句。','第二句。','第三句。']);
  const next=group('reply-b',['新的回复，把后两句也说了。'],{replyId:'input-2'});
  const {told}=await decide(w,crashing,'input-2','rewrite_remainder');
  assert.equal(told.state,step==='recorded'?'failed':'recorded');
  if(step==='linked')await assert.rejects(crashing.tail.linkNew(crashing.draft(next,w.state.epoch,'feishu')),/Simulated crash after linked/);
  if(step==='next-draft'){w.state.now+=1000;assert.deepEqual(crashing.draft(next,w.state.epoch,'feishu').continues.map(c=>c.group_id),['reply-a']);}
  assert.ok(crashed);
  const onDisk=w.guard.manifests.read('reply-a').tail_intent;
  assert.deepEqual([onDisk.state,onDisk.decision,onDisk.carrier,onDisk.new_input_id,onDisk.receipt.requestId,onDisk.items],[step==='next-draft'?'applied':step,'rewrite_remainder','classify','input-2','request-input-2',['reply-a-d1','reply-a-d2']]);
  assert.ok(TAIL_INTENT_STATES.includes(onDisk.state));
  // A new process: nothing in memory, only the manifests.
  const fresh=w.open();w.state.now+=60000;
  w.state.verdict=covering('reply-b',{'reply-a-d1':'reply-b-d0','reply-a-d2':'reply-b-d0'});
  await fresh.resumeDue({guard:w.hostGuard,send:w.transport});
  const sent=await fresh.replyGroup(next,w.state.epoch,{guard:w.hostGuard,send:w.transport});
  assert.equal(sent.state,'accepted');
  assert.deepEqual(once(w.canceled),{'reply-a-d1':1,'reply-a-d2':1},'share-cancel for every retired draft, each exactly once');
  assert.deepEqual(once(w.events.filter(e=>e.state==='canceled').map(e=>e.bubble_id)),{'reply-a-m1':1,'reply-a-m2':1});
  const old=fresh.manifests.read('reply-a'),newer=fresh.manifests.read('reply-b');
  assert.equal(newer.continues_reply_id,'reply-a');assert.deepEqual(newer.continues.map(c=>[c.group_id,c.intent_id]),[['reply-a',onDisk.id]]);
  assert.deepEqual(old.bubbles.map(b=>[b.state,b.superseded_by??null,b.covered_by??null]),[['accepted',null,null],['canceled','reply-b','reply-b-d0'],['canceled','reply-b','reply-b-d0']],
    'superseded_by moved from the intent to the new group');
  assert.deepEqual([old.state,old.reason,old.tail_intent.state,old.tail_intent.linked_group,old.tail_intent.outcome.state],['retired','tail-rewrite_remainder','settled','reply-b','covered']);
  assert.deepEqual(w.preflights.at(-1).remainder,{reply_id:'reply-a',items:[{id:'reply-a-d1',text:'第二句。',references:[]},{id:'reply-a-d2',text:'第三句。',references:[]}]},
    'the review of the next group is asked about the old draft IDs');
  assert.deepEqual(w.asked,[],'no call of its own was needed');
  await fresh.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.deepEqual(fresh.manifests.live(),[],'settled on both sides: both groups leave the live set');
  assert.deepEqual(w.transport.sends.map(d=>d.bubbleId),['reply-a-m0','reply-b-m0'],'what was sent stayed sent, what was retired never went out');
});

test('continue: the old group finishes its remainder under the same transport IDs, and sent bubbles are never touched',async t=>{
  const w=world(t,{contracts:{feishu:{text:{limit:20,measure:'utf16'}}}}),long='第一段内容写到这里，先停一下。第二段内容接着往下写，把话说完整。';
  await interrupted(w,w.guard,'reply-a',['开头一句。',long,'最后一句。']);
  const before=w.guard.manifests.read('reply-a'),planned=before.bubbles.map(b=>b.fragments.map(f=>f.transport_id));
  assert.ok(planned[1].length>1,'the unsent bubble was already cut, once, when the review passed');
  const {told}=await decide(w,w.guard,'input-2','continue');
  assert.equal(told.state,'recorded');assert.deepEqual(w.woken,['reply-a']);
  const continued=w.guard.manifests.read('reply-a');
  assert.deepEqual([continued.state,continued.ownerEpoch,continued.continued.by,continued.tail_intent.state,continued.tail_intent.outcome.state],['sending',w.state.epoch,'input-2','settled','continued']);
  assert.deepEqual(w.canceled,[],'nothing is retired');
  w.state.now+=60000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const done=w.guard.manifests.read('reply-a');
  assert.equal(done.state,'accepted');
  assert.deepEqual(done.bubbles.map(b=>b.fragments.map(f=>f.transport_id)),planned,'not one identity was made a second time');
  assert.deepEqual(w.transport.sends.map(d=>d.id),planned.flat(),'each fragment went out once, under the ID the manifest stored');
  assert.deepEqual([done.bubbles[0].text,done.bubbles[0].fragments[0].receipt.messageId],[before.bubbles[0].text,before.bubbles[0].fragments[0].receipt.messageId]);
  assert.equal(w.preflights.filter(r=>!r.frozen).length,1,'and it was reviewed as a whole exactly once');
  // The message that interrupted it does not interrupt it again; a still newer one does.
  const v=world(t);
  await interrupted(v,v.guard,'reply-c',['第一句。','第二句。','第三句。']);
  await decide(v,v.guard,'input-2','continue');
  let submits=0;v.platform.onSubmit(()=>{if(++submits===1){v.state.now+=1000;v.guard.tail.missed({inputId:'input-3',reason:'not-classified'});}});
  await v.guard.resumeDue({guard:v.hostGuard,send:v.transport});
  const again=v.guard.manifests.read('reply-c');
  assert.deepEqual([again.state,again.reason,again.interrupted.by,...again.bubbles.map(b=>b.state)],['interrupted','new-owner-input','input-3','accepted','accepted','unsent']);
});

test('rewrite_remainder is an intention: what the next reply did not cover comes back, twice at most, then only continue or supersede is left',async t=>{
  const w=world(t),uncovered=batch=>covering(batch,{});
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。','第三句。']);
  for(const [round,input,batch] of [[1,'input-2','reply-b'],[2,'input-3','reply-c'],[3,'input-4','reply-d']]) {
    const {offer}=await decide(w,w.guard,input,'rewrite_remainder');
    assert.deepEqual(offer.reply.decisions,[...TAIL_DECISIONS]);assert.equal(offer.reply.resurfaced,round===1?undefined:round-1);
    assert.deepEqual(offer.reply.unsent.map(u=>u.text),['第二句。','第三句。']);
    w.state.verdict=uncovered(batch);w.state.now+=1000;
    assert.equal((await w.guard.replyGroup(group(batch,['只回答了新问题。'],{replyId:input}),w.state.epoch,{guard:w.hostGuard,send:w.transport})).state,'accepted');
    assert.deepEqual(w.preflights.at(-1).remainder.items.map(i=>i.id),['reply-a-d1','reply-a-d2'],'every next reply is asked about what is still owed');
    const old=w.guard.manifests.read('reply-a');
    assert.deepEqual([old.tail_intent.state,old.tail_intent.outcome.state,old.tail_intent.linked_group],['settled','owed',batch]);
    assert.deepEqual(old.tail_owed,{items:['reply-a-d1','reply-a-d2'],resurfaced:Math.min(round,2),forced:round===3,since:w.state.now});
  }
  assert.deepEqual(w.tails.filter(e=>['resurfaced','choice-required'].includes(e.tail.event)).map(e=>[e.groupId,e.tail.event,e.tail.resurfaced]),
    [['reply-a','resurfaced',1],['reply-a','resurfaced',2],['reply-a','choice-required',2]]);
  // The third time it is no longer a question of deferring it again.
  w.state.now+=1000;
  const forced=w.guard.tail.pending({id:'input-5',text:'还有别的吗'});
  assert.deepEqual(forced.reply.decisions,[...FORCED_TAIL_DECISIONS]);assert.equal(forced.reply.reason,'remainder-not-covered');
  assert.deepEqual(await w.guard.tail.decided({inputId:'input-5',key:forced.key,tail:{decision:'rewrite_remainder',reason:'once more'}}),{state:'missed',groupId:'reply-a',reason:'decision-not-allowed'});
  assert.equal((await w.guard.tail.decided({inputId:'input-5',key:forced.key,tail:{decision:'continue',reason:'It is still owed'}})).state,'recorded');
  await w.guard.tail.idleNow();
  // continue, for bubbles that were retired long ago: the same words as a group of their own, reviewed afresh.
  const old=w.guard.manifests.read('reply-a'),continuation=w.guard.manifests.read(old.tail_intent.linked_group);
  assert.deepEqual([old.tail_intent.state,continuation.continues_reply_id,continuation.continuation.items,continuation.ownerEpoch],['linked','reply-a',['reply-a-d1','reply-a-d2'],w.state.epoch]);
  assert.deepEqual(continuation.bubbles.map(b=>[b.text,b.request.reply_id,b.draft_id]),[['第二句。','input-1','reply-a-d1-c4'],['第三句。','input-1','reply-a-d2-c4']]);
  assert.deepEqual(w.woken,[continuation.group_id]);
  w.state.verdict=null;w.state.now+=60000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const settled=w.guard.manifests.read('reply-a');
  assert.deepEqual([settled.tail_intent.state,settled.tail_intent.outcome.state,settled.tail_owed,settled.bubbles[1].superseded_by],['settled','continued',undefined,continuation.group_id]);
  assert.deepEqual(w.platform.delivered.map(d=>d.body),['第一句。','只回答了新问题。','只回答了新问题。','只回答了新问题。','第二句。','第三句。']);
  assert.equal(w.preflights.at(-1).remainder,undefined,'the continuation is the remainder: its review is not asked to cover itself');
  assert.deepEqual(once(w.canceled),{'reply-a-d1':1,'reply-a-d2':1},'across every round each old draft was cancelled exactly once');
  assert.deepEqual(w.asked,[],'and every decision rode on a call that was made anyway');
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});assert.deepEqual(w.guard.manifests.live(),[]);
});

test('what DeepSeek superseded never comes back, whatever the next reply covers',async t=>{
  const w=world(t);
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。']);
  await decide(w,w.guard,'input-2','supersede');
  assert.deepEqual(w.canceled,['reply-a-d1']);
  w.state.verdict=covering('reply-b',{});w.state.now+=1000;
  await w.guard.replyGroup(group('reply-b',['新的回复。'],{replyId:'input-2'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  assert.equal(w.preflights.at(-1).remainder,undefined,'nothing is owed, so the review is not asked to cover anything');
  const old=w.guard.manifests.read('reply-a');
  assert.deepEqual([old.state,old.reason,old.tail_intent.state,old.tail_intent.outcome.state,old.tail_owed,old.bubbles[1].superseded_by,old.tail_intent.basis.reason],
    ['retired','tail-supersede','settled','superseded',undefined,'reply-b','A short public judgment']);
  w.state.now+=3600000;
  assert.equal(w.guard.tail.pending({id:'input-3',text:'后来呢'}),null);
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.deepEqual([w.asked,w.guard.manifests.live(),w.guard.tail.owed()],[[],[],[]]);
  // Without any next group a withdrawn remainder does not wait for ever to be pointed at a successor.
  const v=world(t);
  await interrupted(v,v.guard,'reply-c',['第一句。','第二句。']);
  await decide(v,v.guard,'input-2','supersede');
  v.state.now+=10*60000;await v.guard.resumeDue({guard:v.hostGuard,send:v.transport});
  assert.deepEqual([v.guard.manifests.read('reply-c').tail_intent.state,v.guard.manifests.read('reply-c').tail_intent.outcome],['settled',{state:'superseded',linked:false}]);
});

test('an owner stop retires the remainder without asking any model, survives a restart, and nothing of it comes back',async t=>{
  const w=world(t);let classified=0;
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。','第三句。']);
  w.guard.deferGroup(group('reply-held',['排队中的一句。','排队中的另一句。'],{replyId:'input-0'}),'epoch-0');
  const router=new MobileRouter({file:path.join(w.root,'router.json'),sessionId:'synthetic',replyTail:w.guard.tail,now:w.clock,classify:async()=>{classified++;return {route:'chat',reason:'synthetic'};},
    inspect:async()=>({known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'deepseek-flash',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0}),
    switchModel:async()=>{throw Error('no switch in this test');},waitForIdle:async()=>{}});
  w.state.now+=1000;
  const record=await router.select({id:'input-stop',text:'停止任务'});
  assert.deepEqual([record.reason,record.tail],['owner-stop-command',{carrier:'owner-stop',state:'recorded'}]);
  await w.guard.tail.idleNow();
  assert.deepEqual([classified,w.asked],[0,[]],'neither the routing call nor a call of its own');
  assert.deepEqual(w.canceled.sort(),['reply-a-d1','reply-a-d2','reply-held-d0','reply-held-d1']);
  for(const id of ['reply-a','reply-held']) {
    const stopped=w.guard.manifests.read(id);
    assert.deepEqual([stopped.state,stopped.reason,stopped.tail_intent.decision,stopped.tail_intent.carrier,stopped.tail_intent.receipt,stopped.tail_intent.new_input_id,stopped.tail_intent.basis.reason],
      ['retired','owner-stop','supersede','owner-stop',null,'input-stop','owner-stop-command']);
  }
  // The acknowledgement is written after the stop: it is a reply of its own, and it is not asked to cover anything.
  w.state.now+=1000;
  const ack=await w.guard.replyGroup(group('reply-ack',['好，停下了。'],{replyId:'input-stop'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  assert.equal(ack.state,'accepted');assert.equal(w.preflights.at(-1).remainder,undefined);
  assert.deepEqual(w.guard.manifests.read('reply-a').bubbles.map(b=>b.superseded_by??null),[null,'reply-ack','reply-ack']);
  w.state.now+=3600000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.deepEqual([w.guard.manifests.live(),w.guard.tail.owed(),w.asked,classified],[[],[],[],0]);
  assert.deepEqual(w.platform.delivered.map(d=>d.body),['第一句。','好，停下了。']);
  // A group that is being sent when the stop arrives stops at its next bubble boundary; a restart in between loses nothing.
  const v=world(t);let submits=0;
  v.platform.onSubmit(async()=>{if(++submits===1){v.state.now+=1000;assert.deepEqual(await v.guard.tail.stopped({inputId:'input-stop'}),{state:'recorded'});}});
  const live=await v.guard.replyGroup(group('reply-c',['第一句。','第二句。']),'epoch-1',{guard:async()=>'send',send:v.transport});
  assert.deepEqual([live.groupState,live.reason,...live.entries.map(e=>e.state)],['retired','owner-stop','accepted','canceled']);
  const u=world(t);
  await interrupted(u,u.guard,'reply-d',['第一句。','第二句。']);
  await decide(u,u.guard,'input-2','rewrite_remainder');
  // The stop became durable, then the process died before anything was done about it.
  u.state.now+=1000;
  writeJsonAtomic(path.join(u.root,'share-checks','reply-manifests','tail','stops.json'),{stops:[{input_id:'input-stop',at:u.state.now}]},{previous:false});
  const restarted=u.open();u.state.now+=1000;u.state.verdict=covering('reply-e',{});
  await restarted.replyGroup(group('reply-e',['好，不说了。'],{replyId:'input-stop'}),u.state.epoch,{guard:u.hostGuard,send:u.transport});
  assert.equal(u.preflights.at(-1).remainder,undefined,'what the owner stopped is not owed to the next reply');
  const promised=restarted.manifests.read('reply-d');
  assert.deepEqual([promised.tail_intent.state,promised.tail_intent.outcome,promised.tail_owed,promised.tail_stop.input_id],['settled',{state:'superseded'},undefined,'input-stop'],
    'a remainder that was promised to the next reply is no longer owed once the owner said stop');
});

test('an old group with an unknown fragment blocks only itself: new chat goes on and the routing call is never given that group',async t=>{
  const w=world(t),seen=[];
  w.platform.script('lost');
  const old=await w.guard.replyGroup(group('reply-old',['旧的第一句。','旧的第二句。']),'epoch-1',{guard:w.hostGuard,send:w.transport});
  assert.equal(old.groupState,'blocked-unknown');
  const router=new MobileRouter({file:path.join(w.root,'router.json'),sessionId:'synthetic',replyTail:w.guard.tail,now:w.clock,classify:async input=>{seen.push(input);return {route:'chat',reason:'synthetic'};},
    inspect:async()=>({known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'deepseek-flash',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0}),
    switchModel:async()=>{throw Error('no switch in this test');},waitForIdle:async()=>{}});
  w.state.now+=1000;w.state.epoch='epoch-2';
  const record=await router.select({id:'input-2',text:'换个话题'});
  assert.deepEqual([seen.length,Object.hasOwn(seen[0],'interruptedReply'),record.tail],[1,false,undefined]);
  const next=await w.guard.replyGroup(group('reply-new',['新的回复。'],{replyId:'input-2'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  assert.equal(next.state,'accepted');
  assert.deepEqual([w.guard.manifests.read('reply-new').continues,w.preflights.at(-1).remainder],[undefined,undefined],'the new group neither continues it nor is asked to cover it');
  w.state.now+=3600000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const blocked=w.guard.manifests.read('reply-old');
  assert.deepEqual([blocked.state,blocked.tail_intent,blocked.bubbles.map(b=>b.state),w.asked,w.canceled],['blocked-unknown',undefined,['unconfirmed','unsent'],[],[]],'and no decision of any kind is made about it');
  // Once the receipt can tell, the group moves again, is interrupted by the newer message, and only then is it offered.
  await w.guard.manifests.resolve('reply-old',blocked.bubbles[0].fragments[0].transport_id,{outcome:'accepted',messageId:'om_operator'});
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.equal(w.guard.manifests.read('reply-old').state,'interrupted');
  w.state.now+=1000;
  await router.select({id:'input-3',text:'刚才那个呢'});
  assert.deepEqual(seen[1].interruptedReply.sent.map(s=>[s.text,s.receipt.messageId]),[['旧的第一句。','om_operator']]);
});

test('a classifier timeout falls back to the settlement carrier: the review of the next reply',async t=>{
  const w=world(t);
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。','第三句。']);
  const router=new MobileRouter({file:path.join(w.root,'router.json'),sessionId:'synthetic',replyTail:w.guard.tail,now:w.clock,classify:async()=>{throw Error('classification-timeout');},
    inspect:async()=>({known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'gpt-6-astra',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0}),
    switchModel:async()=>{throw Error('no switch in this test');},waitForIdle:async()=>{}});
  w.state.now+=1000;
  const record=await router.select({id:'input-2',text:'那第二点呢'});
  // KIN-ITER-20260918-02: the classification failure routes nothing; the tail still misses.
  assert.deepEqual([record.state,record.reason,record.tail],['semantic-pending','classification-timeout',{carrier:'classify',state:'missed',groupId:'reply-a',reason:'classifier-unconfirmed'}]);
  assert.equal(w.guard.manifests.read('reply-a').tail_intent,undefined,'no decision was made up');
  // The next reply covers the second bubble and says nothing about the third.
  w.state.verdict=covering('reply-b',{'reply-a-d1':'reply-b-d0'});w.state.now+=1000;
  await w.guard.replyGroup(group('reply-b',['第二点是这样的。'],{replyId:'input-2'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  assert.deepEqual(w.preflights.at(-1).remainder.items.map(i=>i.id),['reply-a-d1','reply-a-d2']);
  const old=w.guard.manifests.read('reply-a');
  assert.deepEqual([old.tail_intent.carrier,old.tail_intent.decision,old.tail_intent.receipt,old.tail_intent.items,old.tail_intent.state,old.tail_intent.outcome.state],
    ['review','rewrite_remainder',{review_id:'review-reply-b',group_id:'reply-b'},['reply-a-d1'],'settled','covered']);
  assert.deepEqual(old.bubbles.map(b=>[b.state,b.superseded_by??null]),[['accepted',null],['canceled','reply-b'],['unsent',null]],'what the review did not mention stays exactly as it was');
  assert.deepEqual([old.state,w.canceled,w.asked],['interrupted',['reply-a-d1'],[]]);
  assert.equal(w.guard.manifests.read('reply-b').review.coverage_applied,true);
  // The rest still waits for a decision, and the next routing call carries it.
  w.state.now+=1000;
  assert.deepEqual(w.guard.tail.pending({id:'input-3',text:'好'}).reply.unsent.map(u=>u.text),['第三句。']);
  // Continued, the old group sends what is left under the ID it always had. The review that validates it reads every
  // request of the group, so what it may have reserved again for the withdrawn bubble is released once more.
  const planned=old.bubbles[2].fragments[0].transport_id;
  await decide(w,w.guard,'input-4','continue');w.state.verdict=null;w.state.now+=60000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const done=w.guard.manifests.read('reply-a');
  assert.deepEqual([done.state,done.reason,done.bubbles.map(b=>b.state),w.transport.sends.at(-1).id],['retired','tail-rewrite_remainder',['accepted','canceled','accepted'],planned]);
  assert.deepEqual([once(w.canceled),w.events.filter(e=>e.state==='canceled').length],[{'reply-a-d1':2},1],'released again, reported once');
});

test('a call of its own happens only when no carrier ran, only after a wait, only while the owner turn is idle, and a bounded number of times',async t=>{
  const w=world(t,{tailLimits:{dedicatedAfterMs:120000,decisionAttempts:3,decisionBaseMs:60000}});
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。']);
  w.state.now+=1000;
  assert.deepEqual(w.guard.tail.missed({inputId:'input-2',reason:'not-classified'}),{state:'missed',reason:'not-classified'});   // an attachment: never classified, and the reply to it was a deliberate silence
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.deepEqual(w.asked,[],'not before the settlement carrier had its chance');
  w.state.now+=120000;
  await w.guard.resumeDue({guard:async()=>'wait',send:w.transport});
  assert.deepEqual(w.asked,[],'not while the owner turn is running: its reply may still carry the decision');
  for(let pass=0;pass<12;pass++){await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});w.state.now+=20*60000;}
  assert.equal(w.asked.length,3,'a model that does not answer is asked a bounded number of times');
  assert.deepEqual(w.asked[0],{interruptedReply:{reason:'input-or-session-superseded',sent:[{text:'第一句。',receipt:{messageId:'om_1',acceptedAt:w.asked[0].interruptedReply.sent[0].receipt.acceptedAt}}],
    unconfirmed:[],unsent:[{text:'第二句。'}],decisions:[...TAIL_DECISIONS]},newMessage:null});
  assert.deepEqual([w.guard.tail.status().groups['reply-a'].decision_attempts,w.guard.manifests.read('reply-a').state,w.canceled],[3,'interrupted',[]],'and nothing is decided in its place');
  assert.ok(w.tails.some(e=>e.tail.event==='decision-failed'&&e.tail.attempts===3));
  // A new owner message is a new reason to ask; this time the model answers.
  w.state.now+=1000;w.guard.tail.missed({inputId:'input-3',reason:'not-classified'});w.state.now+=120000;
  w.state.decide=()=>({decision:'supersede',reason:'The owner moved on',receipt:{provider:'deepseek',requestId:'request-tail'}});
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const old=w.guard.manifests.read('reply-a');
  assert.deepEqual([w.asked.length,old.tail_intent.carrier,old.tail_intent.decision,old.tail_intent.receipt.requestId,old.state,w.canceled],[4,'dedicated','supersede','request-tail','retired',['reply-a-d1']]);
});

test('a new owner message while a group is held: the routing call carries the decision, and the input it answers never interrupts it',async t=>{
  const w=world(t);
  w.state.verdict=()=>({state:'pending',reason:'prior-delivery-needs-review'});
  const held=await w.guard.replyGroup(group('reply-a',['第一句。','第二句。']),'epoch-1',{guard:w.hostGuard,send:w.transport});
  assert.equal(held.groupState,'held');
  w.state.now+=1000;w.state.epoch='epoch-2';
  const offer=w.guard.tail.pending({id:'input-2',text:'算了不用说了'});
  assert.deepEqual([offer.reply.reason,offer.reply.sent,offer.reply.unsent.map(u=>u.text)],['new-owner-input',[],['第一句。','第二句。']]);
  assert.equal(w.guard.manifests.read('reply-a').state,'held','offering a group changes nothing on disk');
  await w.guard.tail.decided({inputId:'input-2',key:offer.key,tail:{decision:'supersede',reason:'The owner does not want it'}});await w.guard.tail.idleNow();
  assert.deepEqual([w.guard.manifests.read('reply-a').state,w.canceled],['retired',['reply-a-d0','reply-a-d1']]);
  // A decision made about one view never lands on another.
  const v=world(t);
  await interrupted(v,v.guard,'reply-b',['第一句。','第二句。','第三句。']);
  v.state.now+=1000;
  const stale=v.guard.tail.pending({id:'input-2',text:'继续'});
  await decide(v,v.guard,'input-3','supersede');
  assert.deepEqual(await v.guard.tail.decided({inputId:'input-2',key:stale.key,tail:{decision:'continue',reason:'late'}}),{state:'missed',groupId:'reply-b',reason:'group-changed'});
  // The reply to the newest message is not interrupted by that message; work replies follow the host's own rule.
  const u=world(t);
  u.state.now+=1000;u.guard.tail.missed({inputId:'input-9',reason:'not-classified'});u.state.now+=1000;
  assert.equal((await u.guard.replyGroup(group('reply-c',['第一句。','第二句。'],{replyId:'input-9'}),u.state.epoch,{guard:u.hostGuard,send:u.transport})).state,'accepted');
  let submits=0;u.platform.onSubmit(()=>{if(++submits===1){u.state.now+=1000;u.guard.tail.missed({inputId:'input-10',reason:'not-classified'});}});
  assert.equal((await u.guard.replyGroup(group('reply-work',['结果第一段。','结果第二段。'],{work:true,taskId:'task-1'}),u.state.epoch,{guard:async()=>'send',send:u.transport})).state,'accepted');
});

test('frozen-remainder-review-required is answered by a second preflight with a model, for the unsent remainder only; sent bubbles are never rewritten',async t=>{
  const w=world(t);
  w.transport.fault('before-receipt',{when:d=>d.bubbleId==='reply-a-m1'});
  const first=await w.guard.replyGroup(group('reply-a',['已经发出的一句。','还没发出的一句。']),'epoch-1',{guard:w.hostGuard,send:w.transport});
  assert.deepEqual(first.entries.map(e=>e.state),['accepted','unsent']);
  w.state.verdict=request=>request.allow_model?{state:'ready',review_id:'review-frozen',frozen_remainder:{sent:['reply-a-d0'],reviewed:['reply-a-d1']},
    checked:request.entries.map(e=>({text:'改写过的：'+e.text,references:[]}))}:{state:'pending',reason:'frozen-remainder-review-required',review_id:'review-1'};
  w.state.now+=60000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const [,without,withModel]=w.preflights;
  assert.deepEqual([w.preflights.length,without.frozen,without.allow_model,withModel.frozen,withModel.allow_model],[3,true,false,true,true]);
  assert.deepEqual(withModel.entries,without.entries);
  assert.deepEqual(withModel.outbox.map(row=>[row.draft_id,row.state,row.text,row.message_id]),[['reply-a-d0','accepted','已经发出的一句。','om_1']],
    'the manifest itself says what was sent, so the review judges the remainder only');
  const done=w.guard.manifests.read('reply-a');
  assert.equal(done.state,'accepted');
  assert.deepEqual(done.bubbles.map(b=>b.text),['已经发出的一句。','还没发出的一句。'],'a frozen group is validated, never rewritten: not what was sent, not what was frozen');
  assert.deepEqual(w.platform.delivered.map(d=>d.body),['已经发出的一句。','还没发出的一句。']);
  assert.equal(w.transport.sends.filter(d=>d.bubbleId==='reply-a-m0').length,1);
});

test('a frozen remainder that has to be rewritten is a tail decision, not a wait; a reply no review can take ends visibly',async t=>{
  const w=world(t);
  w.transport.fault('before-receipt',{when:d=>d.bubbleId==='reply-a-m1'});
  await w.guard.replyGroup(group('reply-a',['已经发出的一句。','需要改写的一句。']),'epoch-1',{guard:w.hostGuard,send:w.transport});
  w.state.verdict=request=>request.frozen?{state:'pending',reason:'frozen-remainder-needs-new-group',review_id:'review-1'}:null;
  w.state.now+=60000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const waiting=w.guard.manifests.read('reply-a');
  assert.deepEqual([waiting.state,waiting.reason,waiting.holds],['interrupted','frozen-remainder-needs-new-group',undefined]);
  // continue cannot mean "send it as it stands" here: the same words become a group of their own, reviewed on its own evidence.
  await decide(w,w.guard,'input-2','continue');
  const old=w.guard.manifests.read('reply-a'),continuation=w.guard.manifests.read(old.tail_intent.linked_group);
  assert.deepEqual([old.state,old.reason,w.canceled,continuation.bubbles.map(b=>b.text)],['retired','tail-continue',['reply-a-d1'],['需要改写的一句。']]);
  w.state.verdict=request=>({state:'ready',review_id:'review-c',checked:request.entries.map(()=>({text:'改写后的一句。',references:[]}))});
  w.state.now+=60000;await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.deepEqual(w.platform.delivered.map(d=>d.body),['已经发出的一句。','改写后的一句。']);
  assert.equal(w.guard.manifests.read('reply-a').tail_intent.outcome.state,'continued');
  // Switched off, the same answer is an ordinary, bounded hold.
  const off=world(t,{replyTailDecision:false});
  off.transport.fault('before-receipt',{when:d=>d.bubbleId==='reply-b-m1'});
  await off.guard.replyGroup(group('reply-b',['已经发出的一句。','需要改写的一句。']),'epoch-1',{guard:off.hostGuard,send:off.transport});
  off.state.verdict=request=>request.frozen?{state:'pending',reason:'frozen-remainder-needs-new-group'}:null;off.state.now+=60000;
  await off.guard.resumeDue({guard:off.hostGuard,send:off.transport});
  assert.deepEqual([off.guard.manifests.read('reply-b').state,off.guard.manifests.read('reply-b').holds],['held',1]);
  const big=world(t);
  big.state.verdict=()=>({state:'pending',reason:'reply-exceeds-review-capacity'});
  const refused=await big.guard.replyGroup(group('reply-c',['第一句。','第二句。']),'epoch-1',{guard:big.hostGuard,send:big.transport});
  assert.deepEqual([refused.groupState,refused.reason,big.canceled.sort(),big.preflights.length],['undeliverable','reply-exceeds-review-capacity',['reply-c-d0','reply-c-d1'],1]);
});

test('a parked group costs no model call; a new owner message is a new reason and needs none either',async t=>{
  const w=world(t,{retry:{maxHolds:3}});
  w.state.verdict=()=>({state:'pending',reason:'prior-delivery-needs-review'});
  await w.guard.replyGroup(group('reply-a',['第一句。','第二句。']),'epoch-1',{guard:w.hostGuard,send:w.transport});
  for(let pass=0;pass<40;pass++){w.state.now+=15*60000;await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});}
  const parked=w.guard.manifests.read('reply-a');
  assert.deepEqual([w.preflights.length,parked.state,parked.holds,parked.parked.reason,w.guard.tail.status().groups['reply-a'].parked.holds],[3,'held',3,'review-hold-limit',3]);
  w.state.now+=1000;w.state.epoch='epoch-2';
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.deepEqual([w.guard.manifests.read('reply-a').state,w.preflights.length,w.asked.length],['interrupted',3,0]);
  await decide(w,w.guard,'input-2','continue');
  w.state.verdict=null;w.state.now+=60000;
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  assert.deepEqual([w.guard.manifests.read('reply-a').state,w.preflights.length],['accepted',4],'DeepSeek said it still goes out: one fresh review, and it does');
});

test('cancel, rewrite and continuation are visible in status, keyed by group, while a newer input is being awaited',async t=>{
  const w=world(t);
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。']);
  await decide(w,w.guard,'input-2','rewrite_remainder');
  w.state.verdict=covering('reply-b',{'reply-a-d1':'reply-b-d0'});w.state.now+=1000;
  await w.guard.replyGroup(group('reply-b',['新的回复。'],{replyId:'input-2'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  let status={awaitingReplyInputId:'input-2',awaitingReplySince:'2026-01-01T00:00:00Z',replyDisposition:'waiting'};
  w.tails.forEach((detail,index)=>{status={...status,...replyProgress(status,'reply-tail',detail,new Date(Date.UTC(2026,0,1,0,0,index)).toISOString())};});
  assert.deepEqual(w.tails.map(e=>[e.groupId,e.inputId,e.tail.event]),[['reply-a','input-1','decision'],['reply-a','input-1','applied'],['reply-a','input-1','linked'],['reply-a','input-1','settled']]);
  const shown=status.replyGroups['reply-a'];
  assert.deepEqual([shown.state,shown.reason,shown.inputId,shown.event,shown.tail.event,shown.tail.decision,shown.tail.carrier,shown.tail.linkedGroup,shown.tail.outcome,shown.tail.newInputId],
    ['retired','tail-rewrite_remainder','input-1','reply-tail','settled','rewrite_remainder','classify','reply-b','covered','input-2']);
  assert.deepEqual([status.awaitingReplyInputId,status.replyDisposition,status.awaitingReplySince],['input-2','waiting','2026-01-01T00:00:00Z'],'what became of an older group never answers for the input awaited now');
  assert.ok(!JSON.stringify([w.tails,status]).includes('第二句')&&!JSON.stringify(w.tails).includes('public judgment'),'no reply text and no model text in status');
  const summary=w.guard.manifests.status().groups['reply-a'];
  assert.deepEqual([summary.tail_intent.decision,summary.tail_intent.state,summary.tail_intent.linked_group,summary.bubbles[1].superseded_by],['rewrite_remainder','settled','reply-b','reply-b']);
  assert.equal(w.guard.manifests.status().groups['reply-b'].continues_reply_id,'reply-a');
});

test('what the next turn should know: the withdrawn remainder, until it is covered',async t=>{
  const w=world(t);
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。']);
  assert.deepEqual(w.guard.tail.owed(),[],'nothing is owed to the next reply before anybody decided that');
  await decide(w,w.guard,'input-2','rewrite_remainder');
  assert.deepEqual(w.guard.tail.owed(),[{group_id:'reply-a',reply_id:'input-1',resurfaced:0,sent:['第一句。'],unsent:['第二句。']}]);
  w.state.verdict=covering('reply-b',{'reply-a-d1':'reply-b-d0'});w.state.now+=1000;
  await w.guard.replyGroup(group('reply-b',['新的回复。'],{replyId:'input-2'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  assert.deepEqual(w.guard.tail.owed(),[]);
});

test('reply_tail_decision switched off: an interrupted group is handled exactly as the manifest alone handles it',async t=>{
  const w=world(t,{replyTailDecision:false});let submits=0;
  assert.equal(w.guard.tail,null);
  w.platform.onSubmit(()=>{if(++submits===1)w.state.epoch='epoch-2';});
  const result=await w.guard.replyGroup(group('reply-a',['第一句。','第二句。','第三句。']),'epoch-1',{guard:w.hostGuard,send:w.transport});
  assert.deepEqual([result.groupState,result.reason,...result.entries.map(e=>e.state)],['retired','input-or-session-superseded','accepted','canceled','canceled']);
  assert.deepEqual(w.canceled,['reply-a-d1','reply-a-d2']);
  const next=await w.guard.replyGroup(group('reply-b',['新的回复。'],{replyId:'input-2'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  assert.equal(next.state,'accepted');
  assert.deepEqual([w.guard.manifests.read('reply-b').continues_reply_id,w.preflights.at(-1).remainder,w.asked],[undefined,undefined,[]]);
  assert.deepEqual(Object.keys(await w.guard.resumeDue({guard:w.hostGuard,send:w.transport})).sort(),['checked','groups','imported','state']);
  assert.ok(!fs.existsSync(path.join(w.root,'share-checks','reply-manifests','tail')));
});

test('a fragment whose send may have begun is reconciled under its original ID before anybody is asked about the remainder',async t=>{
  const w=world(t);let crashes=0;
  const crashing=w.open({hooks:{afterSend:()=>{if(!crashes++)throw Error('Simulated crash after the send, before the manifest knew');}}});
  await assert.rejects(crashing.replyGroup(group('reply-a',['第一句。','第二句。']),'epoch-1',{guard:w.hostGuard,send:w.transport}),/Simulated crash/);
  const left=w.guard.manifests.read('reply-a');
  assert.deepEqual([left.state,left.bubbles[0].fragments[0].state],['sending','submitting']);
  // The holder is gone and its lease has run out; the owner writes again.
  w.state.now+=200000;w.state.epoch='epoch-2';
  assert.equal(w.guard.tail.pending({id:'input-2',text:'还在吗'}),null,'what the first bubble did is not known yet: nobody is asked');
  assert.deepEqual(w.guard.tail.continuesFor(group('reply-b',['新的回复。'],{replyId:'input-2'})),[]);
  await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});
  const reconciled=w.guard.manifests.read('reply-a');
  assert.deepEqual([reconciled.state,...reconciled.bubbles.map(b=>b.state),w.transport.sends.length],['interrupted','accepted','unsent',1],'settled by its receipt, under the ID it was sent with, and never sent twice');
  w.state.now+=1000;
  const offer=w.guard.tail.pending({id:'input-3',text:'然后呢'});
  assert.deepEqual([offer.reply.sent.map(s=>s.text),offer.reply.unsent.map(u=>u.text)],[['第一句。'],['第二句。']]);
});

test('what the routing call carries is capped: excerpts, the last few sent bubbles, the first few unsent ones, and a count of the rest',async t=>{
  const w=world(t),texts=Array.from({length:16},(_,i)=>'第'+(i+1)+'段。'+'很长的内容'.repeat(i===7?80:1));
  await interrupted(w,w.guard,'reply-a',texts,{after:6});
  w.state.now+=1000;
  const {reply}=w.guard.tail.pending({id:'input-2',text:'等等'});
  assert.deepEqual([reply.sent.length,reply.unsent.length,reply.omitted],[4,6,{sent:2,unsent:4}]);
  assert.deepEqual(reply.sent.map(s=>s.text.slice(0,3)),['第3段','第4段','第5段','第6段'],'the bubbles nearest to the break');
  assert.deepEqual([reply.unsent[1].text.length,reply.unsent[1].complete,reply.unsent[0].complete],[280,false,undefined]);
  assert.ok(JSON.stringify(reply).length<6000&&!JSON.stringify(reply).includes('reply-a'),'small enough for the classifier budget, and no identifiers');
});

test('a memory host that is down while the remainder is retired: the intent stays recorded, is tried again, and every reservation is released once',async t=>{
  const w=world(t);
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。','第三句。']);
  w.state.cancelFailures=3;
  const {told}=await decide(w,w.guard,'input-2','supersede');
  const stuck=w.guard.manifests.read('reply-a');
  assert.deepEqual([told.state,stuck.tail_intent.state,stuck.state,stuck.bubbles.map(b=>b.state),w.canceled],['recorded','recorded','retired',['accepted','canceled','canceled'],[]],
    'the decision and the retirement are durable; what is still owed is the release of the reservations');
  assert.equal(w.guard.tail.status().groups['reply-a'].awaiting,'retirement');
  for(let pass=0;pass<4;pass++){w.state.now+=60000;await w.guard.resumeDue({guard:w.hostGuard,send:w.transport});}
  assert.deepEqual([once(w.canceled),w.guard.manifests.read('reply-a').tail_intent.state],[{'reply-a-d1':1,'reply-a-d2':1},'applied']);
  assert.equal(w.transport.sends.length,1,'and nothing of the retired remainder was ever sent');
});

test('two cut-off replies at once: the routing call carries the latest one, the next group answers for both',async t=>{
  const w=world(t);
  w.state.verdict=request=>request.entries[0].draft_id.startsWith('reply-held')?{state:'pending',reason:'prior-delivery-needs-review'}:null;
  await w.guard.replyGroup(group('reply-held',['早先排队的一句。'],{replyId:'input-0'}),'epoch-1',{guard:w.hostGuard,send:w.transport});
  w.state.now+=1000;
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。']);
  w.state.now+=1000;
  const offer=w.guard.tail.pending({id:'input-2',text:'等一下'});
  assert.deepEqual(offer.reply.unsent.map(u=>u.text),['第二句。'],'one reply per routing call: the one that was cut off last');
  await w.guard.tail.decided({inputId:'input-2',key:offer.key,tail:{decision:'supersede',reason:'moved on'}});await w.guard.tail.idleNow();
  w.state.verdict=covering('reply-b',{'reply-held-d0':'reply-b-d0'});w.state.now+=1000;
  await w.guard.replyGroup(group('reply-b',['新的回复，也说了早先那句。'],{replyId:'input-2'}),w.state.epoch,{guard:w.hostGuard,send:w.transport});
  const next=w.guard.manifests.read('reply-b');
  assert.deepEqual([next.continues_reply_id,next.continues.map(c=>c.group_id)],['reply-a',['reply-a','reply-held']]);
  assert.deepEqual(w.preflights.at(-1).remainder.items.map(i=>i.id),['reply-held-d0'],'only what is still owed is put to the review, never what was superseded');
  assert.deepEqual(['reply-a','reply-held'].map(id=>{const m=w.guard.manifests.read(id);return [m.state,m.tail_intent.carrier,m.tail_intent.outcome.state,m.bubbles.at(-1).superseded_by];}),
    [['retired','classify','superseded','reply-b'],['retired','review','covered','reply-b']]);
  assert.deepEqual(once(w.canceled),{'reply-a-d1':1,'reply-held-d0':1});
});

test('an operator who continues an interrupted group is not overruled by the message that interrupted it',async t=>{
  const w=world(t);
  await interrupted(w,w.guard,'reply-a',['第一句。','第二句。']);
  await w.open({role:'cli'}).manifests.continueGroup('reply-a');
  const told=w.guard.manifests.read('reply-a');
  assert.deepEqual([told.state,told.ownerEpoch,told.continued.epoch],['sending','epoch-1',undefined],'the tool does not know the host epoch, and does not pretend to');
  w.state.now+=60000;
  await w.open().resumeDue({guard:w.hostGuard,send:w.transport});
  assert.equal(w.guard.manifests.read('reply-a').state,'accepted');
  // After DeepSeek's continue, by contrast, the epoch is known: a restart forgets the newer message, the moved epoch does not.
  const v=world(t);
  await interrupted(v,v.guard,'reply-b',['第一句。','第二句。','第三句。']);
  await decide(v,v.guard,'input-2','continue');
  v.state.epoch='epoch-after-input-3';v.state.now+=60000;
  await v.open().resumeDue({guard:v.hostGuard,send:v.transport});
  assert.deepEqual([v.guard.manifests.read('reply-b').state,v.transport.sends.length],['interrupted',1]);
});
