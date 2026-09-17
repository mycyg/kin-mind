import test from 'node:test';
import assert from 'node:assert/strict';
import {createContactBatch} from './contact-batch.mjs';
import {splitChatText} from './chat-bubbles.mjs';
import {parseContactDraft} from './contact-draft.mjs';

test('review the complete reply before sending its introduction; restart resumes in order',async()=>{
 const journal=new Map(),sent=[];let ready=false,now=0;
 const opts={read:id=>structuredClone(journal.get(id)),write:(id,v)=>journal.set(id,structuredClone(v)),now:()=>now,
   preflight:async r=>r.entries.some(e=>e.text==='The full body')&&!ready?{state:'pending',reason:'deepseek-incomplete-or-unverified'}:{state:'ready'},
   send:async r=>{sent.push(r.text);return {state:'accepted',messageId:r.id};}};
 const draft={id:'whole',bubbles:['Here it is:','The full body','Closing sentence']};
 assert.equal((await createContactBatch(opts)(draft)).state,'pending');assert.deepEqual(sent,[]);
 ready=true;now=60001;
 assert.equal((await createContactBatch(opts)({id:'whole'})).state,'accepted');assert.deepEqual(sent,draft.bubbles);
 await createContactBatch(opts)({id:'whole'});assert.equal(sent.length,3);
});

