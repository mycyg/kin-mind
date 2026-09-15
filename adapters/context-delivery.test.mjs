import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {NativeContextDelivery} from './context-delivery.mjs';
import {checkpointMarker} from './native-window.mjs';
import {currentCheckpoint} from './mobile-session-host.mjs';
const hash=t=>createHash('sha256').update(t).digest('hex');
function system(t){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-context-test-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const file=path.join(dir,'native.jsonl');fs.writeFileSync(file,'');
 const context={id:'stable',session:'thread',epoch:'e',marker:'kin-context:stable',text:'kin-context:stable\nPublic historical data',tokens:20,state:'prepared'};
 context.text_hash=hash(context.text);let writes=0,count=0,fail=false,afterAppend=false;
 const runtime=async()=>({known:true,active:false,threadId:'thread',rolloutPath:file});
 const call=async(action,args)=>{
  if(action==='context-delivery-pending')return ['sending','unconfirmed'].includes(context.state)?[{...context}]:[];
  if(action==='context-delivery-begin'){if(context.state==='prepared')context.state='sending';return {...context};}
  if(action==='context-delivery-uncertain'){context.state='unconfirmed';return {...context};}
  if(action==='context-delivery-ack'){assert.equal(args.text_hash,context.text_hash);assert.equal(args.verified,true);if(context.state!=='accepted')count+=context.tokens;context.state='accepted';return {...context};}
  throw Error(action);
 };
 const inject=async request=>{writes++;if(fail)throw Error('before receipt');fs.appendFileSync(file,JSON.stringify({type:'response_item',timestamp:'2026-09-15T00:00:00Z',payload:request.items[0]})+'\n');if(afterAppend)throw Error('lost acknowledgement');};
 return {context,file,newHost:()=>new NativeContextDelivery({runtime,call,inject}),stats:()=>({writes,count}),failBefore:()=>fail=true,loseReceipt:()=>afterAppend=true};
}
test('prepared background is counted once from persisted native text',async t=>{const s=system(t);assert.equal((await s.newHost().deliver(s.context)).state,'accepted');assert.equal((await s.newHost().deliver(s.context)).state,'accepted');assert.deepEqual(s.stats(),{writes:1,count:20});});
test('lost append acknowledgement reconciles the original item after restart',async t=>{const s=system(t);s.loseReceipt();assert.equal((await s.newHost().deliver(s.context)).state,'unconfirmed');assert.deepEqual(s.stats(),{writes:1,count:0});assert.equal((await s.newHost().deliver(s.context)).state,'accepted');assert.deepEqual(s.stats(),{writes:1,count:20});});
test('unknown append without evidence is never replayed',async t=>{const s=system(t);s.failBefore();assert.equal((await s.newHost().deliver(s.context)).state,'unconfirmed');assert.equal((await s.newHost().deliver(s.context)).state,'waiting');assert.deepEqual(s.stats(),{writes:1,count:0});});
test('a quoted marker without the frozen body is not a receipt',async t=>{const s=system(t);fs.writeFileSync(s.file,JSON.stringify({type:'response_item',payload:{type:'message',role:'user',content:[{type:'input_text',text:'I saw '+s.context.marker}]}})+'\n');assert.equal((await checkpointMarker(s.file,s.context.marker,{textHash:s.context.text_hash})).found,false);});
test('a busy or wrong native thread cannot receive context',async()=>{const host=new NativeContextDelivery({runtime:async()=>({known:true,active:true,threadId:'another'}),call:async()=>{throw Error('must not call');}});await assert.rejects(()=>host.deliver({id:'a',session:'thread'}),/boundary/);});
test('a new native thread without a rollout file accepts its first append',async t=>{const s=system(t);fs.unlinkSync(s.file);assert.equal((await s.newHost().deliver(s.context)).state,'accepted');assert.deepEqual(s.stats(),{writes:1,count:20});});
test('a user quotation of the exact frozen payload is not host proof',async t=>{const s=system(t);fs.writeFileSync(s.file,JSON.stringify({type:'response_item',payload:{type:'message',role:'user',content:[{type:'input_text',text:s.context.text}]}})+'\n');assert.equal((await checkpointMarker(s.file,s.context.marker,{textHash:s.context.text_hash,role:'assistant'})).found,false);});
test('a completed old restoration does not hide the current rolling manifest',()=>{const old={id:'old'},fresh={id:'current'};assert.equal(currentCheckpoint({restoreCheckpoint:old,rollingCheckpoint:fresh,restorePending:false}),fresh);assert.equal(currentCheckpoint({restoreCheckpoint:old,rollingCheckpoint:fresh,restorePending:true}),old);});
