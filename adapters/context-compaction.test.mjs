import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContextCompaction} from './context-compaction.mjs';
test('compaction waits for active work then preserves the task lock at an idle boundary',async()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-compact-'));let work=true,calls=0,acks=0;
  try {
    const c=new ContextCompaction({file:path.join(directory,'state.json'),sessionId:'thread',
      router:{locked:f=>f(),busy:()=>work,tasks:()=>[{id:'unfinished',inputVersion:2,tools:{tool:{status:'completed'}}}]},inspect:async()=>({known:true,sessionId:'thread',model:'work-model'}),
      compact:async()=>{calls++;return{completed:true,actual_session:'thread'};},ack:async()=>{acks++;return{state:'applied'};}});
    assert.equal((await c.run('one')).reason,'owner-work');assert.equal(calls,0);
    work=false;assert.equal((await c.run('one')).state,'complete');await c.run('one');assert.equal(calls,1);assert.equal(acks,1);
  } finally {fs.rmSync(directory,{recursive:true,force:true});}
});
test('uncertain compaction keeps the old injection ledger',async()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-compact-'));let acks=0;
  try {
    const c=new ContextCompaction({file:path.join(directory,'state.json'),sessionId:'thread',router:{locked:f=>f(),busy:()=>false,tasks:()=>[]},
      inspect:async()=>({known:true,sessionId:'thread',model:'m'}),compact:async()=>({completed:true,actual_session:'wrong'}),ack:async()=>{acks++;}});
    assert.equal((await c.run('one')).state,'unconfirmed');assert.equal(acks,0);
    assert.equal((await c.run('two')).reason,'compaction-receipt-unconfirmed');
  } finally {fs.rmSync(directory,{recursive:true,force:true});}
});

test('idle native status alone cannot compact an unfinished tool',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-compact-tool-'));let calls=0;
 try {
  const c=new ContextCompaction({file:path.join(directory,'state.json'),sessionId:'thread',router:{locked:f=>f(),busy:()=>false,tasks:()=>[{tools:{background:{status:'in_progress'}}}]},inspect:async()=>({known:true,sessionId:'thread',model:'m'}),compact:async()=>{calls++;}});
  assert.equal((await c.run('one')).reason,'owner-work');assert.equal(calls,0);
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

test('a completed manual compaction does not consume a future full-window request',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-compact-epoch-'));let calls=0;
 try {
  const options={file:path.join(directory,'state.json'),sessionId:'thread',router:{locked:f=>f(),busy:()=>false,tasks:()=>[]},inspect:async()=>({known:true,sessionId:'thread',model:'m'}),compact:async id=>{calls++;assert.ok(id.length<64);return {completed:true,actual_session:'thread'};},ack:async()=>({state:'applied'})};
  const c=new ContextCompaction(options);c.externalReceipt('manual');
  assert.equal((await new ContextCompaction(options).run('manual')).state,'complete');assert.equal(calls,1);
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

test('recovered compaction checks the original model and task before acknowledging',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-compact-recover-'));let model='work-model',tasks=[{id:'work',inputVersion:2}],acks=0;
 try {
  const options={file:path.join(directory,'state.json'),sessionId:'thread',router:{locked:f=>f(),busy:()=>false,tasks:()=>tasks},inspect:async()=>({known:true,sessionId:'thread',model}),compact:async()=>{throw Error('timeout');},reconcile:async()=>({completed:true,actual_session:'thread'}),ack:async()=>{acks++;return {state:'applied'};}};
  assert.equal((await new ContextCompaction(options).run('one')).state,'unconfirmed');
  const restored=new ContextCompaction(options);model='chat-model';
  assert.equal((await restored.run('one')).state,'waiting');assert.equal(acks,0);
  model='work-model';tasks=[{id:'work',inputVersion:3}];
  assert.equal((await restored.run('one')).state,'waiting');assert.equal(acks,0);
  tasks=[{id:'work',inputVersion:2}];
  assert.equal((await restored.run('one')).state,'complete');assert.equal(acks,1);
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});
