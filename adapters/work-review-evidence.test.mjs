import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {workEvidence} from './work-review-evidence.mjs';
import {TransportManifests} from './transport-manifest.mjs';

const digest=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');

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

test('a bubble the transport will never deliver is reported as that, not as an unconfirmed delivery',async t=>{
  for(const [state,reason,reported] of [['rejected','platform-rejected','platform-rejected'],['undeliverable','file-exceeds-channel-limit','file-exceeds-channel-limit'],
    ['undeliverable','A sentence the platform wrote','transport-undeliverable']]) {
    const f=manifestFixture(t);
    await f.manifests.mutate('reply-deferred',m=>{Object.assign(m.bubbles[0],{state,reason});},{operatorOnly:true});
    const evidence=await f.adapter.collect(f.snapshot);
    assert.deepEqual(evidence.receipts['bubble-0'],{state,reason:reported,source:'transport-manifest'},'the state and a static reason, never the platform’s own words');
    assert.deepEqual(evidence.input.outputs.find(o=>o.id==='bubble-0'),{id:'bubble-0',undelivered:true,reason:reported,receivedByServer:false});
    assert.equal(evidence.receipts.reply.state,'accepted','what was delivered is unchanged');
    assert.deepEqual(evidence.input.cancellableDeferred,[],'a group one of whose bubbles is settled is no longer a deferred draft');
  }
});

test('native media receipts supplement task evidence and are re-read before commit',async t=>{
  const f=fixture(t);f.snapshot.task.tools={original:{status:'completed'}};
  let calls=0;
  const proof={id:'image-outbox',toolId:'original',sessionId:'synthetic',taskId:'task',messageId:'image-message',state:'accepted',source:'native-tool-outbox',sourceHash:'a'.repeat(64),artifact:{sha256:'b'.repeat(64),bytes:12,type:'image',name:'result.jpg'}};
  const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),lastReply:async()=>null,toolDeliveries:async snapshot=>{assert.equal(snapshot.task.id,'task');calls++;return{proofs:[proof]};}});
  const first=await adapter.collect(f.snapshot);
  assert.equal(first.input.outputs.find(o=>o.id==='native-tool:image-outbox').file.sha256,'b'.repeat(64));
  assert.deepEqual(first.receipts['native-tool:image-outbox'],{state:'accepted',messageId:'image-message',source:'native-tool-outbox',sourceHash:'a'.repeat(64),proofHash:digest(proof)});
  assert.equal(first.receipts.reply.messageId,'receipt');
  proof.sourceHash='c'.repeat(64);
  const second=await adapter.collect(f.snapshot);
  assert.equal(calls,2);
  assert.equal(second.receipts['native-tool:image-outbox'].sourceHash,'c'.repeat(64));
  assert.notEqual(first.receipts['native-tool:image-outbox'].sourceHash,second.receipts['native-tool:image-outbox'].sourceHash);
  assert.notEqual(first.receipts['native-tool:image-outbox'].proofHash,second.receipts['native-tool:image-outbox'].proofHash);
  assert.notEqual(digest(first),digest(second),'the pre-commit evidence hash covers the complete native proof');
  assert.equal(f.snapshot.task.deliveries['native-tool:image-outbox'],undefined,'collection never manufactures a task delivery');
});

