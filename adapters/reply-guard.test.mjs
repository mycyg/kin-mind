import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ReplyGuard,outboxEvidence} from './reply-guard.mjs';
import {createContactBatch} from './contact-batch.mjs';
import {createFakeTransport} from './testing/fake-transport.mjs';

test('a deferred ordinary reply retains every bubble and reconciles uncertainty without replay',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-group-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let now=0,ready=false;const sent=[],outbox=[];
 const args={directory,transportManifest:false,clock:()=>now,outbox:()=>outbox,call:async(action,r)=>({state:r.text==='body'&&!ready?'pending':'ready',text:r.text})};
 let guard=new ReplyGuard(args);
 const entries=['intro','body','tail'].map((text,i)=>({request:{draft_id:'d'+i,text},delivery:{id:'m'+i,text}}));
 assert.equal((await guard.checkGroup(entries.map(e=>e.request))).state,'pending');
 guard.deferGroup(entries,'owner');ready=true;now=60001;guard=new ReplyGuard(args);
 const options={guard:async()=> 'send',send:async r=>{sent.push(r.id);return {state:r.id==='m1'?'unconfirmed':'accepted',messageId:r.id};}};
 await guard.resumeDue(options);assert.deepEqual(sent,['m0','m1']);
 await guard.resumeDue(options);assert.deepEqual(sent,['m0','m1']);
 outbox.push({id:'m1',state:'accepted',message_id:'server-m1'});
 await guard.resumeDue(options);assert.deepEqual(sent,['m0','m1','m2']);
 await guard.resumeDue(options);assert.equal(sent.length,3);
});

