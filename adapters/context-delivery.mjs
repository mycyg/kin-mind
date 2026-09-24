import {checkpointMarker,nativeWindowFor,reconcileLegacyInjections} from './native-window.mjs';

/** How far before the receipt index the one-time legacy pass reads (CR-LIFE-09):
 * explicit and finite. An identity it does not find stays unknown. */
export const LEGACY_SCAN_BYTES=512*1024*1024;
/** One page of unsettled identities, as the memory store hands them out. */
export const PENDING_PAGE=16;
/** Pages one legacy run reads; a longer list goes on from its cursor next time. */
const LEGACY_PAGES=64;

/** Called inside the mobile dispatch coordinator. It never starts a model
 * turn, writes a user input, or retries an uncertain append. Receipts come from
 * the incremental reader of the native history (`window(runtime)`, else the one
 * this process keeps for the rollout); nothing here reads the history from its
 * first byte on the owner's path. */
export class NativeContextDelivery {
  constructor({call,runtime,inject,find=null,window=null,background=work=>{void work.catch(()=>{});},legacyScanBytes=LEGACY_SCAN_BYTES}) {
    Object.assign(this,{call,runtime,inject,find,window,background,legacyScanBytes});
  }
  reader(runtime){return this.find?null:(this.window?.(runtime)??nativeWindowFor(runtime.rolloutPath));}
  async lookup(runtime,context) {
    const window=this.reader(runtime);
    if(!window)return (this.find??checkpointMarker)(runtime.rolloutPath,context.marker,{textHash:context.text_hash,role:'assistant'});
    await window.poll();
    const hit=window.receipts.find({marker:context.marker,textHash:context.text_hash,role:'assistant'});
    return hit?{found:true,at:hit.at,offset:hit.o}:{found:false,state:'unconfirmed'};
  }
  async acknowledge(context,runtime,proof) {
    return this.call('context-delivery-ack',{session:context.session,epoch:context.epoch,id:context.id,
      actual_session:runtime.threadId,marker:context.marker,text_hash:context.text_hash,verified:true,native_at:proof.at});
  }
  async reconcile(context,runtime) {
    const proof=await this.lookup(runtime,context);
    if(!proof.found)return {state:'unconfirmed',id:context.id};
    return this.acknowledge(context,runtime,proof);
  }
  /** The pass over history written before receipts were indexed, for every identity
   * still unsettled in this window (CR-LIFE-09): read page by page in id order, never
   * only the first page, searched within `legacyScanBytes`, and recorded per identity.
   * The window counts as migrated only once the last page was read. It runs beside
   * the owner's path. */
  migrate(runtime) {
    const window=this.reader(runtime);
    if(!window||window.migrated()||this.migrating)return this.migrating??null;
    this.migrating=(async()=>{
      const legacy=window.state.receipts.legacy??{},contexts=[];
      let after=typeof legacy.after==='string'?legacy.after:'',complete=false;
      for(let page=0;page<LEGACY_PAGES;page++) {
        const rows=await this.call('context-delivery-pending',{session:runtime.threadId,after,limit:PENDING_PAGE});
        const list=Array.isArray(rows)?rows.filter(row=>typeof row?.id==='string'&&row.id>after):[];
        contexts.push(...list);
        if(list.length<PENDING_PAGE){complete=true;break;}
        after=list.at(-1).id;
      }
      const result=await reconcileLegacyInjections(window,contexts.map(c=>c.marker),{maxBytes:this.legacyScanBytes,complete});
      if(!complete){window.state.receipts.legacy.after=after;window.save();}
      for(const context of contexts) {
        const hit=window.receipts.find({marker:context.marker,textHash:context.text_hash,role:'assistant'});
        if(hit)try{await this.acknowledge(context,runtime,{at:hit.at});}catch{/* The next delivery reconciles it again. */}
      }
      return result;
    })().finally(()=>{this.migrating=null;});
    return this.migrating;
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
    const pending=await this.call('context-delivery-pending',{session:context.session});
    const window=this.reader(runtime);
    if(window&&!window.migrated())this.background(this.migrate(runtime)??Promise.resolve());
    for(const prior of pending){
      const receipt=await this.reconcile(prior,runtime);
      // Reconcile each original identity, without making one uncertain append
      // a global barrier to unrelated new context. Never replay that identity.
      if(prior.id===context.id)return receipt;
    }
    const operation=await this.call('context-delivery-begin',{session:context.session,epoch:context.epoch,id:context.id});
    if(operation.state!=='sending')return operation;
    // A recovered sending record may already be persisted in native history.
    const proof=await this.lookup(runtime,operation);
    if(!proof.found){
      // CR-LIFE-09: only a send right taken by this very call may append. A record that
      // was already sending (a restart, or more unsettled than one pending page holds)
      // is settled by its proof alone: without one it stays unknown, never appended again.
      if(operation.acquired!==true){await this.call('context-delivery-uncertain',{session:context.session,epoch:context.epoch,id:context.id});return {state:'unconfirmed',id:context.id};}
      try {await this.inject({sessionId:context.session,operationId:context.id,
        items:[{type:'message',role:'assistant',content:[{type:'output_text',text:operation.text}]}]});}
      catch {await this.call('context-delivery-uncertain',{session:context.session,epoch:context.epoch,id:context.id});return {state:'unconfirmed',id:context.id};}
    }
    const receipt=await this.reconcile(operation,runtime);
    if(receipt.state!=='accepted')await this.call('context-delivery-uncertain',{session:context.session,epoch:context.epoch,id:context.id});
    return receipt;
  }
}
