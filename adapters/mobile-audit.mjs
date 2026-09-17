import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {atomicJson} from './mobile-router.mjs';
import {REVIEWER_LANES,REVIEWER_PURPOSES} from './mobile-reviewer.mjs';

export function appraisalProgress(operations, mind={}) {
  const running=operations?.queues?.find(q=>q.lane==='action'&&q.state==='running'&&q.count>0);
  if(running)return {state:'running',at:running.attempt_started_at??null,leaseExpiresUnix:running.lease_expires_unix??null};
  const success=operations?.action?.last_success,progress=mind.appraisalProgress;
  if(success&&(!progress?.checkedAt||Date.parse(progress.checkedAt)<=Date.parse(success)))return {state:'complete',at:success};
  if(progress?.checkedAt)return {state:progress.state,at:progress.checkedAt};
  return {state:'unknown',at:null,lastRecordedResult:mind.appraisal?.state??null};
}

/** Health reviews never change the conversation provider. Repair requests are
 * durable internal jobs which the shared conversation accepts when idle. */
export class MobileAudit {
  constructor({file,collect,review,intervalHours=4,now=()=>Date.now(),lease=null,
    lane=REVIEWER_LANES.audit,purpose=REVIEWER_PURPOSES.audit,skipRetryMs=5*60000}) {
    Object.assign(this,{file,collect,review,intervalHours,now,lease,lane,purpose,skipRetryMs});this.running=false;
    this.state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{schema:1,nextAt:now(),history:[],repairs:{}};
    if(this.state.schema!==1)throw Error('Unknown audit schema');
    if(this.state.status==='running') {this.state.status='interrupted';atomicJson(file,this.state);}
  }
  async tick() {
    if(this.running||this.now()<this.state.nextAt)return{state:'not-due'};
    // The health review is background work. At capacity, or with the ledger
    // unreachable, this run is skipped before anything is collected or spent; it
    // never waits for a slot, because waiting here would delay nothing useful.
    const held=this.lease?await this.lease.acquire({lane:this.lane,purpose:this.purpose}):null;
    if(held&&!held.proceed){await held.release();return{state:'skipped',lane:held.lane,reason:held.reason??held.state};}
    this.running=true;
    const id='audit-'+this.now();
    this.state.status='running';this.state.startedAt=this.now();this.state.nextAt=this.now()+this.intervalHours*3600000;
    atomicJson(this.file,this.state);
    try {
      const snapshot=await this.collect();const result=await this.review(snapshot,{held});
      this.state.status=result.status;this.state.failures=0;this.state.lastSuccessAt=this.now();this.state.nextAt=this.now()+this.intervalHours*3600000;this.state.lastReview={id,at:this.now(),snapshot,result};
      if(result.status==='healthy')this.state.lastIncident=null;
      else {
        const fingerprint=createHash('sha256').update(JSON.stringify(result.findings.map(f=>f.code).sort())).digest('hex');
        if(fingerprint!==this.state.lastIncident) {
          this.state.repairs[id]={id,state:'pending',fingerprint,result,createdAt:this.now()};
          this.state.lastIncident=fingerprint;
        }
      }
      this.state.history.push({id,at:this.now(),status:result.status});this.state.history=this.state.history.slice(-60);
      return{state:result.status};
    } catch(error) {
      // A lane that refused the run is not a failed review: nothing was spent, so it
      // costs no failure count and returns soon rather than in four hours.
      if(error?.leaseSkipped){this.state.status='skipped';this.state.nextAt=this.now()+this.skipRetryMs;return{state:'skipped',lane:this.lane,reason:error.lease?.leaseReason??error.lease?.leaseState??'model-lane-unavailable'};}
      this.state.status='failed';this.state.failures=(this.state.failures??0)+1;this.state.nextAt=this.now()+(this.state.failures===1?5:15)*60000;this.state.lastError={at:this.now(),reason:error.message?.startsWith('deepseek-')?error.message:'audit-review-unavailable',receipt:error.receipt};return{state:'failed',nextAt:this.state.nextAt};}
    finally {this.running=false;await held?.release();atomicJson(this.file,this.state);}
  }
  pending() {return Object.values(this.state.repairs).filter(r=>r.state==='pending');}
  settle(id,state,receipt) {
    if(!this.state.repairs[id])throw Error('Unknown audit repair');
    if(!['accepted','unconfirmed','resolved','needs-attention'].includes(state))throw Error('Invalid repair state');
    Object.assign(this.state.repairs[id],{state,receipt,updatedAt:this.now()});
    if(this.state.lastReview?.id===id&&['resolved','needs-attention'].includes(state)) {
      this.state.status=state==='resolved'?'healthy':'needs_attention';
      this.state.lastFollowup={id,state,at:this.now(),receipt};
    }
    atomicJson(this.file,this.state);
  }
}
