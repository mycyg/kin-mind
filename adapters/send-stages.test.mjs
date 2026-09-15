import test from 'node:test';
import assert from 'node:assert/strict';
import {submitPayload} from './send-stages.mjs';

test('upload failure proves no message was submitted',async()=>{
  const phases=[];let sent=0;
  await assert.rejects(()=>submitPayload({media:{},uuid:'original',checkpoint:r=>phases.push(r),
    upload:async()=>{throw Error('upload failed');},create:async()=>sent++}),/upload failed/);
  assert.equal(sent,0);assert.equal(phases.at(-1).submissionStarted,false);
});
test('loss after create remains submitted and preserves the same UUID',async()=>{
  const phases=[];let sentUuid;
  await assert.rejects(()=>submitPayload({media:{},uuid:'original',checkpoint:r=>phases.push(r),
    upload:async()=>({file_key:'fixture'}),create:async({uuid})=>{sentUuid=uuid;throw Error('timeout');}}),/timeout/);
  assert.equal(sentUuid,'original');assert.equal(phases.at(-1).submissionStarted,true);
});
test('file and text success both require platform IDs',async()=>{
  for(const media of [undefined,{}]){
    const id=await submitPayload({media,uuid:'same',checkpoint:()=>{},upload:async()=>({file_key:'fixture'}),
      create:async({uuid})=>{assert.equal(uuid,'same');return{code:0,data:{message_id:'accepted'}};}});
    assert.equal(id,'accepted');
    await assert.rejects(()=>submitPayload({uuid:'same',checkpoint:()=>{},create:async()=>({code:0,data:{}})}),/no message ID/);
  }
});
