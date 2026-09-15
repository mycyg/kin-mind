import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {SessionManager} from './session-manager.mjs';
import {SESSION_DEFAULTS,windowPressure,rotationEligibility} from './session-policy.mjs';
import {NativeWindow,checkpointMarker} from './native-window.mjs';
import {recoverSessionStore} from './mobile-session-host.mjs';

function fixture(t){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-sessions-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const clock={now:20000000},runtime={known:true,threadId:'old',sessionId:'old',nativeSessionId:'old',model:'deepseek-flash',reasoningEffort:'max',fastMode:'off',nativeStatus:'idle',modelContextWindow:100000,lastTokenUsage:{inputTokens:70000}};
 const context={cursors:{input:1,send:1},configVersion:'persona-v1',tasks:[],inputs:[]};const calls=[];
 const config={...SESSION_DEFAULTS,prepare:true,rotate:true};
 const options={file:path.join(dir,'registry.json'),binding:{threadId:'old',nativeSessionId:'old',conversationId:'logical'},coordinator:{locked:f=>f()},inspect:async()=>({...runtime}),collect:async()=>structuredClone(context),now:()=>clock.now,config,lease:false,
  checkpoint:async(c,b)=>({id:'cp:'+c.cursors.input,conversationId:b.conversationId,generation:b.generation,configVersion:c.configVersion,cursors:c.cursors,complete:true,tokens:100,sourceRevisions:{u:1,a:1},items:[{id:'u',revision:1,role:'user',text:'蓝色机器人叫云朵。'},{id:'a',revision:1,role:'assistant',text:'你要云朵的方形贴纸吗？'}],pendingQuestions:[{sourceId:'a',target:'云朵的方形贴纸'}]}),
  compact:async id=>{calls.push('compact');runtime.lastTokenUsage.inputTokens=10000;return {completed:true,actual_session:'old',operationId:id,at:new Date(clock.now).toISOString()};},ackCompact:async()=>calls.push('ack'),
  createCandidate:async()=>{calls.push('create');return {threadId:'new',nativeSessionId:'new'};},injectCandidate:async()=>{calls.push('inject');return {verified:true};},verifyCandidate:async({checkpoint})=>({verified:true,checkpointId:checkpoint.id,model:runtime.model,reasoningEffort:runtime.reasoningEffort,fastMode:runtime.fastMode}),
  promote:async()=>{calls.push('promote');return {verified:true,threadId:'new'};},reviewRequested:async()=>calls.push('review')};
 const manager=new SessionManager(options);
 const advise=async(action,evidenceIds=[])=>{await manager.observe(runtime,context);return manager.advise({action,reason:'Synthetic review',evidenceIds,compactionId:manager.state.compactions.at(-1)?.id},{model:'deepseek-flash',reasoning:'max'},manager.state.observation.id);};
 const degraded=()=>{clock.now+=2000000;manager.evidence({id:'failure',sourceId:'owner-correction',revision:1,kind:'reference-error',basis:'owner-statement',at:clock.now});};
 return {manager,options,clock,runtime,context,calls,advise,degraded,dir};
}
test('pressure uses current input, verified window and output reserve; cumulative use is irrelevant',()=>{
 const p=windowPressure({modelContextWindow:100000,lastTokenUsage:{inputTokens:30000,totalTokens:999999999},totalTokenUsage:{totalTokens:1e12}});
 assert.equal(p.ratio,(30000+32768+8192)/100000);assert.equal(p.level,'elevated');assert.equal(windowPressure({}).known,false);
});
test('high pressure is compacted first; fifty historical compactions do not authorize rotation',async t=>{
 const f=fixture(t);await f.advise('rotate');assert.equal((await f.manager.tick()).reason,'compact-first');assert.ok(!f.calls.includes('create'));
 await f.advise('compact');assert.equal((await f.manager.tick()).state,'complete');assert.deepEqual(f.calls.filter(c=>c!=='review'),['compact','ack']);
 f.manager.state.compactions.push(...Array.from({length:50},(_,i)=>({id:'old:'+i,generation:1,state:'complete',completedAt:f.clock.now})));
 await f.advise('rotate');assert.equal((await f.manager.tick()).reason,'post-compaction-evidence-required');
});
test('idle ticks do not call DS repeatedly and recovery does not repeat compaction',async t=>{
 const f=fixture(t);await f.manager.tick();await f.manager.tick();assert.equal(f.calls.filter(c=>c==='review').length,1);
 await f.advise('compact');await f.manager.tick();const recovered=new SessionManager(f.options);await recovered.tick();assert.equal(f.calls.filter(c=>c==='compact').length,1);
});
test('successful compression keeps the same logical and native conversation',async t=>{
 const f=fixture(t),binding=f.manager.fence();await f.advise('compact');await f.manager.tick();await f.advise('keep');assert.equal((await f.manager.tick()).state,'keep');assert.deepEqual(f.manager.fence(),binding);assert.ok(!f.calls.includes('create'));
});
test('active work, background tools, uncertain sends and queued inputs block native compaction',async t=>{
 for(const variant of ['active','backgroundTasks','pendingDeliveries','input','tool']){
  const f=fixture(t);if(variant==='input')f.context.inputs=[{state:'unconfirmed'}];else if(variant==='tool')f.context.tasks=[{tools:{t:{status:'running'}}}];else f.runtime[variant]=1;
  await f.advise('compact');assert.equal((await f.manager.tick()).state,'waiting');assert.ok(!f.calls.includes('compact'));
 }
});
test('verified idle work can compact without releasing its task or work model',async t=>{
 const f=fixture(t);f.runtime.model='gpt-6-astra';f.context.tasks=[{id:'work',inputVersion:3,tools:{t:{status:'completed'}}}];
 await f.advise('compact');assert.equal((await f.manager.tick()).state,'complete');assert.equal(f.context.tasks[0].inputVersion,3);assert.equal(f.runtime.model,'gpt-6-astra');
});
test('new input while building a checkpoint postpones compaction without losing that input',async t=>{
 const f=fixture(t),prepare=f.manager.checkpoint;f.manager.checkpoint=async(...args)=>{const cp=await prepare(...args);f.context.cursors.input++;return cp;};
 await f.advise('compact');assert.equal((await f.manager.tick()).reason,'checkpoint-increments-pending');assert.ok(!f.calls.includes('compact'));
});
test('unconfirmed compaction is not replayed; a native completion settles it once',async t=>{
 const f=fixture(t);f.manager.compact=async()=>{f.calls.push('compact');throw Error('timeout');};await f.advise('compact');assert.equal((await f.manager.tick()).state,'unconfirmed');
 await f.manager.tick();assert.equal(f.calls.filter(c=>c==='compact').length,1);
 const event={id:'native-compact',operationId:f.manager.state.compactions[0].id,state:'completed',threadId:'old',at:new Date(f.clock.now+1).toISOString()};
 await f.manager.nativeCompaction(event);assert.equal((await f.manager.nativeCompaction(event)).state,'deduplicated');assert.equal(f.calls.filter(c=>c==='ack').length,1);
});
test('only post-compaction sourced degradation permits a verified handover',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();await f.advise('rotate',['failure']);const old=f.manager.fence();
 assert.equal((await f.manager.tick()).state,'complete');assert.equal(f.manager.fence().conversationId,old.conversationId);assert.equal(f.manager.fence().generation,2);assert.throws(()=>f.manager.assertFence(old),/STALE/);
 assert.deepEqual(f.calls.filter(c=>['create','inject','promote'].includes(c)),['create','inject','promote']);
});
test('source correction and ordinary latency never justify handover',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();f.manager.state.evidence.failure.needsReview=true;await f.advise('rotate',['failure']);assert.equal((await f.manager.tick()).state,'waiting');
 f.manager.state.evidence.failure.needsReview=false;f.manager.state.evidence.failure.kind='network-latency';assert.equal(rotationEligibility(f.manager.state,f.manager.state.advice,f.clock.now).eligible,false);
});
test('creation ambiguity survives restart and never opens a second candidate',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();await f.advise('rotate',['failure']);f.manager.createCandidate=async()=>{f.calls.push('create');throw Error('timeout');};
 assert.equal((await f.manager.tick()).state,'unconfirmed');const restored=new SessionManager(f.options);await restored.tick();assert.equal(f.calls.filter(c=>c==='create').length,1);
});
test('candidate model verification and input revision are checked again at promotion',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();await f.advise('rotate',['failure']);const verify=f.manager.verifyCandidate;
 f.manager.verifyCandidate=async args=>{const r=await verify(args);f.context.cursors.input++;return r;};
 assert.equal((await f.manager.tick()).reason,'checkpoint-increments-pending');assert.ok(!f.calls.includes('promote'));assert.equal(f.manager.fence().generation,1);
});
test('a second live host cannot take a lease',t=>{const f=fixture(t);f.manager.acquireLease();t.after(()=>f.manager.close());assert.throws(()=>new SessionManager(f.options).acquireLease(),/ALREADY_RUNNING/);});
test('a crash after binding commit rolls forward without creating or injecting again',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();await f.advise('rotate',['failure']);
 f.manager.promote=async()=>{throw Error('crash after durable commit');};assert.equal((await f.manager.tick()).state,'unconfirmed');
 const saved={users:{owner:{sessions:{main:{sessionId:'old'}}}}};assert.equal(recoverSessionStore(f.manager.view(),saved,'owner').users.owner.sessions.main.sessionId,'new');assert.equal(saved.users.owner.sessions.main.sessionId,'old');
 const recovered=new SessionManager(f.options);const receipt=await recovered.tick();assert.equal(receipt.recovered,true);assert.equal(recovered.fence().generation,2);assert.equal(f.calls.filter(c=>c==='create').length,1);assert.equal(f.calls.filter(c=>c==='inject').length,1);
});
test('configuration is sourced, versioned and idempotent; it cannot disable work checks',t=>{
 const f=fixture(t),request={id:'cfg-1',sourceId:'owner-1',reason:'Compression first',expectedRevision:0,changes:{rotate:false,preparePressure:0.7}};
 const first=f.manager.configure(request);assert.deepEqual(f.manager.configure(request),first);assert.equal(first.revision,1);
 assert.throws(()=>f.manager.configure({...request,id:'cfg-2'}),/revision changed/);
 assert.throws(()=>f.manager.configure({...request,id:'cfg-2',expectedRevision:1,changes:{pausedTaskHandover:true}}),/Unsupported/);
});
test('a transient queue failure retries the same observation instead of losing it',async t=>{
 const f=fixture(t),queue=f.manager.reviewRequested;let tries=0;f.manager.reviewRequested=async event=>{if(++tries===1)throw Error('queue busy');return queue(event);};
 await assert.rejects(f.manager.tick());await f.manager.tick();assert.equal(tries,2);assert.equal(f.calls.filter(c=>c==='review').length,1);
});
test('native receipt duplicates after a later window never reset the earlier ledger',async t=>{
 const f=fixture(t);const event=id=>({id,state:'completed',threadId:'old',at:new Date(f.clock.now++).toISOString()});const a=event('a'),b=event('b');
 await f.manager.nativeCompaction(a);await f.manager.nativeCompaction(b);await f.manager.nativeCompaction(a);assert.equal(f.calls.filter(c=>c==='ack').length,2);
});
test('native parser resumes complete JSONL lines and excludes reasoning from its state',async t=>{
 const f=fixture(t),file=path.join(f.dir,'rollout.jsonl'),stateFile=path.join(f.dir,'window.json');
 fs.writeFileSync(file,JSON.stringify({type:'session_meta',payload:{id:'old'}})+'\n'+JSON.stringify({type:'compacted',timestamp:'2026-09-15T01:00:00Z',payload:{window_id:'w1',message:'PRIVATE SUMMARY'}})+'\n'+JSON.stringify({type:'event_msg',timestamp:'now',payload:{type:'token_count',info:{model_context_window:100000,last_token_usage:{input_tokens:1234}}}})+'\n'+JSON.stringify({type:'response_item',payload:{type:'message',role:'assistant',content:[{type:'output_text',text:'marker-123'}]}}));
 const reader=new NativeWindow({file,stateFile,threadId:'old'});const result=await reader.poll();assert.equal(result.compactions,1);assert.equal(result.lastTokenUsage.inputTokens,1234);assert.ok(!JSON.stringify(result).includes('PRIVATE SUMMARY'));
 fs.appendFileSync(file,'\n');assert.equal((await new NativeWindow({file,stateFile,threadId:'old'}).poll()).compactions,1);assert.equal((await checkpointMarker(file,'marker-123')).found,true);
});
