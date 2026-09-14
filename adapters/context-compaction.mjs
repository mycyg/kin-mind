import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {atomicJson} from './mobile-router.mjs';

export class ContextCompaction {
  constructor({file,router,inspect,compact,reconcile,ack,sessionId}) {
    Object.assign(this,{file,router,inspect,compact,reconcile,ack,sessionId});
    this.state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};
    if(this.state.state==='running'){this.state.state='unconfirmed';atomicJson(file,this.state);}
  }
  async run(epoch) {
    return this.router.locked(async()=>{
      if(!this.state.external&&this.state.epoch===epoch&&this.state.state==='complete')return this.state;
      if(['running','unconfirmed'].includes(this.state.state)) {
        const receipt=await this.reconcile?.(this.state.operationId);
        const actual=receipt?.completed?await this.inspect():null;
        const sameOwner=!this.state.ownerModel||actual?.model===this.state.ownerModel;
        const sameTasks=!this.state.taskSnapshot||JSON.stringify(this.router.tasks())===this.state.taskSnapshot;
        if(receipt?.completed&&receipt.actual_session===this.sessionId&&actual?.known&&actual.sessionId===this.sessionId&&sameOwner&&sameTasks) {
          const result=await this.ack({session:this.sessionId,epoch:this.state.operationId,actual_session:this.sessionId,completed:true});
          this.state={...this.state,state:'complete',receipt,result};atomicJson(this.file,this.state);return this.state;
        }
        return {state:'waiting',reason:'compaction-receipt-unconfirmed'};
      }
      const before=await this.inspect();
      const tasks=this.router.tasks();
      const toolsRunning=tasks.some(t=>Object.values(t.tools??{}).some(tool=>!['completed','failed'].includes(tool.status)));
      if(this.router.busy(before)||toolsRunning)return {state:'waiting',reason:'owner-work'};
      const taskSnapshot=JSON.stringify(tasks);
      const operationId='memory-compact:'+createHash('sha256').update(epoch).digest('hex').slice(0,32);
      this.state={state:'running',epoch,operationId,sessionId:this.sessionId,ownerModel:before.model,taskSnapshot};atomicJson(this.file,this.state);
      try {
        const receipt=await this.compact(operationId);
        const after=await this.inspect();
        if(!receipt?.completed||receipt.actual_session!==this.sessionId||!after.known||after.sessionId!==this.sessionId||after.model!==before.model||JSON.stringify(this.router.tasks())!==taskSnapshot)throw Error('Compaction receipt mismatch');
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
