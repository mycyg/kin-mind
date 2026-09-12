import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const clone = value => structuredClone(value);
const open = task => !['completed','canceled'].includes(task.status);
export const ROUTER_MODELS = Object.freeze({chat:'deepseek-flash',work:'gpt-6-astra'});
export function recentConversation(items) {
  const counts={user:0,assistant:0};
  return items.slice().reverse().filter(item=>Object.hasOwn(counts,item.role)&&++counts[item.role]<=8).reverse();
}
export function modeCommand(text) {
  const value=text.trim().replace(/[~～!！。\s]+$/u,'');
  if (['进入正经模式','正经模式','/mode work'].includes(value)) return 'work';
  if (['退出正经模式','恢复自动分流','恢复自动','/mode auto'].includes(value)) return 'auto';
  return null;
}
export function atomicJson(file, value) {
  fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});
  const temporary=file+'.tmp-'+process.pid;
  const fd=fs.openSync(temporary,'w',0o600);
  try {fs.writeFileSync(fd,JSON.stringify(value,null,2));fs.fsyncSync(fd);} finally {fs.closeSync(fd);}
  fs.renameSync(temporary,file);
}

/** One host owns this durable state; all provider changes and input acceptance
 * share its mutex. MCP requests only record intent and never wait for a turn. */
