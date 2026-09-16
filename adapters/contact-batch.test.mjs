import test from 'node:test';
import assert from 'node:assert/strict';
import {createContactBatch} from './contact-batch.mjs';
import {splitChatText} from './chat-bubbles.mjs';
import {parseContactDraft} from './contact-draft.mjs';

test('review the complete reply before sending its introduction; restart resumes in order',async()=>{
 const journal=new Map(),sent=[];let ready=false,now=0;
 const opts={read:id=>structuredClone(journal.get(id)),write:(id,v)=>journal.set(id,structuredClone(v)),now:()=>now,
   preflight:async r=>r.text==='The full body'&&!ready?{state:'pending',reason:'deepseek-incomplete-or-unverified'}:{state:'ready'},
   send:async r=>{sent.push(r.text);return {state:'accepted',messageId:r.id};}};
 const draft={id:'whole',bubbles:['Here it is:','The full body','Closing sentence']};
 assert.equal((await createContactBatch(opts)(draft)).state,'pending');assert.deepEqual(sent,[]);
 ready=true;now=60001;
 assert.equal((await createContactBatch(opts)({id:'whole'})).state,'accepted');assert.deepEqual(sent,draft.bubbles);
 await createContactBatch(opts)({id:'whole'});assert.equal(sent.length,3);
});

test('duplicate body does not leave a new orphan introduction',async()=>{
 let state,calls=0;
 const send=createContactBatch({read:()=>state,write:(_,v)=>{state=v;},preflight:async r=>({state:r.text==='body'?'duplicate':'ready'}),send:async()=>{calls++;}});
 const result=await send({id:'duplicate',bubbles:['Introduction:','body']});
 assert.equal(result.state,'canceled');assert.equal(calls,0);
});

test('partial receipt reconciles the same ID then sends only unsent bubbles after restart',async()=>{
  const journal=new Map(),receipts=new Map(),sent=[];let fail=true;
  const args={read:id=>structuredClone(journal.get(id)),write:(id,b)=>journal.set(id,structuredClone(b)),
    receipt:id=>receipts.get(id),send:async request=>{sent.push(request.id);if(sent.length===2&&fail)throw Error('timeout');return{state:'accepted',messageId:'server-'+request.id};}};
  const first=await createContactBatch(args)({id:'synthetic-attempt',bubbles:['我冒出一个怪念头。','云会不会打嗝呀？','你先别笑嘛。'],channel:'synthetic'});
  assert.equal(first.state,'unconfirmed');assert.equal(first.acceptedBubbles,1);
  const restored=createContactBatch(args);
  assert.equal((await restored({id:first.id})).state,'unconfirmed');assert.equal(sent.length,2);
  receipts.set(sent[1],{state:'accepted',messageId:'server-second'});fail=false;
  const final=await restored({id:first.id});
  assert.equal(final.state,'accepted');assert.equal(sent.length,3);assert.equal(final.messageIds.length,3);
  await restored({id:first.id});assert.equal(sent.length,3);
  await assert.rejects(restored({id:first.id,bubbles:['Changed content']}),/content-conflict/);
});

test('quiet or busy guard preserves unsent content and stable IDs',async()=>{
  const records=new Map();let allowed=false,calls=0;
  const batch=createContactBatch({read:id=>records.get(id),write:(id,v)=>records.set(id,v),eligible:()=>allowed,
    send:async()=>{calls++;return{state:'accepted',messageId:'synthetic-message'};}});
  assert.equal((await batch({id:'held',text:'我想找你聊两句。'})).state,'pending');assert.equal(calls,0);
  allowed=true;await batch({id:'held',guard:()=>false});assert.equal(calls,0);
  assert.equal((await batch({id:'held'})).state,'accepted');assert.equal(calls,1);
});

test('paragraph bubbles keep complete words, code and links; no fifth-bubble collapse',()=>{
  const text=['我刚想到一个问题。','你想不想听呀？','我有点好奇。','我猜是这样。','你别笑嘛。','我还没说完呢。'].join('\n\n');
  assert.equal(splitChatText(text).length,6);
  const code='```python\nfirst()\n\nsecond()\n```';
  assert.deepEqual(splitChatText(code+'\n\nhttps://example.com/a-long-path'),[code,'https://example.com/a-long-path']);
  const parsed=parseContactDraft([JSON.stringify({action:'send',bubbles:splitChatText(text)})]);
  assert.equal(parsed.bubbles.length,6);assert.equal(parsed.text,text);
});

test('owner reply retires unsent remainder without blocking the next conversation',async()=>{
  const journal=new Map();let epoch=1,calls=0;
  const batch=createContactBatch({read:id=>journal.get(id),write:(id,v)=>journal.set(id,v),send:async()=>{calls++;epoch++;return{state:'accepted',messageId:'first'};}});
  const partial=await batch({id:'superseded',bubbles:['First','Second'],guard:()=>epoch===1});
  assert.equal(partial.state,'pending');assert.equal(calls,1);
  const settled=await batch({id:'superseded',superseded:true});
  assert.equal(settled.state,'accepted');assert.equal(settled.partial,true);assert.equal(settled.canceledBubbles,1);assert.equal(calls,1);
});
test('each bubble carries the durable memory batch and expected count',async()=>{
  let state;const sent=[];
  const run=createContactBatch({read:()=>state,write:(_,v)=>{state=v;},send:async request=>{sent.push(request);return{state:'accepted',messageId:request.id};}});
  await run({id:'synthetic-batch',channel:'synthetic',bubbles:['First thought.','Its continuation.']});
  assert.deepEqual(sent.map(r=>[r.memoryBatchId,r.expectedBubbles]),[['synthetic-batch',2],['synthetic-batch',2]]);
});