test('deliberate silence belongs to one casual input; work and new inputs remain actionable',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-reply-'));
 try {
  const calls=[];
  const call=async(action,data)=>{calls.push(action);return action==='reply-status'?{action:data.input_id==='one'?'silent':'reply'}:{state:'ready',text:data.text};};
  const guard=new ReplyGuard({call,directory});
  assert.equal((await guard.check({reply_id:'one',draft_id:'a',text:'hello'})).state,'silent');
  assert.equal(calls.includes('share-preflight'),false);
  assert.equal((await guard.check({reply_id:'two',draft_id:'b',text:'new'})).state,'ready');
  assert.equal((await guard.check({reply_id:'one',draft_id:'work',text:'delivery',work:true})).state,'ready');
  const restarted=new ReplyGuard({call:()=>{throw Error('must reuse decision');},directory});
  assert.equal((await restarted.check({reply_id:'one',draft_id:'a',text:'hello'})).state,'silent');
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

test('semantic wait rechecks task/new-input guard; partial receipt IDs are stable',async()=>{
 const saved=new Map(),sent=[];let valid=true,first=true;
 const options={read:async id=>saved.get(id),write:async(id,data)=>saved.set(id,structuredClone(data)),
  preflight:async request=>{if(first){valid=false;first=false;}return {state:'ready',checked:request.entries.map(entry=>({draft_id:entry.draft_id,text:entry.text,references:entry.references}))};},
  send:async req=>{sent.push(req);return {state:'accepted',messageId:'receipt-'+req.id};},guard:()=>valid};
 const send=createContactBatch(options);
 const req={id:'batch',bubbles:['One finding','Another finding'],references:[[{unit_id:'x',version:1}],[{unit_id:'y',version:1}]],guard:()=>valid};
 assert.equal((await send(req)).state,'pending');assert.equal(sent.length,0);
 valid=true;assert.equal((await send(req)).state,'accepted');assert.equal(sent.length,2);
 assert.equal(sent[0].references[0].unit_id,'x');
 await createContactBatch(options)(req);assert.equal(sent.length,2);
 await assert.rejects(send({...req,references:[[{unit_id:'z',version:1}]]}),/reference-conflict/);
});

test('outbox exposes only public evidence and stable receipt data',()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-outbox-'));
 try{fs.writeFileSync(path.join(dir,'one.json'),JSON.stringify({id:'one',state:'accepted',text:'public',messageId:'r1',references:[{unit_id:'f',version:1}],token:'never-export',reasoning_content:'never-export'}));
 const evidence=outboxEvidence([dir]);assert.equal(evidence[0].message_id,'r1');assert.equal(JSON.stringify(evidence).includes('never-export'),false);
 }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test('pending replies survive restart, freeze a stable send and never replay uncertainty',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-deferred-'));let now=0,calls=0;
 const call=async()=>({state:'ready',text:'Remember that observation?',references:[]});
 try {
  let guard=new ReplyGuard({call,directory,transportManifest:false,clock:()=>now});
  guard.defer({draft_id:'d',reply_id:'input',text:'Old finding'},{id:'stable-message',text:'Old finding'},'owner-epoch');
  guard=new ReplyGuard({call,directory,transportManifest:false,clock:()=>now});now=21*60000;
  const options={guard:async()=> 'send',send:async delivery=>{calls++;assert.equal(delivery.id,'stable-message');assert.equal(delivery.text,'Remember that observation?');return {state:'unconfirmed'};}};
  await guard.resumeDue(options);await guard.resumeDue(options);assert.equal(calls,1);
  guard.defer({draft_id:'later',text:'Stale thought'},{id:'later'},'old-epoch');now+=21*60000;
  await guard.resumeDue({...options,guard:async()=> 'cancel'});assert.equal(calls,1);
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

test('whole reply freezes every body before transport, survives restart and never replays accepted or uncertain IDs',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-whole-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 const entries=['intro','complete body','tail'].map((text,i)=>({request:{draft_id:'d'+i,reply_id:'current',text},delivery:{id:'m'+i,text}}));
 const sent=[],outbox=[];let reviews=0,now=0;
 const args={directory,transportManifest:false,clock:()=>now,wholeReplyReview:true,outbox:()=>outbox,call:async(action,r)=>{
  if(action==='reply-status')return {action:'reply'};
  assert.equal(action,'share-preflight-group');assert.equal(r.entries.length,3);
  if(!r.frozen)reviews++;
  return {state:'ready',checked:r.entries.map(e=>({...e,state:'ready',references:[]}))};
 }};
 let guard=new ReplyGuard(args);
 const review=await guard.checkGroup(entries.map(e=>e.request));
 const options={guard:async()=> 'send',send:async d=>{sent.push(d);return d.id==='m1'?{state:'unconfirmed'}:{state:'accepted',messageId:'server-'+d.id};}};
 assert.equal((await guard.deliverGroup(entries,'owner',review,options)).state,'unconfirmed');
 now=60001;guard=new ReplyGuard(args);await guard.resumeDue(options);
 assert.deepEqual(sent.map(s=>s.id),['m0','m1']);
 outbox.push({id:'m1',state:'accepted',message_id:'server-m1'});await guard.resumeDue(options);
 assert.deepEqual(sent.map(s=>s.id),['m0','m1','m2']);assert.equal(reviews,1);
 await guard.resumeDue(options);assert.equal(sent.length,3);
});

test('semantic duplicate is durable pending; genuine input-specific silence bypasses review',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-hold-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let silence=false,models=0;
 const guard=new ReplyGuard({directory,wholeReplyReview:true,call:async(action)=>{
  if(action==='reply-status')return {action:silence?'silent':'reply',input_id:'one'};
  models++;return {state:'duplicate',reason:'Needs revision'};
 }});
 const request={draft_id:'d',reply_id:'one',text:'body'};
 assert.equal((await guard.checkGroup([request])).state,'pending');
 silence=true;assert.equal((await guard.checkGroup([request])).state,'silent');assert.equal(models,1);
});

for(const transportManifest of [false,true])test(`a late input choice releases prepared unsent finding reservations (transport manifest ${transportManifest?'on':'off'})`,async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-late-choice-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let silent=false;const canceled=[];
 const guard=new ReplyGuard({directory,transportManifest,wholeReplyReview:true,call:async(action,r)=>{
  if(action==='reply-status')return {action:silent?'silent':'reply',input_id:'current'};
  if(action==='share-cancel'){canceled.push(r.draft_id);return {state:'canceled'};}
  return {state:'ready',checked:r.entries.map(e=>({...e,references:[]}))};
 }});
 const entries=[{request:{draft_id:'finding-draft',reply_id:'current',text:'Body'},delivery:{id:'fixed-id',text:'Body'}}];
 const review=await guard.checkGroup(entries.map(e=>e.request));silent=true;
 const result=await guard.deliverGroup(entries,'owner',review,{guard:async()=> 'send',send:()=>{throw Error('must not send');}});
 assert.equal(result.state,'silent');assert.deepEqual(canceled,['finding-draft']);
});

// ---- transport manifest on (the default) -----------------------------------
const quiet={sleep:async()=>{},lease:{heartbeat:false}};
const replyEntries=(batch,texts)=>texts.map((text,i)=>({request:{draft_id:batch+'-d'+i,reply_id:'current',text},delivery:{id:batch+'-m'+i,text,kind:'reply',draftId:batch+'-d'+i,memoryBatchId:batch,expectedBubbles:texts.length}}));
const wholeReview=counter=>async(action,r)=>{
 if(action==='reply-status')return {action:'reply'};
 if(action==='share-cancel')return {state:'canceled'};
 assert.equal(action,'share-preflight-group');if(!r.frozen)counter.reviews++;
 return {state:'ready',review_id:'review',checked:r.entries.map(e=>({...e,state:'ready',references:[]}))};
};

test('with only the wiring the current host passes, a whole reply is delivered through the manifest and old uncertainty still never replays',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-host-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 const counter={reviews:0},sent=[],outbox=[],outcomes=[];
 const args={directory,wholeReplyReview:true,outbox:()=>outbox,onOutcome:detail=>outcomes.push(detail),call:wholeReview(counter)};
 let guard=new ReplyGuard(args);
 const entries=replyEntries('reply-host',['intro','complete body','tail']);
 const review=await guard.checkGroup(entries.map(e=>e.request));
 const options={guard:async entry=>{assert.equal(entry.ownerEpoch,'owner');assert.equal(entry.request.reply_id,'current');return 'send';},
  send:async d=>{sent.push(d);return d.bubbleId==='reply-host-m1'?{state:'unconfirmed'}:{state:'accepted',messageId:'server-'+d.bubbleId};}};
 const first=await guard.deliverGroup(entries,'owner',review,options);
 assert.equal(first.state,'unconfirmed');assert.equal(first.groupState,'blocked-unknown');assert.equal(first.groupId,'reply-host');
 guard=new ReplyGuard(args);await guard.resumeDue(options);
 assert.deepEqual(sent.map(d=>d.bubbleId),['reply-host-m0','reply-host-m1']);
 assert.ok(sent.every(d=>/^kin-frag-[0-9a-f]{48}$/.test(d.id)&&d.memory===false&&d.memoryBatchId==='reply-host'));
 outbox.push({id:sent[1].id,state:'accepted',message_id:'server-late'});
 await guard.resumeDue(options);
 assert.deepEqual(sent.map(d=>d.bubbleId),['reply-host-m0','reply-host-m1','reply-host-m2']);assert.equal(counter.reviews,1);
 await guard.resumeDue(options);assert.equal(sent.length,3);
 const again=await guard.deliverGroup(entries,'owner',review,options);
 assert.equal(again.state,'accepted');assert.deepEqual(again.entries.map(e=>e.delivery.text),['intro','complete body','tail']);assert.equal(sent.length,3);
 assert.ok(outcomes.every(o=>o.groupId==='reply-host'&&o.inputId==='current'));assert.deepEqual(outcomes.at(-1).state,'accepted');
 assert.deepEqual(fs.readdirSync(directory).filter(n=>n.endsWith('.pending.json')),[],'no old journal file is written any more');
 assert.deepEqual(fs.readdirSync(path.join(directory,'reply-manifests')).filter(n=>n.endsWith('.json')),[],'the finished group left the live set');
});

test('replyGroup puts the draft on disk before the review, the final manifest before the send',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-order-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 const manifestDirectory=path.join(directory,'reply-manifests'),order=[];
 const stateOnDisk=()=>JSON.parse(fs.readFileSync(path.join(manifestDirectory,'reply-order.json'),'utf8')).state;
 const guard=new ReplyGuard({directory,manifestDirectory,wholeReplyReview:true,...quiet,call:async(action,r)=>{
  if(action==='reply-status')return {action:'reply'};
  order.push([r.frozen?'validate':'review',stateOnDisk()]);
  return {state:'ready',review_id:'review',checked:r.entries.map(e=>({text:e.text.toUpperCase(),references:[]}))};
 }});
 const transport=createFakeTransport({directory:path.join(directory,'outbox')});
 transport.platform.onSubmit(()=>{order.push(['send',stateOnDisk()]);});
 const result=await guard.replyGroup(replyEntries('reply-order',['one','two']),'owner',{guard:async()=>'send',send:transport});
 assert.deepEqual(order,[['review','draft'],['send','sending'],['send','sending']]);
 assert.equal(result.state,'accepted');assert.deepEqual(result.entries.map(e=>e.delivery.text),['ONE','TWO'],'the reviewed text is what was frozen and sent');
 assert.deepEqual(transport.sends.map(d=>d.text),['ONE','TWO']);
});

