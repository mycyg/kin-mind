import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {atomicJson} from './mobile-router.mjs';
import {REVIEWER_LANES,REVIEWER_PURPOSES,AUDIT_CODES} from './mobile-reviewer.mjs';

const digest=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');

export function appraisalProgress(operations, mind={}) {
  const running=operations?.queues?.find(q=>q.lane==='action'&&q.state==='running'&&q.count>0);
  if(running)return {state:'running',at:running.attempt_started_at??null,leaseExpiresUnix:running.lease_expires_unix??null};
  const success=operations?.action?.last_success,progress=mind.appraisalProgress;
  if(success&&(!progress?.checkedAt||Date.parse(progress.checkedAt)<=Date.parse(success)))return {state:'complete',at:success};
  if(progress?.checkedAt)return {state:progress.state,at:progress.checkedAt};
  return {state:'unknown',at:null,lastRecordedResult:mind.appraisal?.state??null};
}

/** A failed reading is retried soon twice, then waits for its ordinary interval. */
export const AUDIT_FAILURE_RETRY_MINUTES=Object.freeze([5,15]);
/** A fault seen again within this long after it cleared is the same incident. */
export const INCIDENT_REOPEN_MS=24*3600000;

/** Health readings never change the conversation provider and never start work in
 * the conversation. A reading that is not healthy is one incident, keyed by its
 * fault classes: the same classes seen again only refresh its evidence. The
 * findings are facts for Kin, who decides when and whether to look at them (N13). */
