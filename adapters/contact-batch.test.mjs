import test from 'node:test';
import assert from 'node:assert/strict';
import {createContactBatch} from './contact-batch.mjs';
import {splitChatText} from './chat-bubbles.mjs';
import {parseContactDraft} from './contact-draft.mjs';
const approved=request=>({state:'ready',checked:request.entries.map(entry=>({state:'ready',draft_id:entry.draft_id,text:entry.text,references:entry.references??[]}))});

test('review the complete reply before sending its introduction; restart resumes in order',async()=>{
 const journal=new Map(),sent=[];let ready=false,now=0;
 const opts={read:id=>structuredClone(journal.get(id)),write:(id,v)=>journal.set(id,structuredClone(v)),now:()=>now,
   preflight:async r=>r.entries.some(e=>e.text==='The full body')&&!ready?{state:'pending',reason:'deepseek-incomplete-or-unverified'}:approved(r),
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
 assert.equal(result.state,'canceled');assert.equal(result.decision.action,'abandon');assert.equal(result.decision.reason,'duplicate');assert.equal(calls,0);
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

test('owner reply can release a wholly unsent superseded group with explicit proof',async()=>{
  const journal=new Map();let calls=0;
  const batch=createContactBatch({read:id=>structuredClone(journal.get(id)),write:(id,value)=>journal.set(id,structuredClone(value)),
    send:async()=>{calls++;return{state:'accepted',messageId:'must-not-send'};}});
  assert.equal((await batch({id:'superseded-unsent',bubbles:['First','Second'],guard:()=>false})).state,'pending');
  const settled=await batch({id:'superseded-unsent',superseded:true});
  assert.equal(settled.state,'canceled');assert.equal(settled.safeToRelease,true);
  assert.equal(settled.acceptedBubbles,0);assert.equal(calls,0);
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
  const verdict=request=>request.entries.some(entry=>entry.text===bubbles[2])?{state:'duplicate',reason:'already-said'}:approved(request);
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
    preflight:async request=>{releasedSeen.push(request.entries.length);return approved(request);},
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
    preflight:async request=>{reviews++;return approved(request);},
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
      :approved(request);},
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

test('a semantic hold that never releases the group is parked for DS review after a bounded number of tries',async()=>{
  const journal=new Map(),sent=[];let clock=0,reviews=0,last;
  const batch=createContactBatch({...store(journal),now:()=>clock,
    preflight:async()=>{reviews++;return {state:'pending',reason:'share-review-pending'};},
    send:async request=>{sent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}});
  for(let attempt=0;attempt<8;attempt++){last=await batch({id:'never-released',bubbles:['One part.','Another part.']});clock+=15*60000;}
  assert.equal(reviews,6);                                         // the default allowance, and not one call more
  assert.equal(last.state,'needs-review');assert.equal(last.reason,'contact-review-held-too-long');
  assert.equal(last.failure.category,'semantic-hold');assert.equal(last.safeToRelease,true);
  assert.equal(last.canceledBubbles,0);assert.equal(last.acceptedBubbles,0);
  assert.deepEqual(sent,[]);                                       // the review precedes the first bubble: nothing was exposed
  assert.ok(journal.get('never-released').items.every(item=>item.state==='unsent'));
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

test('a temporarily unreachable reviewer backs off and can recover inside its separate failure budget',async()=>{
  const journal=new Map(),sent=[];let clock=0,reviews=0,reachable=false;
  const batch=createContactBatch({...store(journal),now:()=>clock,
    preflight:async request=>{reviews++;return reachable?approved(request):{state:'pending',reason:'share-review-unavailable',transient:true};},
    send:async request=>{sent.push(request.id);return {state:'accepted',messageId:'receipt-'+request.id};}});
  const waits=[];
  for(let attempt=0;attempt<5;attempt++) {
    await batch({id:'unreachable',bubbles:['One part.','Another part.']});
    waits.push(journal.get('unreachable').reviewNotBefore-clock);
    clock=journal.get('unreachable').reviewNotBefore;
  }
  assert.deepEqual(waits,[60000,120000,240000,480000,900000]);
  assert.equal(journal.get('unreachable').holds,undefined);        // nothing was judged, so nothing was spent
  assert.deepEqual(sent,[]);
  reachable=true;
  assert.equal((await batch({id:'unreachable'})).state,'accepted');
  assert.equal(sent.length,2);assert.equal(reviews,6);
});

test('an eligibility or guard refusal costs nothing and spends none of the group tries',async()=>{
  const journal=new Map(),sent=[];let allowed=false,reviews=0;
  const batch=createContactBatch({...store(journal),eligible:()=>allowed,
    preflight:async request=>{reviews++;return approved(request);},
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

test('new multi-bubble review IDs are unique, persisted and reused by review and delivery',async()=>{
  const journal=new Map(),reviews=[],sent=[];
  const options={...store(journal),preflight:async request=>{reviews.push(request.entries.map(entry=>entry.draft_id));return approved(request);},
    send:async request=>{sent.push({id:request.id,draftId:request.draftId});return {state:'accepted',messageId:'message-'+request.id};}};
  const request={id:'identity-v2',channel:'synthetic',bubbles:['One.','Two.','Three.']};
  assert.equal((await createContactBatch(options)(request)).state,'accepted');
  const persisted=journal.get(request.id).items.map(item=>({id:item.id,draftId:item.draftId,bubbleIndex:item.bubbleIndex}));
  assert.equal(new Set(reviews[0]).size,3);assert.deepEqual(reviews[0],persisted.map(item=>item.draftId));
  assert.deepEqual(sent,persisted.map(item=>({id:item.id,draftId:item.draftId})));
  assert.deepEqual(persisted.map(item=>item.bubbleIndex),[0,1,2]);
  await createContactBatch(options)({id:request.id});assert.equal(reviews.length,1);assert.equal(sent.length,3);
});

test('legacy identity migration protects wholly unsent, reviewed, accepted and uncertain batches',async()=>{
  const legacy=(id,states,{review=false}={})=>({id,channel:'synthetic',state:'pending',...(review?{review:{at:1}}:{}),items:states.map((state,index)=>({
    id:id+'-item-'+index,text:'Part '+index,references:[],state,...(state==='accepted'?{messageId:id+'-message-'+index}:{})}))});

  // Wholly unsent and unreviewed: the existing per-item IDs become review IDs.
  const freshJournal=new Map([['legacy-unsent',legacy('legacy-unsent',['unsent','unsent'])]]),freshEntries=[],freshSent=[];
  const fresh=await createContactBatch({...store(freshJournal),preflight:async request=>{freshEntries.push(...request.entries);return approved(request);},
    send:async request=>{freshSent.push(request);return {state:'accepted',messageId:'sent-'+request.id};}})({id:'legacy-unsent'});
  assert.equal(fresh.state,'accepted');assert.deepEqual(freshEntries.map(entry=>entry.draft_id),['legacy-unsent-item-0','legacy-unsent-item-1']);
  assert.deepEqual(freshSent.map(item=>item.draftId),freshEntries.map(entry=>entry.draft_id));

  // A completed old review keeps the old group draft identity for its unsent tail.
  const reviewedJournal=new Map([['legacy-reviewed',legacy('legacy-reviewed',['unsent','unsent'],{review:true})]]),reviewedSent=[];
  await createContactBatch({...store(reviewedJournal),preflight:async()=>assert.fail('must not review twice'),
    send:async request=>{reviewedSent.push(request);return {state:'accepted',messageId:'sent-'+request.id};}})({id:'legacy-reviewed'});
  assert.deepEqual(reviewedSent.map(item=>item.draftId),['legacy-reviewed','legacy-reviewed']);

  // A delivered head remains delivered; only the frozen tail is sent.
  const acceptedJournal=new Map([['legacy-accepted',legacy('legacy-accepted',['accepted','unsent'],{review:true})]]),acceptedSent=[];
  const accepted=await createContactBatch({...store(acceptedJournal),send:async request=>{acceptedSent.push(request);return {state:'accepted',messageId:'tail'};}})({id:'legacy-accepted'});
  assert.equal(accepted.state,'accepted');assert.deepEqual(acceptedSent.map(item=>[item.id,item.draftId]),[['legacy-accepted-item-1','legacy-accepted']]);

  // An uncertain head blocks the tail and neither transport ID is changed or replayed.
  const uncertainJournal=new Map([['legacy-uncertain',legacy('legacy-uncertain',['unconfirmed','unsent'],{review:true})]]),uncertainSent=[];
  const uncertain=await createContactBatch({...store(uncertainJournal),receipt:()=>null,send:async request=>{uncertainSent.push(request);}})({id:'legacy-uncertain'});
  assert.equal(uncertain.state,'unconfirmed');assert.deepEqual(uncertainSent,[]);
  assert.deepEqual(uncertainJournal.get('legacy-uncertain').items.map(item=>[item.id,item.draftId]),[
    ['legacy-uncertain-item-0','legacy-uncertain'],['legacy-uncertain-item-1','legacy-uncertain']]);
});

test('identity migration must be durable before review or send and resumes with the same IDs after a write interruption',async()=>{
  const original={id:'migration-crash',channel:'synthetic',state:'pending',items:[
    {id:'old-one',text:'One',references:[],state:'unsent'},{id:'old-two',text:'Two',references:[],state:'unsent'}]};
  let saved=structuredClone(original),interrupt=true,reviews=0,sends=0;
  const args={read:()=>structuredClone(saved),write:(_,value)=>{if(interrupt&&value.identity?.version===2){interrupt=false;throw Error('migration-write-interrupted');}saved=structuredClone(value);},
    preflight:async request=>{reviews++;return approved(request);},send:async request=>{sends++;return {state:'accepted',messageId:request.id};}};
  await assert.rejects(createContactBatch(args)({id:original.id}),/migration-write-interrupted/);
  assert.equal(reviews,0);assert.equal(sends,0);assert.deepEqual(saved,original);
  const completed=await createContactBatch(args)({id:original.id});assert.equal(completed.state,'accepted');
  assert.deepEqual(saved.items.map(item=>item.id),['old-one','old-two']);assert.deepEqual(saved.items.map(item=>item.draftId),['old-one','old-two']);
});

test('malformed reviewer identity or deterministic journal identity is quarantined before model cost or delivery',async()=>{
  const mismatch=new Map(),sent=[];
  const bad=await createContactBatch({...store(mismatch),preflight:async request=>({state:'ready',checked:[...request.entries].reverse().map(entry=>({draft_id:entry.draft_id,text:entry.text,references:[]}))}),
    send:async request=>{sent.push(request);}})({id:'mismatched-review',bubbles:['One','Two']});
  assert.equal(bad.state,'needs-review');assert.equal(bad.failure.code,'contact-review-response-mismatch');assert.equal(bad.safeToRelease,true);
  assert.deepEqual(sent,[]);assert.equal(mismatch.get('mismatched-review').state,'needs-review');

  const duplicate=new Map([['duplicate-identity',{id:'duplicate-identity',channel:'synthetic',state:'pending',items:[
    {id:'same',text:'One',references:[],state:'unsent'},{id:'same',text:'Two',references:[],state:'unsent'}]}]]);let modelCalls=0;
  const invalid=await createContactBatch({...store(duplicate),preflight:async()=>{modelCalls++;},send:async()=>assert.fail('must not send')})({id:'duplicate-identity'});
  assert.equal(invalid.state,'needs-review');assert.equal(invalid.failure.category,'contract');assert.equal(modelCalls,0);

  for(const [id,preflight] of [
    ['null-verdict',async()=>null],
    ['object-text',async request=>({state:'ready',checked:request.entries.map(entry=>({draft_id:entry.draft_id,text:{body:entry.text},references:[]}))})],
  ]) {
    const journal=new Map();let sends=0;
    const result=await createContactBatch({...store(journal),preflight,send:async()=>{sends++;}})({id,bubbles:['One']});
    assert.equal(result.state,'needs-review');assert.equal(result.safeToRelease,true);
    assert.equal(result.failure.category,'contract');assert.equal(result.failure.code,id==='null-verdict'?'contact-review-verdict-invalid':'contact-review-response-mismatch');
    assert.equal(sends,0);assert.equal(journal.get(id).state,'needs-review');
  }
});

test('transport identities are globally unique across text and files before any external call',async()=>{
  const journal=new Map([['cross-kind-collision',{id:'cross-kind-collision',channel:'synthetic',state:'pending',items:[
    {id:'same-transport',text:'One',references:[],state:'unsent'},
    {id:'same-transport',file:{sha256:'file-hash',path:'fixture'},state:'unsent'},
  ]}]]);let receipts=0,reviews=0,sends=0;
  const result=await createContactBatch({...store(journal),receipt:async()=>{receipts++;},preflight:async()=>{reviews++;},
    verifyFile:async()=>assert.fail('must not verify files'),send:async()=>{sends++;}})({id:'cross-kind-collision'});
  assert.equal(result.state,'needs-review');assert.equal(result.safeToRelease,true);
  assert.equal(result.failure.code,'contact-transport-identities-invalid');
  assert.deepEqual([receipts,reviews,sends],[0,0,0]);
});

test('operational review failure is bounded, redacted and an old retry storm is parked immediately',async()=>{
  const journal=new Map();let clock=0;
  const batch=createContactBatch({...store(journal),now:()=>clock,maxReviewFailures:2,
    preflight:async()=>({state:'pending',reason:'share-review-unavailable',failure:{category:'model-unavailable',stage:'contact-review-model',code:'deepseek-timeout',retry_condition:'backoff',
      model_invoked:true,model_receipt:{provider:'deepseek',model:'deepseek-flash',usage:null,usage_status:'unknown',outcome:'timeout',prompt:'must-not-persist'}}}),
    send:async()=>assert.fail('must not send')});
  assert.equal((await batch({id:'bounded-outage',bubbles:['One']})).state,'pending');clock=60000;
  const parked=await batch({id:'bounded-outage'});
  assert.equal(parked.state,'needs-review');assert.equal(parked.failure.category,'model-unavailable');assert.equal(parked.failure.model_invoked,true);
  assert.equal(parked.failure.model_receipt.outcome,'timeout');assert.ok(!JSON.stringify(journal.get('bounded-outage')).includes('must-not-persist'));

  const storm=new Map([['old-storm',{id:'old-storm',channel:'synthetic',state:'pending',backoffs:464,reason:'share-review-unavailable',items:[
    {id:'old-storm-one',text:'One',references:[],state:'unsent'}]}]]);let calls=0;
  const recovered=await createContactBatch({...store(storm),preflight:async()=>{calls++;},send:async()=>assert.fail('must not send')})({id:'old-storm'});
  assert.equal(recovered.state,'needs-review');assert.equal(recovered.failure.code,'legacy-review-failures-exhausted');assert.equal(calls,0);
});

test('a thrown structured review failure keeps only its diagnostic receipt',async()=>{
  const journal=new Map();
  const error=Error('private provider response');
  error.failure={category:'model-unavailable',stage:'contact-review-model',code:'deepseek-timeout',retry_condition:'backoff',model_invoked:true,
    model_receipt:{provider:'deepseek',outcome:'timeout',usage:null,prompt:'secret prompt'}};
  const batch=createContactBatch({...store(journal),maxReviewFailures:1,
    preflight:async()=>{throw error;},send:async()=>assert.fail('must not send')});
  const result=await batch({id:'structured-throw',bubbles:['One']});
  assert.equal(result.state,'needs-review');assert.equal(result.failure.category,'model-unavailable');
  assert.equal(result.failure.model_invoked,true);assert.deepEqual(result.failure.model_receipt,{provider:'deepseek',outcome:'timeout',usage:null});
  assert.ok(!JSON.stringify(journal.get('structured-throw')).includes('private provider response'));
  assert.ok(!JSON.stringify(journal.get('structured-throw')).includes('secret prompt'));
});

/** One interrupted send, then whatever the transport wrote down about it. */
const interrupted=async(journal,receipts,sent,reviews)=>{
  const options={...store(journal),receipt:id=>receipts.get(id)??null,
    preflight:async request=>{reviews.push(request.entries.length);return approved(request);},
    send:async request=>{sent.push(request.id);if(sent.length===1)throw Error('connection lost');return {state:'accepted',messageId:'receipt-'+request.id};}};
  const first=await createContactBatch(options)({id:'interrupted',bubbles:['One part.','Another part.']});
  assert.equal(first.state,'unconfirmed');assert.equal(sent.length,1);
  return options;
};

test('a receipt proving nothing was submitted sends the same frozen ID again',async()=>{
  const journal=new Map(),receipts=new Map(),sent=[],reviews=[];
  const options=await interrupted(journal,receipts,sent,reviews);
  const ids=journal.get('interrupted').items.map(item=>item.id);
  receipts.set(ids[0],{state:'not-submitted'});
  const finished=await createContactBatch(options)({id:'interrupted'});
  assert.equal(finished.state,'accepted');
  assert.deepEqual(sent,[ids[0],ids[0],ids[1]]);            // the same frozen ID, never a new one
  assert.equal(finished.acceptedBubbles,2);assert.equal(finished.partial,false);
  assert.deepEqual(reviews,[2]);                            // the group's verdict is already persisted
});

test('a definitive platform refusal cancels that bubble and lets the group finish',async()=>{
  const journal=new Map(),receipts=new Map(),sent=[],reviews=[];
  const options=await interrupted(journal,receipts,sent,reviews);
  const ids=journal.get('interrupted').items.map(item=>item.id);
  receipts.set(ids[0],{state:'rejected',platformCode:'230001'});
  const finished=await createContactBatch(options)({id:'interrupted'});
  assert.deepEqual(sent,[ids[0],ids[1]]);                   // a refused bubble is never sent again
  assert.equal(finished.partial,true);
  assert.equal(finished.canceledBubbles,1);assert.equal(finished.acceptedBubbles,1);
  assert.deepEqual(reviews,[2]);
  const refused=journal.get('interrupted').items[0];
  assert.equal(refused.state,'canceled');assert.equal(refused.reason,'platform-rejected');
  assert.equal(refused.platformCode,'230001');
  // A refusal that carries prose instead of a code keeps the reason and drops the prose.
  const wordy=new Map(),wordyReceipts=new Map(),wordySent=[];
  const second=await interrupted(wordy,wordyReceipts,wordySent,[]);
  const wordyIds=wordy.get('interrupted').items.map(item=>item.id);
  wordyReceipts.set(wordyIds[0],{state:'rejected',platformCode:'the platform said no'});
  await createContactBatch(second)({id:'interrupted'});
  assert.equal(wordy.get('interrupted').items[0].reason,'platform-rejected');
  assert.equal(wordy.get('interrupted').items[0].platformCode,undefined);
});

test('a receipt that cannot tell still blocks the group and sends nothing',async()=>{
  const journal=new Map(),receipts=new Map(),sent=[],reviews=[];
  const options=await interrupted(journal,receipts,sent,reviews);
  const ids=journal.get('interrupted').items.map(item=>item.id);
  assert.equal((await createContactBatch(options)({id:'interrupted'})).state,'unconfirmed');
  assert.deepEqual(sent,[ids[0]]);                          // no receipt where the reader looked: never a second send
  receipts.set(ids[0],{state:'pending',submissionStarted:true});
  assert.equal((await createContactBatch(options)({id:'interrupted'})).state,'unconfirmed');
  assert.deepEqual(sent,[ids[0]]);
  assert.equal(journal.get('interrupted').items[0].state,'unconfirmed');
  assert.equal(journal.get('interrupted').items[1].state,'unsent');
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

test('a bubble that never starts is re-sent a bounded number of times, then given up on while the rest goes out',async()=>{
  const journal=new Map(),attempts=[];
  const args={read:id=>structuredClone(journal.get(id)),write:(id,value)=>journal.set(id,structuredClone(value)),
    receipt:id=>attempts.includes(id)?{state:'not-submitted',submissionStarted:false}:null,
    send:async request=>{attempts.push(request.id);
      if(request.text==='The first bubble.')throw Error('the transport never began');
      return {state:'accepted',messageId:'server-'+request.id};}};
  const batch=createContactBatch(args),request={id:'never-starts',bubbles:['The first bubble.','The second bubble.'],channel:'synthetic'};
  for(let pass=0;pass<7;pass++) {
    const result=await batch({...request,id:request.id});
    assert.equal(result.state,'unconfirmed','the group waits while the first bubble is still being tried');
  }
  assert.equal(attempts.length,7,'the first send and six more, the same bound the transport gives a group');
  const settled=await batch({id:request.id});
  const [first,second]=journal.get(request.id).items;
  assert.deepEqual([first.state,first.reason,first.restarts],['canceled','transport-never-started',7]);
  assert.deepEqual([second.state,settled.state,settled.acceptedBubbles,settled.partial],['accepted','accepted',1,true]);
  assert.equal(attempts.filter(id=>id===first.id).length,7,'no eighth attempt at the bubble that was given up on');
  await batch({id:request.id});
  assert.equal(attempts.length,8,'seven at the first bubble, one at the second, and nothing once the group is settled');
});

test('a group whose every attempt is proven never-started releases safely for a new DS decision',async()=>{
  const journal=new Map(),attempts=[];
  const batch=createContactBatch({...store(journal),maxFailures:2,
    receipt:()=>({state:'not-submitted',submissionStarted:false}),
    send:async request=>{attempts.push(request.id);throw Error('pre-submit unavailable');}});
  const request={id:'never-started-group',bubbles:['One']};let result=await batch(request);
  while(result.state==='unconfirmed')result=await batch({id:request.id});
  assert.equal(result.state,'needs-review');assert.equal(result.safeToRelease,true);
  assert.equal(result.failure.stage,'contact-delivery');assert.equal(result.failure.code,'transport-never-started-exhausted');
  assert.equal(journal.get(request.id).items[0].submission,'never-started');assert.equal(attempts.length,3);
  const again=await batch({id:request.id});assert.equal(again.state,'needs-review');assert.equal(attempts.length,3);
});
