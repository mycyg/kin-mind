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

/** What the host itself found in a reading by a fixed rule (`snapshot.detected`: `{code, summary,
 * evidence}`, a code of AUDIT_CODES) is a finding whatever the review answered, and the reading needs
 * attention. The review reads the same snapshot, but a model's reading of it is no rule: on 2026-09-28
 * it answered healthy with no codes while every proactive contact had failed for an hour. The host's
 * findings come first and keep their place within the eight a reading holds; the review's finding of
 * the same code gives way to the host's. */
export function withDetected(result,snapshot) {
  const detected=(Array.isArray(snapshot?.detected)?snapshot.detected:[]).filter(item=>AUDIT_CODES.includes(item?.code)&&item.code!=='other')
    .map(item=>({code:item.code,summary:String(item.summary??'').slice(0,600),evidence:String(item.evidence??'').slice(0,600)}));
  if(!detected.length)return result;
  const codes=new Set(detected.map(item=>item.code));
  return {...result,status:'needs_attention',findings:[...detected,...(result.findings??[]).filter(f=>!codes.has(f?.code))].slice(0,8)};
}

/** A failed reading is retried soon twice, then waits for its ordinary interval. */
export const AUDIT_FAILURE_RETRY_MINUTES=Object.freeze([5,15]);
/** A fault seen again within this long after it cleared is the same incident. */
export const INCIDENT_REOPEN_MS=24*3600000;
/** The schema of the audit's state as this code writes it: incidents Kin is told of, the repair
 * queue of the releases before 5fb4f85 kept as `retiredRepairs`. Schema 1 is that queue
 * (`repairs`), and a file of it this code reads is migrated, never written back as schema 1: the
 * migration used to happen in place under schema 1, and a release rolled back with its data kept
 * read `repairs` from it every second -- the router's pump failed ten thousand times (OPS-06). */
export const AUDIT_SCHEMA=2;
const plain=value=>Boolean(value)&&typeof value==='object'&&!Array.isArray(value);
/** A file's content: its value, `undefined` when there is none, or `{unreadable}`. */
function readState(file) {
  if(!file)return undefined;
  let text;
  try{text=fs.readFileSync(file,'utf8');}catch(error){if(error.code==='ENOENT')return undefined;return {unreadable:String(error.code??'read-failed')};}
  try{return JSON.parse(text);}catch{return {unreadable:'invalid-json'};}
}
/** Schema 1, either shape, as schema 2. */
function fromSchema1(state,at) {
  const next={...state,schema:AUDIT_SCHEMA,incidents:plain(state.incidents)?state.incidents:{}};
  // Repairs from before the findings became Kin's are history: kept as they were, and nothing runs them any more.
  if(next.repairs){next.retiredRepairs={at,repairs:next.repairs,lastIncident:next.lastIncident??null,lastFollowup:next.lastFollowup??null};
    delete next.repairs;delete next.lastIncident;delete next.lastFollowup;}
  return next;
}

/** Health readings never change the conversation provider and never start work in
 * the conversation. A reading that is not healthy is one incident, keyed by its
 * fault classes: the same classes seen again only refresh its evidence. The
 * findings are facts for Kin, who decides when and whether to look at them (N13). */