export class MobileAudit {
  /** `activity` is the router's gate for what starts beside the owner's conversation
   * (`beginActivity`); a host passes it, and every reading then passes it (CR3-FLOW-01). */
  constructor({file,collect,review,intervalHours=4,now=()=>Date.now(),lease=null,activity=null,
    lane=REVIEWER_LANES.audit,purpose=REVIEWER_PURPOSES.audit,skipRetryMs=5*60000}) {
    Object.assign(this,{file,collect,review,intervalHours,now,lease,activity,lane,purpose,skipRetryMs});this.running=false;
    this.state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{schema:1,nextAt:now(),history:[],incidents:{}};
    if(this.state.schema!==1)throw Error('Unknown audit schema');
    this.state.incidents??={};
    // Repairs from before the findings became Kin's are history: kept as they were,
    // and nothing runs them any more.
    if(this.state.repairs){this.state.retiredRepairs={at:now(),repairs:this.state.repairs,lastIncident:this.state.lastIncident??null,lastFollowup:this.state.lastFollowup??null};
      delete this.state.repairs;delete this.state.lastIncident;delete this.state.lastFollowup;}
    if(this.state.status==='running')this.state.status='interrupted';
    atomicJson(file,this.state);
  }
  async tick() {
    if(this.running||this.now()<this.state.nextAt)return{state:'not-due'};
    const id='audit-'+this.now();
    // A reading is a model call beside her conversation. Dispatch must not be frozen and
    // the lane must admit it, both before anything is collected, spent or counted; the
    // drain counts the reading until it has returned. Refused, it counts nothing and is
    // due again at the next tick (CR3-FLOW-01).
    const gate=this.activity?this.activity({kind:'health-review',id}):null;
    if(gate&&!gate.ok)return{state:'skipped',reason:'dispatch-'+gate.reason};
    this.running=true;
    // The health review is background work. At capacity, or with the ledger
    // unreachable, this run is skipped before anything is collected or spent; it
    // never waits for a slot, because waiting here would delay nothing useful.
    let held=null;
    try {held=this.lease?await this.lease.acquire({lane:this.lane,purpose:this.purpose}):null;}
    catch(error){this.running=false;gate?.release();throw error;}
    if(held&&!held.proceed){this.running=false;gate?.release();await held.release();return{state:'skipped',lane:held.lane,reason:held.reason??held.state};}
    this.state.status='running';this.state.startedAt=this.now();this.state.nextAt=this.now()+this.intervalHours*3600000;
    try {atomicJson(this.file,this.state);}
    catch(error){this.running=false;gate?.release();await held?.release();throw error;}
    try {
      const snapshot=await this.collect();const result=await this.review(snapshot,{held});
      const at=this.now();
      this.state.status=result.status;this.state.failures=0;this.state.lastSuccessAt=at;this.state.nextAt=at+this.intervalHours*3600000;
      this.state.lastReview={id,at,status:result.status,codes:[...new Set(result.findings.map(f=>f.code))].sort()};
      this.record(id,result,at);
      this.state.history.push({id,at,status:result.status});this.state.history=this.state.history.slice(-60);
      return{state:result.status};
    } catch(error) {
      // A lane that refused the run is not a failed review: nothing was spent, so it
      // costs no failure count and returns soon rather than in four hours.
      if(error?.leaseSkipped){this.state.status='skipped';this.state.nextAt=this.now()+this.skipRetryMs;return{state:'skipped',lane:this.lane,reason:error.lease?.leaseReason??error.lease?.leaseState??'model-lane-unavailable'};}
      this.state.status='failed';this.state.failures=(this.state.failures??0)+1;
      const minutes=AUDIT_FAILURE_RETRY_MINUTES[this.state.failures-1]??this.intervalHours*60;
      this.state.nextAt=this.now()+minutes*60000;
      this.state.lastError={at:this.now(),reason:error.message?.startsWith('deepseek-')?error.message:'audit-review-unavailable',receipt:error.receipt};return{state:'failed',nextAt:this.state.nextAt};}
    finally {this.running=false;gate?.release();await held?.release();atomicJson(this.file,this.state);}
  }
  record(id,result,at) {
    const incidents=Object.values(this.state.incidents);
    if(result.status==='healthy') {
      for(const incident of incidents)if(incident.state==='open'){incident.state='cleared';incident.clearedAt=at;}
      return null;
    }
    const codes=[...new Set(result.findings.map(f=>AUDIT_CODES.includes(f.code)?f.code:'other'))].sort(),fingerprint=digest(codes);
    const same=incidents.filter(i=>i.fingerprint===fingerprint).sort((a,b)=>b.lastSeenAt-a.lastSeenAt)[0];
    if(same&&(same.state==='open'||at-(same.clearedAt??0)<INCIDENT_REOPEN_MS)) {
      Object.assign(same,{state:'open',findings:result.findings,lastSeenAt:at,seen:(same.seen??1)+1});delete same.clearedAt;
      return same;
    }
    const incident={id,state:'open',fingerprint,codes,findings:result.findings,firstSeenAt:at,lastSeenAt:at,seen:1};
    this.state.incidents[id]=incident;
    const keep=Object.values(this.state.incidents).sort((a,b)=>b.lastSeenAt-a.lastSeenAt).slice(0,32).map(i=>i.id);
    for(const key of Object.keys(this.state.incidents))if(!keep.includes(key))delete this.state.incidents[key];
    return incident;
  }
  /** Open incidents Kin has not been told about yet, as facts. */
  untold() {
    return Object.values(this.state.incidents).filter(i=>i.state==='open'&&!i.toldAt)
      .map(i=>({id:i.id,codes:i.codes,findings:i.findings.map(f=>({code:f.code,summary:f.summary,evidence:f.evidence})),firstSeenAt:i.firstSeenAt,lastSeenAt:i.lastSeenAt}));
  }
  markTold(ids,at=this.now()) {
    let changed=false;
    for(const id of ids){const incident=this.state.incidents[id];if(incident&&!incident.toldAt){incident.toldAt=at;changed=true;}}
    if(changed)atomicJson(this.file,this.state);
    return changed;
  }
  view() {
    return {status:this.state.status??'not-run',nextAt:this.state.nextAt,lastSuccessAt:this.state.lastSuccessAt,failures:this.state.failures??0,
      incidents:Object.values(this.state.incidents).filter(i=>i.state==='open').map(i=>({id:i.id,codes:i.codes,seen:i.seen,firstSeenAt:i.firstSeenAt,lastSeenAt:i.lastSeenAt,told:Boolean(i.toldAt)}))};
  }
}
