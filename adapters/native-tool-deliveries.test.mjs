import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {readNativeToolDeliveries} from './native-tool-deliveries.mjs';

const sha256=value=>createHash('sha256').update(value).digest('hex');

function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-native-deliveries-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const outboxDirectory=path.join(root,'outbox'),artifactDirectory=path.join(root,'artifacts');
  fs.mkdirSync(outboxDirectory);fs.mkdirSync(artifactDirectory);
  const sessionId='session-native-1',toolId='exec-native-1',task={id:'work-native-1',tools:{[toolId]:{status:'completed'}}};
  const start=Date.parse('2026-09-20T01:40:54.000Z'),end=Date.parse('2026-09-20T01:40:57.000Z');
  const outboxId='kin-cat-proof-1',messageId='om_native_1',bytes=Buffer.from('actual image bytes');
  const artifactPath=path.join(artifactDirectory,'stored-artifact');fs.writeFileSync(artifactPath,bytes);
  const record={id:outboxId,kind:'reply',text:'private body',state:'accepted',submissionStarted:true,stage:'platform-accepted',attemptedAt:'2026-09-20T01:40:54.100Z',
    uploadedAt:'2026-09-20T01:40:55.000Z',submittedAt:'2026-09-20T01:40:55.500Z',acceptedAt:'2026-09-20T01:40:56.500Z',messageId,
    artifact:{sha256:sha256(bytes),name:'cat.jpg',bytes:bytes.length,path:artifactPath},media:{type:'image',name:'cat.jpg',bytes:bytes.length}};
  const event={timestamp:'2026-09-20T01:40:57.000Z',type:'event_msg',payload:{type:'item_completed',thread_id:sessionId,turn_id:'turn-native-1',started_at_ms:start,completed_at_ms:end,
    item:{type:'CommandExecution',id:toolId,status:'completed',exit_code:0,command:'private command with '+outboxId,stdout:JSON.stringify({state:'accepted',messageId})+'\n',stderr:''}}};
  const meta={timestamp:'2026-09-10T00:00:00.000Z',type:'session_meta',payload:{id:sessionId}};
  const file=path.join(root,'rollout.jsonl');
  const writeRollout=(records=[meta,event],tail='')=>fs.writeFileSync(file,records.map(value=>JSON.stringify(value)).join('\n')+'\n'+tail);
  const writeOutbox=(value=record,id=value.id)=>fs.writeFileSync(path.join(outboxDirectory,id+'.json'),JSON.stringify(value));
  writeRollout();writeOutbox();
  return {root,file,outboxDirectory,artifactDirectory,artifactPath,sessionId,toolId,task,start,end,outboxId,messageId,bytes,record,event,meta,writeRollout,writeOutbox,
    args(){return {file,sessionId,task,outboxDirectory,artifactRoots:[artifactDirectory]};}};
}

test('returns a bounded proof from the exact completed native tool, accepted outbox and real artifact',async t=>{
  const f=fixture(t),first=await readNativeToolDeliveries(f.args()),second=await readNativeToolDeliveries(f.args());
  assert.deepEqual(first.diagnostics,[]);assert.equal(first.proofs.length,1);assert.deepEqual(first,second,'the same persisted evidence has a stable digest');
  const proof=first.proofs[0];
  assert.deepEqual({...proof,sourceHash:undefined},{id:f.outboxId,outboxId:f.outboxId,toolId:f.toolId,sessionId:f.sessionId,taskId:f.task.id,messageId:f.messageId,
    state:'accepted',source:'native-tool-outbox',sourceHash:undefined,artifact:{sha256:sha256(f.bytes),bytes:f.bytes.length,type:'image',name:'cat.jpg'},
    toolStartedAt:'2026-09-20T01:40:54.000Z',toolCompletedAt:'2026-09-20T01:40:57.000Z',acceptedAt:'2026-09-20T01:40:56.500Z'});
  assert.match(proof.sourceHash,/^[a-f0-9]{64}$/);
  const publicShape=JSON.stringify(first);
  assert.equal(publicShape.includes('private command'),false);assert.equal(publicShape.includes('private body'),false);assert.equal(publicShape.includes(f.artifactPath),false);
});