export class MobileRouter {
  constructor({file,sessionId,inspect,switchModel,classify,waitForIdle,now=()=>Date.now()}) {
    Object.assign(this,{file,sessionId,inspect,switchModel,classify,waitForIdle,now});
    this.tail=Promise.resolve();this.inflight=new Map();
    this.state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{schema:1,sessionId,revision:0,mode:'auto',exitRequested:false,tasks:{},inputs:{},requests:{},history:[],recent:[],config:{classifierTimeoutMs:15000,auditIntervalHours:4}};
    if(this.state.schema!==1||this.state.sessionId!==sessionId)throw Error('Router session mismatch');
    this.state.configRevision??=0;
    // An interrupted acceptance/switch cannot safely be replayed after restart.
    for(const record of Object.values(this.state.inputs))if(record.state==='submitting')record.state='unconfirmed';
    if(this.state.transition?.state==='switching')this.state.transition.state='unconfirmed';
    this.save('startup');
  }
  save(kind,detail={}) {
    this.state.revision++;
    const event={at:this.now(),kind,...detail,revision:this.state.revision};
    this.state.history.push(event);
    this.state.history=this.state.history.slice(-200);
    atomicJson(this.file,this.state);
    fs.appendFileSync(this.file+'.events.jsonl',JSON.stringify(event)+'\n',{mode:0o600});
  }
  locked(fn) {
    const operation=this.tail.then(fn);this.tail=operation.catch(()=>{});return operation;
  }
  tasks() {return Object.values(this.state.tasks).filter(open);}
  snapshot() {return clone(this.state);}
  currentTask() {return this.tasks().at(-1);}
  busy(runtime) {
    return !runtime.known || runtime.sessionId!==this.sessionId || runtime.threadId!==this.sessionId ||
      runtime.nativeSessionId!==this.sessionId || runtime.nativeStatus!=='idle' || runtime.active ||
      runtime.queued>0 || runtime.backgroundTasks>0 || runtime.pendingDeliveries>0;
  }
  verified(runtime,model) {
    return runtime.known&&runtime.profileReady!==false&&runtime.model===model&&
      runtime.sessionId===this.sessionId&&runtime.threadId===this.sessionId&&runtime.nativeSessionId===this.sessionId;
  }
  async reconcileTransition(runtime) {
    const transition=this.state.transition;
    if(transition?.state!=='unconfirmed'||this.busy(runtime))return runtime;
    const expected=this.tasks().length?ROUTER_MODELS.work:runtime.model;
    if(Object.values(ROUTER_MODELS).includes(expected)&&this.verified(runtime,expected)) {
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
      this.state.actual=actual;transition.state='failed-restored';this.save('switch-recovered');return actual;
    } catch {this.save('switch-recovery-unconfirmed');return runtime;}
  }
  addTask(input) {
    let task=this.currentTask();
    if(!task) {
      const id='work-'+digest(input.id).slice(0,24);
      task={id,status:'running',requiresDelivery:!['repair','exploration-plan','proactive'].includes(input.kind),inputVersion:0,inputIds:[],summary:input.text.slice(0,1200),tools:{},deliveries:{},createdAt:this.now()};
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
        if(previous.hash!==hash)throw Error('Input id reused with different content');
        if(['submitting','unconfirmed'].includes(previous.state))throw Error('Input acceptance requires reconciliation');
        return clone(previous);
      }
      const stop=/^(?:停止任务|取消当前任务|\/停|\/acp-cancel)[!！。~～\s]*$/.test(input.text.trim());
      const command=stop?'stop':modeCommand(input.text), runtime=await this.inspect();
      let decision,reason;
      if(stop) {for(const task of this.tasks())task.cancelRequested=true;decision='work';reason='owner-stop-command';}
      else if(command==='work') {this.state.mode='work';this.state.exitRequested=false;decision='work';reason='owner-mode-command';}
      else if(command==='auto') {
        this.state.exitRequested=true;
        if(!this.tasks().length && !this.busy(runtime))this.state.mode='auto';
        decision=this.tasks().length||this.busy(runtime)?'work':'chat';reason='owner-exit-command';
      } else if(this.tasks().length||this.state.mode==='work'||(runtime.active&&runtime.model===ROUTER_MODELS.work)) {
        decision='work';reason='work-lock';
      } else if(input.attachments?.length||['repair','work-result','exploration-plan','handoff'].includes(input.kind)) {
        decision='work';reason='work-input';
      } else if(input.kind==='proactive') {decision='chat';reason='casual-outreach';}
      else {
        try {
          let timer;
          const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('classification-timeout')),this.state.config.classifierTimeoutMs);});
          let result;
          try {result=await Promise.race([this.classify({text:input.text,recent:recentConversation(this.state.recent),task:this.currentTask()?.summary??null,timeoutMs:this.state.config.classifierTimeoutMs}),timeout]);}
          finally {clearTimeout(timer);}
          if(!['chat','work'].includes(result?.route))throw Error('Invalid classification');
          decision=result.route;reason=result.reason?.slice(0,200)??'classification';
        } catch {decision='work';reason='classifier-unconfirmed';}
      }
      const task=decision==='work'&&!command?this.addTask(input):this.currentTask();
      const record={id:input.id,hash,kind:input.kind??'owner',state:'selected',route:decision,reason,command,taskId:task?.id,at:this.now()};
      this.state.inputs[input.id]=record;
      if(!input.kind||input.kind==='owner') {
        this.state.recent.push({role:'user',text:input.text.slice(0,4000)});
        this.state.recent=this.state.recent.slice(-16);
      }
      this.save('input-selected',{id:input.id,route:decision,reason});return clone(record);
    });
  }
  dispatch(input,submit) {
    if(this.inflight.has(input.id))return this.inflight.get(input.id);
    const pending=this.dispatchOnce(input,submit).finally(()=>this.inflight.delete(input.id));
    this.inflight.set(input.id,pending);return pending;
  }
  async dispatchOnce(input,submit) {
    const selected=await this.select(input);
    if(selected.state==='accepted')return {route:'deduplicated',model:selected.model};
    for(;;) {
      const outcome=await this.locked(async()=>{
        const record=this.state.inputs[input.id];
        if(record.state!=='selected')throw Error('Input acceptance requires reconciliation');
        const runtime=await this.reconcileTransition(await this.inspect());
        let target=ROUTER_MODELS[record.route];
        if(this.tasks().length||this.state.mode==='work')target=ROUTER_MODELS.work;
        // A queued work request must not change the provider mid-DeepSeek turn.
        if((target!==runtime.model||runtime.profileReady===false)&&this.busy(runtime))return null;
        if(!runtime.known)throw Error('Native runtime requires reconciliation');
        if(this.state.transition?.state==='unconfirmed')throw Error('Provider switch requires reconciliation');
        if(target!==runtime.model||runtime.profileReady===false) {
          this.state.transition={state:'switching',from:runtime.model,to:target,reason:record.reason,at:this.now()};this.save('switch-requested');
          try {
            const actual=await this.switchModel(target);
            if(!this.verified(actual,target))throw Error('Provider verification failed');
            this.state.actual=actual;this.state.transition.state='applied';this.save('switch-applied',{model:target});
          } catch {
            // No owner input has been submitted. Restore the work provider only
            // after a fresh idle check; ambiguous restoration remains held.
            try {
              const current=await this.inspect();if(this.busy(current))throw Error('busy');
              const restored=await this.switchModel(ROUTER_MODELS.work);
              if(!this.verified(restored,ROUTER_MODELS.work))throw Error('restore-unconfirmed');
              this.state.actual=restored;this.state.transition.state='failed-restored';target=ROUTER_MODELS.work;
              if(!record.taskId&&!record.command)record.taskId=this.addTask(input).id;
              this.save('switch-failed-restored');
            } catch {this.state.transition.state='unconfirmed';this.save('switch-unconfirmed');throw Error('Provider switch requires reconciliation');}
          }
        } else this.state.actual=runtime;
        if(target===ROUTER_MODELS.work&&!record.taskId&&!record.command&&this.currentTask())record.taskId=this.addTask(input).id;
        record.state='submitting';record.model=target;this.save('input-submitting',{id:input.id});
        try {
          const route=await submit({model:target,taskId:record.taskId,reason:record.reason,command:record.command});
          if(route==='superseded') {record.state='superseded';this.save('input-superseded',{id:input.id});return{route,model:target};}
          record.state='accepted';record.acceptedAt=this.now();this.save('input-accepted',{id:input.id});
          return{route,model:target};
        } catch {record.state='unconfirmed';this.save('input-unconfirmed',{id:input.id});throw Error('Input acceptance requires reconciliation');}
      });
      if(outcome)return outcome;
      await this.waitForIdle();
    }
  }
  async requestMode(request) {
    return this.locked(async()=>{
      if(!request.commandId||!['work','auto'].includes(request.mode)||!request.reason?.trim())throw Error('Invalid mode request');
      const hash=digest(request), previous=this.state.requests[request.commandId];
      if(previous) {if(previous.hash!==hash)throw Error('Command id conflict');return clone(previous);}
      if(request.expectedRevision!==undefined&&request.expectedRevision!==this.state.configRevision)throw Error('Router configuration revision changed; read current state');
      if(request.completedTaskId&&(!this.state.tasks[request.completedTaskId]||!open(this.state.tasks[request.completedTaskId])))throw Error('Task is not open');
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
        sourceInputId:Object.values(this.state.inputs).filter(i=>i.kind==='owner').at(-1)?.id,at:this.now()};
      this.state.requests[request.commandId]=result;this.save('mode-request',{commandId:request.commandId,mode:request.mode});
      return clone(result);
    });
  }
  async applyPendingMode() {
    return this.locked(async()=>{
      const runtime=await this.reconcileTransition(await this.inspect());
      const request=Object.values(this.state.requests).findLast(r=>r.state==='pending');
      if(!request||this.busy(runtime)||this.state.transition?.state==='unconfirmed')return{state:'pending'};
      if(request.mode==='auto'&&this.tasks().length)return{state:'work-held'};
      const target=ROUTER_MODELS[request.mode==='work'?'work':'chat'];
      try {
        if(!this.verified(runtime,target)) {
          this.state.transition={state:'switching',from:runtime.model,to:target,reason:request.reason,at:this.now()};this.save('switch-requested');
          const actual=await this.switchModel(target);
          if(!this.verified(actual,target))throw Error('Unverified mode change');
          this.state.actual=actual;this.state.transition.state='applied';
        } else this.state.actual=runtime;
        // A later request replaces earlier pending mode intents, but preserves
        // their receipts and the task completion proposal they may have carried.
        for(const prior of Object.values(this.state.requests))if(prior!==request&&prior.state==='pending'){prior.state='superseded';prior.supersededBy=request.commandId;}
        request.state='applied';request.appliedAt=this.now();this.save('mode-applied',{commandId:request.commandId,model:target});
      } catch {
        request.state='failed';request.failedAt=this.now();
        this.state.transition.state='unconfirmed';this.save('mode-failed',{commandId:request.commandId});
        await this.reconcileTransition(await this.inspect());
      }
      return clone(request);
    });
  }
  observe(kind,data={}) {
    return this.locked(async()=>{
      const task=data.taskId?this.state.tasks[data.taskId]:this.currentTask();
      if(kind==='reply'&&data.final) {this.state.recent.push({role:'assistant',text:data.text.slice(0,4000)});this.state.recent=this.state.recent.slice(-16);}
      if(task&&open(task)) {
        if(kind==='prompt-start') {task.status='running';task.turnStartedAt=this.now();delete task.turnEndedAt;}
        if(kind==='prompt-end') {task.turnEndedAt=this.now();task.stopReason=data.stopReason;if(data.stopReason!=='end_turn')task.status='failed';}
        if(kind==='tool')task.tools[data.id]={status:data.status??task.tools[data.id]?.status??'pending'};
        if(kind==='delivery')task.deliveries[data.id]={state:data.state,messageId:data.messageId,at:this.now()};
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
      const runtime=await this.reconcileTransition(await this.inspect());this.state.actual=runtime;
      if(this.busy(runtime))return {state:'busy'};
      let changed=false;
      for(const task of this.tasks()) {
        if(task.cancelRequested) {task.status='canceled';task.canceledAt=this.now();changed=true;continue;}
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
        if(request.mode==='work'&&this.verified(runtime,ROUTER_MODELS.work) || request.mode==='auto'&&!this.tasks().length&&this.verified(runtime,ROUTER_MODELS.chat)) {request.state='applied';request.appliedAt=this.now();changed=true;}
      }
      if(changed)this.save('reconciled');return {state:this.tasks().length?'work-held':'idle'};
    });
  }
}
