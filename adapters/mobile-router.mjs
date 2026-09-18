import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {writeJsonAtomic,readJsonFile,loadJson} from './atomic-json.mjs';
import {publicMobileRuntime,runtimeReply} from './mobile-controls.mjs';
import {conversationClock} from './conversation-time.mjs';
import {messageIntents} from './mobile-reviewer.mjs';

const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const clone = value => structuredClone(value);
const open = task => !['completed','canceled'].includes(task.status);
export const ROUTER_MODELS = Object.freeze({chat:'deepseek-flash',work:'gpt-6-astra'});
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
  constructor({file,sessionId,inspect,switchModel,classify,waitForIdle,now=()=>Date.now(),binding=null,replyTail=null,classifyIntents=false}) {
    Object.assign(this,{file,sessionId,inspect,switchModel,classify,waitForIdle,now,replyTail,classifyIntents:classifyIntents===true});
    this.tail=Promise.resolve();this.inflight=new Map();
    const loaded=loadState(file,{now,validate:value=>typeof value.sessionId==='string'&&Boolean(value.tasks&&value.inputs&&value.requests)});
    this.state=loaded.value??{schema:1,sessionId,revision:0,mode:'auto',exitRequested:false,tasks:{},inputs:{},requests:{},history:[],recent:[],config:{classifierTimeoutMs:15000,auditIntervalHours:4}};
    if(this.state.schema!==1)throw Error('Router schema mismatch');
    // A quarantined revision is never a silent fresh start: what happened is recorded,
    // and with nothing left to restore the accepted inputs come back from the journal.
    if(loaded.recovery){this.state.recovery=loaded.recovery;if(!loaded.value)this.state.recovery.restoredInputs=this.restoreLedger();}
    if(binding){
      if(this.state.conversationId&&this.state.conversationId!==binding.conversationId)throw Error('Router logical conversation mismatch');
      if(this.state.sessionId!==sessionId&&!this.state.conversationId)throw Error('Unmigrated router session mismatch');
      this.state.conversationId=binding.conversationId;this.state.generation=binding.generation;this.state.nativeSessionId=binding.nativeSessionId;this.state.sessionId=sessionId;
    }else if(this.state.sessionId!==sessionId)throw Error('Router session mismatch');
    this.state.configRevision??=0;this.state.notices??={};this.state.operations??={};this.state.semanticPending??={};
    for(const operation of Object.values(this.state.operations))if(['submitted','running'].includes(operation.state))operation.state='unconfirmed';
    for(const notice of Object.values(this.state.notices))if(notice.state==='sending')notice.state='unconfirmed';
    // A classification attempt interrupted mid-call is retried, never concluded.
    for(const entry of Object.values(this.state.semanticPending))if(entry.state==='classifying')entry.state='retry';
    // An interrupted acceptance/switch cannot safely be replayed after restart.
    for(const record of Object.values(this.state.inputs)){if(record.state==='submitting')record.state='unconfirmed';if(record.state==='preparing')record.state='failed-before-submit';}
    if(this.state.transition?.state==='switching')this.state.transition.state='unconfirmed';
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
  busy(runtime) {
    if(Object.values(this.state.notices).some(n=>n.state==='sending'))return true;
    if(Object.values(this.state.operations).some(o=>['submitted','running','unconfirmed'].includes(o.state)))return true;
    return !runtime.known || runtime.sessionId!==this.sessionId || runtime.threadId!==this.sessionId ||
      runtime.nativeSessionId!==(this.state.nativeSessionId??this.sessionId) || runtime.nativeStatus!=='idle' || runtime.active ||
      runtime.queued>0 || runtime.backgroundTasks>0 || runtime.pendingDeliveries>0 || runtime.handoffTasks>0;
  }
  verified(runtime,model) {
    return runtime.known&&runtime.profileReady!==false&&runtime.model===model&&
      runtime.sessionId===this.sessionId&&runtime.threadId===this.sessionId&&runtime.nativeSessionId===(this.state.nativeSessionId??this.sessionId);
  }
  async reconcileTransition(runtime) {
    const transition=this.state.transition;
    if(transition?.state!=='unconfirmed'||this.busy(runtime))return runtime;
    const expected=this.tasks().length?ROUTER_MODELS.work:runtime.model;
    if(Object.values(ROUTER_MODELS).includes(expected)&&this.verified(runtime,expected)) {
      this.finishTransition(runtime);
      transition.state=runtime.model===transition.to?'applied-reconciled':'failed-restored';
      this.state.actual=runtime;this.save('switch-reconciled',{model:runtime.model});return runtime;
    }
    // One recovery attempt restores the work provider. It never replays an input
    // whose native acceptance is uncertain, and never replaces a busy runtime.
    if(transition.recoveryAttemptedAt)return runtime;
    transition.recoveryAttemptedAt=this.now();this.save('switch-recovery-requested');
    try {
      const actual=await this.switchModel(ROUTER_MODELS.work);
      if(!this.verified(actual,ROUTER_MODELS.work))throw Error('Unverified recovery');
      this.finishTransition(actual);transition.state='failed-restored';this.save('switch-recovered');return actual;
    } catch {this.save('switch-recovery-unconfirmed');return runtime;}
  }
  addTask(input) {
    let task=this.currentTask();
    if(!task) {
      const id='work-'+digest(input.id).slice(0,24);
      task={id,conversationId:this.state.conversationId,generation:this.state.generation,status:'running',requiresDelivery:!['repair','exploration-plan','proactive'].includes(input.kind),inputVersion:0,inputIds:[],summary:input.text.slice(0,1200),tools:{},deliveries:{},createdAt:this.now()};
      this.state.tasks[id]=task;
    }
    if(!task.inputIds.includes(input.id)) {task.inputIds.push(input.id);task.inputVersion++;delete task.completion;}
    if(!input.kind||input.kind==='owner')task.requiresDelivery=true;
    this.state.mode='work'; return task;
  }
  async select(input) {
    return this.locked(async()=>{
      const hash=digest([input.text,input.attachments??[]]);
      const previous=this.state.inputs[input.id];
      if(previous) {
        if(previous.recovered)throw Error('Input acceptance requires reconciliation');
        if(previous.hash!==hash)throw Error('Input id reused with different content');
        if(['submitting','unconfirmed'].includes(previous.state))throw Error('Input acceptance requires reconciliation');
        if(previous.state==='failed-before-submit'){previous.state='selected';this.save('input-preparation-retry',{id:input.id});}
        return clone(previous);
      }
      // The literal stop is read before any model is asked, and answers on its own when none can be.
      const stop=/^(?:停止任务|取消当前任务|\/停|\/acp-cancel)[!！。~～\s]*$/.test(input.text.trim());
      const owner=!input.kind||input.kind==='owner';
      const intents=this.classifyIntents&&owner;
      let command=stop?'stop':owner&&!input.attachments?.length?(modeCommand(input.text)??(input.text.trim()==='/compact'?'compact':null)):null;
      const runtime=await this.inspect();
      if(input.kind==='proactive'&&(this.busy(runtime)||this.tasks().length||this.state.mode==='work'))return {state:'deferred',reason:'owner-work-held'};
      let decision,reason,recall={mode:'light',reason:'no-semantic-recall-decision'},fileSend=null,stopIntent=null;
      // The reply tail never decides routing: a port that is absent, slow to answer or failing changes nothing here.
      const port=async(method,detail)=>{try{return await this.replyTail?.[method]?.(detail)??null;}catch{return null;}};
      let tail=null,offered=null,classified=false,semanticFailure=null;
      if(stop) {
        for(const task of this.tasks())task.cancelRequested=true;decision='work';reason='owner-stop-command';
        // A literal stop needs no model: whatever is still unsent is retired by the host itself.
        if(owner&&this.replyTail)tail={carrier:'owner-stop',...(await port('stopped',{inputId:input.id}))};
      }
      else if(['work','auto','status','watch'].includes(command)) {decision='control';reason='owner-runtime-'+command;
      } else if(command==='compact') {decision='maintenance';reason='native-compact';
      // An owner message with attachments used to be work without anyone reading it. With
      // intents on it is classified like any other, with the attachment metadata in view.
      } else if(input.attachments?.length&&!intents||['repair','work-result','exploration-plan','handoff'].includes(input.kind)) {
        decision='work';reason='work-input';
      } else if(input.kind==='proactive') {decision='chat';reason='casual-outreach';}
      else {
        // An interrupted reply with nothing unknown about it rides on this call. With none,
        // the classifier is asked exactly what it was always asked.
        classified=true;
        offered=owner?await port('pending',{id:input.id,text:input.text}):null;
        if(!offered?.reply)offered=null;
        const files=intents?attachmentMetadata(input.attachments):[];
        try {
          const result=await this.askClassification(input,{files,intents,offered,runtime,wait:this.state.config.classifierTimeoutMs});
          ({decision,reason,command,recall,fileSend,stopIntent}=this.readClassification(result,{owner,intents,allowStop:true}));
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
      // Attachments and commands are never classified, so they cannot carry a tail decision either.
      if(owner&&!stop&&!classified)await port('missed',{inputId:input.id,reason:'not-classified'});
      if(semanticFailure) {
        const current=this.currentTask();
        this.state.semanticPending[input.id]={id:input.id,hash,kind:input.kind??'owner',text:input.text.slice(0,4000),
          attachments:intents?attachmentMetadata(input.attachments):[],occurredAt:input.occurredAt??null,receivedAt:input.receivedAt??null,
          conversationId:this.state.conversationId??null,generation:this.state.generation??null,
          taskId:current?.id??null,inputVersion:current?.inputVersion??null,taskVersions:Object.fromEntries(this.tasks().map(t=>[t.id,t.inputVersion])),
          model:runtime.model??null,mode:this.state.mode,failure:{class:semanticFailure,at:this.now()},
          attempts:1,maxAttempts:SEMANTIC_RETRY_BUDGET,nextAttemptAt:this.now(),state:'pending',createdAt:this.now(),updatedAt:this.now()};
        const record={id:input.id,hash,kind:input.kind??'owner',state:'semantic-pending',route:null,intent:null,reason:'classification-'+semanticFailure,recall,command:null,taskId:null,at:this.now(),conversationId:this.state.conversationId,generation:this.state.generation,nativeThreadId:this.sessionId,...(tail?{tail}:{})};
        this.state.inputs[input.id]=record;
        if(!input.kind||input.kind==='owner')this.rememberOwner(input);
        this.save('input-semantic-pending',{id:input.id,failure:semanticFailure});
        return clone(record);
      }
      const intent=decision;
      const locked=this.applyRouteLock(input,{decision,reason,intent,command,runtime});
      ({decision,reason}=locked);
      const task=locked.task;
      const record={id:input.id,hash,kind:input.kind??'owner',state:'selected',route:decision,intent,reason,recall,command,taskId:task?.id,at:this.now(),conversationId:this.state.conversationId,generation:this.state.generation,nativeThreadId:this.sessionId,...(tail?{tail}:{}),...(fileSend?{fileSend}:{}),...(stopIntent?{stop:stopIntent}:{})};
      this.state.inputs[input.id]=record;
      if(!input.kind||input.kind==='owner')this.rememberOwner(input);
      this.save('input-selected',{id:input.id,route:decision,reason});return clone(record);
    });
  }
  /** One bounded ask of the classifier, with its own clock. The longest the call
   * may wait is the caller's; the answer is never rerouted by trigger words. */
  async askClassification(input,{files=[],intents=false,offered=null,runtime=null,wait}) {
    let timer;
    const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('classification-timeout')),wait);});
    try {return await Promise.race([this.classify({text:input.text,clock:conversationClock(input,this.now()),recent:recentConversation(this.state.recent),task:this.currentTask()?.summary??null,mode:this.state.mode,workHeld:Boolean(this.tasks().length||runtime?.active&&runtime.model===ROUTER_MODELS.work),timeoutMs:wait,...(files.length?{attachments:files}:{}),...(intents?{intents:true}:{}),...(offered?{interruptedReply:offered.reply}:{})}),timeout]);}
    finally {clearTimeout(timer);}
  }
  /** A classification answer, validated and read within its bounds. Never owns the
   * execution lock, and only ever exists when the classifier actually answered. */
  readClassification(result,{owner,intents,allowStop=false}={}) {
    if(!['chat','work','control'].includes(result?.route))throw Error('Invalid classification');
    let command=null;
    if(result.route==='control') {if(!owner||!['status','watch','work','auto'].includes(result.control))throw Error('Invalid runtime control');command=result.control;}
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
    // an open task, and a classifier that actually answered. It is never inferred here,
    // and a late answer applies only against the task basis it was read from.
    if(allowStop&&asked.stop==='current_task'&&this.tasks().length) {
      const taskIds=this.tasks().map(task=>task.id);
      for(const task of this.tasks())task.cancelRequested=true;
      stopIntent={requested:'current_task',decisionSource:CLASSIFIER_DECISION,taskIds};
    }
    return {decision,reason,command,recall,fileSend,stopIntent};
  }
  /** DeepSeek judges meaning; its answer never owns the execution lock. Real work
   * keeps its model, tools and delivery lock; a chat that arrives while work is
   * open rides as context on the existing task, never as a new one. */
  applyRouteLock(input,{decision,reason,intent,command,runtime}) {
    if(!command&&(this.tasks().length||this.state.mode==='work'||runtime.active&&runtime.model===ROUTER_MODELS.work)){decision='work';reason='work-lock: '+reason;}
    const task=decision==='work'&&!command?(intent==='work'?this.addTask(input):this.currentTask()):command?null:this.currentTask();
    if(task&&!task.inputIds.includes(input.id)){task.contextInputIds??=[];task.contextInputIds.push(input.id);}
    return {decision,reason,task};
  }
  rememberOwner(input) {
    this.state.recent.push({role:'user',text:input.text.slice(0,4000),at:input.occurredAt??input.at??this.now(),receivedAt:input.receivedAt??this.now()});
    this.state.recent=this.state.recent.slice(-16);
  }
  dispatch(input,submit) {
    if(this.inflight.has(input.id))return this.inflight.get(input.id);
    const pending=this.dispatchOnce(input,submit).finally(()=>this.inflight.delete(input.id));
    this.inflight.set(input.id,pending);return pending;
  }
  async dispatchOnce(input,submit) {
    const selected=await this.select(input);
    if(selected.state==='deferred')return {route:'deferred',reason:selected.reason};
    if(selected.state==='accepted')return {route:'deduplicated',model:selected.model};
    if(['semantic-failed','semantic-canceled'].includes(selected.state))return {route:selected.state,reason:selected.reason};
    for(;;) {
      // A semantic wait is settled by its own bounded review — driven here while
      // the caller is still alive, and by the host's pump when it is not.
      if(this.state.inputs[input.id]?.state==='semantic-pending')await this.reviewSemanticPending();
      const outcome=await this.locked(async()=>{
        const record=this.state.inputs[input.id];
        if(record.state==='semantic-pending')return null;
        if(['semantic-failed','semantic-canceled'].includes(record.state))return {route:record.state,reason:record.reason};
        if(record.state!=='selected')throw Error('Input acceptance requires reconciliation');
        const runtime=await this.reconcileTransition(await this.inspect());
        if(input.kind==='proactive'&&(this.busy(runtime)||this.tasks().length||this.state.mode==='work'))return {route:'deferred',reason:'owner-work-held'};
        if(record.route==='control') {
          this.acceptControl(record,runtime);record.state='accepted';record.acceptedAt=this.now();
          this.save('control-accepted',{id:input.id,command:record.command});return{route:'host-control',model:runtime.model};
        }
        if(record.route==='maintenance'&&this.busy(runtime))return null;
        let target=record.route==='maintenance'?runtime.model:ROUTER_MODELS[record.route];
        if(record.route!=='maintenance'&&(this.tasks().length||this.state.mode==='work'))target=ROUTER_MODELS.work;
        // A queued work request must not change the provider mid-DeepSeek turn.
        if((target!==runtime.model||runtime.profileReady===false)&&this.busy(runtime))return null;
        if(!runtime.known)throw Error('Native runtime requires reconciliation');
        if(this.state.transition?.state==='unconfirmed')throw Error('Provider switch requires reconciliation');
        if(target!==runtime.model||runtime.profileReady===false) {
          this.startTransition(runtime,target,record.reason,'input',input.id);this.save('switch-requested');
          try {
            const actual=await this.switchModel(target);
            if(!this.verified(actual,target))throw Error('Provider verification failed');
            this.finishTransition(actual);this.save('switch-applied',{model:target});
          } catch {
            // No owner input has been submitted. Restore the work provider only
            // after a fresh idle check; ambiguous restoration remains held.
            try {
              const current=await this.inspect();if(this.busy(current))throw Error('busy');
              const restored=await this.switchModel(ROUTER_MODELS.work);
              if(!this.verified(restored,ROUTER_MODELS.work))throw Error('restore-unconfirmed');
              this.finishTransition(restored);this.state.transition.state='failed-restored';target=ROUTER_MODELS.work;
              if(!record.taskId&&!record.command)record.taskId=this.addTask(input).id;
              this.save('switch-failed-restored');
            } catch {this.state.transition.state='unconfirmed';this.save('switch-unconfirmed');throw Error('Provider switch requires reconciliation');}
          }
        } else this.state.actual=runtime;
        if(target===ROUTER_MODELS.work&&!record.taskId&&!record.command&&this.currentTask())record.taskId=this.currentTask().id;
        if(record.command==='compact')this.state.operations[record.id]={inputId:record.id,kind:'compact',state:'submitted',at:this.now()};
        if(input.kind==='proactive'&&target!==ROUTER_MODELS.chat)return {route:'deferred',reason:'deepseek-not-verified'};
        record.state='preparing';record.model=target;this.save('input-preparing',{id:input.id});
        const markSubmitted=()=>{record.state='submitting';record.submissionStartedAt=this.now();this.save('input-submitting',{id:input.id});};
        if(input.submissionProtocol!=='host-boundary-v1')markSubmitted();
        try {
          const route=await submit({model:target,taskId:record.taskId,reason:record.reason,command:record.command,inputId:record.id,inputVersion:record.taskId?this.state.tasks[record.taskId].inputVersion:null},markSubmitted);
          if(route==='superseded') {if(this.state.operations[record.id])this.state.operations[record.id].state='canceled';record.state='superseded';this.save('input-superseded',{id:input.id});return{route,model:target};}
          record.state='accepted';record.acceptedAt=this.now();this.save('input-accepted',{id:input.id});
          return{route,model:target};
        } catch(error) {record.state=record.submissionStartedAt?'unconfirmed':'failed-before-submit';record.failureStage=record.submissionStartedAt?'native-submit':'preparation';this.save('input-'+record.state,{id:input.id});throw Error(record.submissionStartedAt?'Input acceptance requires reconciliation':'Input preparation failed before native submission',{cause:error});}
      });
      if(outcome)return outcome;
      await this.waitForIdle();
    }
  }
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
        if(!record||record.state!=='semantic-pending'||record.hash!==e.hash||(record.conversationId??null)!==e.conversationId||(record.generation??null)!==e.generation) {
          e.state='superseded';e.updatedAt=this.now();this.save('semantic-review-superseded',{id});return null;
        }
        e.state='classifying';e.attempts++;e.updatedAt=this.now();this.save('semantic-retry',{id,attempt:e.attempts});
        return clone(e);
      });
      if(!entry)continue;
      let result=null,failure=null;
      try {result=await this.askClassification({id:entry.id,kind:entry.kind,text:entry.text,occurredAt:entry.occurredAt,receivedAt:entry.receivedAt},
        {files:entry.attachments??[],intents:this.classifyIntents&&entry.kind==='owner',wait:Math.min((this.state.config.classifierTimeoutMs??15000)*2,CLASSIFY_RETRY_MAX_MS)});}
      catch(error){failure=classificationFailure(error);}
      await this.locked(async()=>{
        const e=this.state.semanticPending[id];if(!e||e.state!=='classifying')return;
        const record=this.state.inputs[id];e.updatedAt=this.now();
        const fail=cls=>{
          e.lastFailure={class:cls,at:this.now()};
          if(e.attempts>=e.maxAttempts) {
            e.state='failed';record.state='semantic-failed';record.reason='classification-exhausted';record.failureClass=e.failure.class;
            this.save('input-semantic-failed',{id,failure:e.failure.class,attempts:e.attempts});
          } else {e.state='retry';e.nextAttemptAt=this.now()+(e.attempts-1)*15000;this.save('semantic-retry-waiting',{id,attempt:e.attempts,failure:cls});}
        };
        if(!record||record.state!=='semantic-pending'||record.hash!==e.hash){e.state='superseded';this.save('semantic-review-superseded',{id});return;}
        if((this.state.conversationId??null)!==e.conversationId||(this.state.generation??null)!==e.generation) {
          e.state='superseded';record.state='semantic-canceled';record.reason='generation-changed';
          this.save('semantic-canceled',{id,reason:'generation-changed'});return;
        }
        if(failure)return fail(failure);
        const owner=e.kind==='owner',intents=this.classifyIntents&&owner;
        // A stop intent read late applies only to the exact task basis it was read from.
        const basisSame=JSON.stringify(Object.fromEntries(this.tasks().map(t=>[t.id,t.inputVersion])))===JSON.stringify(e.taskVersions);
        let read;
        try{read=this.readClassification(result,{owner,intents,allowStop:basisSame});}catch(error){return fail(classificationFailure(error));}
        // A stop that landed while the answer was in flight retires the input: a
        // late work decision must never resurrect or extend what the owner canceled.
        const canceled=Object.values(this.state.tasks).some(t=>t.cancelRequested||t.status==='canceled'&&(t.canceledAt??0)>=e.createdAt);
        if(canceled&&read.decision==='work') {
          e.state='superseded';record.state='semantic-canceled';record.reason='superseded-by-cancel';
          this.save('semantic-canceled',{id,reason:'superseded-by-cancel'});return;
        }
        const runtime=await this.inspect();
        const locked=this.applyRouteLock({id:e.id,kind:e.kind,text:e.text},{decision:read.decision,reason:read.reason,intent:read.decision,command:read.command,runtime});
        Object.assign(record,{state:'selected',route:locked.decision,intent:read.decision,reason:locked.reason,recall:read.recall,command:read.command,
          taskId:locked.task?.id??null,lateClassifiedAt:this.now(),...(read.fileSend?{fileSend:read.fileSend}:{}),...(read.stopIntent?{stop:read.stopIntent}:{})});
        if(!basisSame)record.lateBasis={captured:e.taskVersions,current:Object.fromEntries(this.tasks().map(t=>[t.id,t.inputVersion]))};
        e.state='classified';e.updatedAt=this.now();
        this.save('input-classified-late',{id,route:locked.decision,reason:locked.reason,attempts:e.attempts});
      });
    }
  }
  async requestMode(request) {
    return this.locked(async()=>{
      return this.recordModeRequest(request);
    });
  }
  recordModeRequest(request) {
      if(!request.commandId||!['work','auto'].includes(request.mode)||!request.reason?.trim())throw Error('Invalid mode request');
      const hash=digest(request), previous=this.state.requests[request.commandId];
      if(previous) {if(previous.hash!==hash)throw Error('Command id conflict');return clone(previous);}
      if(request.expectedRevision!==undefined&&request.expectedRevision!==this.state.configRevision)throw Error('Router configuration revision changed; read current state');
      if(request.completedTaskId&&(!this.state.tasks[request.completedTaskId]||!open(this.state.tasks[request.completedTaskId])))throw Error('Task is not open');
      if(request.completedTaskId&&request.completedInputVersion!==this.state.tasks[request.completedTaskId].inputVersion)throw Error('Task input version changed; read current runtime');
      if(request.mode==='work') {
        this.state.mode='work';this.state.exitRequested=false;
        if(request.handoff) {
          const task=this.addTask({id:'handoff:'+request.commandId,text:request.handoff});
          task.handoff={id:request.commandId,text:request.handoff,state:'pending'};
        }
      } else {
        this.state.exitRequested=true;
        if(request.completedTaskId) {
          const task=this.state.tasks[request.completedTaskId];
          task.completion={inputVersion:task.inputVersion,at:this.now(),summary:request.reason};
        }
        if(!this.tasks().length)this.state.mode='auto';
      }
      this.state.configRevision++;
      const result={state:'pending',mode:request.mode,commandId:request.commandId,hash,reason:request.reason,revision:this.state.configRevision,
        sourceInputId:request.sourceInputId??Object.values(this.state.inputs).filter(i=>i.kind==='owner').at(-1)?.id,notify:request.notify===true,at:this.now()};
      for(const prior of Object.values(this.state.requests))if(prior.state==='pending'&&['work','auto'].includes(prior.mode)){
        if(prior.mode===request.mode&&prior.notify){result.notify=true;result.notificationOrigin=prior.notificationOrigin??prior.commandId;result.notificationSubscribers=[...new Set([...(prior.notificationSubscribers??[]),prior.commandId])];}
        prior.state='superseded';prior.supersededBy=request.commandId;
      }
      this.state.requests[request.commandId]=result;this.save('mode-request',{commandId:request.commandId,mode:request.mode});
      return clone(result);
  }
  async applyPendingMode() {
    return this.locked(async()=>{
      const runtime=await this.reconcileTransition(await this.inspect());
      const request=Object.values(this.state.requests).findLast(r=>r.state==='pending'&&['work','auto'].includes(r.mode));
      if(!request||this.busy(runtime)||this.state.transition?.state==='unconfirmed')return{state:'pending'};
      if(request.mode==='auto'&&this.tasks().length)return{state:'work-held'};
      const target=ROUTER_MODELS[request.mode==='work'?'work':'chat'];
      try {
        if(!this.verified(runtime,target)) {
          this.startTransition(runtime,target,request.reason,'mode-request',request.commandId);this.save('switch-requested');
          const actual=await this.switchModel(target);
          if(!this.verified(actual,target))throw Error('Unverified mode change');
          this.finishTransition(actual);
        } else this.state.actual=runtime;
        // A later request replaces earlier pending mode intents, but preserves
        // their receipts and the task completion proposal they may have carried.
        for(const prior of Object.values(this.state.requests))if(prior!==request&&prior.state==='pending'){prior.state='superseded';prior.supersededBy=request.commandId;}
        this.modeApplied(request,this.state.actual);this.save('mode-applied',{commandId:request.commandId,model:target});
      } catch {
        request.state='failed';request.failedAt=this.now();
        this.state.transition.state='unconfirmed';this.save('mode-failed',{commandId:request.commandId});
        await this.reconcileTransition(await this.inspect());
      }
      return clone(request);
    });
  }
  startTransition(before,model,reason,source,sourceId) {
    this.state.transition={id:'switch-'+digest([this.sessionId,this.state.revision,sourceId,model]).slice(0,24),state:'switching',
      from:before.model,to:model,reason,source,sourceId,at:this.now()};
  }
  finishTransition(actual) {
    this.state.actual=actual;Object.assign(this.state.transition,{state:'applied',actualModel:actual.model,verifiedAt:actual.checkedAt,appliedAt:this.now()});
    const transition=this.state.transition;
    if(transition.from&&transition.from!==actual.model&&this.verified(actual,actual.model)) {
      const view=publicMobileRuntime(this.state,actual,this.sessionId);
      // A maintenance restart that puts back the model the owner was last told about
      // changed nothing they can see: the notice is settled as suppressed, never sent.
      const silent=transition.source==='host-restart'&&actual.model===this.lastToldModel();
      const notice=this.queueNotice(transition.id,'model-switched',{transitionId:transition.id,sourceInputId:this.state.inputs[transition.sourceId]?transition.sourceId:this.state.requests[transition.sourceId]?.sourceInputId,
        target:actual.model,from:transition.from,runtime:view.actual,
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
      this.startTransition(this.state.actual,runtime.model,'Observed native model change','runtime-observation');
      this.finishTransition(runtime);this.state.transition.state='observed';this.save('runtime-model-observed');
    }
    this.state.actual=runtime;
  }
  async readRuntime(loaded=true) {
    return this.locked(async()=>{const runtime=await this.inspect();this.observeRuntime(runtime);return publicMobileRuntime(this.state,runtime,this.sessionId,loaded);});
  }
  async restoreRoutingProfile() {
    return this.locked(async()=>{
      if(this.runtimeRestored)return {state:'restored'};
      let runtime=await this.inspect();
      if(this.busy(runtime)||Object.values(this.state.inputs).some(i=>['selected','submitting','unconfirmed'].includes(i.state)))return {state:'waiting'};
      runtime=await this.reconcileTransition(runtime);
      if(this.state.transition?.state==='unconfirmed')return {state:'waiting'};
      const target=this.tasks().length||this.state.mode==='work'?ROUTER_MODELS.work:ROUTER_MODELS.chat;
      if(!this.verified(runtime,target)){
        this.startTransition(runtime,target,'Restore persisted mobile routing mode','host-restart');this.save('restart-profile-requested');
        try {runtime=await this.switchModel(target);if(!this.verified(runtime,target))throw Error('Restart profile unverified');this.finishTransition(runtime);}
        catch(error){this.state.transition.state='unconfirmed';this.save('restart-profile-unconfirmed');throw error;}
      }
      this.observeRuntime(runtime);this.runtimeRestored=true;this.save('restart-profile-restored',{model:runtime.model});
      return {state:'restored',model:runtime.model,threadId:runtime.threadId};
    });
  }
  async prepareModel(model) {
    return this.locked(async()=>{
      const runtime=await this.inspect();
      if(this.tasks().length||this.busy(runtime))throw Error('Work prevents model verification');
      if(this.verified(runtime,model)){this.observeRuntime(runtime);return runtime;}
      this.startTransition(runtime,model,'Host model verification','probe');this.save('switch-requested');
      try {const actual=await this.switchModel(model);if(!this.verified(actual,model))throw Error('Unverified model');this.finishTransition(actual);this.save('switch-applied');return actual;}
      catch(error){this.state.transition.state='unconfirmed';this.save('switch-unconfirmed');throw error;}
    });
  }
  queueNotice(key,kind,extra={}) {
    const id='kin-mode-'+digest([this.sessionId,key,kind]).slice(0,40);
    this.state.notices[id]??={id,kind,state:'pending',sourceInputId:this.state.inputs[key]?key:this.state.requests[key]?.sourceInputId,createdAt:this.now(),...extra};return this.state.notices[id];
  }
  modeApplied(request,runtime) {
    request.state='applied';request.appliedAt=this.now();
    request.result={model:runtime.model,provider:runtime.modelProvider,reasoningEffort:runtime.reasoningEffort,
      sessionId:this.sessionId,verifiedAt:runtime.checkedAt,transitionId:this.state.transition?.id};
    const transition=this.state.transition;
    const notice=transition?.sourceId===request.commandId&&this.state.notices[transition.noticeId];
    if(notice){notice.requestId=request.commandId;notice.subscriberIds=request.notificationSubscribers??[request.commandId];}
    else if(request.notify)this.queueNotice(request.notificationOrigin??request.commandId,'mode-applied',{requestId:request.commandId,subscriberIds:request.notificationSubscribers??[request.commandId],target:runtime.model});
  }
  acceptControl(record,runtime) {
    if(['work','auto'].includes(record.command)) {
      this.recordModeRequest({commandId:'owner-mode:'+record.id,mode:record.command,reason:'Explicit owner mode command',sourceInputId:record.id,notify:true});
      if(this.busy(runtime)||this.tasks().length)this.queueNotice(record.id,'mode-pending');
    } else if(record.command==='watch') {
      const request=Object.values(this.state.requests).findLast(r=>r.state==='pending'&&['work','auto'].includes(r.mode));
      if(request){request.notify=true;request.notificationSubscribers=[...new Set([...(request.notificationSubscribers??[]),record.id])];this.queueNotice(record.id,'mode-pending',{requestId:request.commandId});}
      else this.queueNotice(record.id,'status');
    } else this.queueNotice(record.id,'status');
  }
  observeOperation(inputId,phase,result={}) {
    return this.locked(async()=>{
      const operation=this.state.operations[inputId];if(!operation)throw Error('Unknown native operation');
      operation.state=phase==='start'?'running':result.stopReason==='end_turn'?'completed':'unconfirmed';
      operation.updatedAt=this.now();operation.stopReason=result.stopReason;
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
        if(!view.actual.verified)return null;
        if(n.kind==='mode-applied'&&runtime.model!==n.target){n.state='superseded';n.updatedAt=this.now();this.save('notice-superseded',{id});return null;}
        if(n.kind==='model-switched'&&runtime.model!==n.target) {
          const past=runtimeReply({actual:n.runtime,tasks:[]},{switched:true}).replace('已经切到 ','此前已切到 ');
          n.text=past+' '+runtimeReply(view);
        }
        if(n.state==='retry'&&n.kind!=='model-switched'){delete n.text;delete n.runtime;}
        n.text??=runtimeReply(view,{pending:n.kind==='mode-pending',switched:n.kind==='mode-applied'});
        // Whatever else the message recalls, this is the model it names as the current one.
        n.runtime??=view.actual;n.toldModel=view.actual.model;n.state='sending';n.stage='sending';n.attempts=(n.attempts??0)+1;n.updatedAt=this.now();
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
        if(kind==='accepted'){n.state='accepted';n.messageId=receipt.messageId;n.acceptedAt=this.now();n.replyRecorded=true;n.nextAction='none';n.stage='settled';}
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
  observe(kind,data={}) {
    return this.locked(async()=>{
      const task=data.taskId?this.state.tasks[data.taskId]:this.currentTask();
      if(kind==='reply'&&data.final) {this.state.recent.push({role:'assistant',text:data.text.slice(0,4000),at:data.at??this.now()});this.state.recent=this.state.recent.slice(-16);}
      if(task&&open(task)) {
        if(kind==='prompt-start') {task.status='running';task.turnStartedAt=this.now();delete task.turnEndedAt;}
        if(kind==='prompt-end') {task.turnEndedAt=this.now();task.stopReason=data.stopReason;if(data.stopReason!=='end_turn')task.status='failed';}
        if(kind==='tool')task.tools[data.id]={status:data.status??task.tools[data.id]?.status??'pending',...(data.reason?{reason:data.reason}: {})};
        if(kind==='delivery')task.deliveries[data.id]={state:data.state,messageId:data.messageId,outboxId:data.outboxId,stage:data.stage,submissionStarted:data.submissionStarted,at:this.now()};
      }
      this.save(kind);
    });
  }
  completeInternal(taskId,inputVersion,receipt) {
    return this.locked(async()=>{
      const task=this.state.tasks[taskId];
      if(!task||task.requiresDelivery||task.inputVersion!==inputVersion||!receipt?.verified)return{state:'superseded'};
      task.completion={inputVersion,at:task.turnStartedAt??this.now(),summary:'Host verified internal result'};
      task.internalReceipt=receipt;this.save('internal-result',{taskId});return{state:'recorded'};
    });
  }
  async reconcile() {
    return this.locked(async()=>{
      const runtime=await this.reconcileTransition(await this.inspect());this.observeRuntime(runtime);
      if(this.busy(runtime))return {state:'busy'};
      let changed=false;
      for(const task of this.tasks()) {
        if(task.cancelRequested) {task.status='canceled';task.canceledAt=this.now();changed=true;continue;}
        // With semantic review installed, assistant completion is a proposal.
        // Internal repairs retain their separate verified-result protocol.
        if(this.workReviewerEnabled&&task.requiresDelivery!==false)continue;
        const deliveries=Object.values(task.deliveries);
        if(task.completion?.inputVersion===task.inputVersion && task.stopReason==='end_turn' && task.turnEndedAt>=task.completion.at &&
          Object.values(task.tools).every(tool=>['completed','failed'].includes(tool.status)) &&
          (task.requiresDelivery===false?task.internalReceipt?.verified:
            deliveries.length && deliveries.every(d=>d.state==='accepted'&&d.messageId) && deliveries.some(d=>d.at>=task.completion.at))) {
          task.status='completed';task.completedAt=this.now();changed=true;
        }
      }
      if(changed&&!this.tasks().length) {this.state.mode='auto';this.state.exitRequested=false;}
      for(const request of Object.values(this.state.requests))if(request.state==='pending') {
        if(request.mode==='work'&&this.verified(runtime,ROUTER_MODELS.work) || request.mode==='auto'&&!this.tasks().length&&this.verified(runtime,ROUTER_MODELS.chat)) {this.modeApplied(request,runtime);changed=true;}
      }
      if(changed)this.save('reconciled');return {state:this.tasks().length?'work-held':'idle'};
    });
  }
}
