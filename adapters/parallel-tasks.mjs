import {createHash,randomUUID} from 'node:crypto';
import {privateReplyBoundary} from './chat-bubbles.mjs';

const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex').slice(0,32);
const copy=value=>structuredClone(value);
const terminal=new Set(['completed','failed','canceled','blocked']);
const doneItem=new Set(['accepted','dropped','failed']);
const tState=(t,p)=>!terminal.has(p.state)&&(t.cancelRequested||t.inputVersion!==p.inputVersion)?'cancel_requested':p.state;
const text=(value,max=8000)=>typeof value==='string'&&value.trim()&&value.length<=max?value.trim():null;

/** One task executor and an optional-expression queue, in the router's existing ledger.
 * No model reasoning or raw tool output is admitted to visibleItems. All I/O is a port;
 * the host supplies its owned runtime and existing transport manifests. */
export class ParallelTasks {
  constructor({router,runtime,prepare=async()=>null,retire=async()=>{},deliver,now=()=>Date.now(),onChange=()=>{},saveNote=async()=>{},parseFinal,prepareTimeoutMs=15000}) {
    Object.assign(this,{router,runtime,prepare,retire,deliver,now,onChange,saveNote,parseFinal,prepareTimeoutMs});
    router.state.visibleItems??={};router.state.visibleSequence??=0;
    this.preparing=new Map();this.prepared=new Map();this.notifying=new Map();this.running=new Map();this.pumping=false;this.closed=false;
  }
  async write(fn){return this.router.locked(async()=>{const result=await fn();this.router.save('parallel-task');return result;});}
  tasks(){return Object.values(this.router.state.tasks).filter(t=>t.parallel);}
  snapshot(taskId) {
    const task=this.router.state.tasks[taskId],p=task?.parallel;if(!p)return null;
    const tools=Object.values(p.tools??{}),items=Object.values(this.router.state.visibleItems).filter(i=>i.taskId===taskId&&i.inputVersion===task.inputVersion);
    return {taskId,inputVersion:p.inputVersion,requestedInputVersion:task.inputVersion,executionEpoch:p.executionEpoch,stateRevision:p.revision,observedAt:this.now(),
      state:tState(task,p),phase:p.phase??null,completedCount:tools.filter(t=>t.status==='completed').length,completed:tools.filter(t=>t.status==='completed').slice(-12).map(t=>({id:t.id,name:t.name,receipt:t.eventId})),
      pending:[...tools.filter(t=>!['completed','failed','canceled'].includes(t.status)).map(t=>({id:t.id,name:t.name})),
        ...(!terminal.has(p.state)?[{id:p.runId,kind:'native-task-turn',state:p.state}]:[])],
      blockers:[...(p.blocker?[p.blocker]:[]),...(p.finalDropped?['final-dropped:'+p.finalDropped]:[]),...(items.some(i=>i.kind==='final'&&['failed','unknown'].includes(i.state))?['final-delivery-unconfirmed']:[])],result:task.inputVersion===p.inputVersion?p.result??null:null,lastEventSeq:p.eventSeq??0,lastProgressAt:p.lastProgressAt??null,
      finalDelivery:items.filter(i=>i.kind==='final').map(i=>({id:i.id,state:i.state})),workerThreadId:p.threadId??null};
  }
  snapshots(){return this.tasks().slice(-4).map(t=>this.snapshot(t.id));}
  async start({taskId,inputVersion,commandId,brief},source) {
    if(!text(commandId,160)||!text(brief,12000))throw Error('Task command and bounded brief required');
    return this.write(()=>{
      const t=this.router.state.tasks[taskId];
      if(!t||t.inputVersion!==inputVersion||!t.inputIds.includes(source))throw Error('Task source or version changed');
      if(t.parallel?.commandId===commandId)return this.snapshot(taskId);
      if(t.parallel?.state==='blocked')throw Error('Interrupted execution needs reconciliation before a new dispatch');
      if(this.tasks().some(x=>!terminal.has(x.parallel.state)))throw Error('One background task is already active');
      if(t.cancelRequested||!['proposed','running'].includes(t.status))throw Error('Task is not executable');
      t.status='running';t.acceptedAt??=this.now();t.acceptance='kin-background';
      t.parallel={commandId,brief,sourceInputId:source,runId:randomUUID(),capability:randomUUID(),revision:(t.parallel?.revision??0)+1,
        inputVersion,executionEpoch:t.executionEpoch,state:'admitted',tools:{},eventSeq:0,lastProgressAt:this.now()};
      return this.snapshot(taskId);
    });
  }
  caller(capability) {
    const t=this.tasks().find(t=>t.parallel.capability===capability);
    if(!t||t.cancelRequested||terminal.has(t.parallel.state)||t.inputVersion!==t.parallel.inputVersion||t.executionEpoch!==t.parallel.executionEpoch)throw Error('Worker capability expired');
    return {taskId:t.id,inputVersion:t.inputVersion,executionEpoch:t.executionEpoch,sourceInputId:t.parallel.sourceInputId,runId:t.parallel.runId,threadId:t.parallel.threadId};
  }
  current(item) {
    const t=item.taskId?this.router.state.tasks[item.taskId]:null;
    if(item.expiresAt<=this.now())return false;
    // A worker's items follow its task's epoch; a force switch elsewhere does not stale them.
    if(!item.taskId)return item.executionEpoch===this.router.state.executionEpoch;
    return Boolean(t&&!t.cancelRequested&&t.inputVersion===item.inputVersion&&t.executionEpoch===item.executionEpoch&&
      (item.kind==='final'?t.parallel?.state==='completed':!terminal.has(t.parallel?.state)));
  }
  async publish(args,source,{kind='progress'}={}) {
    const allowed=['commandId','publicText','presentation','meme','important'];
    if(!args||Object.keys(args).some(k=>!allowed.includes(k))||!text(args.commandId,160))throw Error('Only explicit public expression fields are allowed');
    const publicText=text(args.publicText),meme=args.meme;
    if(publicText&&privateReplyBoundary(publicText)!==null)throw Error('Private protocol cannot be published');
    if(meme&&(typeof meme!=='object'||Object.keys(meme).some(k=>!['meme_id','inspection_id'].includes(k))||!text(meme.meme_id,160)||!text(meme.inspection_id,160)))throw Error('Inspected MEME reference required');
    if(!publicText&&!meme)throw Error('Public expression is empty');
    const mode=args.presentation?.mode??'text';if(!['text','voice','both'].includes(mode))throw Error('Invalid presentation');
    if(args.presentation&&Object.keys(args.presentation).some(k=>!['mode','style'].includes(k)))throw Error('Invalid presentation fields');
    // This renderer has no separate style channel. Only publicText enters TTS.
    if(args.presentation?.style)throw Error('Independent speech style is unsupported');
    const body={publicText:publicText??'[MEME]',presentation:{mode,style:''},meme:meme??null,important:args.important===true};
    const id='visible-'+hash([source.runId??source.sourceInputId,kind,args.commandId]);
    return this.write(()=>{
      const old=this.router.state.visibleItems[id];
      if(old){if(JSON.stringify({publicText:old.publicText,presentation:old.presentation,meme:old.meme,important:old.important})!==JSON.stringify(body)||JSON.stringify(old.route??null)!==JSON.stringify(source.route??null)||JSON.stringify(old.answeredInputIds??[])!==JSON.stringify(source.answeredInputIds??[]))throw Error('Visible identity conflicts with frozen content');return {id,state:old.state,sequence:old.sequence};}
      const item={id,commandId:args.commandId,...copy(source),...body,kind,state:'admitted',sequence:++this.router.state.visibleSequence,
        createdAt:this.now(),expiresAt:kind==='progress'&&!body.important?this.now()+60000:Number.MAX_SAFE_INTEGER};
      if(!this.current(item))throw Error('Expression source is stale');
      this.router.state.visibleItems[id]=item;
      return {id,state:'queued',sequence:item.sequence};
    });
  }
  async recover() {
    await this.write(()=>{
      for(const t of this.tasks()) {
        if(!terminal.has(t.parallel.state)&&t.parallel.state!=='finalizing')
          Object.assign(t.parallel,{state:'blocked',blocker:'host-restarted-execution-needs-reconciliation',revision:t.parallel.revision+1});
        if(t.parallel.notification==='submitting')t.parallel.notification='unknown';
      }
      for(const item of Object.values(this.router.state.visibleItems))if(['preparing','ready'].includes(item.state))item.state='admitted';
    });
  }
  runTask(t) {
    if(this.running.has(t.id))return this.running.get(t.id);
    const job=this.advanceTask(t).finally(()=>this.running.delete(t.id));
    this.running.set(t.id,job);return job;
  }
  async advanceTask(t) {
    const p=t.parallel,basis={taskId:t.id,inputVersion:p.inputVersion,executionEpoch:p.executionEpoch,sourceInputId:p.sourceInputId,runId:p.runId,threadId:p.threadId};
    if(t.cancelRequested||t.status==='canceled'||t.inputVersion!==p.inputVersion||t.executionEpoch!==p.executionEpoch) {
      const result=p.state==='admitted'?{state:'interrupted'}:await this.runtime({op:'cancel',runId:p.runId});
      await this.write(()=>{p.state=['interrupted','completed','failed'].includes(result.state)?'canceled':'cancel_requested';
        p.blocker=p.state==='cancel_requested'?'tool-stop-unconfirmed':null;
        if(p.state==='canceled'&&t.cancelRequested){t.status='canceled';t.canceledAt=this.now();t.outcome='owner-canceled';this.router.state.autoRestoreDue=true;}
        p.revision++;});
      // Continue a revised goal only after the original turn actually ended, using
      // the same native thread, never replaying the old initial prompt.
      if(!t.cancelRequested&&t.status!=='canceled'&&p.state==='canceled')await this.write(()=>{
        const updates=t.inputIds.filter(id=>id!==p.sourceInputId).map(id=>({id,text:this.router.state.inputs[id]?.text??this.router.state.recent.find(r=>r.id===id)?.text??''}));
        Object.assign(p,{state:'admitted',runId:randomUUID(),capability:randomUUID(),inputVersion:t.inputVersion,executionEpoch:t.executionEpoch,
          brief:'任务要求已更新，沿当前线程继续，先核对已有动作和结果，不重复已完成动作。'+JSON.stringify(updates),tools:{},eventSeq:0,finalDropped:null,revision:p.revision+1});
      });
      return;
    }
    if(p.state==='admitted') {
      // Persist uncertainty before RPC; a lost reply must never restart work blindly.
      const admitted=await this.write(()=>{if(this.router.frozen?.())return false;p.state='starting';p.revision++;return true;});
      if(!admitted)return;
      const receipt=await this.runtime({op:'start',runId:p.runId,threadId:p.threadId,capability:p.capability,input:p.brief});
      await this.write(()=>{p.threadId=receipt.threadId??p.threadId;p.state=receipt.state;p.blocker=receipt.reason??null;p.phase=receipt.state==='running'?'executing':null;p.revision++;});return;
    }
    if(!['starting','running','cancel_requested','finalizing'].includes(p.state))return;
    if(p.state==='finalizing')return this.finishFinal(t,basis);
    const result=await this.runtime({op:'status',runId:p.runId});
    if(result.state==='missing'){await this.write(()=>{p.state='blocked';p.blocker='native-execution-unavailable';p.revision++;});return;}
    if((result.events??[]).some(e=>e.seq>(p.eventSeq??0))||result.threadId&&result.threadId!==p.threadId)await this.write(()=>{
      for(const e of result.events??[])if(e.seq>(p.eventSeq??0)) {
        p.eventSeq=e.seq;p.lastProgressAt=e.at;
        if(e.type==='tool') {p.tools[e.id]={id:e.id,name:e.name,status:e.status,eventId:p.runId+':'+e.seq};p.phase=e.status==='completed'?'tool-completed':'working';}
      }
      p.threadId=result.threadId??p.threadId;p.revision++;
    });
    if(!['completed','failed','interrupted'].includes(result.state))return;
    if(result.state!=='completed') {await this.write(()=>{p.state=result.state==='interrupted'?'canceled':'failed';p.blocker=result.reason??'native-turn-failed';p.revision++;});return;}
    if(t.inputVersion!==basis.inputVersion||t.cancelRequested||t.executionEpoch!==basis.executionEpoch)return;
    const parsed=this.parseFinal(result.text);
    if(parsed?.state!=='send'||parsed.bubbles.some(b=>privateReplyBoundary(b)!==null)){await this.write(()=>{p.state='failed';p.blocker='invalid-final-envelope';p.revision++;});return;}
    await this.saveNote({id:'note:'+p.runId,groupId:'visible-'+hash([p.runId,'final','final-0']),inputId:p.sourceInputId,note:parsed.private_note,text:parsed.bubbles[0]});
    await this.write(()=>{
      if(t.inputVersion!==basis.inputVersion||t.executionEpoch!==basis.executionEpoch||t.cancelRequested)return;
      const {private_note,...publicFinal}=parsed;
      p.state='finalizing';p.final=publicFinal;p.result={text:parsed.bubbles.join('\n\n'),basis:'completed-native-turn',verification:'model-reported',turnId:result.turnId};p.phase='result-ready';p.revision++;
    });
    return this.finishFinal(t,basis);
  }
  async finishFinal(t,basis) {
    const p=t.parallel,parsed=p.final;
    // Public-only final data stays recoverable until all stable item identities exist.
    // A single ledger transaction makes terminal fencing and final admission atomic.
    await this.write(()=>{
      if(t.inputVersion!==basis.inputVersion||t.executionEpoch!==basis.executionEpoch||t.cancelRequested)return;
      for(let i=0;i<parsed.bubbles.length;i++) {
        const id='visible-'+hash([basis.runId,'final','final-'+i]);
        this.router.state.visibleItems[id]??={id,...copy(basis),publicText:parsed.bubbles[i],
          presentation:parsed.presentations?.[i]??parsed.presentation??{mode:'text',style:''},meme:parsed.meme_refs?.[i]??null,important:true,
          kind:'final',state:'admitted',sequence:++this.router.state.visibleSequence,createdAt:this.now(),expiresAt:Number.MAX_SAFE_INTEGER};
      }
      p.state='completed';delete p.final;p.revision++;
    });
  }
  /** A dropped final is a blocker of its run, told to Kin once by the notification pass. */
  drop(item,reason) {
    item.state='dropped';item.reason=reason;
    const p=item.kind==='final'?this.router.state.tasks[item.taskId]?.parallel:null;
    if(p&&p.runId===item.runId)p.finalDropped=reason;
  }
  async tick() {
    if(this.closed||this.pumping)return;this.pumping=true;
    try {
      for(const t of this.tasks())if(!terminal.has(t.parallel.state))try {await this.runTask(t);}catch {
        await this.write(()=>{t.parallel.blocker='runtime-unavailable';t.parallel.revision++;});
      }
      for(const item of Object.values(this.router.state.visibleItems)) {
        if(doneItem.has(item.state)||item.state==='submitting'||item.state==='unknown')continue;
        if(!this.current(item)){await this.write(()=>this.drop(item,'stale-expression'));continue;}
        if(item.state==='admitted'&&!this.preparing.has(item.id)) {
          await this.write(()=>{item.state='preparing';});
          let timeout;
          const deadline=new Promise((_,reject)=>{timeout=setTimeout(()=>reject(Error('Media preparation deadline')),item.important?Math.min(5000,this.prepareTimeoutMs):this.prepareTimeoutMs);timeout.unref?.();});
          const job=Promise.race([Promise.resolve().then(()=>this.prepare(copy(item))),deadline]).then(value=>this.write(()=>{
            if(this.closed||item.state!=='preparing')return;
            this.prepared.set(item.id,value);if(this.current(item))item.state='ready';else this.drop(item,'stale-expression');
          })).catch(()=>this.write(()=>{
            if(this.closed||item.state!=='preparing')return;
            if(item.important&&item.publicText!=='[MEME]'){item.meme=null;item.presentation={mode:'text',style:''};item.state='ready';}
            else {item.state='failed';item.reason='preparation-failed';}
          })).finally(()=>{clearTimeout(timeout);this.preparing.delete(item.id);});
          this.preparing.set(item.id,job);
        }
      }
      // Preparation never holds the send lane; optional older items yield to ready results.
      for(const item of Object.values(this.router.state.visibleItems).sort((a,b)=>a.sequence-b.sequence)) {
        if(!['ready','submitting','unknown'].includes(item.state))continue;
        if(item.state==='ready'&&!this.current(item)){await this.write(()=>this.drop(item,'stale-expression'));continue;}
        for(const earlier of Object.values(this.router.state.visibleItems))if(earlier.sequence<item.sequence&&earlier.state==='preparing'&&!earlier.important)
          await this.write(()=>{earlier.state='dropped';earlier.reason='newer-expression-ready';});
        const lookupOnly=item.state!=='ready';await this.write(()=>{if(!lookupOnly)item.state='submitting';});
        let receipt;try{receipt=await this.deliver(copy(item),this.prepared.get(item.id),()=>this.current(item),{lookupOnly});}catch{receipt={state:'unknown'};}
        await this.write(()=>{
          item.state=receipt?.state==='accepted'?'accepted':['rejected','undeliverable','partial'].includes(receipt?.state)?'failed':receipt?.state==='retired'?'dropped':receipt?.state==='not-submitted'?'admitted':'unknown';
          if(item.state==='accepted')item.receipt=receipt;
          if(item.state==='dropped')this.drop(item,'delivery-retired');
          const t=this.router.state.tasks[item.taskId];
          if(item.kind==='final'&&t?.parallel?.state==='completed'&&!t.closure&&t.inputVersion===item.inputVersion&&
            Object.values(this.router.state.visibleItems).filter(i=>i.taskId===t.id&&i.inputVersion===t.inputVersion&&i.kind==='final').every(i=>i.state==='accepted')) {
            t.status='completed';t.completedAt=this.now();t.outcome='completed';this.router.state.autoRestoreDue=true;t.parallel.finalDeliveredAt=this.now();
          }
        });
        this.prepared.delete(item.id);
      }
      for(const item of Object.values(this.router.state.visibleItems))if(item.state==='dropped'&&!item.retired)try {
        await this.retire(copy(item));await this.write(()=>{item.retired=true;});
      }catch{}
      for(const t of this.tasks()) {
        const p=t.parallel;
        if(!terminal.has(p.state)||this.notifying.has(p.runId)||this.router.frozen?.())continue;
        const notificationKey=p.state+':'+(p.finalDeliveredAt?'delivered':'unsettled');
        if(p.notificationKey===notificationKey&&['accepted','unknown'].includes(p.notification))continue;
        if(p.state==='completed'&&!p.finalDeliveredAt&&!p.finalDropped&&!Object.values(this.router.state.visibleItems).some(i=>i.taskId===t.id&&i.inputVersion===t.inputVersion&&i.kind==='final'&&['failed','unknown'].includes(i.state)))continue;
        const snapshot=this.snapshot(t.id);
        await this.write(()=>{p.notification='submitting';p.notificationKey=notificationKey;});
        const job=Promise.resolve().then(()=>this.onChange(snapshot)).then(receipt=>this.write(()=>{
          p.notification=receipt?.state==='pending'?'pending':'accepted';
        })).catch(()=>this.write(()=>{p.notification='unknown';})).catch(()=>{}).finally(()=>this.notifying.delete(p.runId));
        this.notifying.set(p.runId,job);
      }
    } finally {this.pumping=false;}
  }
  async close(){this.closed=true;for(const t of this.tasks())if(!terminal.has(t.parallel.state))try{await this.runtime({op:'cancel',runId:t.parallel.runId});}catch{};}
}