test('re-entry on a group in flight gets busy and marks nothing unconfirmed',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-busy-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 const counter={reviews:0},guard=new ReplyGuard({directory,wholeReplyReview:true,...quiet,call:wholeReview(counter)});
 const entries=replyEntries('reply-busy',['one','two']),review=await guard.checkGroup(entries.map(e=>e.request));
 let release;const gate=new Promise(resolve=>{release=resolve;}),sent=[];
 const options={guard:async()=>'send',send:async d=>{sent.push(d.bubbleId);await gate;return {state:'accepted',messageId:'server-'+d.bubbleId};}};
 const first=guard.deliverGroup(entries,'owner',review,options);
 while(!sent.length)await new Promise(resolve=>setImmediate(resolve));
 const file=path.join(directory,'reply-manifests','reply-busy.json'),before=fs.readFileSync(file,'utf8');
 const second=await guard.deliverGroup(entries,'owner',review,{...options,send:()=>{throw Error('must not send');}});
 assert.deepEqual([second.state,second.groupId],['busy','reply-busy']);
 assert.equal((await new ReplyGuard({directory,wholeReplyReview:true,...quiet,call:wholeReview(counter)}).resumeDue({...options,send:()=>{throw Error('must not send');}})).checked,0);
 assert.equal(fs.readFileSync(file,'utf8'),before);assert.ok(!before.includes('unconfirmed')&&!before.includes('"unknown"'));
 release();assert.equal((await first).state,'accepted');assert.deepEqual(sent,['reply-busy-m0','reply-busy-m1']);
});

