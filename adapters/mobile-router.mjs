import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {writeJsonAtomic,readJsonFile,loadJson} from './atomic-json.mjs';
import {publicMobileRuntime,runtimeReply,TASK_CLOSED,openTask} from './mobile-controls.mjs';
import {conversationClock} from './conversation-time.mjs';
import {messageIntents} from './mobile-reviewer.mjs';
import {runtimeProfile,profileMatches,normalizeModelCatalog,resolveModelProfile} from './codex-models.mjs';
import {inputSettled,inputInFlight,inputSummary,unsettledView,emptySummary,ownerInput,answered,turnRunning,SUBMIT_RETRY_MS} from './input-ledger.mjs';

const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const clone = value => structuredClone(value);
const open=openTask;
/** What Kin may declare about a task. The host verifies facts, never the choice. */
export const TASK_OUTCOMES=Object.freeze(['completed','partial','declined','deferred']);
export {TASK_CLOSED,openTask};
/** A deferral names a moment within this long. */
export const DEFER_MAX_MS=30*24*3600000;
/** How old an owner input may be and still authorize a desktop hand-off (H3-20). */
export const HANDOFF_SOURCE_MAX_HOURS=24;
/** What starts beside the owner's conversation and must stop for a freeze and be waited
 * for by a drain: sends (a reply, a system notice, a reminder, a desktop hand-off result,
 * a proactive contact), the mind's own runs, the host's own background model calls and its
 * session maintenance. Other kinds are accepted as named (CR-LIFE-08, CR-MIND-01, CR2-INT-01,
 * CR2-MIND-01, CR3-FLOW-01, CL6-FLOW-02). */
export const ACTIVITY_KINDS=Object.freeze(['reply','notice','reminder','handoff','contact','assessment','contact-draft','creation','exploration',
  'appraisal','enrichment','memory-prep','work-summary','health-review','classification-retry','session-maintenance']);
/** The mind orders its own runs itself; a run in its own process never holds the owner's dispatch. */
const MIND_ACTIVITIES=new Set(['assessment','contact-draft','creation','exploration']);
const DETACHED_ACTIVITIES=new Set(['creation','exploration']);
/** The mind's own model work on its store, each in a process of its own and never in the
 * session: an appraisal, a memory enrichment, a memory warm-up. A freeze refuses it and a
 * drain waits for it, and that is all: it holds no turn, no switch and no dispatch (CR2-MIND-01). */
const STORE_ACTIVITIES=new Set(['appraisal','enrichment','memory-prep']);
/** The host's own background model calls, each a request of its own and never a turn in the
 * session: a summary of open work, a health reading, a classification asked again. Like the
 * store work, a freeze refuses one and a drain waits for it until the call has ended, and it
 * holds no turn, no switch and no dispatch (CR3-FLOW-01). So does session maintenance: a
 * checkpoint, and a rotation candidate in an app-server of its own; a compaction holds the
 * session through the coordinator and the native runtime, not through this (CL6-FLOW-02). */
const REVIEW_ACTIVITIES=new Set(['work-summary','health-review','classification-retry','session-maintenance']);
/** A notice round that used its attempts rests this long before its identity is tried
 * or looked up again; it is never given up (CR-LIFE-05). */
export const NOTICE_ROUND_REST_MS=Object.freeze([30*60000,2*3600000,6*3600000]);
/** An input proven unsent goes back to the inbox at most this many times (CR-LIFE-03). */
export const REQUEUE_BUDGET=3;
/** How often an input went back to its inbox, across every attempt (CR2-LIFE-05). */
const requeuesOf=record=>Number.isSafeInteger(record?.requeues)?record.requeues:record?.retry?.requeues??0;
/** A deferral whose plan port could not answer is asked again after these waits, then left to Kin (CR-MIND-03). */
export const DEFERRAL_PLAN_RETRY_MS=Object.freeze([60000,5*60000,15*60000,3600000,3*3600000]);
/** The channels an input may arrive on, kept as its receipt fact (CR-LIFE-17). */
const INPUT_CHANNELS=new Set(['feishu','wechat','desktop-handoff','cli']);
/** An input's kind as the journal may name it (owner, handoff, work-result, the mind's own…). */
const INPUT_KIND=/^[a-z][a-z-]{0,39}$/;
/** How far an input had got when the owner's stop settled it as hers, as the journal keeps it
 * beside the stop that did it: `preparation` — before native submission; `host-queue` —
 * handed only to the host's queue, its prompt not begun; `prompt-start` — refused where its
 * prompt would begin; `native-session` — it had reached, or may have reached, the native
 * session. A restore from the journal alone brings each back canceled (§0: an explicit
 * stop always stands). */
export const CANCEL_SCOPES=Object.freeze(['preparation','host-queue','prompt-start','native-session']);
/** What the journal keeps of the owner's stop on an input: which stop, and how far it had got. */
const cancelFacts=(record,scope)=>({canceledBy:record.canceledBy,scope,...(scope==='native-session'?{state:record.state}:{})});
/** When the host first received an input: its own receipt time, never in the future and
 * never refreshed by a retry (CR-LIFE-18). */
function receiptTime(value,now) {
  const at=typeof value==='string'?Date.parse(value):Number.isFinite(value)?value:NaN;
  return Number.isFinite(at)&&at<=now+60000?Math.min(at,now):now;
}
/** A delivery that will not change by waiting. A held draft is a fact Kin is told
 * about, never something the host waits on or withdraws (N3). */
const settledDelivery = delivery => delivery.state==='accepted'?Boolean(delivery.messageId):
  ['rejected','undeliverable','retired','not-submitted','canceled-before-send','deferred'].includes(delivery.state);