export class MobileAudit {
  /** `activity` is the router's gate for what starts beside the owner's conversation
   * (`beginActivity`); a host passes it, and every reading then passes it (CR3-FLOW-01).
   *
   * `file` holds the state in this code's schema. `legacyFile`, when a host names one, is where the
   * releases before kept it as schema 1: read once, while `file` is not there yet, and never written,
   * so a release rolled back with its data kept reads it as it left it (OPS-06). Without one, a
   * schema-1 `file` is migrated where it is.
   *
   * A state of a schema this code does not know -- a later release's, left by a rollback -- or one
   * nobody can read is refused, not thrown on and not overwritten: no reading runs, nothing is told,
   * `view` says why, and the file stays as it is for the release that wrote it. */
  constructor({file,legacyFile=null,collect,review,intervalHours=4,now=()=>Date.now(),lease=null,activity=null,
    lane=REVIEWER_LANES.audit,purpose=REVIEWER_PURPOSES.audit,skipRetryMs=5*60000}) {
    Object.assign(this,{file,collect,review,intervalHours,now,lease,activity,lane,purpose,skipRetryMs});this.running=false;this.refused=null;
    const fresh=()=>({schema:AUDIT_SCHEMA,nextAt:now(),history:[],incidents:{}});
    let found=readState(file),imported=null;
    if(found===undefined&&legacyFile){
      found=readState(legacyFile);
      // A legacy file this code cannot read is none of its own: it starts afresh and leaves that file alone.
      if(found!==undefined){imported=plain(found)&&found.schema===1?'schema-1':'left-alone';if(imported!=='schema-1')found=fresh();}
    }
    if(found===undefined)found=fresh();
    if(!plain(found)||found.unreadable||![1,AUDIT_SCHEMA].includes(found.schema)){
      this.refused={reason:plain(found)&&found.unreadable?'unreadable-audit-state':'unknown-audit-schema',
        schema:plain(found)&&['number','string'].includes(typeof found.schema)?found.schema:null};
      this.state={schema:this.refused.schema,nextAt:Infinity,history:[],incidents:{}};
      return;
    }
    this.state=found.schema===1?fromSchema1(found,now()):found;
    if(!plain(this.state.incidents))this.state.incidents={};
    if(!Array.isArray(this.state.history))this.state.history=[];
    if(imported)this.state.importedFrom={legacy:imported,at:now()};
    if(this.state.status==='running')this.state.status='interrupted';
    atomicJson(file,this.state);
  }
  /** A new interval, from when the last reading started; a refused state is left as it is. */
  reschedule(intervalHours) {
    this.intervalHours=intervalHours;
    if(this.refused)return false;
    this.state.nextAt=(this.state.startedAt??this.now())+intervalHours*3600000;
    atomicJson(this.file,this.state);
    return true;
  }
  async tick() {
    if(this.refused)return{state:'refused',reason:this.refused.reason};
    if(this.running||this.now()<this.state.nextAt)return{state:'not-due'};
    const id='audit-'+this.now();
    // A reading is a model call beside her conversation. Dispatch must not be frozen and
    // the lane must admit it, both before anything is collected, spent or counted; the
    // drain counts the reading until it has returned and let go of its lease. Refused, it
    // counts nothing and is due again at the next tick (CR3-FLOW-01, CR4-FLOW-01).
    const gate=this.activity?this.activity({kind:'health-review',id}):null;
    if(gate&&!gate.ok)return{state:'skipped',reason:'dispatch-'+gate.reason};
    this.running=true;
    // The health review is background work. At capacity, or with the ledger
    // unreachable, this run is skipped before anything is collected or spent; it
    // never waits for a slot, because waiting here would delay nothing useful.
    let held=null;
    try {held=this.lease?await this.lease.acquire({lane:this.lane,purpose:this.purpose}):null;}
    catch(error){this.running=false;gate?.release();throw error;}
    if(held&&!held.proceed){this.running=false;try {await held.release();} finally {gate?.release();}return{state:'skipped',lane:held.lane,reason:held.reason??held.state};}
    this.state.status='running';this.state.startedAt=this.now();this.state.nextAt=this.now()+this.intervalHours*3600000;
    try {atomicJson(this.file,this.state);}
    catch(error){this.running=false;try {await held?.release();} finally {gate?.release();}throw error;}
    let snapshot,stage='collect';
    try {
      snapshot=await this.collect();stage='review';const result=withDetected(await this.review(snapshot,{held}),snapshot);
      stage='record';
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
      // What the host found by its own rule does not wait for the review: it is an incident now, and
      // the reading is still a failed one, retried as before.
      const found=withDetected({status:'healthy',findings:[]},snapshot);
      if(found.findings.length)this.record(id,found,this.now());
      this.state.status='failed';this.state.failures=(this.state.failures??0)+1;
      const minutes=AUDIT_FAILURE_RETRY_MINUTES[this.state.failures-1]??this.intervalHours*60;
      this.state.nextAt=this.now()+minutes*60000;
      this.state.lastError={at:this.now(),stage,reason:error.message?.startsWith('deepseek-')?error.message:'audit-review-unavailable',receipt:error.receipt};return{state:'failed',nextAt:this.state.nextAt};}
    finally {this.running=false;try {await held?.release();} finally {gate?.release();}atomicJson(this.file,this.state);}
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
  /** The incidents as a list: none while the state is refused, or when it holds no incidents it can read. */
  incidentList() {return this.refused||!plain(this.state.incidents)?[]:Object.values(this.state.incidents).filter(plain);}
  /** Open incidents Kin has not been told about yet, as facts. */
  untold() {
    return this.incidentList().filter(i=>i.state==='open'&&!i.toldAt&&Array.isArray(i.findings))
      .map(i=>({id:i.id,codes:i.codes,findings:i.findings.map(f=>({code:f.code,summary:f.summary,evidence:f.evidence})),firstSeenAt:i.firstSeenAt,lastSeenAt:i.lastSeenAt}));
  }
  markTold(ids,at=this.now()) {
    if(this.refused)return false;
    let changed=false;
    for(const id of ids){const incident=this.state.incidents[id];if(incident&&!incident.toldAt){incident.toldAt=at;changed=true;}}
    if(changed)atomicJson(this.file,this.state);
    return changed;
  }
  view() {
    if(this.refused)return {status:'refused',reason:this.refused.reason,schema:this.refused.schema,incidents:[]};
    return {status:this.state.status??'not-run',nextAt:this.state.nextAt,lastSuccessAt:this.state.lastSuccessAt,failures:this.state.failures??0,
      incidents:this.incidentList().filter(i=>i.state==='open').map(i=>({id:i.id,codes:i.codes,seen:i.seen,firstSeenAt:i.firstSeenAt,lastSeenAt:i.lastSeenAt,told:Boolean(i.toldAt)}))};
  }
}