test('deferred groups are held manifests; bubble-by-bubble review only ever looks at bubbles that have not begun',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-defer-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let now=0,ready=false;const reviewed=[],canceled=[];
 const args={directory,clock:()=>now,...quiet,call:async(action,r)=>{
  if(action==='share-cancel'){canceled.push(r.draft_id);return {state:'canceled'};}
  if(action==='reply-status')return {action:'reply'};
  reviewed.push(r.draft_id);return {state:r.text==='body'&&!ready?'pending':'ready',text:r.text};
 }};
 let guard=new ReplyGuard(args);
 const entries=replyEntries('reply-defer',['intro','body','tail']);
 assert.equal((await guard.checkGroup(entries.map(e=>e.request))).state,'pending');
 const parked=guard.deferGroup(entries,'owner');
 assert.deepEqual([parked.state,parked.groupState,parked.retryAt],['pending','held',60000]);
 assert.deepEqual(guard.deferGroup(entries,'owner').groupId,'reply-defer','deferring twice is the same group');
 const single=guard.defer({draft_id:'solo-d',reply_id:'current',text:'solo'},{id:'solo-m',text:'solo',kind:'reply',memoryBatchId:'reply-solo',expectedBubbles:4},'owner');
 assert.deepEqual([single.groupState,single.retryAt,single.delivery.expectedBubbles],['held',20*60000,4]);
 const transport=createFakeTransport({directory:path.join(directory,'outbox'),clock:()=>now});
 transport.fault('before-receipt',{when:d=>d.bubbleId==='reply-defer-m2'});
 const options={guard:async()=>'send',send:transport};
 ready=true;now=59999;assert.equal((await guard.resumeDue(options)).state,'idle');
 now=60000;reviewed.length=0;guard=new ReplyGuard(args);await guard.resumeDue(options);
 assert.deepEqual(reviewed,['reply-defer-d0','reply-defer-d1','reply-defer-d2']);assert.deepEqual(transport.sends.map(d=>d.bubbleId),['reply-defer-m0','reply-defer-m1','reply-defer-m2']);
 now+=60000;reviewed.length=0;await guard.resumeDue(options);
 assert.deepEqual(reviewed,['reply-defer-d2'],'sent bubbles are never put through review again');
 assert.deepEqual(transport.platform.delivered.map(d=>d.body),['intro','body','tail']);
 // A bare 'cancel' is how the host says "a newer message arrived". With tail decisions on (the default) that interrupts
 // the group and releases nothing; a host that means a real cancel says so and gives its reason.
 guard.defer({draft_id:'gone-d',reply_id:'current',text:'gone'},{id:'gone-m',text:'gone',kind:'reply',memoryBatchId:'reply-gone',expectedBubbles:1},'owner');
 now=40*60000;await guard.resumeDue({...options,guard:async entry=>entry.groupId==='reply-solo'?'cancel':entry.groupId==='reply-gone'?{action:'cancel',reason:'session-superseded'}:'send'});
 assert.deepEqual(canceled,['gone-d']);assert.equal(transport.platform.delivered.length,3);
 assert.deepEqual(['reply-solo','reply-gone'].map(id=>{const m=guard.manifests.read(id);return [m.state,m.reason];}),[['interrupted','input-or-session-superseded'],['retired','session-superseded']]);
});

