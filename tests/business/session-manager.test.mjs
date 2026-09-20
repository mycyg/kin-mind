import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {SessionManager} from '../../adapters/session-manager.mjs';
import {SESSION_DEFAULTS,windowPressure,rotationEligibility} from '../../adapters/session-policy.mjs';
import {NativeWindow,checkpointMarker,nativePressureRuntime} from '../../adapters/native-window.mjs';
import {recoverSessionStore,loadCandidateSession,candidateConfigForRuntime,startMobileSessions} from '../../adapters/mobile-session-host.mjs';
import {sha256} from '../../adapters/instruction-evidence.mjs';

const providerBinding=profile=>({sourceProvider:profile.provider,sourceProviderKind:profile.providerKind,
 launchProvider:profile.providerKind==='gateway'?'kin_session_gateway':profile.provider,endpointSha256:profile.providerKind==='gateway'?'a'.repeat(64):null});
const verifiedProfileReceipt=(profile,binding=providerBinding(profile))=>({verified:true,requestedProfile:structuredClone(profile),providerBinding:structuredClone(binding),
 profileEvidence:{model:'verified',modelProvider:'verified',reasoningEffort:'verified',serviceTierConfiguration:'verified'},
 actualProfile:{model:profile.model,modelProvider:binding.launchProvider,reasoningEffort:profile.reasoningEffort,serviceTier:profile.serviceTierPreference==='fast'?'fast':null}});

