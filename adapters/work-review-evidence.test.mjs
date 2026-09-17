import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {workEvidence} from './work-review-evidence.mjs';
import {TransportManifests} from './transport-manifest.mjs';

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

// ---- deferred replies held as transport manifests (D8) -----------------------
function manifestFixture(t,{texts=['I will explore','and report back','when I am done'],replyId='first',taskId='task'}={}) {
  const f=fixture(t),released=[];
  const manifests=new TransportManifests({directory:path.join(f.root,'reply-manifests'),lease:{heartbeat:false},sleep:async()=>{},cancelShare:async draftId=>{released.push(draftId);}});
  const entries=texts.map((text,i)=>({request:{draft_id:'draft-'+i,reply_id:replyId,text},delivery:{id:'bubble-'+i,text,kind:'reply',draftId:'draft-'+i,memoryBatchId:'reply-deferred',expectedBubbles:texts.length,taskId}}));
  manifests.createDraft({entries,ownerEpoch:'original',hold:{reason:'share-review-pending',retryAt:Date.now()+3600000}});
  const snapshot={...f.snapshot,task:{...f.snapshot.task,deliveries:{reply:{state:'accepted',messageId:'receipt'},...Object.fromEntries(entries.map(e=>[e.delivery.id,{state:'deferred'}]))}}};
  const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),
    lastReply:async()=>({text:'Welcome back',status:'completed',turnId:'turn'}),manifests:()=>manifests,cancelShare:async()=>{throw Error('the manifest releases its own reservations');}});
  return {...f,manifests,snapshot,adapter,released,entries};
}

test('a deferred group held as a manifest is cancelled as one unit: the reservation of every bubble is released, each once',async t=>{
  const f=manifestFixture(t),evidence=await f.adapter.collect(f.snapshot);
  assert.deepEqual(evidence.input.cancellableDeferred.map(x=>[x.id,x.replyId,x.text]),[['bubble-0','first','I will explore'],['bubble-1','first','and report back'],['bubble-2','first','when I am done']]);
  assert.deepEqual(['bubble-0','bubble-1','bubble-2'].map(id=>evidence.receipts[id].state),['not-submitted','not-submitted','not-submitted']);
  assert.deepEqual(evidence.deferredGroups,{'bubble-0':'reply-deferred','bubble-1':'reply-deferred','bubble-2':'reply-deferred'});
  for(const id of ['bubble-0','bubble-1','bubble-2'])assert.deepEqual(await f.adapter.cancelDeferred(id,{reviewId:'review-1',evidence}),{state:'canceled-before-send',reviewId:'review-1'});
  assert.deepEqual(f.released,['draft-0','draft-1','draft-2'],'not only the first bubble, and none of them twice');
  const retired=f.manifests.read('reply-deferred');
  assert.deepEqual([retired.state,retired.reason,retired.work_review_id,...retired.bubbles.map(b=>b.state)],['retired','superseded-ordinary-reply','review-1','canceled','canceled','canceled']);
  assert.deepEqual(f.manifests.live(),[],'nothing of it can be sent any more');
  assert.deepEqual((await f.adapter.collect(f.snapshot)).input.cancellableDeferred,[]);
});

test('a manifest group that was reviewed, began, answers the newest input or belongs to another task cannot be discarded',async t=>{
  for(const [options,change] of [[{},m=>{m.review={id:'review',checked_hashes:[]};}],[{},m=>{m.state='sending';}],[{replyId:'second'},null],[{taskId:'another-task'},null],
    [{},m=>{m.bubbles[1].fragments=[{transport_id:'kin-frag-x',index:0,start:0,end:m.bubbles[1].text.length,body_sha256:m.bubbles[1].body_sha256,kind:'text',state:'submitting',receipt:null}];}]]) {
    const f=manifestFixture(t,options);
    if(change)await f.manifests.mutate('reply-deferred',change,{operatorOnly:true});
    const evidence=await f.adapter.collect(f.snapshot);
    assert.deepEqual([evidence.input.cancellableDeferred,evidence.receipts['bubble-0'].state,evidence.deferredGroups],[[],'unconfirmed',undefined]);
  }
});

