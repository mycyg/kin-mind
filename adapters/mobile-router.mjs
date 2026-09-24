import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {writeJsonAtomic,readJsonFile,loadJson} from './atomic-json.mjs';
import {publicMobileRuntime,runtimeReply} from './mobile-controls.mjs';
import {conversationClock} from './conversation-time.mjs';
import {messageIntents} from './mobile-reviewer.mjs';
import {runtimeProfile,profileMatches,normalizeModelCatalog,resolveModelProfile} from './codex-models.mjs';
import {inputSettled,inputInFlight,inputSummary,unsettledView,emptySummary,ownerInput,answered,turnRunning,SUBMIT_RETRY_MS} from './input-ledger.mjs';

const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const clone = value => structuredClone(value);
const open = task => !['completed','canceled'].includes(task.status);
const settledDelivery = delivery => delivery.state==='accepted'?Boolean(delivery.messageId):
  ['rejected','undeliverable','retired','not-submitted','canceled-before-send'].includes(delivery.state);
const SHA256=/^[a-f0-9]{64}$/i;
const RECLASSIFICATION_EVIDENCE_KEYS=['acceptanceSha256','actualSessionId','conversationId','generation','id','ownerBindingSha256','sourceSha256','version'];
export const ROUTER_PROFILES = Object.freeze({
  chat:Object.freeze({model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'}),
  work:Object.freeze({model:'gpt-6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'}),
});
export const ROUTER_MODELS = Object.freeze({chat:ROUTER_PROFILES.chat.model,work:ROUTER_PROFILES.work.model});
const LEDGER_TAIL_BYTES=1024*1024;
/** The longest a second attempt at one classification may wait. */
export const CLASSIFY_RETRY_MAX_MS=45000;
export const NOTICE_SUPPRESSED='restart-restored-known-model';
/** KIN-ITER-20260918-02: a failed classification is retried on the SAME input, and
 * the budget — the first attempt plus this many asks in all — is durable. */
export const SEMANTIC_RETRY_BUDGET=4;
/** KIN-ITER-20260918-03: one notice id is sent at most this many times, and only
 * while the ground truth says nothing was submitted; a notice whose submission is
 * unknown is looked up at most this many times before it stops polling. */
export const NOTICE_SEND_BUDGET=5;
export const NOTICE_LOOKUP_BUDGET=10;
/** How long a freeze nobody lifts holds new dispatch. */
export const FREEZE_TTL_MS=2*3600000;
const frozenError=reason=>Object.assign(Error('Dispatch is frozen: '+reason),{code:'dispatch-frozen',retryable:true});
/** Why a classification attempt failed. The class is recorded, never collapsed
 * into one anonymous catch: timeout / http / parse / unavailable. */
export function classificationFailure(error) {
  const message=String(error?.message??error);
  if(error?.name==='TimeoutError'||error?.name==='AbortError'||/timeout|timed out/i.test(message))return 'timeout';
  if(/^deepseek-http-\d+/.test(message))return 'http';
  if(error?.name==='SyntaxError'||/invalid|unreadable|budget-exhausted/i.test(message))return 'parse';
  return 'unavailable';
}
/** How one receipt from the owner-bound sender reads for the notice pipeline.
 * `not-submitted` is ground truth that nothing reached the platform (no outbox
 * record, or one that proves no submission): the SAME notice id may be sent again,
 * bounded. `unknown` means the network effect cannot be told: the original id is
 * only ever looked up, never resent — a second message to the owner is worse. */
export function noticeReceiptClass(receipt) {
  if(!receipt||typeof receipt.state!=='string')return 'unknown';
  if(receipt.state==='accepted')return receipt.messageId?'accepted':'unknown';
  if(receipt.state==='rejected')return 'rejected';
  if(receipt.state==='not-started'||receipt.state==='not-submitted')return 'not-submitted';
  if(receipt.state==='pending'&&receipt.submissionStarted===false)return 'not-submitted';
  return 'unknown';
}
export function recentConversation(items) {
  const counts={user:0,assistant:0};
  return items.slice().reverse().filter(item=>Object.hasOwn(counts,item.role)&&++counts[item.role]<=8).reverse();
}
/** The classifier's own answer, told apart from the host's literal commands. */
export const CLASSIFIER_DECISION='deepseek-input-classification';
export const ATTACHMENT_KINDS=Object.freeze(['file','image','video','voice','other']);
const label=(value,max)=>typeof value==='string'&&value.trim().length>0&&!/[\p{Cc}]/u.test(value)?value.trim().slice(0,max):null;
/** What a classifier may learn about an attachment: kind, type, name, size. Whatever else
 * the host carries about it — a path, a URL, a key, the bytes themselves — is built out of
 * the request here, so no caller can widen it by passing a richer object. */
export function attachmentMetadata(list) {
  return (Array.isArray(list)?list:[]).slice(0,24).map(item=>({
    kind:ATTACHMENT_KINDS.includes(item?.kind)?item.kind:'other',
    ...(label(item?.mimeType,100)?{mimeType:label(item.mimeType,100)}:{}),
    ...(label(item?.name,120)?{name:label(item.name,120)}:{}),
    ...(Number.isSafeInteger(item?.bytes)&&item.bytes>=0?{bytes:item.bytes}:{})}));
}
export function modeCommand(text) {
  const value=text.trim().replace(/[~～!！。\s]+$/u,'');
  if (value==='/mode work') return 'work';
  if (value==='/mode auto') return 'auto';
  return null;
}
/** Every durable adapter state file is written through here: a temporary name no
 * other writer can share, fsync before the rename, and — for the files that must
 * survive a corrupt revision — the replaced one kept as `<file>.prev`. */
export function atomicJson(file,value,{previous=false}={}) {writeJsonAtomic(file,value,{previous,pretty:true});}

export const STATE_RECOVERY=Object.freeze({previous:'restored-from-previous-revision',none:'state-file-unreadable'});
/** Load one durable state file: the file itself, else the `.prev` copy the writer
 * keeps. An unreadable revision is moved to `quarantine/` beside it (never deleted)
 * and reported as a text-free fact for status. A file written under another schema
 * is reported to the caller, never moved: it is not damage. */
export function loadState(file,{schema=1,validate=()=>true,now=()=>Date.now()}={}) {
  const current=readJsonFile(file);
  if(current.state==='ok'&&Number.isSafeInteger(current.value?.schema)&&current.value.schema!==schema)return {value:current.value,source:'current'};
  const loaded=loadJson(file,{validate:value=>value?.schema===schema&&validate(value)===true,quarantine:path.join(path.dirname(file),'quarantine')});
  if(loaded.source==='current'||(loaded.source==='none'&&!loaded.quarantined.length))return {value:loaded.value,source:loaded.source};
  return {value:loaded.value,source:loaded.source,recovery:{at:now(),from:loaded.source,
    reason:loaded.source==='previous'?STATE_RECOVERY.previous:STATE_RECOVERY.none,quarantined:loaded.quarantined.map(name=>path.basename(name))}};
}

/** One host owns this durable state; all provider changes and input acceptance
 * share its mutex. MCP requests only record intent and never wait for a turn. */
export class MobileRouter {
  /** `replyTail` is the host's reply-tail port (`pending`, `decided`, `missed`, `stopped`), all
   * optional. It lets the unsent rest of an interrupted reply ride on the routing call this
   * router makes anyway; without it nothing here changes.
   * `classifyIntents` lets that same call carry what the owner wants done with the message —
   * whether to stop the running task, whether a file was asked for, what they call themselves
   * — and lets an owner message with attachments be classified instead of assumed to be work.
   * Left off, every request and every record is what it was before intents existed. */
  constructor({file,sessionId,inspect,switchModel,classify,waitForIdle,now=()=>Date.now(),binding=null,replyTail=null,classifyIntents=false,modelCatalog=null,resolveProfile=null,forceSwitch=null}) {
    Object.assign(this,{file,sessionId,inspect,switchModel,classify,waitForIdle,now,replyTail,classifyIntents:classifyIntents===true,modelCatalog,resolveProfile,forceSwitch});
    this.tail=Promise.resolve();this.inflight=new Map();this.progress=new Map();this.acceptance=new Map();this.reservations=new Map();
    const loaded=loadState(file,{now,validate:value=>typeof value.sessionId==='string'&&Boolean(value.tasks&&value.inputs&&value.requests)});
    this.state=loaded.value??{schema:1,sessionId,revision:0,mode:'auto',exitRequested:false,tasks:{},inputs:{},requests:{},history:[],recent:[],config:{classifierTimeoutMs:15000,auditIntervalHours:4},ledgerVersion:2};
    if(this.state.schema!==1)throw Error('Router schema mismatch');
    // A quarantined revision is never a silent fresh start: what happened is recorded,
    // and with nothing left to restore the accepted inputs come back from the journal.
    if(loaded.recovery){this.state.recovery=loaded.recovery;if(!loaded.value)this.state.recovery.restoredInputs=this.restoreLedger();}
    if(binding){
      if(this.state.conversationId&&this.state.conversationId!==binding.conversationId)throw Error('Router logical conversation mismatch');
      if(this.state.sessionId!==sessionId&&!this.state.conversationId)throw Error('Unmigrated router session mismatch');
      this.state.conversationId=binding.conversationId;this.state.generation=binding.generation;this.state.nativeSessionId=binding.nativeSessionId;this.state.sessionId=sessionId;
    }else if(this.state.sessionId!==sessionId)throw Error('Router session mismatch');
    this.state.configRevision??=0;this.state.notices??={};this.state.operations??={};this.state.semanticPending??={};this.state.reclassifications??={};
    this.state.executionEpoch??=0;this.state.forceBoundaries??=[];this.state.autoIdleProfile??=clone(ROUTER_PROFILES.chat);
    const at=now();
    // A native command cannot outlive the ACP process that ran it: it is interrupted,
    // never left counting as running work (AD1-03).
    for(const operation of Object.values(this.state.operations))if(['submitted','running','unconfirmed'].includes(operation.state)){operation.state='interrupted';operation.interruptedAt=at;}
    for(const request of Object.values(this.state.requests))if(request.forceState==='interrupting')request.forceState='unconfirmed';
    for(const notice of Object.values(this.state.notices))if(notice.state==='sending')notice.state='unconfirmed';
    // A classification attempt interrupted mid-call is retried, never concluded.
    for(const entry of Object.values(this.state.semanticPending))if(entry.state==='classifying')entry.state='retry';
    // An interrupted acceptance/switch cannot safely be replayed after restart.
    for(const record of Object.values(this.state.inputs)){
      // An interrupted acceptance cannot safely be replayed; an input that only
      // reached the in-memory session queue provably never reached the native session.
      if(record.state==='submitting')record.state='unconfirmed';
      if(record.state==='preparing')this.notSubmitted(record,'restart-before-submission',{restart:true});
      if(record.state==='queued')this.notSubmitted(record,'queued-prompt-never-started',{restart:true});
      // A notice whose send was cut off is looked up by its own id, never sent anew.
      if(record.ownerNotice?.state==='sending')record.ownerNotice.state='unknown';
      // A native turn cannot outlive the process that ran it.
      if(record.turnStartedAt&&!(record.turnEndedAt>=record.turnStartedAt)){record.turnEndedAt=at;record.stopReason='host-restart';}
    }
    delete this.state.turn;
    if(this.state.transition?.state==='switching')this.state.transition.state='unconfirmed';
    // KIN-FIX-20260924: records written before the ledger existed have no reply facts.
    // An accepted one is taken as answered; any other keeps its reason and id as
    // history and is never acted on, or counted as unsettled, again.
    if(this.state.ledgerVersion!==2) {
      for(const record of Object.values(this.state.inputs)) {
        if(record.state==='accepted')record.answer??={state:'legacy'};
        else if(!inputInFlight(record)&&record.state!=='superseded')record.historical={at,reason:'pre-ledger:'+record.state};
      }
      this.state.ledgerVersion=2;
    }
    this.save('startup');
  }
  save(kind,detail={}) {
    this.state.revision++;
    const event={at:this.now(),kind,...detail,revision:this.state.revision};
    this.state.history.push(event);
    this.state.history=this.state.history.slice(-200);
    atomicJson(this.file,this.state,{previous:true});
    fs.appendFileSync(this.file+'.events.jsonl',JSON.stringify(event)+'\n',{mode:0o600});
  }
  /** With no revision left to restore, the append-only journal still names every
   * input this router accepted. They come back unconfirmed, so a replayed input is
   * refused until it is reconciled instead of being submitted a second time — and
   * the restart profile waits rather than switching the provider under lost work. */
  restoreLedger() {
    let ids=[];
    try {
      const journal=this.file+'.events.jsonl',size=fs.statSync(journal).size,from=Math.max(0,size-LEDGER_TAIL_BYTES),handle=fs.openSync(journal,'r');
      let body='';
      try {const buffer=Buffer.alloc(size-from);fs.readSync(handle,buffer,0,buffer.length,from);body=buffer.toString('utf8');} finally {fs.closeSync(handle);}
      ids=[...new Set(body.split('\n').slice(from?1:0).map(line=>{
        try {const event=JSON.parse(line);return String(event?.kind).startsWith('input-')&&typeof event.id==='string'?event.id:null;} catch {return null;}
      }).filter(Boolean))];
    } catch {/* No journal: this router never accepted anything here. */}
    for(const id of ids)this.state.inputs[id]??={id,state:'unconfirmed',recovered:true,at:this.now()};
    return ids.length;
  }
  locked(fn) {
    const operation=this.tail.then(fn);this.tail=operation.catch(()=>{});return operation;
  }
  tasks() {return Object.values(this.state.tasks).filter(open);}
  snapshot() {return clone(this.state);}
  currentTask() {return this.tasks().at(-1);}
  adoptBinding(binding,actual) {
    if(binding.conversationId!==this.state.conversationId||binding.generation<=this.state.generation||actual.threadId!==binding.threadId||actual.nativeSessionId!==binding.nativeSessionId||!actual.known)throw Error('Unverified session promotion');
    this.sessionId=binding.threadId;this.state.sessionId=binding.threadId;this.state.nativeSessionId=binding.nativeSessionId;this.state.generation=binding.generation;this.state.actual=actual;
    this.save('session-promoted',{generation:binding.generation,threadId:binding.threadId});
  }
  busy(runtime,{assessment=false}={}) {
    if(Object.values(this.state.notices).some(n=>n.state==='sending'))return true;
    if(Object.values(this.state.operations).some(o=>['submitted','running','unconfirmed'].includes(o.state)))return true;
    return !runtime.known || runtime.sessionId!==this.sessionId || runtime.threadId!==this.sessionId ||
      runtime.nativeSessionId!==(this.state.nativeSessionId??this.sessionId) ||
      (runtime.nativeStatus!=='idle'&&!(assessment&&runtime.nativeStatus==='systemError')) || runtime.active ||
      runtime.queued>0 || runtime.backgroundTasks>0 || runtime.pendingDeliveries>0 || runtime.handoffTasks>0;
  }
  nativeBusy(runtime,{confirmedForce=false}={}) {
    const wrongNative=!runtime.known || runtime.sessionId!==this.sessionId || runtime.threadId!==this.sessionId ||
      runtime.nativeSessionId!==(this.state.nativeSessionId??this.sessionId) || runtime.nativeStatus!=='idle';
    // Once forceSwitch has confirmed `interrupted`/`idle`, `active` and
    // backgroundTasks may still describe the old coordinator sender/tool ledger.
    // They remain durable evidence, but no longer veto the hot switch.
    return wrongNative||!confirmedForce&&(runtime.active||runtime.backgroundTasks>0);
  }
  verified(runtime,profile) {
    return runtime.known&&runtime.profileReady!==false&&profileMatches(runtime,profile)&&
      runtime.sessionId===this.sessionId&&runtime.threadId===this.sessionId&&runtime.nativeSessionId===(this.state.nativeSessionId??this.sessionId);
  }
  // ---- The input ledger: the one definition of an unsettled input, its eight-state
  // summary, and the freeze a release or a segment swap puts on new dispatch.
  /** Every input with no outcome yet, by its original id and fact state. Inbox jobs
   * the router has not seen yet are named by the host through `received`. */
  unsettledInputs({received=[]}={}) {
    const now=this.now(),list=Object.values(this.state.inputs).filter(record=>!inputSettled(record)).map(record=>unsettledView(record,now));
    for(const job of received)if(!this.state.inputs[job.id])
      list.push({id:job.id,kind:job.kind??'owner',state:'received',summary:'received',inFlight:job.processing===true,at:job.at??null,ageMs:Number.isFinite(job.at)?Math.max(0,now-job.at):null});
    return list;
  }
  /** Owner inputs in the eight summary states. Records older than the ledger are
   * counted apart, and the host's own turns are summarised on their own. */
  summary({received=[]}={}) {
    const owner=emptySummary(),internal=emptySummary();let historical=0;
    for(const record of Object.values(this.state.inputs)) {
      const state=inputSummary(record);
      if(state==='historical'){historical++;continue;}
      (ownerInput(record)?owner:internal)[state]++;
    }
    for(const job of received)if(!this.state.inputs[job.id])owner.received++;
    return {...owner,historical,internal,frozen:this.frozen()?clone(this.state.freeze):null};
  }
  frozen() {
    const freeze=this.state.freeze;
    return Boolean(freeze&&!(Number.isFinite(freeze.until)&&freeze.until<=this.now()));
  }
  /** Stop new dispatch — both channels, the mind's turns, handoffs and mode changes
   * nobody forced — while in-flight work settles. The owner's literal stop and her
   * own mode commands still act. It survives a restart and lifts itself at `until`
   * (two hours unless asked otherwise), so a release or migration that is lost half
   * way can never leave the owner unanswered for good. Asking again for the same
   * migration changes nothing, so a caller may poll it while it drains. */
  freezeDispatch(reason,{ttlMs=FREEZE_TTL_MS,migrationId=null,by=null}={}) {
    return this.locked(async()=>{
      if(typeof reason!=='string'||!reason.trim())throw Error('A freeze needs a reason');
      const current=this.frozen()?this.state.freeze:null,id=migrationId?String(migrationId).slice(0,120):null;
      if(current&&current.reason===reason.trim().slice(0,200)&&(current.migrationId??null)===id)return clone(current);
      const ttl=Math.min(Math.max(Number.isFinite(ttlMs)?ttlMs:FREEZE_TTL_MS,60000),12*3600000);
      this.state.freeze={reason:reason.trim().slice(0,200),at:current?.at??this.now(),until:this.now()+ttl,...(id?{migrationId:id}:{}),...(by?{by:String(by).slice(0,120)}:{})};
      this.save('dispatch-frozen',{reason:this.state.freeze.reason,until:this.state.freeze.until,...(id?{migrationId:id}:{})});
      return clone(this.state.freeze);
    });
  }
  thawDispatch(reason='thawed',{migrationId=null}={}) {
    return this.locked(async()=>{
      const previous=this.state.freeze;if(!previous)return {state:'not-frozen'};
      delete this.state.freeze;this.save('dispatch-thawed',{reason:String(reason).slice(0,200),frozenAt:previous.at,...(migrationId?{migrationId:String(migrationId).slice(0,120)}:{})});
      return {state:'thawed',frozenAt:previous.at,...(previous.migrationId?{migrationId:previous.migrationId}:{})};
    });
  }
  async availableModels(runtime=null) {
    try{return normalizeModelCatalog(await this.modelCatalog?.(runtime)??[]);}catch{return [];}
  }
  async resolvedProfile(profile) {
    if(!profile||profile.model==='__unsupported__'||profile.reasoningEffort==='__unsupported__'||profile.serviceTierPreference==='__unsupported__')throw Error('Unsupported model profile');
    if(this.resolveProfile)return clone(await this.resolveProfile(clone(profile)));
    if(this.modelCatalog)return resolveModelProfile(profile,await this.modelCatalog());
    return clone(profile);
  }
  async switchTo(profile,{forceBoundary=null}={}) {
    const target=await this.resolvedProfile(profile);
    const actual=await this.switchModel(target.model,target,{forceBoundary:forceBoundary?clone(forceBoundary):null});
    actual.serviceTierPreference??=actual.fastMode==='on'||actual.fastMode===true?'fast':actual.fastMode==='off'||actual.fastMode===false?'default':null;
    return {actual,target};
  }
  automaticIdleProfile() {return clone(this.state.autoReturnProfile??this.state.autoIdleProfile??ROUTER_PROFILES.chat);}
  desiredProfile(runtime,{route=null}={}) {
    if(this.state.mode==='manual'&&this.state.manualProfile)return clone(this.state.manualProfile);
    if(this.tasks().length||this.state.mode==='work'||route==='work')return clone(ROUTER_PROFILES.work);
    return this.automaticIdleProfile()??runtimeProfile(runtime);
  }
  captureAutomaticReturn(runtime) {
    if(this.state.mode==='manual')return false;
    const complete=profile=>Boolean(profile?.provider&&profile?.providerKind&&profile?.model&&profile?.reasoningEffort&&profile?.serviceTierPreference);
    if(this.state.autoReturnProfile)return complete(this.state.autoReturnProfile);
    if(!this.verified(runtime,runtime.model))return false;
    const profile=runtimeProfile(runtime);if(!complete(profile))return false;
    this.state.autoReturnProfile=profile;this.state.autoIdleProfile=clone(profile);return true;
  }
  async reconcileTransition(runtime) {
    const transition=this.state.transition;
    if(transition?.state!=='unconfirmed'||this.busy(runtime))return runtime;
    // Pre-profile revisions persisted only model names. They can be reconciled
    // when the live runtime proves it is already at `to` or back at `from`, but
    // they do not authorize inventing an old effort/provider/tier and switching
    // to modern defaults.
    if(!transition.targetProfile&&transition.to&&this.verified(runtime,{model:transition.to})) {
      this.finishTransition(runtime);transition.state='applied-reconciled';this.state.actual=runtime;this.save('switch-reconciled',{model:runtime.model,legacy:true});return runtime;
    }
    if(!transition.fromProfile&&transition.from&&this.verified(runtime,{model:transition.from})) {
      this.finishTransition(runtime);transition.state='failed-restored';this.state.actual=runtime;this.save('switch-reconciled',{model:runtime.model,legacy:true});return runtime;
    }
    if(!transition.targetProfile||!transition.fromProfile) {
      if(transition.waitingReason!=='legacy-profile-unavailable') {transition.waitingReason='legacy-profile-unavailable';this.save('switch-reconciliation-pending',{model:runtime.model,reason:transition.waitingReason});}
      return runtime;
    }
    const expected=transition.targetProfile;
    if(expected?.model&&this.verified(runtime,expected)) {
      this.finishTransition(runtime);
      transition.state=profileMatches(runtime,transition.targetProfile??{model:transition.to})?'applied-reconciled':'failed-restored';
      this.state.actual=runtime;this.save('switch-reconciled',{model:runtime.model});return runtime;
    }
    // One recovery attempt restores the exact pre-switch profile. It never replays
    // an input whose native acceptance is uncertain, and never replaces a busy runtime.
    if(transition.recoveryAttemptedAt)return runtime;
    transition.recoveryAttemptedAt=this.now();this.save('switch-recovery-requested');
    try {
      const recovery=transition.fromProfile,{actual,target}=await this.switchTo(recovery);
      if(!this.verified(actual,target))throw Error('Unverified recovery');
      this.finishTransition(actual);transition.state='failed-restored';this.save('switch-recovered');return actual;
    } catch {this.save('switch-recovery-unconfirmed');return runtime;}
  }
  addTask(input) {
    let task=this.tasks().filter(item=>!item.cancelRequested).at(-1);
    if(!task) {
      const id='work-'+digest(input.id).slice(0,24);
      task={id,conversationId:this.state.conversationId,generation:this.state.generation,executionEpoch:this.state.executionEpoch,status:'running',requiresDelivery:!['repair','exploration-plan','proactive','assessment'].includes(input.kind),inputVersion:0,inputIds:[],summary:input.text,tools:{},deliveries:{},createdAt:this.now()};
      this.state.tasks[id]=task;
    }
    if(!task.inputIds.includes(input.id)) {this.supersedeDecline(task,'new-work-input:'+input.id);task.inputIds.push(input.id);task.inputVersion++;delete task.completion;}
    if(!input.kind||input.kind==='owner')task.requiresDelivery=true;
    return task;
  }
  supersedeDecline(task,reason) {
    if(task.completion?.outcome!=='declined')return;
    const request=this.state.requests[task.completion.commandId];
    if(request?.state==='pending'){
      request.state='superseded';request.supersededBy=reason;
      const latest=Object.values(this.state.requests).findLast(item=>item.state==='pending'&&['work','auto','manual'].includes(item.mode));
      this.state.requestedMode=latest?.mode??null;this.state.exitRequested=latest?.mode==='auto';
    }
    delete task.completion;
  }
  /** The owner's stop, from the literal command or from a classifier that actually
   * answered: recorded on the stop input itself, applied to the tasks it names. */
  applyStop(inputId,stop) {
    for(const task of this.tasks())if(!stop.taskIds||stop.taskIds.includes(task.id)){task.cancelRequested=true;task.cancelSourceInputId=inputId;}
  }
  /** The mind's own turns wait for the owner's work and for a coordinator that is busy. */
  internalHeld(runtime,kind) {
    return this.busy(runtime,{assessment:kind==='assessment'})||this.tasks().length>0||this.state.mode==='work';
  }
  async select(input) {
    const hash=digest([input.text,input.attachments??[]]);
    const owner=!input.kind||input.kind==='owner',intents=this.classifyIntents&&owner;
    // Phase one, under the mutex: identity, literal commands and everything that needs
    // no model. The classification itself never holds the mutex (AD1-06).
    const first=await this.locked(async()=>{
      const previous=this.state.inputs[input.id];
      if(previous) {
        if(previous.recovered)throw Error('Input acceptance requires reconciliation');
        if(previous.hash!==hash)throw Error('Input id reused with different content');
        if(['submitting','unconfirmed'].includes(previous.state))throw Error('Input acceptance requires reconciliation');
        // What predates the ledger keeps its reason and id and is never re-run.
        if(previous.historical)throw Object.assign(Error('Historical input is not re-run: '+previous.historical.reason),{code:'input-historical'});
        if(previous.state==='failed-before-submit'){
          if(this.frozen())throw frozenError(this.state.freeze.reason);
          previous.state='selected';previous.retry={...(previous.retry??{attempts:0}),lastAttemptAt:this.now()};
          delete previous.reason;delete previous.failureStage;delete previous.ownerNotice;
          this.save('input-preparation-retry',{id:input.id,attempt:previous.retry.attempts});
        }
        return {record:clone(previous)};
      }
      // The literal stop is read before any model is asked, and answers on its own when none can be.
      const stop=/^(?:停止任务|取消当前任务|\/停|\/acp-cancel)[!！。~～\s]*$/.test(input.text.trim());
      const command=stop?'stop':owner&&!input.attachments?.length?(modeCommand(input.text)??(input.text.trim()==='/compact'?'compact':null)):null;
      if(this.frozen()&&!(owner&&(stop||['work','auto'].includes(command))))throw frozenError(this.state.freeze.reason);
      const runtime=await this.inspect();
      const priorTask=this.currentTask();
      if(priorTask?.completion?.outcome==='declined'&&this.declineReady(priorTask,runtime)){
        priorTask.status='canceled';priorTask.canceledAt=this.now();this.save('decline-settled-before-input',{taskId:priorTask.id});
      }
      // A failed native turn is terminal, not active work. Only a new assessment
      // on the current profile may proceed; switching and other busy checks stay unchanged.
      if(['proactive','assessment'].includes(input.kind)&&this.internalHeld(runtime,input.kind))return {deferred:{state:'deferred',reason:'owner-work-held'}};
      let decision=null,reason;
      if(stop){decision=this.tasks().length?'work':'chat';reason='owner-stop-command';}
      else if(['work','auto','status','watch'].includes(command)){decision='control';reason='owner-runtime-'+command;}
      else if(command==='compact'){decision='maintenance';reason='native-compact';}
      // An owner message with attachments used to be work without anyone reading it. With
      // intents on it is classified like any other, with the attachment metadata in view.
      else if(input.attachments?.length&&!intents||['repair','work-result','exploration-plan','handoff'].includes(input.kind)){decision='work';reason='work-input';}
      else if(input.kind==='assessment'){decision='chat';reason='silent-main-assessment';}
      else if(input.kind==='proactive'){decision='chat';reason='casual-outreach';}
      if(decision===null)return {classify:{runtime,taskVersions:Object.fromEntries(this.tasks().map(t=>[t.id,t.inputVersion]))}};
      const recall={mode:'light',reason:'no-semantic-recall-decision'};
      if(stop) {
        const stopIntent={requested:'current_task',decisionSource:'literal-owner-command',taskIds:this.tasks().map(t=>t.id)};
        this.applyStop(input.id,stopIntent);
        return {decided:{decision,reason,command,recall,stopIntent,runtime}};
      }
      return {decided:{decision,reason,command,recall,force:['work','auto'].includes(command),runtime}};
    });
    if(first.record)return first.record;
    if(first.deferred)return first.deferred;
    // The reply tail never decides routing: a port that is absent, slow to answer or failing changes nothing here.
    const port=async(method,detail)=>{try{return await this.replyTail?.[method]?.(detail)??null;}catch{return null;}};
    let tail=null,offered=null,classified=false,semanticFailure=null,read=null;
    if(first.decided) {
      // A literal stop needs no model: whatever is still unsent is retired by the host itself.
      if(owner&&first.decided.command==='stop'&&this.replyTail)tail={carrier:'owner-stop',...(await port('stopped',{inputId:input.id}))};
      // Attachments and commands are never classified, so they cannot carry a tail decision either.
      else if(owner)await port('missed',{inputId:input.id,reason:'not-classified'});
    } else {
      // An interrupted reply with nothing unknown about it rides on this call. With none,
      // the classifier is asked exactly what it was always asked.
      classified=true;
      offered=owner?await port('pending',{id:input.id,text:input.text}):null;
      if(!offered?.reply)offered=null;
      const files=intents?attachmentMetadata(input.attachments):[];
      try {
        const result=await this.askClassification(input,{files,intents,offered,runtime:first.classify.runtime,wait:this.state.config.classifierTimeoutMs});
        read=this.readClassification(result,{owner,intents,allowStop:true});
        if(offered)tail=result.tail?.decision?{carrier:'classify',decision:result.tail.decision,...(await port('decided',{inputId:input.id,key:offered.key,tail:result.tail}))}
          :{carrier:'classify',state:'missed',...(await port('missed',{inputId:input.id,key:offered.key,reason:'no-tail-decision'}))};
      } catch(error) {
        // KIN-ITER-20260918-02 REVISES the old tradeoff in which a classification
        // anomaly always became work: the catch manufactured a provisional task and
        // a GPT switch out of a chat nobody had read. A failure now decides
        // nothing — no task, no provider switch, no execution grant — and the
        // input waits as itself for the bounded review, failure class on record.
        semanticFailure=classificationFailure(error);
        // The remainder waits for its next carrier: the review of the next reply, or a call of its own.
        if(offered)tail={carrier:'classify',state:'missed',...(await port('missed',{inputId:input.id,key:offered.key,reason:'classifier-unconfirmed'}))};
      }
    }
    // Phase two, under the mutex: the answer applies only against the basis it was read from.
    return this.locked(async()=>{
      if(this.state.inputs[input.id])return clone(this.state.inputs[input.id]);
      const base={id:input.id,hash,kind:input.kind??'owner',at:this.now(),conversationId:this.state.conversationId,generation:this.state.generation,nativeThreadId:this.sessionId,...(tail?{tail}:{})};
      if(semanticFailure) {
        const current=this.currentTask(),runtime=first.classify.runtime;
        this.state.semanticPending[input.id]={id:input.id,hash,kind:input.kind??'owner',text:input.text,
          attachments:intents?attachmentMetadata(input.attachments):[],occurredAt:input.occurredAt??null,receivedAt:input.receivedAt??null,
          conversationId:this.state.conversationId??null,generation:this.state.generation??null,
          taskId:current?.id??null,inputVersion:current?.inputVersion??null,taskVersions:first.classify.taskVersions,
          model:runtime.model??null,mode:this.state.mode,failure:{class:semanticFailure,at:this.now()},
          attempts:1,maxAttempts:SEMANTIC_RETRY_BUDGET,nextAttemptAt:this.now(),state:'pending',createdAt:this.now(),updatedAt:this.now()};
        const record={...base,state:'semantic-pending',route:null,intent:null,reason:'classification-'+semanticFailure,recall:{mode:'light',reason:'no-semantic-recall-decision'},command:null,taskId:null};
        this.state.inputs[input.id]=record;
        if(owner)this.rememberOwner(input);
        this.save('input-semantic-pending',{id:input.id,failure:semanticFailure});
        return clone(record);
      }
      let decided=first.decided;
      if(read) {
        // A stop read from a classification applies only to the tasks it was read against.
        const basisSame=JSON.stringify(Object.fromEntries(this.tasks().map(t=>[t.id,t.inputVersion])))===JSON.stringify(first.classify.taskVersions);
        const stopIntent=basisSame?read.stopIntent:null;
        if(stopIntent)this.applyStop(input.id,stopIntent);
        decided={...read,stopIntent};
      }
      const {decision,reason:why,command,recall,fileSend,stopIntent,profile,force}=decided;
      const locked=this.applyRouteLock(input,{decision,reason:why,intent:decision,command,stop:stopIntent});
      const modeControl=['manual','auto','work'].includes(command);
      const record={...base,state:'selected',route:locked.decision,intent:decision,reason:locked.reason,recall,command,taskId:locked.task?.id,
        ...(fileSend?{fileSend}:{}),...(stopIntent?{stop:stopIntent}:{}),...(profile?{profile}:{}),...(modeControl?{force}:{})};
      this.state.inputs[input.id]=record;
      if(owner)this.rememberOwner(input);
      this.save('input-selected',{id:input.id,route:locked.decision,reason:locked.reason,...(classified?{classified}:{})});return clone(record);
    });
  }
  /** One bounded ask of the classifier, with its own clock. The longest the call
   * may wait is the caller's; the answer is never rerouted by trigger words. */
  async askClassification(input,{files=[],intents=false,offered=null,runtime=null,wait}) {
    // The catalog is read before the clock starts, and the clock always has a
    // listener: a slow catalog can never leave a rejection nobody handles (AD1-05).
    const availableModels=await this.availableModels(runtime);
    let timer;
    const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('classification-timeout')),wait);});
    timeout.catch(()=>{});
    try {return await Promise.race([this.classify({text:input.text,clock:conversationClock(input,this.now()),recent:recentConversation(this.state.recent),task:this.currentTask()?.summary??null,mode:this.state.mode,currentProfile:this.state.manualProfile??(runtime?runtimeProfile(runtime):null),workHeld:Boolean(this.tasks().length||runtime?.active),availableModels,timeoutMs:wait,...(files.length?{attachments:files}:{}),...(intents?{intents:true}:{}),...(offered?{interruptedReply:offered.reply}:{})}),timeout]);}
    finally {clearTimeout(timer);}
  }
  /** A classification answer, validated and read within its bounds. It never owns
   * the execution lock and never changes state; it exists only when the classifier
   * actually answered. */
  readClassification(result,{owner,intents,allowStop=false}={}) {
    if(!['chat','work','control'].includes(result?.route))throw Error('Invalid classification');
    let command=null;
    if(result.route==='control'||result.control!==undefined) {
      if(!owner||!['status','watch','work','auto','manual'].includes(result.control)||result.route!=='control'&&!['manual','auto','work'].includes(result.control))throw Error('Invalid runtime control');
      command=result.control;
    }
    const decision=result.route,reason=result.reason?.slice(0,200)??'classification';
    let recall={mode:'light',reason:'no-semantic-recall-decision'},fileSend=null,stopIntent=null;
    // Only the bounded reading of the intents is kept, and only when they were asked for.
    const asked=intents?messageIntents(result):{};
    if(['light','deep'].includes(result.recall?.mode)) {
      const {owner_words:unbounded,...rest}=result.recall;
      recall={...rest,...(asked.ownerWords?{owner_words:asked.ownerWords}:{}),decisionSource:CLASSIFIER_DECISION};
    }
    if(asked.fileSend)fileSend={...asked.fileSend,decisionSource:CLASSIFIER_DECISION,at:this.now()};
    // A natural-language stop is the literal command's equal, and only that: the owner,
    // an open task, and a classifier that actually answered. Reading it changes
    // nothing: the caller applies it against the task basis it was read from (AD1-01).
    if(allowStop&&asked.stop==='current_task'&&this.tasks().length)
      stopIntent={requested:'current_task',decisionSource:CLASSIFIER_DECISION,taskIds:this.tasks().map(task=>task.id)};
    const profile=command==='manual'?clone(result.profile):null;
    if(command==='manual'&&(!profile?.model||!profile.reasoningEffort||!profile.serviceTierPreference))throw Error('Invalid model profile');
    if(result.force!==undefined&&typeof result.force!=='boolean')throw Error('Invalid force decision');
    // An explicit owner profile/mode control is immediate by default. `false` is
    // reserved for the semantic case where the owner expressly asked to wait.
    const force=owner&&['manual','auto','work'].includes(command)&&result.force!==false;
    return {decision,reason,command,recall,fileSend,stopIntent,profile,force};
  }
  /** DeepSeek judges meaning; its answer never owns the execution lock. Real work
   * keeps its model, tools and delivery lock; a chat that arrives while work is
   * open rides as context on the existing task, never as a new one. Only open work
   * the owner is not stopping holds the lock, never the model that happens to run. */
  applyRouteLock(input,{decision,reason,intent,command,stop=null}) {
    const current=this.tasks().filter(task=>!task.cancelRequested).at(-1)??null;
    if(!command&&(current||this.state.mode==='work')){decision='work';reason='work-lock: '+reason;}
    // The owner's stop is read by the task it stops; it never opens or extends work (AD1-01).
    const task=stop?this.tasks().filter(item=>stop.taskIds?.includes(item.id)).at(-1)??current:
      decision==='work'&&!command?(intent==='work'?this.addTask(input):current):command?null:current;
    if(task&&!task.inputIds.includes(input.id)){task.contextInputIds??=[];task.contextInputIds.push(input.id);}
    return {decision,reason,task};
  }
  rememberOwner(input) {
    this.state.recent.push({role:'user',text:input.text,at:input.occurredAt??input.at??this.now(),receivedAt:input.receivedAt??this.now()});
    this.state.recent=this.state.recent.slice(-16);
  }
  /** Record that nothing of this input reached the native session, with that
   * evidence and the moment the same id may be tried again (AD1-02, H2a-03). */
  notSubmitted(record,reason,{restart=false,stage='preparation'}={}) {
    // Handed only to the in-memory queue is not a submission: the fact is kept apart.
    if(record.state==='queued'&&record.submissionStartedAt){record.queuedSubmissionAt=record.submissionStartedAt;delete record.submissionStartedAt;}
    record.state='failed-before-submit';record.failureStage=stage;record.reason=reason;delete record.settledAt;
    const attempts=(record.retry?.attempts??0)+(restart?0:1),delay=SUBMIT_RETRY_MS[Math.max(0,attempts-1)];
    record.retry={attempts,evidence:'not-submitted',lastFailureAt:this.now(),...(delay===undefined?{exhausted:true}:{nextAt:this.now()+(restart?0:delay)})};
    return record.retry;
  }
  dispatch(input,submit) {
    if(this.inflight.has(input.id))return this.inflight.get(input.id);
    const pending=this.dispatchOnce(input,submit).catch(async error=>{
      const retry=await this.locked(()=>{
        const record=this.state.inputs[input.id];
        if(record&&['selected','preparing'].includes(record.state)&&!record.submissionStartedAt){
          const retry=this.notSubmitted(record,error.message);
          this.save('input-failed-before-submit',{id:input.id,attempt:retry.attempts});return retry;
        }
        return record?.state==='failed-before-submit'?record.retry:null;
      });
      // The caller learns that the same id may be tried again, and when.
      if(retry&&!error.code)Object.assign(error,{code:'input-not-submitted',inputId:input.id,retryAt:retry.nextAt??null,retryExhausted:Boolean(retry.exhausted)});
      throw error;
    }).finally(()=>{this.inflight.delete(input.id);this.reservations.delete(input.id);});
    this.inflight.set(input.id,pending);return pending;
  }
  /** Called under the router mutex by the existing minute review. A live dispatch
   * owns its wait; a submitted input owns its original reconciliation identity. */
  expireUnsubmittedInputs() {
    const cutoff=this.now()-(this.state.config.workReviewIntervalMinutes??20)*60000;
    for(const record of Object.values(this.state.inputs)) {
      if(!['selected','preparing'].includes(record.state)||record.submissionStartedAt||this.inflight.has(record.id)||!(record.at<=cutoff))continue;
      this.notSubmitted(record,'input-preparation-timeout');
      this.save('input-failed-before-submit',{id:record.id,reason:record.reason});
    }
  }
  async dispatchOnce(input,submit) {
    const selected=await this.select(input);
    if(selected.state==='deferred')return {route:'deferred',reason:selected.reason};
    if(['accepted','queued','superseded'].includes(selected.state))return {route:'deduplicated',model:selected.model};
    // A dispatch waits for the coordinator, a switch or a classification, never for
    // ever: past its deadline nothing has been submitted, and the same id is retried
    // or reported like any other unsubmitted input.
    const deadline=this.now()+(this.state.config.dispatchWaitMinutes??20)*60000;
    for(;;) {
      // A semantic wait is settled by its own bounded review — driven here while
      // the caller is still alive, and by the host's pump when it is not.
      if(this.state.inputs[input.id]?.state==='semantic-pending')await this.reviewSemanticPending();
      const plan=await this.locked(async()=>this.planDispatch(input));
      if(plan.outcome)return plan.outcome;
      if(!plan.wait) {
        const done=await this.submitPlanned(input,plan,submit);
        if(!done.retry)return done;
      }
      if(this.now()>=deadline)throw Error('dispatch-wait-exceeded:'+(this.state.inputs[input.id]?.waitingReason??'coordinator-busy'));
      await this.waitForIdle();
    }
  }
  /** Phase one of a dispatch, under the mutex: controls, the provider switch and a
   * reservation that keeps any other dispatch from switching the provider until this
   * one has been handed to the session. */
  async planDispatch(input) {
    const record=this.state.inputs[input.id];
    if(record.state==='semantic-pending')return {wait:true};
    if(['accepted','queued','superseded'].includes(record.state))return {outcome:{route:'deduplicated',model:record.model}};
    if(record.state!=='selected')throw Error('Input acceptance requires reconciliation');
    let runtime=await this.reconcileTransition(await this.inspect());
    if(['proactive','assessment'].includes(input.kind)&&(this.busy(runtime,{assessment:input.kind==='assessment'})||this.tasks().length||this.state.mode==='work'))return {outcome:{route:'deferred',reason:'owner-work-held'}};
    if(record.route==='control'||['manual','auto','work'].includes(record.command)) {
      const request=await this.acceptControl(record,runtime);
      const applied=request?await this.applyModeRequest(request,runtime):{runtime};
      runtime=applied.runtime??runtime;
      if(record.route==='control') {
        record.state='accepted';record.acceptedAt=this.now();
        this.save('control-accepted',{id:input.id,command:record.command});
        return {outcome:{route:'host-control',model:runtime.model,state:request?.state}};
      }
      // Mixed requests execute their work only after the exact requested
      // profile is verified. Failure never silently runs it on the old model.
      if(request?.state==='pending')return {wait:true};
      if(request?.state!=='applied') {
        // The owner's own control failed and was reported; the work is not re-run on another model.
        this.notSubmitted(record,'model-control-'+(request?.state??'failed'),{stage:'model-control'});record.retry.exhausted=true;delete record.retry.nextAt;
        this.save('input-failed-before-submit',{id:input.id});
        return {outcome:{route:'control-failed',state:request?.state,model:runtime.model}};
      }
    }
    if(record.route==='work'&&['manual','auto','work'].includes(record.command)&&!record.taskId)record.taskId=this.addTask(input).id;
    if(record.route==='maintenance'&&this.busy(runtime))return {wait:true};
    let targetProfile=record.route==='maintenance'||input.kind==='assessment'||record.unlabeled?runtimeProfile(runtime):this.desiredProfile(runtime,{route:record.route});
    if(input.kind==='assessment'&&(!runtime.known||runtime.profileReady===false))return {outcome:{route:'deferred',reason:'native-profile-unconfirmed'}};
    // A native runtime that cannot be read, or a switch still being reconciled, is a
    // wait: nothing has been submitted, and nothing is failed for it (AD1-02).
    if(!runtime.known||this.state.transition?.state==='unconfirmed'){this.noteWait(record,!runtime.known?'native-runtime-unknown':'switch-unconfirmed');return {wait:true};}
    if(record.route==='work'&&this.state.mode!=='manual'&&!this.captureAutomaticReturn(runtime))throw Error('Automatic return profile unverified');
    if(record.route!=='maintenance'&&!record.unlabeled&&(this.tasks().length||this.state.mode==='work')&&this.state.mode!=='manual')targetProfile=clone(ROUTER_PROFILES.work);
    try {targetProfile=await this.resolvedProfile(targetProfile);}
    catch(error) {if(/Canonical model provider unavailable|catalog/i.test(String(error?.message))){this.noteWait(record,'model-catalog-unavailable');return {wait:true};}throw error;}
    let target=targetProfile.model;
    // A queued request must not change the provider mid-turn, nor under another
    // dispatch that is planned but not yet handed to the session.
    const otherReserved=[...this.reservations.keys()].some(id=>id!==input.id);
    if((!this.verified(runtime,targetProfile)||runtime.profileReady===false)&&(this.busy(runtime)||otherReserved))return {wait:true};
    if(!this.verified(runtime,targetProfile)||runtime.profileReady===false) {
      this.startTransition(runtime,targetProfile,record.reason,'input',input.id);this.save('switch-requested');
      try {
        const switched=await this.switchTo(targetProfile),actual=switched.actual;targetProfile=switched.target;target=targetProfile.model;
        if(!this.verified(actual,targetProfile))throw Error('Provider verification failed');
        this.finishTransition(actual);this.save('switch-applied',{model:target});
      } catch {
        // No owner input has been submitted. Restore the exact profile observed
        // before this attempt; ambiguous restoration remains held.
        try {
          const current=await this.inspect();if(this.busy(current))throw Error('busy');
          const restoredResult=await this.switchTo(this.state.transition.fromProfile),restored=restoredResult.actual;
          if(!this.verified(restored,restoredResult.target))throw Error('restore-unconfirmed');
          this.finishTransition(restored);this.state.transition.state='failed-restored';target=restored.model;targetProfile=restoredResult.target;
          // Only work opens work: a chat that ran on the restored profile stays a chat (AD1-04).
          if(!record.taskId&&!record.command&&record.route==='work')record.taskId=this.addTask(input).id;
          this.save('switch-failed-restored');
        } catch {this.state.transition.state='unconfirmed';this.save('switch-unconfirmed');throw Error('Provider switch requires reconciliation');}
      }
    } else this.state.actual=runtime;
    if(record.route==='work'&&!record.taskId&&!record.command&&this.currentTask())record.taskId=this.currentTask().id;
    if(input.kind==='proactive'&&this.state.mode!=='manual'&&target!==ROUTER_MODELS.chat)return {outcome:{route:'deferred',reason:'deepseek-not-verified'}};
    record.state='preparing';record.model=target;record.executionEpoch=this.state.executionEpoch;delete record.waitingReason;this.save('input-preparing',{id:input.id});
    record.plannedTransition=this.state.transition?.id??null;
    this.reservations.set(input.id,{profile:targetProfile,at:this.now()});
    return {decision:{model:target,profile:targetProfile,taskId:record.taskId,intent:record.intent,reason:record.reason,command:record.command,inputId:record.id,
      inputVersion:record.taskId?this.state.tasks[record.taskId].inputVersion:null,turnFence:this.state.executionEpoch}};
  }
  /** Phase two: the host prepares its prompt outside the mutex (AD1-06), then marks
   * the submission under it, re-checking the basis the dispatch was planned on. */
  async submitPlanned(input,plan,submit) {
    let marking=null;
    const markSubmitted=()=>marking??=this.locked(async()=>{
      const record=this.state.inputs[input.id];
      // Any provider change since the plan means planning again, never submitting on a stale basis.
      if(record.state!=='preparing'||record.executionEpoch!==this.state.executionEpoch||(this.state.transition?.id??null)!==record.plannedTransition||this.state.transition?.state==='switching')
        throw Object.assign(Error('dispatch-basis-changed'),{dispatchRetry:true});
      record.state='submitting';record.submissionStartedAt=this.now();
      // A native command exists from its submission, never before it (AD1-03).
      if(record.command==='compact')this.state.operations[record.id]={inputId:record.id,kind:'compact',state:'submitted',at:this.now()};
      this.save('input-submitting',{id:input.id});
    });
    if(input.submissionProtocol!=='host-boundary-v1')await markSubmitted().catch(()=>{});
    let outcome,failure=null;
    if(!marking||await marking.then(()=>true,error=>{failure=error;return false;})) {
      try {outcome=await submit(plan.decision,markSubmitted);} catch(error) {failure=error;}
      if(marking)await marking.catch(error=>{failure??=error;});
    }
    return this.locked(async()=>{
      this.reservations.delete(input.id);
      const record=this.state.inputs[input.id],target=plan.decision.model;
      if(failure) {
        if(failure.dispatchRetry&&record.state==='preparing'){record.state='selected';this.save('input-dispatch-replanned',{id:input.id});return {retry:true};}
        const operation=this.state.operations[record.id];
        if(record.submissionStartedAt&&record.state==='submitting') {
          record.state='unconfirmed';record.failureStage='native-submit';
          // The command's own outcome is unknown; it is closed, never left running (AD1-03).
          if(operation?.state==='submitted')Object.assign(operation,{state:'failed',stopReason:'submit-outcome-unknown',updatedAt:this.now()});
          this.save('input-unconfirmed',{id:input.id});
          throw Error('Input acceptance requires reconciliation',{cause:failure});
        }
        if(record.state!=='preparing')throw Error('Input acceptance requires reconciliation',{cause:failure});
        const retry=this.notSubmitted(record,String(failure?.message??failure));
        this.save('input-failed-before-submit',{id:input.id,attempt:retry.attempts});
        throw Object.assign(Error('Input preparation failed before native submission',{cause:failure}),{code:'input-not-submitted',inputId:input.id,retryAt:retry.nextAt??null,retryExhausted:Boolean(retry.exhausted)});
      }
      const route=typeof outcome==='string'?outcome:outcome?.route;
      if(route==='superseded') {if(this.state.operations[record.id])this.state.operations[record.id].state='canceled';record.state='superseded';record.settledAt=this.now();this.save('input-superseded',{id:input.id});return{route,model:target};}
      // Handed only to the session's in-memory queue is not yet native acceptance:
      // the input is accepted when its prompt begins (H2a-02).
      if(outcome?.queued===true&&!record.turnStartedAt){record.state='queued';record.queuedAt=this.now();this.save('input-queued',{id:input.id});return {route,model:target,queued:true};}
      record.state='accepted';record.acceptedAt??=this.now();
      if(route==='steered'&&this.state.turn&&!this.state.turn.inputIds.includes(record.id)){record.turnStartedAt=this.state.turn.startedAt;this.state.turn.inputIds.push(record.id);}
      this.save('input-accepted',{id:input.id,route});
      return{route,model:target};
    });
  }
  noteWait(record,reason) {
    if(record.waitingReason===reason)return;
    record.waitingReason=reason;this.save('input-waiting',{id:record.id,reason});
  }
  /** Resolves when a queued input's prompt begins in the native session, or with its
   * state when the session let it go first or the host is stopping. */
  awaitAcceptance(inputId) {
    const record=this.state.inputs[inputId];
    if(record?.state!=='queued')return Promise.resolve({state:record?.state??'missing'});
    let entry=this.acceptance.get(inputId);
    if(!entry){let resolve;const promise=new Promise(r=>{resolve=r;});entry={promise,resolve};this.acceptance.set(inputId,entry);}
    return entry.promise;
  }
  settleAcceptance(inputId,value) {
    const entry=this.acceptance.get(inputId);if(!entry)return;
    this.acceptance.delete(inputId);entry.resolve(value);
  }
  releaseAcceptance(reason='host-stopping') {for(const id of [...this.acceptance.keys()])this.settleAcceptance(id,{state:'released',reason});}
  /** KIN-ITER-20260918-02: the bounded review of inputs whose classification
   * failed. The SAME input is asked of DeepSeek again — never rescored by trigger
   * words — and a late answer routes only after its version basis, cancel state
   * and the generation (lease) it was captured under have been checked. Whatever
   * the budget cannot settle becomes an explicit failed state the ops and reply
   * chains can see; the input is never silently swallowed. */
  async reviewSemanticPending() {
    for(const id of Object.keys(this.state.semanticPending)) {
      const entry=await this.locked(async()=>{
        const e=this.state.semanticPending[id];
        if(!e||!['pending','retry'].includes(e.state)||e.nextAttemptAt>this.now())return null;
        const record=this.state.inputs[id];
        if(!record||record.state!=='semantic-pending'||record.hash!==e.hash) {
          e.state='superseded';e.updatedAt=this.now();delete e.text;this.save('semantic-review-superseded',{id});return null;
        }
        if(e.attempts>=e.maxAttempts)return clone({...e,exhausted:true});
        e.state='classifying';e.attempts++;e.updatedAt=this.now();this.save('semantic-retry',{id,attempt:e.attempts});
        return clone(e);
      });
      if(!entry)continue;
      let result=null,failure=null;
      if(!entry.exhausted) {
        try {result=await this.askClassification({id:entry.id,kind:entry.kind,text:entry.text,occurredAt:entry.occurredAt,receivedAt:entry.receivedAt},
          {files:entry.attachments??[],intents:this.classifyIntents&&entry.kind==='owner',wait:Math.min((this.state.config.classifierTimeoutMs??15000)*2,CLASSIFY_RETRY_MAX_MS)});}
        catch(error){failure=classificationFailure(error);}
      }
      await this.locked(async()=>{
        const e=this.state.semanticPending[id];if(!e||!['classifying','pending','retry'].includes(e.state))return;
        const record=this.state.inputs[id];e.updatedAt=this.now();
        // With the budget spent the owner's message is still hers: it goes to Kin
        // unlabelled, on the current profile, instead of ending unanswered (AD1-02).
        const unlabeled=()=>{
          e.state='failed';delete e.text;
          Object.assign(record,{state:'selected',route:'chat',intent:null,unlabeled:true,reason:'classification-unavailable',failureClass:e.failure.class,taskId:null,lateClassifiedAt:this.now()});
          this.save('input-unlabeled',{id,failure:e.failure.class,attempts:e.attempts});
        };
        const fail=cls=>{
          e.lastFailure={class:cls,at:this.now()};
          if(e.attempts>=e.maxAttempts)return unlabeled();
          e.state='retry';e.nextAttemptAt=this.now()+(e.attempts-1)*15000;this.save('semantic-retry-waiting',{id,attempt:e.attempts,failure:cls});
        };
        if(!record||record.state!=='semantic-pending'||record.hash!==e.hash){e.state='superseded';delete e.text;this.save('semantic-review-superseded',{id});return;}
        if(entry.exhausted)return unlabeled();
        if((this.state.conversationId??null)!==e.conversationId||(this.state.generation??null)!==e.generation) {
          // The session moved on while this was read; the owner's message did not. It is read again in the new one.
          e.conversationId=this.state.conversationId??null;e.generation=this.state.generation??null;
          Object.assign(record,{conversationId:e.conversationId,generation:e.generation,nativeThreadId:this.sessionId});
          e.state='retry';e.nextAttemptAt=this.now();this.save('semantic-rebased',{id,generation:e.generation});return;
        }
        if(failure)return fail(failure);
        const owner=e.kind==='owner',intents=this.classifyIntents&&owner;
        // A stop intent read late applies only to the exact task basis it was read from.
        const basisSame=JSON.stringify(Object.fromEntries(this.tasks().map(t=>[t.id,t.inputVersion])))===JSON.stringify(e.taskVersions);
        let read;
        try{read=this.readClassification(result,{owner,intents,allowStop:basisSame});}catch(error){return fail(classificationFailure(error));}
        // The late answer takes the same path as a prompt one: its own stop is recorded on
        // it and applied now, and late work never extends a task the owner is stopping —
        // it opens work of its own instead. Nothing is retired unanswered (AD1-01).
        if(read.stopIntent)this.applyStop(id,read.stopIntent);
        const locked=this.applyRouteLock({id:e.id,kind:e.kind,text:e.text},{decision:read.decision,reason:read.reason,intent:read.decision,command:read.command,stop:read.stopIntent});
        Object.assign(record,{state:'selected',route:locked.decision,intent:read.decision,reason:locked.reason,recall:read.recall,command:read.command,
          taskId:locked.task?.id??null,lateClassifiedAt:this.now(),...(read.fileSend?{fileSend:read.fileSend}:{}),...(read.stopIntent?{stop:read.stopIntent}:{}),...(read.profile?{profile:read.profile}:{}),...(['manual','auto','work'].includes(read.command)?{force:read.force}:{})});
        if(!basisSame)record.lateBasis={captured:e.taskVersions,current:Object.fromEntries(this.tasks().map(t=>[t.id,t.inputVersion]))};
        e.state='classified';e.updatedAt=this.now();delete e.text;
        this.save('input-classified-late',{id,route:locked.decision,reason:locked.reason,attempts:e.attempts});
      });
    }
  }
  async requestMode(request) {
    return this.locked(async()=>{
      return this.recordModeRequest(request);
    });
  }
  /** Recover one already accepted owner message whose original semantic route was
   * wrong. The private host authenticates the source and asks the current DS
   * classifier; this boundary validates and records only cryptographic evidence
   * plus the bounded control decision. It never dispatches the old input again. */
  async reclassifyAcceptedControl({commandId,sourceInputId,sourceHash,expectedRevision,evidence,decision}) {
    return this.locked(async()=>{
      const exactId=(value,max=200)=>{const v=label(value,max);if(v!==value)throw Error('Invalid reclassification identifier');return v;};
      commandId=exactId(commandId);sourceInputId=exactId(sourceInputId);
      if(!SHA256.test(sourceHash??'')||!Number.isSafeInteger(expectedRevision)||expectedRevision<0||!evidence||typeof evidence!=='object')throw Error('Invalid reclassification basis');
      if(JSON.stringify(Object.keys(evidence).sort())!==JSON.stringify(RECLASSIFICATION_EVIDENCE_KEYS))throw Error('Invalid reclassification evidence fields');
      const summary={id:exactId(evidence.id),version:evidence.version,sourceSha256:evidence.sourceSha256,acceptanceSha256:evidence.acceptanceSha256,
        ownerBindingSha256:evidence.ownerBindingSha256,actualSessionId:exactId(evidence.actualSessionId),conversationId:exactId(evidence.conversationId),generation:evidence.generation};
      if(!Number.isSafeInteger(summary.version)||summary.version<1||!Number.isSafeInteger(summary.generation)||summary.generation<1||
        !SHA256.test(summary.sourceSha256??'')||!SHA256.test(summary.acceptanceSha256??'')||!SHA256.test(summary.ownerBindingSha256??''))throw Error('Invalid reclassification evidence');
      const encoded=JSON.stringify(decision);if(!decision||typeof decision!=='object'||encoded.length>8000)throw Error('Invalid reclassification decision');
      const basisHash=digest({commandId,sourceInputId,sourceHash,expectedRevision,evidence:summary,decision});
      const receipts=Object.values(this.state.reclassifications);
      const collision=receipts.find(receipt=>receipt?.commandId===commandId||receipt?.sourceInputId===sourceInputId||receipt?.evidence?.id===summary.id);
      if(collision) {
        if(collision.basisHash!==basisHash)throw Error('Reclassification receipt conflict');
        return {receipt:clone(collision),request:clone(this.state.requests[collision.requestId])};
      }
      if(this.state.requests[commandId])throw Error('Reclassification command id conflict');
      if(expectedRevision!==this.state.revision)throw Error('Router revision changed; reauthenticate source evidence');
      const source=this.state.inputs[sourceInputId];
      if(!source||source.kind!=='owner'||source.state!=='accepted')throw Error('Reclassification source is not an accepted owner input');
      // `sourceHash` is the router's semantic input hash. `sourceSha256` names the
      // private host's authenticated source evidence and is deliberately a separate
      // digest: the public adapter can validate its shape without pretending both
      // byte streams were identical.
      if(source.hash!==sourceHash)throw Error('Reclassification source hash mismatch');
      if(summary.actualSessionId!==this.sessionId||summary.actualSessionId!==source.nativeThreadId||summary.conversationId!==this.state.conversationId||summary.conversationId!==source.conversationId||summary.generation!==this.state.generation||summary.generation!==source.generation)throw Error('Reclassification source identity mismatch');
      const inputs=Object.values(this.state.inputs),sourceIndex=inputs.indexOf(source);
      const newerControl=inputs.slice(sourceIndex+1).some(input=>input.kind==='owner'&&input.state==='accepted'&&['manual','auto','work'].includes(input.command));
      const newerReceipt=receipts.some(receipt=>(receipt.recordedAt??Infinity)>=(source.at??-Infinity));
      if(newerControl||newerReceipt)throw Error('Reclassification evidence is stale');
      const read=this.readClassification(decision,{owner:true,intents:false,allowStop:false});
      if(!['manual','auto','work'].includes(read.command))throw Error('Reclassification must be a model control');
      const profile=read.command==='manual'?await this.resolvedProfile(read.profile):null;
      const id='reclassification-'+digest([this.sessionId,sourceInputId,summary.id,summary.version]).slice(0,40);
      const modeRequest={commandId,mode:read.command,reason:'Verified owner control reclassification: '+read.reason,sourceInputId,sourceHash,notify:true,force:read.force,reclassificationId:id,...(profile?{profile}: {})};
      const receipt={id,version:1,state:'recorded',basisHash,basisRevision:expectedRevision,commandId,requestId:commandId,modeRequestState:'pending',sourceInputId,sourceHash,originalRoute:source.route,evidence:summary,
        decision:{control:read.command,force:read.force,reason:read.reason,...(profile?{profile}: {})},requestHash:digest(modeRequest),recordedAt:this.now()};
      this.state.reclassifications[id]=receipt;
      try {this.recordModeRequest(modeRequest);}
      catch(error){delete this.state.reclassifications[id];throw error;}
      receipt.modeRequestState=this.state.requests[commandId].state;receipt.updatedAt=this.now();this.save('input-reclassification-requested',{id,commandId});
      const runtime=await this.reconcileTransition(await this.inspect());
      await this.applyModeRequest(this.state.requests[commandId],runtime);
      receipt.modeRequestState=this.state.requests[commandId].state;receipt.updatedAt=this.now();this.save('input-reclassification-applied',{id,commandId,state:receipt.modeRequestState});
      return {receipt:clone(receipt),request:clone(this.state.requests[commandId])};
    });
  }
  recordModeRequest(request) {
      if(!request.commandId||!['work','auto','manual'].includes(request.mode)||!request.reason?.trim()||(request.mode==='manual'&&!request.profile?.model))throw Error('Invalid mode request');
      const taskOutcome=request.taskOutcome??'completed';
      if(!['completed','declined'].includes(taskOutcome)||request.taskOutcome!==undefined&&!request.completedTaskId)throw Error('Invalid task outcome');
      if(taskOutcome==='declined'&&(request.mode!=='auto'||!request.completedTaskId||!Number.isSafeInteger(request.completedInputVersion)||request.handoff))throw Error('Decline requires the current task and input version');
      const hash=digest(request), previous=this.state.requests[request.commandId];
      if(previous) {if(previous.hash!==hash)throw Error('Command id conflict');return clone(previous);}
      if(request.expectedRevision!==undefined&&request.expectedRevision!==this.state.configRevision)throw Error('Router configuration revision changed; read current state');
      if(request.completedTaskId&&(!this.state.tasks[request.completedTaskId]||!open(this.state.tasks[request.completedTaskId])))throw Error('Task is not open');
      if(request.completedTaskId&&request.completedInputVersion!==this.state.tasks[request.completedTaskId].inputVersion)throw Error('Task input version changed; read current runtime');
      if(taskOutcome==='declined'){
        const task=this.state.tasks[request.completedTaskId];
        if(this.currentTask()?.id!==task.id||!task.turnStartedAt||task.turnEndedAt||task.executionEpoch!==this.state.executionEpoch)throw Error('Decline must belong to the current native task turn');
      }
      const sourceInputId=taskOutcome==='declined'?request.sourceInputId:
        request.sourceInputId??Object.values(this.state.inputs).filter(i=>i.kind==='owner').at(-1)?.id;
      if(taskOutcome==='declined'&&!sourceInputId)throw Error('Decline requires the current source input');
      if(request.handoff) {
        const task=this.addTask({id:'handoff:'+request.commandId,text:request.handoff});
        task.handoff={id:request.commandId,text:request.handoff,state:'pending'};
      }
      if(request.mode==='work') {
        this.state.exitRequested=false;
      } else if(request.mode==='auto') {
        this.state.exitRequested=true;
        if(request.completedTaskId) {
          const task=this.state.tasks[request.completedTaskId];
          task.completion={inputVersion:task.inputVersion,turnFence:task.executionEpoch,at:this.now(),summary:request.reason,outcome:taskOutcome,
            ...(taskOutcome==='declined'?{commandId:request.commandId,turnStartedAt:task.turnStartedAt,sourceInputId}:{})};
        }
      }
      const source=sourceInputId?this.state.inputs[sourceInputId]:null;
      const reclassification=request.reclassificationId?this.state.reclassifications[request.reclassificationId]:null;
      const reclassificationAuthorized=Boolean(reclassification?.state==='recorded'&&reclassification.commandId===request.commandId&&
        reclassification.sourceInputId===sourceInputId&&reclassification.sourceHash===source?.hash&&reclassification.originalRoute===source?.route&&
        reclassification.decision?.control===request.mode&&reclassification.requestHash===hash&&request.sourceHash===source?.hash);
      // A classified owner control authorizes only the exact request assembled by
      // acceptControl: same owner source/hash, owner-mode command id, mode, force
      // choice and (for manual mode) catalog-resolved profile. An old `auto` source
      // can therefore never be repurposed as force authority for an arbitrary model.
      const directAuthorized=Boolean(source?.kind==='owner'&&source.state==='selected'&&['chat','work','control'].includes(source.route)&&
        request.commandId==='owner-mode:'+source.id&&request.mode===source.command&&request.sourceHash===source.hash&&
        source.controlRequestHash===hash&&['manual','auto','work'].includes(source.command));
      const directForce=directAuthorized&&source.force===true;
      const directDefer=directAuthorized&&source.force===false;
      const forceAuthorized=request.force===true&&(directForce||reclassificationAuthorized&&reclassification.decision.force===true);
      const deferAuthorized=request.force===false&&(directDefer||reclassificationAuthorized&&reclassification.decision.force===false);
      const keepManual=request.mode==='auto'&&request.completedTaskId&&this.state.mode==='manual'&&this.state.manualProfile;
      this.state.configRevision++;
      const result={state:'pending',mode:keepManual?'manual':request.mode,commandId:request.commandId,hash,reason:request.reason,revision:this.state.configRevision,
        sourceInputId,...(request.sourceHash?{sourceHash:request.sourceHash}:{}),notify:request.notify===true,force:forceAuthorized,deferUntilSettled:deferAuthorized,...(request.reclassificationId?{reclassificationId:request.reclassificationId}:{}),...((keepManual||request.profile)?{profile:clone(keepManual||request.profile)}:{}),...(request.completedTaskId?{taskId:request.completedTaskId,inputVersion:request.completedInputVersion,taskOutcome}:{}),at:this.now()};
      for(const prior of Object.values(this.state.requests))if(prior.state==='pending'&&['work','auto','manual'].includes(prior.mode)){
        if(prior.mode===request.mode&&prior.notify){result.notify=true;result.notificationOrigin=prior.notificationOrigin??prior.commandId;result.notificationSubscribers=[...new Set([...(prior.notificationSubscribers??[]),prior.commandId])];}
        prior.state='superseded';prior.supersededBy=request.commandId;
      }
      this.state.requests[request.commandId]=result;this.state.requestedMode=result.mode;this.save('mode-request',{commandId:request.commandId,mode:result.mode,force:forceAuthorized});
      return clone(result);
  }
  async applyPendingMode() {
    return this.locked(async()=>{
      const runtime=await this.reconcileTransition(await this.inspect());
      const unfenced=this.unfencedForce();
      if(unfenced&&!this.nativeBusy(runtime)) {
        const boundary=await this.forceBoundary(unfenced,runtime);
        if(boundary&&unfenced.result){unfenced.result.forceBoundaryId=boundary.id;this.save('applied-force-fenced',{commandId:unfenced.commandId,boundaryId:boundary.id});}
      }
      const request=Object.values(this.state.requests).findLast(r=>r.state==='pending'&&['work','auto','manual'].includes(r.mode));
      if(!request)return{state:'pending'};
      if(this.frozen()&&!request.force)return clone(request);
      await this.applyModeRequest(request,runtime);return clone(request);
    });
  }
  unfencedForce() {
    const boundaryAt=this.state.forceBoundaries.at(-1)?.at??-Infinity;
    return Object.values(this.state.requests).findLast(request=>{
      if(request.force!==true||request.forceState!=='unconfirmed'||request.forceBoundary||
        !Number.isFinite(request.forceStartedAt)||request.forceStartedAt<=boundaryAt||
        (request.state==='applied'&&request.result?.sessionId!==this.sessionId)||
        request.result?.sessionId&&request.result.sessionId!==this.sessionId)return false;
      const source=this.state.inputs[request.sourceInputId];
      return source?.kind==='owner'&&source.hash===request.sourceHash&&source.nativeThreadId===this.sessionId&&
        (!this.state.conversationId||source.conversationId===this.state.conversationId)&&
        (!this.state.generation||source.generation===this.state.generation);
    })??null;
  }
  async forceBoundary(request,runtime) {
    if(!request.force)return null;
    if(request.forceBoundary)return request.forceBoundary;
    if(request.forceState==='unconfirmed'&&!this.nativeBusy(runtime)) {
      if(this.unfencedForce()!==request)return null;
      return this.applyForceFence(request,{state:'idle',reconciled:true,checkedAt:runtime.checkedAt});
    }
    if(request.forceState==='unconfirmed')return null;
    if(!this.forceSwitch){this.failModeRequest(request,'force-switch-unavailable','没有切换：当前宿主不能安全中断正在进行的任务。');this.save('force-switch-failed',{commandId:request.commandId,reason:'force-switch-unavailable'});return null;}
    request.forceState='interrupting';request.forceStartedAt=this.now();this.save('force-switch-requested',{commandId:request.commandId});
    let receipt;
    try {receipt=await this.forceSwitch({sessionId:this.sessionId,commandId:request.commandId,sourceInputId:request.sourceInputId,reclassificationId:request.reclassificationId??null,fromEpoch:this.state.executionEpoch,toEpoch:this.state.executionEpoch+1});}
    catch {request.forceState='unconfirmed';request.waitingReason='force-interrupt-unconfirmed';this.pendingModeNotice(request);this.save('force-switch-unconfirmed',{commandId:request.commandId});return null;}
    if(!['interrupted','idle'].includes(receipt?.state)) {
      if(receipt?.state==='failed'){this.failModeRequest(request,receipt?.reason??'force-interrupt-failed','没有切换：当前任务未能安全中断。');this.save('force-switch-failed',{commandId:request.commandId,reason:request.failureReason});}
      else {request.forceState='unconfirmed';request.waitingReason=receipt?.reason??'force-interrupt-unconfirmed';this.pendingModeNotice(request);this.save('force-switch-unconfirmed',{commandId:request.commandId});}
      return null;
    }
    return this.applyForceFence(request,receipt);
  }
  applyForceFence(request,receipt) {
    if(request.forceBoundary)return request.forceBoundary;
    const fromEpoch=this.state.executionEpoch,toEpoch=fromEpoch+1,at=this.now();
    const interruptedTask=receipt.state==='interrupted'?((receipt.taskId&&this.state.tasks[receipt.taskId])??this.currentTask()):null;
    const boundary={id:'force-'+digest([this.sessionId,request.commandId,fromEpoch]).slice(0,24),commandId:request.commandId,sourceInputId:request.sourceInputId,fromEpoch,toEpoch,at,taskId:interruptedTask?.id??null,receipt:clone(receipt)};
    this.state.executionEpoch=toEpoch;this.state.forceBoundaries.push(boundary);this.state.forceBoundaries=this.state.forceBoundaries.slice(-32);
    for(const operation of Object.values(this.state.operations))if(['submitted','running','unconfirmed'].includes(operation.state)){operation.state='fenced-unconfirmed';operation.fencedBy=boundary.id;operation.fencedAt=at;}
    for(const input of Object.values(this.state.inputs))if(['submitting','unconfirmed'].includes(input.state)){input.state='fenced-unconfirmed';input.fencedBy=boundary.id;input.fencedAt=at;}
    if(this.state.turn){for(const id of this.state.turn.inputIds){const input=this.state.inputs[id];if(input?.turnStartedAt&&!(input.turnEndedAt>=input.turnStartedAt)){input.turnEndedAt=at;input.stopReason='owner-force-interrupt';}}delete this.state.turn;}
    if(interruptedTask) {
      if(interruptedTask.completion) {
        const historical={...clone(interruptedTask.completion),turnFence:interruptedTask.completion.turnFence??interruptedTask.executionEpoch,state:'historical-proposal',fencedBy:boundary.id,fencedAt:at};
        interruptedTask.completionHistory??=[];interruptedTask.completionHistory.push(historical);interruptedTask.completionHistory=interruptedTask.completionHistory.slice(-16);interruptedTask.completion=historical;
      }
      if(interruptedTask.turnStartedAt||interruptedTask.turnEndedAt||interruptedTask.stopReason) {
        interruptedTask.turnHistory??=[];interruptedTask.turnHistory.push({turnFence:interruptedTask.executionEpoch,turnStartedAt:interruptedTask.turnStartedAt,turnEndedAt:interruptedTask.turnEndedAt,stopReason:interruptedTask.stopReason,fencedBy:boundary.id});interruptedTask.turnHistory=interruptedTask.turnHistory.slice(-16);
      }
      interruptedTask.executionEpoch=toEpoch;interruptedTask.continuationRequired=true;interruptedTask.interruptedBy=boundary.id;
      delete interruptedTask.turnStartedAt;delete interruptedTask.turnEndedAt;delete interruptedTask.turnEndedFence;delete interruptedTask.stopReason;
    }
    request.forceState='confirmed';request.forceBoundary=boundary;request.waitingReason=null;this.save('force-switch-fenced',{commandId:request.commandId,boundaryId:boundary.id,toEpoch});return boundary;
  }
  pendingModeNotice(request) {
    if(!request?.notify)return null;
    return this.queueNotice(request.sourceInputId??request.commandId,'mode-pending',{requestId:request.commandId,subscriberIds:request.notificationSubscribers??[request.commandId],text:'切换正在处理，当前模型尚未完成核验。'});
  }
  failModeRequest(request,reason,text) {
    request.state='failed';request.forceState=request.forceState==='unconfirmed'?'unconfirmed':'failed';request.failedAt=this.now();request.failureReason=String(reason).slice(0,160);request.waitingReason=null;
    if(this.state.requestedMode===request.mode)this.state.requestedMode=null;
    if(request.notify)this.queueNotice(request.sourceInputId??request.commandId,'mode-failed',{requestId:request.commandId,subscriberIds:request.notificationSubscribers??[request.commandId],text});
  }
  async applyModeRequest(request,runtime) {
    if(!request||request.state!=='pending')return {runtime};
    let target=request.mode==='manual'?request.profile:request.mode==='work'?ROUTER_PROFILES.work:
      this.tasks().length?ROUTER_PROFILES.work:this.automaticIdleProfile();
    if(request.mode==='auto'&&request.force){
      try {request.plannedAutoReturnProfile=await this.resolvedProfile(ROUTER_PROFILES.chat);}catch(error){
        this.failModeRequest(request,String(error.message??error),'没有切换：当前宿主不支持自动聊天配置。');this.save('mode-failed',{commandId:request.commandId,reason:'unsupported-auto-profile'});return {runtime};
      }
      target=this.tasks().length?ROUTER_PROFILES.work:request.plannedAutoReturnProfile;
    }
    // Validate the exact model/effort/tier before interrupting anything. A
    // nonexistent or unsupported profile is a failed control request, not a
    // reason to cancel the owner's current native turn.
    try {target=await this.resolvedProfile(target);}catch(error){
      this.failModeRequest(request,String(error.message??error),'没有切换：当前宿主不支持这组模型设置。');
      this.save('mode-failed',{commandId:request.commandId,reason:'unsupported-profile'});return {runtime};
    }
    // A prior owner force can suppress the current epoch before a newer mode
    // choice supersedes it. Settle that interruption, never its old mode choice.
    const prior=this.unfencedForce();
    if(prior&&prior!==request&&!this.nativeBusy(runtime)) {
      const boundary=await this.forceBoundary(prior,runtime);
      if(boundary&&prior.result){prior.result.forceBoundaryId=boundary.id;this.save('earlier-force-fenced',{commandId:prior.commandId,boundaryId:boundary.id});}
    }
    const priorNeedsFence=Boolean(prior&&prior!==request&&!prior.forceBoundary);
    if(priorNeedsFence&&!request.force){request.waitingReason='prior-force-native-idle-pending';this.pendingModeNotice(request);return {runtime};}
    // Retaining an already active manual profile is not an interruption or a
    // model transition. Keep task/tools/delivery ownership exactly as it is.
    const unchanged=request.mode==='manual'&&this.state.mode==='manual'&&
      this.state.manualProfile&&this.verified(runtime,this.state.manualProfile)&&this.verified(runtime,target)&&
      !request.forceState&&!priorNeedsFence&&this.state.transition?.state!=='unconfirmed';
    if(!unchanged&&request.deferUntilSettled&&(this.tasks().length||this.busy(runtime))){request.waitingReason='owner-requested-settlement';this.pendingModeNotice(request);return {runtime};}
    if(request.mode==='work'&&!this.state.autoReturnProfile)this.captureAutomaticReturn(runtime);
    let forced=Boolean(request.forceBoundary);
    if(!unchanged&&(this.busy(runtime)||this.reservations.size>0&&!request.force||request.forceState==='unconfirmed'||priorNeedsFence)) {
      if(!request.force){request.waitingReason='coordinator-busy';return {runtime};}
      const boundary=await this.forceBoundary(request,runtime);if(!boundary)return {runtime};forced=true;
      runtime=await this.inspect();
      if(this.nativeBusy(runtime,{confirmedForce:true})){request.waitingReason='native-interrupt-pending';this.pendingModeNotice(request);this.save('force-native-idle-pending',{commandId:request.commandId});return {runtime};}
    }
    if(this.state.transition?.state==='unconfirmed') {
      if(!request.force)return {runtime};
      if(this.nativeBusy(runtime,{confirmedForce:Boolean(request.forceBoundary)}))return {runtime};
      this.state.transition.state='superseded-by-owner';this.save('switch-superseded',{commandId:request.commandId});
    }
    if(request.mode==='auto'&&this.tasks().length&&!request.force)return {state:'work-held',runtime};
    try {
      let actual=runtime;
      if(!this.verified(runtime,target)) {
        this.startTransition(runtime,target,request.reason,'mode-request',request.commandId);this.save('switch-requested');
        const switched=await this.switchTo(target,{forceBoundary:request.forceBoundary});actual=switched.actual;target=switched.target;
        if(!this.verified(actual,target))throw Error('Unverified mode change');
        this.applyModeState(request,actual,target);this.finishTransition(actual);
      } else {this.applyModeState(request,runtime,target);this.state.actual=runtime;}
      for(const prior of Object.values(this.state.requests))if(prior!==request&&prior.state==='pending'){prior.state='superseded';prior.supersededBy=request.commandId;}
      this.modeApplied(request,this.state.actual,{unchanged});this.save(unchanged?'mode-unchanged':'mode-applied',{commandId:request.commandId,model:target.model,forced});return {runtime:this.state.actual};
    } catch(error) {
      request.failedAt=this.now();request.waitingReason='switch-unconfirmed';
      if(this.state.transition)this.state.transition.state='unconfirmed';
      this.pendingModeNotice(request);
      this.save('mode-switch-unconfirmed',{commandId:request.commandId,reason:String(error?.message??error).slice(0,120)});
      return {runtime:await this.inspect()};
    }
  }
  applyModeState(request,actual,target) {
    const applied=runtimeProfile({...actual,serviceTierPreference:target.serviceTierPreference??actual.serviceTierPreference});
    if(request.mode==='manual') {this.state.mode='manual';this.state.manualProfile=applied;delete this.state.autoReturnProfile;}
    else if(request.mode==='work') {this.state.mode='work';delete this.state.manualProfile;}
    else {
      this.state.mode='auto';delete this.state.manualProfile;
      if(request.force) {
        const returnProfile=clone(request.plannedAutoReturnProfile??ROUTER_PROFILES.chat);this.state.autoIdleProfile=returnProfile;
        if(this.tasks().length)this.state.autoReturnProfile=clone(returnProfile);else delete this.state.autoReturnProfile;
      } else if(!this.tasks().length){this.state.autoIdleProfile=applied;delete this.state.autoReturnProfile;}
    }
    this.state.requestedMode=null;this.state.exitRequested=false;
  }
  startTransition(before,profile,reason,source,sourceId) {
    if(typeof profile==='string')profile={model:profile};
    const fromProfile=runtimeProfile(before);
    this.state.transition={id:'switch-'+digest([this.sessionId,this.state.revision,sourceId,profile]).slice(0,24),state:'switching',
      from:before.model,to:profile.model,fromProfile,targetProfile:clone(profile),reason,source,sourceId,at:this.now()};
  }
  finishTransition(actual) {
    this.state.actual=actual;Object.assign(this.state.transition,{state:'applied',actualModel:actual.model,verifiedAt:actual.checkedAt,appliedAt:this.now()});
    const transition=this.state.transition;
    const changed=transition.fromProfile?digest(transition.fromProfile)!==digest(runtimeProfile(actual)):transition.from&&transition.from!==actual.model;
    if(transition.from&&changed&&this.verified(actual,transition.targetProfile??actual.model)) {
      const view=publicMobileRuntime(this.state,actual,this.sessionId);
      // A maintenance restart that puts back the model the owner was last told about
      // changed nothing they can see: the notice is settled as suppressed, never sent.
      const silent=transition.source==='host-restart'&&actual.model===this.lastToldModel();
      const notice=this.queueNotice(transition.id,'model-switched',{transitionId:transition.id,sourceInputId:this.state.inputs[transition.sourceId]?transition.sourceId:this.state.requests[transition.sourceId]?.sourceInputId,
        target:actual.model,targetProfile:transition.targetProfile,from:transition.from,runtime:view.actual,mode:this.state.mode,
        ...(silent?{state:'suppressed',reason:NOTICE_SUPPRESSED,settledAt:this.now()}:{text:runtimeReply(view,{switched:true})})});
      transition.noticeId=notice.id;
      // KIN-ITER-20260918-03: every real change records what became of its owner
      // notification. A suppressed notice is the record of a message deliberately
      // NOT sent — the owner-visible state already matched — and it never counts
      // as a delivered notification.
      transition.notification=silent?{state:'suppressed',reason:NOTICE_SUPPRESSED,noticeId:notice.id}:{state:'queued',noticeId:notice.id};
    } else {
      // A rebind that leaves the model where it was is not a change the owner can
      // see: no notice is generated, and the transition says why.
      transition.notification??={state:'not-generated',reason:transition.from===actual.model?'rebind-no-model-change':transition.from?'switch-unverified':'no-prior-model'};
    }
  }
  /** The model the owner was last told about: the most recent runtime-status notice
   * the platform accepted. It is the only durable record here of what they heard —
   * deliveries carry a message ID and a time, never the model that wrote them. */
  lastToldModel() {
    const told=Object.values(this.state.notices).filter(n=>n.state==='accepted'&&n.messageId&&(n.toldModel??n.runtime?.model))
      .sort((a,b)=>(a.acceptedAt??0)-(b.acceptedAt??0)).at(-1);
    return told?told.toldModel??told.runtime.model:null;
  }
  observeRuntime(runtime) {
    if(this.verified(runtime,runtime.model)&&this.state.actual?.known&&this.state.actual.model!==runtime.model&&this.state.transition?.state!=='switching'&&this.state.transition?.state!=='unconfirmed') {
      this.startTransition(this.state.actual,runtimeProfile(runtime),'Observed native model change','runtime-observation');
      this.finishTransition(runtime);this.state.transition.state='observed';this.save('runtime-model-observed');
    }
    if(this.state.mode==='auto'&&!this.tasks().length&&!this.state.autoReturnProfile&&this.verified(runtime,runtime.model))this.state.autoIdleProfile=runtimeProfile(runtime);
    this.state.actual=runtime;
  }
  async readRuntime(loaded=true) {
    return this.locked(async()=>{const runtime=await this.inspect();this.observeRuntime(runtime);
      return {...publicMobileRuntime(this.state,runtime,this.sessionId,loaded),models:await this.availableModels(runtime),defaults:ROUTER_PROFILES};});
  }
  async restoreRoutingProfile() {
    return this.locked(async()=>{
      if(this.runtimeRestored)return {state:'restored'};
      let runtime=await this.inspect();
      runtime=await this.reconcileTransition(runtime);
      if(this.state.transition?.state==='unconfirmed')return {state:'waiting'};
      let target=await this.resolvedProfile(this.desiredProfile(runtime));
      if(!this.verified(runtime,target)){
        // Only an actual switch waits for work in flight. An unconfirmed input is
        // reconciled by its own id and never holds the restart profile (AD1-10).
        if(this.busy(runtime)||Object.values(this.state.inputs).some(inputInFlight))return {state:'waiting'};
        this.startTransition(runtime,target,'Restore persisted mobile routing mode','host-restart');this.save('restart-profile-requested');
        try {const switched=await this.switchTo(target);runtime=switched.actual;target=switched.target;if(!this.verified(runtime,target))throw Error('Restart profile unverified');this.finishTransition(runtime);}
        catch(error){this.state.transition.state='unconfirmed';this.save('restart-profile-unconfirmed');throw error;}
      }
      this.observeRuntime(runtime);this.runtimeRestored=true;this.save('restart-profile-restored',{model:runtime.model});
      return {state:'restored',model:runtime.model,threadId:runtime.threadId};
    });
  }
  async prepareModel(model) {
    return this.locked(async()=>{
      const runtime=await this.inspect();
      if(this.tasks().length||this.busy(runtime)||this.reservations.size)throw Error('Work prevents model verification');
      let profile=await this.resolvedProfile(typeof model==='string'?{model}:model);
      if(this.verified(runtime,profile)){this.observeRuntime(runtime);return runtime;}
      this.startTransition(runtime,profile,'Host model verification','probe');this.save('switch-requested');
      try {const switched=await this.switchTo(profile),actual=switched.actual;profile=switched.target;if(!this.verified(actual,profile))throw Error('Unverified model');this.finishTransition(actual);this.save('switch-applied');return actual;}
      catch(error){this.state.transition.state='unconfirmed';this.save('switch-unconfirmed');throw error;}
    });
  }
  queueNotice(key,kind,extra={}) {
    const id='kin-mode-'+digest([this.sessionId,key,kind]).slice(0,40);
    this.state.notices[id]??={id,kind,state:'pending',sourceInputId:this.state.inputs[key]?key:this.state.requests[key]?.sourceInputId,createdAt:this.now(),...extra};return this.state.notices[id];
  }
  modeApplied(request,runtime,{unchanged=false}={}) {
    request.state='applied';request.appliedAt=this.now();
    request.result={model:runtime.model,provider:runtime.modelProvider,reasoningEffort:runtime.reasoningEffort,
      serviceTier:runtime.serviceTier??null,serviceTierVerified:Boolean(runtime.serviceTier&&runtime.serviceTierVerified!==false),
      serviceTierPreference:runtime.serviceTierPreference??(runtime.fastMode==='on'||runtime.fastMode===true?'fast':runtime.fastMode==='off'||runtime.fastMode===false?'default':null),
      mode:this.state.mode,sessionId:this.sessionId,verifiedAt:runtime.checkedAt,transitionId:this.state.transition?.id,forceBoundaryId:request.forceBoundary?.id};
    request.waitingReason=null;
    if(unchanged)return;
    const task=this.currentTask(),source=this.state.inputs[request.sourceInputId];
    if(task?.continuationRequired&&source?.route==='control'&&!task.cancelRequested)
      task.handoff={id:request.commandId,text:'模型已按小光要求切换。继续原任务，读取已有进度和交付回执，不重做已完成的动作；结果发飞书。',state:'pending'};
    const transition=this.state.transition;
    const notice=transition?.sourceId===request.commandId&&this.state.notices[transition.noticeId];
    if(notice){notice.requestId=request.commandId;notice.subscriberIds=request.notificationSubscribers??[request.commandId];}
    else if(request.notify)this.queueNotice(request.notificationOrigin??request.commandId,'mode-applied',{requestId:request.commandId,subscriberIds:request.notificationSubscribers??[request.commandId],target:runtime.model,targetProfile:runtimeProfile(runtime),mode:this.state.mode});
  }
  async acceptControl(record,runtime) {
    if(['work','auto','manual'].includes(record.command)) {
      const commandId='owner-mode:'+record.id;
      if(this.state.requests[commandId])return this.state.requests[commandId];
      let profile=record.profile,resolved=true;
      if(record.command==='manual') {
        try {profile=await this.resolvedProfile(record.profile);record.resolvedControlProfile=clone(profile);}
        catch {resolved=false;}
      }
      const modeRequest={commandId,mode:record.command,reason:'Explicit owner mode command',sourceInputId:record.id,sourceHash:record.hash,notify:true,force:record.force===true,...(profile?{profile}: {})};
      // Invalid profiles still become an honest failed request/notice, but they
      // never receive interrupt authority. Supported controls bind authority to
      // the exact resolved request hash before it enters recordModeRequest.
      if(resolved)record.controlRequestHash=digest(modeRequest);
      this.recordModeRequest(modeRequest);
      const request=this.state.requests[commandId];
      if(!request.force&&(this.busy(runtime)||record.command==='auto'&&this.tasks().length))this.queueNotice(record.id,'mode-pending',{requestId:commandId});
      return request;
    } else if(record.command==='watch') {
      const request=Object.values(this.state.requests).findLast(r=>r.state==='pending'&&['work','auto','manual'].includes(r.mode));
      if(request){request.notify=true;request.notificationSubscribers=[...new Set([...(request.notificationSubscribers??[]),record.id])];this.queueNotice(record.id,'mode-pending',{requestId:request.commandId});}
      else this.queueNotice(record.id,'status');
    } else this.queueNotice(record.id,'status');
    return null;
  }
  observeOperation(inputId,phase,result={}) {
    return this.locked(async()=>{
      const operation=this.state.operations[inputId];if(!operation)throw Error('Unknown native operation');
      // A native command that did not end its turn normally is a terminal failure with
      // its stop reason kept, never an operation left counting as running (AD1-03).
      operation.state=phase==='start'?'running':result.stopReason==='end_turn'?'completed':'failed';
      operation.updatedAt=this.now();operation.stopReason=result.stopReason;
      if(phase==='start')this.turnStarted({inputIds:[inputId]});else this.turnEnded({stopReason:result.stopReason});
      this.save('native-operation-'+phase,{inputId,state:operation.state});
    });
  }
  async flushNotices({send,lookup}) {
    // The owner-bound sender has its own durable outbox. Never replay an
    // ambiguous send; reconcile the original ID, including after host restart.
    // KIN-ITER-20260918-03: a receipt proves one of three things — accepted,
    // rejected, or not-submitted (nothing reached the platform; the SAME id may be
    // sent again, bounded by NOTICE_SEND_BUDGET, resumable across restarts).
    // Anything else is submission-unknown: the original id is only ever looked up,
    // never resent, and the re-checks are bounded by NOTICE_LOOKUP_BUDGET before
    // the notice stops polling as a visible `unresolved`.
    for(const id of Object.keys(this.state.notices)) {
      const notice=await this.locked(async()=>{
        const n=this.state.notices[id];
        if(!['pending','retry','unconfirmed'].includes(n.state)||n.nextAttemptAt>this.now())return null;
        if(n.state==='unconfirmed') {
          if((n.lookups??0)>=NOTICE_LOOKUP_BUDGET) {
            n.state='unresolved';n.waitingReason='receipt-lookup-exhausted';n.nextAction='none';n.updatedAt=this.now();
            this.save('notice-settled',{id,state:n.state,reason:n.waitingReason});return null;
          }
          return {...clone(n),lookupOnly:true};
        }
        // A reconcile-able id and stage are durable BEFORE any failable preparation.
        n.stage='preparing';n.updatedAt=this.now();this.save('notice-preparing',{id});
        const runtime=await this.inspect();const view=publicMobileRuntime(this.state,runtime,this.sessionId);
        const controlWithoutActual=['mode-pending','mode-failed'].includes(n.kind)&&Boolean(n.text);
        if(!view.actual.verified&&!controlWithoutActual)return null;
        if(n.kind==='mode-pending'&&this.state.requests[n.requestId]?.state!=='pending'){n.state='superseded';n.updatedAt=this.now();this.save('notice-superseded',{id});return null;}
        if(view.actual.verified&&n.kind==='mode-applied'&&(!profileMatches(runtime,n.targetProfile??{model:n.target}))){n.state='superseded';n.updatedAt=this.now();this.save('notice-superseded',{id});return null;}
        if(view.actual.verified&&n.kind==='model-switched'&&!profileMatches(runtime,n.targetProfile??{model:n.target})) {
          const past=runtimeReply({actual:n.runtime,tasks:[],mode:n.mode},{switched:true}).replace('已切换到 ','此前已切换到 ');
          n.text=past+' '+runtimeReply(view);
        }
        if(n.state==='retry'&&n.kind!=='model-switched'){delete n.text;delete n.runtime;}
        n.text??=runtimeReply(view,{pending:n.kind==='mode-pending',switched:n.kind==='mode-applied'});
        // Whatever else the message recalls, this is the model it names as the current one.
        if(view.actual.verified){n.runtime??=view.actual;if(n.kind!=='mode-failed')n.toldModel=view.actual.model;}n.state='sending';n.stage='sending';n.attempts=(n.attempts??0)+1;n.updatedAt=this.now();
        this.save('notice-sending',{id});return {...clone(n),sendNow:true};
      });
      if(!notice)continue;
      let receipt;
      if(notice.sendNow) {
        try {receipt=await send({id,text:notice.text,kind:'runtime-status',sourceInputId:notice.sourceInputId,notificationSubscribers:notice.subscriberIds??[]});}
        catch {receipt=null;}
        // Any non-accepted send outcome is settled from the outbox ground truth.
        if(noticeReceiptClass(receipt)!=='accepted')receipt=await lookup(id).catch(()=>null)??receipt;
      } else receipt=await lookup(id).catch(()=>null);
      await this.locked(async()=>{
        const n=this.state.notices[id];
        const before=JSON.stringify([n.state,n.waitingReason,n.nextAction,n.attempts,n.lookups,n.messageId,n.stage]);
        const kind=noticeReceiptClass(receipt);
        if(kind==='accepted'){n.state='accepted';n.messageId=receipt.messageId;n.acceptedAt=this.now();n.replyRecorded=true;n.nextAction='none';n.stage='settled';
          const source=this.state.inputs[n.sourceInputId];
          if(n.kind==='mode-failed'&&source?.state==='failed-before-submit'&&source.failureStage==='model-control')source.ownerNotice={kind:'stopped',id:n.id,state:'accepted',messageId:n.messageId,acceptedAt:n.acceptedAt};}
        else if(kind==='rejected'){n.firstFailure??={class:'receipt-rejected',at:this.now()};n.state='rejected';n.waitingReason='platform-rejected';n.nextAction='none';n.stage='settled';}
        else if(kind==='not-submitted') {
          n.firstFailure??={class:notice.sendNow?'send-not-submitted':'receipt-not-submitted',at:this.now()};
          if((n.attempts??0)<NOTICE_SEND_BUDGET){n.state='retry';n.nextAttemptAt=this.now()+2000*(n.attempts??1);n.nextAction='resend-same-id';n.waitingReason='not-submitted-retry-scheduled';}
          else{n.state='failed';n.waitingReason='send-attempts-exhausted';n.nextAction='none';n.stage='settled';}
        } else {
          n.firstFailure??={class:notice.sendNow?'send-outcome-unknown':'receipt-unknown',at:this.now()};
          if(!notice.sendNow)n.lookups=(n.lookups??0)+1;
          n.state='unconfirmed';n.nextAttemptAt=this.now()+30000;n.nextAction='lookup-only';n.waitingReason='submission-unknown';
        }
        n.updatedAt=this.now();
        // A settle that changed nothing writes nothing.
        if(JSON.stringify([n.state,n.waitingReason,n.nextAction,n.attempts,n.lookups,n.messageId,n.stage])!==before)
          this.save('notice-settled',{id,state:n.state,...(n.waitingReason?{reason:n.waitingReason}:{})});
      });
    }
  }
  // ---- Facts for the ledger: which inputs a native prompt carries, when it ends, and
  // what of the reply reached the owner.
  turnStarted(data) {
    const ids=[...new Set(Array.isArray(data.inputIds)?data.inputIds:[])],at=this.now();
    this.state.turn={startedAt:at,inputIds:ids};
    for(const id of ids) {
      const record=this.state.inputs[id];if(!record)continue;
      if(record.state==='queued'){record.state='accepted';record.acceptedAt=at;}
      if(['accepted','submitting'].includes(record.state)){record.turnStartedAt=at;delete record.turnEndedAt;delete record.stopReason;this.progress.set(id,at);}
      this.settleAcceptance(id,{state:record.state});
    }
    return ids.length>0;
  }
  turnEnded(data) {
    const turn=this.state.turn,at=this.now();if(!turn)return false;
    for(const id of turn.inputIds) {
      const record=this.state.inputs[id];if(!record?.turnStartedAt||record.turnEndedAt>=record.turnStartedAt)continue;
      record.turnEndedAt=at;record.stopReason=data.stopReason??null;
      if(!ownerInput(record))record.settledAt??=at;
    }
    delete this.state.turn;
    return true;
  }
  /** A bubble answering an input reached the platform, or was refused. */
  inputDelivery(data) {
    const record=typeof data.sourceInputId==='string'?this.state.inputs[data.sourceInputId]:null;
    if(!record)return false;
    this.progress.set(record.id,this.now());
    if(data.state==='accepted'&&data.messageId){record.delivered=(record.delivered??0)+1;record.lastDeliveredAt=this.now();return true;}
    if(['rejected','undeliverable'].includes(data.state)){record.undelivered=(record.undelivered??0)+1;return true;}
    return false;
  }
  /** The whole reply to an owner input reached the platform, or Kin chose not to
   * reply. Earlier owner inputs already in the session when it came are answered by it. */
  inputAnswered(kind,data) {
    const record=typeof data.inputId==='string'?this.state.inputs[data.inputId]:null;
    if(!record)return false;
    const state=kind==='reply-complete'?'accepted':['silent','merged'].includes(data.state)?data.state:null;if(!state)return false;
    const at=this.now();record.answer={state,at,...(data.mergedInto?{mergedInto:data.mergedInto}:{})};record.settledAt??=at;
    const basis=record.acceptedAt??at;
    if(ownerInput(record))for(const other of Object.values(this.state.inputs))
      if(other!==record&&ownerInput(other)&&other.state==='accepted'&&!answered(other)&&(other.acceptedAt??Infinity)<=basis){other.answer={state:'covered',by:record.id,at};other.settledAt??=at;}
    return true;
  }
  observe(kind,data={}) {
    return this.locked(async()=>{
      if(kind==='prompt-start')this.turnStarted(data);
      if(kind==='prompt-end')this.turnEnded(data);
      if(kind==='delivery'&&data.sourceInputId)this.inputDelivery(data);
      if(kind==='reply-complete'||kind==='reply-choice'){if(this.inputAnswered(kind,data))this.save(kind,{inputId:data.inputId});return;}
      if(kind==='input-dropped') {
        // The session let a queued prompt go before it began: provably never submitted.
        const record=this.state.inputs[data.inputId];
        if(record?.state==='queued'){this.notSubmitted(record,'session-dropped-before-prompt',{restart:true});this.save('input-failed-before-submit',{id:record.id,reason:record.reason});}
        this.settleAcceptance(data.inputId,{state:record?.state??'missing'});return;
      }
      if(kind==='tool'&&this.state.turn)for(const id of this.state.turn.inputIds)this.progress.set(id,this.now());
      // An explicit null belongs to a no-task/chat turn. It must not be rebound to
      // whichever task happens to be current when a delayed callback arrives.
      const task=Object.hasOwn(data,'taskId')?(data.taskId?this.state.tasks[data.taskId]:null):this.currentTask();
      const turnFence=data.turnFence??data.executionEpoch;
      const taskEvent=['prompt-start','prompt-end','tool','delivery'].includes(kind);
      const storeHistorical=(reason,{authoritative=false}={})=>{
        const at=this.now();
        const event={kind,reason,taskId:task?.id,inputVersion:data.inputVersion,turnFence:turnFence??null,currentEpoch:this.state.executionEpoch,at,
          id:data.id,state:data.state,status:data.status,messageId:data.messageId,outboxId:data.outboxId,stage:data.stage,submissionStarted:data.submissionStarted,
          sourceInputId:data.sourceInputId,stopReason:data.stopReason};
        this.state.lateEvents??=[];
        this.state.lateEvents.push(event);this.state.lateEvents=this.state.lateEvents.slice(-512);
        // External-effect evidence is retained under a fence/version-specific
        // history key. It never overwrites the current ledger entry with the same
        // id. Only an explicit older fence may later settle a completion that was
        // intentionally preserved across an already-idle force boundary.
        if(task&&kind==='delivery') {
          task.deliveryHistory??={};
          const key='delivery-'+digest([data.id,turnFence??null,data.inputVersion??null,reason]).slice(0,40);
          task.deliveryHistory[key]={id:data.id,state:data.state,messageId:data.messageId,outboxId:data.outboxId,stage:data.stage,sourceInputId:data.sourceInputId,
            submissionStarted:data.submissionStarted,inputVersion:data.inputVersion,turnFence:turnFence??null,lateAfterForce:true,
            authority:authoritative?'historical-fence':'evidence-only',reason,at};
        }
        if(task&&kind==='tool') {
          task.toolHistory??={};
          const key='tool-'+digest([data.id,turnFence??null,data.inputVersion??null,reason]).slice(0,40);
          task.toolHistory[key]={id:data.id,status:data.status??'pending',inputVersion:data.inputVersion,turnFence:turnFence??null,
            lateAfterForce:true,authority:authoritative?'historical-fence':'evidence-only',reason,...(data.reason?{detailReason:data.reason}:{}),at};
        }
        this.save('late-'+kind,{taskId:task?.id,reason,turnFence:turnFence??null,currentEpoch:this.state.executionEpoch});
      };
      let historicalReason=null;
      if(taskEvent) {
        if((data.turnFence!==undefined||data.executionEpoch!==undefined)&&!Number.isInteger(turnFence))historicalReason='invalid-turn-fence';
        else if(data.inputVersion!==undefined&&!Number.isSafeInteger(data.inputVersion))historicalReason='invalid-input-version';
        else if(this.state.executionEpoch>0&&(turnFence===undefined||data.inputVersion===undefined))historicalReason='unversioned-after-force';
        else if(Number.isInteger(turnFence)&&turnFence<this.state.executionEpoch)historicalReason='older-fence';
        else if(Number.isInteger(turnFence)&&turnFence>this.state.executionEpoch)historicalReason='future-fence';
        else if(task&&data.inputVersion!==undefined&&data.inputVersion!==task.inputVersion)historicalReason='input-version-mismatch';
        else if(task&&kind!=='prompt-start'&&Number.isInteger(turnFence)&&turnFence!==task.executionEpoch)historicalReason='task-fence-mismatch';
      }
      if(historicalReason) {
        storeHistorical(historicalReason,{authoritative:historicalReason==='older-fence'&&Number.isInteger(turnFence)&&Number.isSafeInteger(data.inputVersion)});
        return;
      }
      if(kind==='reply'&&data.final) {this.state.recent.push({role:'assistant',text:data.text.slice(0,4000),at:data.at??this.now()});this.state.recent=this.state.recent.slice(-16);}
      if(task&&open(task)) {
        if(kind==='prompt-start') {
          if(task.completion?.outcome==='declined'){
            if(this.declineReady(task,await this.inspect())){task.status='canceled';task.canceledAt=this.now();}
          }
          if(open(task)){task.status='running';task.turnStartedAt=this.now();task.executionEpoch=turnFence??this.state.executionEpoch;task.continuationRequired=false;if(task.completion?.state==='historical-proposal')delete task.completion;delete task.turnEndedAt;delete task.turnEndedFence;}
        }
        if(kind==='prompt-end') {
          task.turnEndedAt=this.now();task.turnEndedFence=turnFence??task.executionEpoch;task.stopReason=data.stopReason;
          const proposal=task.completion;
          if(proposal?.outcome==='declined'&&!proposal.turnEndedAt&&proposal.turnStartedAt===task.turnStartedAt&&proposal.inputVersion===task.inputVersion&&proposal.turnFence===task.turnEndedFence){
            proposal.turnEndedAt=task.turnEndedAt;proposal.turnEndedFence=task.turnEndedFence;proposal.stopReason=data.stopReason;
          }
          if(data.stopReason!=='end_turn')task.status='failed';
        }
        if(kind==='tool') {
          const inputVersion=data.inputVersion??task.inputVersion,fence=turnFence??task.executionEpoch,previous=task.tools[data.id];
          if(previous&&(previous.inputVersion!==inputVersion||previous.turnFence!==fence)) {
            task.toolHistory??={};const key='tool-'+digest([data.id,previous.turnFence??null,previous.inputVersion??null,'replaced-current-entry']).slice(0,40);
            task.toolHistory[key]={id:data.id,...clone(previous),authority:'historical-fence',reason:'replaced-current-entry'};
          }
          task.tools[data.id]={status:data.status??previous?.status??'pending',inputVersion,turnFence:fence,...(data.reason?{reason:data.reason}: {})};
        }
        if(kind==='delivery') {
          const inputVersion=data.inputVersion??task.inputVersion,fence=turnFence??task.executionEpoch,previous=task.deliveries[data.id];
          if(previous&&(previous.inputVersion!==inputVersion||previous.turnFence!==fence)) {
            task.deliveryHistory??={};const key='delivery-'+digest([data.id,previous.turnFence??null,previous.inputVersion??null,'replaced-current-entry']).slice(0,40);
            task.deliveryHistory[key]={id:data.id,...clone(previous),authority:'historical-fence',reason:'replaced-current-entry'};
          }
          task.deliveries[data.id]={state:data.state,messageId:data.messageId,outboxId:data.outboxId,stage:data.stage,submissionStarted:data.submissionStarted,
            sourceInputId:data.sourceInputId,inputVersion,turnFence:fence,at:this.now()};
        }
      }
      this.save(kind);
    });
  }
  completeInternal(taskId,inputVersion,receipt) {
    return this.locked(async()=>{
      const task=this.state.tasks[taskId];
      if(!task||task.requiresDelivery||task.inputVersion!==inputVersion||!receipt?.verified)return{state:'superseded'};
      task.completion={inputVersion,turnFence:task.executionEpoch,at:task.turnStartedAt??this.now(),summary:'Host verified internal result'};
      task.internalReceipt=receipt;this.save('internal-result',{taskId});return{state:'recorded'};
    });
  }
  archiveInterruptedTools() {
    let changed=false;
    for(const task of this.tasks())for(const [id,tool] of Object.entries(task.tools??{})) {
      if(['completed','failed','canceled','cancelled'].includes(tool.status))continue;
      const boundary=this.state.forceBoundaries.find(b=>b.taskId===task.id&&b.fromEpoch===tool.turnFence&&
        b.toEpoch<=task.executionEpoch&&b.receipt?.state==='interrupted'&&b.receipt.cancel?.cancelledTurn===true&&
        b.receipt.runtime?.known===true&&b.receipt.runtime.sessionId===this.sessionId&&
        b.receipt.runtime.nativeStatus==='idle'&&b.receipt.runtime.active===false&&b.receipt.runtime.backgroundTasks===0);
      if(!boundary)continue;
      task.toolHistory??={};
      task.toolHistory[id+':fence:'+tool.turnFence]={id,...clone(tool),authority:'historical-fence',
        reason:'native-turn-interrupted',fencedBy:boundary.id};
      // Preserve the original status: a canceled native turn does not prove an
      // external action succeeded or was undone. It is no longer active work.
      delete task.tools[id];changed=true;
    }
    return changed;
  }
  declineReady(task,runtime) {
    const proposal=task.completion,fence=proposal?.turnFence,version=proposal?.inputVersion;
    if(runtime?.pendingDeliveries!==0||proposal?.outcome!=='declined'||proposal.state==='historical-proposal'||version!==task.inputVersion||
      fence!==task.executionEpoch||proposal.stopReason!=='end_turn'||!proposal.turnStartedAt||proposal.turnStartedAt>proposal.at||
      proposal.turnEndedAt<proposal.at||proposal.turnEndedFence!==fence)return false;
    const tools=[...Object.values(task.tools??{}),...Object.values(task.toolHistory??{}).filter(t=>t.authority==='historical-fence'&&t.turnFence===fence&&t.inputVersion===version)];
    if(!tools.every(tool=>['completed','failed'].includes(tool.status)))return false;
    const deliveries=[...Object.values(task.deliveries??{}),...Object.values(task.deliveryHistory??{}).filter(d=>d.authority==='historical-fence'&&d.turnFence===fence&&d.inputVersion===version)];
    return deliveries.every(settledDelivery)&&deliveries.some(d=>d.sourceInputId===proposal.sourceInputId&&d.inputVersion===version&&d.turnFence===fence&&d.state==='accepted'&&d.messageId&&d.at>=proposal.at);
  }
  async reconcile() {
    return this.locked(async()=>{
      const runtime=await this.reconcileTransition(await this.inspect());this.observeRuntime(runtime);
      if(this.busy(runtime))return {state:'busy'};
      let changed=this.archiveInterruptedTools();
      for(const task of this.tasks()) {
        if(task.cancelRequested) {task.status='canceled';task.canceledAt=this.now();changed=true;continue;}
        if(task.completion?.outcome==='declined'){
          if(this.declineReady(task,runtime)){task.status='canceled';task.canceledAt=this.now();changed=true;}
          continue;
        }
        // With semantic review installed, assistant completion is a proposal.
        // Internal repairs retain their separate verified-result protocol.
        if(this.workReviewerEnabled&&task.requiresDelivery!==false)continue;
        const fence=task.completion?.turnFence;
        const deliveryMap=new Map(Object.entries(task.deliveries).filter(([,delivery])=>(delivery.inputVersion===undefined||delivery.inputVersion===task.completion?.inputVersion)&&(fence===undefined||delivery.turnFence===undefined||delivery.turnFence===fence)));
        if(fence!==undefined)for(const delivery of Object.values(task.deliveryHistory??{}))if(delivery.authority==='historical-fence'&&delivery.turnFence===fence&&delivery.inputVersion===task.completion?.inputVersion)deliveryMap.set(delivery.id,delivery);
        const deliveries=[...deliveryMap.values()];
        const toolMap=new Map(Object.entries(task.tools).filter(([,tool])=>(fence===undefined||tool.turnFence===undefined||tool.turnFence===fence)));
        if(fence!==undefined)for(const tool of Object.values(task.toolHistory??{}))if(tool.authority==='historical-fence'&&tool.turnFence===fence&&tool.inputVersion===task.completion?.inputVersion)toolMap.set(tool.id,tool);
        const tools=[...toolMap.values()];
        if(task.completion?.state!=='historical-proposal'&&task.completion?.inputVersion===task.inputVersion && task.stopReason==='end_turn' && task.turnEndedAt>=task.completion.at &&
          (fence===undefined||task.turnEndedFence===undefined||task.turnEndedFence===fence) && tools.every(tool=>['completed','failed'].includes(tool.status)) &&
          (task.requiresDelivery===false?task.internalReceipt?.verified:
            deliveries.length && deliveries.every(d=>d.state==='accepted'&&d.messageId) && deliveries.some(d=>d.at>=task.completion.at))) {
          task.status='completed';task.completedAt=this.now();changed=true;
        }
      }
      if(changed&&!this.tasks().length&&this.state.mode!=='manual'&&this.state.autoReturnProfile&&!Object.values(this.state.requests).some(r=>r.state==='pending'&&['work','auto','manual'].includes(r.mode))) {
        const commandId='automatic-restore:'+digest([this.state.revision,this.state.autoReturnProfile]).slice(0,24);
        this.recordModeRequest({commandId,mode:'auto',reason:'All automatic work tasks are settled',notify:true});
      }
      if(!this.frozen())for(const request of Object.values(this.state.requests))if(request.state==='pending') {
        if(request.deferUntilSettled&&this.tasks().length)continue;
        const target=request.mode==='manual'?request.profile:request.mode==='work'?ROUTER_PROFILES.work:!this.tasks().length?this.automaticIdleProfile():null;
        if(target&&this.verified(runtime,target)) {await this.applyModeRequest(request,runtime);changed=true;}
      }
      if(changed)this.save('reconciled');return {state:this.tasks().length?'work-held':'idle'};
    });
  }
  /** The watchdog. Every owner input gets an outcome: a retry only on evidence that
   * the same id never arrived (not submitted, refused by the platform, reconciled as
   * not received), a reconciliation of an uncertain submission by its original id,
   * or a system notice to the owner. An input is `failed-notified` only once that
   * notice itself has a platform receipt. The ports run outside the mutex:
   * `requeue(id)` puts the durable inbox job back, `reconcileInput(id)` answers
   * found / not-found / unknown, and `notifyOwner(kind,id)` sends — or, for an id it
   * already tried, looks up — the one notice for that input. */
  async watch({notifyOwner=null,reconcileInput=null,requeue=null,sessionBusy=false}={}) {
    const now=this.now(),stuckMs=(this.state.config.inputStuckMinutes??10)*60000,gapMs=(this.state.config.noticeGapMinutes??10)*60000;
    const actions=await this.locked(async()=>{
      this.expireUnsubmittedInputs();
      const list=[],due=[];let changed=false;
      for(const record of Object.values(this.state.inputs)) {
        if(!ownerInput(record)||record.historical||this.inflight.has(record.id))continue;
        const summary=inputSummary(record),notice=record.ownerNotice;
        if(['answered','superseded','canceled-by-owner','failed-notified'].includes(summary))continue;
        // A failed owner control is reported by its own mode notice.
        if(record.failureStage==='model-control'&&Object.values(this.state.notices).some(n=>n.sourceInputId===record.id&&!['failed','rejected','unresolved','superseded'].includes(n.state)))continue;
        if(notice) {
          if(!['sending','exhausted'].includes(notice.state)&&!(notice.nextAt>now)){notice.state='sending';list.push({kind:'notify',id:record.id,notice:notice.kind});changed=true;}
          continue;
        }
        const last=Math.max(this.progress.get(record.id)??0,record.lastDeliveredAt??0,record.turnEndedAt??0,record.acceptedAt??0,record.retry?.lastFailureAt??0,record.at??0);
        const idle=now-last>=stuckMs;
        let verdict=null;
        if(record.state==='failed-before-submit') {
          if(record.retry&&!record.retry.exhausted&&requeue){if(record.retry.nextAt<=now&&!this.frozen())list.push({kind:'requeue',id:record.id});}
          else verdict='stopped';
        } else if(record.state==='queued') {
          // Only the host's in-memory queue held it: with the session idle and its
          // prompt never begun, it provably never reached the native session.
          if(idle&&!sessionBusy){this.notSubmitted(record,'queued-prompt-never-started',{restart:true});changed=true;}
        } else if(['unconfirmed','fenced-unconfirmed'].includes(record.state)) {
          if(!record.reconciliation&&reconcileInput)list.push({kind:'reconcile',id:record.id});
          else verdict='unknown';
        } else if(record.state==='accepted'&&idle&&!sessionBusy&&!turnRunning(record))verdict=(record.delivered??0)>0?'partial':'unknown';
        if(verdict)due.push({record,verdict});
      }
      // One notice at a time, never two within the gap: the oldest input first.
      if(due.length&&!(now-this.state.lastOwnerNoticeAt<gapMs)) {
        const {record,verdict}=due.sort((a,b)=>(a.record.at??0)-(b.record.at??0))[0];
        record.ownerNotice={kind:verdict,id:'kin-input-notice-'+digest([this.sessionId,record.id,verdict]).slice(0,32),state:'sending',attempts:0,at:now};
        this.state.lastOwnerNoticeAt=now;list.push({kind:'notify',id:record.id,notice:verdict});changed=true;
      }
      if(changed)this.save('input-watch',{notices:list.filter(action=>action.kind==='notify').map(action=>action.id)});
      return list;
    });
    const results=[];
    for(const action of actions) {
      if(action.kind==='requeue') {
        let result;try{result=await requeue(action.id);}catch{result={state:'unavailable'};}
        await this.locked(async()=>{
          const record=this.state.inputs[action.id];if(!record||record.state!=='failed-before-submit')return;
          if(['requeued','pending','processing'].includes(result?.state)){record.retry={...record.retry,requeuedAt:this.now(),nextAt:this.now()+stuckMs};this.save('input-requeued',{id:action.id});}
          else {record.retry={...record.retry,exhausted:true,requeue:result?.state??'missing'};this.save('input-retry-exhausted',{id:action.id,reason:record.retry.requeue});}
        });
        results.push({...action,result:result?.state??null});continue;
      }
      if(action.kind==='reconcile') {
        let result;try{result=await reconcileInput(action.id);}catch{result={state:'unknown'};}
        await this.locked(async()=>{
          const record=this.state.inputs[action.id];if(!record||!['unconfirmed','fenced-unconfirmed'].includes(record.state))return;
          const state=['found','not-found'].includes(result?.state)?result.state:'unknown';
          record.reconciliation={state,at:this.now()};
          if(state==='found'){record.state='accepted';record.acceptedAt??=this.now();}
          else if(state==='not-found'){this.notSubmitted(record,'reconciled-not-received',{restart:true,stage:'reconciliation'});record.retry.evidence='reconciled-not-received';}
          this.save('input-reconciled',{id:action.id,state});
        });
        results.push({...action,result:result?.state??null});continue;
      }
      let receipt=null;
      if(notifyOwner)try{receipt=await notifyOwner(action.notice,action.id);}catch{receipt=null;}
      await this.locked(async()=>{
        const record=this.state.inputs[action.id],notice=record?.ownerNotice;if(!notice)return;
        notice.attempts=(notice.attempts??0)+1;notice.updatedAt=this.now();
        const kind=notifyOwner?noticeReceiptClass(receipt):'no-port';
        if(kind==='accepted'){notice.state='accepted';notice.messageId=receipt.messageId;notice.acceptedAt=this.now();record.settledAt=this.now();}
        else if(notice.attempts>=(kind==='unknown'?NOTICE_LOOKUP_BUDGET:NOTICE_SEND_BUDGET)){notice.state='exhausted';notice.lastReceipt=kind;}
        else {notice.state=kind;notice.nextAt=this.now()+(kind==='not-submitted'?30000:120000);}
        this.save('input-notice',{id:action.id,kind:notice.kind,state:notice.state});
      });
      results.push({...action,result:notifyOwner?noticeReceiptClass(receipt):'no-port'});
    }
    return results;
  }
}
