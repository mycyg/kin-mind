import fs from 'node:fs';
import {createHash,randomUUID} from 'node:crypto';
import {readJsonFile,createJsonExclusive} from './atomic-json.mjs';
import {atomicJson,loadState} from './mobile-router.mjs';
import {SESSION_DEFAULTS,windowPressure,rotationEligibility,safeBoundary,validateCheckpoint} from './session-policy.mjs';
import {runtimeProfile} from './codex-models.mjs';
const hash=v=>createHash('sha256').update(JSON.stringify(v)).digest('hex');
const copy=v=>structuredClone(v);
const candidateProfile=value=>{
  const source=Object.hasOwn(value??{},'provider')?value:runtimeProfile(value),profile={provider:source.provider,providerKind:source.providerKind,model:source.model,reasoningEffort:source.reasoningEffort,serviceTierPreference:source.serviceTierPreference};
  return profile.provider&&['gateway','native'].includes(profile.providerKind)&&profile.model&&profile.reasoningEffort&&['default','fast'].includes(profile.serviceTierPreference)?profile:null;
};
const sameCandidateProfile=(left,right)=>Boolean(left&&right&&['provider','providerKind','model','reasoningEffort','serviceTierPreference'].every(key=>left[key]===right[key]));
const sameProviderBinding=(left,right)=>Boolean(left&&right&&['sourceProvider','sourceProviderKind','launchProvider','endpointSha256'].every(key=>left[key]===right[key]));
const validProviderBinding=(binding,profile)=>Boolean(binding&&profile&&binding.sourceProvider===profile.provider&&binding.sourceProviderKind===profile.providerKind&&
  (profile.providerKind==='native'?binding.launchProvider===profile.provider&&binding.endpointSha256===null:
    binding.launchProvider!==profile.provider&&/^[a-f0-9]{64}$/.test(binding.endpointSha256??'')));
const candidateHasProfileAuthority=candidate=>{
  const profile=candidateProfile(candidate?.profile),nativeProfile=candidateProfile(candidate?.native?.profile),binding=candidate?.native?.providerBinding;
  return Boolean(sameCandidateProfile(profile,nativeProfile)&&validProviderBinding(binding,profile));
};
const RESPONSE_EVIDENCE=['model','modelProvider','reasoningEffort','serviceTierConfiguration'];
export const candidateVerificationReady=(verification,candidate)=>Boolean(verification?.verified&&
  sameCandidateProfile(candidateProfile(verification.requestedProfile),candidateProfile(candidate?.profile))&&
  sameProviderBinding(verification.providerBinding,candidate?.native?.providerBinding)&&
  RESPONSE_EVIDENCE.every(key=>verification.profileEvidence?.[key]==='verified'));
const retireLegacyCandidate=state=>{
  const candidate=state.candidate;
  if(!candidate||!['created','injected','ready','waiting'].includes(candidate.state)||candidateHasProfileAuthority(candidate))return false;
  candidate.state='stale';candidate.staleReason='legacy-profile-evidence-missing';state.retiredCandidates??=[];state.retiredCandidates.push(copy(candidate));return true;
};

/** One durable binding, used by every channel. Acquiring this lease is a host
 * operation. A second live host cannot silently steal dispatch or sending. */
