import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {atomicJson,ROUTER_MODELS} from './mobile-router.mjs';

const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const copy=value=>structuredClone(value);
const fingerprint=(router,task)=>hash({reviewPolicyVersion:4,task,inputs:task.inputIds.map(id=>router.state.inputs[id]),configRevision:router.state.configRevision,mode:router.state.mode,exitRequested:router.state.exitRequested});

/** DeepSeek supplies the semantic judgment; the host owns execution and receipts.
 * Reviews run outside the router mutex and never take over a native user turn. */
export class WorkLockReview {
  constructor({router,file,collect,review,cancelDeferred=async()=>{throw Error('Deferred cancellation unavailable');},now=()=>Date.now(),retryMs=20*60000}) {
    Object.assign(this,{router,file,collect,review,cancelDeferred,now,retryMs});
    router.workReviewerEnabled=true;
    this.running=false;this.closed=false;
    this.state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{schema:1,attempts:{}};
    if(this.state.schema!==1)throw Error('Work review schema mismatch');
    for(const attempt of Object.values(this.state.attempts))if(attempt.state==='reviewing'){attempt.state='interrupted';attempt.retryAt=now();}
  }
  close(){this.closed=true;}
  save(attempt){this.state.attempts[attempt.id]=attempt;this.state.latest=attempt.id;atomicJson(this.file,this.state);fs.appendFileSync(this.file+'.events.jsonl',JSON.stringify(attempt)+'\n',{mode:0o600});return attempt;}
  view(){const a=this.state.attempts[this.state.latest];return a?{state:a.state,taskId:a.taskId,inputVersion:a.inputVersion,disposition:a.decision?.disposition,reason:a.reason??a.decision?.reason,model:a.receipt?.model,reasoning:a.receipt?.reasoning,verifiedAt:a.receipt?.verifiedAt,checkedAt:a.checkedAt,lastCheckAt:this.state.lastCheckAt??a.checkedAt,retryAt:a.retryAt??(a.checkedAt+this.retryMs)}: {state:'not-needed'};}
  blocked(task,runtime) {
    if(this.router.busy(runtime))return 'native-work-active-or-unknown';
    if(!this.router.verified(runtime,ROUTER_MODELS.work))return 'work-model-unverified';
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
        const task=this.router.currentTask();
        if(!task||task.requiresDelivery===false)return null;
        const runtime=await this.router.inspect(),reason=this.blocked(task,runtime);
        return {task:copy(task),inputs:[...task.inputIds,...(task.contextInputIds??[])].map(id=>copy(this.router.state.inputs[id])),key:fingerprint(this.router,task),reason};
      });
      if(!snapshot)return {state:'not-needed'};
      const id='work-review-'+snapshot.key;
      const previous=this.state.attempts[id];
      if(snapshot.reason){
        if(previous?.state==='waiting'&&previous.reason===snapshot.reason){this.state.lastCheckAt=this.now();atomicJson(this.file,this.state);return previous;}
        return this.save({id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,state:'waiting',reason:snapshot.reason,checkedAt:this.now()});
      }
      if(previous&&(previous.state==='applied'||previous.retryAt>this.now()))return previous;
      const evidence=await this.collect(snapshot);
      if(this.closed)return {state:'closed'};
      // Never silently truncate a task into a misleading completion decision.
      if(JSON.stringify(evidence.input).length>64000||snapshot.inputs.length>128)return this.save({id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,state:'waiting',reason:'review-context-needs-summary',checkedAt:this.now()});
      attempt=this.save({id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,key:snapshot.key,evidenceHash:hash(evidence),evidenceIndex:{inputs:(evidence.input.inputs??[]).map(x=>({id:x.id,sourceHash:x.sourceHash})),receipts:evidence.receipts},state:'reviewing',checkedAt:this.now(),attempts:(previous?.attempts??0)+1});
      const result=await this.review(evidence.input),d=result.decision;
      const inputIds=new Set([...snapshot.task.inputIds,...(snapshot.task.contextInputIds??[])]),discardable=new Set((evidence.input.cancellableDeferred??[]).map(x=>x.id));
      // Retain the structured proposal even when its references fail validation.
      // This audit never contains model text/thinking blocks.
      attempt=this.save({...attempt,decision:d,receipt:result.receipt,state:'received',decisionVerified:false});
      if(!['keep','complete','not_a_task'].includes(d?.disposition)||!d.reason?.trim()||!Array.isArray(d.evidenceIds)||!d.evidenceIds.length||!Array.isArray(d.remaining)||!Array.isArray(d.discardDraftIds))throw Error('Unverified work review result: invalid-decision-shape');
      if(d.evidenceIds.some(id=>!inputIds.has(id)))throw Error('Unverified work review result: unknown-input-reference');
      if(d.discardDraftIds.some(id=>!discardable.has(id)))throw Error('Unverified work review result: unknown-deferred-reference');
      if(result.receipt?.provider!=='deepseek'||result.receipt?.model!=='deepseek-flash'||result.receipt?.reasoning!=='max'||!result.receipt?.requestId)throw Error('Unverified work review result: provider-receipt-mismatch');
      attempt=this.save({...attempt,state:'reviewed',decisionVerified:true});
      if(this.closed)return this.save({...attempt,state:'interrupted',retryAt:this.now()});
      return await this.router.locked(async()=>{
        const task=this.router.currentTask(),runtime=await this.router.inspect();
        if(!task||task.id!==snapshot.task.id||fingerprint(this.router,task)!==snapshot.key)return this.save({...attempt,state:'superseded',reason:'task-changed-during-review'});
        const blocked=this.blocked(task,runtime);
        if(blocked)return this.save({...attempt,state:'waiting',reason:blocked,retryAt:this.now()+this.retryMs});
        if(d.disposition==='keep')return this.save({...attempt,state:'kept',retryAt:this.now()+this.retryMs});
        if(d.remaining.length||!d.evidenceIds.includes(task.inputIds[0])||!d.evidenceIds.includes(task.inputIds.at(-1)))throw Error('Work review does not cover original and current inputs');
        // Re-read evidence under the input/switch mutex. Changed receipts or
        // sources cannot be accepted using the earlier model decision.
        const fresh=await this.collect(snapshot);
        if(hash(fresh)!==attempt.evidenceHash)return this.save({...attempt,state:'superseded',reason:'evidence-changed-during-review',retryAt:this.now()});
        const changedRuntime=this.blocked(task,await this.router.inspect());
        if(changedRuntime)return this.save({...attempt,state:'waiting',reason:changedRuntime,retryAt:this.now()+this.retryMs});
        const canceled=[],replacements=[];
        for(const [deliveryId,delivery] of Object.entries(task.deliveries??{})) {
          const proof=fresh.receipts?.[deliveryId];
          if(delivery.state==='accepted'&&delivery.messageId&&proof?.state==='accepted'&&proof.messageId===delivery.messageId)continue;
          // Failed upload attempts are terminal. An independently verified
          // replacement must satisfy the same artifact before work can close.
          if(proof?.state==='not-submitted'&&proof.fulfilledBy?.length&&proof.fulfilledBy.every(p=>p.messageId&&p.verified)){
            replacements.push([deliveryId,proof.fulfilledBy]);continue;
          }
          if(d.disposition!=='not_a_task'||!d.discardDraftIds.includes(deliveryId)||proof?.state!=='not-submitted')return this.save({...attempt,state:'waiting',reason:'delivery-unconfirmed',retryAt:this.now()+this.retryMs});
          canceled.push(deliveryId);
        }
        if(!Object.values(task.deliveries??{}).some(x=>x.state==='accepted'&&x.messageId))return this.save({...attempt,state:'waiting',reason:'no-delivery-evidence',retryAt:this.now()+this.retryMs});
        for(const deliveryId of canceled) {
          const proof=await this.cancelDeferred(deliveryId,{reviewId:id,evidence:fresh});
          if(proof?.state!=='canceled-before-send')throw Error('Deferred draft was not canceled');
          task.deliveries[deliveryId]={...task.deliveries[deliveryId],state:'canceled-before-send',reviewId:id};
        }
        const finalBlock=this.blocked(task,await this.router.inspect());
        if(finalBlock){this.router.save('work-review-held',{taskId:task.id,reason:finalBlock});return this.save({...attempt,state:'waiting',reason:finalBlock,retryAt:this.now()+this.retryMs});}
        for(const [deliveryId,fulfilledBy] of replacements)Object.assign(task.deliveries[deliveryId],{state:'not-submitted',fulfilledBy,workReviewId:id});
        task.status=d.disposition==='not_a_task'?'canceled':'completed';
        task.completedAt=this.now();task.workReview={id,disposition:d.disposition,reason:d.reason,evidenceIds:d.evidenceIds,receipt:result.receipt,inputVersion:task.inputVersion,canceledDraftIds:canceled};
        this.router.save('work-reviewed',{taskId:task.id,reviewId:id,disposition:d.disposition});
        if(!this.router.tasks().length)this.router.recordModeRequest({mode:'auto',commandId:id,reason:'DeepSeek verified the work lifecycle: '+d.reason,notify:false,sourceInputId:'host:'+id});
        return this.save({...attempt,state:'applied',appliedAt:this.now()});
      });
    } catch(error) {
      return attempt?this.save({...attempt,state:'failed',reason:error.message,receipt:error.receipt??attempt.receipt,retryAt:this.now()+this.retryMs}):{state:'waiting',reason:'work-evidence-unavailable'};
    } finally {this.running=false;}
  }
}
