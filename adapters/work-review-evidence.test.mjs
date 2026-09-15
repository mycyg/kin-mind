import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {workEvidence} from './work-review-evidence.mjs';

function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-work-evidence-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  for(const dir of ['inputs','outbox','deferred'])fs.mkdirSync(path.join(root,dir));
  const put=(dir,id,value)=>fs.writeFileSync(path.join(root,dir,id+'.json'),JSON.stringify(value));
  const snapshot={task:{id:'task',inputIds:['first','second'],inputVersion:2,tools:{},deliveries:{reply:{state:'accepted',messageId:'receipt'},old:{state:'deferred'}}},inputs:[{id:'first',kind:'owner'},{id:'second',kind:'owner'}]};
  put('inputs','first',{id:'first',canonicalSessionId:'synthetic',senderId:'bound-owner',text:'You can explore when idle'});
  put('inputs','second',{id:'second',canonicalSessionId:'synthetic',senderId:'bound-owner',text:'I am back'});
  put('outbox','reply',{id:'reply',state:'accepted',messageId:'receipt',text:'Welcome back'});
  const deferredName=createHash('sha256').update('old').digest('hex')+'.pending';
  const draft={state:'pending',request:{reply_id:'first',draft_id:'draft-old',text:'I will explore'},delivery:{id:'old',taskId:'task',kind:'reply'},ownerEpoch:'original'};
  put('deferred',deferredName,draft);let canceled=0;
  const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(root,'inputs'),outboxDirectory:path.join(root,'outbox'),deferredDirectory:path.join(root,'deferred'),lastReply:async()=>({text:'Welcome back',status:'completed',turnId:'turn'}),cancelShare:async id=>{assert.equal(id,'draft-old');canceled++;}});
  return{root,put,snapshot,adapter,draft,deferredName,canceled:()=>canceled};
}

test('only older unsent ordinary drafts are eligible for semantic cancellation',async t=>{
  const f=fixture(t),e=await f.adapter.collect(f.snapshot);
  assert.equal(e.receipts.reply.messageId,'receipt');assert.equal(e.receipts.old.state,'not-submitted');assert.deepEqual(e.input.cancellableDeferred.map(x=>x.id),['old']);
  assert.equal((await f.adapter.cancelDeferred('old',{reviewId:'review',evidence:e})).state,'canceled-before-send');assert.equal(f.canceled(),1);
  assert.equal(fs.existsSync(path.join(f.root,'outbox','old.json')),false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root,'deferred',f.deferredName+'.json'))).state,'canceled');
});

test('a prepared draft, file, newest input or uncertain transport receipt cannot be discarded',async t=>{
  for(const change of [d=>d.state='prepared',d=>d.delivery.media={type:'file'},d=>d.request.reply_id='second']) {
    const f=fixture(t);change(f.draft);f.put('deferred',f.deferredName,f.draft);const e=await f.adapter.collect(f.snapshot);assert.equal(e.input.cancellableDeferred.length,0);assert.equal(e.receipts.old.state,'unconfirmed');
  }
  const f=fixture(t);f.put('outbox','old',{id:'old',state:'unconfirmed'});const e=await f.adapter.collect(f.snapshot);assert.equal(e.input.cancellableDeferred.length,0);
});

test('transport starting during cancellation or source identity changing retains uncertainty',async t=>{
  const f=fixture(t),e=await f.adapter.collect(f.snapshot);f.put('outbox','old',{id:'old',state:'pending'});
  await assert.rejects(f.adapter.cancelDeferred('old',{reviewId:'review',evidence:e}),/transport began/);assert.equal(f.canceled(),0);
  f.put('inputs','first',{id:'first',senderId:'bound-owner',canonicalSessionId:'different',text:'Unrelated input'});
  await assert.rejects(f.adapter.collect(f.snapshot),/Authenticated input missing/);
});
