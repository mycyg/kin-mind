import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ReplyGuard} from '../../adapters/reply-guard.mjs';
import {MobileRouter} from '../../adapters/mobile-router.mjs';

function chat(t, options={}) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-chat-'));
  t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const calls=[],sent=[],receipts=new Map(),time={ms:Date.parse('2026-09-24T12:00:00Z')};
  const guard=new ReplyGuard({directory,sleep:async()=>{},clock:()=>time.ms,lease:{heartbeat:false},
    call:async (action,input)=>{calls.push({action,input});if(action==='reply-status')return {action:'none'};},
    receipt:async id=>receipts.get(id),...options});
  const send=async delivery=>{sent.push(delivery.text);const r={state:'accepted',messageId:delivery.id,acceptedAt:new Date(time.ms).toISOString()};receipts.set(delivery.id,r);return r;};
  const entries=(texts,{reason=null,batch='reply',input='input'}={})=>(Array.isArray(texts)?texts:[texts]).map((text,i)=>({
    request:{reply_id:input,draft_id:batch+'-draft-'+i,text,...(reason?{repair_reason:reason}:{})},
    delivery:{id:batch+'-bubble-'+i,memoryBatchId:batch,text,kind:'reply',expectedBubbles:Array.isArray(texts)?texts.length:1}}));
  return {guard,calls,sent,receipts,send,entries,directory,time};
}

test('ordinary replies send without another semantic review; memory failure cannot block them',async t=>{
  const h=chat(t,{call:async()=>{throw Error('memory unavailable');}});
  const result=await h.guard.deliver(h.entries('再玩一次昨天那个游戏嘛～'),'epoch',{send:h.send});
  assert.equal(result.state,'accepted');assert.equal(h.sent.length,1);
  await h.guard.run('reply',{send:h.send});assert.equal(h.sent.length,1);
  assert.equal(h.guard.manifests.isLive('reply'),false,'a delivered group is filed away');
});

test('a reply that cannot go out as written is withheld, never rewritten, and owed to Kin as a fact',async t=>{
  const h=chat(t);
  const result=await h.guard.deliver(h.entries('kin-context:context:invented\n共享记忆资料',{reason:'private-context-source-echo'}),'epoch',{send:h.send});
  assert.equal(result.state,'canceled');assert.equal(h.sent.length,0);
  assert.deepEqual(h.calls.map(c=>c.action).filter(a=>a!=='reply-status'&&a!=='share-cancel'),[],'no model is asked to rewrite it');
  const [owed]=h.guard.tail.owed();
  assert.deepEqual([owed.words,owed.reason,owed.unsent,owed.count],[false,'private-context-source-echo',[],1],'the words themselves are not repeated');
  // Nothing about it is ever sent later: not by a restart, not by the resume pass.
  const restarted=new ReplyGuard({directory:h.directory,receipt:async id=>h.receipts.get(id),lease:{heartbeat:false},clock:()=>h.time.ms});
  await restarted.resumeDue({guard:async()=>'send',send:h.send});
  assert.equal(h.sent.length,0);
});

test('an unknown delivery keeps its original identity; nothing is guessed and nothing is owed',async t=>{
  const h=chat(t);
  const send=async delivery=>{h.receipts.set(delivery.id,{state:'unconfirmed',submissionStarted:true});return {state:'unconfirmed',submissionStarted:true};};
  assert.equal((await h.guard.deliver(h.entries('正文'),'epoch',{send})).state,'unconfirmed');
  assert.equal(h.guard.tail.owed().length,0);
  for(const id of h.receipts.keys())h.receipts.set(id,{state:'accepted',messageId:'known'});
  assert.equal((await h.guard.run('reply',{send:h.send})).state,'accepted');assert.equal(h.sent.length,0);
});

test('a refused bubble is final, never resent, and its words are owed to Kin (the guard itself says nothing to the owner)',async t=>{
  const h=chat(t);
  const result=await h.guard.deliver(h.entries(['先说这句。','这句被拒了。']),'epoch',{send:async delivery=>delivery.text.includes('拒')?{state:'rejected'}:h.send(delivery)});
  assert.equal(result.groupState,'partial');
  assert.deepEqual(h.guard.tail.owed().map(o=>[o.words,o.sent,o.unsent]),[[true,['先说这句。'],['这句被拒了。']]]);
  assert.equal((await h.guard.resumeDue({guard:async()=>'send',send:h.send})).checked,0,'a finished group waiting for Kin takes no pass');
  assert.deepEqual(h.sent,['先说这句。']);
});

test('an intentional quiet choice remains quiet: nothing sent, nothing owed',async t=>{
  const h=chat(t,{call:async action=>action==='reply-status'?{action:'silent'}:undefined});
  assert.equal((await h.guard.deliver(h.entries('不用发送'),'epoch',{send:h.send})).state,'silent');
  assert.equal(h.sent.length,0);assert.equal(h.guard.tail.owed().length,0);
});