test('a send that begins between the review and the cancellation keeps the group: nothing is released',async t=>{
  const f=manifestFixture(t),evidence=await f.adapter.collect(f.snapshot);
  const transport=async delivery=>({state:'accepted',messageId:'om-'+delivery.bubbleId});
  await f.manifests.run('reply-deferred',{transport});
  await assert.rejects(f.adapter.cancelDeferred('bubble-0',{reviewId:'review-1',evidence}),/changed or transport began/);
  assert.deepEqual([f.released,f.manifests.read('reply-deferred').state],[[],'accepted']);
  // While a group is still live, a bubble the platform took is evidence of delivery under its bubble ID.
  const g=manifestFixture(t);let sent=0;
  await g.manifests.run('reply-deferred',{transport,guard:async()=>++sent<3?'send':'interrupt'});
  const partial=await g.adapter.collect({...g.snapshot,task:{...g.snapshot.task,deliveries:{...g.snapshot.task.deliveries,'bubble-0':{state:'accepted',messageId:'om-bubble-0'}}}});
  assert.deepEqual(partial.receipts['bubble-0'],{state:'accepted',messageId:'om-bubble-0',source:'transport-manifest'});
  assert.deepEqual([partial.receipts['bubble-1'].state,partial.input.cancellableDeferred],['unconfirmed',[]]);
  assert.ok(partial.input.outputs.some(o=>o.id==='bubble-0'&&o.text==='I will explore'&&o.receivedByServer));
});

test('the old journal releases every bubble of a deferred group too',async t=>{
  const f=fixture(t),released=[];
  const entries=['old','old-1','old-2'].map((id,i)=>({request:{reply_id:'first',draft_id:'draft-'+id,text:'Part '+i},delivery:{id,taskId:'task',kind:'reply'},state:'unsent'}));
  f.put('deferred',f.deferredName,{...f.draft,request:entries[0].request,entries});
  const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),
    lastReply:async()=>({text:'Welcome back',status:'completed',turnId:'turn'}),cancelShare:async id=>{released.push(id);}});
  const evidence=await adapter.collect(f.snapshot);
  assert.equal((await adapter.cancelDeferred('old',{reviewId:'review',evidence})).state,'canceled-before-send');
  assert.deepEqual(released,['draft-old','draft-old-1','draft-old-2']);
});

test('a bubble the host retired is settled evidence, and stays readable after its group is filed away',async t=>{
  const f=manifestFixture(t);
  await f.manifests.retireRemainder('reply-deferred',{reason:'input-or-session-superseded'});
  assert.deepEqual(f.manifests.live(),[],'the group is filed away, out of the live set');
  assert.deepEqual(f.released,['draft-0','draft-1','draft-2']);
  const evidence=await f.adapter.collect(f.snapshot);
  assert.deepEqual(['bubble-0','bubble-1','bubble-2'].map(id=>evidence.receipts[id]),
    Array.from({length:3},()=>({state:'retired',reason:'input-or-session-superseded',source:'transport-manifest'})));
  assert.deepEqual(evidence.input.cancellableDeferred,[],'there is nothing left to discard');
  assert.deepEqual(evidence.input.outputs.filter(o=>o.retired).map(o=>o.id),['bubble-0','bubble-1','bubble-2']);
  assert.equal(evidence.receipts.reply.state,'accepted','what was delivered is unchanged');
});

test('a group that is merely held is still unconfirmed, never mistaken for retired',async t=>{
  const f=manifestFixture(t);
  await f.manifests.mutate('reply-deferred',m=>{m.review={id:'review',checked_hashes:[]};},{operatorOnly:true});
  const evidence=await f.adapter.collect(f.snapshot);
  assert.deepEqual(['bubble-0','bubble-1','bubble-2'].map(id=>evidence.receipts[id].state),['unconfirmed','unconfirmed','unconfirmed']);
});