test('with reply_tail_decision switched off a bare cancel from the host guard retires the group, as it did before tails existed',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-cancel-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let now=0;const canceled=[];
 const guard=new ReplyGuard({directory,clock:()=>now,replyTailDecision:false,...quiet,call:async(action,r)=>{if(action==='share-cancel')canceled.push(r.draft_id);return action==='reply-status'?{action:'reply'}:{state:'ready',text:r.text};}});
 assert.equal(guard.tail,null);
 guard.defer({draft_id:'solo-d',reply_id:'current',text:'solo'},{id:'solo-m',text:'solo',kind:'reply',memoryBatchId:'reply-solo',expectedBubbles:1},'owner');
 now=20*60000;
 const result=await guard.resumeDue({guard:async()=>'cancel',send:()=>{throw Error('must not send');}});
 assert.deepEqual(canceled,['solo-d']);assert.equal(result.tail,undefined);
 assert.deepEqual([guard.manifests.read('reply-solo').state,guard.manifests.read('reply-solo').reason],['retired','input-or-session-superseded']);
});

test('resumeDue imports unfinished groups of the old journal once; finished ones and the switched-off guard are left alone',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-legacy-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let now=0;const counter={reviews:0},args={directory,clock:()=>now,wholeReplyReview:true,...quiet,call:wholeReview(counter)};
 const old=new ReplyGuard({...args,transportManifest:false});
 const pending=replyEntries('reply-old-pending',['waiting one','waiting two']),finished=replyEntries('reply-old-done',['delivered long ago']);
 old.deferGroup(pending,'owner');
 await old.deliverGroup(finished,'owner',await old.checkGroup(finished.map(e=>e.request)),{guard:async()=>'send',send:async d=>({state:'accepted',messageId:'server-'+d.id})});
 assert.equal(fs.existsSync(path.join(directory,'reply-manifests')),false,'switched off, the guard never touches the manifest directory');
 const journal=()=>Object.fromEntries(fs.readdirSync(directory).filter(n=>n.endsWith('.pending.json')).map(n=>{const v=JSON.parse(fs.readFileSync(path.join(directory,n),'utf8'));return [v.delivery.id,v.state];}));
 assert.deepEqual(journal(),{'reply-old-pending-m0':'pending','reply-old-done-m0':'accepted'});
 now=60001;const transport=createFakeTransport({directory:path.join(directory,'outbox'),clock:()=>now});
 const result=await new ReplyGuard(args).resumeDue({guard:async()=>'send',send:transport});
 assert.equal(result.imported,1);assert.deepEqual(journal(),{'reply-old-pending-m0':'migrated','reply-old-done-m0':'accepted'});
 assert.deepEqual(transport.sends.map(d=>d.id),['reply-old-pending-m0','reply-old-pending-m1'],'imported bubbles go out under their old bubble IDs; the finished reply is never replayed');
 assert.equal((await new ReplyGuard(args).resumeDue({guard:async()=>'send',send:transport})).imported,0);
 assert.deepEqual(await new ReplyGuard({...args,transportManifest:false}).resumeDue({guard:async()=>'send',send:()=>{throw Error('must not send');}}),{state:'idle',checked:0},'the old resume filter ignores migrated files');
});

