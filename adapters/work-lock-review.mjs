import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {atomicJson,loadState} from './mobile-router.mjs';
import {REVIEWER_LANES,REVIEWER_PURPOSES} from './mobile-reviewer.mjs';

const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const copy=value=>structuredClone(value);
const fingerprint=(router,task)=>hash({reviewPolicyVersion:7,task,inputs:task.inputIds.map(id=>router.state.inputs[id]),configRevision:router.state.configRevision,
  routing:{mode:router.state.mode,requestedMode:router.state.requestedMode,executionEpoch:router.state.executionEpoch,manualProfile:router.state.manualProfile,autoReturnProfile:router.state.autoReturnProfile},exitRequested:router.state.exitRequested});

export const REVIEW_LIMITS=Object.freeze({bytes:64000,items:128,chunkItems:48,maxChunks:8,excerpt:4000,minExcerpt:250});
/** Transport outcomes that will never become a delivery. They are reported to the review
 * as what they are, instead of holding the work lock open for a receipt that cannot come. */
export const FINAL_NON_DELIVERY=Object.freeze(['rejected','undeliverable']);
const DISPOSITIONS=['keep','resume','complete','not_a_task'];
// Reuse original input identity and the existing handoff. No separate chat ledger.
export function workChatInput(router,task) {
  const id=task.contextInputIds?.findLast(id=>{const r=router.state.inputs[id];return r?.intent==='chat'&&r.kind==='owner'&&r.state==='accepted';});
  const input=id&&router.state.inputs[id];
  if(!input||input.executionEpoch!==task.executionEpoch||input.at<task.turnStartedAt||
    task.handoff?.id==='work-chat:'+id||router.state.inputs['handoff:work-chat:'+id])return null;
  return id;
}
const EXCERPT_GAP='\n[…]\n';
const malformed=d=>!DISPOSITIONS.includes(d?.disposition)||!d.reason?.trim()||!Array.isArray(d.evidenceIds)||!d.evidenceIds.length||!Array.isArray(d.remaining)||!Array.isArray(d.discardDraftIds);
const unverified=r=>r?.provider!=='deepseek'||r?.model!=='deepseek-flash'||r?.reasoning!=='high'||!r?.requestId;
const size=value=>JSON.stringify(value).length;
/** Head and tail of an oversized body, with the original length and digest beside
 * it: what is missing is stated, never dropped in silence. */
const excerpt=(item,budget)=>{
  const text=item?.text;
  if(typeof text!=='string'||text.length<=budget)return item;
  const head=Math.ceil(budget*2/3);
  return {...item,text:text.slice(0,head)+EXCERPT_GAP+text.slice(text.length-(budget-head)),text_excerpted:{chars:text.length,sha256:hash(text)}};
};
function split(input,limits) {
  const inputs=input.inputs??[],outputs=input.outputs??[];
  const items=[...inputs.map(value=>['inputs',value]),...outputs.map(value=>['outputs',value])];
  const outline={inputs:inputs.map(i=>i.id),outputs:outputs.map(o=>o.id),items:items.length};
  const room=limits.bytes-size({...input,inputs:[],outputs:[],review_chunk:{index:0,count:0,outline,decisions:[]}});
  if(room<=0||!items.length)return null;
  const per=Math.max(limits.chunkItems,Math.ceil(items.length/limits.maxChunks));
  const batches=[];let batch=[],used=0;
  for(const item of items) {
    const cost=size(item[1])+2;
    if(batch.length&&(batch.length>=per||used+cost>room)){batches.push(batch);batch=[];used=0;}
    batch.push(item);used+=cost;
  }
  if(batch.length)batches.push(batch);
  return batches.map((entries,index)=>({...input,
    inputs:entries.filter(([kind])=>kind==='inputs').map(([,value])=>value),
    outputs:entries.filter(([kind])=>kind==='outputs').map(([,value])=>value),
    review_chunk:{index:index+1,count:batches.length,outline,decisions:[]}}));
}
/** Evidence too large for one review is excerpted and split, never refused: an
 * oversized task would otherwise hold the work lock for ever. Every chunk carries
 * the outline of the whole and, at call time, the decisions taken before it.
 * Null means it does not fit even at the smallest excerpt. */
