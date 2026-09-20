import fs from 'node:fs';
import path from 'node:path';
import {TransportManifests,manifestView,groupIdFor} from './transport-manifest.mjs';
import {ReplyTail} from './reply-tail.mjs';

const within=(work,ms)=>{let timer;return Promise.race([work,new Promise(resolve=>{timer=setTimeout(resolve,ms);timer.unref?.();})]).finally(()=>clearTimeout(timer));};

/** Generation owns wording. Transport checks only the public body and its identity. */
export function directReply(entries) {
  if(!Array.isArray(entries)||!entries.length)return {state:'pending',reason:'empty-reply',route:'repair'};
  const invalid=entries.find(e=>e.repair_reason||typeof e.text!=='string'||!e.text.trim());
  if(invalid)return {state:'pending',reason:invalid.repair_reason??'empty-reply',route:'repair'};
  return {state:'ready',checked:entries.map(e=>({state:'ready',draft_id:e.draft_id,text:e.text,references:e.references??[]}))};
}

export function outboxEvidence(directories) {
  const values=[];
  for(const directory of directories) {
    if(!fs.existsSync(directory))continue;
    for(const name of fs.readdirSync(directory).filter(n=>n.endsWith('.json'))) {
      try {
        const value=JSON.parse(fs.readFileSync(path.join(directory,name),'utf8'));
        if(value.id&&value.state)values.push({id:value.id,text:value.text??'',state:value.state,
          references:value.references??[],draft_id:value.draftId,message_id:value.messageId,
          at:value.acceptedAt??value.checkedAt??value.attemptedAt,
          ...(typeof value.submissionStarted==='boolean'?{submission_started:value.submissionStarted}:{})});
      }catch{/* Transport reconciles unreadable receipts. */}
    }
  }
  return values.sort((a,b)=>(b.at??'').localeCompare(a.at??''));
}

