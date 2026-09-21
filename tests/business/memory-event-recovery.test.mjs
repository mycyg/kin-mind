import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MemoryEventJournal,deliveryEvent,reconcileOutbox} from '../../adapters/memory-events.mjs';
const at='2026-01-01T00:00:00.000Z';
function fixture(t){const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-event-recovery-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));const directory=path.join(root,'outbox');fs.mkdirSync(directory);let calls=0;const journal=new MemoryEventJournal({directory:path.join(root,'journal'),call:async()=>{calls++;return{state:'recorded'};}});return{directory,journal,calls:()=>calls,put:r=>fs.writeFileSync(path.join(directory,r.id+'.json'),JSON.stringify(r))};}
const receipt=id=>({id,state:'accepted',attemptedAt:at,acceptedAt:at,messageId:'platform-'+id,text:'A delivered reply'});
test('startup skips committed delivery identities after mutable metadata changes, without a new ingestion',async t=>{
 const f=fixture(t),r=receipt('sent');f.put(r);const original=deliveryEvent(r);await f.journal.deliver(original);
 f.put({...r,taskId:'task-later',inputVersion:2});
 assert.deepEqual(reconcileOutbox({...f,historicalBefore:at}),{queued:0,recorded:1});
 assert.equal(f.journal.snapshot().length,0);assert.equal(f.calls(),1);
 assert.throws(()=>f.journal.append({...original,text:'A different live event'}),/conflicts with committed/,'live writes still cannot replace the old event');
});
test('one conflicting uncommitted outbox record does not block another delivery gap',async t=>{
 const f=fixture(t),r=receipt('conflict');f.journal.append(deliveryEvent(r));f.put({...r,text:'changed metadata'});f.put(receipt('next'));
 assert.deepEqual(reconcileOutbox({...f,historicalBefore:at}),{queued:1,failed:1});
 assert.deepEqual(f.journal.snapshot().map(e=>e.bubble_id).sort(),['conflict','next']);
 assert.equal(f.journal.snapshot().find(e=>e.bubble_id==='conflict').text,r.text);assert.equal(f.calls(),0);
});
test('unknown delivery stays unconfirmed and recovery never calls a transport',async t=>{
 const f=fixture(t);f.put({...receipt('unknown'),state:'unconfirmed',checkedAt:at,messageId:null});
 assert.deepEqual(reconcileOutbox({...f,historicalBefore:at}),{queued:1});
 assert.equal(f.journal.snapshot()[0].state,'unconfirmed');assert.equal(f.calls(),0);
});