test('an accepted stdout receipt may bind by its exact outbox id when it has no message id',async t=>{
  const f=fixture(t);f.event.payload.item.stdout=JSON.stringify({state:'accepted',id:f.outboxId})+'\n';f.writeRollout();
  const result=await readNativeToolDeliveries(f.args());
  assert.deepEqual(result.proofs.map(p=>[p.id,p.messageId]),[[f.outboxId,f.messageId]]);
});

test('an id-only receipt still rejects a second outbox that reuses its platform message id',async t=>{
  const f=fixture(t),copy={...f.record,id:'kin-cat-proof-2'};
  f.event.payload.item.stdout=JSON.stringify({state:'accepted',id:f.outboxId})+'\n';f.writeRollout();f.writeOutbox(copy);
  const result=await readNativeToolDeliveries(f.args());
  assert.equal(result.proofs.length,0);assert.deepEqual(result.diagnostics,[{toolId:f.toolId,code:'native-tool-outbox-not-unique'}]);
});

test('session ownership is mandatory and an incomplete task tool has no authority',async t=>{
  const f=fixture(t);
  await assert.rejects(readNativeToolDeliveries({...f.args(),sessionId:'another-session'}),error=>error.code==='native-rollout-session-mismatch');
  f.task.tools[f.toolId].status='failed';
  assert.deepEqual(await readNativeToolDeliveries(f.args()),{proofs:[],diagnostics:[]});
});

test('assistant text, reasoning, raw command text and similar tool ids cannot create delivery proof',async t=>{
  const f=fixture(t),accepted=JSON.stringify({state:'accepted',messageId:f.messageId});
  f.event.payload.item.id=f.toolId+'-historical';f.event.payload.item.stdout='not a receipt';
  const assistant={timestamp:'2026-09-20T01:40:55.000Z',type:'response_item',payload:{type:'message',role:'assistant',content:[{type:'output_text',text:accepted+' '+f.toolId}]}};
  const reasoning={timestamp:'2026-09-20T01:40:55.000Z',type:'event_msg',payload:{type:'item_completed',thread_id:f.sessionId,turn_id:'turn-native-1',started_at_ms:f.start,completed_at_ms:f.end,
    item:{type:'Reasoning',id:'reasoning-1',status:'completed',raw_content:accepted+' '+f.toolId}}};
  f.writeRollout([f.meta,assistant,reasoning,f.event]);
  assert.deepEqual(await readNativeToolDeliveries(f.args()),{proofs:[],diagnostics:[]});
});

test('a nonterminal, failed, cross-session or duplicate terminal event is never accepted',async t=>{
  for(const change of [
    f=>{f.event.payload.item.status='in_progress';},
    f=>{f.event.payload.item.exit_code=1;},
    f=>{f.event.payload.thread_id='another-session';},
    f=>{f.writeRollout([f.meta,f.event,f.event]);return true;},
  ]){
    await t.test(change.name||'invalid event',async st=>{const f=fixture(st),written=change(f);if(!written)f.writeRollout();const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);});
  }
});

test('two completed task tools cannot count the same platform delivery twice',async t=>{
  const f=fixture(t),secondId='exec-native-2',second=structuredClone(f.event);
  second.payload.item.id=secondId;second.payload.turn_id='turn-native-2';f.task.tools[secondId]={status:'completed'};f.writeRollout([f.meta,f.event,second]);
  const result=await readNativeToolDeliveries(f.args());
  assert.deepEqual(result.proofs,[]);assert.deepEqual(result.diagnostics,[{toolId:f.toolId,code:'native-tool-proof-conflict'},{toolId:secondId,code:'native-tool-proof-conflict'}]);
});

test('only one exact outbox with the stdout message id can satisfy the tool receipt',async t=>{
  await t.test('no matching message id',async st=>{const f=fixture(st);f.event.payload.item.stdout=JSON.stringify({state:'accepted',messageId:'om_absent'})+'\n';f.writeRollout();const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);assert.equal(result.diagnostics[0].code,'native-tool-outbox-not-unique');});
  await t.test('duplicate platform message id',async st=>{const f=fixture(st),copy={...f.record,id:'kin-cat-proof-2'};f.writeOutbox(copy);const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);assert.equal(result.diagnostics[0].code,'native-tool-outbox-not-unique');});
  await t.test('non-accepted outbox',async st=>{const f=fixture(st);f.writeOutbox({...f.record,state:'pending'});const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);assert.equal(result.diagnostics[0].code,'native-tool-outbox-invalid');});
});

