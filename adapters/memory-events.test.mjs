import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MemoryEventJournal,deliveryEvent,ToolArtifactObserver} from './memory-events.mjs';
test('journal survives failures without retrying platform sends',async()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-memory-'));
  try {
    const event=deliveryEvent({id:'b1',text:'A thought',state:'accepted',messageId:'p1',acceptedAt:'2026-09-14T01:00:00Z'});
    const journal=new MemoryEventJournal({directory,clock:()=>0,call:async()=>{throw Error('offline');}});
    journal.append(event);journal.append(event);
    assert.throws(()=>journal.append({...event,text:'changed'}),/conflict/);
    assert.equal((await journal.drain()).failed,1);assert.equal(fs.readdirSync(directory).filter(n=>n.endsWith('.json')).length,1);
    let received;
    const restarted=new MemoryEventJournal({directory,clock:()=>300001,call:async(action,input)=>{received=input;return{state:'recorded'};}});
    assert.equal((await restarted.drain()).recorded,1);assert.equal(received.message_id,'p1');
    const replay=new MemoryEventJournal({directory,call:async()=>{throw Error('Committed event must not be reingested');}});
    assert.equal(replay.append(event).state,'recorded');assert.equal((await replay.drain()).recorded,0);
    assert.throws(()=>replay.append({...event,text:'changed'}),/conflict/);
  } finally {fs.rmSync(directory,{recursive:true,force:true});}
});
test('a failed archive event cannot block later input and reply receipts',async()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-isolated-journal-')),calls=[];
  try {
    const journal=new MemoryEventJournal({directory,clock:()=>1000,call:async(action,event)=>{
      calls.push([action,event.id]);if(event.id==='bad')throw Error('unreadable artifact');
      return action==='ingest'?{source_id:'original-source',appraisal:{id:'original-job'},memory_enabled:true}:{state:'recorded'};
    }});
    journal.append({id:'bad',kind:'delivery',at:'2026-09-14T00:00:00Z'});
    const input={id:'owner',kind:'owner-message',at:'2026-09-14T01:00:00Z',text:'New question',session:'original-session',defer_context:true};
    await journal.deliver(input);
    journal.append({id:'good',kind:'delivery',at:'2026-09-14T02:00:00Z'});
    const result=await journal.drain();assert.equal(result.recorded,1);assert.equal(result.failed,1);
    assert.equal(calls[0][0],'ingest');assert.equal(journal.snapshot().length,1);
    const before=calls.length;await journal.drain();assert.equal(calls.length,before);
  } finally {fs.rmSync(directory,{recursive:true,force:true});}
});
test('new host events take priority over a historical outbox backlog',async()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-priority-')),seen=[];
  try {
    const journal=new MemoryEventJournal({directory,call:async(_,e)=>{seen.push(e.id);return{state:'recorded'};}});
    journal.append({id:'old',kind:'delivery',at:'2026-01-01T00:00:00Z',historical:true});
    journal.append({id:'new',kind:'delivery',at:'2026-09-14T00:00:00Z'});
    await journal.drain(1);assert.deepEqual(seen,['new']);
    await journal.drain(1);assert.deepEqual(seen,['new','old']);
  } finally {fs.rmSync(directory,{recursive:true,force:true});}
});
test('actual edit provenance differs from merely observing a file',()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-artifact-')),file=path.join(directory,'fixture.txt'),events=[];
  try {
    const observer=new ToolArtifactObserver({journal:{directory,append:e=>events.push(e)},task:()=> 'task-one'});
    observer.update({toolCallId:'edit',kind:'edit',status:'in_progress',locations:[{path:file}]});
    fs.writeFileSync(file,'actual output');
    observer.update({toolCallId:'edit',kind:'edit',status:'completed',locations:[{path:file}]});
    assert.equal(events[0].kind,'artifact-created');assert.equal(events[0].task_id,'task-one');
    fs.writeFileSync(file,'a later revision');
    assert.equal(fs.readFileSync(events[0].artifact.path,'utf8'),'actual output');
    observer.update({toolCallId:'shell',kind:'execute',status:'completed',locations:[{path:file}]});
    assert.equal(events[1].kind,'artifact-observed');
  } finally {fs.rmSync(directory,{recursive:true,force:true});}
});
