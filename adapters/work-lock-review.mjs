import {createHash} from 'node:crypto';
import {atomicJson,loadState} from './mobile-router.mjs';
import {openTask} from './mobile-controls.mjs';
import {REVIEWER_LANES,REVIEWER_PURPOSES} from './mobile-reviewer.mjs';

const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const copy=value=>structuredClone(value);
/** Transport outcomes that will never become a delivery. */
export const FINAL_NON_DELIVERY=Object.freeze(['rejected','undeliverable']);
/** The facts of a task a summary is written about. Cheap, compared under the router
 * mutex; turn times and the host's own turns are left out, so telling Kin the facts
 * does not itself make them new. */
export function taskFingerprint(router,task) {
  return hash({id:task.id,status:task.status,inputVersion:task.inputVersion,completion:task.completion?.outcome??null,
    owner:(task.contextInputIds??[]).filter(id=>(router.state.inputs[id]?.kind??'owner')==='owner').length,
    deliveries:Object.entries(task.deliveries??{}).map(([id,d])=>[id,d.state,d.messageId??null]).sort(),
    tools:Object.entries(task.tools??{}).map(([id,t])=>[id,t.status]).sort(),executionEpoch:router.state.executionEpoch});
}
const lastActivity=task=>Math.max(task.turnEndedAt??0,task.turnStartedAt??0,task.acceptedAt??0,task.createdAt??0,
  ...Object.values(task.deliveries??{}).map(d=>d.at??0));

/** When Kin has taken on a task and gone quiet about it without declaring how it
 * ended, a model writes the facts down for her. It decides nothing: the lock stays
 * hers until she declares, no draft is withdrawn, and the summary reaches her as
 * facts — in her next turn, and through a host turn that states them if nothing
 * else brings one (N1–N3). Evidence is read outside the router mutex; under it only
 * a fingerprint is compared (AD1-12). An unchanged task is summarized again only
 * after an interval that doubles each time, up to a day. */