export function reviewChunks(input,limits=REVIEW_LIMITS) {
  for(let budget=limits.excerpt;;budget=Math.max(limits.minExcerpt,Math.floor(budget/2))) {
    const trimmed={...input,inputs:(input.inputs??[]).map(i=>excerpt(i,budget)),outputs:(input.outputs??[]).map(o=>excerpt(o,budget)),
      cancellableDeferred:(input.cancellableDeferred??[]).map(d=>excerpt(d,budget)),
      ...(input.lastPublicReply?{lastPublicReply:excerpt(input.lastPublicReply,budget)}:{})};
    const parts=split(trimmed,limits);
    if(parts&&parts.length<=limits.maxChunks)return parts;
    if(budget<=limits.minExcerpt)return null;
  }
}
/** One verdict out of several chunk verdicts: the most conservative answer wins,
 * so a single chunk that says keep keeps the lock. A malformed chunk is returned
 * as it came, for the same refusal an unchunked review would get. */
export function mergeDecisions(list) {
  const bad=list.findIndex(malformed);
  if(bad>=0||!list.length)return list[bad>=0?bad:0];
  return {disposition:DISPOSITIONS.find(name=>list.some(d=>d.disposition===name)),
    reason:[...new Set(list.map(d=>d.reason.trim()))].join(' ').slice(0,1200),
    evidenceIds:[...new Set(list.flatMap(d=>d.evidenceIds))],
    remaining:[...new Set(list.flatMap(d=>d.remaining))],
    discardDraftIds:[...new Set(list.flatMap(d=>d.discardDraftIds))]};
}

/** DeepSeek supplies the semantic judgment; the host owns execution and receipts.
 * Reviews run outside the router mutex and never take over a native user turn. */