/** One sender. Old pending journals are imported, never dispatched by a second implementation. */
export class ReplyGuard {
  constructor({call,directory,outbox=()=>[],clock=()=>Date.now(),onOutcome=()=>{},
    manifestDirectory,channel='feishu',contracts,receipt,emit,lease,role,hooks,sleep,retry,
    replyTailDecision=true,decideTail=null,ownerEpoch=null,onTail=null,onWake=()=>{},
    tailLimits,tailHooks,tailWaitMs=5000,regenerate=null,notifyFailure=null}) {
    Object.assign(this,{call,directory,outbox,clock,onOutcome,channel,tailWaitMs,regenerate,notifyFailure});
    this.active=new Map();
    this.manifests=new TransportManifests({directory:manifestDirectory??path.join(directory,'reply-manifests'),
      clock,contracts,emit,lease,role,hooks,sleep,retry,
      receipt:receipt??(async id=>(await this.outbox()).find(r=>r.id===id)??null),
      cancelShare:draftId=>this.call?.('share-cancel',{draft_id:draftId}),
      review:requests=>this.checkGroup(requests),onOutcome,
      keepLive:m=>['partial','undeliverable'].includes(m.state)&&!m.recovery?.notice});
    this.tail=replyTailDecision?new ReplyTail({manifests:this.manifests,clock,decide:decideTail,
      ownerEpoch,onWake,hooks:tailHooks,limits:tailLimits,
      onEvent:detail=>(onTail??onOutcome)(detail)}):null;
  }
  async check(request) {
    const result=await this.checkGroup([request]);
    return result.state==='ready'?result.checked[0]:result;
  }
  async checkGroup(entries) {
    for(const inputId of new Set(entries.filter(e=>!e.work&&e.reply_id).map(e=>e.reply_id))) {
      let choice;
      try{choice=await this.call?.('reply-status',{input_id:inputId});}
      catch{/* Memory availability does not prevent a public reply. */}
      if(['silent','merged'].includes(choice?.action))return {state:choice.action,choice};
    }
    return directReply(entries);
  }
  reviewGroup(requests){return this.checkGroup(requests);}
  report(result,groupId) {
    if(result.busy||result.lost||!result.manifest)return {state:result.busy?'busy':result.lost?'lease-lost':'missing',groupId,entries:[]};
    return manifestView(result.manifest);
  }
  draft(entries,ownerEpoch,channel,hold=null) {
    const continues=this.tail?.continuesFor(entries)??[];
    return this.manifests.createDraft({entries,ownerEpoch,channel,hold,...(continues.length?{continues}:{})});
  }
  replyGroup(entries,ownerEpoch,options={}) {return this.deliver(entries,ownerEpoch,options);}
  deliverGroup(entries,ownerEpoch,_review,options={}) {return this.deliver(entries,ownerEpoch,options);}
  async deliver(entries,ownerEpoch,options={}) {
    const draft=this.draft(entries,ownerEpoch,options.channel??this.channel);
    return this.run(draft.group_id,options);
  }
  run(groupId,options={}) {
    if(this.active.has(groupId))return this.active.get(groupId);
    const work=this.dispatch(groupId,options).finally(()=>this.active.delete(groupId));
    this.active.set(groupId,work);return work;
  }
  async dispatch(groupId,options) {
    const prior=this.manifests.read(groupId);
    if(prior?.recovery)return this.recover(prior,options);
    if(this.tail)await within(this.tail.linkNew(prior).catch(()=>{}),this.tailWaitMs);
    const result=await this.manifests.run(groupId,{guard:this.tail?.guard(options.guard)??options.guard,transport:options.send});
    if(!result.manifest||result.busy||result.lost)return this.report(result,groupId);
    const current=result.manifest;
    if(current.state==='held'&&current.bubbles.some(b=>b.request.repair_reason||!b.request.text?.trim()))
      return this.recover(current,options);
    if(['partial','undeliverable'].includes(current.state))return this.failureNotice(current,options);
    if(this.tail)await within(this.tail.after(result).catch(()=>{}),this.tailWaitMs);
    return this.report(result,groupId);
  }
  async allowed(manifest,options) {
    const answer=await options.guard?.(manifestView(manifest))??'send';
    return typeof answer==='string'?answer:answer.action;
  }
  /** A single text-only correction, persisted on the original delivery record before calling DS. */
  async recover(manifest,options) {
    const id=manifest.group_id;
    if(manifest.recovery?.notice)return {state:'failed',reason:manifest.recovery.reason,groupId:id,
      notified:manifest.recovery.notice.state==='accepted',entries:[]};
    if(await this.allowed(manifest,options)!=='send')return manifestView(manifest);
    if(manifest.recovery?.replacement)return this.run(manifest.recovery.replacement,options);
    // Draft creation and linking can be separated by a restart. Its deterministic
    // batch identity recovers that one replacement without a second model call.
    const replacementId=groupIdFor([{delivery:{memoryBatchId:id+'-r1'}}]);
    if(this.manifests.read(replacementId)) {
      await this.manifests.retireRemainder(id,{reason:'reply-regenerated'});
      return this.run(replacementId,options);
    }
    if(manifest.bubbles.some(b=>b.fragments.some(f=>['submitting','unknown'].includes(f.state))))
      return this.failureNotice(manifest,options,'delivery-outcome-unknown');
    let claimed=false;
    const claim=await this.manifests.mutate(id,value=>{
      if(value.recovery?.attempts)return false;
      value.recovery={attempts:1,reason:value.reason??'invalid-reply',startedAt:this.clock()};claimed=true;
    },{operatorOnly:true});
    if(claim.busy||claim.lost)return this.report(claim,id);
    manifest=claim.manifest??manifest;
    if(!claimed) {
      if(this.clock()-manifest.recovery.startedAt<150000)return {state:'busy',groupId:id,entries:[]};
      return this.failureNotice(manifest,options,'reply-repair-interrupted');
    }
    try {
      const unsent=manifest.bubbles.filter(b=>!['accepted','canceled'].includes(b.state)).map(b=>
        b.fragments.some(f=>f.state==='accepted')?b.fragments.filter(f=>f.state==='unsent').map(f=>b.text.slice(f.start,f.end)).join(''):b.request.text??b.text);
      const repaired=await this.regenerate?.({input_id:manifest.reply_id,reason:manifest.recovery.reason,
        draft:unsent.join('\n\n'),
        sent:manifest.bubbles.flatMap(b=>b.state==='accepted'?[b.text]:b.fragments.filter(f=>f.state==='accepted').map(f=>b.text.slice(f.start,f.end)))});
      if(!Array.isArray(repaired?.bubbles)||!repaired.bubbles.length||repaired.bubbles.some(s=>typeof s!=='string'||!s.trim()))
        throw Error('reply-repair-unavailable');
      if(await this.allowed(manifest,options)!=='send') {
        await this.manifests.retireRemainder(id,{reason:'input-or-session-superseded'});
        return {state:'canceled',groupId:id,entries:[]};
      }
      const basis=manifestView(manifest).entries[0];
      const entries=repaired.bubbles.map((text,i)=>({
        request:{...basis.request,draft_id:id+'-r1-'+i,text,repair_reason:null,references:[]},
        delivery:{...basis.delivery,id:basis.delivery.id+'-r1-'+i,text,references:[],
          memoryBatchId:id+'-r1',expectedBubbles:repaired.bubbles.length,draftId:id+'-r1-'+i}
      }));
      const next=this.draft(entries,manifest.ownerEpoch,manifest.channel);
      await this.manifests.mutate(id,value=>{value.recovery.replacement=next.group_id;value.recovery.receipt=repaired.receipt??null;},{operatorOnly:true});
      await this.manifests.retireRemainder(id,{reason:'reply-regenerated'});
      return this.run(next.group_id,options);
    }catch(error) {
      await this.manifests.mutate(id,value=>{value.recovery.error=error.code??error.message??'reply-repair-failed';},{operatorOnly:true});
      return this.failureNotice(this.manifests.read(id)??manifest,options,'reply-repair-failed');
    }
  }
  async failureNotice(manifest,options,reason=manifest.reason??'reply-incomplete') {
    if(await this.allowed(manifest,options)!=='send')return manifestView(manifest);
    if(manifest.recovery?.notice?.state==='accepted')return {state:'failed',reason,groupId:manifest.group_id,notified:true,entries:[]};
    let receipt;
    try{receipt=await this.notifyFailure?.(manifestView(manifest),reason);}
    catch{receipt={state:'unconfirmed'};}
    await this.manifests.mutate(manifest.group_id,value=>{
      value.recovery={...value.recovery,notice:receipt??{state:'failed'},reason};
    });
    if(!['partial','undeliverable','blocked-unknown'].includes(manifest.state))
      await this.manifests.retireRemainder(manifest.group_id,{reason:'reply-repair-failed'});
    this.onOutcome({state:'failed',reason,inputId:manifest.reply_id,groupId:manifest.group_id,notified:receipt?.state==='accepted'});
    return {state:'failed',reason,groupId:manifest.group_id,notified:receipt?.state==='accepted',entries:[]};
  }
  deferGroup(entries,ownerEpoch){return this.park(entries,ownerEpoch,60000);}
  defer(request,delivery,ownerEpoch){return this.park([{request,delivery}],ownerEpoch,60000);}
  park(entries,ownerEpoch,delay) {
    return manifestView(this.draft(entries,ownerEpoch,this.channel,{reason:'delivery-deferred',retryAt:this.clock()+delay}));
  }
  async resumeDue({guard,send,limit=2}) {
    if(this.resuming)return {state:'idle'};
    this.resuming=true;let handled=0;
    try {
      const imported=this.manifests.importLegacy(this.directory).imported.length;
      if(this.tail)await this.tail.recover().catch(()=>{});
      for(const id of this.manifests.live()) {
        if(handled>=limit)break;
        const manifest=this.manifests.read(id);
        if(!manifest||['retired','accepted','interrupted'].includes(manifest.state)||manifest.retryAt>this.clock())continue;
        if(manifest.state==='held'&&manifest.parked)await this.manifests.unpark(id);
        const result=await this.run(id,{guard,send});
        if(!['busy','lease-lost','unconfirmed'].includes(result.state))handled++;
      }
      if(this.tail)await this.tail.tick({guard}).catch(()=>{});
      return {state:handled?'checked':'idle',checked:handled,imported};
    }finally{this.resuming=false;}
  }
}