export class WorkLockReview {
  constructor({router,file,collect,summarize,deliver=null,now=()=>Date.now(),idleMs=60*60000,repeatMaxMs=24*3600000,retryMs=20*60000,skipRetryMs=5*60000}) {
    Object.assign(this,{router,file,collect,summarize,deliver,now,idleMs,repeatMaxMs,retryMs,skipRetryMs});
    this.running=false;this.closed=false;
    const loaded=loadState(file,{now,validate:value=>Boolean(value.attempts)});
    this.state=loaded.value??{schema:1,attempts:{}};
    if(this.state.schema!==1)throw Error('Work review schema mismatch');
    if(loaded.recovery)this.state.recovery=loaded.recovery;
    for(const attempt of Object.values(this.state.attempts))if(['reviewing','summarizing'].includes(attempt.state)){attempt.state='interrupted';attempt.retryAt=now();}
  }
  close(){this.closed=true;}
  save(attempt) {
    this.state.attempts[attempt.id]=attempt;this.state.latest=attempt.id;
    const ids=Object.keys(this.state.attempts);
    for(const id of ids.slice(0,Math.max(0,ids.length-64)))delete this.state.attempts[id];
    atomicJson(this.file,this.state,{previous:true,pretty:false});return attempt;
  }
  view() {
    const a=this.state.attempts[this.state.latest];
    return a?{state:a.state,taskId:a.taskId,inputVersion:a.inputVersion,reason:a.reason??null,model:a.receipt?.model,checkedAt:a.checkedAt,retryAt:a.retryAt??null,
      ...(this.state.recovery?{recovery:this.state.recovery.reason}:{})}:{state:'not-needed',...(this.state.recovery?{recovery:this.state.recovery.reason}:{})};
  }
  /** Why no summary is due now, or null. */
  waiting(task,runtime) {
    // A declaration whose report is still unproven past the report wait goes back to Kin
    // as facts; the lock stays until the report is proven or she declares otherwise
    // (CR-LIFE-11, CR-MIND-02).
    if(task.completion?.outcome&&task.completion.state!=='historical-proposal'&&!this.router.declarationStalled?.(task,runtime))return 'kin-declared-outcome';
    if(task.status==='proposed')return 'proposal-not-taken-on';
    if(task.cancelRequested)return 'owner-cancel-pending';
    if(this.router.busy(runtime))return 'native-work-active-or-unknown';
    if(this.now()-lastActivity(task)<this.idleMs)return 'task-recently-active';
    return null;
  }
  async tick() {
    if(this.closed||this.running)return {state:'busy'};
    this.running=true;let attempt;
    try {
      const snapshot=await this.router.locked(async()=>{
        const task=this.router.tasks().filter(item=>item.requiresDelivery!==false).at(-1);
        if(!task)return null;
        const runtime=await this.router.inspect();
        return {task:copy(task),inputs:[...task.inputIds,...(task.contextInputIds??[])].map(id=>this.router.state.inputs[id]).filter(Boolean).map(copy),
          key:taskFingerprint(this.router,task),reason:this.waiting(task,runtime)};
      });
      if(!snapshot)return {state:'not-needed'};
      const id='work-summary-'+snapshot.key.slice(0,40),previous=this.state.attempts[id];
      if(snapshot.reason) {
        if(previous?.state==='waiting'&&previous.reason===snapshot.reason)return previous;
        if(previous&&previous.state!=='waiting')return previous;
        return this.save({id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,state:'waiting',reason:snapshot.reason,checkedAt:this.now()});
      }
      if(previous?.retryAt>this.now())return previous;
      const repeats=previous?.state==='told'?(previous.repeats??0)+1:(previous?.repeats??0);
      attempt={id,taskId:snapshot.task.id,inputVersion:snapshot.task.inputVersion,key:snapshot.key,checkedAt:this.now(),repeats,
        lane:REVIEWER_LANES.summarizeWork,purpose:REVIEWER_PURPOSES.summarizeWork};
      const evidence=await this.collect(snapshot);
      if(this.closed)return {state:'closed'};
      attempt=this.save({...attempt,state:'summarizing'});
      const result=await this.summarize(evidence.input);
      const receipt={model:result.receipt?.model??null,requestId:result.receipt?.requestId??null,verifiedAt:result.receipt?.verifiedAt??null};
      const recorded=await this.router.locked(async()=>{
        const task=this.router.state.tasks[snapshot.task.id];
        if(!openTask(task)||taskFingerprint(this.router,task)!==snapshot.key)return false;
        return this.router.recordWorkSummary(task.id,snapshot.key,{...result.summary,unsentDraftIds:evidence.facts.unsentDraftIds,undelivered:evidence.facts.undelivered,receipt});
      });
      if(!recorded)return this.save({...attempt,state:'superseded',reason:'task-changed-during-summary',retryAt:this.now()});
      let delivery=null;
      if(this.deliver&&!this.closed){try{delivery=await this.deliver(snapshot.task.id);}catch{delivery={state:'failed'};}}
      return this.save({...attempt,state:'told',receipt,delivery:delivery?.state??null,retryAt:this.now()+Math.min(this.repeatMaxMs,this.idleMs*2**Math.min(repeats+1,16))});
    } catch(error) {
      // A refused lane is not a failure and costs no summary: it comes back soon.
      if(error?.leaseSkipped)return attempt?this.save({...attempt,state:'waiting',reason:'work-summary-lane-unavailable',retryAt:this.now()+this.skipRetryMs}):{state:'waiting',reason:'work-summary-lane-unavailable'};
      return attempt?this.save({...attempt,state:'failed',reason:String(error?.message??error).slice(0,160),retryAt:this.now()+this.retryMs}):{state:'waiting',reason:'work-evidence-unavailable'};
    } finally {this.running=false;}
  }
}