test('historical receipts outside the native tool interval cannot be reused',async t=>{
  const f=fixture(t);f.writeOutbox({...f.record,attemptedAt:'2026-09-20T01:39:54.100Z'});
  const result=await readNativeToolDeliveries(f.args());
  assert.equal(result.proofs.length,0);assert.deepEqual(result.diagnostics,[{toolId:f.toolId,code:'native-tool-outbox-invalid'}]);
});

test('artifact scope, bytes and sha256 are independently verified',async t=>{
  await t.test('declared hash differs from disk',async st=>{const f=fixture(st);f.writeOutbox({...f.record,artifact:{...f.record.artifact,sha256:'0'.repeat(64)}});const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);assert.equal(result.diagnostics[0].code,'native-tool-artifact-invalid');});
  await t.test('declared size differs from disk',async st=>{const f=fixture(st),bytes=f.record.artifact.bytes+1;f.writeOutbox({...f.record,artifact:{...f.record.artifact,bytes},media:{...f.record.media,bytes}});const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);});
  await t.test('artifact is outside the host roots',async st=>{const f=fixture(st),outside=path.join(f.root,'outside');fs.writeFileSync(outside,f.bytes);f.writeOutbox({...f.record,artifact:{...f.record.artifact,path:outside}});const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);});
  await t.test('artifact path is a symlink',async st=>{const f=fixture(st),link=path.join(f.artifactDirectory,'link');fs.symlinkSync(f.artifactPath,link);f.writeOutbox({...f.record,artifact:{...f.record.artifact,path:link}});const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);});
});

test('proof media uses the same nonempty contract as work evidence',async t=>{
  await t.test('zero byte artifact',async st=>{const f=fixture(st),empty=Buffer.alloc(0);fs.writeFileSync(f.artifactPath,empty);f.writeOutbox({...f.record,artifact:{...f.record.artifact,sha256:sha256(empty),bytes:0},media:{...f.record.media,bytes:0}});assert.equal((await readNativeToolDeliveries(f.args())).proofs.length,0);});
  await t.test('unknown media type',async st=>{const f=fixture(st);f.writeOutbox({...f.record,media:{...f.record.media,type:'thumbnail'}});assert.equal((await readNativeToolDeliveries(f.args())).proofs.length,0);});
  await t.test('empty artifact name',async st=>{const f=fixture(st);f.writeOutbox({...f.record,artifact:{...f.record.artifact,name:''},media:{...f.record.media,name:''}});assert.equal((await readNativeToolDeliveries(f.args())).proofs.length,0);});
});

test('read corruption fails closed while a live unterminated rollout tail is ignored',async t=>{
  await t.test('relevant malformed JSONL',async st=>{const f=fixture(st);fs.writeFileSync(f.file,'{"type":"session_meta"\n');await assert.rejects(readNativeToolDeliveries(f.args()),error=>error.code==='native-rollout-invalid-session-meta');});
  await t.test('missing artifact',async st=>{const f=fixture(st);fs.unlinkSync(f.artifactPath);await assert.rejects(readNativeToolDeliveries(f.args()),error=>error.code==='native-tool-artifact-unreadable');});
  await t.test('partial append',async st=>{const f=fixture(st);f.writeRollout([f.meta,f.event],'{"type":"event_msg"');assert.equal((await readNativeToolDeliveries(f.args())).proofs.length,1);});
  await t.test('oversize unterminated line',async st=>{const f=fixture(st);f.writeRollout([f.meta],'{'.padEnd(16*1024*1024+2,'x'));await assert.rejects(readNativeToolDeliveries(f.args()),error=>error.code==='native-rollout-line-too-large');});
  await t.test('out of range native time',async st=>{const f=fixture(st);f.event.payload.started_at_ms=Number.MAX_VALUE;f.writeRollout();const result=await readNativeToolDeliveries(f.args());assert.equal(result.proofs.length,0);assert.equal(result.diagnostics[0].code,'native-tool-event-invalid');});
});