test('duplicate body does not leave a new orphan introduction',async()=>{
 let state,calls=0;
 const send=createContactBatch({read:()=>state,write:(_,v)=>{state=v;},preflight:async r=>({state:r.entries.some(e=>e.text==='body')?'duplicate':'ready'}),send:async()=>{calls++;}});
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

const store=journal=>({read:id=>structuredClone(journal.get(id)),write:(id,value)=>journal.set(id,structuredClone(value))});

test('a whole group is reviewed once and released or refused as a whole',async()=>{
  const bubbles=['An opening line.','A second line.','A third line.','A fourth line.','A closing line.'];
  // The middle bubble is the one a per-bubble review would have refused after
  // its neighbours were already on the phone.
  const verdict=request=>request.entries.some(entry=>entry.text===bubbles[2])?{state:'duplicate',reason:'already-said'}:{state:'ready'};
  const refusedJournal=new Map(),refusedSeen=[],refusedSent=[];
  const refused=await createContactBatch({...store(refusedJournal),
    preflight:async request=>{refusedSeen.push(request.entries.map(entry=>entry.text));return verdict(request);},
    send:async request=>{refusedSent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}})({id:'refused-group',bubbles});
  assert.deepEqual(refusedSeen,[bubbles]);                       // one review, the whole group inside it
  assert.deepEqual(refusedSent,[]);                              // never two sent and three cancelled
  assert.equal(refused.state,'canceled');assert.equal(refused.reason,'already-said');
  assert.equal(refused.acceptedBubbles,0);assert.equal(refused.canceledBubbles,5);
  assert.ok(refusedJournal.get('refused-group').items.every(item=>item.state==='canceled'&&item.reason==='already-said'));

  const releasedJournal=new Map(),releasedSeen=[],releasedSent=[];
  const released=await createContactBatch({...store(releasedJournal),
    preflight:async request=>{releasedSeen.push(request.entries.length);return {state:'ready'};},
    send:async request=>{releasedSent.push(request.text);return {state:'accepted',messageId:'receipt-'+request.id};}})({id:'released-group',bubbles:bubbles.slice(0,4).concat(bubbles[4])});
  assert.deepEqual(releasedSeen,[5]);
  assert.deepEqual(releasedSent,bubbles);
  assert.equal(released.state,'accepted');assert.equal(released.acceptedBubbles,5);
});

test('a long draft reaches the group whole and an over-limit bubble is cut on sentence boundaries',async()=>{
  const long=Array.from({length:80},(_,index)=>`This is sentence number ${index+1} of one long synthetic note. `).join('').trim();
  const bubbles=[long,'A short closing paragraph.'];
  const payload=JSON.stringify({action:'send',bubbles});
  // The same decision, delivered in pieces: one of the cuts falls inside a string.
  const parsed=parseContactDraft([payload.slice(0,11),payload.slice(11,900),payload.slice(900)]);
  assert.deepEqual(parsed.bubbles,bubbles);
  assert.ok(parsed.text.length>3000);
  const journal=new Map(),sent=[];
  const finished=await createContactBatch({...store(journal),contracts:{synthetic:{text:{limit:200,measure:'utf16'}}},
    send:async request=>{sent.push(request);return {state:'accepted',messageId:'receipt-'+request.id};}})({id:'long-note',channel:'synthetic',bubbles:parsed.bubbles});
  assert.equal(finished.state,'accepted');
  assert.equal(sent.map(request=>request.text).join(''),bubbles.join(''));   // every character, in order
  assert.ok(sent.length>bubbles.length);
  assert.ok(sent.every(request=>request.text.length<=200));
  for(const request of sent.slice(0,-1))assert.match(request.text,/[.!?]\s*$/);
  assert.ok(sent.every(request=>request.expectedBubbles===sent.length));
  assert.equal(new Set(sent.map(request=>request.id)).size,sent.length);
});

test('the journal a fragmented group leaves behind replays under the previous reader',async()=>{
  // What the previous batch code understood: flat items in order, one state
  // name set, a text or a file each. Anything else strands the group on rollback.
  const replay=batch=>batch.items.map(item=>{
    assert.ok(['unsent','pending','accepted','unconfirmed','canceled'].includes(item.state));
    assert.equal(typeof item.id,'string');
    assert.ok(typeof item.text==='string'||(item.file&&typeof item.file==='object'));
    assert.equal(item.fragments,undefined);
    assert.ok(Array.isArray(item.references??[]));
    return {id:item.id,text:item.text,file:item.file};
  });
  const journal=new Map(),sent=[];let allowance=1;
  const options={...store(journal),contracts:{synthetic:{text:{limit:120,measure:'utf16'}}},eligible:()=>allowance-->0,
    verifyFile:async()=>({state:'ready'}),send:async request=>{sent.push(request);return {state:'accepted',messageId:'receipt-'+request.id};}};
  const bubbles=['A first paragraph that is comfortably short.',
    Array.from({length:12},(_,index)=>`Sentence ${index+1} of a body no single message can hold. `).join('').trim()];
  const request={id:'replayable',channel:'synthetic',bubbles,files:[{sha256:'synthetic-file-hash',path:'fixture'}]};
  assert.equal((await createContactBatch(options)(request)).state,'pending');
  assert.deepEqual(sent,[]);
  const frozen=replay(journal.get('replayable'));
  assert.ok(frozen.length>bubbles.length+1);
  assert.equal(frozen.filter(item=>item.text!==undefined).map(item=>item.text).join(''),bubbles.join(''));
  allowance=99;
  assert.equal((await createContactBatch(options)({id:'replayable'})).state,'accepted');
  assert.deepEqual(sent.map(item=>({id:item.id,text:item.text,file:item.file})),frozen);
});

test('a body no cut can divide stays whole and travels as a complete file',async()=>{
  const journal=new Map(),sent=[];
  const block='```text\n'+Array.from({length:40},(_,index)=>`unbreakable-line-${index}`).join('\n')+'\n```';
  const finished=await createContactBatch({...store(journal),contracts:{synthetic:{text:{limit:200,measure:'utf16'}}},
    send:async request=>{sent.push(request);return {state:'accepted',messageId:'receipt-'+request.id};}})({id:'unsplittable',channel:'synthetic',bubbles:[block]});
  assert.equal(finished.state,'accepted');
  assert.equal(sent.length,1);
  assert.equal(sent[0].text,block);                                  // never cut, never shortened
  assert.equal(sent[0].media.type,'file');
  assert.match(sent[0].media.name,/^kin-reply-[0-9a-f]{16}\.md$/);
  assert.equal(sent[0].media.data.toString('utf8'),block);
  assert.equal(journal.get('unsplittable').items.length,1);
});

test('a crash after the review and between two bubbles resumes without a second review',async()=>{
  const journal=new Map(),sent=[];let reviews=0,crashAt=null;
  const options={read:id=>structuredClone(journal.get(id)),
    write:(id,value)=>{journal.set(id,structuredClone(value));if(crashAt?.(value))throw Error('synthetic-crash');},
    preflight:async request=>{reviews++;return {state:'ready',checked:request.entries.map(()=>({}))};},
    send:async request=>{sent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}};
  const request={id:'resumed',bubbles:['A first part.','A second part.','A third part.']};
  crashAt=value=>Boolean(value.review)&&value.items.every(item=>item.state==='unsent');
  await assert.rejects(createContactBatch(options)(request),/synthetic-crash/);
  assert.equal(reviews,1);assert.deepEqual(sent,[]);
  crashAt=value=>value.items[0].state==='accepted'&&value.items[1].state==='unsent';
  await assert.rejects(createContactBatch(options)({id:'resumed'}),/synthetic-crash/);
  assert.equal(reviews,1);assert.equal(sent.length,1);
  crashAt=null;
  const finished=await createContactBatch(options)({id:'resumed'});
  assert.equal(finished.state,'accepted');assert.equal(reviews,1);
  assert.deepEqual(sent,journal.get('resumed').items.map(item=>item.id));   // frozen IDs, each sent once
});

test('a resumable review reason holds the whole group and resumes later',async()=>{
  const journal=new Map(),sent=[];let clock=0,reviews=0;
  const options={...store(journal),now:()=>clock,
    preflight:async request=>{reviews++;return reviews===1
      ?{state:'pending',reason:'reply-review-chunks-incomplete',chunks:{reviewed:1,total:2},retryAfterMs:60000}
      :{state:'ready',checked:request.entries.map(entry=>({text:entry.text}))};},
    send:async request=>{sent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}};
  const held=await createContactBatch(options)({id:'chunked',bubbles:['One part.','Another part.','A last part.']});
  assert.equal(held.state,'pending');assert.equal(held.reason,'reply-review-chunks-incomplete');
  assert.deepEqual(sent,[]);
  assert.ok(journal.get('chunked').items.every(item=>item.state==='unsent'));   // held whole, not dropped
  assert.equal((await createContactBatch(options)({id:'chunked'})).state,'pending');
  assert.equal(reviews,1);
  clock=60001;
  const finished=await createContactBatch(options)({id:'chunked'});
  assert.equal(finished.state,'accepted');assert.equal(reviews,2);assert.equal(sent.length,3);
});

test('a review that never releases the group refuses it as a whole after a bounded number of tries',async()=>{
  const journal=new Map(),sent=[];let clock=0,reviews=0,last;
  const batch=createContactBatch({...store(journal),now:()=>clock,
    preflight:async()=>{reviews++;return {state:'pending',reason:'share-review-pending'};},
    send:async request=>{sent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}});
  for(let attempt=0;attempt<8;attempt++){last=await batch({id:'never-released',bubbles:['One part.','Another part.']});clock+=15*60000;}
  assert.equal(reviews,6);                                         // the default allowance, and not one call more
  assert.equal(last.state,'canceled');assert.equal(last.reason,'contact-review-held-too-long');
  assert.equal(last.canceledBubbles,2);assert.equal(last.acceptedBubbles,0);
  assert.deepEqual(sent,[]);                                       // the review precedes the first bubble: nothing was exposed
  assert.ok(journal.get('never-released').items.every(item=>item.state==='canceled'&&item.reason==='contact-review-held-too-long'));
});

test('an unscheduled hold waits longer each time, up to a quarter of an hour',async()=>{
  const journal=new Map();let clock=0;
  const batch=createContactBatch({...store(journal),now:()=>clock,maxHolds:99,
    preflight:async()=>({state:'pending',reason:'share-review-pending'}),send:async()=>({state:'accepted',messageId:'receipt'})});
  const waits=[];
  for(let attempt=0;attempt<7;attempt++) {
    await batch({id:'backing-off',bubbles:['One part.']});
    waits.push(journal.get('backing-off').reviewNotBefore-clock);
    clock=journal.get('backing-off').reviewNotBefore;
  }
  assert.deepEqual(waits,[60000,120000,240000,480000,900000,900000,900000]);
});

test('a review that names its own retry time is obeyed',async()=>{
  const journal=new Map();let clock=0,own=300000;
  const batch=createContactBatch({...store(journal),now:()=>clock,
    preflight:async()=>own?{state:'pending',reason:'share-review-pending',retryAfterMs:own}:{state:'pending',reason:'share-review-pending',retryAt:clock+7200000},
    send:async()=>({state:'accepted',messageId:'receipt'})});
  await batch({id:'own-schedule',bubbles:['One part.']});
  assert.equal(journal.get('own-schedule').reviewNotBefore,300000);
  clock=300000;own=0;
  await batch({id:'own-schedule'});
  assert.equal(journal.get('own-schedule').reviewNotBefore,7500000);
});

test('a reviewer that could not be reached backs off but spends none of the group tries',async()=>{
  const journal=new Map(),sent=[];let clock=0,reviews=0,reachable=false;
  const batch=createContactBatch({...store(journal),now:()=>clock,
    preflight:async request=>{reviews++;return reachable?{state:'ready',checked:request.entries.map(()=>({}))}:{state:'pending',reason:'share-review-unavailable',transient:true};},
    send:async request=>{sent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}});
  const waits=[];
  for(let attempt=0;attempt<8;attempt++) {
    await batch({id:'unreachable',bubbles:['One part.','Another part.']});
    waits.push(journal.get('unreachable').reviewNotBefore-clock);
    clock=journal.get('unreachable').reviewNotBefore;
  }
  assert.deepEqual(waits,[60000,120000,240000,480000,900000,900000,900000,900000]);
  assert.equal(journal.get('unreachable').holds,undefined);        // nothing was judged, so nothing was spent
  assert.deepEqual(sent,[]);
  reachable=true;
  assert.equal((await batch({id:'unreachable'})).state,'accepted');
  assert.equal(sent.length,2);assert.equal(reviews,9);
});

test('an eligibility or guard refusal costs nothing and spends none of the group tries',async()=>{
  const journal=new Map(),sent=[];let allowed=false,reviews=0;
  const batch=createContactBatch({...store(journal),eligible:()=>allowed,
    preflight:async request=>{reviews++;return {state:'ready',checked:request.entries.map(()=>({}))};},
    send:async request=>{sent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}});
  for(let attempt=0;attempt<10;attempt++)assert.equal((await batch({id:'not-now',bubbles:['One part.']})).state,'pending');
  allowed=true;
  assert.equal((await batch({id:'not-now',guard:()=>false})).state,'pending');
  assert.equal(reviews,0);
  assert.equal(journal.get('not-now').holds,undefined);
  assert.equal(journal.get('not-now').reviewNotBefore,undefined);
  assert.equal((await batch({id:'not-now'})).state,'accepted');
  assert.equal(sent.length,1);assert.equal(reviews,1);
});

test('file preflight happens before the introduction and unknown delivery only reconciles',async()=>{
 let journal;const sent=[];let verified=false,ack=false;
 const run=createContactBatch({read:()=>journal,write:(_,v)=>{journal=v;},verifyFile:async()=>({state:verified?'ready':'pending',retryAfterMs:0}),
 receipt:async()=>ack?{state:'accepted',messageId:'file-receipt'}:null,
 send:async r=>{sent.push(r);if(r.file)throw Error('uncertain');return{state:'accepted',messageId:'intro'};}});
 const request={id:'file-plan',channel:'synthetic',bubbles:['Here is the file.'],files:[{sha256:'verified-hash',path:'fixture'}]};
 assert.equal((await run(request)).state,'pending');assert.equal(sent.length,0);
 verified=true;assert.equal((await run({id:request.id})).state,'unconfirmed');assert.equal(sent.length,2);
 assert.equal((await run({id:request.id})).state,'unconfirmed');assert.equal(sent.length,2);
 ack=true;assert.equal((await run({id:request.id})).state,'accepted');assert.equal(sent.length,2);
 assert.equal(sent[1].expectedBubbles,2);
});
