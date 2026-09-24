import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {SessionManager,commitRegistryMigration} from '../../adapters/session-manager.mjs';
import {SESSION_DEFAULTS,windowPressure,rotationEligibility} from '../../adapters/session-policy.mjs';
import {NativeWindow,checkpointMarker,nativePressureRuntime} from '../../adapters/native-window.mjs';
import {recoverSessionStore,restoreInjection,loadCandidateSession,candidateConfigForRuntime,startMobileSessions,sessionReviewCursors} from '../../adapters/mobile-session-host.mjs';
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
  promote:async()=>{calls.push('promote');return {verified:true,threadId:'new'};},
  // The mind answers with the job it holds for the attempt it was asked about.
  reviewRequested:async request=>{calls.push('review');return {id:'job:'+request.id,state:'pending',requestId:request.id,snapshotId:request.cause};}};
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
test('sixty assessment completions do not renew their own review, including after restart',async t=>{
 const f=fixture(t),inputs=[{id:'owner',kind:'owner',state:'accepted',hash:'owner-text'}];
 f.context.reviewCursors=sessionReviewCursors(f.context.cursors,inputs);
 await f.manager.tick();const snapshot=f.manager.state.observation.id;
 for(let i=0;i<60;i++){
  inputs.push({id:'assessment-'+i,kind:'assessment',state:'accepted',hash:String(i)});
  f.context.cursors.inputs='full-execution-cursor-'+i;
  f.context.reviewCursors=sessionReviewCursors(f.context.cursors,inputs);
  f.runtime.lastTokenUsage.inputTokens++;
  f.runtime.serviceTier=i%2?'priority':null;
  await f.manager.tick();
  assert.equal(f.manager.state.observation.id,snapshot);
 }
 assert.equal(f.manager.state.observation.pressure.inputTokens,70060,'exact live pressure stays available');
 assert.equal(f.calls.filter(x=>x==='review').length,1);
 assert.equal(f.manager.advise({action:'defer',reason:'Wait for new evidence',evidenceIds:[]},
  {model:'deepseek-flash',reasoning:'high'},snapshot).state,'recorded');
 const restarted=new SessionManager(f.options);assert.equal((await restarted.tick()).state,'defer');
 assert.equal(f.calls.filter(x=>x==='review').length,1);
});
test('tasks, corrected sources, the profile, configuration, pressure and capacity ask a new review',async t=>{
 for(const change of ['task','correction','memory-correction','profile','configuration','pressure','capacity']){
  const f=fixture(t),inputs=[{id:'owner',kind:'owner',state:'accepted',hash:'before'}];
  f.context.reviewCursors=sessionReviewCursors(f.context.cursors,inputs);
  f.runtime.lastTokenUsage.inputTokens=30000;
  await f.advise('keep');const snapshot=f.manager.state.observation.id;
  if(change==='task')f.context.tasks=[{id:'work',inputVersion:2,status:'running'}];
  if(change==='correction')f.context.invalidatedSources=['owner-message-with-corrected-source'];
  if(change==='memory-correction')f.context.linked={items:[],needs_review_ids:['concern-with-corrected-source']};
  if(change==='profile')f.runtime.serviceTierPreference='fast';
  if(change==='configuration')f.context.configVersion='persona-v2';
  if(change==='pressure')f.runtime.lastTokenUsage.inputTokens=70000;
  if(change==='capacity')f.runtime.modelContextWindow=105000;
  assert.equal((await f.manager.tick()).state,'observing',change);
  assert.notEqual(f.manager.state.observation.id,snapshot,change);
  assert.equal(f.calls.filter(x=>x==='review').length,2,change);
  await f.manager.tick();assert.equal(f.calls.filter(x=>x==='review').length,2,change);
 }
});
test('new dialogue, revised memory entries and a finished compaction leave the judgment in force',async t=>{
 // AD1-13, K3-06: each of these used to open another main-session review at high
 // pressure, and every review filled the window towards the next native compaction.
 for(const change of ['owner','memory','dialogue','compaction']){
  const f=fixture(t),inputs=[{id:'owner',kind:'owner',state:'accepted',hash:'before'}];
  f.context.reviewCursors=sessionReviewCursors(f.context.cursors,inputs);
  f.runtime.lastTokenUsage.inputTokens=30000;
  await f.advise('keep');const snapshot=f.manager.state.observation.id;
  if(change==='owner'){inputs.push({id:'owner-2',kind:'owner',state:'accepted',hash:'next'});f.context.reviewCursors=sessionReviewCursors(f.context.cursors,inputs);}
  if(change==='memory')f.context.reviewCursors.linked='concern-updated-by-an-appraisal';
  if(change==='dialogue')f.context.items=[{id:'owner-2',role:'user',at:'2026-09-24T01:00:00Z',revision:'r2',text:'刚到家。'}];
  if(change==='compaction')await f.manager.nativeCompaction({id:'native-1',state:'completed',threadId:'old',at:new Date(f.clock.now).toISOString()});
  assert.equal((await f.manager.tick()).state,'keep',change);
  assert.equal(f.manager.state.observation.id,snapshot,change);
  assert.equal(f.calls.filter(x=>x==='review').length,1,change);
 }
});
test('current-session native advice supports each selected model without switching it',async t=>{
 for(const [model,provider,reasoning] of [['gpt-6-sol','openai-15m','medium'],['gpt-6-astra','openai-15m','high'],['deepseek-flash','custom-gateway','high']]){
  const f=fixture(t);Object.assign(f.runtime,{model,modelProvider:provider,reasoningEffort:reasoning,serviceTierPreference:'fast'});
  await f.manager.observe(f.runtime,f.context);
  const native={model,provider,reasoning,native_session_id:'old',generation:1,native_turn_id:'final-one',verified_at:'2026-09-24T00:00:00Z'};
  const receipt={model,reasoning,request_id:'final-one',native_receipt:native};
  const advice={action:'keep',reason:'Current model completed the judgment',evidenceIds:[]};
  // AD1-19: the fields alone no longer suffice; the turn must be one the host saw complete.
  assert.throws(()=>f.manager.advise(advice,receipt,f.manager.state.observation.id,f.runtime),/no host assessment record/);
  f.manager.recordAssessment({requestId:'appraisal:1',turnId:'final-one',forkThreadId:'fork-one'});
  assert.equal(f.manager.advise(advice,receipt,f.manager.state.observation.id,f.runtime).state,'recorded');
  assert.equal(f.manager.state.sessionAdvice.turnId,'final-one');
  assert.equal(f.manager.state.events[f.manager.state.observation.id].answer.turnId,'final-one');
  for(const bad of [{native_session_id:'other'},{generation:2},{model:'other'},{provider:'other'},{reasoning:'low'},{native_turn_id:null},{verified_at:null}]){
   assert.throws(()=>f.manager.advise(advice,{...receipt,native_receipt:{...native,...bad}},f.manager.state.observation.id,f.runtime),/native receipt unverified/);
  }
  assert.throws(()=>f.manager.advise(advice,{...receipt,request_id:'late-other-turn'},f.manager.state.observation.id,f.runtime),/native receipt unverified/);
  assert.equal((await f.manager.tick()).state,'keep');assert.equal(f.runtime.model,model);
 }
});
test('review cursor exclusions never remove unsettled assessments from the execution boundary',async t=>{
 const f=fixture(t),inputs=[{id:'internal',kind:'assessment',state:'unconfirmed',hash:'pending'}];
 f.context.inputs=inputs;f.context.reviewCursors=sessionReviewCursors(f.context.cursors,inputs);
 await f.advise('compact');assert.equal((await f.manager.tick()).reason,'input-awaiting-dispatch-or-reconciliation');
 assert.ok(!f.calls.includes('compact'));assert.equal(f.context.inputs[0].state,'unconfirmed');
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
 f.manager.state.evidence.failure.needsReview=false;f.manager.state.evidence.failure.kind='network-latency';assert.equal(rotationEligibility(f.manager.state,f.manager.state.sessionAdvice,f.clock.now).eligible,false);
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

// --- Session maintenance after the review, lease and storage fixes (WS5) ---
const critical=f=>{f.runtime.lastTokenUsage.inputTokens=95000;f.runtime.model='gpt-6-sol';f.runtime.modelProvider='openai-15m';f.runtime.providerOverride=false;f.runtime.reasoningEffort='medium';};
const minutes=(f,n)=>{f.clock.now+=n*60000;};
const registryRevision=f=>JSON.parse(fs.readFileSync(f.options.file,'utf8')).revision;

test('sustained critical pressure asks one review, not one per native compaction (AD1-13)',async t=>{
 const f=fixture(t);critical(f);await f.manager.tick();
 for(let i=0;i<8;i++){minutes(f,7.5);await f.manager.nativeCompaction({id:'native-'+i,state:'completed',threadId:'old',at:new Date(f.clock.now).toISOString(),origin:'native-history'});await f.manager.tick();}
 assert.equal(f.calls.filter(c=>c==='review').length,1);
 assert.equal(f.manager.state.observation.pressure.level,'critical');
 assert.equal(Object.keys(f.manager.state.events).length,1);
});
test('a cooldown records when compaction may run, and an incomplete checkpoint is not rebuilt every minute',async t=>{
 const f=fixture(t);critical(f);let builds=0;
 f.manager.checkpoint=async()=>{builds++;return {complete:false};};
 await f.manager.nativeCompaction({id:'native-0',state:'completed',threadId:'old',at:new Date(f.clock.now).toISOString()});
 await f.advise('compact');
 const cooling=await f.manager.tick();
 assert.deepEqual([cooling.state,cooling.reason],['waiting','observe-after-compaction']);
 assert.equal(cooling.nextAt,f.clock.now+SESSION_DEFAULTS.compactCooldownMs,'the cooldown is a time, not a keep');
 assert.equal(f.manager.state.sessionAdvice.action,'compact','the judgment is still in force');
 const reasons=new Set();
 for(let i=0;i<60;i++){minutes(f,1);reasons.add((await f.manager.tick()).reason);}
 assert.ok(builds>=1&&builds<=3,'built '+builds+' times in 60 minutes');
 assert.ok(!f.calls.includes('compact'));assert.ok(reasons.has('checkpoint-coverage-incomplete'));
 assert.equal(f.calls.filter(c=>c==='review').length,1);
});
test('a compaction that never answers frees the coordinator and records why (N1-03)',async t=>{
 const f=fixture(t);critical(f);let held=false,checks=0;
 f.manager.coordinator={locked:async fn=>{assert.equal(held,false,'coordinator re-entered while held');held=true;try{return await fn();}finally{held=false;}}};
 f.manager.limits={...f.manager.limits,compactTimeoutMs:30,compactCheckMs:60000,compactQuietMs:3600000};
 f.manager.compact=async()=>{f.calls.push('compact');return new Promise(()=>{});};
 f.manager.reconcileCompact=async()=>{checks++;return {completed:false,operationId:'x'};};
 await f.advise('compact');
 const result=await f.manager.tick();
 assert.deepEqual([result.state,result.reason],['unconfirmed','compaction-timeout']);assert.equal(held,false);
 const operation=f.manager.state.compactions.at(-1);
 assert.equal(operation.state,'unconfirmed');assert.equal(operation.failure.reason,'host-timeout');
 assert.equal(JSON.parse(fs.readFileSync(f.options.file,'utf8')).compactions.at(-1).failure.reason,'host-timeout','the failure receipt is durable');
 assert.equal((await f.manager.tick()).reason,'compaction-receipt-unconfirmed');assert.equal(checks,1);
 assert.equal((await f.manager.tick()).reason,'compaction-receipt-unconfirmed');assert.equal(checks,1,'status checks are spaced');
 // The runtime's own status settles it: a failure frees the next attempt, under a new ID.
 minutes(f,6);f.manager.reconcileCompact=async()=>({completed:false,failed:true});
 f.manager.compact=async id=>{f.calls.push('compact');f.runtime.lastTokenUsage.inputTokens=10000;return {completed:true,actual_session:'old',operationId:id};};
 const retried=await f.manager.tick();
 assert.equal(retried.state,'complete');assert.equal(f.calls.filter(c=>c==='compact').length,2);
 assert.equal(f.manager.state.compactions.at(-2).state,'failed');assert.notEqual(f.manager.state.compactions.at(-1).id,operation.id);
});
test('an unconfirmed compaction with no native completion for an hour is settled as not run',async t=>{
 const f=fixture(t);critical(f);
 f.manager.compact=async()=>{f.calls.push('compact');throw Error('connection closed');};
 await f.advise('compact');assert.equal((await f.manager.tick()).state,'unconfirmed');
 minutes(f,30);assert.equal((await f.manager.tick()).reason,'compaction-receipt-unconfirmed');
 minutes(f,31);f.manager.compact=async id=>{f.calls.push('compact');return {completed:true,actual_session:'old',operationId:id};};
 assert.equal((await f.manager.tick()).state,'complete');
 assert.equal(f.manager.state.compactions.at(-2).failure.reason,'no-native-completion');
});
test('an unanswered review is asked again after a wait, and failed requests back off',async t=>{
 const f=fixture(t);critical(f);f.manager.limits={...f.manager.limits,reviewTimeoutMs:3600000};
 await f.manager.tick();assert.equal(f.calls.filter(c=>c==='review').length,1);
 minutes(f,59);await f.manager.tick();assert.equal(f.calls.filter(c=>c==='review').length,1);
 minutes(f,2);await f.manager.tick();assert.equal(f.calls.filter(c=>c==='review').length,1,'the wait starts when the answer is overdue');
 const event=Object.values(f.manager.state.events)[0];assert.equal(event.state,'pending');assert.equal(event.unanswered,1);
 minutes(f,31);await f.manager.tick();assert.equal(f.calls.filter(c=>c==='review').length,2);
 let tries=0;f.manager.reviewRequested=async()=>{tries++;throw Error('worker unavailable');};
 f.context.tasks=[{id:'work',inputVersion:1,status:'running'}];
 for(let i=0;i<10;i++){minutes(f,1);await assert.rejects(f.manager.tick(),/worker unavailable/).catch(()=>{});}
 assert.ok(tries>=3&&tries<=5,'tried '+tries+' times in ten minutes');
});
test('the same observation again is answered already; it is asked again only after a growing wait',async t=>{
 const f=fixture(t);f.runtime.lastTokenUsage.inputTokens=30000;
 await f.advise('keep');const elevated=f.manager.state.observation.id;
 f.runtime.lastTokenUsage.inputTokens=70000;await f.advise('keep');assert.notEqual(f.manager.state.observation.id,elevated);
 f.runtime.lastTokenUsage.inputTokens=30000;minutes(f,5);await f.manager.tick();
 assert.equal(f.manager.state.observation.id,elevated);assert.equal(f.calls.filter(c=>c==='review').length,2,'deduplicated');
 minutes(f,30);await f.manager.tick();assert.equal(f.calls.filter(c=>c==='review').length,3,'asked again after the wait');
 assert.equal(f.manager.state.events[elevated].attempts,2);
});
test('idle minutes write nothing; the registry and its log change only with their content (AD1-14)',async t=>{
 const f=fixture(t);f.runtime.lastTokenUsage.inputTokens=30000;await f.advise('keep');await f.manager.tick();
 const revision=registryRevision(f),log=fs.readFileSync(f.options.file+'.events.jsonl','utf8').split('\n').length;
 for(let i=0;i<60;i++){minutes(f,1);f.runtime.lastTokenUsage.inputTokens++;assert.equal((await f.manager.tick()).state,'keep');}
 assert.equal(registryRevision(f),revision);assert.equal(fs.readFileSync(f.options.file+'.events.jsonl','utf8').split('\n').length,log);
 assert.equal(f.manager.state.observation.pressure.inputTokens,30060,'exact pressure stays current in memory');
});
test('review events settle and are pruned; legacy queued events and the old advice field are migrated (AD1-14, K3-11)',async t=>{
 const f=fixture(t);
 const legacy=JSON.parse(fs.readFileSync(f.options.file,'utf8'));
 legacy.events=Object.fromEntries(Array.from({length:900},(_,i)=>['refresh:'+i,{state:'queued',observationId:'o'+i}]));
 legacy.advice={action:'keep',reason:'old',evidenceIds:[],snapshotId:'old-observation',generation:1};
 fs.writeFileSync(f.options.file,JSON.stringify(legacy));
 const opened=new SessionManager(f.options);
 assert.equal(Object.keys(opened.state.events).length,0);assert.equal(opened.state.sessionAdvice.reason,'old');assert.equal(Object.hasOwn(opened.state,'advice'),false);
 for(let i=0;i<80;i++)opened.state.events['cause-'+i]={cause:'cause-'+i,state:'answered',attempts:1,at:f.clock.now-i,settledAt:f.clock.now-i};
 opened.state.events.open={cause:'open',state:'queued',attempts:1,at:0,queuedAt:0};
 opened.prune();
 assert.equal(Object.keys(opened.state.events).length,64);assert.ok(opened.state.events.open,'an open question is never dropped');
 assert.ok(opened.state.events['cause-0']&&!opened.state.events['cause-79'],'the oldest settled go first');
 minutes(f,8*24*60);opened.prune();assert.deepEqual(Object.keys(opened.state.events),['open']);
});
test('pruned compaction records leave a floor, so old native events are not taken for new ones',async t=>{
 const f=fixture(t),events=Array.from({length:60},(_,i)=>({id:'w'+i,state:'completed',threadId:'old',at:new Date(f.clock.now+i*1000).toISOString()}));
 for(const event of events)await f.manager.nativeCompaction(event);
 assert.equal(f.manager.state.compactions.length,50);assert.equal(f.calls.filter(c=>c==='ack').length,60);
 for(const event of events)if(!f.manager.nativeSeen(event))await f.manager.nativeCompaction(event);
 assert.equal(f.calls.filter(c=>c==='ack').length,60,'no event is acknowledged twice');
 const next={id:'w60',state:'completed',threadId:'old',at:new Date(f.clock.now+60000).toISOString()};
 assert.equal(f.manager.nativeSeen(next),false);
});
test('a judgment arrives with the snapshot, once, and a refused one waits for its own review (DB1-03)',async t=>{
 const f=fixture(t);f.runtime.lastTokenUsage.inputTokens=30000;await f.manager.tick();
 const snapshotId=f.manager.state.observation.id,record=(eventId,decision)=>({eventId,snapshotId,generation:1,decision,receipt:{model:'deepseek-flash',reasoning:'high'}});
 f.context.sessionAdvice=record('mind-event-1',{action:'defer',reason:'Wait for the next exchange',evidenceIds:[]});
 assert.equal((await f.manager.tick()).state,'defer');
 const stored=JSON.parse(fs.readFileSync(f.options.file,'utf8'));
 assert.equal(stored.sessionAdvice.action,'defer');assert.equal(stored.lastAdviceEvent,'mind-event-1');assert.equal(stored.events[snapshotId].state,'answered');
 f.context.sessionAdvice=record('mind-event-2',{action:'rotate',reason:'',evidenceIds:[]});
 assert.equal((await f.manager.tick()).state,'defer','an invalid judgment does not replace the one in force');
 assert.equal(f.manager.state.lastAdviceEvent,'mind-event-2');
});
test('configured values that differ from the registry are recorded, and the registry decides (AD1-21)',async t=>{
 const f=fixture(t);f.manager.close();
 const reopened=new SessionManager({...f.options,config:{...f.options.config,compactCooldownMs:900000,rotate:false}});
 assert.deepEqual(reopened.state.configDrift,{keys:['compactCooldownMs','rotate'],configured:{compactCooldownMs:900000,rotate:false},registry:{compactCooldownMs:SESSION_DEFAULTS.compactCooldownMs,rotate:true}});
 assert.equal(reopened.state.config.rotate,true);
 const same=new SessionManager(f.options);assert.equal(same.state.configDrift,undefined);
});
test('a full window still compacts and hands over with a fixed-budget checkpoint (T-22)',async t=>{
 const f=fixture(t);f.runtime.lastTokenUsage.inputTokens=99000;
 await f.advise('compact');assert.equal((await f.manager.tick()).state,'complete');
 f.runtime.lastTokenUsage.inputTokens=99000;f.degraded();await f.advise('rotate',['failure']);
 assert.equal((await f.manager.tick()).state,'complete');
 assert.deepEqual(f.calls.filter(c=>['compact','create','inject','promote'].includes(c)),['compact','create','inject','promote']);
 assert.equal(f.manager.fence().generation,2);
});
test('the minute tick reads its judgment from the snapshot, builds fixed-budget checkpoints and writes only changes',async t=>{
 // K3-04: no separate `read` per minute. K3-05: no window room in any checkpoint request.
 // DB1-03: the judgment lands in the registry. AD1-19: its turn is one the host saw complete.
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-session-host-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 fs.mkdirSync(path.join(root,'conversation'),{recursive:true});fs.mkdirSync(path.join(root,'state'),{recursive:true});
 fs.writeFileSync(path.join(root,'conversation/AGENTS.md'),'Synthetic persona\n');
 const runtime={known:true,threadId:'main',sessionId:'main',nativeSessionId:'main',nativeStatus:'idle',active:false,backgroundTasks:0,model:'gpt-6-sol',modelProvider:'openai-15m',providerOverride:false,
  reasoningEffort:'medium',serviceTierPreference:'default',fastMode:'off',modelContextWindow:100000,lastTokenUsage:{inputTokens:98000}};
 const calls=[],status=[];let advice=null;
 const snapshot=()=>({configVersion:'persona-v1',cursors:{public:'p'},items:[],invalidatedSources:[],sourceRevisions:{},scope:{},shared:{},manifestVersion:'continuity-manifest-v1',...(advice?{sessionAdvice:advice}:{})});
 const mindCall=async(action,request)=>{calls.push({action,request});
  if(action==='session-snapshot')return snapshot();
  if(action==='session-checkpoint')return {id:'cp',complete:true,tokens:100,budgetPlan:{reason:'recent-dialogue',requested:request.budget,effective:request.budget,limit:8000}};
  if(action==='session-review')return {id:'job:'+request.id,state:'pending',requestId:request.id,snapshotId:request.snapshotId};
  throw Error('unexpected mind call '+action);};
 const router={state:{},snapshot:()=>({inputs:{},notices:{},configRevision:0}),tasks:()=>[],restoreRoutingProfile:async()=>{},save(){},locked:async fn=>fn()};
 const bridge={ownerId:'owner',sessionManager:{getSession:()=>({processing:false,queue:[]})},
  mobileRouting:{router,gateway:{token:'synthetic'},inspect:async()=>({...runtime}),ensureSession:async()=>({agentInfo:{connection:{extMethod:async()=>({})}}})},
  runMainAssessment:async request=>({state:'complete',result:{},receipt:{native_turn_id:'turn-'+request.id,model:'gpt-6-sol'}})};
 const config={adaptive_sessions:true,main_session_review:true,companion_instructions:{enabled:false},session_management:{observe:true,compact:false,prepare:false,rotate:false}};
 const api=await startMobileSessions({bridge,root,config,mindCall,recordStatus:value=>{if(value.sessionManagement)status.push(value.sessionManagement);}});
 t.after(()=>api.close());
 for(let i=0;i<100&&!status.length;i++)await new Promise(resolve=>setTimeout(resolve,10));
 const observationFile=path.join(root,'state/session-observation.json'),registry=path.join(root,'state/conversation-registry.json');
 assert.equal(api.manager.state.observation.pressure.level,'critical');
 assert.ok(calls.some(c=>c.action==='session-checkpoint'),'a rolling checkpoint was prepared');
 for(const call of calls.filter(c=>c.action==='session-checkpoint'))assert.equal(Object.hasOwn(call.request,'native_capacity'),false);
 assert.equal(calls.filter(c=>c.action==='session-review').length,1);
 const written=fs.statSync(observationFile).ino,revision=JSON.parse(fs.readFileSync(registry,'utf8')).revision;
 for(let i=0;i<5;i++)await api.tick();
 assert.equal(fs.statSync(observationFile).ino,written,'an unchanged observation is not written again');
 assert.equal(JSON.parse(fs.readFileSync(registry,'utf8')).revision,revision,'an idle minute writes no registry revision');
 await bridge.runMainAssessment({id:'appraisal:1'});
 advice={eventId:'mind-event-1',snapshotId:api.manager.state.observation.id,generation:1,decision:{action:'defer',reason:'Let the window settle',evidenceIds:[]},
  receipt:{model:'gpt-6-sol',reasoning:'medium',request_id:'turn-appraisal:1',native_receipt:{model:'gpt-6-sol',provider:'openai-15m',reasoning:'medium',native_session_id:'main',generation:1,native_turn_id:'turn-appraisal:1',verified_at:'2026-09-24T00:00:00Z'}}};
 await api.tick();
 const stored=JSON.parse(fs.readFileSync(registry,'utf8'));
 assert.equal(stored.sessionAdvice.action,'defer');assert.equal(stored.sessionAdvice.turnId,'turn-appraisal:1');
 assert.equal(stored.assessments.at(-1).requestId,'appraisal:1');assert.equal(status.at(-1).status.state,'defer');
 assert.equal(calls.filter(c=>c.action==='read').length,0);
});

// SPEC-v2 §1, step 8: the migration's single commit, made while the host is stopped.
function migrationRegistry(t,generation){
 const f=fixture(t);f.manager.close();
 const stored=JSON.parse(fs.readFileSync(f.options.file,'utf8'));
 stored.binding.generation=generation;stored.segments.at(-1).generation=generation;
 stored.sessionAdvice={action:'defer',generation};stored.rollingCheckpoint={id:'rolling'};stored.rollingCursor='cursor';
 fs.writeFileSync(f.options.file,JSON.stringify(stored));
 const expected={conversationId:'logical',generation,threadId:'old',nativeSessionId:'old'};
 const checkpoint={id:'checkpoint:migration',payload:{kind:'internal-continuity-checkpoint'},items:[]};
 const request={id:'kin-home:1',expected,native:{threadId:'kin-home-thread',nativeSessionId:'kin-home-thread'},codexHome:path.join(f.dir,'codex-home'),homeHash:'h'.repeat(64),checkpoint};
 return {...f,expected,request,read:()=>JSON.parse(fs.readFileSync(f.options.file,'utf8'))};
}
test('the Kin home migration moves the binding one generation past the one the registry holds, once',t=>{
 const f=migrationRegistry(t,4),before=fs.readFileSync(f.options.file,'utf8');
 // A live host holds the lease: the commit is refused and nothing is written.
 const host=new SessionManager({...f.options,config:null,lease:true});
 assert.throws(()=>commitRegistryMigration(f.options.file,f.request),/KIN_SESSION_MANAGER_ALREADY_RUNNING/);
 host.close();
 assert.throws(()=>commitRegistryMigration(f.options.file,{...f.request,expected:{...f.expected,generation:3}}),/KIN_MIGRATION_BINDING_CHANGED/);
 assert.throws(()=>commitRegistryMigration(f.options.file,{...f.request,native:{threadId:'old',nativeSessionId:'old'}}),/KIN_MIGRATION_BINDING_CHANGED|KIN_MIGRATION_THREAD_REUSED/);
 assert.equal(JSON.parse(fs.readFileSync(f.options.file,'utf8')).binding.generation,4,'a refused commit changes no binding');
 assert.equal(fs.existsSync(f.options.file+'.lease'),false,'a refused commit leaves no lease behind');
 const receipt=commitRegistryMigration(f.options.file,f.request);
 assert.deepEqual([receipt.state,receipt.previous.generation,receipt.binding.generation,receipt.binding.threadId],['committed',4,5,'kin-home-thread']);
 const stored=f.read();
 assert.deepEqual(stored.binding,{conversationId:'logical',generation:5,threadId:'kin-home-thread',nativeSessionId:'kin-home-thread'});
 assert.deepEqual(stored.segments.map(s=>[s.generation,s.threadId,s.state]),[[4,'old','retired'],[5,'kin-home-thread','active']]);
 assert.deepEqual([stored.segments[1].codexHome,stored.segments[1].homeHash,stored.segments[1].migrationId,stored.segments[1].checkpointId],[f.request.codexHome,'h'.repeat(64),'kin-home:1','checkpoint:migration']);
 assert.equal(stored.sessionAdvice,null);assert.equal(stored.rollingCheckpoint,null);assert.equal(stored.restoreCheckpoint.id,'checkpoint:migration');
 assert.equal(stored.restorePending,false);
 assert.match(fs.readFileSync(f.options.file+'.events.jsonl','utf8'),/"kind":"migration-committed"/);
 assert.equal(fs.existsSync(f.options.file+'.lease'),false,'the lease goes with the commit');
 assert.notEqual(before,fs.readFileSync(f.options.file,'utf8'));
 // Asked again (a run that died before it wrote down the answer): the same answer, no new revision.
 const revision=stored.revision;
 assert.deepEqual(commitRegistryMigration(f.options.file,f.request),receipt);assert.equal(f.read().revision,revision);
 assert.throws(()=>commitRegistryMigration(f.options.file,{...f.request,id:'kin-home:2'}),/KIN_MIGRATION_ALREADY_COMMITTED/);
 assert.throws(()=>commitRegistryMigration(f.options.file,{...f.request,native:{threadId:'another',nativeSessionId:'another'}}),/KIN_MIGRATION_ID_CONFLICT/);
 assert.throws(()=>commitRegistryMigration(path.join(f.dir,'missing.json'),f.request),/KIN_SESSION_REGISTRY_MISSING/);
});
test('a rotation still in flight holds the migration back; a finished one is kept as a record',t=>{
 const f=migrationRegistry(t,2),stored=f.read();
 stored.candidate={id:'rotation:1',state:'unconfirmed',generation:2};fs.writeFileSync(f.options.file,JSON.stringify(stored));
 assert.throws(()=>commitRegistryMigration(f.options.file,f.request),/KIN_MIGRATION_ROTATION_UNSETTLED/);
 const profile={provider:'openai-15m',providerKind:'native',model:'gpt-6-sol',reasoningEffort:'medium',serviceTierPreference:'default'};
 const ready=f.read();ready.candidate={id:'rotation:1',state:'ready',generation:2,profile,native:{threadId:'candidate',nativeSessionId:'candidate',profile,providerBinding:providerBinding(profile)},checkpoint:{id:'old-candidate'}};
 fs.writeFileSync(f.options.file,JSON.stringify(ready));
 commitRegistryMigration(f.options.file,f.request);
 const after=f.read();
 assert.equal(after.candidate,null,'no handover prepared for the old runtime is left to promote');
 assert.deepEqual([after.retiredCandidates.at(-1).id,after.retiredCandidates.at(-1).state,after.retiredCandidates.at(-1).staleReason],['rotation:1','stale','kin-home-migration']);
});
test('a host started after the migration reopens the committed thread, never an unrelated one',t=>{
 const f=migrationRegistry(t,3);commitRegistryMigration(f.options.file,f.request);
 const registry=f.read(),saved=scope=>({users:{owner:{sessions:{[scope]:{sessionId:'old',updatedAt:1}}}}});
 const recovered=recoverSessionStore(registry,saved('main'),'owner');
 assert.deepEqual(recovered.users.owner.sessions.main,{sessionId:'kin-home-thread',updatedAt:1});
 assert.equal(recoverSessionStore(registry,{users:{owner:{sessions:{main:{sessionId:'kin-home-thread'}}}}},'owner'),null);
 assert.throws(()=>recoverSessionStore(registry,{users:{owner:{sessions:{main:{sessionId:'unrelated'}}}}},'owner'),/unrelated session storage/);
 assert.throws(()=>recoverSessionStore(registry,{users:{owner:{sessions:{}}}},'owner'),/unrelated session storage/);
 // The package the new thread was given carries the payload and the marker that proves it arrived.
 const injection=restoreInjection({payload:{kind:'internal-continuity-checkpoint',publicHistory:[]}},'op');
 assert.equal(injection.marker,'kin-checkpoint:op');
 assert.deepEqual(JSON.parse(injection.items[0].content[0].text),{kind:'internal-continuity-checkpoint',publicHistory:[],marker:'kin-checkpoint:op'});
});

// CR-RT-09: spec §1 keeps these maintenance causes at every pressure, not only above normal.
test('at normal pressure a task, the model, the configuration or a corrected source still asks a review; tokens alone do not',async t=>{
 for(const change of ['task','correction','memory-correction','profile','configuration','router-configuration']){
  const f=fixture(t);f.runtime.lastTokenUsage.inputTokens=10000;
  await f.manager.tick();assert.equal(f.manager.state.observation.pressure.level,'normal');
  for(let i=0;i<30;i++){minutes(f,1);f.runtime.lastTokenUsage.inputTokens+=100;await f.manager.tick();}
  assert.equal(f.calls.filter(c=>c==='review').length,0,change+': token growth alone asks nothing');
  if(change==='task')f.context.tasks=[{id:'work',inputVersion:1,status:'running'}];
  if(change==='correction')f.context.invalidatedSources=['owner-message-with-corrected-source'];
  if(change==='memory-correction')f.context.linked={items:[],needs_review_ids:['concern-with-corrected-source']};
  if(change==='profile')Object.assign(f.runtime,{model:'gpt-6-sol',modelProvider:'openai-15m',providerOverride:false,reasoningEffort:'medium'});
  if(change==='configuration')f.context.configVersion='persona-v2';
  if(change==='router-configuration')f.context.cursors={...f.context.cursors,config:3};
  await f.manager.tick();
  assert.equal(f.calls.filter(c=>c==='review').length,1,change);
  minutes(f,1);await f.manager.tick();
  assert.equal(f.calls.filter(c=>c==='review').length,1,change+': the same observation is asked once');
 }
});
test('changes at normal pressure are asked about once per cooldown, and the latest question stands',async t=>{
 const f=fixture(t);f.runtime.lastTokenUsage.inputTokens=10000;await f.manager.tick();
 f.context.tasks=[{id:'a',inputVersion:1,status:'running'}];await f.manager.tick();
 assert.equal(f.calls.filter(c=>c==='review').length,1);
 for(let version=2;version<=5;version++){minutes(f,5);f.context.tasks=[{id:'a',inputVersion:version,status:'running'}];await f.manager.tick();}
 assert.equal(f.calls.filter(c=>c==='review').length,1,'four more changes inside the cooldown ask nothing yet');
 const latest=f.manager.state.observation.id;
 assert.equal(f.manager.state.events[latest].state,'pending');
 assert.ok(Object.values(f.manager.state.events).filter(e=>e.state==='superseded').length>=3,'each earlier question gave way to the next');
 minutes(f,10);await f.manager.tick();
 assert.equal(f.calls.filter(c=>c==='review').length,2,'the latest change is asked once the cooldown is over');
 assert.equal(f.manager.state.events[latest].state,'queued');
 // Pressure above normal is not held by that cooldown.
 f.runtime.lastTokenUsage.inputTokens=30000;minutes(f,1);await f.manager.tick();
 assert.equal(f.calls.filter(c=>c==='review').length,3);
});
// CR-RT-08: a new attempt about the same observation used to come back as the first,
// finished job; the host marked it queued and waited for an answer that never came.
test('each review attempt is its own job in the mind, and only the job the mind reports for it counts',async t=>{
 const f=fixture(t);critical(f);const asked=[];let answer;
 f.manager.reviewRequested=async request=>{asked.push(request.id);return answer(request);};
 answer=request=>({id:'job:old',state:'complete',requestId:'review:elsewhere:1',snapshotId:request.cause});
 await f.manager.tick();
 const cause=f.manager.state.observation.id,event=f.manager.state.events[cause],id=n=>'review:'+cause.slice(0,24)+':'+n;
 assert.deepEqual([event.state,event.attempts,event.failures],['pending',0,1],'an answer about another attempt queues nothing');
 answer=request=>({id:'job:'+request.id,state:'needs-repair',requestId:request.id,snapshotId:request.cause});
 minutes(f,1);await f.manager.tick();
 assert.deepEqual([event.state,event.attempts],['pending',1],'a job that ended without an answer uses its attempt up');
 answer=request=>({id:'job:'+request.id,state:'pending',requestId:request.id,snapshotId:request.cause});
 minutes(f,2);await f.manager.tick();
 assert.deepEqual([event.state,event.requestId,event.jobId],['queued',id(2),'job:'+id(2)]);
 assert.deepEqual(asked,[id(1),id(1),id(2)]);
 f.context.sessionAdvice={eventId:'mind-event-9',snapshotId:cause,requestId:id(2),generation:1,
  decision:{action:'defer',reason:'Wait for the window to settle',evidenceIds:[]},receipt:{model:'deepseek-flash',reasoning:'high'}};
 assert.equal((await f.manager.tick()).state,'defer');
 assert.deepEqual([event.state,event.answer.requestId],['answered',id(2)],'the judgment names the attempt it answers');
});

