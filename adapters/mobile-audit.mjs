import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {atomicJson} from './mobile-router.mjs';

/** Health reviews never change the conversation provider. Repair requests are
 * durable internal jobs which the shared conversation accepts when idle. */
export class MobileAudit {
  constructor({file,collect,review,intervalHours=4,now=()=>Date.now()}) {
    Object.assign(this,{file,collect,review,intervalHours,now});this.running=false;
    this.state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{schema:1,nextAt:now(),history:[],repairs:{}};
    if(this.state.schema!==1)throw Error('Unknown audit schema');
    if(this.state.status==='running') {this.state.status='interrupted';atomicJson(file,this.state);}
  }
  async tick() {
    if(this.running||this.now()<this.state.nextAt)return{state:'not-due'};
    this.running=true;
    const id='audit-'+this.now();
    this.state.status='running';this.state.startedAt=this.now();this.state.nextAt=this.now()+this.intervalHours*3600000;
    atomicJson(this.file,this.state);
    try {
      const snapshot=await this.collect();const result=await this.review(snapshot);
      this.state.status=result.status;this.state.lastReview={id,at:this.now(),snapshot,result};
      if(result.status==='healthy')this.state.lastIncident=null;
      else {
        const fingerprint=createHash('sha256').update(JSON.stringify(result.findings.map(f=>[f.code,f.evidence]).sort())).digest('hex');
        if(fingerprint!==this.state.lastIncident) {
          this.state.repairs[id]={id,state:'pending',fingerprint,result,createdAt:this.now()};
          this.state.lastIncident=fingerprint;
        }
      }
      this.state.history.push({id,at:this.now(),status:result.status});this.state.history=this.state.history.slice(-60);
      return{state:result.status};
    } catch {this.state.status='failed';this.state.lastError={at:this.now(),reason:'audit-review-unavailable'};return{state:'failed'};}
    finally {this.running=false;atomicJson(this.file,this.state);}
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
