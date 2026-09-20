import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {SessionManager,candidateVerificationReady} from './session-manager.mjs';
import {NativeWindow,checkpointMarker,nativePressureRuntime} from './native-window.mjs';
import {NativeContextDelivery} from './context-delivery.mjs';
import {NativeCandidate} from './native-candidate.mjs';
import {atomicJson} from './mobile-router.mjs';
import {safeReadOnlyPreparation,checkpointBudget} from './session-policy.mjs';
import {switchCodexModel,runtimeProfile,profileMatches} from './codex-models.mjs';
import {sameInstructionBinding,verifyCompanionInstructions} from './instruction-evidence.mjs';
const digest=v=>createHash('sha256').update(JSON.stringify(v)).digest('hex');
const read=file=>JSON.parse(fs.readFileSync(file,'utf8'));
const SAFE_PROVIDER=/^[A-Za-z0-9_-]{1,120}$/;

/** The maintenance process receives the exact non-secret profile already
 * verified by the router. A missing provider or preference is a reason to wait,
 * never a cue to infer an OpenAI/native default from the model name. */
export function candidateRuntimeProfile(value={}) {
  const source=Object.hasOwn(value,'provider')?value:runtimeProfile(value),profile={
    provider:source.provider,providerKind:source.providerKind,model:source.model,reasoningEffort:source.reasoningEffort,
    serviceTierPreference:source.serviceTierPreference,
  };
  if(!SAFE_PROVIDER.test(profile.provider??'')||!['gateway','native'].includes(profile.providerKind)||
    typeof profile.model!=='string'||!profile.model||typeof profile.reasoningEffort!=='string'||!profile.reasoningEffort||
    !['default','fast'].includes(profile.serviceTierPreference))throw Error('Candidate runtime profile is unverified');
  return profile;
}

export function candidateConfigForRuntime(value,{catalogFile,gateway,companionInstructions}={}) {
  const profile=candidateRuntimeProfile(value),config={model_catalog_json:catalogFile};
  const companion=verifyCompanionInstructions(companionInstructions);
  if(companion.enabled)config.model_instructions_file=companion.modelInstructionsFile;
  let modelProvider=profile.provider,endpointSha256=null;
  if(profile.providerKind==='gateway') {
    if(!gateway?.baseUrl||!gateway?.token)throw Error('Candidate gateway profile is unavailable');
    // The router's provider is the serving identity. The isolated candidate has
    // a separate local provider alias whose endpoint is explicitly bound here;
    // equal names are neither required nor treated as verification.
    modelProvider='kin_session_gateway';endpointSha256=digest(gateway.baseUrl);
    config['model_providers.'+modelProvider]={name:'Kin session gateway',base_url:gateway.baseUrl,env_key:'KIN_SESSION_GATEWAY_TOKEN',wire_api:'responses',requires_openai_auth:false};
  }
  const providerBinding={sourceProvider:profile.provider,sourceProviderKind:profile.providerKind,launchProvider:modelProvider,endpointSha256};
  return {profile,providerBinding,modelProvider,reasoningEffort:profile.reasoningEffort,
    fastMode:profile.serviceTierPreference==='fast'?'on':'off',config,
    developerInstructions:companion.enabled?companion.developerInstructions:undefined,
    instructionBinding:companion.enabled?companion.evidence:null};
}

export function registryBinding(file,fallback){return fs.existsSync(file)?read(file).binding:fallback;}

export function currentCheckpoint(state){return state.restorePending&&state.restoreCheckpoint?state.restoreCheckpoint:state.rollingCheckpoint??state.candidate?.checkpoint??state.restoreCheckpoint;}

/** An accepted native append is not safe to expose to a model turn until the
 * private reply boundary has durably registered the exact source identity.
 * Keep the public context body and dependency items out of that registry. */
