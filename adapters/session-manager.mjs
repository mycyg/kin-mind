import fs from 'node:fs';
import {createHash,randomUUID} from 'node:crypto';
import {readJsonFile,claimPidLock,releasePidLock} from './atomic-json.mjs';
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
// Retention (AD1-14, K3-11): settled review events and finished compaction records leave
// after a week or past a count, oldest first. Nothing still open is ever dropped.
const RETENTION=Object.freeze({ms:7*86400000,events:64,compactions:50,candidates:10,assessments:32});
const SETTLED=new Set(['answered','rejected','superseded','executed']);
const HOUR=3600000;
// Observation fields whose change asks a review at any pressure (with the router's configuration
// revision, read from the cursors): tasks, the serving model, the full configuration, corrections.
const SEMANTIC_CAUSES=Object.freeze(['configVersion','policyVersion','profile','tasks','corrections']);
// A review job the mind holds for an attempt: still to answer (or answered, through the
// snapshot), or ended without an answer, which uses the attempt up.
const LIVE_REVIEW_JOB=new Set(['pending','running','batched','complete']),SPENT_REVIEW_JOB=new Set(['needs-repair','superseded']);
// How long the host waits on its own maintenance calls (N1-03) and on an unanswered review.
export const MAINTENANCE_LIMITS=Object.freeze({reviewTimeoutMs:2*HOUR,compactTimeoutMs:600000,compactCheckMs:300000,
  compactCheckTimeoutMs:30000,compactQuietMs:HOUR});
const withTimeout=(promise,ms)=>{let timer;return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('KIN_MAINTENANCE_TIMEOUT')),ms);})]).finally(()=>clearTimeout(timer));};
const retireLegacyCandidate=state=>{
  const candidate=state.candidate;
  if(!candidate||!['created','injected','ready','waiting'].includes(candidate.state)||candidateHasProfileAuthority(candidate))return false;
  candidate.state='stale';candidate.staleReason='legacy-profile-evidence-missing';state.retiredCandidates??=[];state.retiredCandidates.push(copy(candidate));return true;
};

/** One durable binding, used by every channel. Acquiring this lease is a host
 * operation. A second live host cannot silently steal dispatch or sending. */