test('a native proof enriches one original accepted delivery without counting the platform message twice',async t=>{
  const f=fixture(t);f.snapshot.task.tools={original:{status:'completed'}};
  const original=(await f.adapter.collect(f.snapshot)).receipts.reply.sourceHash;
  const artifact={sha256:'b'.repeat(64),bytes:12,type:'image',name:'result.jpg'};
  const proof={id:'image-outbox',toolId:'original',sessionId:'synthetic',taskId:'task',messageId:'receipt',state:'accepted',source:'native-tool-outbox',sourceHash:'a'.repeat(64),artifact};
  const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),lastReply:async()=>null,toolDeliveries:async()=>({proofs:[proof]})});
  const evidence=await adapter.collect(f.snapshot),output=evidence.input.outputs.find(o=>o.id==='reply');
  assert.equal(evidence.input.outputs.filter(o=>evidence.receipts[o.id]?.messageId==='receipt').length,1);
  assert.equal(evidence.receipts['native-tool:image-outbox'],undefined);
  assert.deepEqual(output,{id:'reply',toolId:'original',text:'Welcome back',file:artifact,receivedByServer:true});
  assert.deepEqual(evidence.receipts.reply,{state:'accepted',messageId:'receipt',sourceHash:original,nativeSourceHash:'a'.repeat(64),proofHash:digest(proof)});

  // The verified native source may also supply the missing independent receipt
  // for an accepted router delivery; it still keeps the router's delivery ID.
  const g=fixture(t);g.snapshot.task.tools={original:{status:'completed'}};
  fs.rmSync(path.join(g.root,'outbox','reply.json'));
  const recovered=await workEvidence({sessionId:'synthetic',inputDirectory:path.join(g.root,'inputs'),outboxDirectory:path.join(g.root,'outbox'),deferredDirectory:path.join(g.root,'deferred'),lastReply:async()=>null,toolDeliveries:async()=>({proofs:[proof]})}).collect(g.snapshot);
  assert.equal(recovered.receipts.reply.state,'accepted');
  assert.equal(recovered.receipts.reply.nativeSourceHash,'a'.repeat(64));
  assert.deepEqual(recovered.input.outputs.find(o=>o.id==='reply').file,artifact);
  assert.equal(recovered.input.outputs.some(o=>o.id==='native-tool:image-outbox'),false);
});

test('native enrichment refuses an original artifact or tool identity conflict',async t=>{
  const artifact={sha256:'b'.repeat(64),bytes:12,type:'image',name:'result.jpg'};
  const proof={id:'image-outbox',toolId:'original',sessionId:'synthetic',taskId:'task',messageId:'receipt',state:'accepted',source:'native-tool-outbox',sourceHash:'a'.repeat(64),artifact};
  for(const change of [
    {artifact:{...artifact,type:'audio'}},
    {artifact:{...artifact,name:'other.jpg'}},
    {artifact:{...artifact,sha256:'c'.repeat(64)}},
    {artifact:{...artifact,bytes:13}},
  ]) {
    const f=fixture(t);f.snapshot.task.tools={original:{status:'completed'}};
    f.put('outbox','reply',{id:'reply',state:'accepted',messageId:'receipt',text:'Welcome back',media:{type:'image',name:'result.jpg',bytes:12},artifact});
    const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),lastReply:async()=>null,toolDeliveries:async()=>({proofs:[{...proof,...change}]})});
    await assert.rejects(adapter.collect(f.snapshot),/Native delivery artifact collision/);
  }
  const f=fixture(t);f.snapshot.task.tools={original:{status:'completed'},other:{status:'completed'}};
  f.snapshot.task.deliveries['file-tool:other']={state:'accepted',messageId:'image-message'};
  const differentTool={...proof,messageId:'image-message'};
  const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),lastReply:async()=>null,toolDeliveries:async()=>({proofs:[differentTool]})});
  await assert.rejects(adapter.collect(f.snapshot),/Native delivery tool collision/);
});

test('native proof IDs and platform message IDs are unique and cannot collide with router evidence',async t=>{
  const f=fixture(t);f.snapshot.task.tools={original:{status:'completed'}};
  const base={id:'image-outbox',toolId:'original',sessionId:'synthetic',taskId:'task',messageId:'image-message',state:'accepted',source:'native-tool-outbox',sourceHash:'a'.repeat(64),artifact:{sha256:'b'.repeat(64),bytes:12,type:'image',name:'result.jpg'}};
  const collect=(proofs,snapshot=f.snapshot)=>workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),lastReply:async()=>null,toolDeliveries:async()=>({proofs})}).collect(snapshot);
  await assert.rejects(collect([base,{...base,messageId:'another-message'}]),/Duplicate native delivery proof/);
  await assert.rejects(collect([base,{...base,id:'another-proof'}]),/Duplicate native delivery message/);

  const identityCollision=structuredClone(f.snapshot);
  identityCollision.task.deliveries['native-tool:image-outbox']={state:'accepted',messageId:'router-message'};
  await assert.rejects(collect([base],identityCollision),/Native delivery identity collision/);
  identityCollision.task.deliveries.reply.messageId='image-message';
  await assert.rejects(collect([base],identityCollision),/Native delivery identity collision/,
    'matching another original message cannot hide a collision with the synthetic proof ID');

  const messageCollision=structuredClone(f.snapshot);
  messageCollision.task.deliveries.other={state:'accepted',messageId:'receipt'};
  await assert.rejects(collect([{...base,messageId:'receipt'}],messageCollision),/Native delivery message collision/);
  const unfinished=structuredClone(f.snapshot);
  unfinished.task.deliveries.other={state:'unconfirmed',messageId:'image-message'};
  await assert.rejects(collect([base],unfinished),/Native delivery message collision/);
});