export class SessionManager {
  constructor({file,binding,coordinator,inspect,collect,checkpoint,compact,ackCompact,createCandidate,injectCandidate,verifyCandidate,promote,closeCandidate,reconcileCandidate,validateEvidence=async()=>true,reviewRequested=async()=>{},now=()=>Date.now(),config={},lease=true}) {
    Object.assign(this,{file,coordinator,inspect,collect,checkpoint,compact,ackCompact,createCandidate,injectCandidate,verifyCandidate,promote,closeCandidate,reconcileCandidate,validateEvidence,reviewRequested,now});
    this.tail=Promise.resolve();this.closed=false;
    if(lease)this.acquireLease();
    const loaded=loadState(file,{now,validate:value=>Boolean(value.binding?.threadId&&value.binding.nativeSessionId)&&Array.isArray(value.segments)});
    // The binding is a fencing token. A registry with no readable revision left cannot
    // be reopened at generation 1: an older fence, durable in a manifest, would pass it
    // again. The quarantined bytes stay on disk, and restoring them is an operator step.
    if(!loaded.value&&loaded.recovery)throw Error('KIN_SESSION_REGISTRY_UNREADABLE');
    this.state=loaded.value??{schema:1,revision:0,binding:{conversationId:binding.conversationId??randomUUID(),generation:1,threadId:binding.threadId,nativeSessionId:binding.nativeSessionId},segments:[],config:{...SESSION_DEFAULTS,...config},compactions:[],evidence:{},events:{},requests:{},advice:null};
    if(this.state.schema!==1||!this.state.binding.threadId||!this.state.binding.nativeSessionId)throw Error('Invalid conversation registry');
    if(loaded.recovery)this.state.recovery=loaded.recovery;
    this.state.config={...SESSION_DEFAULTS,...this.state.config};
    if(!this.state.segments.length)this.state.segments.push({...this.state.binding,state:'active',activatedAt:null});
    // A crash cannot turn an uncertain create, compact or promotion into retry.
    for(const op of [this.state.candidate,...this.state.compactions])if(op&&['creating','injecting','committing','running'].includes(op.state))op.state='unconfirmed';
    const legacyCandidateRetired=retireLegacyCandidate(this.state);
    this.save('open',legacyCandidateRetired?{candidateMigration:'legacy-profile-evidence-missing'}:{});
  }
  save(kind,details={}) {
    this.state.revision++;
    atomicJson(this.file,this.state,{previous:true});
    fs.appendFileSync(this.file+'.events.jsonl',JSON.stringify({at:this.now(),revision:this.state.revision,kind,...details})+'\n',{mode:0o600});
  }
  view(){return copy(this.state);}
  fence(){return copy(this.state.binding);}
  assertFence(fence) {
    // The current file only: a fence is never checked against an older revision.
    const current=readJsonFile(this.file);
    if(current.state!=='ok'||!current.value?.binding)throw Error('KIN_SESSION_REGISTRY_UNREADABLE');
    if(hash(current.value.binding)!==hash(fence))throw Error('KIN_STALE_SESSION_GENERATION');
    return true;
  }
  async locked(fn){return this.coordinator.locked(async()=>{this.assertFence(this.state.binding);return fn();});}
  acquireLease() {
    const file=this.file+'.lease';
    this.leaseNonce=randomUUID();
    const write=()=>createJsonExclusive(file,{pid:process.pid,nonce:this.leaseNonce});
    if(!write()) {
      // An unreadable lease is treated as held: a second live host is the dangerous case.
      const owner=readJsonFile(file).value;let dead=false;
      if(Number.isInteger(owner?.pid)){try{process.kill(owner.pid,0);}catch(e){dead=e.code==='ESRCH';}}
      if(!dead)throw Error('KIN_SESSION_MANAGER_ALREADY_RUNNING');
      fs.unlinkSync(file);if(!write())throw Error('KIN_SESSION_MANAGER_ALREADY_RUNNING');
    }
    this.leaseFile=file;
  }
  close(){this.closed=true;if(this.leaseFile&&readJsonFile(this.leaseFile).value?.nonce===this.leaseNonce)fs.unlinkSync(this.leaseFile);}
  evidence(event) {
    if(!event.id||!event.sourceId||!event.revision||!Number.isFinite(event.at))throw Error('Session evidence needs a source and time');
    const old=this.state.evidence[event.id];
    if(old&&old.revision===event.revision){if(hash(old)!==hash({...event,generation:event.generation??this.state.binding.generation}))throw Error('Evidence ID conflict');return old;}
    this.state.evidence[event.id]={...copy(event),generation:event.generation??this.state.binding.generation};this.save('evidence',{id:event.id});return this.state.evidence[event.id];
  }
  request({id,action='review',reason,sourceId}) {
    if(!id||!reason||!sourceId||!['review','compact','rotate'].includes(action))throw Error('Invalid session request');
    const input={id,action,reason,sourceId};const prior=this.state.requests[id];
    if(prior){if(prior.hash!==hash(input))throw Error('Session request ID conflict');return copy(prior);}
    this.state.requests[id]={...input,hash:hash(input),state:'pending',at:this.now()};this.save('request',{id});return copy(this.state.requests[id]);
  }
  configure({id,sourceId,reason,expectedRevision,changes}) {
    const input={id,sourceId,reason,expectedRevision,changes};
    if(!id||!sourceId||!reason||!changes)throw Error('Configuration requires a sourced command');
    this.state.configReceipts??={};const prior=this.state.configReceipts[id];
    if(prior){if(prior.hash!==hash(input))throw Error('Configuration command conflict');return copy(prior);}
    if(expectedRevision!==(this.state.configRevision??0))throw Error('Configuration revision changed');
    const booleans=['observe','compact','prepare','rotate'],numbers={preparePressure:[0.4,0.9],criticalPressure:[0.6,0.98],compactCooldownMs:[300000,86400000],rotationCooldownMs:[300000,86400000],restoreBudget:[500,4000]};
    for(const [key,value] of Object.entries(changes))if(!(booleans.includes(key)&&typeof value==='boolean')&&!(numbers[key]&&Number.isFinite(value)&&value>=numbers[key][0]&&value<=numbers[key][1]))throw Error('Unsupported session configuration');
    const next={...this.state.config,...changes};if(next.criticalPressure<=next.preparePressure)throw Error('Pressure thresholds out of order');
    this.state.configRevision=(this.state.configRevision??0)+1;next.version='compact-first-v1:'+this.state.configRevision;this.state.config=next;
    const receipt={id,sourceId,reason,changes,hash:hash(input),state:'applied',revision:this.state.configRevision,version:next.version,at:this.now()};this.state.configReceipts[id]=receipt;this.state.advice=null;this.save('configuration-applied',{id});return copy(receipt);
  }
  settleRequests(state,receipt) {
    for(const request of Object.values(this.state.requests))if(request.state==='pending')Object.assign(request,{state,receipt:copy(receipt),reviewedAt:this.now()});
    this.save('requests-reviewed');
  }
  async recoverPromotion() {
    const candidate=this.state.candidate;
    if(candidate?.state!=='unconfirmed'||candidate.native?.threadId!==this.state.binding.threadId||candidate.generation+1!==this.state.binding.generation)return null;
    if(!candidateHasProfileAuthority(candidate)||!candidateVerificationReady(candidate.verification,candidate))return {state:'waiting',reason:'candidate-profile-unverified'};
    return this.locked(async()=>{
      const boundary=safeBoundary({...await this.collect(),runtime:await this.inspect()});if(!boundary.safe)return {state:'waiting',reason:boundary.reason};
      const previous=candidate.previousBinding??this.state.segments.find(s=>s.generation===candidate.generation);
      try{const receipt=await this.promote({previous,binding:this.fence(),candidate});if(!receipt?.verified||receipt.threadId!==this.state.binding.threadId)throw Error('Promotion recovery unverified');candidate.state='retired';candidate.promotion=receipt;this.state.advice=null;this.settleRequests('complete',receipt);this.save('promotion-recovered');return {state:'complete',recovered:true,binding:this.fence()};}
      catch(error){candidate.error=error.name;this.save('promotion-recovery-waiting');return {state:'unconfirmed',reason:'promotion-recovery-pending'};}
    });
  }
  advise(advice,receipt,snapshotId,runtime) {
    if(!advice||!['keep','recall','compact','prepare','rotate','defer'].includes(advice.action)||!advice.reason?.trim()||!Array.isArray(advice.evidenceIds))throw Error('Invalid session advice');
    const native=receipt?.native_receipt;
    if(native) {
      // The main-session host already verifies final completion and the full profile.
      if(!runtime?.known||!native.native_turn_id||!native.verified_at||
        native.native_session_id!==this.state.binding.nativeSessionId||native.generation!==this.state.binding.generation||
        native.model!==runtime.model||native.provider!==runtime.modelProvider||native.reasoning!==runtime.reasoningEffort||
        receipt.request_id!==native.native_turn_id||receipt.model!==native.model||receipt.reasoning!==native.reasoning)
        throw Error('Session advice native receipt unverified');
    } else if(receipt?.model!=='deepseek-flash'||receipt.reasoning!=='high')throw Error('Session advice provider unverified');
    if(this.state.observation?.id!==snapshotId)return {state:'stale',reason:'observation-changed'};
    for(const finding of advice.findings??[]){
      const source=this.state.observation.recent?.find(i=>i.id===finding.sourceId&&i.role==='user'&&i.text?.includes(finding.quote));
      if(!source)throw Error('Session finding has no exact owner source');
      const id='session:'+hash([source.id,finding.kind]).slice(0,32);
      if(!this.state.evidence[id])this.evidence({id,sourceId:source.id,revision:source.revision,dependencies:source.dependencies??[],kind:finding.kind,at:Date.parse(source.at),basis:'owner-statement',accountBasis:'model-interpretation-of-owner-correction',quote:finding.quote,reason:finding.reason});
    }
    this.state.advice={...copy(advice),receipt:copy(receipt),snapshotId,at:this.now(),generation:this.state.binding.generation};
    this.save('advice',{action:advice.action});return {state:'recorded'};
  }
  async observe(runtime,context) {
    if(!this.state.config.observe)return;
    this.assertFence(this.state.binding);
    const last=this.state.compactions.findLast(c=>c.state==='complete');
    const content={binding:this.fence(),configVersion:context.configVersion,policyVersion:this.state.config.version,cursors:context.reviewCursors??context.cursors,profile:candidateProfile(runtime),pressure:windowPressure(runtime,this.state.config),
      lastCompaction:last?{id:last.id,completedAt:last.completedAt,before:last.before,after:last.after,origin:last.origin}:null,
      evidence:Object.values(this.state.evidence).filter(e=>e.generation===this.state.binding.generation&&!e.needsReview&&!e.resolved),
      tasks:(context.tasks??[]).map(t=>({id:t.id,inputVersion:t.inputVersion,status:t.status})),
      recent:(context.items??[]).slice(-8).map(i=>({id:i.id,role:i.role,at:i.at,revision:i.revision,dependencies:i.dependencies??[],...(i.text.length<=2000?{text:i.text}:{readId:i.sourceId,textOmitted:true})})),
      requested:Object.values(this.state.requests).filter(r=>r.state==='pending')};
    // Keep exact pressure available, without expiring a judgment for each token it used.
    const {inputTokens,expectedInputTokens,ratio,...pressure}=content.pressure;
    const id=hash({...content,pressure});
    const changed=this.state.observation?.id!==id||hash(this.state.observation?.pressure??null)!==hash(content.pressure);
    this.state.observation={...content,id,at:this.now()};
    if(changed)this.save('observation',{id});
    // Edge/event driven. Repeated minute ticks carry no new evaluation request.
    const level=content.pressure.level;
    const key=id;
    if((['elevated','critical'].includes(level)||content.evidence.length||content.requested.length)&&!this.state.events[key]) {
      this.state.events[key]={state:'pending',observationId:id};this.save('review-event',{key});
    }
    const pending=Object.entries(this.state.events).find(([,e])=>e.state==='pending');
    if(pending){const [key,event]=pending;await this.reviewRequested({id:key,observation:this.state.observation});event.state='queued';this.save('review-queued',{key});}
  }
  async tick() {
    if(this.running||this.closed)return {state:'busy'};this.running=true;
    try{
      const recovery=await this.recoverPromotion();if(recovery)return recovery;
      const runtime=await this.inspect(),context=await this.collect();await this.observe(runtime,context);
      const advice=this.state.advice;
      if(!this.state.config.observe||!advice||advice.snapshotId!==this.state.observation?.id||advice.generation!==this.state.binding.generation)return {state:'observing'};
      if(['keep','recall','defer'].includes(advice.action)){if(Object.values(this.state.requests).some(r=>r.state==='pending'))this.settleRequests('reviewed',advice);return {state:advice.action,reason:advice.reason};}
      if(advice.action==='compact')return await this.runCompaction(advice,context);
      const eligibility=rotationEligibility(this.state,advice,this.now());
      if(!eligibility.eligible)return {state:'waiting',reason:eligibility.reason};
      if(!await this.validateEvidence(eligibility.evidenceIds.map(id=>this.state.evidence[id])))return {state:'waiting',reason:'degradation-source-invalidated'};
      if(!this.state.config.prepare)return {state:'waiting',reason:'candidate-preparation-disabled'};
      return await this.prepare(advice,context);
    }finally{this.running=false;}
  }
  async runCompaction(advice,context) {
    if(!this.state.config.compact)return {state:'waiting',reason:'compaction-disabled'};
    const pending=this.state.compactions.findLast(c=>['running','unconfirmed'].includes(c.state));
    if(pending)return {state:'waiting',reason:'compaction-receipt-unconfirmed',operationId:pending.id};
    const last=this.state.compactions.findLast(c=>c.state==='complete');
    if(last&&this.now()-last.completedAt<this.state.config.compactCooldownMs)return {state:'waiting',reason:'observe-after-compaction'};
    // Checkpoint compression may call DS; it deliberately happens outside the coordinator.
    const budget=Math.max(this.state.config.restoreBudget,context.tasks?.length?4000:0);
    const checkpoint=await this.checkpoint(context,this.state.binding,budget);
    return this.locked(async()=>{
      const before=await this.inspect(),current=await this.collect();
      if(!this.state.config.compact)return {state:'waiting',reason:'compaction-disabled'};
      const boundary=safeBoundary({...current,runtime:before});if(!boundary.safe)return {state:'waiting',reason:boundary.reason};
      const invalid=validateCheckpoint(checkpoint,{binding:this.state.binding,cursors:current.cursors,configVersion:current.configVersion,budget});
      if(invalid)return {state:'waiting',reason:invalid};
      const id='compact:'+hash([this.fence(),advice.snapshotId]).slice(0,32);
      const operation={id,state:'running',generation:this.state.binding.generation,startedAt:this.now(),before:windowPressure(before,this.state.config),model:before.model,tasks:copy(current.tasks??[]),checkpoint};
      this.state.compactions.push(operation);this.save('compact-start',{id});
      try{
        const receipt=await this.compact(id),after=await this.inspect();
        if(!receipt?.completed||receipt.actual_session!==this.state.binding.threadId||after.model!==before.model||!after.known||hash((await this.collect()).tasks??[])!==hash(current.tasks??[]))throw Error('Compaction ownership unconfirmed');
        await this.ackCompact(receipt,checkpoint);
        operation.state='complete';operation.receipt=receipt;operation.completedAt=this.now();operation.after=windowPressure(after,this.state.config);
        this.state.advice=null;this.settleRequests('complete',receipt);this.save('compact-complete',{id});return {state:'complete',operationId:id};
      }catch(error){operation.state='unconfirmed';operation.error=error.name;this.save('compact-unconfirmed',{id});return {state:'unconfirmed',operationId:id};}
    });
  }
  async nativeCompaction(event) {
    if(event.state!=='completed'||!event.id||event.threadId!==this.state.binding.threadId)return {state:'ignored'};
    const at=Date.parse(event.at);
    const prior=this.state.compactions.find(c=>c.nativeEventId===event.id||c.id===event.operationId)||this.state.compactions.findLast(c=>c.startedAt<=at&&(!c.completedAt||at<=c.completedAt)&&c.generation===this.state.binding.generation);
    if(prior?.state==='complete'){if(prior.nativeEventId!==event.id){prior.nativeEventId=event.id;this.save('compaction-native-id-reconciled',{id:prior.id});}return {state:'deduplicated'};}
    const receipt={completed:true,actual_session:event.threadId,nativeEventId:event.id,operationId:event.operationId??event.id,at:event.at};
    // Native events are receipt evidence. They never become an owner interaction.
    await this.ackCompact(receipt,prior?.checkpoint);
    const op=prior??{id:event.operationId??event.id,generation:this.state.binding.generation};
    Object.assign(op,{state:'complete',nativeEventId:event.id,receipt,completedAt:Date.parse(event.at),origin:event.origin??'native'});
    if(!prior)this.state.compactions.push(op);
    this.state.advice=null;this.save('native-compact-complete',{id:op.id});return {state:'complete'};
  }
  async prepare(advice,context) {
    let candidate=this.state.candidate;
    if(retireLegacyCandidate(this.state)){this.save('legacy-candidate-retired',{id:candidate.id});await this.closeCandidate?.(candidate);candidate=this.state.candidate;}
    if(candidate?.state==='unconfirmed'){
      if(!candidateHasProfileAuthority(candidate))return {state:'waiting',reason:'candidate-operation-unconfirmed'};
      if(candidate.native&&candidate.injectionId&&await this.reconcileCandidate?.(candidate)){candidate.state='injected';this.save('candidate-injection-reconciled');}
      else return {state:'waiting',reason:'candidate-operation-unconfirmed'};
    }
    const fence=this.fence();
    const budget=Math.max(this.state.config.restoreBudget,context.tasks?.length?4000:0);
    const checkpoint=await this.checkpoint(context,fence,budget);
    const current=await this.collect();
    const invalid=validateCheckpoint(checkpoint,{binding:fence,cursors:current.cursors,configVersion:current.configVersion,budget});
    if(invalid)return {state:'waiting',reason:invalid};
    this.assertFence(fence);
    if(!candidate||['retired','failed','stale'].includes(candidate.state)){
      const runtime=await this.inspect(),profile=candidateProfile(runtime);
      if(!profile)return {state:'waiting',reason:'candidate-profile-unverified'};
      candidate={id:'rotation:'+randomUUID(),state:'creating',generation:fence.generation,checkpoint,profile,at:this.now()};
      this.state.candidate=candidate;this.save('candidate-creating',{id:candidate.id});
      try{candidate.native=await this.createCandidate({id:candidate.id,checkpoint,model:profile.model,profile:copy(profile)});if(!candidate.native?.threadId||!candidate.native?.nativeSessionId||!candidateHasProfileAuthority(candidate))throw Error('Missing native creation receipt');candidate.state='created';this.save('candidate-created',{id:candidate.id});}
      catch(error){candidate.state='unconfirmed';candidate.error=error.name;this.save('candidate-unconfirmed',{id:candidate.id});return {state:'unconfirmed'};}
    }
    if(candidate.checkpoint.id!==checkpoint.id){
      candidate.state='stale';this.state.retiredCandidates??=[];this.state.retiredCandidates.push(copy(candidate));this.save('candidate-checkpoint-invalidated');await this.closeCandidate?.(candidate);return {state:'waiting',reason:'candidate-checkpoint-changed'};
    }
    if(candidate.state==='created'){
      candidate.state='injecting';candidate.injectionId=hash([candidate.id,checkpoint.id]);this.save('candidate-injecting',{id:candidate.id});
      try{candidate.injection=await this.injectCandidate({...candidate.native,checkpoint,operationId:candidate.injectionId});if(!candidate.injection?.verified)throw Error('Injection unconfirmed');candidate.state='injected';this.save('candidate-injected',{id:candidate.id});}
      catch(error){candidate.state='unconfirmed';candidate.error=error.name;this.save('candidate-unconfirmed',{id:candidate.id});return {state:'unconfirmed'};}
    }
    const verification=await this.verifyCandidate({...candidate.native,checkpoint});
    if(!verification?.verified||verification.checkpointId!==checkpoint.id){candidate.state='waiting';this.save('candidate-verification-waiting');return {state:'waiting',reason:'candidate-continuity-unverified'};}
    if(!candidateVerificationReady(verification,candidate)){candidate.state='waiting';this.save('candidate-profile-unverified');return {state:'waiting',reason:'candidate-profile-unverified'};}
    candidate.state='ready';candidate.verification=verification;this.save('candidate-ready');
    if(advice.action!=='rotate'||!this.state.config.rotate)return {state:'ready',reason:'promotion-not-enabled-or-requested'};
    return this.locked(async()=>{
      const runtime=await this.inspect(),latest=await this.collect();
      const boundary=safeBoundary({...latest,runtime});if(!boundary.safe)return {state:'waiting',reason:boundary.reason};
      if((latest.tasks??[]).length&&(!this.state.config.pausedTaskHandover||!latest.tasks.every(t=>t.restartable&&verification.taskIds?.includes(t.id))))return {state:'waiting',reason:'work-checkpoint-required'};
      const invalid=validateCheckpoint(checkpoint,{binding:fence,cursors:latest.cursors,configVersion:latest.configVersion,budget});
      if(invalid)return {state:'waiting',reason:invalid};
      if(!rotationEligibility(this.state,advice,this.now()).eligible)return {state:'waiting',reason:'rotation-evidence-changed'};
      if(!await this.validateEvidence(advice.evidenceIds.map(id=>this.state.evidence[id])))return {state:'waiting',reason:'degradation-source-invalidated'};
      if(!sameCandidateProfile(candidate.profile,candidateProfile(runtime)))return {state:'waiting',reason:'candidate-profile-changed'};
      if(!this.state.config.prepare||!this.state.config.rotate)return {state:'ready',reason:'promotion-disabled'};
      candidate.state='committing';candidate.previousBinding=fence;this.save('promotion-start');
      const next={conversationId:fence.conversationId,generation:fence.generation+1,threadId:candidate.native.threadId,nativeSessionId:candidate.native.nativeSessionId};
      // This is the single durable commit point. On a later adapter failure we
      // roll forward; old generation no longer owns dispatch or transport.
      this.state.binding=next;this.state.segments.at(-1).state='retired';this.state.segments.push({...next,state:'active',activatedAt:this.now(),checkpointId:checkpoint.id});this.save('binding-committed',{rotationId:candidate.id});
      try{const receipt=await this.promote({previous:fence,binding:next,candidate});if(!receipt?.verified||receipt.threadId!==next.threadId)throw Error('Promotion unconfirmed');candidate.state='retired';candidate.promotion=receipt;this.state.advice=null;this.settleRequests('complete',receipt);this.save('promotion-complete');return {state:'complete',binding:next};}
      catch(error){candidate.state='unconfirmed';candidate.error=error.name;this.save('promotion-unconfirmed');return {state:'unconfirmed',binding:next};}
    });
  }
}