export class SessionManager {
  constructor({file,binding,coordinator,inspect,collect,checkpoint,compact,reconcileCompact,ackCompact,createCandidate,injectCandidate,verifyCandidate,promote,closeCandidate,reconcileCandidate,validateEvidence=async()=>true,reviewRequested=async()=>{},now=()=>Date.now(),config={},limits={},lease=true}) {
    Object.assign(this,{file,coordinator,inspect,collect,checkpoint,compact,reconcileCompact,ackCompact,createCandidate,injectCandidate,verifyCandidate,promote,closeCandidate,reconcileCandidate,validateEvidence,reviewRequested,now});
    this.limits={...MAINTENANCE_LIMITS,...limits};
    this.tail=Promise.resolve();this.closed=false;
    if(lease)this.acquireLease();
    // A registry this process cannot open leaves the lease to the next one, not to its exit.
    try{this.openRegistry({file,binding,config,now});}catch(error){this.close();throw error;}
  }
  openRegistry({file,binding,config,now}) {
    const loaded=loadState(file,{now,validate:value=>Boolean(value.binding?.threadId&&value.binding.nativeSessionId)&&Array.isArray(value.segments)});
    // The binding is a fencing token. A registry with no readable revision left cannot
    // be reopened at generation 1: an older fence, durable in a manifest, would pass it
    // again. The quarantined bytes stay on disk, and restoring them is an operator step.
    if(!loaded.value&&loaded.recovery)throw Error('KIN_SESSION_REGISTRY_UNREADABLE');
    this.state=loaded.value??{schema:1,revision:0,binding:{conversationId:binding.conversationId??randomUUID(),generation:1,threadId:binding.threadId,nativeSessionId:binding.nativeSessionId},segments:[],config:{...SESSION_DEFAULTS,...config},compactions:[],evidence:{},events:{},requests:{},sessionAdvice:null};
    if(this.state.schema!==1||!this.state.binding.threadId||!this.state.binding.nativeSessionId)throw Error('Invalid conversation registry');
    this.saved=loaded.value?this.body():null;
    if(loaded.recovery)this.state.recovery=loaded.recovery;
    this.state.config={...SESSION_DEFAULTS,...this.state.config};
    if(!this.state.segments.length)this.state.segments.push({...this.state.binding,state:'active',activatedAt:null});
    // A crash cannot turn an uncertain create, compact or promotion into retry.
    for(const op of [this.state.candidate,...this.state.compactions])if(op&&['creating','injecting','committing','running'].includes(op.state))op.state='unconfirmed';
    // A caller that configures nothing (the migration's one commit) leaves the drift record alone.
    const legacyCandidateRetired=retireLegacyCandidate(this.state),retiredEvents=this.migrate(),drift=config===null?null:this.recordConfigDrift(config);
    this.prune();
    this.save('open',{...(legacyCandidateRetired?{candidateMigration:'legacy-profile-evidence-missing'}:{}),...(retiredEvents?{retiredLegacyEvents:retiredEvents}:{}),...(drift?{configDrift:drift}:{})});
  }
  body(){const {revision,...content}=this.state;return JSON.stringify(content);}
  /** Written only when something in it changed: a minute that changed nothing writes nothing. */
  save(kind,details={}) {
    const body=this.body();if(body===this.saved)return false;
    this.state.revision++;
    atomicJson(this.file,this.state,{previous:true});this.saved=body;
    fs.appendFileSync(this.file+'.events.jsonl',JSON.stringify({at:this.now(),revision:this.state.revision,kind,...details})+'\n',{mode:0o600});
    return true;
  }
  /** Registries written by earlier releases. The accepted judgment is `sessionAdvice`, and
   * review events keyed by a whole observation -- queued for good, 884 of them left by the
   * removed refresh: path -- can no longer be answered by anything. Returns how many went. */
  migrate() {
    const state=this.state;let retired=0;
    if(Object.hasOwn(state,'advice')){state.sessionAdvice??=state.advice;delete state.advice;}
    state.events??={};state.requests??={};
    for(const [key,event] of Object.entries(state.events))if(!event?.cause){delete state.events[key];retired++;}
    return retired;
  }
  /** AD1-21: the registry is the configuration of record, changed only by a sourced
   * `configure`; the host's configured values seed a new registry. A difference is kept
   * where the operator can see it instead of being dropped without a word. */
  recordConfigDrift(configured) {
    const keys=Object.keys(configured??{}).filter(key=>key!=='version'&&hash(configured[key]??null)!==hash(this.state.config[key]??null)).sort();
    if(!keys.length){delete this.state.configDrift;return null;}
    this.state.configDrift={keys,configured:Object.fromEntries(keys.map(key=>[key,configured[key]])),registry:Object.fromEntries(keys.map(key=>[key,this.state.config[key]??null]))};
    return keys;
  }
  prune() {
    const state=this.state,now=this.now(),old=at=>Number.isFinite(at)&&now-at>RETENTION.ms;
    let excess=Object.keys(state.events).length-RETENTION.events;
    for(const [key,event] of Object.entries(state.events).filter(([,e])=>SETTLED.has(e.state)).sort(([,a],[,b])=>(a.settledAt??a.at)-(b.settledAt??b.at)))
      if(excess>0||old(event.settledAt??event.at)){delete state.events[key];excess--;}
    const finished=state.compactions.filter(c=>['complete','failed'].includes(c.state)),drop=new Set(finished.slice(0,Math.max(0,finished.length-RETENTION.compactions)));
    if(drop.size){
      // Native events at or before the newest dropped record were already accounted for.
      const at=Math.max(...[...drop].filter(c=>c.generation===state.binding.generation).map(c=>c.nativeAt??(c.nativeEventId?c.completedAt:NaN)).filter(Number.isFinite));
      if(Number.isFinite(at))state.nativeFloor={generation:state.binding.generation,at:Math.max(at,state.nativeFloor?.generation===state.binding.generation?state.nativeFloor.at:0)};
      state.compactions=state.compactions.filter(c=>!drop.has(c));
    }
    if(state.retiredCandidates?.length>RETENTION.candidates)state.retiredCandidates=state.retiredCandidates.slice(-RETENTION.candidates);
    for(const [id,request] of Object.entries(state.requests))if(request.state!=='pending'&&old(request.reviewedAt??request.at))delete state.requests[id];
  }
  /** Whether a native compaction event has already been accounted for. */
  nativeSeen(event) {
    const floor=this.state.nativeFloor;
    return this.state.compactions.some(c=>c.nativeEventId===event.id)||Boolean(floor&&floor.generation===this.state.binding.generation&&Date.parse(event.at)<=floor.at);
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
  /** The hosts' one process lock (AD1-20), held for this manager's lifetime and
   * judged by pid and start time: a crashed host whose pid was handed on no longer
   * blocks the next start, and a second live host cannot take it. A lease left by a
   * crash is taken over; what that host left in flight the registry already marks
   * unconfirmed, and nothing of it is replayed. */
  acquireLease() {
    const file=this.file+'.lease',claimed=claimPidLock(file);
    if(claimed.state!=='held')throw Error('KIN_SESSION_MANAGER_ALREADY_RUNNING');
    this.leaseFile=file;this.leaseClaim=claimed.claim;
  }
  close(){this.closed=true;if(this.leaseClaim)releasePidLock(this.leaseFile,this.leaseClaim);}
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
    const receipt={id,sourceId,reason,changes,hash:hash(input),state:'applied',revision:this.state.configRevision,version:next.version,at:this.now()};this.state.configReceipts[id]=receipt;this.state.sessionAdvice=null;this.save('configuration-applied',{id});return copy(receipt);
  }
  /** Callers save: the revision that settles the requests is the one that answered them. */
  settleRequests(state,receipt) {
    for(const request of Object.values(this.state.requests))if(request.state==='pending')Object.assign(request,{state,receipt:copy(receipt),reviewedAt:this.now()});
  }
  async recoverPromotion() {
    const candidate=this.state.candidate;
    if(candidate?.state!=='unconfirmed'||candidate.native?.threadId!==this.state.binding.threadId||candidate.generation+1!==this.state.binding.generation)return null;
    if(!candidateHasProfileAuthority(candidate)||!candidateVerificationReady(candidate.verification,candidate))return {state:'waiting',reason:'candidate-profile-unverified'};
    return this.locked(async()=>{
      const boundary=safeBoundary({...await this.collect(),runtime:await this.inspect()});if(!boundary.safe)return {state:'waiting',reason:boundary.reason};
      const previous=candidate.previousBinding??this.state.segments.find(s=>s.generation===candidate.generation);
      try{const receipt=await this.promote({previous,binding:this.fence(),candidate});if(!receipt?.verified||receipt.threadId!==this.state.binding.threadId)throw Error('Promotion recovery unverified');candidate.state='retired';candidate.promotion=receipt;this.state.sessionAdvice=null;this.settleRequests('complete',receipt);this.save('promotion-recovered');return {state:'complete',recovered:true,binding:this.fence()};}
      catch(error){candidate.error=error.name;this.save('promotion-recovery-waiting');return {state:'unconfirmed',reason:'promotion-recovery-pending'};}
    });
  }
  /** The one-time move into the Kin runtime home (SPEC-v2 §1, step 8). The thread it binds was
   * built, given the checkpoint and verified before this is reached; this is the migration's
   * single commit point. Before it the old thread is untouched and still usable. After it the
   * generation only goes up and the old thread is never bound again. Asked again with the
   * same id, it answers with what it already did. */
  commitMigration({id,expected,native,codexHome,homeHash,checkpoint}) {
    const done=this.state.migration;
    if(done){
      if(done.id!==id)throw Error('KIN_MIGRATION_ALREADY_COMMITTED');
      if(done.binding.threadId!==native?.threadId)throw Error('KIN_MIGRATION_ID_CONFLICT');
      return copy(done);
    }
    if(!id||!native?.threadId||!native.nativeSessionId||!codexHome||!homeHash||!checkpoint?.id)throw Error('KIN_MIGRATION_INCOMPLETE');
    const fence=this.fence();
    if(['conversationId','generation','threadId','nativeSessionId'].some(key=>fence[key]!==expected?.[key]))throw Error('KIN_MIGRATION_BINDING_CHANGED');
    if(this.state.segments.some(s=>s.threadId===native.threadId))throw Error('KIN_MIGRATION_THREAD_REUSED');
    const candidate=this.state.candidate;
    if(['creating','injecting','committing','unconfirmed'].includes(candidate?.state))throw Error('KIN_MIGRATION_ROTATION_UNSETTLED');
    if(candidate){
      // A handover prepared for the old runtime goes with it; its record is kept.
      if(!['retired','failed','stale'].includes(candidate.state))Object.assign(candidate,{state:'stale',staleReason:'kin-home-migration'});
      this.state.retiredCandidates=[...(this.state.retiredCandidates??[]).filter(c=>c.id!==candidate.id),copy(candidate)];this.state.candidate=null;
    }
    const now=this.now(),next={conversationId:fence.conversationId,generation:fence.generation+1,threadId:native.threadId,nativeSessionId:native.nativeSessionId};
    this.state.binding=next;Object.assign(this.state.segments.at(-1),{state:'retired',retiredAt:now});
    this.state.segments.push({...next,state:'active',activatedAt:now,checkpointId:checkpoint.id,codexHome,homeHash,migrationId:id});
    this.state.migration={id,state:'committed',previous:fence,binding:next,codexHome,homeHash,checkpointId:checkpoint.id,committedAt:now};
    // The old window's judgment and pending restores stay with it. The checkpoint the new
    // thread was given is the one the read tool now serves.
    Object.assign(this.state,{sessionAdvice:null,restoreRequired:false,restorePending:false,restoreCheckpoint:copy(checkpoint),rollingCheckpoint:null,rollingCursor:null});
    this.save('migration-committed',{id,generation:next.generation});
    return copy(this.state.migration);
  }
  /** A judgment handed over by the appraisal that produced it, carried in the snapshot. */
  receive(record,runtime) {
    if(!record?.eventId||record.eventId===this.state.lastAdviceEvent)return null;
    this.state.lastAdviceEvent=record.eventId;
    try{return this.advise(record.decision,record.receipt,record.snapshotId,runtime,record.requestId??null);}
    catch(error){
      const event=this.state.events[record.snapshotId],now=this.now();
      if(event&&event.state!=='answered')Object.assign(event,{state:'rejected',reason:error.message,settledAt:now,nextAt:now+this.backoff(event.attempts)});
      this.save('advice-rejected',{eventId:record.eventId,reason:error.message});return {state:'rejected',reason:error.message};
    }
  }
  /** AD1-19: the host's own record of an assessment turn it saw complete, kept so that the
   * judgment a turn produced is reconciled with the turn itself, not only with its fields. */
  recordAssessment({requestId=null,turnId,forkThreadId=null}) {
    if(!turnId)return false;
    this.state.assessments=[...(this.state.assessments??[]).filter(a=>a.turnId!==turnId),{requestId,turnId,forkThreadId,at:this.now()}].slice(-RETENTION.assessments);
    return this.save('assessment-recorded',{turnId});
  }
  advise(advice,receipt,snapshotId,runtime,requestId=null) {
    if(!advice||!['keep','recall','compact','prepare','rotate','defer'].includes(advice.action)||!advice.reason?.trim()||!Array.isArray(advice.evidenceIds))throw Error('Invalid session advice');
    const native=receipt?.native_receipt;
    if(native) {
      // The main-session host already verifies final completion and the full profile.
      if(!runtime?.known||!native.native_turn_id||!native.verified_at||
        native.native_session_id!==this.state.binding.nativeSessionId||native.generation!==this.state.binding.generation||
        native.model!==runtime.model||native.provider!==runtime.modelProvider||native.reasoning!==runtime.reasoningEffort||
        receipt.request_id!==native.native_turn_id||receipt.model!==native.model||receipt.reasoning!==native.reasoning)
        throw Error('Session advice native receipt unverified');
      if(!(this.state.assessments??[]).some(a=>a.turnId===native.native_turn_id))throw Error('Session advice has no host assessment record');
    } else if(receipt?.model!=='deepseek-flash'||receipt.reasoning!=='high')throw Error('Session advice provider unverified');
    const now=this.now(),current=this.state.observation?.id===snapshotId,event=this.state.events[snapshotId];
    if(!current&&!event)return {state:'stale',reason:'observation-changed'};
    if(current)for(const finding of advice.findings??[]){
      const source=this.state.observation.recent?.find(i=>i.id===finding.sourceId&&i.role==='user'&&i.text?.includes(finding.quote));
      if(!source)throw Error('Session finding has no exact owner source');
      const id='session:'+hash([source.id,finding.kind]).slice(0,32);
      if(!this.state.evidence[id])this.evidence({id,sourceId:source.id,revision:source.revision,dependencies:source.dependencies??[],kind:finding.kind,at:Date.parse(source.at),basis:'owner-statement',accountBasis:'model-interpretation-of-owner-correction',quote:finding.quote,reason:finding.reason});
    }
    const answer={action:advice.action,requestId:requestId??event?.requestId??null,turnId:native?.native_turn_id??null,forkThreadId:native?.fork_thread_id??null,at:now};
    // A late answer still settles the question it was asked about: the same observation
    // coming back finds it answered instead of asking again at once.
    if(event)Object.assign(event,{state:'answered',answer,settledAt:now,nextAt:now+this.backoff(event.attempts)});
    if(!current){this.save('advice-late',{action:advice.action});return {state:'stale',reason:'observation-changed'};}
    this.state.sessionAdvice={...copy(advice),receipt:copy(receipt),snapshotId,at:now,generation:this.state.binding.generation,turnId:answer.turnId,forkThreadId:answer.forkThreadId};
    this.save('advice',{action:advice.action});return {state:'recorded'};
  }
  backoff(attempts){return Math.min(8*HOUR,this.state.config.compactCooldownMs*2**Math.max(0,attempts-1));}
  /** What a maintenance review answers (AD1-13, K3-06): the binding, the full configuration
   * and policy, the serving profile, the window and its pressure level, open tasks, owner
   * requests, sourced degradation and corrected sources. Dialogue that only grows, a
   * compaction that finished and memory entries an appraisal revised are recorded in the
   * observation and read by the next review, but do not by themselves ask one.
   *
   * Which of these ask one (spec §1, CR-RT-09): pressure above normal, sourced degradation
   * and owner requests, as before; and at any pressure a change of task, model, full
   * configuration or a corrected source. Token growth alone never does. A change of that
   * kind at normal pressure is asked about at most once per cooldown: the question is kept,
   * and a later change replaces it, rather than one review per change. */
  async observe(runtime,context) {
    if(!this.state.config.observe)return;
    this.assertFence(this.state.binding);
    const now=this.now(),generation=this.state.binding.generation,previous=this.state.observation;
    const last=this.state.compactions.findLast(c=>c.state==='complete');
    const pressure=windowPressure(runtime,this.state.config),{inputTokens,expectedInputTokens,ratio,...window}=pressure;
    const evidence=Object.values(this.state.evidence).filter(e=>e.generation===generation&&!e.needsReview&&!e.resolved);
    const requested=Object.values(this.state.requests).filter(r=>r.state==='pending');
    const cursors=context.reviewCursors??context.cursors;
    const cause={binding:this.fence(),configVersion:context.configVersion,policyVersion:this.state.config.version,config:cursors?.config??null,
      profile:candidateProfile(runtime),window,tasks:(context.tasks??[]).map(t=>({id:t.id,inputVersion:t.inputVersion,status:t.status})),
      requested:requested.map(r=>r.id).sort(),evidence:evidence.map(e=>[e.id,e.revision]),
      corrections:[...new Set([...(context.invalidatedSources??[]),...(context.linked?.needs_review_ids??[])])].sort()};
    const id=hash(cause);
    const content={binding:cause.binding,configVersion:cause.configVersion,policyVersion:cause.policyVersion,cursors,profile:cause.profile,pressure,
      lastCompaction:last?{id:last.id,completedAt:last.completedAt,before:last.before,after:last.after,origin:last.origin}:null,
      evidence,tasks:cause.tasks,corrections:cause.corrections,
      recent:(context.items??[]).slice(-8).map(i=>({id:i.id,role:i.role,at:i.at,revision:i.revision,dependencies:i.dependencies??[],...(i.text.length<=2000?{text:i.text}:{readId:i.sourceId,textOmitted:true})})),
      requested};
    // Exact token counts stay current in memory without a write for each token used.
    const stable=value=>value&&hash({...value,id:undefined,at:undefined,pressure:{...value.pressure,inputTokens:undefined,expectedInputTokens:undefined,ratio:undefined}});
    const changed=previous?.id!==id||stable(previous)!==stable(content);
    const semantic=Boolean(previous&&previous.binding?.generation===generation&&(SEMANTIC_CAUSES.some(key=>hash(previous[key]??null)!==hash(content[key]??null))||
      hash(previous.cursors?.config??null)!==hash(cursors?.config??null)));
    this.state.observation={...content,id,at:changed?now:previous.at};
    if(changed)this.save('observation',{id});
    const urgent=['elevated','critical'].includes(window.level)||evidence.length>0||requested.length>0;
    await this.review(id,{entered:previous?.id!==id,worth:urgent||semantic,spaced:!urgent&&semantic});
  }
  /** One review per cause. The same cause again is the same question: already answered,
   * or asked again only after a wait that doubles each time. */
  async review(cause,{entered,worth,spaced=false}) {
    const now=this.now(),events=this.state.events;
    if(entered)for(const other of Object.values(events))if(other.cause!==cause&&['pending','queued'].includes(other.state)){
      // An asked question is not asked again before its answer could have come back.
      if(other.state==='queued')other.nextAt=Math.max(other.nextAt??0,other.queuedAt+this.limits.reviewTimeoutMs);
      other.state='superseded';other.settledAt=now;
    }
    let event=events[cause];
    const advised=this.state.sessionAdvice?.snapshotId===cause&&this.state.sessionAdvice.generation===this.state.binding.generation;
    if(!event&&worth){
      const nextAt=spaced?Math.max(now,(this.state.semanticReviewAt??-Infinity)+this.state.config.compactCooldownMs):now;
      event=events[cause]={cause,state:'pending',attempts:0,failures:0,at:now,nextAt,...(spaced?{spaced:true}:{})};this.prune();
    }
    if(!event)return;
    if(event.state==='queued'&&now-event.queuedAt>=this.limits.reviewTimeoutMs)Object.assign(event,{state:'pending',unanswered:(event.unanswered??0)+1,nextAt:now+this.backoff(event.attempts)});
    else if(SETTLED.has(event.state)&&!advised&&worth&&now>=(event.nextAt??0))event.state='pending';
    if(event.state!=='pending'||now<event.nextAt)return;
    const attempt=event.attempts+1,requestId='review:'+cause.slice(0,24)+':'+attempt;
    const failed=reason=>{event.failures++;event.nextAt=now+(event.failures<2?0:Math.min(HOUR,60000*2**(event.failures-2)));this.save('review-request-failed',{cause,reason});};
    let job;
    try{job=await this.reviewRequested({id:requestId,cause,attempt,observation:this.state.observation});}
    catch(error){failed(error.message);throw error;}
    // What the mind actually holds for this attempt, not the fact that it was asked: a job
    // for another observation or attempt is no answer, and a failed one uses the attempt up.
    if(job?.requestId!==requestId||job.snapshotId!==cause||!LIVE_REVIEW_JOB.has(job.state)&&!SPENT_REVIEW_JOB.has(job.state)){failed('review-not-queued');return;}
    if(SPENT_REVIEW_JOB.has(job.state)){event.attempts=attempt;failed('review-job-'+job.state);return;}
    Object.assign(event,{state:'queued',attempts:attempt,failures:0,requestId,jobId:job.id??null,queuedAt:now});delete event.settledAt;
    if(event.spaced)this.state.semanticReviewAt=now;
    this.save('review-queued',{cause,requestId});
  }
  async tick({runtime:observed,context:collected}={}) {
    if(this.running||this.closed)return {state:'busy'};this.running=true;
    try{
      const recovery=await this.recoverPromotion();if(recovery)return recovery;
      const runtime=observed??await this.inspect(),context=collected??await this.collect();await this.observe(runtime,context);
      if(context.sessionAdvice)this.receive(context.sessionAdvice,runtime);
      const advice=this.state.sessionAdvice,event=this.state.events[this.state.observation?.id];
      if(!this.state.config.observe||!advice||advice.snapshotId!==this.state.observation?.id||advice.generation!==this.state.binding.generation)
        return {state:'observing',...(event?{review:event.state,...(event.state!=='queued'?{nextAt:event.nextAt}:{})}:{})};
      if(['keep','recall','defer'].includes(advice.action)){if(Object.values(this.state.requests).some(r=>r.state==='pending')){this.settleRequests('reviewed',advice);this.save('requests-reviewed');}return {state:advice.action,reason:advice.reason};}
      if(advice.action==='compact')return await this.runCompaction(advice,context);
      const eligibility=rotationEligibility(this.state,advice,this.now());
      if(!eligibility.eligible)return {state:'waiting',reason:eligibility.reason};
      if(!await this.validateEvidence(eligibility.evidenceIds.map(id=>this.state.evidence[id])))return {state:'waiting',reason:'degradation-source-invalidated'};
      if(!this.state.config.prepare)return {state:'waiting',reason:'candidate-preparation-disabled'};
      return await this.prepare(advice,context);
    }finally{this.running=false;}
  }
  /** Checkpoints are built once per cursor state (K3-05, K3-09, AD1-13): an unchanged
   * conversation is not rebuilt every minute, and an incomplete one is retried after a
   * growing wait instead of at every tick. */
  async preparedCheckpoint(context,binding,budget) {
    const key=hash([binding,context.cursors,context.configVersion,budget]),memo=this.checkpointMemo,now=this.now();
    if(memo?.key===key&&(memo.checkpoint.complete||now<memo.retryAt))return {checkpoint:memo.checkpoint,retryAt:memo.checkpoint.complete?null:memo.retryAt};
    const checkpoint=await this.checkpoint(context,binding,budget),failures=checkpoint?.complete?0:(memo?.key===key?memo.failures:0)+1;
    this.checkpointMemo={key,checkpoint,failures,retryAt:now+Math.min(HOUR,300000*2**Math.max(0,failures-1))};
    return {checkpoint,retryAt:null};
  }
  async runCompaction(advice,context) {
    if(!this.state.config.compact)return {state:'waiting',reason:'compaction-disabled'};
    const pending=this.state.compactions.findLast(c=>['running','unconfirmed'].includes(c.state)&&c.generation===this.state.binding.generation);
    if(pending&&await this.settleCompaction(pending)!=='settled')return {state:'waiting',reason:'compaction-receipt-unconfirmed',operationId:pending.id};
    const last=this.state.compactions.findLast(c=>c.state==='complete');
    // A cooldown says when the next compaction may run. It is not a judgment that none is needed.
    if(last&&this.now()-last.completedAt<this.state.config.compactCooldownMs)return {state:'waiting',reason:'observe-after-compaction',nextAt:last.completedAt+this.state.config.compactCooldownMs};
    // Checkpoint compression may call DS; it deliberately happens outside the coordinator.
    const budget=Math.max(this.state.config.restoreBudget,context.tasks?.length?4000:0);
    const {checkpoint,retryAt}=await this.preparedCheckpoint(context,this.state.binding,budget);
    if(retryAt)return {state:'waiting',reason:'checkpoint-coverage-incomplete',nextAt:retryAt};
    return this.locked(async()=>{
      const before=await this.inspect(),current=await this.collect();
      if(!this.state.config.compact)return {state:'waiting',reason:'compaction-disabled'};
      const boundary=safeBoundary({...current,runtime:before});if(!boundary.safe)return {state:'waiting',reason:boundary.reason};
      const invalid=validateCheckpoint(checkpoint,{binding:this.state.binding,cursors:current.cursors,configVersion:current.configVersion,budget});
      if(invalid)return {state:'waiting',reason:invalid};
      // A failed attempt is settled first; the next one has an ID of its own.
      const attempt=this.state.compactions.filter(c=>c.snapshotId===advice.snapshotId&&c.generation===this.state.binding.generation).length;
      const id='compact:'+hash(attempt?[this.fence(),advice.snapshotId,attempt]:[this.fence(),advice.snapshotId]).slice(0,32);
      const operation={id,state:'running',generation:this.state.binding.generation,snapshotId:advice.snapshotId,startedAt:this.now(),before:windowPressure(before,this.state.config),model:before.model,tasks:copy(current.tasks??[]),checkpoint};
      this.state.compactions.push(operation);this.save('compact-start',{id});
      // N1-03: a native compaction that fails may never answer. The host stops waiting and
      // leaves the coordinator; the operation stays unconfirmed, with the reason recorded,
      // until the native history or a status check settles it. It is never replayed.
      const pending=Promise.resolve().then(()=>this.compact(id));
      try{
        const receipt=await withTimeout(pending,this.limits.compactTimeoutMs);
        if(receipt?.failed===true&&!receipt.completed){
          operation.state='failed';operation.failure={reason:receipt.reason??'native-compaction-failed',at:this.now()};this.save('compact-failed',{id});return {state:'failed',operationId:id};
        }
        const after=await this.inspect();
        if(!receipt?.completed||receipt.actual_session!==this.state.binding.threadId||after.model!==before.model||!after.known||hash((await this.collect()).tasks??[])!==hash(current.tasks??[]))throw Error('Compaction ownership unconfirmed');
        await this.ackCompact(receipt,checkpoint);
        operation.state='complete';operation.receipt=receipt;operation.completedAt=this.now();operation.after=windowPressure(after,this.state.config);
        this.executed(advice.snapshotId);this.settleRequests('complete',receipt);this.save('compact-complete',{id});return {state:'complete',operationId:id};
      }catch(error){
        const timedOut=error.message==='KIN_MAINTENANCE_TIMEOUT';
        if(timedOut)pending.then(late=>this.lateReceipt(id,late),failure=>this.lateReceipt(id,null,failure));
        operation.state='unconfirmed';operation.error=error.name;
        operation.failure={reason:timedOut?'host-timeout':'compaction-error',...(timedOut?{timeoutMs:this.limits.compactTimeoutMs}:{}),at:this.now()};
        this.save(timedOut?'compact-timeout':'compact-unconfirmed',{id});return {state:'unconfirmed',operationId:id,...(timedOut?{reason:'compaction-timeout'}:{})};
      }
    });
  }
  /** The advice that asked for a compaction has been carried out: its question is open
   * again once the window has been observed for a cooldown after it. */
  executed(cause) {
    if(this.state.sessionAdvice?.snapshotId===cause)this.state.sessionAdvice=null;
    const event=this.state.events[cause],now=this.now();
    if(event)Object.assign(event,{state:'executed',settledAt:now,nextAt:now+this.state.config.compactCooldownMs});
  }
  /** A reply that arrived after the host stopped waiting is kept as evidence. It settles
   * nothing on its own: the native history and the status check do. */
  lateReceipt(id,receipt,error=null) {
    const operation=this.state.compactions.find(c=>c.id===id);if(!operation||this.closed)return;
    operation.lateReceipt={completed:Boolean(receipt?.completed),failed:receipt?.failed===true,...(error?{error:error.name}:{}),at:this.now()};
    try{this.save('compact-late-receipt',{id});}catch{/* The next save carries it. */}
  }
  /** An unconfirmed compaction is settled by evidence, never by a retry: the runtime's own
   * receipt for that ID, the native history (a completion there settles it on the next
   * tick), or -- after an hour with neither -- the conclusion that it never ran. */
  async settleCompaction(operation) {
    const now=this.now();
    if(operation.checkedAt&&now-operation.checkedAt<this.limits.compactCheckMs)return 'waiting';
    operation.checkedAt=now;
    let receipt=null;
    if(this.reconcileCompact)try{receipt=await withTimeout(Promise.resolve().then(()=>this.reconcileCompact(operation.id)),this.limits.compactCheckTimeoutMs);}catch{/* Unknown stays unknown. */}
    // The native history may have settled it while the status check was out.
    if(!['running','unconfirmed'].includes(operation.state))return 'settled';
    if(receipt?.completed&&receipt.actual_session===this.state.binding.threadId){
      await this.nativeCompaction({state:'completed',id:receipt.nativeEventId??operation.id,operationId:operation.id,threadId:receipt.actual_session,at:receipt.at??new Date(now).toISOString(),origin:'host-receipt'});
      return 'settled';
    }
    if(receipt?.failed===true||now-(operation.failure?.at??operation.startedAt)>=this.limits.compactQuietMs){
      operation.state='failed';operation.failure={...operation.failure,reason:receipt?.failed===true?'native-compaction-failed':'no-native-completion',settledAt:now};
      this.save('compact-failed',{id:operation.id});return 'settled';
    }
    this.save('compact-check',{id:operation.id});return 'waiting';
  }
  async nativeCompaction(event) {
    if(event.state!=='completed'||!event.id||event.threadId!==this.state.binding.threadId)return {state:'ignored'};
    const at=Date.parse(event.at);
    const prior=this.state.compactions.find(c=>c.nativeEventId===event.id||c.id===event.operationId)||this.state.compactions.findLast(c=>c.startedAt<=at&&(!c.completedAt||at<=c.completedAt)&&c.generation===this.state.binding.generation);
    if(prior?.state==='complete'){if(prior.nativeEventId!==event.id){prior.nativeEventId=event.id;prior.nativeAt=at;this.save('compaction-native-id-reconciled',{id:prior.id});}return {state:'deduplicated'};}
    const receipt={completed:true,actual_session:event.threadId,nativeEventId:event.id,operationId:event.operationId??event.id,at:event.at};
    // Native events are receipt evidence. They never become an owner interaction.
    await this.ackCompact(receipt,prior?.checkpoint);
    const op=prior??{id:event.operationId??event.id,generation:this.state.binding.generation};
    Object.assign(op,{state:'complete',nativeEventId:event.id,nativeAt:at,receipt,completedAt:at,origin:event.origin??'native'});
    if(!prior)this.state.compactions.push(op);
    if(prior?.snapshotId)this.executed(prior.snapshotId);
    // The judgment in force still stands: a finished compaction answers no question by
    // itself, and a compaction it asked for waits out the cooldown.
    this.prune();this.save('native-compact-complete',{id:op.id});return {state:'complete'};
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
    const {checkpoint,retryAt}=await this.preparedCheckpoint(context,fence,budget);
    if(retryAt)return {state:'waiting',reason:'checkpoint-coverage-incomplete',nextAt:retryAt};
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
      try{const receipt=await this.promote({previous:fence,binding:next,candidate});if(!receipt?.verified||receipt.threadId!==next.threadId)throw Error('Promotion unconfirmed');candidate.state='retired';candidate.promotion=receipt;this.state.sessionAdvice=null;this.settleRequests('complete',receipt);this.save('promotion-complete');return {state:'complete',binding:next};}
      catch(error){candidate.state='unconfirmed';candidate.error=error.name;this.save('promotion-unconfirmed');return {state:'unconfirmed',binding:next};}
    });
  }
}

/** The migration's commit made from outside a running host. Whoever holds the registry lease
 * is its only writer, so the host has been stopped first and this takes the lease for the one
 * write. A live holder is refused, never waited out or broken. */
export function commitRegistryMigration(file,request,{now=()=>Date.now()}={}) {
  if(!fs.existsSync(file))throw Error('KIN_SESSION_REGISTRY_MISSING');
  const manager=new SessionManager({file,binding:{},coordinator:{locked:fn=>fn()},config:null,now});
  try{return manager.commitMigration(request);}finally{manager.close();}
}