test('duplicate original platform identities fail even when direct outbox lookup succeeds',async t=>{
  const f=fixture(t);
  f.put('outbox','another',{id:'another',state:'accepted',messageId:'receipt',text:'Different record'});
  await assert.rejects(f.adapter.collect(f.snapshot),/Platform message identity collision/);
});

test('native delivery gaps are review evidence while malformed or conflicting diagnostics fail',async t=>{
  const f=fixture(t);f.snapshot.task.tools={original:{status:'completed'},second:{status:'completed'}};
  const args={sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),lastReply:async()=>null};
  let diagnostics=[{toolId:'second',code:'native-tool-artifact-invalid'},{toolId:'original',code:'native-tool-outbox-not-unique'}];
  const adapter=workEvidence({...args,toolDeliveries:async()=>({proofs:[],diagnostics})});
  const first=await adapter.collect(f.snapshot);
  assert.deepEqual(first.input.deliveryEvidenceGaps,[
    {toolId:'original',code:'native-tool-outbox-not-unique'},
    {toolId:'second',code:'native-tool-artifact-invalid'},
  ]);
  diagnostics=[{toolId:'original',code:'native-tool-outbox-invalid'}];
  const second=await adapter.collect(f.snapshot);
  assert.notEqual(digest(first),digest(second),'the review evidence hash covers changed delivery gaps');
  assert.equal(second.receipts.reply.messageId,'receipt','a gap does not erase independently accepted output');

  for(const value of [
    {},
    [{toolId:'original',code:'unknown'}],
    [{toolId:'missing',code:'native-tool-outbox-invalid'}],
    [{toolId:'original',code:'native-tool-outbox-invalid',detail:'private'}],
    [{toolId:'original',code:'native-tool-outbox-invalid'},{toolId:'original',code:'native-tool-outbox-invalid'}],
  ]) await assert.rejects(workEvidence({...args,toolDeliveries:async()=>({proofs:[],diagnostics:value})}).collect(f.snapshot),/Invalid native delivery evidence/);

  for(const code of ['native-tool-receipt-ambiguous','native-tool-event-invalid','native-tool-proof-conflict'])
    await assert.rejects(workEvidence({...args,toolDeliveries:async()=>({proofs:[],diagnostics:[{toolId:'original',code}]})}).collect(f.snapshot),/Native delivery evidence integrity conflict/);
});

test('native media proof cannot belong to a different session, task or unfinished tool',async t=>{
  const f=fixture(t);f.snapshot.task.tools={original:{status:'completed'}};
  const base={id:'image-outbox',toolId:'original',sessionId:'synthetic',taskId:'task',messageId:'image-message',state:'accepted',source:'native-tool-outbox',sourceHash:'a'.repeat(64),artifact:{sha256:'b'.repeat(64),bytes:12,type:'image',name:'result.jpg'}};
  for(const change of [{sessionId:'other'},{taskId:'other'},{toolId:'invented'},{state:'unconfirmed'},{messageId:''},{sourceHash:''},
    {artifact:{...base.artifact,sha256:''}},{artifact:{...base.artifact,bytes:0}},{artifact:{...base.artifact,type:'text'}},{artifact:{...base.artifact,name:null}}]) {
    const adapter=workEvidence({sessionId:'synthetic',inputDirectory:path.join(f.root,'inputs'),outboxDirectory:path.join(f.root,'outbox'),deferredDirectory:path.join(f.root,'deferred'),lastReply:async()=>null,toolDeliveries:async()=>({proofs:[{...base,...change}]})});
    await assert.rejects(adapter.collect(f.snapshot),/Invalid native delivery proof/);
  }
});
