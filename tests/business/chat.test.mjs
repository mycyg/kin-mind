import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ReplyGuard} from '../../adapters/reply-guard.mjs';

function chat(t, options={}) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-chat-'));
  t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const calls=[],sent=[],notices=[],receipts=new Map();
  const guard=new ReplyGuard({directory,replyTailDecision:false,sleep:async()=>{},
    call:async action=>{calls.push(action);if(action==='reply-status')return {action:'none'};},
    receipt:async id=>receipts.get(id),
    notifyFailure:async group=>{notices.push(group.groupId);return {state:'accepted',messageId:'notice'};},
    ...options});
  const send=async delivery=>{sent.push(delivery.text);const r={state:'accepted',messageId:delivery.id,acceptedAt:new Date().toISOString()};receipts.set(delivery.id,r);return r;};
  const entries=(text,reason)=>[{request:{reply_id:'input',draft_id:'draft',text,...(reason?{repair_reason:reason}:{})},
    delivery:{id:'bubble',memoryBatchId:'reply',text,kind:'reply'}}];
  return {guard,calls,sent,notices,receipts,send,entries,directory};
}

test('ordinary replies send without another semantic review; memory failure cannot block them',async t=>{
  const h=chat(t,{call:async()=>{throw Error('memory unavailable');}});
  const result=await h.guard.deliver(h.entries('再玩一次昨天那个游戏嘛～'),'epoch',{send:h.send});
  assert.equal(result.state,'accepted');assert.equal(h.sent.length,1);
  await h.guard.run('reply',{send:h.send});assert.equal(h.sent.length,1);
});

test('bad public body is corrected once, with the original input and draft',async t=>{
  let calls=0;
  const h=chat(t,{regenerate:async request=>{calls++;assert.equal(request.input_id,'input');assert.equal(request.draft,'bad envelope');return {bubbles:['好呀，继续玩～']};}});
  const results=await Promise.all([h.guard.deliver(h.entries('bad envelope','format'),'epoch',{send:h.send}),h.guard.run('reply',{send:h.send})]);
  assert.ok(results.every(r=>r.state==='accepted'));assert.equal(calls,1);assert.deepEqual(h.sent,['好呀，继续玩～']);
  const afterRestart=new ReplyGuard({directory:h.directory,replyTailDecision:false,receipt:async id=>h.receipts.get(id),regenerate:async()=>{throw Error('must not call');}});
  assert.equal((await afterRestart.run('reply',{send:h.send})).state,'accepted');assert.equal(h.sent.length,1);
});

test('failed correction ends with one visible status, including after restart',async t=>{
  let calls=0;const h=chat(t,{regenerate:async()=>{calls++;throw Error('model unavailable');}});
  const result=await h.guard.deliver(h.entries('bad','format'),'epoch',{send:h.send});
  assert.equal(result.state,'failed');assert.equal(result.notified,true);assert.equal(calls,1);assert.equal(h.sent.length,0);assert.equal(h.notices.length,1);
  const g=new ReplyGuard({directory:h.directory,replyTailDecision:false,regenerate:async()=>{throw Error('must not call');},notifyFailure:async()=>{throw Error('must not call');}});
  assert.equal((await g.run('reply',{send:h.send})).notified,true);
});

test('unknown delivery keeps its original identity and never starts a text correction',async t=>{
  let repairs=0;const h=chat(t,{regenerate:async()=>{repairs++;return {bubbles:['replacement']};}});
  const send=async delivery=>{h.receipts.set(delivery.id,{state:'unconfirmed'});return {state:'unconfirmed'};};
  assert.equal((await h.guard.deliver(h.entries('正文'),'epoch',{send})).state,'unconfirmed');
  assert.equal(repairs,0);assert.equal(h.notices.length,0);
  for(const id of h.receipts.keys())h.receipts.set(id,{state:'accepted',messageId:'known'});
  assert.equal((await h.guard.run('reply',{send:h.send})).state,'accepted');assert.equal(h.sent.length,0);
});

test('terminal transport failure sends a status once and remains a failure',async t=>{
  const h=chat(t);
  const result=await h.guard.deliver(h.entries('正文'),'epoch',{send:async()=>({state:'rejected'})});
  assert.equal(result.state,'failed');assert.equal(result.notified,true);
  assert.equal((await h.guard.run('reply',{send:h.send})).state,'failed');assert.equal(h.notices.length,1);
});

test('an intentional quiet choice remains quiet, not a failed reply',async t=>{
  const h=chat(t,{call:async()=>({action:'silent'})});
  assert.equal((await h.guard.deliver(h.entries('不用发送'),'epoch',{send:h.send})).state,'silent');
  assert.equal(h.sent.length,0);assert.equal(h.notices.length,0);
});

test('a DS-approved replacement settles the old unsent remainder only after its delivery',async t=>{
 const h=chat(t,{replyTailDecision:true,ownerEpoch:()=>1});
 h.guard.draft(h.entries('旧的未发正文'),1,'feishu');
 await h.guard.manifests.mutate('reply',m=>{m.state='interrupted';},{operatorOnly:true});
 const old=h.guard.manifests.read('reply');
 await h.guard.tail.decided({inputId:'new-input',key:h.guard.tail.key(old),tail:{decision:'rewrite_remainder',reason:'Continue in the new reply'}});
 await h.guard.tail.recover();
 const next=h.entries('新的完整回复');next[0].request.draft_id='new-draft';next[0].request.reply_id='new-input';
 Object.assign(next[0].delivery,{id:'new-bubble',memoryBatchId:'new-reply'});
 assert.equal((await h.guard.deliver(next,1,{send:h.send})).state,'accepted');
 await h.guard.tail.recover();
 assert.deepEqual(h.sent,['新的完整回复']);assert.equal(h.guard.tail.owed().length,0);
 assert.equal(h.guard.manifests.read('reply').tail_intent.outcome.state,'replaced');
});


test('a repeated private envelope repair fails once and never reaches transport',async t=>{
  let calls=0;const bad='kin-context:context:invented\n共享记忆资料';
  const h=chat(t,{regenerate:async()=>{calls++;return {bubbles:[bad]};}});
  const result=await h.guard.replyGroup([{request:{draft_id:'bad',reply_id:'input',text:bad},delivery:{id:'bad',text:bad,memoryBatchId:'bad'}}],'epoch',{send:h.send});
  assert.equal(calls,1);assert.equal(result.state,'failed');
  assert.equal(h.sent.length,0);
});