function fixture(t){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-sessions-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const clock={now:20000000},runtime={known:true,threadId:'old',sessionId:'old',nativeSessionId:'old',model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',nativeStatus:'idle',modelContextWindow:100000,lastTokenUsage:{inputTokens:70000}};
 const context={cursors:{input:1,send:1},configVersion:'persona-v1',tasks:[],inputs:[]};const calls=[],candidateRequests=[];
 const config={...SESSION_DEFAULTS,prepare:true,rotate:true};
 const options={file:path.join(dir,'registry.json'),binding:{threadId:'old',nativeSessionId:'old',conversationId:'logical'},coordinator:{locked:f=>f()},inspect:async()=>({...runtime}),collect:async()=>structuredClone(context),now:()=>clock.now,config,lease:false,
  checkpoint:async(c,b)=>({id:'cp:'+c.cursors.input,conversationId:b.conversationId,generation:b.generation,configVersion:c.configVersion,cursors:c.cursors,complete:true,tokens:100,sourceRevisions:{u:1,a:1},items:[{id:'u',revision:1,role:'user',text:'蓝色机器人叫云朵。'},{id:'a',revision:1,role:'assistant',text:'你要云朵的方形贴纸吗？'}],pendingQuestions:[{sourceId:'a',target:'云朵的方形贴纸'}]}),
  compact:async id=>{calls.push('compact');runtime.lastTokenUsage.inputTokens=10000;return {completed:true,actual_session:'old',operationId:id,at:new Date(clock.now).toISOString()};},ackCompact:async()=>calls.push('ack'),
  createCandidate:async request=>{calls.push('create');candidateRequests.push(structuredClone(request));return {threadId:'new',nativeSessionId:'new',profile:structuredClone(request.profile),providerBinding:providerBinding(request.profile)};},injectCandidate:async()=>{calls.push('inject');return {verified:true};},verifyCandidate:async({checkpoint,profile,providerBinding:binding})=>({checkpointId:checkpoint.id,...verifiedProfileReceipt(profile,binding)}),
  promote:async()=>{calls.push('promote');return {verified:true,threadId:'new'};},reviewRequested:async()=>calls.push('review')};
 const manager=new SessionManager(options);
 const advise=async(action,evidenceIds=[])=>{await manager.observe(runtime,context);return manager.advise({action,reason:'Synthetic review',evidenceIds,compactionId:manager.state.compactions.at(-1)?.id},{model:'deepseek-flash',reasoning:'high'},manager.state.observation.id);};
 const degraded=()=>{clock.now+=2000000;manager.evidence({id:'failure',sourceId:'owner-correction',revision:1,kind:'reference-error',basis:'owner-statement',at:clock.now});};
 return {manager,options,clock,runtime,context,calls,candidateRequests,advise,degraded,dir};
}
test('pressure uses current input, verified window and output reserve; cumulative use is irrelevant',()=>{
 const p=windowPressure({modelContextWindow:100000,lastTokenUsage:{inputTokens:30000,totalTokens:999999999},totalTokenUsage:{totalTokens:1e12}});
 assert.equal(p.ratio,(30000+32768+8192)/100000);assert.equal(p.level,'elevated');assert.equal(windowPressure({}).known,false);
});
test('restart restores the last native measurement and recognizes a compaction context estimate',()=>{
 const runtime={modelContextWindow:null,lastTokenUsage:{inputTokens:0,outputTokens:0,totalTokens:0},totalTokenUsage:{totalTokens:1e12}};
 const history={modelContextWindow:100000,measuredAt:'2026-09-15T01:00:00Z',lastTokenUsage:{inputTokens:0,outputTokens:0,totalTokens:30056}};
 const value=nativePressureRuntime(runtime,history),pressure=windowPressure(value);
 assert.equal(pressure.expectedInputTokens,30056);assert.equal(value.usageEvidence.kind,'post-compaction-context-estimate');
 assert.equal(value.usageEvidence.measuredAt,history.measuredAt);
 const measured=nativePressureRuntime({...runtime,lastTokenUsage:{inputTokens:40000,outputTokens:1000,totalTokens:41000}},history);
 assert.equal(windowPressure(measured).expectedInputTokens,40000);assert.equal(measured.usageEvidence.source,'native-runtime');
});
test('candidate activation suppresses history replay and restores the verified mobile profile',async()=>{
 const calls=[],gateway={baseUrl:'http://synthetic.invalid',token:'synthetic-token',reasoningEffort:'high'};
 const actual={known:true,sessionId:'new',threadId:'new',nativeSessionId:'new',nativeStatus:'idle',model:'gpt-6-astra',modelProvider:'openai-15m',reasoningEffort:'medium',serviceTierPreference:'default',fastMode:'off',providerOverride:false};
 let suppressed=false;const client={beginSessionReplay(){suppressed=true;},async endSessionReplay(){suppressed=false;calls.push('drained');}};
 const connection={async loadSession(){assert.ok(suppressed);calls.push('load');},async extMethod(method){calls.push(method);if(method==='providers/set')Object.assign(actual,{providerOverride:true,providerBaseUrl:gateway.baseUrl,modelProvider:'custom-gateway'});return {...actual};},
   async setSessionConfigOption({configId,value}){actual[configId==='reasoning_effort'?'reasoningEffort':configId==='fast-mode'?'fastMode':configId]=value;return {configOptions:[{id:configId,value}]};}};
 const profile={provider:'custom-gateway',providerKind:'gateway',model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'},native={providerBinding:providerBinding(profile)};
 const candidate={profile,native,verification:verifiedProfileReceipt(profile,native.providerBinding)};
 const result=await loadCandidateSession({client,connection,binding:{threadId:'new',nativeSessionId:'new'},candidate,cwd:'/synthetic',gateway});
 assert.equal(result.actual.model,'deepseek-flash');assert.equal(result.actual.profileReady,true);assert.deepEqual(calls.slice(0,2),['load','drained']);assert.equal(suppressed,false);
 connection.loadSession=async()=>{throw Error('load failed');};
 await assert.rejects(loadCandidateSession({client,connection,binding:{threadId:'new'},candidate:{},cwd:'/synthetic',gateway}),/load failed/);assert.equal(suppressed,false);
});
test('maintenance candidate configuration uses the exact verified provider and Fast preference',()=>{
 const gateway={baseUrl:'http://synthetic.invalid',token:'token'};
 const sol=candidateConfigForRuntime({known:true,model:'gpt-5.6-sol',modelProvider:'openai-15m',providerOverride:false,reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on'},
   {catalogFile:'/catalog.json',gateway});
 assert.deepEqual(sol.profile,{provider:'openai-15m',providerKind:'native',model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'});
 assert.deepEqual([sol.modelProvider,sol.fastMode,sol.config.model_catalog_json],['openai-15m','on','/catalog.json']);
 assert.deepEqual(sol.providerBinding,{sourceProvider:'openai-15m',sourceProviderKind:'native',launchProvider:'openai-15m',endpointSha256:null});
 assert.equal(Object.keys(sol.config).some(key=>key.startsWith('model_providers.')),false,'a native provider is never rewritten as a gateway');
 const deepseek=candidateConfigForRuntime({model:'deepseek-flash',modelProvider:'private-ds',providerOverride:true,reasoningEffort:'high',fastMode:'off'},
   {catalogFile:'/catalog.json',gateway});
 assert.equal(deepseek.profile.provider,'private-ds');assert.equal(deepseek.modelProvider,'kin_session_gateway');assert.equal(deepseek.config['model_providers.kin_session_gateway'].base_url,gateway.baseUrl);
 assert.deepEqual([deepseek.providerBinding.sourceProvider,deepseek.providerBinding.launchProvider,typeof deepseek.providerBinding.endpointSha256],['private-ds','kin_session_gateway','string']);
 assert.throws(()=>candidateConfigForRuntime({model:'gpt-5.6-sol',reasoningEffort:'medium',fastMode:'on'},{catalogFile:'/catalog.json',gateway}),/unverified/);
});

test('maintenance candidates keep the verified companion base and developer layers across launch and promotion',async t=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-candidate-instructions-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const baseFile=path.join(dir,'base.md'),base='Verified base\n',developer='Verified developer\n';fs.writeFileSync(baseFile,base);
 const companionInstructions={enabled:true,allowedRoot:dir,modelInstructionsFile:baseFile,modelInstructionsSha256:sha256(base),
   developerInstructions:developer,developerInstructionsSha256:sha256(developer)};
 const profile={provider:'openai-15m',providerKind:'native',model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'default'};
 const launch=candidateConfigForRuntime(profile,{catalogFile:'/catalog.json',companionInstructions});
 assert.equal(launch.config.model_instructions_file,fs.realpathSync(baseFile));assert.equal(launch.developerInstructions,developer);
 assert.deepEqual(launch.instructionBinding,{modelInstructionsSha256:sha256(base),modelInstructionsUtf8Bytes:14,
   developerInstructionsSha256:sha256(developer),developerInstructionsUtf8Bytes:19});
 assert.equal(Object.hasOwn(launch.config,'developer_instructions'),false);
 assert.equal(Object.keys(launch.config).some(key=>key.includes('collaboration')),false);

 let loaded=0;const client={beginSessionReplay(){},async endSessionReplay(){}};
 const connection={async loadSession(){loaded++;},async extMethod(){return {known:true,sessionId:'new',threadId:'new',nativeSessionId:'new',active:false,backgroundTasks:0,nativeStatus:'idle',model:'gpt-5.6-sol',modelProvider:'openai-15m',reasoningEffort:'medium',serviceTierPreference:'default',fastMode:'off',providerOverride:false};},
   async setSessionConfigOption(){return {configOptions:[]};}};
 const candidate={profile,native:{providerBinding:launch.providerBinding,instructionBinding:launch.instructionBinding},
   verification:verifiedProfileReceipt(profile,launch.providerBinding)};
 await loadCandidateSession({client,connection,binding:{threadId:'new',nativeSessionId:'new'},candidate,cwd:'/synthetic',instructionBinding:launch.instructionBinding});
 assert.equal(loaded,1);
 await assert.rejects(loadCandidateSession({client,connection,binding:{threadId:'new'},candidate,cwd:'/synthetic',instructionBinding:{...launch.instructionBinding,modelInstructionsSha256:'0'.repeat(64)}}),/instruction binding changed/);
 assert.equal(loaded,1,'a changed instruction binding is rejected before main ACP load');

 fs.writeFileSync(baseFile,'changed\n');
 assert.throws(()=>candidateConfigForRuntime(profile,{catalogFile:'/catalog.json',companionInstructions}),/hash mismatch/);
 assert.throws(()=>candidateConfigForRuntime(profile,{catalogFile:'/catalog.json',companionInstructions:{enabled:true}}),/incomplete/);
});

test('enabled companion maintenance fails before touching the live router when its instruction contract is incomplete',async()=>{
 let touched=false;const bridge={get mobileRouting(){touched=true;throw Error('router must not be touched');}};
 await assert.rejects(startMobileSessions({bridge,root:'/synthetic',config:{adaptive_sessions:true,companion_instructions:{enabled:true}},
   mindCall:async()=>{}}),/configuration is incomplete/);
 assert.equal(touched,false);
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
 assert.deepEqual(f.candidateRequests[0].profile,{provider:'custom-gateway',providerKind:'gateway',model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'});
});
test('a Sol Fast candidate keeps its provider, and provider drift blocks promotion',async t=>{
 const f=fixture(t);Object.assign(f.runtime,{model:'gpt-5.6-sol',modelProvider:'openai-15m',providerOverride:false,reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on'});
 await f.advise('compact');await f.manager.tick();f.degraded();await f.advise('rotate',['failure']);
 const verify=f.manager.verifyCandidate;f.manager.verifyCandidate=async args=>{const receipt=await verify(args);f.runtime.modelProvider='different-native-provider';return receipt;};
 const result=await f.manager.tick();assert.deepEqual([result.state,result.reason],['waiting','candidate-profile-changed']);assert.ok(!f.calls.includes('promote'));
 assert.deepEqual(f.candidateRequests[0].profile,{provider:'openai-15m',providerKind:'native',model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'});
});
test('source correction and ordinary latency never justify handover',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();f.manager.state.evidence.failure.needsReview=true;await f.advise('rotate',['failure']);assert.equal((await f.manager.tick()).state,'waiting');
 f.manager.state.evidence.failure.needsReview=false;f.manager.state.evidence.failure.kind='network-latency';assert.equal(rotationEligibility(f.manager.state,f.manager.state.advice,f.clock.now).eligible,false);
});
test('creation ambiguity survives restart and never opens a second candidate',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();await f.advise('rotate',['failure']);f.manager.createCandidate=async()=>{f.calls.push('create');throw Error('timeout');};
 assert.equal((await f.manager.tick()).state,'unconfirmed');const restored=new SessionManager(f.options);await restored.tick();assert.equal(f.calls.filter(c=>c==='create').length,1);
});
test('legacy candidates without profile evidence are retired only before binding commit',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();f.degraded();await f.advise('rotate',['failure']);
 f.manager.state.candidate={id:'legacy-ready',state:'ready',generation:1,native:{threadId:'legacy',nativeSessionId:'legacy'}};f.manager.save('synthetic-legacy-ready');
 const restored=new SessionManager(f.options);assert.equal(restored.state.candidate.state,'stale');assert.equal(restored.state.retiredCandidates.at(-1).staleReason,'legacy-profile-evidence-missing');
 assert.equal((await restored.tick()).state,'complete');assert.equal(f.calls.filter(c=>c==='create').length,1);assert.equal(f.candidateRequests[0].profile.provider,'custom-gateway');

 const committed=fixture(t),old=committed.manager.fence();Object.assign(committed.manager.state,{binding:{...old,generation:2,threadId:'legacy-new',nativeSessionId:'legacy-new'},
  candidate:{id:'legacy-committed',state:'unconfirmed',generation:1,native:{threadId:'legacy-new',nativeSessionId:'legacy-new'},previousBinding:old}});committed.manager.save('synthetic-legacy-committed');
 const conservative=new SessionManager(committed.options),result=await conservative.tick();assert.deepEqual([result.state,result.reason],['waiting','candidate-profile-unverified']);
 assert.equal(conservative.state.candidate.id,'legacy-committed');assert.equal(conservative.state.retiredCandidates,undefined);assert.ok(!committed.calls.includes('create'));
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
test('a damaged registry falls back to the revision it kept; with nothing left it refuses to open',async t=>{
 const f=fixture(t);await f.advise('compact');await f.manager.tick();
 const file=f.options.file,binding=f.manager.fence(),quarantine=path.join(f.dir,'quarantine');
 assert.equal(fs.existsSync(file+'.prev'),true);
 fs.writeFileSync(file,'{"schema":1,"binding"');
 const restored=new SessionManager(f.options);
 assert.deepEqual(restored.fence(),binding,'the binding survives');
 assert.equal(restored.state.recovery.reason,'restored-from-previous-revision');
 assert.equal(restored.assertFence(binding),true);
 assert.deepEqual(fs.readdirSync(quarantine).length,1);
 // A binding is a fencing token: it can never be reopened at generation one, because an
 // older fence stored elsewhere would pass again. The bytes stay for the operator.
 fs.writeFileSync(file,'{');fs.writeFileSync(file+'.prev','[');
 assert.throws(()=>new SessionManager(f.options),/KIN_SESSION_REGISTRY_UNREADABLE/);
 assert.equal(fs.readdirSync(quarantine).length,3);
 assert.throws(()=>restored.assertFence(binding),/KIN_SESSION_REGISTRY_UNREADABLE/);
 assert.equal(new SessionManager({...f.options,file:path.join(f.dir,'never-written.json')}).state.recovery,undefined,'a missing registry is still a fresh start');
});

test('native parser resumes complete JSONL lines and excludes reasoning from its state',async t=>{
 const f=fixture(t),file=path.join(f.dir,'rollout.jsonl'),stateFile=path.join(f.dir,'window.json');
 fs.writeFileSync(file,JSON.stringify({type:'session_meta',payload:{id:'old'}})+'\n'+JSON.stringify({type:'compacted',timestamp:'2026-09-15T01:00:00Z',payload:{window_id:'w1',message:'PRIVATE SUMMARY'}})+'\n'+JSON.stringify({type:'event_msg',timestamp:'now',payload:{type:'token_count',info:{model_context_window:100000,last_token_usage:{input_tokens:1234}}}})+'\n'+JSON.stringify({type:'response_item',payload:{type:'message',role:'assistant',content:[{type:'output_text',text:'marker-123'}]}}));
 const reader=new NativeWindow({file,stateFile,threadId:'old'});const result=await reader.poll();assert.equal(result.compactions,1);assert.equal(result.lastTokenUsage.inputTokens,1234);assert.ok(!JSON.stringify(result).includes('PRIVATE SUMMARY'));
 fs.appendFileSync(file,'\n');assert.equal((await new NativeWindow({file,stateFile,threadId:'old'}).poll()).compactions,1);assert.equal((await checkpointMarker(file,'marker-123')).found,true);
});