test('a review that cannot be reached judged nothing: the wait grows, the group is never parked, and it goes out once the review answers',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-outage-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let now=0,down=true,reviews=0;
 const guard=new ReplyGuard({directory,clock:()=>now,wholeReplyReview:true,retry:{maxHolds:2},...quiet,call:async(action,r)=>{
  if(action==='reply-status')return {action:'reply'};
  reviews++;if(down)throw Error('Mind worker failed');
  return {state:'ready',review_id:'review',checked:r.entries.map(e=>({text:e.text,references:[]}))};
 }});
 const transport=createFakeTransport({directory:path.join(directory,'outbox'),clock:()=>now}),options={guard:async()=>'send',send:transport};
 assert.equal((await guard.replyGroup(replyEntries('reply-outage',['one','two']),'owner',options)).groupState,'held');
 for(let tick=0;tick<240;tick++){now+=60000;await guard.resumeDue(options);}
 const held=guard.manifests.read('reply-outage');
 assert.deepEqual([held.state,held.reason,held.parked,held.retryAt-held.updated_at],['held','whole-reply-review-unavailable',undefined,15*60000]);
 assert.ok(reviews>2&&reviews<24,'asked again with a growing wait, a few times an hour at most: '+reviews);
 down=false;now+=15*60000;await guard.resumeDue(options);
 assert.equal(guard.manifests.read('reply-outage').state,'accepted');assert.deepEqual(transport.platform.delivered.map(d=>d.body),['one','two']);
});

test('a host with no old journal directory still resumes its manifests',async t=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-nojournal-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 let now=0;
 const guard=new ReplyGuard({directory:path.join(root,'never-created'),manifestDirectory:path.join(root,'reply-manifests'),clock:()=>now,...quiet,
  call:async(action,r)=>action==='reply-status'?{action:'reply'}:{state:'ready',text:r.text}});
 const transport=createFakeTransport({directory:path.join(root,'outbox'),clock:()=>now});
 guard.defer({draft_id:'late-d',reply_id:'current',text:'late'},{id:'late-m',text:'late',kind:'reply',memoryBatchId:'reply-late',expectedBubbles:1},'owner');
 assert.equal(fs.existsSync(path.join(root,'never-created')),false);
 now=21*60000;
 const resumed=await guard.resumeDue({guard:async()=>'send',send:transport});
 assert.equal(resumed.state,'checked');
 assert.deepEqual(transport.platform.delivered.map(d=>d.body),['late']);
});