test('a newer owner message stops the rest at a bubble boundary; the rest goes to Kin whole, with no count and no model',async t=>{
  const h=chat(t);
  let spoke=false;
  // The owner writes while the first bubble is on its way.
  const send=async delivery=>{const r=await h.send(delivery);if(!spoke){spoke=true;h.time.ms+=1000;h.guard.tail.note({id:'new-input'});}return r;};
  h.time.ms-=5000;const draft=h.entries(['第一句。','第二句。','第三句。']);h.time.ms+=5000;
  const result=await h.guard.deliver(draft,'epoch',{send});
  assert.deepEqual(h.sent,['第一句。']);
  assert.equal(result.groupState,'retired');
  const [owed]=h.guard.tail.owed();
  assert.deepEqual([owed.sent,owed.unsent,owed.words],[['第一句。'],['第二句。','第三句。'],true]);
  // The words rode on the next owner turn: the obligation ends and the group is filed.
  await h.guard.tail.handed({groups:[owed.group_id],inputId:'new-input'});
  assert.equal(h.guard.tail.owed().length,0);assert.equal(h.guard.manifests.isLive(owed.group_id),false);
  assert.equal(h.guard.manifests.read(owed.group_id).tail_handed.input_id,'new-input');
  await h.guard.resumeDue({guard:async()=>'send',send:h.send});
  assert.deepEqual(h.sent,['第一句。'],'the withdrawn words are never sent by the host');
});

test('an owner message that arrives while an older reply waits takes that reply\'s words with it, before it is submitted',async t=>{
  const h=chat(t);
  h.guard.draft(h.entries(['还没发的一句。'],{batch:'waiting'}),'epoch','feishu');
  h.time.ms+=1000;
  await h.guard.interruptFor({inputId:'owner-2'});
  const owed=h.guard.tail.owed();
  assert.deepEqual(owed.map(o=>o.unsent),[['还没发的一句。']]);
  assert.equal(h.guard.manifests.read('waiting').state,'retired');
});

test('a literal stop withdraws what was written before it, and what was written after it goes out',async t=>{
  const h=chat(t);
  h.guard.draft(h.entries(['停之前写的。'],{batch:'before'}),'epoch','feishu');
  h.time.ms+=1000;await h.guard.tail.stopped({inputId:'stop-1'});await h.guard.tail.idleNow();
  h.time.ms+=1000;
  assert.equal((await h.guard.deliver(h.entries('停之后写的。',{batch:'after'}),'epoch',{send:h.send})).state,'accepted');
  assert.deepEqual(h.sent,['停之后写的。']);
  assert.deepEqual(h.guard.tail.owed().map(o=>o.unsent),[['停之前写的。']]);
});

test('a group the older tail left mid-decision is migrated: its promised words become owed, its links close',async t=>{
  const h=chat(t);
  h.guard.draft(h.entries(['旧的未发正文'],{batch:'old'}),'epoch','feishu');
  await h.guard.manifests.mutate('old',m=>{
    m.state='interrupted';m.reason='new-owner-input';
    m.tail_intent={id:'tail-x',decision:'rewrite_remainder',carrier:'classify',state:'recorded',items:['old-draft-0'],round:1,at:h.time.ms};
    m.tail_owed={items:[],resurfaced:1,forced:false,since:h.time.ms};
  },{operatorOnly:true});
  await h.guard.tail.recover();
  const old=h.guard.manifests.read('old');
  assert.equal(old.tail_intent.state,'settled');assert.equal(old.state,'retired');
  assert.deepEqual(h.guard.tail.owed().map(o=>o.unsent),[['旧的未发正文']]);
  assert.equal(old.tail_owed.resurfaced,undefined,'no count survives');
});

test('the router hands the owner\'s literal stop to the reply tail and asks its classifier nothing about any reply (N4)',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-chat-router-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',
    active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const heard=[],asked=[];let now=1000;
  const router=new MobileRouter({file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>++now,
    classify:async input=>{asked.push(input);return {route:'chat',reason:'synthetic'};},
    switchModel:async()=>({...runtime}),waitForIdle:async()=>{throw Error('waiting');},
    replyTail:{stopped:async detail=>{heard.push(detail);return {state:'recorded'};}}});
  await router.dispatch({id:'chat-1',text:'在吗'},async()=>'new-turn');
  await router.dispatch({id:'stop-1',text:'停止任务'},async()=>'new-turn');
  assert.deepEqual(heard,[{inputId:'stop-1'}]);
  assert.deepEqual(router.state.inputs['stop-1'].tail,{carrier:'owner-stop',state:'recorded'});
  assert.equal(asked.length,1);assert.equal('interruptedReply' in asked[0],false);assert.equal(router.state.inputs['chat-1'].tail,undefined);
});
