import fs from 'node:fs';
import path from 'node:path';
import {TransportManifests,manifestView} from './transport-manifest.mjs';
import {ReplyTail} from './reply-tail.mjs';
import {privateReplyBoundary} from './chat-bubbles.mjs';

const within=(work,ms)=>{let timer;return Promise.race([work,new Promise(resolve=>{timer=setTimeout(resolve,ms);timer.unref?.();})]).finally(()=>clearTimeout(timer));};

/** Generation owns wording. Transport checks only the public body and its identity. */
export function directReply(entries) {
  if(!Array.isArray(entries)||!entries.length)return {state:'pending',reason:'empty-reply',route:'repair'};
  const invalid=entries.find(e=>e.repair_reason||typeof e.text!=='string'||!e.text.trim()||privateReplyBoundary(e.text)!==null);
  if(invalid)return {state:'pending',reason:invalid.repair_reason??(typeof invalid.text==='string'&&invalid.text.trim()?'private-reply-envelope':'empty-reply'),route:'repair'};
  return {state:'ready',mode:'direct',checked:entries.map(e=>({state:'ready',draft_id:e.draft_id,text:e.text,references:e.references??[]}))};
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

/** One sender. Old pending journals are imported, never dispatched by a second
 * implementation. Nobody rewrites Kin's words and nothing here speaks to the
 * owner: what could not go out is owed to her next turn (`ReplyTail`), and the
 * owner hears about an input the host could not finish from the router's
 * watchdog, as the system (`notifyOwner`). */
export class ReplyGuard {
  /** `onAnswered` hears once per group that its input was answered (CR-LIFE-13). */
  constructor({call,directory,outbox=()=>[],clock=()=>Date.now(),onOutcome=()=>{},onAnswered=null,
    manifestDirectory,channel='feishu',contracts,receipt,emit,lease,role,hooks,sleep,retry,
    ownerEpoch=null,onTail=null,tailLimits,tailHooks,tailWaitMs=5000}) {
    Object.assign(this,{call,directory,outbox,clock,onOutcome,channel,tailWaitMs});
    this.active=new Map();
    this.manifests=new TransportManifests({directory:manifestDirectory??path.join(directory,'reply-manifests'),
      clock,contracts,emit,lease,role,hooks,sleep,retry,onAnswered,
      receipt:receipt??(async id=>(await this.outbox()).find(r=>r.id===id)??null),
      cancelShare:draftId=>this.call?.('share-cancel',{draft_id:draftId}),
      review:requests=>this.checkGroup(requests),onOutcome});
    this.tail=new ReplyTail({manifests:this.manifests,clock,ownerEpoch,hooks:tailHooks,limits:tailLimits,
      onEvent:detail=>(onTail??onOutcome)(detail)});
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
  report(result,groupId) {
    if(result.busy||result.lost||!result.manifest)return {state:result.busy?'busy':result.lost?'lease-lost':'missing',groupId,entries:[],worked:false};
    return {...manifestView(result.manifest),worked:Boolean(result.worked)};
  }
  draft(entries,ownerEpoch,channel){return this.manifests.createDraft({entries,ownerEpoch,channel});}
  replyGroup(entries,ownerEpoch,options={}) {return this.deliver(entries,ownerEpoch,options);}
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
    const result=await this.manifests.run(groupId,{guard:this.tail.guard(options.guard),transport:options.send});
    if(!result.manifest||result.busy||result.lost)return this.report(result,groupId);
    // An interruption becomes an obligation to Kin's next turn at once.
    if(result.manifest.state==='interrupted') {
      await within(this.tail.after(result).catch(()=>{}),this.tailWaitMs);
      return this.report({...result,manifest:this.manifests.read(groupId)??result.manifest},groupId);
    }
    return this.report(result,groupId);
  }
  /** Before an owner message is submitted: what is still going out from before it
   * stops at a bubble boundary, and what that leaves unsaid is owed (bounded wait). */
  interruptFor({inputId=null,waitMs}={}) {
    return this.tail.interruptFor({inputId,active:[...this.active.values()],...(waitMs!==undefined?{waitMs}:{})});
  }
  /** The periodic pass: legacy pending journals, then the one resume loop of
   * the manifests (AD2-02), each group through this guard's own pass. */
  async resumeDue({guard,send,limit=2}) {
    if(this.resuming)return {state:'idle'};
    this.resuming=true;
    try {
      const imported=this.manifests.importLegacy(this.directory).imported.length;
      await this.tail.recover().catch(()=>{});
      const result=await this.manifests.resumeDue({guard,transport:send,limit,run:id=>this.run(id,{guard,send})});
      return {...result,imported};
    }finally{this.resuming=false;}
  }
}
