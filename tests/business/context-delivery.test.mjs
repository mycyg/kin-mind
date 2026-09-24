import test from 'node:test';
import assert from 'node:assert/strict';
import {NativeContextDelivery} from '../../adapters/context-delivery.mjs';
import {patchModelRetries} from '../../adapters/codex-runtime-patch.mjs';

test('a completed failed native turn defers background without pretending an append was sent',async()=>{
  const calls=[];
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,backgroundTasks:0,nativeStatus:'systemError'}),
    call:async(...args)=>calls.push(args),inject:async()=>calls.push('inject')});
  assert.equal((await delivery.deliver({id:'context',session:'main'})).state,'deferred');
  assert.deepEqual(calls,[]);
});

test('an uncertain previous append is kept without replay',async()=>{
  let injected=0;
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,nativeStatus:'idle'}),
    call:async()=>[{id:'old',session:'main',marker:'old'}],find:async()=>({found:false}),inject:async()=>injected++});
  assert.equal((await delivery.deliver({id:'old',session:'main'})).state,'unconfirmed');
  assert.equal(injected,0);
});

test('the native gateway owns at most three HTTP retries with no stream replay loop',()=>{
  const original='config = {\n        wire_api: wireApi\n}';
  const patched=patchModelRetries(original);
  assert.match(patched,/request_max_retries: 3/);assert.match(patched,/stream_max_retries: 0/);
  assert.equal(patchModelRetries(patched),patched);
});

test('one uncertain identity does not hold unrelated fresh background forever',async()=>{
 const calls=[],context={id:'new',session:'main',epoch:'now',marker:'new',text:'new background'};
 let appended=false;
 const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,nativeStatus:'idle'}),
  call:async(action,args)=>{calls.push([action,args.id]);if(action==='context-delivery-pending')return [{id:'old',session:'main',epoch:'before',marker:'old'}];if(action==='context-delivery-begin')return {...context,state:'sending'};if(action==='context-delivery-ack')return {...context,state:'accepted'};assert.fail(action);},
  find:async(file,marker)=>({found:marker==='new'&&appended}),inject:async()=>{appended=true;}});
 assert.equal((await delivery.deliver(context)).state,'accepted');assert.equal(calls.filter(x=>x[1]==='old').length,0);
});

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {NativeWindow,checkpointMarker,reconcileLegacyInjections,nativeWindowFor,reconcileHostInput} from '../../adapters/native-window.mjs';

const sha=value=>createHash('sha256').update(value).digest('hex');
const line=value=>JSON.stringify(value)+'\n';
const assistant=(text,at='2026-09-24T00:00:00Z')=>line({timestamp:at,type:'response_item',payload:{type:'message',role:'assistant',content:[{type:'output_text',text}]}});
const user=(text,at='2026-09-24T00:00:00Z')=>line({timestamp:at,type:'response_item',payload:{type:'message',role:'user',content:[{type:'input_text',text}]}});
function rollout(t,prefix='') {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-native-window-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const file=path.join(dir,'rollout.jsonl');fs.writeFileSync(file,line({type:'session_meta',payload:{id:'main'}})+prefix);
  return {dir,file,stateFile:path.join(dir,'window.json')};
}
/** Every read of the native history, by where it starts. */
function reads(t) {
  const original=fs.createReadStream,starts=[];
  fs.createReadStream=(file,options={})=>{starts.push(options?.start??0);return original.call(fs,file,options);};
  t.after(()=>{fs.createReadStream=original;});
  return starts;
}
const ctx=(id,text)=>{const marker='kin-context:context:'+id,body=marker+'\n'+text;return {id:'context:'+id,session:'main',epoch:'e1',marker,text:body,text_hash:sha(body)};};

test('the receipt index keeps hashes, markers, offsets and times of injections and inputs, never text',async t=>{
  const f=rollout(t),context=ctx('a1','PRIVATE BACKGROUND');
  fs.appendFileSync(f.file,line({type:'turn_context',payload:{turn_id:'t1'}})+user('小光的话 <kin-host-event>'+'b'.repeat(32)+'</kin-host-event>')+assistant(context.text,'2026-09-24T01:00:00Z'));
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});
  await window.poll();
  const hit=window.receipts.get(context.text_hash);
  assert.equal(hit.m[0],context.marker);assert.equal(hit.at,'2026-09-24T01:00:00Z');assert.ok(Number.isSafeInteger(hit.o));
  const input=window.receipts.input('b'.repeat(32));
  assert.ok(input.o>input.tc,'the turn context that owns the input comes first');
  const saved=fs.readFileSync(f.stateFile,'utf8');
  assert.ok(!saved.includes('PRIVATE BACKGROUND')&&!saved.includes('小光的话'),'no message text is kept');
  assert.equal(nativeWindowFor(f.file),window);
  assert.deepEqual(await checkpointMarker(f.file,context.marker,{textHash:context.text_hash,role:'assistant'}),{found:true,at:hit.at,offset:hit.o});
});

