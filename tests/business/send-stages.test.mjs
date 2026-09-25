import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {submitPayload,platformErrorCode} from '../../adapters/send-stages.mjs';
import {classifyReceipt,normalizeReceipt,fileReceipts} from '../../adapters/channel-contract.mjs';
import {createFakeTransport,createFakePlatform} from './helpers/fake-transport.mjs';

// The stages are the sender's evidence for what a failure means: before `message-submitting`
// nothing can have reached the platform, so the same UUID may be sent again.
const stages=()=>{const seen=[];return {seen,checkpoint:value=>seen.push(value)};};

test('a media send records each boundary, and only the create call starts the submission',async()=>{
  const {seen,checkpoint}=stages(),calls=[];
  const id=await submitPayload({media:{name:'a.png'},uuid:'u-1',checkpoint,
    upload:async media=>{calls.push(['upload',media.name]);return {file_key:'fk-1'};},
    create:async request=>{calls.push(['create',request]);return {code:0,data:{message_id:'om_1'}};}});
  assert.equal(id,'om_1');
  assert.deepEqual(seen.map(s=>[s.stage,s.submissionStarted]),[['uploading',false],['uploaded',false],['message-submitting',true]]);
  assert.deepEqual(seen[1].uploadKeys,{file_key:'fk-1'});
  assert.deepEqual(calls,[['upload','a.png'],['create',{content:{file_key:'fk-1'},uuid:'u-1'}]]);
});

test('an upload that fails or returns no key never reaches submission',async()=>{
  for(const upload of [async()=>{throw Error('network down');},async()=>({file_key:''}),async()=>null]){
    const {seen,checkpoint}=stages();let created=false;
    await assert.rejects(submitPayload({media:{},uuid:'u',checkpoint,upload,create:async()=>{created=true;}}));
    assert.equal(created,false);assert.ok(seen.length&&seen.every(s=>s.submissionStarted===false));
  }
});

test('a platform refusal and an answer without a message id are told apart',async()=>{
  const {checkpoint}=stages();
  await assert.rejects(submitPayload({uuid:'u',checkpoint,create:async()=>({code:230002,msg:'bot not in chat'})}),
    e=>e.code==='PLATFORM_REJECTED'&&e.platformCode===230002);
  await assert.rejects(submitPayload({uuid:'u',checkpoint,create:async()=>({code:0,data:{}})}),e=>e.code==='PLATFORM_NO_MESSAGE_ID');
  const text=stages();
  assert.equal(await submitPayload({uuid:'u',checkpoint:text.checkpoint,create:async()=>({code:0,data:{message_id:'om_2'}})}),'om_2');
  assert.deepEqual(text.seen.map(s=>s.stage),['message-submitting']);
});

test('a send asks before its upload and before its message request, and stopping there submits nothing (CR3-FLOW-04)',async()=>{
  const asked=[];
  const {seen,checkpoint}=stages();
  assert.equal(await submitPayload({media:{name:'a.png'},uuid:'u',checkpoint,beforeSubmit:stage=>asked.push(stage),
    upload:async()=>({file_key:'fk'}),create:async()=>({code:0,data:{message_id:'om_3'}})}),'om_3');
  assert.deepEqual(asked,['upload','message']);
  assert.deepEqual(seen.map(s=>s.stage),['uploading','uploaded','message-submitting']);
  // Stopped before the message request (an admission lost during the upload): uploaded, never submitted.
  for(const [refuse,expected] of [['upload',[]],['message',['uploading','uploaded']]]) {
    const run=stages();let created=false,uploaded=false;
    await assert.rejects(submitPayload({media:{name:'b.png'},uuid:'u',checkpoint:run.checkpoint,
      beforeSubmit:stage=>{if(stage===refuse)throw Object.assign(Error('lost'),{code:'KIN_SEND_ADMISSION_LOST'});},
      upload:async()=>{uploaded=true;return {file_key:'fk'};},create:async()=>{created=true;return {code:0,data:{message_id:'om_4'}};}}),
      error=>error.code==='KIN_SEND_ADMISSION_LOST');
    assert.equal(created,false);assert.equal(uploaded,refuse==='message');
    assert.deepEqual(run.seen.map(s=>s.stage),expected);assert.ok(run.seen.every(s=>s.submissionStarted===false));
  }
});

test('a check that answers later is waited for: nothing is uploaded or requested before it, and its rejection submits nothing (CR4-FLOW-02)',async()=>{
  // The host's send admission renews with the granting host before each stage, so its answer arrives after a round trip.
  const order=[],{seen,checkpoint}=stages();
  const later=(stage,fail)=>new Promise((resolve,reject)=>setTimeout(()=>{order.push('checked '+stage);fail?reject(Object.assign(Error('lost'),{code:'KIN_SEND_ADMISSION_LOST'})):resolve();},5));
  assert.equal(await submitPayload({media:{name:'a.png'},uuid:'u',checkpoint:value=>{order.push(value.stage);checkpoint(value);},beforeSubmit:stage=>later(stage),
    upload:async()=>{order.push('upload');return {file_key:'fk'};},create:async()=>{order.push('create');return {code:0,data:{message_id:'om_5'}};}}),'om_5');
  assert.deepEqual(order,['checked upload','uploading','upload','uploaded','checked message','message-submitting','create']);
  // Rejected after the upload (the host restarted meanwhile): uploaded, never submitted.
  const run=stages();let created=false;
  await assert.rejects(submitPayload({media:{name:'b.png'},uuid:'u',checkpoint:run.checkpoint,beforeSubmit:stage=>later(stage,stage==='message'),
    upload:async()=>({file_key:'fk'}),create:async()=>{created=true;return {code:0,data:{message_id:'om_6'}};}}),error=>error.code==='KIN_SEND_ADMISSION_LOST');
  assert.equal(created,false);
  assert.deepEqual(run.seen.map(s=>[s.stage,s.submissionStarted]),[['uploading',false],['uploaded',false]]);
  assert.equal(seen.length,3);
});

