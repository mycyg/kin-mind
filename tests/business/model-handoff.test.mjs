import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {portableHistory,handoffCheckpoint} from '../../adapters/portable-history.mjs';
import {ModelHandoff,needsModelHandoff} from '../../adapters/model-handoff.mjs';
import {SessionManager} from '../../adapters/session-manager.mjs';
import {NativeCandidate,candidateMcpIsolation,candidateStartupDiagnostic} from '../../adapters/native-candidate.mjs';
import {recoverSessionStore} from '../../adapters/mobile-session-host.mjs';
const message=(role,text)=>({type:'message',role,content:[{type:role==='user'?'input_text':'output_text',text}]});
const row=payload=>({type:'response_item',payload});
function history(t,items=[],id='source'){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-model-handoff-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const file=path.join(dir,id+'.jsonl');fs.writeFileSync(file,[{type:'session_meta',payload:{id}},...items].map(i=>JSON.stringify(i)+'\n').join(''));
 return {dir,file,runtime:{known:true,threadId:id,nativeSessionId:id,sessionId:id,rolloutPath:file}};
}
const profiles={gpt:{provider:'native',providerKind:'native',model:'gpt-6.1-sol',reasoningEffort:'medium',serviceTierPreference:'default'},
 kimi:{provider:'kimi',providerKind:'gateway',model:'k3',reasoningEffort:'high',serviceTierPreference:'default'},
 ds:{provider:'deepseek',providerKind:'gateway',model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'}};
const bind=p=>({sourceProvider:p.provider,sourceProviderKind:p.providerKind,launchProvider:p.providerKind==='gateway'?'kin_session_gateway':p.provider,endpointSha256:p.providerKind==='gateway'?'a'.repeat(64):null});
const proof=p=>({verified:true,requestedProfile:p,providerBinding:bind(p),profileEvidence:{model:'verified',modelProvider:'verified',reasoningEffort:'verified',serviceTierConfiguration:'verified'}});
const cp={id:'cp',conversationId:'logical',generation:1,configVersion:'v',cursors:{public:1},items:[{id:'own-summary',role:'assistant',revision:'1',text:'自有压缩摘要：保留约定与否定。'}],coverage:{state:'compressed',covered_ids:['older-1'],omitted_ids:[]},sourceRevisions:{'older-1':'1'},complete:true,payload:{kind:'internal-continuity-checkpoint',checkpointId:'cp',publicHistory:[]}};

test('opaque compaction is retained as provenance, independent readable summaries and tool pairs survive',async t=>{
 const f=history(t,[row(message('user','历史原话')),row({type:'compaction',encrypted_content:'opaque-secret'}),
  {type:'compacted',payload:{replacement_history:[message('assistant','原生可读摘要'),{type:'compaction',encrypted_content:'opaque'}]}},
  row({type:'function_call',call_id:'a',name:'read',arguments:'{}'}),row(message('assistant','间隔消息')),
  row({type:'function_call_output',call_id:'a',output:'结果'}),row({type:'function_call',call_id:'interrupted',name:'write',arguments:'{}'})]);
 const original=fs.readFileSync(f.file);const h=await portableHistory(f.runtime);
 assert.equal(h.coverage.opaqueCompactions,2);assert.equal(h.coverage.unpairedToolItems,1);
 assert.equal(h.items.filter(i=>i.call_id==='a').length,2);assert(!h.items.some(i=>i.call_id==='interrupted'));
 assert(!JSON.stringify(h.items).includes('历史原话'));assert.equal(h.coverage.omittedReadableItems,1);
 assert(JSON.stringify(h).includes('原生可读摘要'));assert(!JSON.stringify(h).includes('opaque-secret'));
 const pack=handoffCheckpoint(cp,h,{id:'op',profile:profiles.kimi});assert.equal(pack.items[0].text,cp.items[0].text);
 assert.equal(pack.payload.continuity.checkpointCoverage.state,'compressed');assert.equal(pack.payload.continuity.portableHistory.coverage.lossless,false);
 assert.deepEqual(fs.readFileSync(f.file),original);
});

test('long histories are bounded, oversized and incomplete tool pairs never become half calls',async t=>{
 const entries=Array.from({length:4000},(_,i)=>row(message('user',`${i}:`+'x'.repeat(100))));
 entries.push(row({type:'function_call',call_id:'huge',name:'read',arguments:'{}'}),row({type:'function_call_output',call_id:'huge',output:'x'.repeat(8000)}));
 entries.push(row({type:'custom_tool_call',call_id:'small',name:'exec',input:'true'}),row({type:'custom_tool_call_output',call_id:'small',output:'ok'}));
 const f=history(t,entries);const h=await portableHistory(f.runtime,{maxBytes:2048});
 assert(h.coverage.omittedReadableItems>3900);assert(!h.items.some(i=>i.call_id==='huge'));
 assert.equal(h.items.filter(i=>i.call_id==='small').length,2);assert(JSON.stringify(h.items).length<2048);
});

test('partial or unrelated histories fail before any mutation',async t=>{
 const f=history(t,[row(message('user','hello'))]);fs.appendFileSync(f.file,'{"type":');
 await assert.rejects(portableHistory(f.runtime),/changed/);
 await assert.rejects(portableHistory({...f.runtime,threadId:'other',sessionId:'other'}),/identity/);
});

test('return handoff unfolds earlier context and includes the new conversation',async t=>{
 const first=history(t,[row(message('user','原来的约定'))]);const h=await portableHistory(first.runtime);
 const pack=handoffCheckpoint(cp,h,{id:'old-op',profile:profiles.kimi});
 const envelope={...pack.payload,marker:'kin-checkpoint:old-op'};
 const next=history(t,[row(message('assistant',JSON.stringify(envelope))),row(message('user','切换后新增的事实'))],'next');
 const returned=await portableHistory(next.runtime);
 assert(returned.items.some(i=>i.content?.[0]?.text==='原来的约定'));
 assert(returned.items.some(i=>i.content?.[0]?.text==='切换后新增的事实'));
 assert(!JSON.stringify(returned.items).includes('cross-model-continuity'));
});

function fixture(t){
 const f=history(t,[row(message('user','source dialogue')),row({type:'compaction',encrypted_content:'opaque'})]);
 let runtime={...f.runtime,model:'gpt-6.1-sol',modelProvider:'native',providerOverride:false,reasoningEffort:'medium',serviceTierPreference:'default',nativeStatus:'idle'};
 const context={cursors:{public:1},configVersion:'v',tasks:[]};const events=[];let serial=0;
 const manager=new SessionManager({file:path.join(f.dir,'registry.json'),binding:{conversationId:'logical',threadId:'source',nativeSessionId:'source'},coordinator:{locked:fn=>fn()},lease:false,inspect:async()=>runtime,collect:async()=>context});
 const native={create:async({profile})=>{events.push('create');const threadId='candidate-'+(++serial),file=path.join(f.dir,threadId+'.jsonl');
  fs.writeFileSync(file,JSON.stringify({type:'session_meta',payload:{id:threadId}})+'\n');return {threadId,nativeSessionId:threadId,path:file,profile,providerBinding:bind(profile),instructionBinding:null};},
  inject:async request=>{events.push('inject');fs.appendFileSync(request.path,JSON.stringify(row(message('assistant',JSON.stringify({...request.checkpoint.payload,marker:'kin-checkpoint:'+request.operationId}))))+'\n');return {verified:true};},
  verifyHandoff:async request=>{events.push('verify');return proof(request.profile);},close:async()=>{}};
 manager.promote=async({binding,candidate})=>{events.push('promote');runtime={...runtime,...binding,sessionId:binding.threadId,rolloutPath:candidate.native.path,model:candidate.profile.model,modelProvider:candidate.profile.provider,providerOverride:candidate.profile.providerKind==='gateway'};return {verified:true,threadId:binding.threadId};};
 const handoff=new ModelHandoff({manager,native,collect:async()=>structuredClone(context),inspect:async()=>({...runtime}),checkpoint:async(_,binding)=>({...structuredClone(cp),generation:binding.generation}),validate:async()=>true});
 return {f,manager,native,handoff,context,events,get runtime(){return runtime;}};
}
for(const name of ['kimi','ds'])test(`${name}: safe handoff then return keeps source and the intervening turn`,async t=>{
 const f=fixture(t),original=fs.readFileSync(f.f.file);
 f.manager.state.restorePending=true;f.manager.state.restoreRequired=true;f.manager.state.restoreCheckpoint={old:true};
 const first=await f.handoff.switchProfile(profiles[name]);
 assert.equal(first.model,profiles[name].model);assert.equal(f.manager.fence().generation,2);assert.deepEqual(f.events,['create','inject','verify','promote']);
 fs.appendFileSync(f.runtime.rolloutPath,JSON.stringify(row(message('user','new after switch')))+'\n');
 const returned=await f.handoff.switchProfile(profiles.gpt);assert.equal(returned.model,profiles.gpt.model);assert.equal(f.manager.fence().generation,3);
 assert(JSON.stringify(f.manager.state.candidate.checkpoint.payload).includes('new after switch'));
 assert.deepEqual(fs.readFileSync(f.f.file),original);assert.equal(f.manager.state.segments.length,3);assert.equal(f.manager.state.restorePending,false);assert.equal(f.manager.state.restoreCheckpoint,undefined);
 assert.equal(f.manager.state.handoffs.length,2);assert.equal(f.manager.state.handoffs[0].previousBinding.threadId,'source');
});

test('failure before commit keeps the source and does not promote',async t=>{
 const f=fixture(t);f.native.verifyHandoff=async()=>({verified:false});
 await assert.rejects(f.handoff.switchProfile(profiles.ds),/unverified/);
 assert.equal(f.manager.fence().threadId,'source');assert(!f.events.includes('promote'));assert.equal(f.manager.state.candidate.state,'failed');
});

test('cancel after injection keeps source; retry can complete',async t=>{
 const f=fixture(t),abort=new AbortController(),verify=f.native.verifyHandoff;
 f.native.verifyHandoff=async r=>{abort.abort();return verify(r);};
 await assert.rejects(f.handoff.switchProfile(profiles.kimi,{signal:abort.signal}),e=>e.code==='HANDOFF_CANCELED');
 assert.equal(f.manager.fence().threadId,'source');assert.equal(f.manager.state.candidate.state,'canceled');
 f.native.verifyHandoff=verify;await f.handoff.switchProfile(profiles.ds);assert.equal(f.manager.fence().generation,2);
});

test('changed source during preparation cancels binding update',async t=>{
 const f=fixture(t),verify=f.native.verifyHandoff;f.native.verifyHandoff=async r=>{f.context.cursors.public++;return verify(r);};
 await assert.rejects(f.handoff.switchProfile(profiles.ds),/sources changed/);assert.equal(f.manager.fence().threadId,'source');
});

test('uncertain promotion survives restart and uses existing registry recovery',async t=>{
 const f=fixture(t),promote=f.manager.promote;f.manager.promote=async()=>{throw Error('lost adapter response');};
 await assert.rejects(f.handoff.switchProfile(profiles.kimi),/lost adapter/);
 assert.equal(f.manager.fence().generation,2);assert.equal(f.manager.state.candidate.state,'unconfirmed');
 const saved={users:{owner:{sessions:{main:{sessionId:'source'}}}}};
 const repaired=recoverSessionStore(f.manager.state,saved,'owner');assert.equal(repaired.users.owner.sessions.main.sessionId,f.manager.fence().threadId);
 const reopened=new SessionManager({file:f.manager.file,binding:{threadId:'source',nativeSessionId:'source'},coordinator:{locked:fn=>fn()},lease:false,promote,collect:async()=>f.context,inspect:async()=>f.runtime});
 assert.equal((await reopened.recoverPromotion()).state,'complete');assert.equal(reopened.fence().generation,2);
});

test('handoff dispatch is provider generic and returning from a gateway also uses it',()=>{
 assert(needsModelHandoff(profiles.kimi,{modelProvider:'native',providerOverride:false}));
 assert(needsModelHandoff(profiles.ds,{modelProvider:'native',providerOverride:false}));
 assert(needsModelHandoff(profiles.gpt,{modelProvider:'deepseek',providerOverride:true}));
 assert(!needsModelHandoff(profiles.gpt,{modelProvider:'native',providerOverride:false}));
});

test('native handoff proof reads persisted bytes and profile without any model turn',async t=>{
 const f=history(t,[],'candidate'),profile=profiles.ds,historyPack={source:{threadId:'source'},items:[],coverage:{lossless:false}};
 const checkpoint=handoffCheckpoint(cp,historyPack,{id:'op',profile});
 for(const i of cp.items)fs.appendFileSync(f.file,JSON.stringify(row(message(i.role,i.text)))+'\n');
 fs.appendFileSync(f.file,JSON.stringify(row(message('assistant',JSON.stringify({...checkpoint.payload,marker:'kin-checkpoint:op'}))))+'\n');
 const native=new NativeCandidate({}),methods=[];
 native.load=async()=>{};native.launch=()=>({profile,providerBinding:bind(profile),modelProvider:'kin_session_gateway'});
 native.requestParams=()=>({});native.request=async method=>{methods.push(method);return {thread:{id:'candidate'},model:profile.model,modelProvider:'kin_session_gateway',reasoningEffort:'high',serviceTier:null};};
 const result=await native.verifyHandoff({checkpoint,operationId:'op',threadId:'candidate',path:f.file,profile});
 assert.equal(result.verified,true);assert.deepEqual(methods,['thread/resume']);
 checkpoint.payload.continuity.operationId='wrong';assert.equal((await native.verifyHandoff({checkpoint,operationId:'op',threadId:'candidate',path:f.file,profile})).verified,false);
});


test('indeterminate commit save uses recovery without claiming source was unchanged',async t=>{
 const f=fixture(t),save=f.manager.save.bind(f.manager);let failed=false;
 f.manager.save=(event,...args)=>{if(event==='binding-committed'&&!failed){failed=true;save(event,...args);throw Error('commit receipt lost');}return save(event,...args);};
 await assert.rejects(f.handoff.switchProfile(profiles.ds),e=>e.message==='commit receipt lost'&&!e.beforeMutation);
 assert.equal(f.manager.fence().generation,2);assert.equal(f.manager.state.candidate.state,'unconfirmed');
 const reopened=new SessionManager({file:f.manager.file,binding:{threadId:'source',nativeSessionId:'source'},coordinator:{locked:fn=>fn()},lease:false,promote:f.manager.promote,collect:async()=>f.context,inspect:async()=>f.runtime});
 assert.equal((await reopened.recoverPromotion()).state,'complete');assert.equal(reopened.fence().generation,2);
});

test('native replacement discards superseded pairs and prevents a pair crossing the compaction boundary',async t=>{
 const f=history(t,[row(message('user','superseded')),
 row({type:'function_call',call_id:'old',name:'read',arguments:'{}'}),row({type:'function_call_output',call_id:'old',output:'old result'}),
 row({type:'function_call',call_id:'cross',name:'read',arguments:'{}'}),
 {type:'compacted',payload:{replacement_history:[message('assistant','active summary')]}},
 row({type:'function_call_output',call_id:'cross',output:'late result'}),row(message('user','recent'))]);
 const original=fs.readFileSync(f.file),h=await portableHistory(f.runtime);
 assert.deepEqual(h.items.map(i=>i.content[0].text),['active summary','recent']);
 assert.equal(h.coverage.omittedReadableItems,3);assert.equal(h.coverage.unpairedToolItems,2);
 assert.deepEqual(fs.readFileSync(f.file),original);
});

test('concurrent switch is refused while preparation owns the manager; cancellation releases it',async t=>{
 const f=fixture(t),abort=new AbortController(),create=f.native.create;let release,entered;
 const reached=new Promise(r=>entered=r),hold=new Promise(r=>release=r);
 f.native.create=async request=>{entered();await hold;return create(request);};
 const first=f.handoff.switchProfile(profiles.kimi,{signal:abort.signal});await reached;
 await assert.rejects(f.handoff.switchProfile(profiles.ds),e=>e.beforeMutation&&/maintenance/.test(e.message));
 assert.equal(f.manager.fence().threadId,'source');abort.abort();release();
 await assert.rejects(first,e=>e.code==='HANDOFF_CANCELED');assert.equal(f.manager.running,false);
 f.native.create=create;await f.handoff.switchProfile(profiles.ds);assert.equal(f.manager.fence().generation,2);
});

test('cancellation after commit finishes the committed promotion instead of rolling it back',async t=>{
 const f=fixture(t),abort=new AbortController(),promote=f.manager.promote;
 f.manager.promote=async request=>{abort.abort();return promote(request);};
 const actual=await f.handoff.switchProfile(profiles.kimi,{signal:abort.signal});
 assert.equal(actual.model,profiles.kimi.model);assert.equal(f.manager.fence().generation,2);
 assert.equal(f.manager.state.candidate.state,'retired');assert.equal(f.manager.running,false);
});

test('new delivery during preparation keeps the original binding and releases native resources',async t=>{
 const f=fixture(t),inspect=f.handoff.inspect;let closed=0,reads=0;
 f.native.close=async()=>{closed++;};
 f.handoff.inspect=async()=>({...await inspect(),pendingDeliveries:++reads>1?1:0});
 await assert.rejects(f.handoff.switchProfile(profiles.ds),e=>e.beforeMutation&&/changed/.test(e.message));
 assert.equal(f.manager.fence().threadId,'source');assert.equal(closed,1);assert.equal(f.manager.running,false);
});


test('candidate MCP isolation targets effective servers without creating absent legacy entries',()=>{
 const result=candidateMcpIsolation({mcp_servers:{memorypalace:{command:'memory'},'computer-use':{command:'computer'}}});
 assert.deepEqual(result,{'mcp_servers.memorypalace.enabled':false,'mcp_servers.computer-use.enabled':false});
 assert(!Object.keys(result).some(k=>k.includes('node_repl')));
 assert.throws(()=>candidateMcpIsolation(undefined),/unavailable/);
 assert.throws(()=>candidateMcpIsolation({mcp_servers:[]}),/unavailable/);
});

test('startup failure retains only bounded categories and reports the failed stage',async t=>{
 const f=fixture(t),secret='private-dialogue-and-secret';
 const diagnostic=candidateStartupDiagnostic('Error: invalid transport\nin `mcp_servers.node_repl`\n'+secret,1,null);
 f.native.create=async()=>{throw Object.assign(Error(secret),{nativeDiagnostic:{...diagnostic,untrusted:secret}});};
 await assert.rejects(f.handoff.switchProfile(profiles.kimi),e=>e.code==='HANDOFF_NOT_COMMITTED'&&e.beforeMutation&&e.message.includes('MCP')&&!e.message.includes(secret));
 assert.deepEqual(f.manager.state.candidate.failure,{stage:'startup',category:'mcp-invalid-transport',exitCode:1});
 assert.equal(f.manager.fence().threadId,'source');assert(!JSON.stringify(f.manager.state.candidate).includes(secret));
});

test('candidate startup process stderr is classified without retaining its body',async t=>{
 const f=history(t),script=path.join(f.dir,'broken.mjs');
 fs.writeFileSync(script,"process.stderr.write('Error: invalid transport\\nin `mcp_servers.node_repl`\\ncredential=private-value');process.exitCode=1;");
 const native=new NativeCandidate({command:process.execPath,args:[script],cwd:f.dir,env:{PATH:'/usr/bin:/bin',HOME:f.dir},inheritEnv:false,timeoutMs:1000});
 await assert.rejects(native.start(),e=>e.nativeDiagnostic?.category==='mcp-invalid-transport'&&e.nativeDiagnostic.exitCode===1&&!JSON.stringify(e).includes('private-value'));
 await native.close();
});
