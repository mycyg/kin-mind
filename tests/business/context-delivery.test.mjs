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

// The owned entry itself is checked in the host's mobile-owned-acp test.
test('patchModelRetries writes the gateway retry limits once and refuses a second pass',()=>{
  const original='config = {\n        wire_api: wireApi\n}';
  const patched=patchModelRetries(original);
  assert.match(patched,/request_max_retries: 3/);assert.match(patched,/stream_max_retries: 0/);
  assert.throws(()=>patchModelRetries(patched),/already carries/);
});

test('one uncertain identity does not hold unrelated fresh background forever',async()=>{
 const calls=[],context={id:'new',session:'main',epoch:'now',marker:'new',text:'new background'};
 let appended=false;
 const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,nativeStatus:'idle'}),
  call:async(action,args)=>{calls.push([action,args.id]);if(action==='context-delivery-pending')return [{id:'old',session:'main',epoch:'before',marker:'old'}];if(action==='context-delivery-begin')return {...context,state:'sending',acquired:true};if(action==='context-delivery-ack')return {...context,state:'accepted'};assert.fail(action);},
  find:async(file,marker)=>({found:marker==='new'&&appended}),inject:async()=>{appended=true;}});
 assert.equal((await delivery.deliver(context)).state,'accepted');assert.equal(calls.filter(x=>x[1]==='old').length,0);
});

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {NativeWindow,checkpointMarker,reconcileLegacyInjections,nativeWindowFor} from '../../adapters/native-window.mjs';

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

test('the receipt index keeps hashes, markers, offsets and times of injections, never text',async t=>{
  const f=rollout(t),context=ctx('a1','PRIVATE BACKGROUND');
  fs.appendFileSync(f.file,line({type:'turn_context',payload:{turn_id:'t1'}})+user('小光的话 <kin-host-event>'+'b'.repeat(32)+'</kin-host-event>')+assistant(context.text,'2026-09-24T01:00:00Z'));
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});
  await window.poll();
  const hit=window.receipts.get(context.text_hash);
  assert.equal(hit.m[0],context.marker);assert.equal(hit.at,'2026-09-24T01:00:00Z');assert.ok(Number.isSafeInteger(hit.o));
  assert.equal(window.state.receipts.items.length,1,'an owner message without a marker is not indexed');
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
    call:async(action,args)=>{calls.push(action);if(action==='context-delivery-pending')return [];if(action==='context-delivery-begin')return {...context,state:'sending',acquired:true};
      if(action==='context-delivery-ack'){assert.equal(args.text_hash,context.text_hash);return {...context,state:'accepted'};}assert.fail(action);},
    inject:async request=>{assert.equal(request.items[0].role,'assistant');fs.appendFileSync(f.file,assistant(request.items[0].content[0].text));}});
  assert.equal((await delivery.deliver(context)).state,'accepted');
  assert.ok(starts.length>0&&starts.every(start=>start>=size),'no read went back into the old history');
  assert.deepEqual(calls,['context-delivery-pending','context-delivery-begin','context-delivery-ack']);
});

/** A memory store's pending list: oldest first by default, or one page in id order after a cursor. */
const pendingPages=list=>args=>args.after===undefined?list.slice(0,16):
  list.filter(c=>c.id>args.after).sort((a,b)=>a.id<b.id?-1:1).slice(0,args.limit??16);

test('old unsettled injection identities are reconciled in a bounded pass; what is missing stays unknown and is never injected',async t=>{
  const found=ctx('old1','旧背景'),missing=ctx('old2','没写进去的背景'),fresh=ctx('new1','新背景');
  const f=rollout(t,assistant('filler')+assistant(found.text)+assistant('filler'));
  // A window written before receipts were indexed: its cursor is already past everything.
  fs.writeFileSync(f.stateFile,JSON.stringify({offset:fs.statSync(f.file).size,threadId:'main',events:[],eventIds:[],compactions:0}));
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});
  assert.equal(window.migrated(),false);
  const acked=[],injected=[],pending=pendingPages([found,missing]);let migration;
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:f.file,active:false,nativeStatus:'idle'}),
    background:work=>{migration=work;},
    call:async(action,args)=>{if(action==='context-delivery-pending')return pending(args);if(action==='context-delivery-begin')return {...fresh,state:'sending',acquired:true};
      if(action==='context-delivery-ack'){acked.push(args.id);return {...args,state:'accepted'};}if(action==='context-delivery-uncertain')return {};assert.fail(action);},
    inject:async request=>{injected.push(request.operationId);fs.appendFileSync(f.file,assistant(request.items[0].content[0].text));}});
  assert.equal((await delivery.deliver(fresh)).state,'accepted');
  const result=await migration;
  assert.deepEqual(result.unknown,[missing.marker]);
  assert.deepEqual(acked.sort(),[found.id,fresh.id].sort());
  assert.deepEqual(injected,[fresh.id],'an unknown identity is never injected again');
  assert.equal(window.migrated(),true);
  assert.deepEqual(Object.keys(window.state.receipts.legacy.markers).sort(),[found.marker,missing.marker].sort(),'each identity keeps what was searched for it');
  assert.equal(window.covers(missing.marker),true,'searched from the first byte: its absence is known');
  const starts=reads(t);
  assert.deepEqual((await reconcileLegacyInjections(window,[missing.marker],{maxBytes:1<<20})).unknown,[],'an identity is searched for once');
  assert.equal((await delivery.deliver(missing)).state,'unconfirmed');
  assert.ok(starts.every(start=>start>0),'after the pass nothing reads the history from its start again');
});