export function bindAcceptedPrivateContext({bridge,contextToken,source,receipt}) {
  if(receipt?.state!=='accepted')throw Error('Restore receipt remains unconfirmed');
  const identity={id:source?.id,marker:source?.marker,text_hash:source?.text_hash,session:source?.session,epoch:source?.epoch};
  if(!identity.id||!identity.marker||!identity.text_hash||!identity.session||!identity.epoch||
    receipt.id!==identity.id||receipt.marker!==identity.marker||receipt.text_hash!==identity.text_hash||
    receipt.session!==identity.session||receipt.epoch!==identity.epoch)
    throw Error('Restore accepted receipt identity mismatch');
  if(typeof bridge?.bindPrivateContext!=='function'||bridge.bindPrivateContext(contextToken,identity)!==true)
    throw Error('Restore private context binding unavailable');
  return receipt;
}

export async function loadCandidateSession({client,connection,binding,candidate,cwd,gateway,instructionBinding=null}) {
  if(!sameInstructionBinding(candidate?.native?.instructionBinding,instructionBinding))
    throw Error('Candidate instruction binding changed');
  client.beginSessionReplay();
  try {await connection.loadSession({sessionId:binding.threadId,cwd,mcpServers:[]});}
  finally {await client.endSessionReplay();}
  // ACP may restore its launch profile. Reapply the verified mobile profile
  // before dispatch opens, without generating a synthetic user turn.
  if(!candidateVerificationReady(candidate?.verification,candidate))throw Error('Candidate profile receipt is unverified');
  const expected=candidateRuntimeProfile(candidate.profile);
  const actual=await switchCodexModel({connection,sessionId:binding.threadId,profile:expected,gateway});
  const options=await connection.setSessionConfigOption({sessionId:binding.threadId,configId:'reasoning_effort',value:expected.reasoningEffort});
  if(!actual.known||actual.threadId!==binding.threadId||actual.nativeSessionId!==binding.nativeSessionId||!profileMatches(actual,expected))throw Error('Promoted native runtime unverified');
  return {actual,configOptions:options.configOptions};
}

export function recoverSessionStore(registry,saved,ownerId){
  const candidate=registry?.candidate,binding=registry?.binding;
  if(!candidate||!['committing','unconfirmed'].includes(candidate.state)||!candidateVerificationReady(candidate.verification,candidate)||candidate.native?.threadId!==binding?.threadId||candidate.generation+1!==binding.generation)return null;
  const sessions=Object.values(saved.users?.[ownerId]?.sessions??{}),previous=registry.segments.find(s=>s.generation===candidate.generation);
  if(sessions.length!==1||![previous?.threadId,binding.threadId].includes(sessions[0].sessionId))throw Error('Committed binding cannot reconcile unrelated session storage');
  const result=structuredClone(saved);Object.values(result.users[ownerId].sessions)[0].sessionId=binding.threadId;return result;
}

/** Adapter dependencies are supplied by the mobile host. No desktop paths,
 * credentials, channel IDs or shared experiences are built into this module. */
