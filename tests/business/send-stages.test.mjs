import test from 'node:test';
import assert from 'node:assert/strict';
import {submitPayload} from '../../adapters/send-stages.mjs';

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