const terminalTool = tool => ['completed','failed','canceled','cancelled'].includes(tool.status);
const SHA256=/^[a-f0-9]{64}$/i;
const RECLASSIFICATION_EVIDENCE_KEYS=['acceptanceSha256','actualSessionId','conversationId','generation','id','ownerBindingSha256','sourceSha256','version'];
/** Built-in defaults. A host passes its configured profiles; nothing here decides them (AD1-15). */
export const ROUTER_PROFILES = Object.freeze({
  chat:Object.freeze({model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'default'}),
  work:Object.freeze({model:'gpt-6.1-sol',reasoningEffort:'medium',serviceTierPreference:'fast'}),
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
/** KIN-FIX-20260924, the first release (WS7): kin-deploy stops a host that predates
 * the ledger right after it is idle -- it cannot be frozen -- and hands over, beside
 * this router's state file, the owner inputs that reached it between that idle check
 * and its confirmed exit, and the freeze this router starts under. Ids and states
 * only. Read once, by the ledger upgrade. */
export const CARRYOVER_FILE='release-carryover.json';
export function readCarryover(stateFile) {
  const value=readJsonFile(path.join(path.dirname(stateFile),CARRYOVER_FILE)).value;
  if(value?.schema!==1||!Array.isArray(value.inputs))return null;
  const inputs=value.inputs.filter(entry=>typeof entry?.id==='string'&&/^[\w:.-]{1,200}$/.test(entry.id))
    .map(entry=>({id:entry.id,create:entry.create===true,...(Number.isFinite(entry.at)?{at:entry.at}:{})}));
  const freeze=typeof value.freeze?.reason==='string'&&value.freeze.reason.trim()&&Number.isFinite(value.freeze.until)
    ?{reason:value.freeze.reason.trim().slice(0,200),at:Number.isFinite(value.freeze.at)?value.freeze.at:null,until:value.freeze.until,...(value.freeze.hold===true?{hold:true}:{})}:null;
  return {releaseId:typeof value.releaseId==='string'?value.releaseId.slice(0,120):null,inputs,freeze};
}
/** Hot state keeps what is unsettled plus a bounded recent tail; the rest moves to
 * `archive/router-YYYY-MM.jsonl` beside the state file, never deleted (AD1-08). */
export const HOT_LIMITS=Object.freeze({inputs:200,internal:24,tasks:32,requests:64,notices:64,history:64,journalBytes:8*1024*1024});
const frozenError=reason=>Object.assign(Error('Dispatch is frozen: '+reason),{code:'dispatch-frozen',retryable:true});
/** A dispatch withdrawn before submission: provably not submitted, so the same id may be tried again. */
const withdrawnError=(record,cause)=>Object.assign(Error('Input dispatch was withdrawn before native submission: '+record.withdrawn.reason,cause?{cause}:undefined),
  {code:'input-not-submitted',inputId:record.id,retryAt:record.retry?.nextAt??null,retryExhausted:Boolean(record.retry?.exhausted)});
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
/** The rests before a notice the platform refused outright is sent again under its own
 * id; past them it is only looked up (CR2-LIFE-06). */
export const NOTICE_REJECT_RETRY_MS=Object.freeze([10*60000,3600000,6*3600000]);
/** What a lookup by original id proves (CR2-INT-02). `found` anywhere is receipt. `not-found`
 * proves an input never arrived only when the whole history of the very thread it was
 * submitted to was read, by a runtime that recorded input ids when it was submitted; any
 * other answer — an older runtime, another thread, a partial read, no record of the
 * submission — stays unknown: looked up again, never submitted again. */
export function reconciliationState(result,submit) {
  if(result?.state==='found')return {state:'found'};
  const reason=typeof result?.reason==='string'?result.reason.slice(0,80):null;
  if(result?.state!=='not-found')return {state:'unknown',...(reason?{reason}:{})};
  if(!submit)return {state:'unknown',reason:'no-submission-record'};
  if(submit.correlation!==true)return {state:'unknown',reason:'runtime-without-input-correlation'};
  if(result.complete!==true)return {state:'unknown',reason:'history-not-read-in-full'};
  if(typeof result.sessionId!=='string'||result.sessionId!==submit.sessionId)return {state:'unknown',reason:'not-the-submitted-thread'};
  return {state:'not-found'};
}
/** Whether the next call for a notice may start a physical send: it never began, its
 * receipt proved nothing reached the platform, or the platform refused it and its bounded
 * retries are not spent. A begun one whose outcome is unknown is only looked up. */
export const noticeMayStart=notice=>!notice.lastReceipt||['not-submitted','no-port'].includes(notice.lastReceipt)||
  notice.lastReceipt==='rejected'&&(notice.rejections??0)<=NOTICE_REJECT_RETRY_MS.length;
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
/** The owner's literal stop, read from the whole message. */
export const STOP_LITERAL=/^(?:停止任务|取消当前任务|\/停|\/acp-cancel)[!！。~～\s]*$/;
/** The owner's literal command, when a whole message is nothing else: her stop, a mode
 * command, or a native command such as `/compact`. The router reads each only from a
 * message of its own, so the durable inbox never batches one with other words
 * (CR2-LIFE-09). */
export function literalCommand(text,{attachments=[]}={}) {
  const value=typeof text==='string'?text.trim():'';
  if(STOP_LITERAL.test(value))return 'stop';
  if(Array.isArray(attachments)&&attachments.length)return null;
  return modeCommand(value)??(value==='/compact'?'compact':null);
}
/** Every durable adapter state file is written through here: a temporary name no
 * other writer can share, fsync before the rename, and — for the files that must
 * survive a corrupt revision — the replaced one kept as `<file>.prev`. */
export function atomicJson(file,value,{previous=false,pretty=true}={}) {writeJsonAtomic(file,value,{previous,pretty});}
const month=at=>new Date(Number.isFinite(at)?at:0).toISOString().slice(0,7);

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
  /** `replyTail` is the host's reply-tail port. Its one method, `stopped`, hears the owner's
   * literal stop, so what is still unsent of an earlier reply is withdrawn and owed to Kin's
   * next turn (N4). No model is asked about it; without the port nothing here changes.
   * `classifyIntents` lets that same call carry what the owner wants done with the message —
   * whether to stop the running task, whether a file was asked for, what they call themselves
   * — and lets an owner message with attachments be classified instead of assumed to be work.
   * Left off, every request and every record is what it was before intents existed.
   * `profiles` are the host's configured chat and work routing profiles; `hotLimits`
   * narrows what the state file keeps (see HOT_LIMITS). `withdrawQueued(ids)` is the
   * host's own: it takes the messages carrying only those input ids out of its session
   * queue before their prompts begin and answers the ids it took (CR3-FLOW-02). */
  constructor({file,sessionId,inspect,switchModel,classify,waitForIdle,now=()=>Date.now(),binding=null,replyTail=null,classifyIntents=false,modelCatalog=null,resolveProfile=null,forceSwitch=null,interruptTurn=null,profiles=null,hotLimits=null,runtimeId=null,withdrawQueued=null}) {
    Object.assign(this,{file,sessionId,inspect,switchModel,classify,waitForIdle,now,replyTail,classifyIntents:classifyIntents===true,modelCatalog,resolveProfile,forceSwitch,interruptTurn,withdrawQueued});
    // The runtime bundle this host runs, named on every submission it makes (CR2-INT-02).
    this.runtimeId=typeof runtimeId==='string'&&runtimeId?runtimeId.slice(0,120):null;
    this.hotLimits=Object.freeze({...HOT_LIMITS,...hotLimits});
    this.profiles=Object.freeze({chat:Object.freeze({...ROUTER_PROFILES.chat,...profiles?.chat}),work:Object.freeze({...ROUTER_PROFILES.work,...profiles?.work})});
    this.tail=Promise.resolve();this.inflight=new Map();this.progress=new Map();this.acceptance=new Map();this.reservations=new Map();this.deadlines=new Map();this.activities=new Map();this.watching=false;
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
    this.state.executionEpoch??=0;this.state.forceBoundaries??=[];this.state.autoIdleProfile??=clone(this.profiles.chat);
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
      if(record.ownerNotice?.state==='sending'){record.ownerNotice.state='unknown';record.ownerNotice.lastReceipt='unknown';}
      // A notice that rested, or one an older host gave up on, is taken up again (CR-LIFE-05).
      if(record.ownerNotice&&record.ownerNotice.state!=='accepted') {
        const notice=record.ownerNotice;
        if(notice.state==='exhausted'){notice.round=(notice.round??0)+1;notice.attempts=0;notice.state=notice.lastReceipt??'unknown';}
        notice.lastReceipt??=['not-submitted','unknown','rejected'].includes(notice.state)?notice.state:undefined;
        notice.nextAt=at;
      }
      // A native command the restart cut off failed; its input is reported, not answered (CR-LIFE-07).
      if(record.route==='maintenance'&&['interrupted','failed'].includes(this.state.operations[record.id]?.state)&&!record.answer)record.operationFailed??={state:this.state.operations[record.id].state,at};
      // A native turn cannot outlive the process that ran it.
      if(record.turnStartedAt&&!(record.turnEndedAt>=record.turnStartedAt)){record.turnEndedAt=at;record.stopReason='host-restart';}
    }
    delete this.state.turn;
    if(this.state.transition?.state==='switching')this.state.transition.state='unconfirmed';
    // KIN-FIX-20260924: records written before the ledger existed have no reply facts.
    // An accepted one is taken as answered; any other keeps its reason and id as
    // history and is never acted on, or counted as unsettled, again.
    if(this.state.ledgerVersion!==2) {
      // The first release's hand-over: these inputs raced the old host's stop and
      // stay live for the watchdog -- retried when provably unsubmitted, reconciled
      // or reported otherwise -- instead of becoming history.
      const carried=readCarryover(file);
      for(const entry of carried?.inputs??[]) {
        let record=this.state.inputs[entry.id];
        if(!record&&entry.create){
          record=this.state.inputs[entry.id]={id:entry.id,kind:'owner',state:'failed-before-submit',route:null,at:entry.at??at,
            conversationId:this.state.conversationId??null,generation:this.state.generation??null};
          this.notSubmitted(record,'release-carryover',{restart:true});
        }
        if(!record||!ownerInput(record)||record.state==='superseded')continue;
        record.carriedOver={at,...(carried.releaseId?{releaseId:carried.releaseId}:{})};
        if((['selected','semantic-pending'].includes(record.state)||record.state==='failed-before-submit'&&!record.retry)&&!record.submissionStartedAt) {
          if(this.state.semanticPending[record.id])this.state.semanticPending[record.id].state='superseded';
          this.notSubmitted(record,'release-carryover',{restart:true});
        }
      }
      for(const record of Object.values(this.state.inputs)) {
        if(record.carriedOver)continue;
        if(record.state==='accepted')record.answer??={state:'legacy'};
        else if(!inputInFlight(record)&&record.state!=='superseded')record.historical={at,reason:'pre-ledger:'+record.state};
      }
      // A stopped turn no longer fails its task (AD1-11), and a task that already held
      // the lock was taken on under the old rules.
      for(const task of Object.values(this.state.tasks))if(open(task)){
        if(task.status==='failed')task.status='running';
        task.acceptedAt??=task.createdAt??at;task.acceptance??='legacy';
      }
      this.state.ledgerVersion=2;
    }
    // KIN-FIX-20260924, CR2-OPS-04: a release's freeze comes beside the state file, and
    // the release renews it before it starts a host, so a long stop never lets a new
    // host dispatch before it is verified. It is taken up at every start: extended,
    // never shortened, never over another holder's freeze; the same hand-over is not
    // taken up again once it has been lifted. A hold is taken up past its end too (CR3-FLOW-03).
    const handed=readCarryover(file)?.freeze,taken=this.state.freezeHandover;
    if(handed&&(handed.until>at||handed.hold)&&!(taken?.reason===handed.reason&&taken.until===handed.until&&!this.frozen())){
      const current=this.frozen()?this.state.freeze:null;
      if(!current||current.reason===handed.reason){
        this.state.freeze={...(current??{}),reason:handed.reason,at:current?.at??handed.at??at,until:Math.max(handed.until,current?.until??0),by:current?.by??'kin-deploy',...(handed.hold?{hold:true}:{})};
        this.state.freezeHandover={reason:handed.reason,until:handed.until};
      }
    }
    this.archivedIds=this.loadArchivedIds();
    this.save('startup');
  }
  save(kind,detail={}) {
    this.state.revision++;
    // An event about an input carries whose input it is, so a restore from the journal
    // can tell the owner's words from Kin's own turns.
    const input=kind.startsWith('input-')&&typeof detail.id==='string'?this.state.inputs[detail.id]:null;
    const event={at:this.now(),kind,...detail,...(typeof input?.kind==='string'?{inputKind:input.kind}:{}),revision:this.state.revision};
    this.state.history.push(event);
    this.state.history=this.state.history.slice(-this.hotLimits.history);
    // Compact on disk; the journal keeps every event (AD1-08).
    writeJsonAtomic(this.file,this.state,{previous:true,pretty:false});
    const journal=this.file+'.events.jsonl';
    fs.appendFileSync(journal,JSON.stringify(event)+'\n',{mode:0o600});
    if(this.state.revision%256===0)this.rotateJournal(journal);
  }
  archiveDirectory(){return path.join(path.dirname(this.file),'archive');}
  /** The journal is read only for its tail; past its size the older part moves to the archive. */
  rotateJournal(journal) {
    try {
      if(fs.statSync(journal).size<this.hotLimits.journalBytes)return;
      fs.mkdirSync(this.archiveDirectory(),{recursive:true,mode:0o700});
      const target=path.join(this.archiveDirectory(),path.basename(this.file)+'.events-'+new Date(this.now()).toISOString().replace(/[:.]/g,'-')+'.jsonl');
      if(!fs.existsSync(target))fs.renameSync(journal,target);
    } catch {/* The journal stays where it is and is tried again later. */}
  }
  /** Ids of archived inputs: a replay of one is refused, never re-run. The id index
   * is read when present; the monthly files are scanned only without it. */
  loadArchivedIds() {
    const ids=new Set(),directory=this.archiveDirectory(),index=path.join(directory,'router-input-ids.txt');
    try {
      if(fs.existsSync(index)){for(const line of fs.readFileSync(index,'utf8').split('\n'))if(line)ids.add(line);return ids;}
      for(const name of fs.readdirSync(directory).filter(name=>/^router-\d{4}-\d{2}\.jsonl$/.test(name)))
        for(const line of fs.readFileSync(path.join(directory,name),'utf8').split('\n'))
          try {const entry=JSON.parse(line);if(entry.kind==='input'&&typeof entry.id==='string')ids.add(entry.id);} catch {}
    } catch {/* No archive yet. */}
    return ids;
  }
  /** Settled records beyond the hot tail move out of the state file, oldest first,
   * into the month they settled in. What is open, pending or still referenced stays. */
  prune() {
    const at=this.now(),byMonth=new Map(),movedIds=[];
    const move=(kind,id,record)=>{const key=month(record.settledAt??record.acceptedAt??record.at??record.createdAt??at);
      const list=byMonth.get(key)??[];list.push({kind,id,archivedAt:at,record});byMonth.set(key,list);};
    const inputs=Object.values(this.state.inputs),owner=inputs.filter(r=>ownerInput(r)&&inputSettled(r)),internal=inputs.filter(r=>!ownerInput(r)&&inputSettled(r));
    const limits=this.hotLimits,excess=[...owner.slice(0,Math.max(0,owner.length-limits.inputs)),...internal.slice(0,Math.max(0,internal.length-limits.internal))];
    const referenced=new Set(Object.values(this.state.tasks).filter(open).flatMap(t=>[...t.inputIds,...(t.contextInputIds??[])]));
    for(const record of excess)if(!referenced.has(record.id)&&!this.inflight.has(record.id)){
      move('input',record.id,record);delete this.state.inputs[record.id];delete this.state.semanticPending[record.id];this.archivedIds.add(record.id);movedIds.push(record.id);
    }
    // A deferral whose plan is not made yet, or whose failure Kin has not been told, is
    // still scheduled: it stays until that is done (CR2-LIFE-11).
    const closed=Object.values(this.state.tasks).filter(t=>!open(t)&&!deferralOwed(t));
    for(const task of closed.slice(0,Math.max(0,closed.length-limits.tasks))){move('task',task.id,task);delete this.state.tasks[task.id];}
    const settledNotice=n=>['accepted','rejected','failed','superseded','suppressed','unresolved'].includes(n.state);
    const kept=new Set([...Object.values(this.state.tasks).flatMap(t=>[t.handoff?.id,t.completion?.commandId,t.acceptedBy]),
      ...Object.values(this.state.notices).filter(n=>!settledNotice(n)).map(n=>n.requestId),...Object.values(this.state.reclassifications??{}).map(r=>r.requestId)].filter(Boolean));
    const requests=Object.values(this.state.requests).filter(r=>r.state!=='pending');
    for(const request of requests.slice(0,Math.max(0,requests.length-limits.requests))){
      const id=request.commandId??Object.keys(this.state.requests).find(key=>this.state.requests[key]===request);
      if(id&&!kept.has(id)){move('request',id,request);delete this.state.requests[id];}
    }
    const lastTold=Object.values(this.state.notices).filter(n=>n.state==='accepted').sort((a,b)=>(a.acceptedAt??0)-(b.acceptedAt??0)).at(-1)?.id;
    const notices=Object.values(this.state.notices).filter(n=>settledNotice(n)&&n.id!==lastTold);
    for(const notice of notices.slice(0,Math.max(0,notices.length-limits.notices))){move('notice',notice.id,notice);delete this.state.notices[notice.id];}
    if(!byMonth.size)return 0;
    fs.mkdirSync(this.archiveDirectory(),{recursive:true,mode:0o700});
    let moved=0;
    for(const [key,list] of byMonth){fs.appendFileSync(path.join(this.archiveDirectory(),'router-'+key+'.jsonl'),list.map(entry=>JSON.stringify(entry)).join('\n')+'\n',{mode:0o600});moved+=list.length;}
    if(movedIds.length){
      const index=path.join(this.archiveDirectory(),'router-input-ids.txt');
      // An index that is missing while archives exist is rebuilt from them first.
      if(!fs.existsSync(index))fs.writeFileSync(index,[...this.archivedIds].filter(id=>!movedIds.includes(id)).map(id=>id+'\n').join(''),{mode:0o600});
      fs.appendFileSync(index,movedIds.map(id=>id+'\n').join(''),{mode:0o600});
    }
    return moved;
  }
  /** With no revision left to restore, the append-only journal still names every
   * input this router took, by id and kind and nothing more. They come back
   * `unconfirmed` and `recovered`: a replay is refused until the watchdog has looked the
   * id up in the native session. Found, the input stands as accepted and a replay is a
   * duplicate; proven never received, it is known by its id alone, and a replay under
   * that id is routed afresh (WS8 #3). An id an older journal did not say the kind of
   * comes back as kind `unknown`: it is only looked up by its id and never told about,
   * since it may be Kin's own. Like any unconfirmed input they never hold the restart
   * profile (AD1-10). An input the journal says the owner's stop settled comes back as
   * that — canceled by the same stop, under its own id — and is never looked up, requeued
   * or routed again (§0). */
  restoreLedger() {
    let ids=[];const kinds=new Map(),submits=new Map(),canceled=new Map();
    try {
      const journal=this.file+'.events.jsonl',size=fs.statSync(journal).size,from=Math.max(0,size-LEDGER_TAIL_BYTES),handle=fs.openSync(journal,'r');
      let body='';
      try {const buffer=Buffer.alloc(size-from);fs.readSync(handle,buffer,0,buffer.length,from);body=buffer.toString('utf8');} finally {fs.closeSync(handle);}
      ids=[...new Set(body.split('\n').slice(from?1:0).map(line=>{
        try {
          const event=JSON.parse(line);
          if(!String(event?.kind).startsWith('input-')||typeof event.id!=='string')return null;
          if(typeof event.inputKind==='string'&&INPUT_KIND.test(event.inputKind))kinds.set(event.id,event.inputKind);
          if(event.kind==='input-submitting'&&typeof event.submit?.sessionId==='string')submits.set(event.id,{sessionId:event.submit.sessionId,runtime:typeof event.submit.runtime==='string'?event.submit.runtime:null,correlation:event.submit.correlation===true});
          // The stop that canceled it is its first; how far it had got is its latest word.
          if(typeof event.canceledBy==='string'&&event.canceledBy&&CANCEL_SCOPES.includes(event.scope))
            canceled.set(event.id,{canceledBy:canceled.get(event.id)?.canceledBy??event.canceledBy.slice(0,200),scope:event.scope,state:typeof event.state==='string'?event.state:null});
          return event.id;
        } catch {return null;}
      }).filter(Boolean))];
    } catch {/* No journal: this router never accepted anything here. */}
    for(const id of ids) {
      if(this.state.inputs[id])continue;
      const kind=kinds.get(id)??'unknown',submit=submits.get(id),cancel=canceled.get(id);
      this.state.inputs[id]=cancel?restoredCancel(id,kind,cancel,this.now(),submit):{id,state:'unconfirmed',recovered:true,kind,...(submit?{submit}:{}),at:this.now()};
    }
    return ids.length;
  }
  locked(fn) {
    const operation=this.tail.then(fn);this.tail=operation.catch(()=>{});return operation;
  }
  tasks() {return Object.values(this.state.tasks).filter(open);}
  /** The work that holds the lock and owes a report: open tasks Kin has taken on. A
   * proposal only hints at routing until she does (CR-LIFE-10, N5). */
  openWork() {return this.tasks().filter(task=>task.status!=='proposed');}
  snapshot() {return clone(this.state);}
  currentTask() {return this.tasks().at(-1);}
  adoptBinding(binding,actual) {
    if(binding.conversationId!==this.state.conversationId||binding.generation<=this.state.generation||actual.threadId!==binding.threadId||actual.nativeSessionId!==binding.nativeSessionId||!actual.known)throw Error('Unverified session promotion');
    this.sessionId=binding.threadId;this.state.sessionId=binding.threadId;this.state.nativeSessionId=binding.nativeSessionId;this.state.generation=binding.generation;this.state.actual=actual;
    this.save('session-promoted',{generation:binding.generation,threadId:binding.threadId});
  }
  /** The one gate for what starts beside the owner's conversation: a send or one of the
   * mind's runs. While dispatch is frozen nothing new starts; otherwise the activity is
   * counted in flight — by busy(), /busy and a freeze's `idle` — until release(). The
   * freeze check and the registration are one synchronous step, so no freeze can report
   * idle while an activity it did not see is starting (CR-LIFE-08, CR-MIND-01). */
  beginActivity({kind,id=null,channel=null}={}) {
    if(typeof kind!=='string'||!/^[a-z][a-z-]{0,39}$/.test(kind))throw Error('An activity needs its kind');
    if(this.frozen())return {ok:false,reason:'frozen'};
    const key=Symbol(kind);let released=false;
    this.activities.set(key,{kind,id:typeof id==='string'?id.slice(0,200):null,channel:typeof channel==='string'?channel.slice(0,32):null,at:this.now()});
    return {ok:true,release:()=>{if(released)return false;released=true;this.activities.delete(key);return true;}};
  }
  activityList() {return [...this.activities.values()].map(activity=>({...activity}));}
  /** Anything in flight. `owner` asks for the owner's own dispatch, which a run in its own
   * process never holds; `internal` asks for the mind's own turn, which orders the mind's
   * runs itself. A send in flight holds both; the mind's store work and the host's own
   * background model calls hold nothing here. */
  busy(runtime,{assessment=false,owner=false,internal=false}={}) {
    for(const activity of this.activities.values())if(!STORE_ACTIVITIES.has(activity.kind)&&!REVIEW_ACTIVITIES.has(activity.kind)&&!(internal&&MIND_ACTIVITIES.has(activity.kind))&&!(owner&&DETACHED_ACTIVITIES.has(activity.kind)))return true;
    if(Object.values(this.state.notices).some(n=>n.state==='sending'))return true;
    if(Object.values(this.state.inputs).some(record=>record.ownerNotice?.state==='sending'))return true;
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
    for(const job of received)if(!this.state.inputs[job.id]&&!this.archivedIds.has(job.id))
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
    for(const job of received)if(!this.state.inputs[job.id]&&!this.archivedIds.has(job.id))owner.received++;
    return {...owner,historical,internal,archived:this.archivedIds.size,frozen:this.frozen()?clone(this.state.freeze):null};
  }
  /** H3-20: the ledger's answer on an owner input cited as the authority for a desktop
   * hand-off, read from memory, so an input accepted a moment ago is already here. It
   * qualifies when the owner's own message reached Kin within handoffSourceMaxHours
   * (default 24). Whether it already authorized another hand-off is the exchange's to
   * check, and whether it asks for this task is Kin's. */
  handoffSource(id) {
    const maxAgeMs=(this.state.config.handoffSourceMaxHours??HANDOFF_SOURCE_MAX_HOURS)*3600000;
    const record=typeof id==='string'?this.state.inputs[id]:null;
    if(!record)return {state:'refused',reason:typeof id==='string'&&this.archivedIds.has(id)?'source-too-old':'source-not-found',sourceInputId:id??null,maxAgeMs};
    // Its age runs from when the host first received it, not from when it was classified (CR-LIFE-18).
    const received=Number.isFinite(record.firstReceivedAt)?record.firstReceivedAt:record.at;
    const ageMs=Number.isFinite(received)?Math.max(0,this.now()-received):null;
    const facts={sourceInputId:record.id,kind:record.kind??'owner',inputState:record.state,at:record.at??null,firstReceivedAt:record.firstReceivedAt??null,acceptedAt:record.acceptedAt??null,ageMs,maxAgeMs};
    const reason=!ownerInput(record)?'source-not-owner':record.state!=='accepted'?'source-not-accepted':ageMs===null||ageMs>maxAgeMs?'source-too-old':null;
    return reason?{state:'refused',reason,...facts}:{state:'eligible',...facts};
  }
  frozen() {
    const freeze=this.state.freeze;
    // KIN-FIX-20260924, CR3-FLOW-03: a deployment's hold (a first release waiting for a forward
    // fix of a runtime that failed its proof) does not lift itself at `until`: past it the freeze
    // is overdue and waits for an explicit thaw. Every other freeze keeps its TTL.
    return Boolean(freeze&&(freeze.hold===true||!(Number.isFinite(freeze.until)&&freeze.until<=this.now())));
  }
  /** Stop new dispatch — both channels, the mind's turns, handoffs and mode changes
   * nobody forced — while in-flight work settles. The owner's literal stop and her
   * own mode commands still act. It survives a restart and lifts itself at `until`
   * (two hours unless asked otherwise), so a release or migration that is lost half
   * way can never leave the owner unanswered for good. Asking again for the same
   * migration changes nothing, so a caller may poll it while it drains. A deployment's
   * hold stays on a freeze that replaces it: only a thaw ends it (CR3-FLOW-03). `hold`
   * asks for one: a release left pending holds the running router at once (CR3-REL-01/02).
   * A hold another holder put stays that holder's (`holdOf`), so a thaw by the holder that
   * replaced it gives it back instead of ending it (CR4-REL-04). */
  freezeDispatch(reason,{ttlMs=FREEZE_TTL_MS,migrationId=null,by=null,hold=false}={}) {
    return this.locked(async()=>{
      if(typeof reason!=='string'||!reason.trim())throw Error('A freeze needs a reason');
      const current=this.frozen()?this.state.freeze:null,id=migrationId?String(migrationId).slice(0,120):null;
      if(current&&current.reason===reason.trim().slice(0,200)&&(current.migrationId??null)===id&&(hold!==true||current.hold===true))return clone(current);
      const ttl=Math.min(Math.max(Number.isFinite(ttlMs)?ttlMs:FREEZE_TTL_MS,60000),12*3600000);
      // KIN-FIX-20260924, CR4-REL-04: whose hold this freeze carries, when it is not this holder's own.
      const holder={reason:reason.trim().slice(0,200),migrationId:id},same=(a,b)=>a.reason===b.reason&&(a.migrationId??null)===(b.migrationId??null);
      const under=current?.hold?current.holdOf??(same(current,holder)?null:{reason:current.reason,...(current.migrationId?{migrationId:current.migrationId}:{}),
        ...(current.by?{by:current.by}:{}),at:current.at,until:current.until}):null;
      const holdOf=under&&!same(under,holder)?under:null;
      this.state.freeze={reason:holder.reason,at:current?.at??this.now(),until:this.now()+ttl,...(id?{migrationId:id}:{}),...(by?{by:String(by).slice(0,120)}:{}),...(current?.hold||hold===true?{hold:true}:{}),...(holdOf?{holdOf}:{})};
      this.save('dispatch-frozen',{reason:this.state.freeze.reason,until:this.state.freeze.until,...(id?{migrationId:id}:{})});
      return clone(this.state.freeze);
    });
  }
  /** Lift the freeze. A thaw by holder (CR4-REL-04) -- `holder` {reason, migrationId}, and any
   * thaw that names its migration, whose holder is then that migration's freeze -- lifts only a
   * freeze that holder put: another's is left as it is (`not-own`), and a hold another put,
   * which stayed on this holder's freeze, is given back to its holder, held (`hold-kept`).
   * Any other thaw -- a release's own, a person's -- lifts whatever freeze there is. */
  thawDispatch(reason='thawed',{migrationId=null,holder=migrationId?{reason,migrationId}:null}={}) {
    return this.locked(async()=>{
      const previous=this.state.freeze;if(!previous)return {state:'not-frozen'};
      const thawed={reason:String(reason).slice(0,200),frozenAt:previous.at,...(migrationId?{migrationId:String(migrationId).slice(0,120)}:{})};
      if(holder) {
        const own=previous.reason===String(holder.reason??'').trim().slice(0,200)&&(previous.migrationId??null)===(holder.migrationId?String(holder.migrationId).slice(0,120):null);
        if(!own)return {state:'not-own',heldBy:previous.reason,...(previous.migrationId?{heldByMigration:previous.migrationId}:{}),...(previous.hold?{hold:true}:{}),frozen:this.frozen()};
        if(previous.holdOf) {
          this.state.freeze={...previous.holdOf,hold:true};this.save('dispatch-thawed',{...thawed,holdKept:previous.holdOf.reason});
          return {state:'hold-kept',frozenAt:previous.at,...(previous.migrationId?{migrationId:previous.migrationId}:{}),heldBy:previous.holdOf.reason};
        }
      }
      delete this.state.freeze;this.save('dispatch-thawed',thawed);
      return {state:'thawed',frozenAt:previous.at,...(previous.migrationId?{migrationId:previous.migrationId}:{})};
    });
  }
  /** Facts about open work for Kin's next turn. States what is true; asks nothing. */
  workFacts() {
    // A deferral whose plan is not written yet, or was left to her, is a fact for Kin too (CR-MIND-03).
    const deferred=Object.values(this.state.tasks).filter(t=>t.status==='deferred'&&['pending','needs-kin'].includes(t.deferral?.plan?.state));
    return [...this.tasks(),...deferred].map(task=>{
      const deliveries=Object.entries(task.deliveries??{});
      return {id:task.id,status:task.status,request:String(task.summary??'').slice(0,200),inputVersion:task.inputVersion,
        ...(task.status==='proposed'?{acceptance:'not-yet-accepted'}:{acceptedAt:task.acceptedAt??null}),
        ...(task.completion?.outcome&&task.completion.state!=='historical-proposal'?{declared:{outcome:task.completion.outcome,at:task.completion.at}}:{}),
        ...(task.interruptedBy?{interruptedBy:task.interruptedBy}:{}),...(task.cancelRequested?{cancelRequested:true}:{}),
        turn:{startedAt:task.turnStartedAt??null,endedAt:task.turnEndedAt??null,stopReason:task.stopReason??null},
        toolsRunning:Object.values(task.tools??{}).filter(tool=>!terminalTool(tool)).length,
        unsentDrafts:deliveries.filter(([,d])=>['deferred','not-submitted'].includes(d.state)&&!d.fulfilledBy).map(([id])=>id).slice(0,16),
        unknownDeliveries:deliveries.filter(([,d])=>['unconfirmed','unknown'].includes(d.state)).map(([id])=>id).slice(0,16),
        ...(task.workSummary?{workSummary:clone(task.workSummary)}:{}),...(task.deferral?{deferral:clone(task.deferral)}:{}),
        // Her handoff that has not reached the session yet, or failed to, is a fact for her too (WS8 #2).
        ...(task.handoff&&task.handoff.state!=='accepted'?{handoff:{state:task.handoff.state,...(task.handoff.reason?{reason:task.handoff.reason}:{})}}:{})};
    });
  }
  async availableModels(runtime=null) {
    try{const models=normalizeModelCatalog(await this.modelCatalog?.(runtime)??[]);this.rememberModelNames(models);return models;}catch{return [];}
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
  automaticIdleProfile() {return clone(this.state.autoReturnProfile??this.state.autoIdleProfile??this.profiles.chat);}
  desiredProfile(runtime,{route=null}={}) {
    if(this.state.mode==='manual'&&this.state.manualProfile)return clone(this.state.manualProfile);
    if(this.openWork().length||this.state.mode==='work'||route==='work')return clone(this.profiles.work);
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
  /** Open work for this input. The classifier's `work` is only a label: an owner
   * request becomes a proposal that keeps its own turn on the work profile and lapses
   * when that turn ends unless Kin takes it on, declares an outcome for it, or declines
   * it. Kin's own handoffs and the host's internal jobs are commitments (N5). */
  addTask(input,{proposal=false}={}) {
    let task=this.tasks().filter(item=>!item.cancelRequested).at(-1);
    if(!task) {
      const id='work-'+digest(input.id).slice(0,24),internal=['repair','exploration-plan','proactive','assessment'].includes(input.kind);
      task={id,conversationId:this.state.conversationId,generation:this.state.generation,executionEpoch:this.state.executionEpoch,status:proposal?'proposed':'running',requiresDelivery:!internal&&!proposal,inputVersion:0,inputIds:[],summary:input.text,tools:{},deliveries:{},createdAt:this.now(),
        ...(proposal?{}:{acceptedAt:this.now(),acceptance:internal?'host-internal':'kin'})};
      this.state.tasks[id]=task;
    }
    if(!task.inputIds.includes(input.id)) {this.supersedeDecline(task,'new-work-input:'+input.id);task.inputIds.push(input.id);task.inputVersion++;delete task.completion;}
    if((!input.kind||input.kind==='owner')&&task.status!=='proposed')task.requiresDelivery=true;
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
  /** The owner's literal stop, under the mutex: the running native turn is cancelled
   * through the host's native interrupt and output fence, and the result is kept on
   * the stop input by its id. A confirmed cut settles the cancellation at once; an
   * unconfirmed one is settled by reconcile() once the session is seen idle
   * (CR-LIFE-01). With nothing running there is nothing to cut. */
  async interruptForStop(record,runtime) {
    const running=Boolean(runtime?.active||runtime?.nativeStatus&&!['idle','systemError'].includes(runtime.nativeStatus));
    const turnInputIds=[...(this.state.turn?.inputIds??[])].filter(id=>id!==record.id);
    if(!running){record.interrupt={state:'not-needed',at:this.now(),turnInputIds};this.settleOwnerStop(record);this.save('stop-settled',{id:record.id});return runtime;}
    record.interrupt={state:'interrupting',at:this.now(),turnInputIds};this.save('stop-interrupting',{id:record.id});
    let receipt;
    if(!this.interruptTurn)receipt={state:'unconfirmed',reason:'native-interrupt-unavailable'};
    else try {receipt=await this.interruptTurn({sessionId:this.sessionId,sourceInputId:record.id});}
    catch(error) {receipt={state:'unconfirmed',reason:'native-interrupt-request-failed'};}
    const confirmed=['interrupted','idle'].includes(receipt?.state);
    Object.assign(record.interrupt,{state:confirmed?'confirmed':receipt?.state==='failed'?'failed':'unconfirmed',result:receipt?.state??null,
      ...(receipt?.reason?{reason:String(receipt.reason).slice(0,120)}:{}),cancelledTurn:Boolean(receipt?.cancel?.cancelledTurn),checkedAt:this.now()});
    if(confirmed)this.settleOwnerStop(record);
    this.save('stop-interrupt-'+record.interrupt.state,{id:record.id});
    try {return await this.inspect();} catch {return runtime;}
  }
  /** What the owner's stop settles once nothing of the stopped turn runs: the work it
   * names is canceled, and the inputs that turn was answering are canceled by her. */
  settleOwnerStop(record) {
    const at=this.now();
    for(const task of this.tasks())if(task.cancelRequested&&task.cancelSourceInputId===record.id)this.cancelTask(task,at);
    for(const id of record.interrupt?.turnInputIds??[]) {
      const other=this.state.inputs[id];
      if(other&&ownerInput(other)&&other.state==='accepted'&&!answered(other))this.cancelSettled(other,record.id,at);
    }
  }
  cancelTask(task,at=this.now()) {
    task.status='canceled';task.canceledAt=at;task.outcome='owner-canceled';
    const queued=[],by=task.cancelSourceInputId??'owner-stop';
    for(const id of task.inputIds){
      const record=this.state.inputs[id];if(!record||task.cancelSourceInputId===id)continue;
      // Not submitted yet: it loses its reservation and its right to submit (CR2-LIFE-01).
      if(['selected','preparing'].includes(record.state)&&!record.submissionStartedAt)this.cancelBeforeSubmission(record,by);
      // Waiting in the host's queue, its prompt not begun: taken back by its own id (CR3-FLOW-02).
      else if(record.state==='queued'&&!record.turnStartedAt)queued.push(record);
      // Anything else of hers still unanswered is settled as hers.
      else if(ownerInput(record)&&!answered(record))this.cancelSettled(record,by,at);
    }
    this.withdrawQueuedWork(queued,by);
  }
  /** The owner's stop settles an input as hers where there is no dispatch to withdraw: its
   * turn was cut, its submission is unknown, or it had already failed before submission.
   * The first stop to reach it stays its receipt, and the journal keeps it with how far the
   * input had got, so a restore from the journal alone keeps it too. */
  cancelSettled(record,stoppedBy,at=this.now()) {
    if(record.canceledBy||record.historical||record.state==='superseded')return false;
    record.canceledBy=stoppedBy;record.settledAt??=at;
    const scope=['accepted','submitting','unconfirmed','fenced-unconfirmed'].includes(record.state)?'native-session':'preparation';
    this.save('input-canceled',{id:record.id,...cancelFacts(record,scope)});
    return true;
  }
  /** The owner's stop, from the literal command or from a classifier that actually
   * answered: recorded on the stop input itself, applied to the tasks it names. */
  applyStop(inputId,stop) {
    const queued=[];
    for(const task of this.tasks())if(!stop.taskIds||stop.taskIds.includes(task.id)){
      task.cancelRequested=true;task.cancelSourceInputId=inputId;
      for(const id of task.inputIds){
        const record=this.state.inputs[id];if(!record||id===inputId)continue;
        // What the stopped work has not submitted yet loses its reservation and its right to submit (CR2-LIFE-01).
        if(['selected','preparing'].includes(record.state)&&!record.submissionStartedAt)this.cancelBeforeSubmission(record,inputId);
        // What of it waits in the host's queue is taken back by its own id before its prompt begins (CR3-FLOW-02).
        else if(record.state==='queued'&&!record.turnStartedAt)queued.push(record);
      }
    }
    this.withdrawQueuedWork(queued,inputId);
  }
  /** The owner's stopped work that waits in the host's queue, its prompts not begun: each
   * input is canceled by her under its own id, and the host takes the messages that carry
   * only such inputs out of its queue. Anything else there keeps its place — whatever came
   * after the stop included. What the host could not find is refused where its prompt
   * would begin (CR3-FLOW-02). */
  withdrawQueuedWork(records,stoppedBy) {
    if(!records.length)return;
    for(const record of records)this.cancelQueued(record,stoppedBy,'host-queue');
    let taken=[];
    try {taken=this.withdrawQueued?.(records.map(record=>record.id))??[];} catch {taken=[];}
    for(const record of records) {
      record.withdrawn.fromQueue=taken.includes(record.id);
      this.save('input-dispatch-withdrawn',{id:record.id,reason:'canceled-by-owner',...cancelFacts(record,'host-queue'),fromQueue:record.withdrawn.fromQueue});
    }
  }
  /** An input handed to the host's queue, its prompt not begun, that the owner's stop
   * reached: canceled by her under its own id, never requeued and never renamed. Nothing
   * of it reached the native session (CR3-FLOW-02). */
  cancelQueued(record,stoppedBy,stage) {
    record.canceledBy??=stoppedBy;record.settledAt??=this.now();
    this.releaseQueued(record,'canceled-by-owner',stage);
    record.withdrawn={reason:'canceled-by-owner',at:this.now(),stage};
  }
  /** Handed to the host's queue, or being handed to it, is not a native submission: that
   * fact is kept apart, and the input is not submitted. */
  releaseQueued(record,reason,stage) {
    if(record.submissionStartedAt){record.queuedSubmissionAt=record.submissionStartedAt;delete record.submissionStartedAt;}
    this.notSubmitted(record,reason,{restart:true,stage});
    this.settleAcceptance(record.id,{state:record.state});
  }
  /** The owner's stop is read again where a prompt would begin, in the step that records
   * its start: a prompt that carries work she stopped never begins. What it carries of that
   * work is canceled under its own ids; anything else it carries — a message merged into it
   * after the stop — is not submitted, and goes again under its own id (CR3-FLOW-02). */
  refusedPrompt(data) {
    const ids=[...new Set(Array.isArray(data.inputIds)?data.inputIds:data.sourceInputId?[data.sourceInputId]:[])];
    const records=ids.map(id=>this.state.inputs[id]).filter(record=>record&&!record.turnStartedAt);
    const handing=record=>['queued','submitting'].includes(record.state);
    // Taken back already, where the host's queue did not hold it, or stopped since it was handed over.
    const stopped=records.filter(record=>record.state==='failed-before-submit'&&record.canceledBy||handing(record)&&this.stopOf(record));
    if(!stopped.length)return null;
    for(const record of stopped.filter(handing)) {
      this.cancelQueued(record,this.stopOf(record),'prompt-start');
      this.save('input-dispatch-withdrawn',{id:record.id,reason:'canceled-by-owner',...cancelFacts(record,'prompt-start')});
    }
    const carried=records.filter(record=>!stopped.includes(record)&&handing(record));
    for(const record of carried) {
      this.releaseQueued(record,'carried-with-stopped-work','prompt-start');
      this.save('input-failed-before-submit',{id:record.id,reason:record.reason});
    }
    const refused={state:'refused',canceled:stopped.map(record=>record.id),notSubmitted:carried.map(record=>record.id)};
    this.save('prompt-refused',{canceled:refused.canceled,notSubmitted:refused.notSubmitted});
    return refused;
  }
  /** The mind's own turns wait for the owner's work and for a coordinator that is busy. */
  internalHeld(runtime,kind) {
    return this.busy(runtime,{assessment:kind==='assessment',internal:true})||this.openWork().length>0||this.state.mode==='work';
  }
  async select(input) {
    const hash=digest([input.text,input.attachments??[]]);
    const owner=!input.kind||input.kind==='owner',intents=this.classifyIntents&&owner;
    // Phase one, under the mutex: identity, literal commands and everything that needs
    // no model. The classification itself never holds the mutex (AD1-06).
    const first=await this.locked(async()=>{
      // CR-LIFE-02: a record the host made when it took the input in is not routed yet.
      const previous=intakeOnly(this.state.inputs[input.id])?null:this.state.inputs[input.id];
      // An archived input was settled long ago: a replay of it is never re-run (AD1-08).
      if(!previous&&this.archivedIds.has(input.id))return {record:{id:input.id,state:'accepted',archived:true}};
      if(previous) {
        if(previous.recovered)throw Error('Input acceptance requires reconciliation');
        // A record restored from the journal kept the id and not the content: there is nothing to compare.
        if(!(previous.restored&&!previous.hash)&&previous.hash!==hash)throw Error('Input id reused with different content');
        if(['submitting','unconfirmed'].includes(previous.state))throw Error('Input acceptance requires reconciliation');
        // What predates the ledger keeps its reason and id and is never re-run.
        if(previous.historical)throw Object.assign(Error('Historical input is not re-run: '+previous.historical.reason),{code:'input-historical'});
        // What the owner's stop withdrew stays withdrawn (CR2-LIFE-01).
        if(previous.state==='failed-before-submit'&&!previous.canceledBy){
          if(this.frozen())throw frozenError(this.state.freeze.reason);
          previous.state='selected';previous.retry={...(previous.retry??{attempts:0}),lastAttemptAt:this.now()};
          delete previous.reason;delete previous.failureStage;delete previous.ownerNotice;delete previous.withdrawn;
          this.save('input-preparation-retry',{id:input.id,attempt:previous.retry.attempts});
        }
        return {record:clone(previous)};
      }
      // The literal stop is read before any model is asked, and answers on its own when none can be.
      const stop=STOP_LITERAL.test(input.text.trim());
      const command=stop?'stop':owner&&!input.attachments?.length?(modeCommand(input.text)??(input.text.trim()==='/compact'?'compact':null)):null;
      if(this.frozen()&&!(owner&&(stop||['work','auto'].includes(command))))throw frozenError(this.state.freeze.reason);
      const runtime=await this.inspect();
      const priorTask=this.currentTask();
      if(priorTask?.completion?.outcome==='declined'&&this.declarationReady(priorTask,runtime))this.closeTask(priorTask,{event:'decline-settled-before-input'});
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
      // The host stating facts about open work is context for Kin, never a new requirement (N2).
      else if(input.kind==='work-facts'){decision='chat';reason='host-work-facts';}
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
    let tail=null,classified=false,semanticFailure=null,read=null;
    if(first.decided) {
      // A literal stop needs no model: whatever is still unsent is withdrawn by the host itself.
      if(owner&&first.decided.command==='stop'&&this.replyTail)tail={carrier:'owner-stop',...(await port('stopped',{inputId:input.id}))};
    } else {
      classified=true;
      const files=intents?attachmentMetadata(input.attachments):[];
      try {
        const result=await this.askClassification(input,{files,intents,runtime:first.classify.runtime,wait:this.state.config.classifierTimeoutMs});
        read=this.readClassification(result,{owner,intents,allowStop:true});
      } catch(error) {
        // KIN-ITER-20260918-02 REVISES the old tradeoff in which a classification
        // anomaly always became work: the catch manufactured a provisional task and
        // a GPT switch out of a chat nobody had read. A failure now decides
        // nothing — no task, no provider switch, no execution grant — and the
        // input waits as itself for the bounded review, failure class on record.
        semanticFailure=classificationFailure(error);
      }
    }
    // Phase two, under the mutex: the answer applies only against the basis it was read from.
    return this.locked(async()=>{
      // CR-LIFE-02: a record the host made on intake is replaced by the routed one, which
      // keeps the retries this input already used.
      const intake=intakeOnly(this.state.inputs[input.id])?this.state.inputs[input.id]:null;
      if(this.state.inputs[input.id]&&!intake)return clone(this.state.inputs[input.id]);
      const now=this.now();
      // When the host first received it is recorded once; routing never moves it later (CR2-LIFE-08).
      const firstReceivedAt=Math.min(receiptTime(input.receivedAt,now),Number.isFinite(intake?.firstReceivedAt)?intake.firstReceivedAt:Infinity);
      // How often it went back to its inbox is the input's own, counted across every stage:
      // routing never starts it again, and it is kept apart from this attempt's evidence (CR3-FLOW-10).
      const base={id:input.id,hash,kind:input.kind??'owner',at:now,firstReceivedAt,
        ...(INPUT_CHANNELS.has(input.channel)?{channel:input.channel}:{}),conversationId:this.state.conversationId,generation:this.state.generation,nativeThreadId:this.sessionId,...(tail?{tail}:{}),
        ...(intake?.retry?{retry:clone(intake.retry)}:{}),...(requeuesOf(intake)?{requeues:requeuesOf(intake)}:{})};
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
  async askClassification(input,{files=[],intents=false,runtime=null,wait}) {
    // The catalog is read before the clock starts, and the clock always has a
    // listener: a slow catalog can never leave a rejection nobody handles (AD1-05).
    const availableModels=await this.availableModels(runtime);
    // One deadline for the whole call, the lane's admission and the request alike. When it
    // passes the call is canceled — a lease that comes late starts nothing — and this
    // answers only once the call has let go of everything it held, so an activity it runs
    // under is held until then too (CR4-FLOW-01).
    const deadline=new AbortController();
    let timer;
    const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>{deadline.abort(Error('classification-timeout'));reject(Error('classification-timeout'));},wait);});
    timeout.catch(()=>{});
    const call=Promise.resolve().then(()=>this.classify({text:input.text,clock:conversationClock(input,this.now()),recent:recentConversation(this.state.recent),task:this.currentTask()?.summary??null,mode:this.state.mode,currentProfile:this.state.manualProfile??(runtime?runtimeProfile(runtime):null),workHeld:Boolean(this.openWork().length||runtime?.active),availableModels,timeoutMs:wait,...(files.length?{attachments:files}:{}),...(intents?{intents:true}:{})},{signal:deadline.signal}));
    try {return await Promise.race([call,timeout]);}
    finally {clearTimeout(timer);await call.then(()=>{},()=>{});}
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
    const current=this.openWork().filter(task=>!task.cancelRequested).at(-1)??null;
    if(!command&&(current||this.state.mode==='work')){decision='work';reason='work-lock: '+reason;}
    // The owner's stop is read by the task it stops; it never opens or extends work (AD1-01).
    const task=stop?this.tasks().filter(item=>stop.taskIds?.includes(item.id)).at(-1)??current:
      decision==='work'&&!command?(intent==='work'?(current?this.addTask(input):this.addTask(input,{proposal:ownerInput(input)})):current):command?null:current;
    if(task&&!task.inputIds.includes(input.id)){task.contextInputIds??=[];task.contextInputIds.push(input.id);}
    return {decision,reason,task};
  }
  rememberOwner(input) {
    this.wakeNotices();
    this.state.recent.push({role:'user',text:input.text,at:input.occurredAt??input.at??this.now(),receivedAt:input.receivedAt??this.now()});
    this.state.recent=this.state.recent.slice(-16);
  }
  /** Record that nothing of this input reached the native session, with that
   * evidence and the moment the same id may be tried again (AD1-02, H2a-03). */
  notSubmitted(record,reason,{restart=false,stage='preparation'}={}) {
    // Handed only to the in-memory queue is not a submission: the fact is kept apart.
    if(record.state==='queued'&&record.submissionStartedAt){record.queuedSubmissionAt=record.submissionStartedAt;delete record.submissionStartedAt;}
    // How often it went back to its inbox counts across attempts; this attempt's evidence is its own (CR2-LIFE-05).
    record.requeues=requeuesOf(record);if(!record.requeues)delete record.requeues;
    record.state='failed-before-submit';record.failureStage=stage;record.reason=reason;if(!record.canceledBy)delete record.settledAt;
    const attempts=(record.retry?.attempts??0)+(restart?0:1),delay=SUBMIT_RETRY_MS[Math.max(0,attempts-1)];
    record.retry={attempts,evidence:'not-submitted',lastFailureAt:this.now(),...(delay===undefined?{exhausted:true}:{nextAt:this.now()+(restart?0:delay)})};
    return record.retry;
  }
  dispatch(input,submit) {
    if(this.inflight.has(input.id))return this.inflight.get(input.id);
    const pending=this.dispatchOnce(input,submit).catch(async error=>{
      const retry=await this.locked(()=>{
        const record=this.state.inputs[input.id];
        // CR-LIFE-02: a freeze took nothing; an input known only from its intake waits in the
        // inbox as it is, and the refusal counts no attempt.
        if(error?.code==='dispatch-frozen'&&intakeOnly(record)&&record.state==='preparing'){this.notSubmitted(record,'dispatch-frozen',{restart:true,stage:'intake'});this.save('input-failed-before-submit',{id:input.id,reason:'dispatch-frozen'});return null;}
        if(record&&['selected','preparing'].includes(record.state)&&!record.submissionStartedAt){
          const retry=this.notSubmitted(record,error.message);
          this.save('input-failed-before-submit',{id:input.id,attempt:retry.attempts});return retry;
        }
        return record?.state==='failed-before-submit'?record.retry:null;
      });
      // The caller learns that the same id may be tried again, and when.
      if(retry&&!error.code)Object.assign(error,{code:'input-not-submitted',inputId:input.id,retryAt:retry.nextAt??null,retryExhausted:Boolean(retry.exhausted)});
      throw error;
    }).finally(()=>{this.inflight.delete(input.id);this.reservations.delete(input.id);this.deadlines.delete(input.id);});
    this.inflight.set(input.id,pending);return pending;
  }
  /** Called under the router mutex by the existing minute review. A submitted input owns
   * its original reconciliation identity. A live dispatch is held to its own deadline even
   * while its preparation hangs: past it, the submission it has not made is withdrawn and a
   * late one is refused (CR2-LIFE-03). */
  expireUnsubmittedInputs() {
    const now=this.now(),cutoff=now-(this.state.config.workReviewIntervalMinutes??20)*60000;
    for(const record of Object.values(this.state.inputs)) {
      if(!['selected','preparing'].includes(record.state)||record.submissionStartedAt)continue;
      if(this.inflight.has(record.id)) {
        const deadline=this.deadlines.get(record.id);
        if(Number.isFinite(deadline)&&now>=deadline)this.withdrawDispatch(record,'dispatch-wait-exceeded');
        continue;
      }
      if(!(record.at<=cutoff))continue;
      this.notSubmitted(record,'input-preparation-timeout');
      this.save('input-failed-before-submit',{id:record.id,reason:record.reason});
    }
  }
  /** Who stopped this input before it was submitted: the owner's stop that canceled it, or
   * the stop asked for the task it was given to (CR2-LIFE-01). The stop itself is not one. */
  stoppedBy(record) {
    if(!record||record.submissionStartedAt)return null;
    return this.stopOf(record);
  }
  /** Who stopped this input, by the facts alone, whatever became of its submission. */
  stopOf(record) {
    if(!record)return null;
    if(record.canceledBy)return record.canceledBy;
    const task=record.taskId?this.state.tasks[record.taskId]:null;
    if(!task||!task.inputIds?.includes(record.id)||task.cancelSourceInputId===record.id)return null;
    return task.cancelRequested||task.status==='canceled'?task.cancelSourceInputId??'owner-stop':null;
  }
  /** A dispatch that may no longer submit: the owner stopped it, or it outlived its
   * deadline. Its reservation goes and a late submission is refused; nothing of it
   * reached the native session (CR2-LIFE-01, CR2-LIFE-03). */
  withdrawDispatch(record,reason,{stage='dispatch'}={}) {
    this.reservations.delete(record.id);
    if(record.submissionStartedAt||record.withdrawn||!['semantic-pending','selected','preparing','failed-before-submit'].includes(record.state))return false;
    if(record.state!=='failed-before-submit')this.notSubmitted(record,reason,{stage});
    record.withdrawn={reason,at:this.now()};
    this.save('input-dispatch-withdrawn',{id:record.id,reason,...(reason==='canceled-by-owner'&&record.canceledBy?cancelFacts(record,'preparation'):{})});
    return true;
  }
  /** The owner's stop reached an input before submission: it is canceled by her, and withdrawn.
   * One withdrawn already (its deadline passed) has the stop journaled on its own. */
  cancelBeforeSubmission(record,stoppedBy) {
    const first=!record.canceledBy;
    record.canceledBy??=stoppedBy;record.settledAt??=this.now();
    const withdrawn=this.withdrawDispatch(record,'canceled-by-owner',{stage:'owner-stop'});
    if(!withdrawn&&first)this.save('input-canceled',{id:record.id,...cancelFacts(record,'preparation')});
    return withdrawn;
  }
  async dispatchOnce(input,submit) {
    const selected=await this.select(input);
    if(selected.state==='deferred')return {route:'deferred',reason:selected.reason};
    if(['accepted','queued','superseded'].includes(selected.state))return {route:'deduplicated',model:selected.model};
    if(selected.canceledBy&&!selected.submissionStartedAt&&selected.state==='failed-before-submit')return {route:'canceled-by-owner'};
    // A dispatch waits for the coordinator, a switch or a classification, never for
    // ever: past its deadline nothing has been submitted, and the same id is retried
    // or reported like any other unsubmitted input. The watchdog holds it to the same
    // deadline while a preparation hangs (CR2-LIFE-03).
    const deadline=this.now()+(this.state.config.dispatchWaitMinutes??20)*60000;
    this.deadlines.set(input.id,deadline);
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
    if(['accepted','queued','superseded'].includes(record.state))return {outcome:{route:'deduplicated',model:record.model}};
    // The owner's stop, or its own deadline, took this dispatch's right to submit (CR2-LIFE-01, CR2-LIFE-03).
    const stopped=this.stoppedBy(record);
    if(stopped){this.cancelBeforeSubmission(record,stopped);return {outcome:{route:'canceled-by-owner'}};}
    if(record.withdrawn)throw withdrawnError(record);
    if(record.state==='semantic-pending')return {wait:true};
    if(record.state!=='selected')throw Error('Input acceptance requires reconciliation');
    let runtime=await this.reconcileTransition(await this.inspect());
    if(['proactive','assessment'].includes(input.kind)&&(this.busy(runtime,{assessment:input.kind==='assessment',internal:true})||this.openWork().length||this.state.mode==='work'))return {outcome:{route:'deferred',reason:'owner-work-held'}};
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
    if(record.route==='work'&&['manual','auto','work'].includes(record.command)&&!record.taskId)record.taskId=this.addTask(input,{proposal:ownerInput(input)}).id;
    // The owner's literal stop cuts the running turn itself, then reaches Kin as a turn of its own (CR-LIFE-01).
    if(record.stop?.decisionSource==='literal-owner-command'&&!record.interrupt)runtime=await this.interruptForStop(record,runtime);
    if(record.route==='maintenance'&&this.busy(runtime))return {wait:true};
    // A stop is answered on whatever is running: it never switches the model.
    let targetProfile=record.route==='maintenance'||input.kind==='assessment'||record.unlabeled||record.command==='stop'?runtimeProfile(runtime):this.desiredProfile(runtime,{route:record.route});
    if(input.kind==='assessment'&&(!runtime.known||runtime.profileReady===false))return {outcome:{route:'deferred',reason:'native-profile-unconfirmed'}};
    // A native runtime that cannot be read, or a switch still being reconciled, is a
    // wait: nothing has been submitted, and nothing is failed for it (AD1-02).
    if(!runtime.known||this.state.transition?.state==='unconfirmed'){this.noteWait(record,!runtime.known?'native-runtime-unknown':'switch-unconfirmed');return {wait:true};}
    if(record.route==='work'&&this.state.mode!=='manual'&&!this.captureAutomaticReturn(runtime))throw Error('Automatic return profile unverified');
    if(record.route!=='maintenance'&&!record.unlabeled&&record.command!=='stop'&&(this.openWork().length||this.state.mode==='work')&&this.state.mode!=='manual')targetProfile=clone(this.profiles.work);
    try {targetProfile=await this.resolvedProfile(targetProfile);}
    catch(error) {if(/Canonical model provider unavailable|catalog/i.test(String(error?.message))){this.noteWait(record,'model-catalog-unavailable');return {wait:true};}throw error;}
    let target=targetProfile.model;
    // A queued request must not change the provider mid-turn, nor under another
    // dispatch that is planned but not yet handed to the session.
    const otherReserved=[...this.reservations.keys()].some(id=>id!==input.id);
    if((!this.verified(runtime,targetProfile)||runtime.profileReady===false)&&(this.busy(runtime,{owner:ownerInput(input)})||otherReserved))return {wait:true};
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
          if(!record.taskId&&!record.command&&record.route==='work')record.taskId=this.addTask(input,{proposal:ownerInput(input)}).id;
          this.save('switch-failed-restored');
        } catch {this.state.transition.state='unconfirmed';this.save('switch-unconfirmed');throw Error('Provider switch requires reconciliation');}
      }
    } else this.state.actual=runtime;
    if(record.route==='work'&&!record.taskId&&!record.command&&this.currentTask())record.taskId=this.currentTask().id;
    // A proactive contact goes out on the current tier's model, as any turn does (AD1-16).
    record.state='preparing';record.planAt=this.now();record.model=target;record.executionEpoch=this.state.executionEpoch;delete record.waitingReason;this.save('input-preparing',{id:input.id});
    record.plannedTransition=this.state.transition?.id??null;
    this.reservations.set(input.id,{profile:targetProfile,at:this.now()});
    return {decision:{model:target,profile:targetProfile,taskId:record.taskId,intent:record.intent,reason:record.reason,command:record.command,inputId:record.id,
      inputVersion:record.taskId?this.state.tasks[record.taskId].inputVersion:null,turnFence:this.state.executionEpoch,...(record.interrupt?{newTurn:true}:{})},
      // Where it goes and whether that runtime records input ids, for any later lookup (CR2-INT-02).
      submit:{sessionId:this.sessionId,runtime:this.runtimeId,correlation:runtime.inputCorrelation===true}};
  }
  /** Phase two: the host prepares its prompt outside the mutex (AD1-06), then marks
   * the submission under it, re-checking the basis the dispatch was planned on. */
  async submitPlanned(input,plan,submit) {
    let marking=null;
    const markSubmitted=()=>marking??=this.locked(async()=>{
      const record=this.state.inputs[input.id];
      // The last check before anything is sent: the owner's stop, or the dispatch's own
      // deadline, took its right to submit (CR2-LIFE-01, CR2-LIFE-03).
      const stopped=this.stoppedBy(record);
      if(stopped){this.cancelBeforeSubmission(record,stopped);throw Object.assign(Error('Input canceled by the owner before submission'),{code:'input-canceled',inputId:input.id});}
      if(record.withdrawn)throw withdrawnError(record);
      // Any provider change since the plan means planning again, never submitting on a stale basis.
      if(record.state!=='preparing'||record.executionEpoch!==this.state.executionEpoch||(this.state.transition?.id??null)!==record.plannedTransition||this.state.transition?.state==='switching')
        throw Object.assign(Error('dispatch-basis-changed'),{dispatchRetry:true});
      record.state='submitting';record.submissionStartedAt=this.now();
      record.submit={sessionId:plan.submit?.sessionId??this.sessionId,runtime:plan.submit?.runtime??this.runtimeId,correlation:plan.submit?.correlation===true,at:record.submissionStartedAt};
      // A new submission is reconciled on its own; what an earlier one proved is kept as history.
      if(record.reconciliation){record.priorReconciliations=[...(record.priorReconciliations??[]),record.reconciliation].slice(-4);delete record.reconciliation;}
      // A native command exists from its submission, never before it (AD1-03).
      if(record.command==='compact')this.state.operations[record.id]={inputId:record.id,kind:'compact',state:'submitted',at:this.now()};
      this.save('input-submitting',{id:input.id,submit:record.submit});
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
        if(failure.code==='input-canceled'&&record.canceledBy&&!record.submissionStartedAt)return {route:'canceled-by-owner',model:target};
        if(record.withdrawn&&!record.submissionStartedAt)throw withdrawnError(record,failure);
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
      // Refused where its prompt would have begun while it was still being handed over: what
      // the owner stopped stays canceled, and what rode with it stays not submitted (CR3-FLOW-02).
      if(record.state==='failed-before-submit'&&record.failureStage==='prompt-start'&&!record.submissionStartedAt) {
        if(record.canceledBy)return {route:'canceled-by-owner',model:target};
        throw Object.assign(Error('Input prompt never began: it rode with work the owner stopped'),
          {code:'input-not-submitted',inputId:input.id,retryAt:record.retry?.nextAt??null,retryExhausted:Boolean(record.retry?.exhausted)});
      }
      if(route==='superseded') {if(this.state.operations[record.id])this.state.operations[record.id].state='canceled';record.state='superseded';record.settledAt=this.now();this.save('input-superseded',{id:input.id});return{route,model:target};}
      // Handed only to the session's in-memory queue is not yet native acceptance:
      // the input is accepted when its prompt begins (H2a-02).
      if(outcome?.queued===true&&!record.turnStartedAt){
        record.state='queued';record.queuedAt=this.now();
        // The owner's stop came while it was being handed over: it is taken back at once (CR3-FLOW-02).
        const stopped=this.stopOf(record);
        if(stopped){this.withdrawQueuedWork([record],stopped);return {route:'canceled-by-owner',model:target};}
        this.save('input-queued',{id:input.id});return {route,model:target,queued:true};
      }
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
   * state when the session let it go first or the host is stopping — and, past
   * `timeoutMs`, with `waiting`: the caller is released, while the input stays queued
   * for the watchdog (CR-LIFE-04). */
  awaitAcceptance(inputId,{timeoutMs=null}={}) {
    const record=this.state.inputs[inputId];
    if(record?.state!=='queued')return Promise.resolve({state:record?.state??'missing'});
    let entry=this.acceptance.get(inputId);
    if(!entry){let resolve;const promise=new Promise(r=>{resolve=r;});entry={promise,resolve};this.acceptance.set(inputId,entry);}
    if(!Number.isFinite(timeoutMs))return entry.promise;
    let timer;
    const expired=new Promise(resolve=>{timer=setTimeout(()=>resolve({state:'waiting',reason:'acceptance-deadline'}),Math.max(0,timeoutMs));});
    return Promise.race([entry.promise,expired]).finally(()=>clearTimeout(timer));
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
      const taken=await this.locked(async()=>{
        const e=this.state.semanticPending[id];
        if(!e||!['pending','retry'].includes(e.state)||e.nextAttemptAt>this.now())return null;
        const record=this.state.inputs[id];
        if(!record||record.state!=='semantic-pending'||record.hash!==e.hash) {
          e.state='superseded';e.updatedAt=this.now();delete e.text;this.save('semantic-review-superseded',{id});return null;
        }
        if(e.attempts>=e.maxAttempts)return {entry:clone({...e,exhausted:true})};
        // Asking the model again passes the gate every call beside her conversation passes, in
        // the step that counts the attempt: a freeze refuses it before anything is counted, and
        // the drain counts it until the call has ended (CR3-FLOW-01).
        const gate=this.beginActivity({kind:'classification-retry',id});
        if(!gate.ok)return null;
        const before={state:e.state,attempts:e.attempts,updatedAt:e.updatedAt};
        try {e.state='classifying';e.attempts++;e.updatedAt=this.now();this.save('semantic-retry',{id,attempt:e.attempts});}
        catch(error){Object.assign(e,before);gate.release();throw error;}
        return {entry:clone(e),gate};
      });
      if(!taken)continue;
      const {entry,gate}=taken;
      let result=null,failure=null;
      if(!entry.exhausted) {
        try {result=await this.askClassification({id:entry.id,kind:entry.kind,text:entry.text,occurredAt:entry.occurredAt,receivedAt:entry.receivedAt},
          {files:entry.attachments??[],intents:this.classifyIntents&&entry.kind==='owner',wait:Math.min((this.state.config.classifierTimeoutMs??15000)*2,CLASSIFY_RETRY_MAX_MS)});}
        catch(error){failure=classificationFailure(error);}
        finally {gate.release();}
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
      if(request?.taskOutcome==='accepted')return this.acceptTask(request);
      return this.recordModeRequest(request);
    });
  }
  /** Kin takes on a task: the proposal becomes the work lock and the duty to deliver.
   * A proposal that already lapsed may still be taken on while nothing else is open. */
  acceptTask(request) {
    if(!request.commandId||!request.completedTaskId||!Number.isSafeInteger(request.completedInputVersion))throw Error('Accepting work requires the task and its input version');
    const hash=digest(request),previous=this.state.requests[request.commandId];
    if(previous){if(previous.hash!==hash)throw Error('Command id conflict');return clone(previous);}
    const task=this.state.tasks[request.completedTaskId];
    if(!task||!(open(task)||task.status==='unclaimed'))throw Error('Task is not open');
    if(request.completedInputVersion!==task.inputVersion)throw Error('Task input version changed; read current runtime');
    if(task.status==='unclaimed'&&this.openWork().some(other=>other.id!==task.id))throw Error('Another task is open; settle it first');
    task.status='running';task.acceptedAt=this.now();task.acceptance='kin';task.acceptedBy=request.commandId;delete task.lapsedAt;delete task.outcome;
    // Taking it on is what creates the duty to report to her (CR-LIFE-10).
    task.requiresDelivery=task.inputIds.some(id=>ownerInput(this.state.inputs[id]??{kind:'owner'}));
    const receipt={state:'applied',taskOutcome:'accepted',commandId:request.commandId,hash,taskId:task.id,inputVersion:task.inputVersion,
      reason:String(request.reason??'').slice(0,500),sourceInputId:request.sourceInputId??null,at:this.now()};
    this.state.requests[request.commandId]=receipt;this.save('task-accepted',{taskId:task.id,commandId:request.commandId});
    return clone(receipt);
  }
  /** Recover one already accepted owner message whose original semantic route was
   * wrong. The private host authenticates the source and asks the current DS
   * classifier; this boundary validates and records only cryptographic evidence
   * plus the bounded control decision. It never dispatches the old input again.
   * The basis is the configuration revision and the owner inputs after the source,
   * not every bookkeeping save (H1-07). */
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
      if(expectedRevision!==this.reclassificationBasis(sourceInputId))throw Error('Router revision changed; reauthenticate source evidence');
      const source=this.state.inputs[sourceInputId];
      if(!source||source.kind!=='owner'||source.state!=='accepted')throw Error('Reclassification source is not an accepted owner input');
      this.assertReclassifiable(source);
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
      receipt.modeRequestState=this.state.requests[commandId].state;receipt.updatedAt=this.now();this.save('input-reclassification-requested',{reclassificationId:id,commandId});
      const runtime=await this.reconcileTransition(await this.inspect());
      await this.applyModeRequest(this.state.requests[commandId],runtime);
      receipt.modeRequestState=this.state.requests[commandId].state;receipt.updatedAt=this.now();this.save('input-reclassification-applied',{reclassificationId:id,commandId,state:receipt.modeRequestState});
      return {receipt:clone(receipt),request:clone(this.state.requests[commandId])};
    });
  }
  /** The basis a reclassification is fenced on: configuration changes plus owner
   * messages after the source. Tool and delivery bookkeeping do not move it (H1-07). */
  reclassificationBasis(sourceInputId) {
    const inputs=Object.values(this.state.inputs),index=inputs.findIndex(input=>input.id===sourceInputId);
    return this.state.configRevision*1000+(index<0?0:inputs.slice(index+1).filter(input=>ownerInput(input)).length);
  }
  /** Only a recent owner message can still be read as a control (H1-07). */
  assertReclassifiable(source) {
    const maxAge=(this.state.config.reclassifyMaxHours??24)*3600000;
    if(!Number.isFinite(source?.at)||this.now()-source.at>maxAge)throw Error('Reclassification source is too old');
  }
  recordModeRequest(request) {
      if(!request.commandId||!['work','auto','manual'].includes(request.mode)||!request.reason?.trim()||(request.mode==='manual'&&!request.profile?.model))throw Error('Invalid mode request');
      const taskOutcome=request.taskOutcome??'completed';
      if(!TASK_OUTCOMES.includes(taskOutcome)||request.taskOutcome!==undefined&&!request.completedTaskId)throw Error('Invalid task outcome');
      if(taskOutcome!=='completed'&&(request.mode!=='auto'||!request.completedTaskId||!Number.isSafeInteger(request.completedInputVersion)||request.handoff))throw Error('A declared outcome requires the current task and input version');
      // Later is Kin's own plan: a moment she names, within bounds (N10).
      let notBefore=null;
      if(taskOutcome==='deferred') {
        notBefore=Date.parse(request.notBefore??'');
        if(!Number.isFinite(notBefore)||notBefore<=this.now()||notBefore-this.now()>DEFER_MAX_MS)throw Error('Deferral requires a future not_before within 30 days');
      }
      const hash=digest(request), previous=this.state.requests[request.commandId];
      if(previous) {if(previous.hash!==hash)throw Error('Command id conflict');return clone(previous);}
      if(request.expectedRevision!==undefined&&request.expectedRevision!==this.state.configRevision)throw Error('Router configuration revision changed; read current state');
      if(request.completedTaskId&&(!this.state.tasks[request.completedTaskId]||!open(this.state.tasks[request.completedTaskId])))throw Error('Task is not open');
      if(request.completedTaskId&&request.completedInputVersion!==this.state.tasks[request.completedTaskId].inputVersion)throw Error('Task input version changed; read current runtime');
      if(taskOutcome==='declined'){
        const task=this.state.tasks[request.completedTaskId];
        if(this.currentTask()?.id!==task.id||!task.turnStartedAt||task.turnEndedAt||task.executionEpoch!==this.state.executionEpoch)throw Error('Decline must belong to the current native task turn');
      }
      // The source is what the caller names: the host passes the input of the native turn
      // the request came from. Nothing is credited to the latest owner message (H1-04).
      const sourceInputId=request.sourceInputId??null;
      if(taskOutcome==='declined'&&!sourceInputId)throw Error('Decline requires the current source input');
      const source=sourceInputId?this.state.inputs[sourceInputId]:null;
      const reclassification=request.reclassificationId?this.state.reclassifications[request.reclassificationId]:null;
      const reclassificationAuthorized=Boolean(reclassification?.state==='recorded'&&reclassification.commandId===request.commandId&&
        reclassification.sourceInputId===sourceInputId&&reclassification.sourceHash===source?.hash&&reclassification.originalRoute===source?.route&&
        reclassification.decision?.control===request.mode&&reclassification.requestHash===hash&&request.sourceHash===source?.hash);
      // The owner's own control, as acceptControl assembles it from her message.
      const ownerControl=Boolean(source?.kind==='owner'&&source.state==='selected'&&request.commandId==='owner-mode:'+source.id&&
        request.mode===source.command&&request.sourceHash===source.hash);
      // A manual profile is the owner's: a model or maintenance caller cannot pin one (H1-04).
      if(request.mode==='manual'&&!ownerControl&&!reclassificationAuthorized)throw Error('Manual profiles come only from the owner');
      if(request.handoff) {
        // Kin handing her own work to her next turn is her commitment to it.
        const task=this.addTask({id:'handoff:'+request.commandId,text:request.handoff,kind:'handoff'});
        if(task.status==='proposed'){task.status='running';task.acceptedAt=this.now();task.acceptance='kin-handoff';
          task.requiresDelivery=task.inputIds.some(id=>ownerInput(this.state.inputs[id]??{kind:'owner'}));}
        task.handoff={id:request.commandId,text:request.handoff,state:'pending'};
      }
      if(request.mode==='work') {
        this.state.exitRequested=false;
      } else if(request.mode==='auto') {
        this.state.exitRequested=true;
        if(request.completedTaskId) {
          const task=this.state.tasks[request.completedTaskId];
          // Declaring how a proposal ended, other than declining it, is taking it on (CR-LIFE-10).
          if(task.status==='proposed'&&taskOutcome!=='declined'){task.status='running';task.acceptedAt=this.now();task.acceptance='kin-declared';
            task.requiresDelivery=task.inputIds.some(id=>ownerInput(this.state.inputs[id]??{kind:'owner'}));}
          // Kin's declaration, kept as she made it. The host closes the task on it once the
          // facts are in: the turn ended, tools finished, receipts arrived (N1).
          task.completion={inputVersion:task.inputVersion,turnFence:task.executionEpoch,at:this.now(),summary:request.reason,outcome:taskOutcome,commandId:request.commandId,
            ...(sourceInputId?{sourceInputId}:{}),...(task.turnStartedAt&&!task.turnEndedAt?{turnStartedAt:task.turnStartedAt}:{}),
            ...(notBefore?{notBefore:new Date(notBefore).toISOString()}:{})};
        }
      }
      // A classified owner control authorizes only the exact request assembled by
      // acceptControl: same owner source/hash, owner-mode command id, mode, force
      // choice and (for manual mode) catalog-resolved profile. An old `auto` source
      // can therefore never be repurposed as force authority for an arbitrary model.
      const directAuthorized=ownerControl&&['chat','work','control'].includes(source.route)&&source.controlRequestHash===hash&&['manual','auto','work'].includes(source.command);
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
      this.state.requests[request.commandId]=result;this.state.requestedMode=result.mode;this.save('mode-request',{commandId:request.commandId,mode:result.mode,force:forceAuthorized,...(request.completedTaskId?{taskOutcome}:{})});
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
  /** A force for the current epoch that is still being carried out. While none is,
   * nothing may keep that epoch's output suppressed (H1-01). */
  forceInProgress() {
    return Object.values(this.state.requests).some(request=>request.force===true&&request.forceState==='interrupting'&&!request.forceBoundary)||Boolean(this.unfencedForce());
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
      // Kin's declaration survives the interruption: it was her decision, and the turn
      // that made it is over. An owner's switch never turns a decline back into work (AD1-23).
      if(interruptedTask.completion?.outcome&&interruptedTask.completion.state!=='historical-proposal')
        interruptedTask.completion={...interruptedTask.completion,turnEndedAt:interruptedTask.completion.turnEndedAt??at,interruptedBy:boundary.id,turnFence:toEpoch};
      if(interruptedTask.turnStartedAt||interruptedTask.turnEndedAt||interruptedTask.stopReason) {
        interruptedTask.turnHistory??=[];interruptedTask.turnHistory.push({turnFence:interruptedTask.executionEpoch,turnStartedAt:interruptedTask.turnStartedAt,turnEndedAt:interruptedTask.turnEndedAt,stopReason:interruptedTask.stopReason,fencedBy:boundary.id});interruptedTask.turnHistory=interruptedTask.turnHistory.slice(-16);
      }
      interruptedTask.executionEpoch=toEpoch;interruptedTask.continuationRequired=true;interruptedTask.interruptedBy=boundary.id;
      delete interruptedTask.turnStartedAt;delete interruptedTask.turnEndedFence;delete interruptedTask.stopReason;interruptedTask.turnEndedAt=at;
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
    let target=request.mode==='manual'?request.profile:request.mode==='work'?this.profiles.work:
      this.openWork().length?this.profiles.work:this.automaticIdleProfile();
    if(request.mode==='auto'&&request.force){
      try {request.plannedAutoReturnProfile=await this.resolvedProfile(this.profiles.chat);}catch(error){
        this.failModeRequest(request,String(error.message??error),'没有切换：当前宿主不支持自动聊天配置。');this.save('mode-failed',{commandId:request.commandId,reason:'unsupported-auto-profile'});return {runtime};
      }
      target=this.openWork().length?this.profiles.work:request.plannedAutoReturnProfile;
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
    if(!unchanged&&request.deferUntilSettled&&(this.openWork().length||this.busy(runtime))){request.waitingReason='owner-requested-settlement';this.pendingModeNotice(request);return {runtime};}
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
    if(request.mode==='auto'&&this.openWork().length&&!request.force)return {state:'work-held',runtime};
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
        const returnProfile=clone(request.plannedAutoReturnProfile??this.profiles.chat);this.state.autoIdleProfile=returnProfile;
        if(this.openWork().length)this.state.autoReturnProfile=clone(returnProfile);else delete this.state.autoReturnProfile;
      } else if(!this.openWork().length){this.state.autoIdleProfile=applied;delete this.state.autoReturnProfile;}
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
        ...(silent?{state:'suppressed',reason:NOTICE_SUPPRESSED,settledAt:this.now()}:{text:runtimeReply(view,{switched:true,names:this.modelNames})})});
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
    if(this.state.mode==='auto'&&!this.openWork().length&&!this.state.autoReturnProfile&&this.verified(runtime,runtime.model))this.state.autoIdleProfile=runtimeProfile(runtime);
    this.state.actual=runtime;
  }
  async readRuntime(loaded=true) {
    return this.locked(async()=>{const runtime=await this.inspect();this.observeRuntime(runtime);
      const models=await this.availableModels(runtime);
      return {...publicMobileRuntime(this.state,runtime,this.sessionId,loaded),models,defaults:clone(this.profiles),workFacts:this.workFacts()};});
  }
  /** Display names come from the live catalog (AD1-15). */
  rememberModelNames(models) {
    const names={};for(const model of models??[])if(model?.id&&model.aliases?.[0])names[model.id]=model.aliases[0];
    if(Object.keys(names).length)this.modelNames=names;
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
      if(this.openWork().length||this.busy(runtime)||this.reservations.size)throw Error('Work prevents model verification');
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
      serviceTier:runtime.serviceTier??null,serviceTierVerified:runtime.serviceTierVerified===true&&Boolean(runtime.serviceTier),
      serviceTierPreference:runtime.serviceTierPreference??(runtime.fastMode==='on'||runtime.fastMode===true?'fast':runtime.fastMode==='off'||runtime.fastMode===false?'default':null),
      mode:this.state.mode,sessionId:this.sessionId,verifiedAt:runtime.checkedAt,transitionId:this.state.transition?.id,forceBoundaryId:request.forceBoundary?.id};
    request.waitingReason=null;
    // Kept as it was: she still hears what is in effect, which answers her control (CR-LIFE-07).
    if(unchanged){if(request.notify&&this.state.inputs[request.sourceInputId]?.route==='control')this.queueNotice(request.notificationOrigin??request.commandId,'status',{requestId:request.commandId,subscriberIds:request.notificationSubscribers??[request.commandId]});return;}
    // The host no longer injects a "continue the task" turn after an owner switch (N2):
    // the interrupted task stays open, and its facts reach Kin in her next turn.
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
      if(!request.force&&(this.busy(runtime)||record.command==='auto'&&this.openWork().length))this.queueNotice(record.id,'mode-pending',{requestId:commandId});
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
      // The owner's command is answered by its carrying out; a failure is reported to her (CR-LIFE-07).
      const record=this.state.inputs[inputId];
      if(phase!=='start'&&record&&ownerInput(record)&&!answered(record)) {
        if(operation.state==='completed'){record.answer={state:'accepted',basis:'operation-completed',at:this.now()};record.settledAt??=this.now();}
        else record.operationFailed={state:operation.state,stopReason:result.stopReason??null,at:this.now()};
      }
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
        // A freeze starts no send: the notice stays as it is, costing no attempt; a lookup
        // of one already begun (above) goes on (CR2-LIFE-02).
        if(this.frozen())return null;
        // A reconcile-able id and stage are durable BEFORE any failable preparation.
        n.stage='preparing';n.updatedAt=this.now();this.save('notice-preparing',{id});
        const runtime=await this.inspect();const view=publicMobileRuntime(this.state,runtime,this.sessionId);
        const controlWithoutActual=['mode-pending','mode-failed'].includes(n.kind)&&Boolean(n.text);
        if(!view.actual.verified&&!controlWithoutActual)return null;
        if(n.kind==='mode-pending'&&this.state.requests[n.requestId]?.state!=='pending'){n.state='superseded';n.updatedAt=this.now();this.save('notice-superseded',{id});return null;}
        if(view.actual.verified&&n.kind==='mode-applied'&&(!profileMatches(runtime,n.targetProfile??{model:n.target}))){n.state='superseded';n.updatedAt=this.now();this.save('notice-superseded',{id});return null;}
        if(view.actual.verified&&n.kind==='model-switched'&&!profileMatches(runtime,n.targetProfile??{model:n.target})) {
          const past=runtimeReply({actual:n.runtime,tasks:[],mode:n.mode},{switched:true,names:this.modelNames}).replace('已切换到 ','此前已切换到 ');
          n.text=past+' '+runtimeReply(view,{names:this.modelNames});
        }
        if(n.state==='retry'&&n.kind!=='model-switched'){delete n.text;delete n.runtime;}
        n.text??=runtimeReply(view,{pending:n.kind==='mode-pending',switched:n.kind==='mode-applied',names:this.modelNames});
        // The send passes the same activity gate as every other send to her, in the same step
        // that marks it sending, so no freeze reports idle while it starts (CR2-LIFE-02).
        const gate=this.beginActivity({kind:'notice',id});
        if(!gate.ok){n.stage='held';n.updatedAt=this.now();this.save('notice-held',{id,reason:gate.reason});return null;}
        // From the permit on, every step before the send is covered: a write that fails lets
        // the permit go, and the notice — nothing of it sent — waits under its own id to be
        // taken up again, its attempts as they were (CR3-FLOW-09).
        const before=clone(n);let handed=false;
        try {
          // Whatever else the message recalls, this is the model it names as the current one.
          if(view.actual.verified){n.runtime??=view.actual;if(n.kind!=='mode-failed')n.toldModel=view.actual.model;}n.state='sending';n.stage='sending';n.attempts=(n.attempts??0)+1;n.updatedAt=this.now();
          this.save('notice-sending',{id});
          const handing={...clone(n),sendNow:true,gate};handed=true;return handing;
        } catch(error) {
          for(const key of Object.keys(n))delete n[key];
          Object.assign(n,before,{stage:'not-submitted',waitingReason:'not-submitted-save-failed',nextAction:'resend-same-id',
            nextAttemptAt:this.now()+2000*Math.max(1,before.attempts??1),updatedAt:this.now()});
          throw error;
        } finally {if(!handed)gate.release();}
      });
      if(!notice)continue;
      let receipt;
      try {
      if(notice.sendNow) {
        try {receipt=await send({id,text:notice.text,kind:'runtime-status',sourceInputId:notice.sourceInputId,notificationSubscribers:notice.subscriberIds??[]});}
        catch {receipt=null;}
        // Any non-accepted send outcome is settled from the outbox ground truth.
        if(noticeReceiptClass(receipt)!=='accepted')receipt=await lookup(id).catch(()=>null)??receipt;
      } else receipt=await lookup(id).catch(()=>null);
      } finally {notice.gate?.release();}
      await this.locked(async()=>{
        const n=this.state.notices[id];
        const before=JSON.stringify([n.state,n.waitingReason,n.nextAction,n.attempts,n.lookups,n.messageId,n.stage]);
        const kind=noticeReceiptClass(receipt);
        if(kind==='accepted'){n.state='accepted';n.messageId=receipt.messageId;n.acceptedAt=this.now();n.replyRecorded=true;n.nextAction='none';n.stage='settled';
          // An owner control is answered by its own reply's platform receipt (CR-LIFE-07).
          for(const id of new Set([n.sourceInputId,...(n.subscriberIds??[])])){
            const control=typeof id==='string'?this.state.inputs[id]:null;
            if(control&&ownerInput(control)&&control.route==='control'&&control.state==='accepted'&&!answered(control)){control.answer={state:'accepted',basis:'control-reply',noticeId:n.id,messageId:n.messageId,at:n.acceptedAt};control.settledAt??=n.acceptedAt;}
          }
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
  // what of the reply reached the owner. Turns without a task are ordinary, not late (AD1-07).
  turnStarted(data) {
    const ids=[...new Set(Array.isArray(data.inputIds)?data.inputIds:data.sourceInputId?[data.sourceInputId]:[])],at=this.now();
    this.state.turn={startedAt:at,inputIds:ids,taskId:data.taskId??null,turnFence:data.turnFence??this.state.executionEpoch};
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
    // A proposal whose own turn has ended without Kin taking it on, declaring an outcome
    // for it or declining it lapses, and the work lock with it (N5).
    for(const task of Object.values(this.state.tasks))if(task.status==='proposed'&&!task.completion&&task.inputIds.every(id=>{const record=this.state.inputs[id];return !record||record.turnEndedAt||['superseded','failed-before-submit'].includes(record.state);})){
      task.status='unclaimed';task.lapsedAt=at;task.outcome='not-accepted';this.state.autoRestoreDue=true;
    }
    return true;
  }
  /** Progress of the active turn, in memory only: streaming text and tool calls show
   * the input is being worked on. It is never written for its own sake. */
  touchTurn() {
    const turn=this.state.turn;if(!turn)return;
    const at=this.now();for(const id of turn.inputIds)this.progress.set(id,at);
  }
  /** Streamed text or thought from the running turn: progress, in memory only (CR-LIFE-04). */
  noteProgress() {this.touchTurn();}
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
   * reply. It settles the input its reply group belongs to and the inputs the group
   * says it answered when the reply was formed (`answeredInputIds`: those merged into its
   * turn before it began, or steered in before the reply formed); nothing else. Sharing
   * the turn, or its start time, is no evidence: an input steered in after the reply
   * formed is still owed its own answer (CR-LIFE-06, CR2-LIFE-04). A group from before
   * the list existed answers only its own input. Returns the ids it settled. */
  inputAnswered(kind,data) {
    const record=typeof data.inputId==='string'?this.state.inputs[data.inputId]:null;
    if(!record)return null;
    const state=kind==='reply-complete'?'accepted':['silent','merged'].includes(data.state)?data.state:null;if(!state)return null;
    const at=this.now();record.answer={state,at,...(data.mergedInto?{mergedInto:data.mergedInto}:{})};record.settledAt??=at;
    const settled=[record.id];
    if(ownerInput(record))for(const id of new Set((Array.isArray(data.answeredInputIds)?data.answeredInputIds:[]).filter(id=>typeof id==='string'))) {
      const other=id===record.id?null:this.state.inputs[id];
      if(!other||!ownerInput(other)||other.state!=='accepted'||answered(other))continue;
      other.answer={state:state==='accepted'?'covered':state,by:record.id,basis:'answered-input-ids',at};other.settledAt??=at;settled.push(id);
    }
    return settled;
  }
  observe(kind,data={}) {
    return this.locked(async()=>{
      let dirty=false;
      if(kind==='prompt-start') {
        const refused=this.refusedPrompt(data);
        if(refused)return refused;
        dirty=this.turnStarted(data)||dirty;
      }
      if(kind==='prompt-end')dirty=this.turnEnded(data)||dirty;
      if(kind==='delivery'&&data.sourceInputId)dirty=this.inputDelivery(data)||dirty;
      if(kind==='reply-complete'||kind==='reply-choice'){const settled=this.inputAnswered(kind,data);if(settled)this.save(kind,{inputId:data.inputId,...(settled.length>1?{covered:settled.slice(1)}:{})});return settled;}
      if(kind==='input-dropped') {
        // The session let a queued prompt go before it began: provably never submitted.
        const record=this.state.inputs[data.inputId];
        if(record?.state==='queued'){this.notSubmitted(record,'session-dropped-before-prompt',{restart:true});this.save('input-failed-before-submit',{id:record.id,reason:record.reason});}
        this.settleAcceptance(data.inputId,{state:record?.state??'missing'});return;
      }
      if(kind==='tool')this.touchTurn();
      // An explicit null belongs to a no-task/chat turn. It must not be rebound to
      // whichever task happens to be current when a delayed callback arrives.
      const noTask=Object.hasOwn(data,'taskId')&&!data.taskId;
      const task=Object.hasOwn(data,'taskId')?(data.taskId?this.state.tasks[data.taskId]:null):this.currentTask();
      const turnFence=data.turnFence??data.executionEpoch;
      const taskEvent=['prompt-start','prompt-end','tool','delivery'].includes(kind);
      // A chat or an internal turn is an ordinary turn: never a late event, and saved
      // only when the ledger learned something from it (AD1-07).
      if(noTask&&taskEvent){if(dirty)this.save(kind);return;}
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
          trim(task,'deliveryHistory');
        }
        if(task&&kind==='tool') {
          task.toolHistory??={};
          const key='tool-'+digest([data.id,turnFence??null,data.inputVersion??null,reason]).slice(0,40);
          task.toolHistory[key]={id:data.id,status:data.status??'pending',inputVersion:data.inputVersion,turnFence:turnFence??null,
            lateAfterForce:true,authority:authoritative?'historical-fence':'evidence-only',reason,...(data.reason?{detailReason:data.reason}:{}),at};
          trim(task,'toolHistory');
        }
        this.save('late-'+kind,{taskId:task?.id,reason,turnFence:turnFence??null,currentEpoch:this.state.executionEpoch});
      };
      let historicalReason=null;
      if(taskEvent&&task) {
        if((data.turnFence!==undefined||data.executionEpoch!==undefined)&&!Number.isInteger(turnFence))historicalReason='invalid-turn-fence';
        else if(data.inputVersion!==undefined&&data.inputVersion!==null&&!Number.isSafeInteger(data.inputVersion))historicalReason='invalid-input-version';
        else if(this.state.executionEpoch>0&&(turnFence===undefined||data.inputVersion===undefined))historicalReason='unversioned-after-force';
        else if(Number.isInteger(turnFence)&&turnFence<this.state.executionEpoch)historicalReason='older-fence';
        else if(Number.isInteger(turnFence)&&turnFence>this.state.executionEpoch)historicalReason='future-fence';
        else if(data.inputVersion!==undefined&&data.inputVersion!==null&&data.inputVersion!==task.inputVersion)historicalReason='input-version-mismatch';
        else if(kind!=='prompt-start'&&Number.isInteger(turnFence)&&turnFence!==task.executionEpoch)historicalReason='task-fence-mismatch';
      }
      if(historicalReason) {
        storeHistorical(historicalReason,{authoritative:historicalReason==='older-fence'&&Number.isInteger(turnFence)&&Number.isSafeInteger(data.inputVersion)});
        return;
      }
      if(kind==='reply'&&data.final) {this.state.recent.push({role:'assistant',text:data.text.slice(0,4000),at:data.at??this.now()});this.state.recent=this.state.recent.slice(-16);}
      if(task&&open(task)) {
        if(kind==='prompt-start') {
          if(task.completion?.outcome==='declined'&&this.declarationReady(task,await this.inspect()))this.closeTask(task);
          if(open(task)){if(task.status!=='proposed')task.status='running';task.turnStartedAt=this.now();task.executionEpoch=turnFence??this.state.executionEpoch;task.continuationRequired=false;if(task.completion?.state==='historical-proposal')delete task.completion;delete task.turnEndedAt;delete task.turnEndedFence;}
        }
        if(kind==='prompt-end') {
          task.turnEndedAt=this.now();task.turnEndedFence=turnFence??task.executionEpoch;task.stopReason=data.stopReason;
          // A declaration made in this turn now has its end. A turn that stopped for any
          // reason has ended: the stop reason is a fact, never a lock that holds (AD1-11).
          const proposal=task.completion;
          if(proposal?.outcome&&!proposal.turnEndedAt&&(proposal.turnStartedAt===undefined||proposal.turnStartedAt===task.turnStartedAt)&&proposal.inputVersion===task.inputVersion&&proposal.turnFence===task.turnEndedFence){
            proposal.turnEndedAt=task.turnEndedAt;proposal.turnEndedFence=task.turnEndedFence;proposal.stopReason=data.stopReason;
          }
        }
        if(kind==='tool') {
          const inputVersion=data.inputVersion??task.inputVersion,fence=turnFence??task.executionEpoch,previous=task.tools[data.id];
          if(previous&&(previous.inputVersion!==inputVersion||previous.turnFence!==fence)) {
            task.toolHistory??={};const key='tool-'+digest([data.id,previous.turnFence??null,previous.inputVersion??null,'replaced-current-entry']).slice(0,40);
            task.toolHistory[key]={id:data.id,...clone(previous),authority:'historical-fence',reason:'replaced-current-entry'};trim(task,'toolHistory');
          }
          task.tools[data.id]={status:data.status??previous?.status??'pending',inputVersion,turnFence:fence,...(data.reason?{reason:data.reason}: {})};
        }
        if(kind==='delivery') {
          const inputVersion=data.inputVersion??task.inputVersion,fence=turnFence??task.executionEpoch,previous=task.deliveries[data.id];
          if(previous&&(previous.inputVersion!==inputVersion||previous.turnFence!==fence)) {
            task.deliveryHistory??={};const key='delivery-'+digest([data.id,previous.turnFence??null,previous.inputVersion??null,'replaced-current-entry']).slice(0,40);
            task.deliveryHistory[key]={id:data.id,...clone(previous),authority:'historical-fence',reason:'replaced-current-entry'};trim(task,'deliveryHistory');
          }
          task.deliveries[data.id]={state:data.state,messageId:data.messageId,outboxId:data.outboxId,stage:data.stage,submissionStarted:data.submissionStarted,
            sourceInputId:data.sourceInputId,inputVersion,turnFence:fence,at:this.now()};
        }
        dirty=true;
      }
      if(dirty||kind==='reply'&&data.final)this.save(kind);
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
  /** The host's facts about Kin's declared outcome: the declaring turn ended (for any
   * stop reason, or by an owner interruption), the task's tools are terminal, its
   * deliveries are settled, and the report to the owner reached the platform. Time
   * passing proves nothing about a delivery: past the report wait an unproven report is
   * looked up by its original id and its facts go back to Kin (CR-LIFE-11, CR-MIND-02).
   * The outcome itself is Kin's; nothing here judges it (N1). */
  declarationReady(task,runtime) {
    const facts=this.declarationFacts(task,runtime);
    return Boolean(facts&&facts.settled&&(task.requiresDelivery===false||facts.reported));
  }
  /** Declared, the turn and its tools done, and past the report wait still without a
   * proven report: what the host looks up again and tells Kin, never closes on. */
  declarationStalled(task,runtime) {
    const facts=this.declarationFacts(task,runtime);
    return Boolean(facts&&task.requiresDelivery!==false&&!(facts.settled&&facts.reported)&&
      this.now()-facts.endedAt>=(this.state.config.reportWaitMinutes??30)*60000);
  }
  declarationFacts(task,runtime) {
    const proposal=task.completion,fence=proposal?.turnFence,version=proposal?.inputVersion;
    if(!proposal?.outcome||proposal.state==='historical-proposal'||version!==task.inputVersion||(runtime&&runtime.pendingDeliveries!==0))return null;
    if(fence!==task.executionEpoch&&!(proposal.interruptedBy&&fence===task.executionEpoch))return null;
    const ended=proposal.interruptedBy||proposal.turnStartedAt===undefined&&(task.turnEndedAt??0)>=proposal.at||
      proposal.turnStartedAt!==undefined&&proposal.turnStartedAt<=proposal.at&&proposal.turnEndedAt>=proposal.at;
    if(!ended)return null;
    const tools=[...Object.values(task.tools??{}),...Object.values(task.toolHistory??{}).filter(t=>t.authority==='historical-fence'&&t.turnFence===fence&&t.inputVersion===version)];
    if(!tools.every(tool=>['completed','failed'].includes(tool.status)||proposal.interruptedBy&&terminalTool(tool)))return null;
    const deliveries=[...Object.values(task.deliveries??{}),...Object.values(task.deliveryHistory??{}).filter(d=>d.authority==='historical-fence'&&d.turnFence===fence&&d.inputVersion===version)];
    const reported=deliveries.some(d=>(!proposal.sourceInputId||d.sourceInputId===proposal.sourceInputId)&&d.inputVersion===version&&(d.turnFence===fence||proposal.interruptedBy)&&d.state==='accepted'&&d.messageId&&d.at>=proposal.at);
    return {endedAt:proposal.turnEndedAt??task.turnEndedAt??proposal.at,settled:deliveries.every(settledDelivery),reported,
      unknown:deliveries.filter(d=>['unconfirmed','unknown'].includes(d.state)).map(d=>d.id)};
  }
  /** Look up, by their original ids, the unproven deliveries of a declaration past its
   * report wait. `lookup(id)` reads a transport receipt; nothing is sent. */
  async reconcileDeliveries(lookup) {
    const due=await this.locked(async()=>{
      if(!this.openWork().some(task=>task.completion?.outcome))return [];
      const runtime=await this.inspect().catch(()=>null);
      return this.openWork().filter(task=>this.declarationStalled(task,runtime)).flatMap(task=>Object.entries(task.deliveries??{})
        .filter(([,d])=>['unconfirmed','unknown'].includes(d.state)).map(([id,d])=>({taskId:task.id,id,key:d.outboxId??id})));
    });
    let changed=0;
    for(const item of due) {
      let receipt=null;try{receipt=await lookup(item.key);}catch{receipt=null;}
      if(receipt?.state!=='accepted'||!receipt.messageId)continue;
      await this.locked(async()=>{
        const delivery=this.state.tasks[item.taskId]?.deliveries?.[item.id];
        if(!delivery||!['unconfirmed','unknown'].includes(delivery.state))return;
        Object.assign(delivery,{state:'accepted',messageId:receipt.messageId,reconciledAt:this.now()});changed++;
        this.save('delivery-reconciled',{taskId:item.taskId,id:item.id});
      });
    }
    return {looked:due.length,accepted:changed};
  }
  /** Close a task on its declared outcome, keeping the facts it closed on. */
  closeTask(task,{event='task-closed'}={}) {
    const outcome=task.completion?.outcome??'completed',at=this.now();
    const deliveries=Object.entries(task.deliveries??{});
    task.closure={outcome,at,reported:deliveries.some(([,d])=>d.state==='accepted'&&d.messageId&&d.at>=(task.completion?.at??0)),
      undelivered:deliveries.filter(([,d])=>['rejected','undeliverable'].includes(d.state)).map(([id])=>id),
      unsent:deliveries.filter(([,d])=>['deferred','not-submitted'].includes(d.state)).map(([id])=>id),
      unknown:deliveries.filter(([,d])=>['unconfirmed','unknown'].includes(d.state)).map(([id])=>id),stopReason:task.completion?.stopReason??task.stopReason??null};
    task.outcome=outcome;
    if(outcome==='declined'){task.status='canceled';task.canceledAt=at;}
    else if(outcome==='partial'){task.status='partial';task.completedAt=at;}
    else if(outcome==='deferred'){task.status='deferred';task.deferral={notBefore:task.completion.notBefore,reason:task.completion.summary,plan:{state:'pending'},at};}
    else {task.status='completed';task.completedAt=at;}
    this.save(event,{taskId:task.id,outcome});
  }
  /** Deferred tasks whose plan is still to be written and is due to be asked for, for
   * the host's plan port (CR-MIND-03). */
  deferredPlans() {const now=this.now();return Object.values(this.state.tasks).filter(t=>t.status==='deferred'&&t.deferral?.plan?.state==='pending'&&!(t.deferral.plan.nextAt>now)).map(t=>clone(t));}
  /** The plan port's answer. `created` settles it; `retry` (the port was unavailable, or
   * the source is not stored yet) keeps it pending with a backoff, never turning a passing
   * failure into a permanent one; `needs-kin` hands it back to Kin, once (CR-MIND-03). */
  recordDeferralPlan(taskId,result) {
    return this.locked(async()=>{
      const task=this.state.tasks[taskId];if(!task?.deferral)return null;
      const previous=task.deferral.plan??{},at=this.now(),reason=result?.reason?String(result.reason).slice(0,160):null;
      if(result?.state==='created')task.deferral.plan={state:'created',...(result.planId?{planId:result.planId}:{}),attempts:(previous.attempts??0)+1,at};
      else if(result?.state==='retry') {
        const attempts=(previous.attempts??0)+1,delay=DEFERRAL_PLAN_RETRY_MS[attempts-1];
        task.deferral.plan=delay===undefined?{state:'needs-kin',reason:'plan-retries-exhausted'+(reason?':'+reason:''),attempts,at}:{state:'pending',attempts,nextAt:at+delay,...(reason?{lastReason:reason}:{}),at};
      }
      else task.deferral.plan={state:'needs-kin',...(reason?{reason}:{}),attempts:(previous.attempts??0)+1,at};
      this.save('deferral-plan',{taskId,state:task.deferral.plan.state});return clone(task.deferral);
    });
  }
  /** Deferrals left to Kin that she has not been told about yet. */
  deferralsToTell() {return Object.values(this.state.tasks).filter(t=>t.status==='deferred'&&t.deferral?.plan?.state==='needs-kin'&&!t.deferral.plan.toldAt).map(t=>clone(t));}
  markDeferralTold(taskId,delivery) {
    return this.locked(async()=>{
      const plan=this.state.tasks[taskId]?.deferral?.plan;if(!plan||plan.state!=='needs-kin'||plan.toldAt)return false;
      plan.toldAt=this.now();plan.told=String(delivery?.state??'accepted').slice(0,40);this.save('deferral-told',{taskId});return true;
    });
  }
  /** Record the idle summary of a task Kin has not declared. It never moves the lock. */
  recordWorkSummary(taskId,fingerprint,summary) {
    const task=this.state.tasks[taskId];if(!task||!open(task))return false;
    task.workSummary={...summary,fingerprint,at:this.now()};this.save('work-summary',{taskId});return true;
  }
  async reconcile() {
    return this.locked(async()=>{
      const runtime=await this.reconcileTransition(await this.inspect());this.observeRuntime(runtime);
      if(this.busy(runtime))return {state:'busy'};
      let changed=this.archiveInterruptedTools();
      // A stop whose interruption was not confirmed is settled once the session is seen idle.
      for(const record of Object.values(this.state.inputs))if(['interrupting','unconfirmed','failed'].includes(record.interrupt?.state)){
        record.interrupt.state='reconciled';record.interrupt.reconciledAt=this.now();this.settleOwnerStop(record);changed=true;
      }
      if(this.lapseProposals())changed=true;
      for(const task of this.tasks()) {
        if(task.cancelRequested){this.cancelTask(task);changed=true;continue;}
        // Kin's declared outcome closes the task once the host's facts are in (N1).
        if(task.completion?.outcome&&task.completion.state!=='historical-proposal') {
          if(this.declarationReady(task,runtime)){this.closeTask(task);changed=true;}
          continue;
        }
        const fence=task.completion?.turnFence;
        const deliveryMap=new Map(Object.entries(task.deliveries).filter(([,delivery])=>(delivery.inputVersion===undefined||delivery.inputVersion===task.completion?.inputVersion)&&(fence===undefined||delivery.turnFence===undefined||delivery.turnFence===fence)));
        if(fence!==undefined)for(const delivery of Object.values(task.deliveryHistory??{}))if(delivery.authority==='historical-fence'&&delivery.turnFence===fence&&delivery.inputVersion===task.completion?.inputVersion)deliveryMap.set(delivery.id,delivery);
        const deliveries=[...deliveryMap.values()];
        const toolMap=new Map(Object.entries(task.tools).filter(([,tool])=>(fence===undefined||tool.turnFence===undefined||tool.turnFence===fence)));
        if(fence!==undefined)for(const tool of Object.values(task.toolHistory??{}))if(tool.authority==='historical-fence'&&tool.turnFence===fence&&tool.inputVersion===task.completion?.inputVersion)toolMap.set(tool.id,tool);
        const tools=[...toolMap.values()];
        // A completion without a declared outcome is the legacy and internal form.
        if(task.completion&&task.completion?.state!=='historical-proposal'&&task.completion?.inputVersion===task.inputVersion && task.stopReason==='end_turn' && task.turnEndedAt>=task.completion.at &&
          (fence===undefined||task.turnEndedFence===undefined||task.turnEndedFence===fence) && tools.every(tool=>['completed','failed'].includes(tool.status)) &&
          (task.requiresDelivery===false?task.internalReceipt?.verified:
            deliveries.length && deliveries.every(d=>d.state==='accepted'&&d.messageId) && deliveries.some(d=>d.at>=task.completion.at))) {
          task.status='completed';task.completedAt=this.now();task.outcome='completed';changed=true;
        }
      }
      // Work that lapsed or closed outside this pass still owes the automatic return.
      if((changed||this.state.autoRestoreDue)&&!this.frozen()) {
        if(!this.openWork().length&&this.state.mode!=='manual'&&this.state.autoReturnProfile&&!Object.values(this.state.requests).some(r=>r.state==='pending'&&['work','auto','manual'].includes(r.mode))) {
          const commandId='automatic-restore:'+digest([this.state.revision,this.state.autoReturnProfile]).slice(0,24);
          this.recordModeRequest({commandId,mode:'auto',reason:'All automatic work tasks are settled',notify:true});
        }
        if(this.state.autoRestoreDue){delete this.state.autoRestoreDue;changed=true;}
      }
      if(!this.frozen())for(const request of Object.values(this.state.requests))if(request.state==='pending') {
        if(request.deferUntilSettled&&this.openWork().length)continue;
        const target=request.mode==='manual'?request.profile:request.mode==='work'?this.profiles.work:!this.openWork().length?this.automaticIdleProfile():null;
        if(target&&this.verified(runtime,target)) {await this.applyModeRequest(request,runtime);changed=true;}
      }
      if(this.prune())changed=true;
      if(changed)this.save('reconciled');return {state:this.openWork().length?'work-held':'idle'};
    });
  }
  // ---- CR-LIFE-02 (WS4): an owner input is on the ledger from the moment the host has
  // verified its source, before anything that can fail prepares it (attachments, previews,
  // the session). A failure there proves it never reached the native session: the watchdog
  // retries it under its own id and, past the retries, tells the owner as the system.
  /** Record a verified input about to be prepared, under its original id, as `preparing`
   * with nothing routed yet (`intake`). A record already routed is left as it is; an
   * earlier preparation failure of this input becomes this attempt. Returns the record and
   * `since`, when this attempt began. */
  received({id,kind='owner',channel=null,receivedAt=null}) {
    return this.locked(async()=>{
      const now=this.now(),record=this.state.inputs[id];
      if(!record&&this.archivedIds.has(id))return {record:{id,state:'accepted',archived:true},since:now};
      if(record&&intakeOnly(record)&&record.state==='failed-before-submit') {
        Object.assign(record,{state:'preparing',at:now});delete record.reason;delete record.failureStage;delete record.ownerNotice;
        // A restored input nobody could say the kind of is the kind the host takes it in as.
        if(record.kind==='unknown')record.kind=kind;
        this.save('input-intake-retry',{id,attempt:record.retry?.attempts??0});
      }
      if(record)return {record:clone(record),since:now};
      const created={id,kind,at:now,firstReceivedAt:receiptTime(receivedAt,now),intake:{at:now},...(INPUT_CHANNELS.has(channel)?{channel}:{}),
        state:'preparing',conversationId:this.state.conversationId,generation:this.state.generation,nativeThreadId:this.sessionId};
      this.state.inputs[id]=created;this.save('input-received',{id});
      return {record:clone(created),since:now};
    });
  }
  /** The host could not prepare this input: nothing of it was submitted. A failure the
   * router's own dispatch already recorded for this attempt (`since`) is not counted twice. */
  intakeFailed(id,reason,{since=0}={}) {
    return this.locked(async()=>{
      const record=this.state.inputs[id];
      if(!record||this.inflight.has(id)||record.submissionStartedAt||record.canceledBy||!['selected','preparing','failed-before-submit'].includes(record.state))return record?clone(record):null;
      if(record.state==='failed-before-submit'&&(record.retry?.lastFailureAt??-Infinity)>=since)return clone(record);
      const retry=this.notSubmitted(record,reason);
      this.save('input-failed-before-submit',{id,reason,attempt:retry.attempts});
      return clone(record);
    });
  }
  /** The watchdog. Every owner input gets an outcome: a retry only on evidence that
   * the same id never arrived (not submitted, refused by the platform, reconciled as
   * not received), a reconciliation of an uncertain submission by its original id,
   * or a system notice to the owner. An input is `failed-notified` only once that
   * notice itself has a platform receipt. The ports run outside the mutex:
   * `requeue(id)` puts the durable inbox job back, `reconcileInput(id)` answers
   * found / not-found / unknown, and `notifyOwner(kind,id,{mayStart})` sends — or, for
   * an id it already tried, looks up — the one notice for that input.
   *
   * Each input is judged by its own last progress (CR-LIFE-04): a busy or hung session
   * holds back only a resubmission nothing proves safe, never one notice or a lookup by
   * the original id. A notice that may start a send passes the freeze gate and the
   * notice gap at the moment it starts, and only a send that started uses the gap
   * (CR-LIFE-08, CR-LIFE-16); a lookup needs neither. A notice whose round used its
   * attempts rests and is taken up again (CR-LIFE-05). */
  async watch({notifyOwner=null,reconcileInput=null,requeue=null,sessionBusy=false}={}) {
    if(this.watching)return [];
    this.watching=true;
    try {return await this.watchOnce({notifyOwner,reconcileInput,requeue,sessionBusy});}
    finally {this.watching=false;}
  }
  async watchOnce({notifyOwner,reconcileInput,requeue,sessionBusy}) {
    const now=this.now(),stuckMs=(this.state.config.inputStuckMinutes??10)*60000,gapMs=(this.state.config.noticeGapMinutes??10)*60000;
    const actions=await this.locked(async()=>{
      this.expireUnsubmittedInputs();
      const list=[],due=[];let changed=false;
      // At most one notice starts a send in a pass, and only past the gap since the last one that did.
      let starting=this.frozen()||now-(this.state.lastOwnerNoticeAt??-Infinity)<gapMs;
      // Counted when it is handed out, so a quick retake cannot lose the count (CR2-LIFE-05).
      const handOutRequeue=record=>{record.requeues=requeuesOf(record)+1;record.retry.requeuedAt=now;record.retry.nextAt=now+stuckMs;changed=true;list.push({kind:'requeue',id:record.id});};
      for(const record of Object.values(this.state.inputs)) {
        if(record.historical)continue;
        // A live dispatch only keeps the same id from being submitted twice: nothing requeues
        // or looks it up meanwhile, but its stall is judged like any other (CR2-LIFE-03).
        const inflight=this.inflight.has(record.id);
        // An internal input whose submission is unknown (a handoff's continuation, the mind's
        // turns) is looked up by its id like the owner's, and nobody is told (WS8 #2).
        if(!ownerInput(record)) {
          if(inflight)continue;
          const reconciliation=record.reconciliation,retry=record.retry;
          if(['unconfirmed','fenced-unconfirmed'].includes(record.state)&&reconcileInput&&(!reconciliation||reconciliation.state==='unknown'&&now-(reconciliation.at??0)>=stuckMs))list.push({kind:'reconcile',id:record.id});
          // A restored input of unknown kind proven never received goes back to the inbox it
          // may have come from, under its own id; with no job there, or its budget spent, it
          // stops, and nobody is told.
          else if(record.kind==='unknown'&&record.state==='failed-before-submit'&&!record.canceledBy&&retry&&!retry.exhausted) {
            if(requeue&&requeuesOf(record)<REQUEUE_BUDGET){if(retry.nextAt<=now&&!this.frozen())handOutRequeue(record);}
            else {retry.exhausted=true;retry.requeue??='budget';changed=true;}
          }
          continue;
        }
        const summary=inputSummary(record),notice=record.ownerNotice;
        if(['answered','superseded','canceled-by-owner','failed-notified'].includes(summary))continue;
        // A failed owner control is reported by its own mode notice.
        if(record.failureStage==='model-control'&&Object.values(this.state.notices).some(n=>n.sourceInputId===record.id&&!['failed','rejected','unresolved','superseded'].includes(n.state)))continue;
        // A notice already owed is sent or looked up; the input's own evidence still counts.
        if(notice&&notice.state!=='sending'&&!(notice.nextAt>now)) {
          const mayStart=noticeMayStart(notice);
          if(!mayStart||!starting){if(mayStart)starting=true;notice.state='sending';list.push({kind:'notify',id:record.id,notice:notice.kind,mayStart,previous:notice.lastReceipt??null});changed=true;}
        }
        const last=Math.max(this.progress.get(record.id)??0,record.lastDeliveredAt??0,record.turnEndedAt??0,record.turnStartedAt??0,record.acceptedAt??0,
          record.queuedAt??0,record.retry?.requeuedAt??0,record.retry?.lastFailureAt??0,record.planAt??0,this.state.semanticPending[record.id]?.updatedAt??0,record.at??0);
        const idle=now-last>=stuckMs;
        let verdict=null;
        if(['semantic-pending','selected','preparing'].includes(record.state)) {
          // Still waiting before submission — for a classification, the coordinator, a switch
          // or its own preparation. Past the stall limit she is told it has not gone through.
          if(idle)verdict='stopped';
        } else if(record.state==='submitting') {
          if(idle)verdict='unknown';
        } else if(record.state==='failed-before-submit') {
          const retry=record.retry;
          // Back to the inbox only on evidence, and only so many times across every attempt
          // (CR-LIFE-03, CR2-LIFE-05); a dispatch that has not let go is not requeued.
          if(inflight){if(idle)verdict='stopped';}
          else if(retry&&!retry.exhausted&&requeue&&requeuesOf(record)<REQUEUE_BUDGET){if(retry.nextAt<=now&&!this.frozen())handOutRequeue(record);}
          else {if(retry&&!retry.exhausted){retry.exhausted=true;retry.requeue??='budget';changed=true;}verdict='stopped';}
        } else if(record.state==='queued') {
          // Only the host's in-memory queue held it. With the session idle and its prompt
          // never begun it provably never arrived; behind a busy or hung turn it may still
          // begin, so it is not resubmitted, but the owner is told.
          if(idle&&!sessionBusy&&!inflight){this.notSubmitted(record,'queued-prompt-never-started',{restart:true});this.settleAcceptance(record.id,{state:record.state});changed=true;}
          else if(idle)verdict='unknown';
        } else if(['unconfirmed','fenced-unconfirmed'].includes(record.state)) {
          const reconciliation=record.reconciliation;
          if(!inflight&&reconcileInput&&(!reconciliation||reconciliation.state==='unknown'&&now-(reconciliation.at??0)>=stuckMs))list.push({kind:'reconcile',id:record.id});
          if(reconciliation)verdict='unknown';
        } else if(record.state==='accepted') {
          if(record.operationFailed)verdict='stopped';
          else if(record.route==='control') {
            // A control is answered by its own reply; while that reply is still being made or
            // sent nothing is owed, and once it failed or went unconfirmed the owner is told.
            const replies=Object.values(this.state.notices).filter(n=>n.sourceInputId===record.id||n.subscriberIds?.includes(record.id));
            if(idle&&!replies.some(n=>['pending','preparing','sending','retry'].includes(n.state)))verdict=replies.some(n=>n.state==='unconfirmed')||!replies.length?'unknown':'stopped';
          }
          else if(idle)verdict=(record.delivered??0)>0?'partial':'unknown';
        }
        if(verdict&&!notice)due.push({record,verdict});
      }
      // One new notice at a time: the oldest input first.
      if(due.length&&!starting) {
        const {record,verdict}=due.sort((a,b)=>(a.record.firstReceivedAt??a.record.at??0)-(b.record.firstReceivedAt??b.record.at??0))[0];
        // The notice's transport identity is the one the sender derives: the input and the kind.
        record.ownerNotice={kind:verdict,id:'kin-input-notice-'+digest([record.id,verdict]).slice(0,32),state:'sending',attempts:0,round:0,at:now};
        list.push({kind:'notify',id:record.id,notice:verdict,mayStart:true,previous:null});changed=true;
      }
      if(this.lapseProposals(now))changed=true;
      if(changed)this.save('input-watch',{notices:list.filter(action=>action.kind==='notify').map(action=>action.id)});
      return list;
    });
    const results=[];
    for(const action of actions) {
      if(action.kind==='requeue') {
        let result;try{result=await requeue(action.id);}catch{result={state:'unavailable'};}
        await this.locked(async()=>{
          const record=this.state.inputs[action.id];if(!record)return;
          if(['requeued','pending','processing'].includes(result?.state)){this.save('input-requeued',{id:action.id,requeues:requeuesOf(record)});return;}
          // Nowhere to go back to: this attempt stops (the count was taken when it was handed out).
          if(record.state!=='failed-before-submit')return;
          record.retry={...record.retry,exhausted:true,requeue:result?.state??'missing'};this.save('input-retry-exhausted',{id:action.id,reason:record.retry.requeue});
        });
        results.push({...action,result:result?.state??null});continue;
      }
      if(action.kind==='reconcile') {
        // Looked up on the thread it was submitted to, never on whatever is current now (CR2-INT-02).
        const submit=this.state.inputs[action.id]?.submit??null;
        let result;try{result=await reconcileInput(action.id,{sessionId:submit?.sessionId??null});}catch{result={state:'unknown'};}
        await this.locked(async()=>{
          const record=this.state.inputs[action.id];if(!record||!['unconfirmed','fenced-unconfirmed'].includes(record.state))return;
          const read=reconciliationState(result,record.submit);
          const state=read.state;
          record.reconciliation={state,at:this.now(),...(read.reason?{reason:read.reason}:{})};
          // A record restored from the journal is settled by its lookup like any other (WS8 #3).
          if(state!=='unknown'&&record.recovered){delete record.recovered;record.restored='journal';}
          if(state==='found'){record.state='accepted';record.acceptedAt??=this.now();}
          else if(state==='not-found') {
            // The submission it looked for is kept as history; proven not received, it no
            // longer stands in the way of trying the same id again (CR-LIFE-03).
            const submitted=record.submissionStartedAt??null;
            this.notSubmitted(record,'reconciled-not-received',{restart:true,stage:'reconciliation'});record.retry.evidence='reconciled-not-received';
            if(submitted){record.reconciliation.submissionStartedAt=submitted;delete record.submissionStartedAt;}
          }
          this.save('input-reconciled',{id:action.id,state});
        });
        results.push({...action,result:result?.state??null});continue;
      }
      // A notice that may start a send passes the gate when it starts; a lookup needs no gate.
      const record=this.state.inputs[action.id];
      let gate=null;
      if(action.mayStart) {
        gate=this.beginActivity({kind:'notice',id:record?.ownerNotice?.id??action.id,channel:record?.channel??null});
        if(!gate.ok) {
          await this.locked(async()=>{const notice=this.state.inputs[action.id]?.ownerNotice;if(notice?.state==='sending'){notice.state=action.previous??'not-submitted';notice.nextAt=this.now()+30000;this.save('input-notice-held',{id:action.id,reason:gate.reason});}});
          results.push({...action,result:'held-'+gate.reason});continue;
        }
      }
      const startedAt=this.now();let receipt=null;
      try {if(notifyOwner)receipt=await notifyOwner(action.notice,action.id,{mayStart:Boolean(action.mayStart)});}
      catch {receipt=null;}
      finally {gate?.release?.();}
      const kind=notifyOwner?noticeReceiptClass(receipt):'no-port';
      await this.locked(async()=>{
        const record=this.state.inputs[action.id],notice=record?.ownerNotice;if(!notice)return;
        notice.attempts=(notice.attempts??0)+1;notice.updatedAt=this.now();notice.lastReceipt=kind;
        // Only a send that started uses the gap, from when it started (CR-LIFE-16).
        if(action.mayStart&&['accepted','rejected','unknown'].includes(kind))this.state.lastOwnerNoticeAt=startedAt;
        if(kind==='accepted'){notice.state='accepted';notice.messageId=receipt.messageId;notice.acceptedAt=this.now();record.settledAt=this.now();}
        else if(kind==='rejected'&&action.mayStart) {
          // Refused outright, so nothing reached her: the same notice goes again after a
          // bounded rest; once the rests are spent it is only looked up (CR2-LIFE-06).
          notice.rejections=(notice.rejections??0)+1;notice.state='rejected';notice.attempts=0;
          const rest=NOTICE_REJECT_RETRY_MS[notice.rejections-1];
          notice.nextAt=this.now()+(rest??NOTICE_ROUND_REST_MS[Math.min((notice.round=(notice.round??0)+1),NOTICE_ROUND_REST_MS.length)-1]);
        }
        else if(notice.attempts>=(kind==='not-submitted'||kind==='no-port'?NOTICE_SEND_BUDGET:NOTICE_LOOKUP_BUDGET)) {
          // The round ends; the notice does not. It rests, then is tried again (proven
          // unsent) or looked up again (begun) under the same identity (CR-LIFE-05).
          notice.round=(notice.round??0)+1;notice.attempts=0;notice.state=kind;
          notice.nextAt=this.now()+NOTICE_ROUND_REST_MS[Math.min(notice.round,NOTICE_ROUND_REST_MS.length)-1];
        }
        else {notice.state=kind;notice.nextAt=this.now()+(kind==='not-submitted'?30000:120000);}
        this.save('input-notice',{id:action.id,notice:notice.kind,state:notice.state,...(notice.round?{round:notice.round}:{})});
      });
      results.push({...action,result:kind});
    }
    return results;
  }
  /** A resting notice is taken up again when something new comes from the owner: her
   * channel may be reachable again (CR-LIFE-05). */
  wakeNotices(at=this.now()) {
    for(const record of Object.values(this.state.inputs))if(record.ownerNotice&&record.ownerNotice.state!=='accepted'&&record.ownerNotice.state!=='sending'&&record.ownerNotice.nextAt>at)record.ownerNotice.nextAt=at;
  }
  /** A proposal whose inputs are all done — their turn ended, or they ended without one —
   * lapses without Kin having taken it on; it never held the lock (CR-LIFE-10, N5). */
  lapseProposals(at=this.now()) {
    let changed=false;
    for(const task of Object.values(this.state.tasks)) {
      if(task.status!=='proposed'||task.completion)continue;
      const done=task.inputIds.every(id=>{
        const record=this.state.inputs[id];
        return !record||record.turnEndedAt||record.canceledBy||['superseded'].includes(record.state)||
          record.state==='failed-before-submit'&&(record.retry?.exhausted||record.ownerNotice?.state==='accepted');
      });
      if(done){task.status='unclaimed';task.lapsedAt=at;task.outcome='not-accepted';this.state.autoRestoreDue=true;changed=true;}
    }
    return changed;
  }
}
/** A record that names an input and nothing more, never routed: made by the host on intake
 * (CR-LIFE-02), or restored from the journal and proven never received (WS8 #3). A replay
 * routes it afresh and keeps the retries it already used. One the owner's stop settled is
 * never that: it stays hers. */
function intakeOnly(record){return Boolean((record?.intake||record?.restored)&&!record.route&&!record.canceledBy&&['preparing','failed-before-submit'].includes(record.state));}
/** An input the journal says the owner's stop settled, restored as that: canceled by the same
 * stop under its own id. What never reached the native session comes back known unsubmitted;
 * what had, or may have, keeps that (accepted, or its submission unknown) — and is never
 * looked up again, since nothing of it is to be sent or told any more. */
function restoredCancel(id,kind,{canceledBy,scope,state},at,submit) {
  const base={id,kind,canceledBy,cancelScope:scope,settledAt:at,restored:'journal',at};
  if(scope==='native-session')return {...base,state:state==='accepted'?'accepted':'unconfirmed',...(submit?{submit}:{})};
  return {...base,state:'failed-before-submit',reason:'canceled-by-owner',failureStage:scope,withdrawn:{reason:'canceled-by-owner',at,stage:scope}};
}
/** A deferred task the router still owes its plan to, or owes Kin its failure. */
function deferralOwed(task){const plan=task.deferral?.plan;return task.status==='deferred'&&(plan?.state==='pending'||plan?.state==='needs-kin'&&!plan.toldAt);}
/** A task keeps a bounded history of replaced and late entries (AD1-08). */
function trim(task,key,limit=64) {
  const entries=Object.entries(task[key]??{});
  if(entries.length>limit)task[key]=Object.fromEntries(entries.slice(-limit));
}
