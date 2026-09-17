import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ReplyGuard,outboxEvidence} from './reply-guard.mjs';
import {createContactBatch} from './contact-batch.mjs';

test('a deferred ordinary reply retains every bubble and reconciles uncertainty without replay',async t=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-group-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let now=0,ready=false;const sent=[],outbox=[];
 const args={directory,clock:()=>now,outbox:()=>outbox,call:async(action,r)=>({state:r.text==='body'&&!ready?'pending':'ready',text:r.text})};
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
  preflight:async request=>{if(first){valid=false;first=false;}return {state:'ready',references:request.references};},
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
  let guard=new ReplyGuard({call,directory,clock:()=>now});
  guard.defer({draft_id:'d',reply_id:'input',text:'Old finding'},{id:'stable-message',text:'Old finding'},'owner-epoch');
  guard=new ReplyGuard({call,directory,clock:()=>now});now=21*60000;
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
 const args={directory,clock:()=>now,wholeReplyReview:true,outbox:()=>outbox,call:async(action,r)=>{
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
