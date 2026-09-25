import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {submitPayload,platformErrorCode} from '../../adapters/send-stages.mjs';
import {classifyReceipt,normalizeReceipt,fileReceipts,feishuAnswer,wechatAnswer} from '../../adapters/channel-contract.mjs';
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
    assert.deepEqual(seen.filter(s=>s.stage).map(s=>[s.stage,s.submissionStarted]),[['message-submitting',true]]);
    // A message ID the unknown answer named is kept beside it, as evidence (CL6-FLOW-01).
    assert.deepEqual(seen.filter(s=>s.answer).map(s=>s.answer),answer?.data?[{status:200,reason:'api-code-unreadable',code:null,messageId:'om_x'}]:[]);
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

// What the Feishu SDK throws for an answer outside 2xx: its HTTP client's error, as it came, with the
// answer on it -- the status, and the body parsed from JSON (@larksuiteoapi/node-sdk 1.73.3 over axios 1.20.0).
const httpError=(status,data)=>Object.assign(Error('Request failed with status code '+status),{name:'AxiosError',code:status<500?'ERR_BAD_REQUEST':'ERR_BAD_RESPONSE',response:{status,data}});

test('an answer that names a message and refuses it as well is unknown, never a refusal, and the message ID it named stays on the receipt (CL6-FLOW-01)',async()=>{
  for(const [label,create,status,code,messageId] of [
    ['2xx, code and message ID',async()=>({code:230002,msg:'bot not in chat',data:{message_id:'om_c1'}}),200,230002,'om_c1'],
    ['400, code and message ID',async()=>{throw httpError(400,{code:230001,data:{message_id:'om_c2'}});},400,230001,'om_c2'],
    ['429, code 0 and message ID',async()=>{throw httpError(429,{code:0,data:{message_id:'om_c3'}});},429,null,'om_c3'],
  ]) {
    const {seen,checkpoint}=stages();
    await assert.rejects(submitPayload({uuid:'u',checkpoint,create}),
      error=>error.code==='PLATFORM_ANSWER_UNKNOWN'&&error.reason==='contradictory-answer'&&!('platformCode' in error),label);
    assert.deepEqual(seen,[{stage:'message-submitting',submissionStarted:true},{answer:{status,reason:'contradictory-answer',code,messageId}}],label);
  }
  // The same rule on both channels, read straight from the contract.
  assert.deepEqual(feishuAnswer({status:200,body:{code:230002,data:{message_id:'om_1'}}}),{state:'unknown',reason:'contradictory-answer',code:230002,messageId:'om_1'});
  assert.deepEqual(feishuAnswer({status:200,body:{code:230002,data:{}}}),{state:'rejected',reason:'api-230002',code:230002});
  for(const [status,body] of [[200,{ret:-2,message_id:'123'}],[200,{errcode:40001,message_id:9}],[413,{ret:0,message_id:'9'}],[400,{message_id:'9'}]])
    assert.deepEqual(wechatAnswer({status,body}),{state:'unknown',reason:'contradictory-answer'},JSON.stringify([status,body]));
  // Only a real server message ID contradicts a refusal: zero, a fraction or words are no message.
  for(const message_id of ['0','abc',0,1.5,-3,''])
    assert.equal(wechatAnswer({status:200,body:{ret:-2,message_id}}).state,'rejected',JSON.stringify(message_id));
  // A 5xx is unknown for its status before anything its body says.
  assert.deepEqual(wechatAnswer({status:502,body:{ret:-1,message_id:'9'}}),{state:'unknown',reason:'http-502'});
  // A status that is not a whole number refuses nothing, on either channel.
  for(const status of ['400',400.5,null,undefined]) {
    assert.equal(feishuAnswer({status,body:{code:230001}}).state,'unknown',String(status));
    assert.equal(wechatAnswer({status,body:{}}).state,'unknown',String(status));
  }
});

test('a Feishu HTTP error is judged by the answer on it, as WeChat is: a 4xx other than 409 with the platform code is a refusal, anything else stays the unknown it came as',async()=>{
  // Refusals: the SDK threw, and the answer on the error proves the platform refused the message.
  for(const [status,code] of [[400,230001],[400,99992402],[403,230002],[429,99991400],[404,-1]]) {
    const {seen,checkpoint}=stages();
    await assert.rejects(submitPayload({uuid:'u',checkpoint,create:async()=>{throw httpError(status,{code,msg:'refused'});}}),
      error=>error.code==='PLATFORM_REJECTED'&&error.platformCode===code,`${status} ${code}`);
    assert.deepEqual(seen.map(s=>s.stage),['message-submitting']);
  }
  // Unknown: the error goes on as it came -- the sender records it the way it records a timeout.
  for(const [label,data,status] of [['409 with a code',{code:230001},409],['500 with a code',{code:230001},500],['502 from a proxy','<html>Bad Gateway</html>',502],
    ['400 without a code',{msg:'bad request'},400],['400 with a code in words',{code:'230001'},400],['400 with a fraction',{code:1.5},400],
    ['400 read as text','Bad Request',400],['400 with no body',undefined,400],['a status nobody can read',{code:230001},'400']]) {
    const {seen,checkpoint}=stages(),error=httpError(status,data);
    await assert.rejects(submitPayload({uuid:'u',checkpoint,create:async()=>{throw error;}}),thrown=>thrown===error,label);
    assert.deepEqual(seen.map(s=>s.stage),['message-submitting'],label);
  }
  // No answer at all: a timeout, a lost connection.
  for(const error of [Object.assign(Error('timeout of 12000ms exceeded'),{name:'AxiosError',code:'ECONNABORTED'}),Object.assign(Error('socket hang up'),{code:'ECONNRESET'}),
    Object.assign(Error('odd'),{response:null}),Object.assign(Error('odd'),{response:'text'})])
    await assert.rejects(submitPayload({uuid:'u',checkpoint:stages().checkpoint,create:async()=>{throw error;}}),thrown=>thrown===error,error.message);
  // A 5xx that names a message: unknown, as it came, and the message ID it named stays on the receipt.
  const {seen,checkpoint}=stages(),error=httpError(503,{code:1,data:{message_id:'om_5xx'}});
  await assert.rejects(submitPayload({uuid:'u',checkpoint,create:async()=>{throw error;}}),thrown=>thrown===error);
  assert.deepEqual(seen[1],{answer:{status:503,reason:'http-503',code:null,messageId:'om_5xx'}});
});