export async function startMobileSessions({bridge,root,config,routerConfig,mindCall,recordStatus=()=>{}}) {
  if(!config.adaptive_sessions)return null;
  const companionInstructions=config.companion_instructions??{enabled:false};
  // This is a preflight gate. Each later candidate launch verifies the bytes
  // again so a long-running host cannot silently use a changed versioned file.
  verifyCompanionInstructions(companionInstructions);
  const routing=bridge.mobileRouting,router=routing.router;
  const file=config.session_registry_file??path.join(root,'state/conversation-registry.json');
  const observationFile=config.session_observation_file??path.join(root,'state/session-observation.json');
  await routing.ensureSession('kin-host:session-management');
  await router.restoreRoutingProfile();
  const initial=await routing.inspect();
  if(!initial.known||!initial.threadId||!initial.nativeSessionId)throw Error('Native session identity unverified');
  let reader,readerId,initialScan=true,closed=false,running=false;
  const native=new NativeCandidate({command:config.codex_command,args:(config.candidate_disabled_mcp_servers??[]).flatMap(name=>['-c','mcp_servers.'+name+'.enabled=false']),cwd:path.join(root,'conversation'),env:{KIN_SESSION_GATEWAY_TOKEN:routing.gateway.token},
    configForModel:profile=>candidateConfigForRuntime(profile,{catalogFile:path.join(root,'mobile-models.json'),gateway:routing.gateway,companionInstructions}),
    personaInstructions:fs.readFileSync(path.join(root,'conversation/AGENTS.md'),'utf8')});
  const collect=async()=>{
    const pending=bridge.mindHost?.memoryJournal?.snapshot?.()??[];
    const state=router.snapshot(),inputs=Object.values(state.inputs),tasks=router.tasks();
    const session=bridge.sessionManager.getSession(bridge.ownerId);
    const foreground=Boolean(session?.processing||session?.queue?.length||tasks.length);
    const snapshot=await mindCall('session-snapshot',{pending,tasks,foreground});
    const outstanding=inputs.filter(i=>['selected','submitting','unconfirmed'].includes(i.state));
    snapshot.inputStates=inputs.slice(-24).map(i=>({id:i.id,state:i.state,taskId:i.taskId,at:i.at}));
    snapshot.cursors={...snapshot.cursors,inputs:digest(inputs.map(i=>[i.id,i.state,i.hash])),tasks:digest(tasks),config:state.configRevision??0};
    return {...snapshot,tasks,inputs:outstanding,notices:Object.values(state.notices),contactRunning:Boolean(bridge.mindHost?.contactRunning)};
  };
  const inspect=async()=>{
    const value=await routing.inspect();
    if(value.known&&value.rolloutPath){
      if(readerId!==value.threadId){reader=new NativeWindow({file:value.rolloutPath,threadId:value.threadId,stateFile:path.join(root,'state/native-window-'+value.threadId+'.json')});readerId=value.threadId;}
      const state=await reader.poll();
      return nativePressureRuntime(value,state);
    }
    return value;
  };
  const background=new NativeContextDelivery({call:mindCall,
    runtime:async()=>{const current=await routing.ensureSession('kin-host:context-status');return current.agentInfo.connection.extMethod('_kin/runtime',{sessionId:manager.fence().threadId});},
    inject:async request=>{const current=await routing.ensureSession('kin-host:context');return current.agentInfo.connection.extMethod('_kin/inject-checkpoint',request);}});
  const manager=new SessionManager({file,binding:{threadId:initial.threadId,nativeSessionId:initial.nativeSessionId},coordinator:router,inspect,collect,config:config.session_management??{},
    checkpoint:async(snapshot,binding,budget)=>mindCall('session-checkpoint',{snapshot,binding,budget:router.tasks().length?Math.max(budget,4000):budget}),
    compact:async operationId=>{const session=await routing.ensureSession('kin-host:compact');return session.agentInfo.connection.extMethod('_kin/compact',{sessionId:manager.fence().threadId,operationId});},
    ackCompact:async(receipt,checkpoint)=>{
      const id=receipt.nativeEventId??receipt.operationId;
      const result=await mindCall('memory-compact-ack',{session:receipt.actual_session,epoch:id,actual_session:receipt.actual_session,completed:true});
      manager.state.restoreEpoch=id;manager.state.restoreRequired=true;
      manager.state.restoreCheckpoint=checkpoint??null;manager.state.restorePending=Boolean(checkpoint);
      manager.save('restore-checkpoint-pending');
      return result;
    },
    reviewRequested:async event=>{atomicJson(observationFile,event.observation);return mindCall('session-review',{id:event.id});},
    createCandidate:request=>native.create(request),injectCandidate:request=>native.inject(request),verifyCandidate:request=>native.verify(request),
    closeCandidate:()=>native.close(),
    reconcileCandidate:async candidate=>candidate.native.path&&fs.existsSync(candidate.native.path)&&(await checkpointMarker(candidate.native.path,'kin-checkpoint:'+candidate.injectionId)).found,
    validateEvidence:async evidence=>(await mindCall('session-validate',{checkpoint:{sourceDependencies:evidence.flatMap(e=>e.dependencies??[])}})).valid,
    promote:async({previous,binding,candidate})=>{
      await native.close();
      const session=bridge.sessionManager.getSession(bridge.ownerId);
      if(!session||session.processing||session.queue.length)throw Error('Host became busy before promotion');
      const connection=session.agentInfo.connection;
      if(!(await mindCall('session-validate',{checkpoint:candidate.checkpoint})).valid)throw Error('Checkpoint sources changed before promotion');
      const companion=verifyCompanionInstructions(companionInstructions);
      const loaded=await loadCandidateSession({client:session.client,connection,binding,candidate,cwd:path.join(root,'conversation'),gateway:routing.gateway,
        instructionBinding:companion.enabled?companion.evidence:null});
      const {actual}=loaded;
      session.agentInfo.sessionId=binding.threadId;session.configOptions=loaded.configOptions;session.agentInfo.configOptions=loaded.configOptions;
      await bridge.sessionManager.opts.persistSessionId(bridge.ownerId,binding.threadId);session.sessionIdPersisted=true;
      if(router.state.generation!==binding.generation)router.adoptBinding(binding,actual);
      // Old native session is unsubscribed; its durable history remains intact.
      if(previous.threadId!==binding.threadId){try{await connection.extMethod('_kin/retire-session',{sessionId:previous.threadId});}catch{recordStatus({sessionRetirement:{threadId:previous.threadId,state:'pending'}});}}
      return {verified:true,threadId:actual.threadId,nativeSessionId:actual.nativeSessionId,model:actual.model,at:new Date().toISOString()};
    }});
  router.state.conversationId=manager.fence().conversationId;router.state.generation=manager.fence().generation;router.state.nativeSessionId=manager.fence().nativeSessionId;router.save('logical-conversation-bound');
  const api={manager,view:()=>{const s=manager.view(),c=s.compactions.at(-1);return {binding:s.binding,policy:s.config,configRevision:s.configRevision??0,pressure:s.observation?.pressure,advice:s.advice,candidate:s.candidate?{id:s.candidate.id,state:s.candidate.state}:null,lastCompaction:c?{id:c.id,state:c.state,completedAt:c.completedAt,before:c.before,after:c.after}:null,compactionCount:s.compactions.length,status:s.lastTick};},
    deliverBackground:context=>background.deliver(context),
    fence:()=>manager.fence(),assertFence:fence=>manager.assertFence(fence),collect,waitForBinding:()=>router.locked(async()=>{}),
    request:request=>manager.request(request),configure:request=>manager.locked(()=>manager.configure(request)),checkpoint:(sourceCursor=null)=>{
      const cp=currentCheckpoint(manager.state);if(!cp)return null;
      if(sourceCursor!==null){if(!Number.isInteger(sourceCursor)||sourceCursor<0)throw Error('Invalid source cursor');const all=Object.entries(cp.sourceRevisions);return {checkpointId:cp.id,sources:all.slice(sourceCursor,sourceCursor+10).map(([id,revision])=>({id,revision})),nextSourceCursor:sourceCursor+10<all.length?sourceCursor+10:null};}
      return {...cp.payload,state:cp.complete?'ready':'incomplete',tokens:cp.tokens,coverage:cp.coverage.state,manifestVersion:cp.manifestVersion??null,memoryCoverage:cp.memoryCoverage,metrics:cp.metrics,watermarks:cp.watermarks,sourceCount:Object.keys(cp.sourceRevisions).length,readSourceCursor:0};
    },
    notice:params=>{const update=params.update;
      // Native maintenance notifications cannot wait on the router mutex held
      // by compaction itself. Their durable lifecycle is read from native history.
      if(update._meta?.contextCompaction){recordStatus({nativeCompactionNotice:{id:update.toolCallId,status:update.status,threadId:params.sessionId}});return true;}
      if(update._meta?.kinRuntimeNotice){recordStatus({runtimeNotice:{...update._meta.kinRuntimeNotice,threadId:params.sessionId}});return true;}return false;},
    async tick(){
      if(running||closed)return;running=true;
      try{
        await inspect();
        const events=reader?.state.events??[];
        if(initialScan){
          if(!manager.state.nativeBootstrapped){for(const event of events)if(!manager.state.compactions.some(c=>c.nativeEventId===event.id))manager.state.compactions.push({id:event.id,nativeEventId:event.id,state:'complete',generation:manager.fence().generation,completedAt:Date.parse(event.at),origin:'history-bootstrap'});manager.state.nativeBootstrapped=true;manager.save('native-history-bootstrapped');}
          initialScan=false;
        }
        for(const event of events)if(!manager.state.compactions.some(c=>c.nativeEventId===event.id))await manager.nativeCompaction(event);
        const snapshot=await collect(),runtime=await inspect();
        // Read-only roster preparation may proceed on a versioned snapshot while
        // owner notifications are unconfirmed; the prepared artifacts carry the
        // annotation. Compaction, injection and promotion keep the strict boundary.
        const preparation=safeReadOnlyPreparation({...snapshot,runtime});
        if(manager.state.restoreRequired&&!manager.state.restorePending&&preparation.safe){
          const cp=await manager.checkpoint(snapshot,manager.fence(),router.tasks().length?4000:manager.state.config.restoreBudget);
          if(cp.complete&&digest(snapshot.cursors)===digest((await collect()).cursors)){manager.state.restoreCheckpoint=cp;manager.state.restorePending=true;manager.save('automatic-compaction-checkpoint-ready',{unconfirmedDeliveries:preparation.unconfirmedDeliveries??[]});}
        }
        if(snapshot.manifestVersion&&preparation.safe){
          const key=digest(snapshot.cursors);
          if(manager.state.rollingCursor!==key){
            const cp=await mindCall('session-checkpoint',{snapshot,binding:manager.fence(),budget:router.tasks().length?4000:manager.state.config.restoreBudget,allow_model:false,shadow:true});
            manager.state.rollingCheckpoint=cp;manager.state.rollingCursor=key;manager.save('rolling-manifest-prepared',{unconfirmedDeliveries:preparation.unconfirmedDeliveries??[]});
          }
        }
        await manager.observe(runtime,snapshot);if(manager.state.observation)atomicJson(observationFile,manager.state.observation);
        const {state}=await mindCall('read');const advice=state?.session_advice;
        if(advice&&manager.state.observation&&advice.eventId!==manager.state.lastAdviceEvent){
          manager.state.lastAdviceEvent=advice.eventId;
          if(advice.snapshotId===manager.state.observation.id)manager.advise(advice.decision,advice.receipt,advice.snapshotId);
          else if(['elevated','critical'].includes(manager.state.observation.pressure.level)||Object.values(manager.state.requests).some(r=>r.state==='pending')){
            const key='refresh:'+manager.state.observation.id;if(!manager.state.events[key])manager.state.events[key]={state:'pending',observationId:manager.state.observation.id};
          }
        }
        const result=await manager.tick();manager.state.lastTick={...result,checkedAt:new Date().toISOString()};manager.save('tick');recordStatus({sessionManagement:api.view()});
      }catch(error){recordStatus({sessionManagement:{state:'waiting',reason:error.name,checkedAt:new Date().toISOString()}});}
      finally{running=false;}
    },
    async restoreBeforeDispatch(contextToken){
      // Called inside the router's dispatch coordinator; never request DS here.
      // A native auto-compaction can finish between minute ticks and inputs.
      // Reconcile its lifecycle before consulting the current window ledger.
      await inspect();
      if(!initialScan)for(const event of reader?.state.events??[])if(!manager.state.compactions.some(c=>c.nativeEventId===event.id))await manager.nativeCompaction(event);
      if(!manager.state.restoreRequired&&!manager.state.restorePending)return;
      let cp=manager.state.restoreCheckpoint;
      if(!manager.state.restorePending||!cp||!(await mindCall('session-validate',{checkpoint:cp})).valid){
        manager.state.restorePending=false;manager.state.restoreCheckpoint=null;manager.save('restore-source-refresh');
        const snapshot=await collect();
        cp=await mindCall('session-checkpoint',{snapshot,binding:manager.fence(),budget:router.tasks().length?4000:manager.state.config.restoreBudget,allow_model:false});
        if(!cp.complete)throw Error('Restore coverage pending outside dispatch');
        manager.state.restoreCheckpoint=cp;manager.state.restorePending=true;manager.save('fresh-restore-checkpoint-ready');
      }
      const runtime=await inspect();if(!runtime.known||runtime.active||runtime.backgroundTasks||!runtime.rolloutPath)throw Error('Recovery boundary unconfirmed');
      if(cp.manifestVersion){
        const prepared=await mindCall('context-delivery-prepare',{session:runtime.threadId,epoch:manager.state.restoreEpoch,
          event_id:'restore:'+cp.id,text:JSON.stringify(cp.payload),items:cp.contextDependencies??[],
          budget:checkpointBudget(cp,Math.max(manager.state.config.restoreBudget,router.tasks().length?4000:0)),kind:'restore',manifest_id:cp.id});
        if(prepared.state==='incomplete')throw Error('Restore envelope exceeds budget');
        const receipt=await background.deliver(prepared);
        bindAcceptedPrivateContext({bridge,contextToken,source:prepared,receipt});
        manager.state.restoration={id:prepared.id,state:'complete',checkpointId:cp.id,receipt:{state:receipt.state,textHash:receipt.text_hash}};
        manager.state.restorePending=false;manager.state.restoreRequired=false;manager.save('manifest-restore-complete');return;
      }
      const operationId='restore:'+digest([cp.id,manager.state.restoreEpoch]);
      let op=manager.state.restoration;
      const marker='kin-checkpoint:'+operationId;
      let proof=await checkpointMarker(runtime.rolloutPath,marker);
      if(!proof.found){
        if(op?.id===operationId&&['injecting','unconfirmed'].includes(op.state))throw Error('Recovery injection receipt unconfirmed');
        op={id:operationId,state:'injecting',checkpointId:cp.id};manager.state.restoration=op;manager.save('restore-injecting');
        const session=await routing.ensureSession('kin-host:restore-context');
        const text=JSON.stringify({...cp.payload,marker});
        try{await session.agentInfo.connection.extMethod('_kin/inject-checkpoint',{sessionId:runtime.threadId,operationId,items:[{type:'message',role:'assistant',content:[{type:'output_text',text}]}]});proof=await checkpointMarker(runtime.rolloutPath,marker);if(!proof.found)throw Error('Missing persisted injection');}
        catch(error){op.state='unconfirmed';manager.save('restore-unconfirmed');throw error;}
      }
      await mindCall('memory-injection-ack',{session:runtime.threadId,epoch:manager.state.restoreEpoch,id:operationId,tokens:cp.tokens});
      manager.state.restoration={id:operationId,state:'complete',checkpointId:cp.id,receipt:proof};manager.state.restorePending=false;manager.state.restoreRequired=false;manager.save('restore-complete');
    },
    async close(){closed=true;clearInterval(timer);await native.close();manager.close();}};
  bridge.mobileSessions=api;
  const timer=setInterval(()=>{void api.tick();},60000);timer.unref();void api.tick();return api;
}