export class WorkLockReview {
  constructor({router,file,collect,review,cancelDeferred=async()=>{throw Error('Deferred cancellation unavailable');},now=()=>Date.now(),retryMs=20*60000,retryMaxMs=8*3600000,skipRetryMs=5*60000,reviewLimits={}}) {
    Object.assign(this,{router,file,collect,review,cancelDeferred,now,retryMs,retryMaxMs,skipRetryMs});
    this.reviewLimits={...REVIEW_LIMITS,...reviewLimits};
    router.workReviewerEnabled=true;
    this.running=false;this.closed=false;
    const loaded=loadState(file,{now,validate:value=>Boolean(value.attempts)});
    this.state=loaded.value??{schema:1,attempts:{}};
    if(this.state.schema!==1)throw Error('Work review schema mismatch');
    if(loaded.recovery)this.state.recovery=loaded.recovery;
    for(const attempt of Object.values(this.state.attempts))if(attempt.state==='reviewing'){attempt.state='interrupted';attempt.retryAt=now();}
  }
  close(){this.closed=true;}
  /** The same question, on a task that has not changed, is not worth paying for again as
   * soon: each repeat of an unchanged verdict waits twice as long, up to the cap. The
   * attempt is keyed by the task fingerprint, so any change to the task starts at zero
   * again. When the lock is released is not touched by this — only when it is asked. */
  keptRetryAt(repeats){return this.now()+Math.min(this.retryMaxMs,this.retryMs*2**Math.min(repeats,32));}
  save(attempt){this.state.attempts[attempt.id]=attempt;this.state.latest=attempt.id;atomicJson(this.file,this.state,{previous:true});fs.appendFileSync(this.file+'.events.jsonl',JSON.stringify(attempt)+'\n',{mode:0o600});return attempt;}
  view(){const a=this.state.attempts[this.state.latest];return a?{state:a.state,taskId:a.taskId,inputVersion:a.inputVersion,disposition:a.decision?.disposition,reason:a.reason??a.decision?.reason,model:a.receipt?.model,reasoning:a.receipt?.reasoning,verifiedAt:a.receipt?.verifiedAt,checkedAt:a.checkedAt,lastCheckAt:this.state.lastCheckAt??a.checkedAt,retryAt:a.retryAt??(a.checkedAt+this.retryMs),...(a.chunks?{chunks:a.chunks}:{}),...(this.state.recovery?{recovery:this.state.recovery.reason}:{})}: {state:'not-needed',...(this.state.recovery?{recovery:this.state.recovery.reason}:{})};}
  /** One review for the ordinary case, with exactly the request it always sent.
   * Oversized evidence is reviewed chunk by chunk and merged here. */
  async judge(input,chunks) {
    if(!chunks)return this.review(input);
    const decisions=[],receipts=[];
    for(const chunk of chunks) {
      const result=await this.review({...chunk,review_chunk:{...chunk.review_chunk,
        decisions:decisions.map((d,index)=>({index:index+1,disposition:d?.disposition,reason:d?.reason,evidenceIds:d?.evidenceIds}))}});
      decisions.push(result?.decision);receipts.push(result?.receipt);
    }
    return {decision:mergeDecisions(decisions),receipt:{...(receipts.find(unverified)??receipts.at(-1)),chunks:receipts.length}};
  }
  blocked(task,runtime) {
    if(this.router.busy(runtime))return 'native-work-active-or-unknown';
    if(!this.router.verified(runtime,this.router.desiredProfile(runtime,{route:'work'})))return 'active-profile-unverified';
    if(task.stopReason!=='end_turn'||!task.turnEndedAt)return 'native-turn-not-finished';
    if(this.now()-task.turnEndedAt<2000)return 'delivery-settling';
    if(task.handoff&&task.handoff.state!=='accepted')return 'handoff-not-settled';
    if(Object.values(task.tools??{}).some(t=>!['completed','failed','canceled','cancelled'].includes(t.status)))return 'tool-not-terminal';
    if(task.inputIds.some(id=>!['accepted','failed-before-submit','superseded-before-submit'].includes(this.router.state.inputs[id]?.state)))return 'input-not-confirmed';
    if(task.cancelRequested)return 'owner-cancel-pending';
    return null;
  }
  async tick() {
    if(this.closed||this.running)return {state:'busy'};
    this.running=true;this.state.lastCheckAt=this.now();let attempt;
    try {
      const snapshot=await this.router.locked(async()=>{
        this.router.expireUnsubmittedInputs();
        const task=this.router.currentTask();
        if(!task||task.requiresDelivery===false)return null;
        const runtime=await this.router.inspect(),reason=this.blocked(task,runtime);
        return {task:copy(task),inputs:[...task.inputIds,...(task.contextInputIds??[])].map(id=>copy(this.router.state.inputs[id])),key:fingerprint(this.router,task),reason};
      });
      if(!snapshot)return {state:'not-needed'};
      const id='work-review-'+snapshot.key;
      const previous=this.state.attempts[id];
      if(snapshot.reason){
        if(previous?.state==='waiting'&&previous.reason===snapshot.reason){this.state.lastCheckAt=this.now();atomicJson(this.file,this.state,{previous:true});return previous;}
        return this.save({id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,state:'waiting',reason:snapshot.reason,checkedAt:this.now()});
      }
      if(previous&&(previous.state==='applied'||previous.retryAt>this.now()))return previous;
      attempt={id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,key:snapshot.key,checkedAt:this.now(),attempts:(previous?.attempts??0)+1};
      const evidence=await this.collect(snapshot);
      const chatInput=workChatInput(this.router,snapshot.task);
      if(chatInput)evidence.input.workChatInputId=chatInput;
      if(this.closed)return {state:'closed'};
      // Never silently truncate a task into a misleading completion decision: oversized
      // evidence is excerpted and reviewed in bounded chunks, so no size ever leaves the
      // work lock held with no review able to run.
      const oversized=size(evidence.input)>this.reviewLimits.bytes||snapshot.inputs.length>this.reviewLimits.items;
      const chunks=oversized?reviewChunks(evidence.input,this.reviewLimits):null;
      if(oversized&&!chunks)return this.save({id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,state:'waiting',reason:'review-context-needs-summary',checkedAt:this.now()});
      // The reserved user-work lane is recorded with the attempt: this verdict is
      // about the owner's own work and is never reused for any other question.
      attempt=this.save({...attempt,lane:REVIEWER_LANES.reviewWork,purpose:REVIEWER_PURPOSES.reviewWork,evidenceHash:hash(evidence),evidenceIndex:{inputs:(evidence.input.inputs??[]).map(x=>({id:x.id,sourceHash:x.sourceHash})),receipts:evidence.receipts},state:'reviewing',...(chunks?{chunks:chunks.length}:{})});
      const result=await this.judge(evidence.input,chunks),d=result.decision;
      const inputIds=new Set([...snapshot.task.inputIds,...(snapshot.task.contextInputIds??[])]),discardable=new Set((evidence.input.cancellableDeferred??[]).map(x=>x.id));
      // Retain the structured proposal even when its references fail validation.
      // This audit never contains model text/thinking blocks.
      attempt=this.save({...attempt,decision:d,receipt:result.receipt,state:'received',decisionVerified:false});
      if(malformed(d))throw Error('Unverified work review result: invalid-decision-shape');
      if(d.evidenceIds.some(id=>!inputIds.has(id)))throw Error('Unverified work review result: unknown-input-reference');
      if(d.discardDraftIds.some(id=>!discardable.has(id)))throw Error('Unverified work review result: unknown-deferred-reference');
      if(unverified(result.receipt))throw Error('Unverified work review result: provider-receipt-mismatch');
      attempt=this.save({...attempt,state:'reviewed',decisionVerified:true});
      if(this.closed)return this.save({...attempt,state:'interrupted',retryAt:this.now()});
      return await this.router.locked(async()=>{
        const task=this.router.currentTask(),runtime=await this.router.inspect();
        if(!task||task.id!==snapshot.task.id||fingerprint(this.router,task)!==snapshot.key)return this.save({...attempt,state:'superseded',reason:'task-changed-during-review'});
        const blocked=this.blocked(task,runtime);
        if(blocked)return this.save({...attempt,state:'waiting',reason:blocked,retryAt:this.now()+this.retryMs});
        if(d.disposition==='keep') {
          const repeats=previous?.state==='kept'&&previous.decision?.disposition==='keep'?(previous.repeats??0)+1:0;
          return this.save({...attempt,state:'kept',repeats,retryAt:this.keptRetryAt(repeats)});
        }
        // Re-read evidence under the input/switch mutex. Changed receipts or
        // sources cannot be accepted using the earlier model decision.
        const fresh=await this.collect(snapshot);
        if(chatInput)fresh.input.workChatInputId=chatInput;
        if(hash(fresh)!==attempt.evidenceHash)return this.save({...attempt,state:'superseded',reason:'evidence-changed-during-review',retryAt:this.now()});
        const changedRuntime=this.blocked(task,await this.router.inspect());
        if(changedRuntime)return this.save({...attempt,state:'waiting',reason:changedRuntime,retryAt:this.now()+this.retryMs});
        if(d.disposition==='resume') {
          if(!chatInput||workChatInput(this.router,task)!==chatInput||!d.remaining.length||d.discardDraftIds.length||
            !d.evidenceIds.includes(chatInput)||!d.evidenceIds.includes(task.inputIds[0]))throw Error('Work chat continuation lacks its original inputs');
          task.handoff={id:'work-chat:'+chatInput,state:'pending',text:'刚才的插话已经接过。继续原任务尚未完成的部分：'+d.remaining.join('；')+'。先使用已有进度、文件与工具结果，已发送内容不重复发送；如有新的暂停或取消要求，按新要求处理。'};
          this.router.save('work-chat-continuation',{taskId:task.id,inputId:chatInput});
          return this.save({...attempt,state:'applied',appliedAt:this.now()});
        }
        const requirements=evidence.input.inputs.filter(i=>task.inputIds.includes(i.id)&&i.workRequirement!==false);
        if(d.remaining.length||!requirements.length||!d.evidenceIds.includes(requirements[0].id)||!d.evidenceIds.includes(requirements.at(-1).id))throw Error('Work review does not cover original and current inputs');
        const canceled=[],replacements=[],retired=[],undelivered=[];
        for(const [deliveryId,delivery] of Object.entries(task.deliveries??{})) {
          const proof=fresh.receipts?.[deliveryId];
          if(delivery.state==='accepted'&&delivery.messageId&&proof?.state==='accepted'&&proof.messageId===delivery.messageId)continue;
          // Refused by the platform, or past every attempt the transport will make: this
          // bubble will never arrive, and waiting for its receipt would hold the work lock
          // for ever. It is recorded as the non-delivery it is; the rule that some bubble
          // must actually have been delivered is enforced below, unchanged.
          if(FINAL_NON_DELIVERY.includes(proof?.state)){undelivered.push(deliveryId);continue;}
          // A bubble the host retired never reached a transport and never will. That is a
          // settled non-delivery, not an unconfirmed one, and it cannot hold the lock for
          // ever. Delivery evidence is still required from the bubbles that were sent.
          if(proof?.state==='retired'){retired.push(deliveryId);continue;}
          // Failed upload attempts are terminal. An independently verified
          // replacement must satisfy the same artifact before work can close.
          if(proof?.state==='not-submitted'&&proof.fulfilledBy?.length&&proof.fulfilledBy.every(p=>p.messageId&&p.verified)){
            replacements.push([deliveryId,proof.fulfilledBy]);continue;
          }
          if(d.disposition!=='not_a_task'||!d.discardDraftIds.includes(deliveryId)||proof?.state!=='not-submitted')return this.save({...attempt,state:'waiting',reason:'delivery-unconfirmed',retryAt:this.now()+this.retryMs});
          canceled.push(deliveryId);
        }
        // At least one bubble must really have arrived. A router record that says accepted
        // while the transport's own evidence says the bubble was refused is not that proof.
        const settled=new Set(undelivered);
        if(!Object.entries(task.deliveries??{}).some(([deliveryId,x])=>x.state==='accepted'&&x.messageId&&!settled.has(deliveryId)))return this.save({...attempt,state:'waiting',reason:'no-delivery-evidence',retryAt:this.now()+this.retryMs});
        for(const deliveryId of canceled) {
          const proof=await this.cancelDeferred(deliveryId,{reviewId:id,evidence:fresh});
          if(proof?.state!=='canceled-before-send')throw Error('Deferred draft was not canceled');
          task.deliveries[deliveryId]={...task.deliveries[deliveryId],state:'canceled-before-send',reviewId:id};
        }
        const finalBlock=this.blocked(task,await this.router.inspect());
        if(finalBlock){this.router.save('work-review-held',{taskId:task.id,reason:finalBlock});return this.save({...attempt,state:'waiting',reason:finalBlock,retryAt:this.now()+this.retryMs});}
        for(const [deliveryId,fulfilledBy] of replacements)Object.assign(task.deliveries[deliveryId],{state:'not-submitted',fulfilledBy,workReviewId:id});
        for(const deliveryId of retired)Object.assign(task.deliveries[deliveryId],{state:'retired',reason:fresh.receipts[deliveryId].reason,workReviewId:id});
        for(const deliveryId of undelivered)Object.assign(task.deliveries[deliveryId],{state:fresh.receipts[deliveryId].state,reason:fresh.receipts[deliveryId].reason,workReviewId:id});
        task.status=d.disposition==='not_a_task'?'canceled':'completed';
        task.completedAt=this.now();task.workReview={id,disposition:d.disposition,reason:d.reason,evidenceIds:d.evidenceIds,receipt:result.receipt,inputVersion:task.inputVersion,canceledDraftIds:canceled,...(retired.length?{retiredDeliveryIds:retired}:{}),...(undelivered.length?{undeliveredDeliveryIds:undelivered}:{})};
        this.router.save('work-reviewed',{taskId:task.id,reviewId:id,disposition:d.disposition});
        if(!this.router.tasks().length&&this.router.state.mode!=='manual'&&!Object.values(this.router.state.requests).some(request=>request.state==='pending'&&['work','auto','manual'].includes(request.mode)))this.router.recordModeRequest({mode:'auto',commandId:id,reason:'DeepSeek verified the work lifecycle: '+d.reason,notify:false,sourceInputId:'host:'+id});
        return this.save({...attempt,state:'applied',appliedAt:this.now()});
      });
    } catch(error) {
      // A refused lane is not a verdict and never resembles one: the work lock stays
      // held and the review comes back, without spending a failure on it.
      if(error?.leaseSkipped)return attempt?this.save({...attempt,state:'waiting',reason:'work-review-lane-unavailable',retryAt:this.now()+this.skipRetryMs}):{state:'waiting',reason:'work-review-lane-unavailable'};
      return attempt?this.save({...attempt,state:'failed',reason:error.message,receipt:error.receipt??attempt.receipt,retryAt:this.now()+this.retryMs}):{state:'waiting',reason:'work-evidence-unavailable'};
    } finally {this.running=false;}
  }
}