test('CR-LIFE-09: a record already sending before this call is settled by proof alone, even past the first pending page',async()=>{
  const contexts=Array.from({length:17},(_,i)=>ctx('stuck'+String(i).padStart(2,'0'),'背景'+i)),last=contexts[16];
  const calls=[],injected=[];
  const make=acquired=>new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,nativeStatus:'idle'}),
    find:async()=>({found:false}),inject:async request=>{injected.push(request.operationId);},
    call:async(action,args)=>{calls.push([action,args.id]);if(action==='context-delivery-pending')return contexts.slice(0,16);
      if(action==='context-delivery-begin')return {...last,state:'sending',...(acquired?{acquired:true}:{})};if(action==='context-delivery-uncertain')return {...last,state:'unconfirmed'};assert.fail(action);}});
  assert.equal((await make(false).deliver(last)).state,'unconfirmed');
  assert.deepEqual(injected,[],'no proof, and the send right was not taken now: never appended again');
  assert.ok(calls.some(([action,id])=>action==='context-delivery-uncertain'&&id===last.id));
  await make(true).deliver(last);
  assert.deepEqual(injected,[last.id],'the send right taken by this call appends');
});

test('CR-LIFE-09: the legacy pass reads every page of unsettled identities, and only the last page makes the window migrated',async t=>{
  const contexts=Array.from({length:40},(_,i)=>ctx('legacy'+String(i).padStart(2,'0'),'旧'+i));
  const f=rollout(t,contexts.filter((_,i)=>i%3===0).map(c=>assistant(c.text)).join(''));
  fs.writeFileSync(f.stateFile,JSON.stringify({offset:fs.statSync(f.file).size,threadId:'main',events:[],eventIds:[],compactions:0}));
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});
  const pages=[],acked=[],pending=pendingPages(contexts);
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:f.file,active:false,nativeStatus:'idle'}),inject:async()=>assert.fail('never injected'),
    call:async(action,args)=>{if(action==='context-delivery-pending'){pages.push(args.after);return pending(args);}
      if(action==='context-delivery-ack'){acked.push(args.id);return {...args,state:'accepted'};}assert.fail(action);}});
  await delivery.migrate({threadId:'main',rolloutPath:f.file});
  assert.deepEqual(pages,['',contexts[15].id,contexts[31].id],'every page, in id order');
  assert.equal(Object.keys(window.state.receipts.legacy.markers).length,40);
  assert.deepEqual(acked.sort(),contexts.filter((_,i)=>i%3===0).map(c=>c.id).sort());
  assert.equal(window.migrated(),true);
});

test('CR-LIFE-09: the legacy pass needs an explicit budget, and a marker it could not reach stays unknown',async t=>{
  const early=ctx('early','很早的背景');
  const f=rollout(t,assistant(early.text)+assistant('y'.repeat(4096)));
  fs.writeFileSync(f.stateFile,JSON.stringify({offset:fs.statSync(f.file).size,threadId:'main',events:[],eventIds:[],compactions:0}));
  const window=new NativeWindow({file:f.file,threadId:'main',stateFile:f.stateFile});
  await assert.rejects(reconcileLegacyInjections(window,[early.marker]),/explicit, finite byte budget/);
  await assert.rejects(reconcileLegacyInjections(window,[early.marker],{maxBytes:Infinity}),/explicit, finite byte budget/);
  const result=await reconcileLegacyInjections(window,[early.marker],{maxBytes:1024});
  assert.deepEqual(result.unknown,[early.marker]);
  assert.equal(window.covers(early.marker),false,'a bounded stretch proves nothing before it');
  assert.equal(window.migrated(),false,'no caller said every identity was handed over');
});