test('only a platform error code is a refusal: an answer without a usable code, or no answer, stays unknown (CR5-FLOW-02)',async()=>{
  // The request has left in every case below: what the platform did with it is not in the answer.
  for(const answer of [{},null,undefined,{code:'230002'},{code:'0'},{code:null},{code:1.5},{code:Number.NaN},{code:true},{code:{}},{msg:'ok',data:{message_id:'om_x'}}]) {
    const {seen,checkpoint}=stages();
    await assert.rejects(submitPayload({uuid:'u',checkpoint,create:async()=>answer}),
      error=>error.code==='PLATFORM_ANSWER_UNKNOWN'&&!('platformCode' in error),JSON.stringify(answer)??'undefined');
    assert.deepEqual(seen.map(s=>[s.stage,s.submissionStarted]),[['message-submitting',true]]);
  }
  // A whole number other than zero is the platform's refusal, whatever its sign.
  for(const code of [230002,99991663,-1]) {
    await assert.rejects(submitPayload({uuid:'u',checkpoint:stages().checkpoint,create:async()=>({code,msg:'refused'})}),
      error=>error.code==='PLATFORM_REJECTED'&&error.platformCode===code);
  }
  assert.deepEqual([0,230002,-1,'230002',1.5,null,undefined,Number.MAX_SAFE_INTEGER+2].map(platformErrorCode),[false,true,true,false,false,false,false,false]);
});

test('a refusal an earlier sender wrote without a platform error code reads as unknown, never as a refusal (CR5-FLOW-02)',async()=>{
  // What the Feishu sender wrote for PLATFORM_REJECTED before the rule: `platformCode` only
  // when the answer had one it could print, and nothing when the code was missing.
  const legacy={id:'kin-legacy',state:'rejected',stage:'platform-rejected',submissionStarted:true,submittedAt:'2026-09-20T00:00:00.000Z',
    checkedAt:'2026-09-20T00:00:01.000Z',errorCode:'PLATFORM_REJECTED',errorName:'Error'};
  for(const unproven of [legacy,{...legacy,platformCode:'230002'},{...legacy,platformCode:1.5},{...legacy,platformCode:true}]) {
    assert.equal(classifyReceipt(unproven),'unknown',JSON.stringify(unproven.platformCode));
    const read=normalizeReceipt(unproven);
    assert.deepEqual([read.state,read.refusal,read.reason,read.submissionStarted],['unconfirmed','unproven','platform-rejected',true]);
    assert.equal(classifyReceipt(read),'unknown','read again, it is still unknown');assert.equal(normalizeReceipt(read).refusal,'unproven');
  }
  // With a platform error code it is the platform's refusal, as it was.
  for(const platformCode of [230002,-1]) {
    const proven={...legacy,platformCode};
    assert.equal(classifyReceipt(proven),'rejected');assert.equal(normalizeReceipt(proven).state,'rejected');assert.equal(normalizeReceipt(proven).refusal,undefined);
  }
  // Read from the outbox the way every consumer reads it.
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-legacy-refusal-'));
  try {
    fs.writeFileSync(path.join(dir,'kin-legacy.json'),JSON.stringify(legacy));
    fs.writeFileSync(path.join(dir,'kin-coded.json'),JSON.stringify({...legacy,id:'kin-coded',platformCode:230002}));
    const read=fileReceipts([dir]);
    assert.equal(classifyReceipt(await read('kin-legacy')),'unknown');
    assert.equal(classifyReceipt(await read('kin-coded')),'rejected');
  } finally {fs.rmSync(dir,{recursive:true,force:true});}
});

test('the test transport keeps the refusal rule on both flavours: a coded refusal carries its code, an uncoded answer stays unknown (CR5-FLOW-02)',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-fake-refusal-'));
  try {
    for(const flavour of ['feishu','wechat']) {
      const platform=createFakePlatform(),transport=createFakeTransport({directory:path.join(dir,flavour),platform,flavour});
      platform.script('reject');
      await assert.rejects(transport.send({id:'kin-refused',text:'一句话'}),error=>error.code==='PLATFORM_REJECTED');
      assert.equal(classifyReceipt(await transport.receipt('kin-refused')),'rejected',flavour);
      platform.script('uncoded');
      await assert.rejects(transport.send({id:'kin-uncoded',text:'一句话'}),error=>error.code==='PLATFORM_ANSWER_UNKNOWN');
      assert.equal(classifyReceipt(await transport.receipt('kin-uncoded')),'unknown',flavour);
      await assert.rejects(transport.send({id:'kin-uncoded',text:'一句话'}),error=>error.code==='KIN_SEND_NEEDS_RECONCILE');
      assert.deepEqual([platform.calls.length,platform.delivered.length],[2,1],'the uncoded answer landed, and nothing went twice');
    }
  } finally {fs.rmSync(dir,{recursive:true,force:true});}
});
