import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter} from '../../adapters/mobile-router.mjs';

function fixture(t) {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'failure-notice-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  let now=1000000;
  const args={file:path.join(dir,'router.json'),sessionId:'synthetic',now:()=>now};
  const router=new MobileRouter(args);
  const add=(id,turn=100)=>router.state.inputs[id]={id,kind:'owner',channel:'feishu',state:'accepted',executionEpoch:0,
    submit:{sessionId:'synthetic'},turnStartedAt:turn,turnEndedAt:turn+10,at:turn,nativeFailure:{code:'native-turn-failed',at:turn+10}};
  return {router,args,add,advance:()=>now+=11*60000};
}
test('one native failure notifies once for its inputs, including an emoji, across reload',async t=>{
  const f=fixture(t);for(const id of ['request','supplement','emoji'])f.add(id);
  f.router.state.inputs.emoji.text='[wave]';
  const sent=[];const notifyOwner=async(kind,id)=>{sent.push({kind,id});return {state:'accepted',messageId:'notice-1'};};
  await f.router.watch({notifyOwner});f.advance();
  const restored=new MobileRouter(f.args);await restored.watch({notifyOwner});f.advance();await restored.watch({notifyOwner});
  assert.equal(sent.length,1);
  for(const r of Object.values(restored.state.inputs)){assert.equal(r.ownerNotice.messageId,'notice-1');assert.equal(r.answer,undefined,'notification is not a reply');}
});
test('an already accepted notice covers the same old failure without another owner send',async t=>{
  const f=fixture(t);const first=f.add('already-told');f.add('old-tail');
  first.ownerNotice={id:'original-notice',kind:'stopped',state:'accepted',messageId:'old-receipt'};
  await f.router.watch({notifyOwner:async()=>assert.fail('already reported this failure')});
  assert.equal(f.router.state.inputs['old-tail'].ownerNotice.messageId,'old-receipt');
  assert.equal(f.router.state.inputs['old-tail'].answer,undefined);
});
test('a rejected notice retries its original identity instead of moving to the next failed input',async t=>{
  const f=fixture(t);f.add('first');f.add('second');const sent=[];
  const notifyOwner=async(kind,id)=>{sent.push(id);return {state:'rejected'};};
  await f.router.watch({notifyOwner});f.advance();await f.router.watch({notifyOwner});
  assert.deepEqual(sent,['first','first']);
});
test('a real reply or handoff settles old failures but an unrelated later reply does not',async t=>{
  const f=fixture(t);const covered=f.add('covered'),handed=f.add('handed',200),independent=f.add('independent',300),newer=f.add('newer',400);
  delete newer.nativeFailure;covered.ownerNotice={id:'unsent',kind:'stopped',state:'not-submitted'};
  handed.replyHandoff={inputId:'newer'};
  await f.router.observe('reply-complete',{inputId:'newer',answeredInputIds:['covered']});
  const sent=[];await f.router.watch({notifyOwner:async(kind,id)=>{sent.push(id);return {state:'accepted',messageId:'n'};}});
  assert.deepEqual(sent,['independent']);assert.equal(covered.answer.state,'covered');assert.equal(handed.answer.state,'merged');assert.equal(independent.answer,undefined);
});
test('unknown delivery in the failed turn remains unknown rather than being reported as stopped',async t=>{
  const f=fixture(t);f.add('first');f.add('second').uncertainDeliveries=['bubble'];
  const sent=[];await f.router.watch({notifyOwner:async(kind)=>{sent.push(kind);return {state:'accepted',messageId:'n'};}});
  assert.deepEqual(sent,['unknown']);
});

test('a resolved input with an unsent notice does not hide an unresolved input from the same turn',async t=>{
  for(const state of ['not-submitted','rejected']) {
    const f=fixture(t);const resolved=f.add('resolved');f.add('still-owed');
    resolved.ownerNotice={id:'never-accepted',kind:'stopped',state,nextAt:0};
    await f.router.observe('reply-complete',{inputId:'resolved'});
    const sent=[];await f.router.watch({notifyOwner:async(kind,id)=>{sent.push(id);return {state:'accepted',messageId:'n'};}});
    assert.deepEqual(sent,['still-owed']);assert.equal(resolved.answer.state,'accepted');
  }
});
