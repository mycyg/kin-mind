import {checkpointMarker} from './native-window.mjs';

/** Called inside the mobile dispatch coordinator. It never starts a model
 * turn, writes a user input, or retries an uncertain append. */
export class NativeContextDelivery {
  constructor({call,runtime,inject,find=checkpointMarker}) {Object.assign(this,{call,runtime,inject,find});}
  async reconcile(context,runtime) {
    const proof=await this.find(runtime.rolloutPath,context.marker,{textHash:context.text_hash,role:'assistant'});
    if(!proof.found)return {state:'unconfirmed',id:context.id};
    return this.call('context-delivery-ack',{session:context.session,epoch:context.epoch,id:context.id,
      actual_session:runtime.threadId,marker:context.marker,text_hash:context.text_hash,verified:true,native_at:proof.at});
  }
  async deliver(context) {
    if(!context?.id)return {state:'skipped'};
    const runtime=await this.runtime();
    if(!runtime.known||!runtime.rolloutPath||runtime.threadId!==context.session)throw Error('Context native boundary unverified');
    if(runtime.active)return {state:'deferred',reason:'native-turn-active',id:context.id};
    // An ended, failed turn can leave systemError until the next prompt. No
    // append was attempted; the optional background must not invent uncertainty.
    if(runtime.backgroundTasks||runtime.nativeStatus&&runtime.nativeStatus!=='idle')
      return {state:'deferred',reason:'native-context-unavailable',id:context.id};
    for(const pending of await this.call('context-delivery-pending',{session:context.session})){
      const receipt=await this.reconcile(pending,runtime);
      // Reconcile each original identity, without making one uncertain append
      // a global barrier to unrelated new context. Never replay that identity.
      if(pending.id===context.id)return receipt;
    }
    const operation=await this.call('context-delivery-begin',{session:context.session,epoch:context.epoch,id:context.id});
    if(operation.state!=='sending')return operation;
    // A recovered sending record may already be persisted in native history.
    const proof=await this.find(runtime.rolloutPath,operation.marker,{textHash:operation.text_hash,role:'assistant'});
    if(!proof.found){
      try {await this.inject({sessionId:context.session,operationId:context.id,
        items:[{type:'message',role:'assistant',content:[{type:'output_text',text:operation.text}]}]});}
      catch {await this.call('context-delivery-uncertain',{session:context.session,epoch:context.epoch,id:context.id});return {state:'unconfirmed',id:context.id};}
    }
    const receipt=await this.reconcile(operation,runtime);
    if(receipt.state!=='accepted')await this.call('context-delivery-uncertain',{session:context.session,epoch:context.epoch,id:context.id});
    return receipt;
  }
}
