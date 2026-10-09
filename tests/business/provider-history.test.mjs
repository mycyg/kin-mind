import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {checkModelHistory} from '../../adapters/provider-history.mjs';
import {switchCodexModel} from '../../adapters/codex-models.mjs';
const kimi={model:'k3',providerKind:'gateway',provider:'gateway',reasoningEffort:'high',serviceTierPreference:'default'};
const gpt={model:'gpt-6.1-sol',providerKind:'native',provider:'native',reasoningEffort:'medium',serviceTierPreference:'default'};
const meta={type:'session_meta',payload:{id:'synthetic'}};
const opaque={type:'compacted',payload:{replacement_history:[{type:'compaction',encrypted_content:'synthetic-only'}]}};
function fixture(t,rows){const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-history-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));const file=path.join(root,'rollout.jsonl');fs.writeFileSync(file,rows.map(x=>JSON.stringify(x)+'\n').join(''));return {file,runtime:{known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',rolloutPath:file,nativeStatus:'idle',active:false,backgroundTasks:0,model:gpt.model,modelProvider:'native',providerOverride:false,reasoningEffort:'medium',fastMode:'off'}};}
test('uncompacted and readable compacted history remains byte-for-byte intact including tool links and references',async t=>{
 const items=[{type:'message',role:'user',content:'synthetic code and ref [1]'},
 {type:'function_call',call_id:'c1',name:'read_file',arguments:'{"path":"synthetic"}'},{type:'function_call_output',call_id:'c1',output:'synthetic ref [1]'}];
 for(const rows of [[meta,...items.map(payload=>({type:'response_item',payload}))],[meta,{type:'compacted',payload:{replacement_history:items}}]]){
 const f=fixture(t,rows),bytes=fs.readFileSync(f.file);await checkModelHistory(kimi,f.runtime);assert.deepEqual(fs.readFileSync(f.file),bytes);}
});
test('opaque compaction refuses before and after process-like reopen; GPT remains available without rewriting history',async t=>{
 const f=fixture(t,[meta,opaque]),bytes=fs.readFileSync(f.file);
 for(let i=0;i<2;i++)await assert.rejects(checkModelHistory(kimi,{...f.runtime}),{code:'KIMI_HISTORY_INCOMPATIBLE',beforeMutation:true});
 await checkModelHistory(gpt,f.runtime);assert.deepEqual(fs.readFileSync(f.file),bytes);
});
test('missing, mismatched, partial, malformed and unproven compacted histories fail closed',async t=>{
 const f=fixture(t,[meta]);
 for(const body of ['',JSON.stringify(meta),'{broken}\n',JSON.stringify({type:'session_meta',payload:{id:'other'}})+'\n',JSON.stringify(meta)+'\n'+JSON.stringify({type:'compacted',payload:{message:'synthetic summary without authoritative replacement'}})+'\n']){
 fs.writeFileSync(f.file,body);await assert.rejects(checkModelHistory(kimi,f.runtime),{code:'KIMI_HISTORY_UNVERIFIED'});}
 await assert.rejects(checkModelHistory(kimi,{...f.runtime,rolloutPath:null}),{code:'KIMI_HISTORY_UNVERIFIED'});
});
test('switch preflight makes no provider/config mutation on refusal and switching back to GPT remains possible',async t=>{
 const f=fixture(t,[meta,opaque]),calls=[];
 const connection={extMethod:async(method)=>{if(method==='_kin/runtime')return {...f.runtime};calls.push(method);if(method==='providers/disable'){f.runtime.providerOverride=false;f.runtime.modelProvider='native';}},setSessionConfigOption:async p=>{calls.push(p.configId);if(p.configId==='model')f.runtime.model=p.value;if(p.configId==='reasoning_effort')f.runtime.reasoningEffort=p.value;}};
 await assert.rejects(switchCodexModel({connection,sessionId:'synthetic',profile:kimi,gateway:{baseUrl:'http://127.0.0.1',token:'synthetic'}}),{code:'KIMI_HISTORY_INCOMPATIBLE'});assert.deepEqual(calls,[]);
 f.runtime.model='k3';f.runtime.providerOverride=true;
 const after=await switchCodexModel({connection,sessionId:'synthetic',profile:gpt});assert.equal(after.model,gpt.model);assert.equal(calls[0],'providers/disable');
});
test('compatible history supports Kimi then GPT with tool/reference continuity and detects later compaction',async t=>{
 const f=fixture(t,[meta,{type:'response_item',payload:{type:'function_call',call_id:'synthetic-call',name:'read_file',arguments:'{}'}},{type:'response_item',payload:{type:'function_call_output',call_id:'synthetic-call',output:'synthetic reference [1]'}}]);
 const bytes=fs.readFileSync(f.file),calls=[];
 const gateway={baseUrl:'http://127.0.0.1:9999/v1',token:'synthetic'};
 const connection={extMethod:async method=>{if(method==='_kin/runtime')return {...f.runtime};calls.push(method);f.runtime.providerOverride=method==='providers/set';f.runtime.modelProvider=f.runtime.providerOverride?'gateway':'native';f.runtime.providerBaseUrl=f.runtime.providerOverride?gateway.baseUrl:null;},setSessionConfigOption:async p=>{if(p.configId==='model')f.runtime.model=p.value;if(p.configId==='reasoning_effort')f.runtime.reasoningEffort=p.value;}};
 assert.equal((await switchCodexModel({connection,sessionId:'synthetic',profile:kimi,gateway})).model,'k3');
 assert.equal((await switchCodexModel({connection,sessionId:'synthetic',profile:gpt,gateway})).model,'gpt-6.1-sol');
 assert.deepEqual(calls,['providers/set','providers/disable']);assert.deepEqual(fs.readFileSync(f.file),bytes);
 fs.appendFileSync(f.file,JSON.stringify(opaque)+'\n');
 await assert.rejects(switchCodexModel({connection,sessionId:'synthetic',profile:kimi,gateway}),{code:'KIMI_HISTORY_INCOMPATIBLE'});assert.equal(calls.length,2);
});