test('T-14: a delivery reads only what was appended since the last read, however long the history is',async t=>{
  const f=rollout(t,assistant('x'.repeat(4096)).repeat(2000));
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});await window.poll();
  const size=fs.statSync(f.file).size,starts=reads(t),context=ctx('fresh','背景');
  const calls=[];
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:f.file,active:false,nativeStatus:'idle'}),
    call:async(action,args)=>{calls.push(action);if(action==='context-delivery-pending')return [];if(action==='context-delivery-begin')return {...context,state:'sending'};
      if(action==='context-delivery-ack'){assert.equal(args.text_hash,context.text_hash);return {...context,state:'accepted'};}assert.fail(action);},
    inject:async request=>{assert.equal(request.items[0].role,'assistant');fs.appendFileSync(f.file,assistant(request.items[0].content[0].text));}});
  assert.equal((await delivery.deliver(context)).state,'accepted');
  assert.ok(starts.length>0&&starts.every(start=>start>=size),'no read went back into the old history');
  assert.deepEqual(calls,['context-delivery-pending','context-delivery-begin','context-delivery-ack']);
});

test('old unsettled injection identities are reconciled once, in one bounded pass; what is missing stays unknown and is never injected',async t=>{
  const found=ctx('old1','旧背景'),missing=ctx('old2','没写进去的背景'),fresh=ctx('new1','新背景');
  const f=rollout(t,assistant('filler')+assistant(found.text)+assistant('filler'));
  // A window written before receipts were indexed: its cursor is already past everything.
  fs.writeFileSync(f.stateFile,JSON.stringify({offset:fs.statSync(f.file).size,threadId:'main',events:[],eventIds:[],compactions:0}));
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});
  assert.equal(window.covers(),false);
  const acked=[],injected=[];let migration;
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:f.file,active:false,nativeStatus:'idle'}),
    background:work=>{migration=work;},
    call:async(action,args)=>{if(action==='context-delivery-pending')return [found,missing];if(action==='context-delivery-begin')return {...fresh,state:'sending'};
      if(action==='context-delivery-ack'){acked.push(args.id);return {...args,state:'accepted'};}if(action==='context-delivery-uncertain')return {};assert.fail(action);},
    inject:async request=>{injected.push(request.operationId);fs.appendFileSync(f.file,assistant(request.items[0].content[0].text));}});
  assert.equal((await delivery.deliver(fresh)).state,'accepted');
  const result=await migration;
  assert.deepEqual(result.unknown,[missing.marker]);
  assert.deepEqual(acked.sort(),[found.id,fresh.id].sort());
  assert.deepEqual(injected,[fresh.id],'an unknown identity is never injected again');
  assert.equal(window.covers(),true);
  assert.equal((await reconcileLegacyInjections(window,[missing.marker])).state,'covered','the pass runs once');
  const starts=reads(t);
  assert.equal((await delivery.deliver(missing)).state,'unconfirmed');
  assert.ok(starts.every(start=>start>0),'after the pass nothing reads the history from its start again');
});

test('AD1-10: an uncertain submission is found by its host event, or proven absent only from history that begins before it',async t=>{
  const sent='c'.repeat(32),lost='d'.repeat(32),submittedAt=Date.parse('2026-09-24T02:00:00Z');
  const f=rollout(t,assistant('filler','2026-09-24T01:00:00Z')+user('小光的话 <kin-host-event>'+sent+'</kin-host-event>','2026-09-24T02:00:01Z'));
  // Without an incremental reader the end of the history is read once, bounded.
  assert.equal((await reconcileHostInput(f.file,[sent],{submittedAt})).state,'found');
  assert.deepEqual(await reconcileHostInput(f.file,[lost],{submittedAt}),{state:'not-found'},'the whole history was read');
  const before=fs.statSync(f.file).size;
  fs.appendFileSync(f.file,assistant('y'.repeat(2048),'2026-09-24T03:00:00Z').repeat(8));
  const bounded=await reconcileHostInput(f.file,[lost],{submittedAt,maxBytes:fs.statSync(f.file).size-before});
  assert.equal(bounded.state,'unknown','a stretch that begins after the submission proves nothing');
  assert.equal((await reconcileHostInput(f.file,[],{submittedAt})).state,'unknown','no event, no answer');
  // With a reader, its receipt index answers without a pass over the file.
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});await window.poll();
  const starts=reads(t);
  assert.equal((await reconcileHostInput(f.file,[sent],{submittedAt})).state,'found');
  assert.ok(starts.every(start=>start>=fs.statSync(f.file).size),'the index answered');
  assert.ok(!fs.readFileSync(f.stateFile,'utf8').includes('小光的话'));
});
