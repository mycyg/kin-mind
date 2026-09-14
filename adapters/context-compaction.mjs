import fs from 'node:fs';
import {atomicJson} from './mobile-router.mjs';

export class ContextCompaction {
  constructor({file,router,inspect,compact,reconcile,ack,sessionId}) {
    Object.assign(this,{file,router,inspect,compact,reconcile,ack,sessionId});
    this.state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};
    if(this.state.state==='running'){this.state.state='unconfirmed';atomicJson(file,this.state);}
  }
  async run(epoch) {
    return this.router.locked(async()=>{
      if(this.state.epoch===epoch&&this.state.state==='complete')return this.state;
      if(['running','unconfirmed'].includes(this.state.state)) {
        const receipt=await this.reconcile?.(this.state.operationId);
        const actual=receipt?.completed?await this.inspect():null;
        if(receipt?.completed&&receipt.actual_session===this.sessionId&&actual?.known&&actual.sessionId===this.sessionId) {
          const result=await this.ack({session:this.sessionId,epoch:this.state.operationId,actual_session:this.sessionId,completed:true});
          this.state={...this.state,state:'complete',receipt,result};atomicJson(this.file,this.state);return this.state;
        }
        return {state:'waiting',reason:'compaction-receipt-unconfirmed'};
      }
      const before=await this.inspect();
      if(this.router.busy(before)||this.router.tasks().length)return {state:'waiting',reason:'owner-work'};
      const operationId='memory-compact:'+epoch;
      this.state={state:'running',epoch,operationId,sessionId:this.sessionId};atomicJson(this.file,this.state);
      try {
        const receipt=await this.compact(operationId);
        const after=await this.inspect();
        if(!receipt?.completed||receipt.actual_session!==this.sessionId||!after.known||after.sessionId!==this.sessionId||after.model!==before.model)throw Error('Compaction receipt mismatch');
        const result=await this.ack({session:this.sessionId,epoch:operationId,actual_session:receipt.actual_session,completed:true});
        this.state={...this.state,state:'complete',receipt,result};atomicJson(this.file,this.state);return this.state;
      } catch(error) {
        this.state={...this.state,state:'unconfirmed',reason:error.name};atomicJson(this.file,this.state);return this.state;
      }
    });
  }
  externalReceipt(epoch) {
    this.state={state:'complete',epoch,sessionId:this.sessionId,external:true};atomicJson(this.file,this.state);
  }
}
