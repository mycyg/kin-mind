import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {TransportManifests,manifestView} from './transport-manifest.mjs';
import {ReplyTail} from './reply-tail.mjs';
import {writeJsonAtomic} from './atomic-json.mjs';
import {readThroughArchive} from './state-pruner.mjs';

// Tail bookkeeping may have to wait for the memory host. A reply never waits for it longer than this; the work goes on behind it.
const within=(work,ms)=>{let timer;return Promise.race([work,new Promise(resolve=>{timer=setTimeout(resolve,ms);timer.unref?.();})]).finally(()=>clearTimeout(timer));};
// A preflight that is not ready is asked again later, a bounded number of times. One answer can never change by
// asking again; with tail decisions on, a frozen remainder that has to be rewritten is a decision, not a wait.
const REVIEW_ROUTES={'reply-exceeds-review-capacity':'undeliverable'};

/** Outbox entries are local evidence, never an instruction or model identity. */
export function outboxEvidence(directories) {
  const values=[];
  for(const directory of directories) {
    if(!fs.existsSync(directory))continue;
    for(const name of fs.readdirSync(directory).filter(n=>n.endsWith('.json'))) {
      try {
        const value=JSON.parse(fs.readFileSync(path.join(directory,name),'utf8'));
        if(!value.id||!value.state)continue;
        values.push({id:value.id,text:value.text??'',state:value.state,references:value.references??[],
          draft_id:value.draftId,message_id:value.messageId,at:value.acceptedAt??value.checkedAt??value.attemptedAt,
          ...(typeof value.submissionStarted==='boolean'?{submission_started:value.submissionStarted}:{})});
      } catch { /* Incomplete atomic replacements are retried from the journal. */ }
    }
  }
  return values.sort((a,b)=>(b.at??'').localeCompare(a.at??''));
}

/** Review decisions plus durable delivery of reply groups. With
 * `transportManifest` (the host's `transport_manifest` switch, on unless the
 * caller injects false) groups live in the transport manifest: reviewed once,
 * frozen, cut into fragments, reconciled by receipt. Switched off, every method
 * below behaves exactly as it did before the manifest existed.
 * With `replyTailDecision` (the host's `reply_tail_decision` switch, on unless
 * the caller injects false) a new owner message interrupts a group instead of
 * cancelling it, and `this.tail` decides what becomes of the unsent rest.
 * Switched off, an interrupted group is handled as the manifest alone does. */
export class ReplyGuard {
  constructor({call,directory,outbox=()=>[],clock=()=>Date.now(),wholeReplyReview=false,onOutcome=()=>{},transportManifest=true,
    manifestDirectory,channel='feishu',contracts,receipt,emit,lease,role,hooks,sleep,retry,archivedState={},
    replyTailDecision=true,decideTail=null,ownerEpoch=null,onTail=null,onWake=()=>{},tailLimits,tailHooks,tailWaitMs=5000}) {
    Object.assign(this,{call,directory,outbox,clock,wholeReplyReview,onOutcome,channel,tailWaitMs,archivedState});this.active=new Map();
    if(transportManifest)this.manifests=new TransportManifests({directory:manifestDirectory??path.join(directory,'reply-manifests'),clock,contracts,emit,lease,role,hooks,sleep,retry,
      // The host may hand over a direct reader of `<outbox>/<transport id>.json`; outbox evidence is the fallback.
      receipt:receipt??(async id=>(await this.outbox()).find(r=>r.id===id)??null),
      cancelShare:draftId=>this.call('share-cancel',{draft_id:draftId}),
      review:(requests,context)=>this.reviewGroup(requests,context),
      onOutcome:detail=>this.onOutcome(detail)});
    this.tail=this.manifests&&replyTailDecision?new ReplyTail({manifests:this.manifests,clock,decide:decideTail,ownerEpoch,onWake,hooks:tailHooks,limits:tailLimits,
      onEvent:detail=>(onTail??this.onOutcome)(detail)}):null;
  }
  check(request) {
    const key=createHash('sha256').update(JSON.stringify(request)).digest('hex');
    if(this.active.has(key))return this.active.get(key);
    const run=this.inspect(request,key).finally(()=>this.active.delete(key));this.active.set(key,run);return run;
  }
  /** A decision that was moved to the archive is still a decision. The lookup
   * that misses looks there before concluding that nothing was ever decided —
   * concluding that is how the owner hears the same thing a second time. */
  prior(file){return readThroughArchive(file,this.archivedState).value;}
  async inspect(request,key) {
    const file=path.join(this.directory,key+'.json');fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    const old=this.prior(file);
    if(old?.state==='silent'||old?.state==='merged')return old;
    if(old?.state==='pending'&&old.retryAt>this.clock())return old;
    let result;
    try {
      if(!request.work&&request.reply_id) {
        const choice=await this.call('reply-status',{input_id:request.reply_id});
        if(['silent','merged'].includes(choice.action))return this.save(file,{state:choice.action,choice});
      }
      result=await this.call('share-preflight',{...request,outbox:await this.outbox(),allow_model:true});
    } catch {result={state:'pending',reason:'share-review-unavailable',transient:true};}
    return this.save(file,{...result,...(result.state==='pending'?{retryAfterMs:60000,retryAt:this.clock()+60000}:{})});
  }
  // A share decision is what stops the same thing being said to the owner twice.
  // It is written through the shared writer: a unique temporary name, so two
  // checks at once never share one, and the bytes on the disk before the rename.
  save(file,value){writeJsonAtomic(file,value,{previous:false});return value;}
  async checkGroup(requests,{frozen=false,remainder=null,sent=[]}={}) {
    if(this.wholeReplyReview){
      const key='group-'+createHash('sha256').update(JSON.stringify(remainder?[requests,frozen,remainder.items.map(item=>item.id)]:[requests,frozen])).digest('hex');
      if(this.active.has(key))return this.active.get(key);
      const run=this.inspectGroup(requests,frozen,{remainder,sent}).finally(()=>this.active.delete(key));
      this.active.set(key,run);return run;
    }
    const checked=[];
    for(const request of requests) {
      const result=await this.check({...request,batch_text:requests.map(r=>r.text).join('\n\n')});
      if(result.state!=='ready')return result;
      checked.push(result);
    }
    return {state:'ready',checked};
  }
  /** `sent` is what the manifest itself knows this group already handed to a transport: it is what
   * makes "the unsent remainder only" true for the review, whatever the outbox scan happens to see.
   * `remainder` is an older group's unsent tail, for the review to report coverage of. Without
   * either, the request is exactly what it was. */
  async inspectGroup(entries,frozen,{remainder=null,sent=[]}={}) {
    try {
      for(const inputId of new Set(entries.filter(e=>!e.work&&e.reply_id).map(e=>e.reply_id))) {
        const choice=await this.call('reply-status',{input_id:inputId});
        if(['silent','merged'].includes(choice.action))return {state:choice.action,choice};
      }
      const known=new Set(sent.map(row=>row.draft_id)),outbox=sent.length?[...sent,...(await this.outbox()).filter(row=>!known.has(row.draft_id))]:await this.outbox();
      const request={entries,frozen,outbox,allow_model:!frozen,...(remainder?{remainder}:{})};
      let result=await this.call('share-preflight-group',request);
      // A frozen group whose evidence moved is judged again, its unsent remainder only, and that takes a model.
      if(frozen&&result.state==='pending'&&result.reason==='frozen-remainder-review-required')result=await this.call('share-preflight-group',{...request,allow_model:true});
      if(result.state==='ready')return result;
      const route=REVIEW_ROUTES[result.reason]??(this.tail&&result.reason==='frozen-remainder-needs-new-group'?'interrupt':null);
      // A semantic hold cannot stand in for a voluntary silence receipt.
      return {...result,state:'pending',retryAt:this.clock()+60000,...(route?{route}:{})};
    }catch{return {state:'pending',reason:'whole-reply-review-unavailable',retryAt:this.clock()+60000,transient:true};}      // nothing was judged: asked again, never parked
  }
  /** The review the manifest asks for. Whole-reply mode validates the entire
   * group (`frozen` once a final review is stored); bubble-by-bubble mode only
   * ever looks at bubbles that have not begun. */
  async reviewGroup(requests,{frozen=false,pending=requests.map((_,i)=>i),groupId=null,sent=[]}={}) {
    if(this.wholeReplyReview)return this.checkGroup(requests,{frozen,sent,remainder:!frozen&&groupId?this.tail?.remainderFor(groupId)??null:null});
    const result=await this.checkGroup(pending.map(i=>requests[i]));
    if(result.state!=='ready')return result;
    const checked=requests.map(()=>null);pending.forEach((index,position)=>{checked[index]=result.checked[position];});
    return {...result,checked};
  }
  /** A second entrant gets `busy` and has changed nothing. */
  report(result,groupId) {
    if(result.busy||result.lost||!result.manifest)return {state:result.busy?'busy':result.lost?'lease-lost':'missing',groupId,entries:[]};
    return manifestView(result.manifest);
  }
  /** Draft on disk first, then the whole-group review, then the final manifest,
   * then the send: the order a complete reply needs. When nothing could be sent
   * yet the result says why: `state` pending (with `reason`), silent or merged
   * (with `choice`), exactly what `checkGroup` would have told the caller. */
  async replyGroup(entries,ownerEpoch,{guard,send,channel=this.channel}={}) {
    if(!this.manifests)return this.deliverGroup(entries,ownerEpoch,await this.checkGroup(entries.map(e=>e.request)),{guard,send});
    return this.deliver(entries,ownerEpoch,{guard,send,channel});
  }
  /** The group's draft. From its very first write it names the older groups whose unsent remainder it answers for. */
  draft(entries,ownerEpoch,channel,hold=null) {
    const continues=this.tail?.continuesFor(entries)??[];
    return this.manifests.createDraft({entries,ownerEpoch,channel,hold,...(continues.length?{continues}:{})});
  }
  async deliver(entries,ownerEpoch,{guard,send,channel,initialReview=null}) {
    const draft=this.draft(entries,ownerEpoch,channel);
    if(!this.tail)return this.report(await this.manifests.run(draft.group_id,{guard,transport:send,initialReview}),draft.group_id);
    // Tail bookkeeping never decides whether this reply goes out, and never holds it up for long: whatever
    // fails or is still running here is rolled forward behind it and by the next pass.
    await within(this.tail.linkNew(draft).catch(()=>{}),this.tailWaitMs);
    const result=await this.manifests.run(draft.group_id,{guard:this.tail.guard(guard),transport:send,initialReview});
    await within(this.tail.after(result).catch(()=>{}),this.tailWaitMs);
    const fresh=result.manifest&&!result.busy&&!result.lost?this.manifests.read(draft.group_id):null;
    return this.report(fresh?{...result,manifest:fresh}:result,draft.group_id);
  }
  async deliverGroup(entries,ownerEpoch,review,{guard,send,channel=this.channel}) {
    if(this.manifests)return this.deliver(entries,ownerEpoch,{guard,send,channel,initialReview:review});
    const saved=this.deferGroup(entries,ownerEpoch);
    const file=path.join(this.directory,createHash('sha256').update(entries[0].delivery.id).digest('hex')+'.pending.json');
    if(saved.state==='accepted')return saved;
    let value=saved;
    if(!saved.review&&review?.state==='ready'){
      if(review.checked?.length!==entries.length)throw Error('Whole reply review is incomplete');
      value={...saved,wholeReply:true,state:'prepared',review,entries:saved.entries.map((entry,i)=>({...entry,
        delivery:{...entry.delivery,text:review.checked[i].text??entry.request.text,references:review.checked[i].references??[]}}))};
      this.save(file,value);
    }
    await this.resumeWholeGroup(file,value,{guard,send});
    return JSON.parse(fs.readFileSync(file,'utf8'));
  }
  async resumeWholeGroup(file,entry,{guard,send}) {
    if(entry.state==='accepted')return;
    const save=value=>{entry=this.save(file,value);this.onOutcome({state:entry.state,inputId:entry.request.reply_id,reason:entry.reason});};
    for(const item of entry.entries.filter(e=>e.state==='unconfirmed')) {
      const receipt=(await this.outbox()).find(r=>r.id===item.delivery.id);
      if(receipt?.state!=='accepted'||!receipt.message_id)return;
      Object.assign(item,{state:'accepted',receipt});save({...entry,state:'prepared'});
    }
    if(entry.entries.every(item=>item.state==='accepted')){save({...entry,state:'accepted',reason:null});return;}
    const permission=await guard(entry);
    if(permission==='wait')return;
    if(permission==='cancel'){
      for(const item of entry.entries.filter(e=>e.state==='unsent'))await this.call('share-cancel',{draft_id:item.request.draft_id});
      save({...entry,state:'canceled',reason:'input-or-session-superseded'});return;
    }
    // Once transport starts, keep the whole reviewed text immutable. Only
    // validate source/configuration versions; never rewrite an unsent suffix.
    const review=await this.checkGroup(entry.entries.map(e=>e.request),{frozen:!!entry.review});
    if(['silent','merged'].includes(review.state)){
      for(const item of entry.entries.filter(e=>e.state==='unsent'))await this.call('share-cancel',{draft_id:item.request.draft_id});
      save({...entry,state:review.state,choice:review.choice});return;
    }
    if(review.state!=='ready'){
      save({...entry,state:'pending',reason:review.reason,retryAt:this.clock()+60000});return;
    }
    if(!entry.review){
      if(review.checked?.length!==entry.entries.length)throw Error('Whole reply review is incomplete');
      entry.entries.forEach((item,i)=>{item.delivery={...item.delivery,text:review.checked[i].text??item.request.text,references:review.checked[i].references??[]};});
      save({...entry,state:'prepared',review});
    }
    for(const item of entry.entries.filter(e=>e.state==='unsent')){
      if(await guard(entry)!=='send')return;
      item.state='unconfirmed';save({...entry,state:'unconfirmed'});
      try{
        const receipt=await send(item.delivery);item.receipt=receipt;
        if(receipt.state!=='accepted'||!receipt.messageId){save({...entry,state:'unconfirmed'});return;}
        item.state='accepted';save({...entry,state:'prepared'});
      }catch{save({...entry,state:'unconfirmed'});return;}
    }
    save({...entry,state:'accepted',reason:null});
  }
  deferGroup(entries,ownerEpoch) {
    const first=entries[0];
    if(!first)return;
    if(this.manifests)return this.park(entries,ownerEpoch,60000);
    const saved=this.defer(first.request,first.delivery,ownerEpoch);
    const file=path.join(this.directory,createHash('sha256').update(first.delivery.id).digest('hex')+'.pending.json');
    if(saved.entries)return saved;
    return this.save(file,{...saved,wholeReply:this.wholeReplyReview,entries:entries.map(e=>({...e,state:'unsent'})),retryAt:this.clock()+60000});
  }
  /** Manifest mode: a deferred group is a `held` manifest; an existing one is returned as it is. */
  park(entries,ownerEpoch,delay) {
    const draft=this.draft(entries,ownerEpoch,this.channel,{reason:'share-review-pending',retryAt:this.clock()+delay});
    if(draft.continues?.length)void this.tail?.linkNew(draft).catch(()=>{});
    return manifestView(draft);
  }
  defer(request,delivery,ownerEpoch) {
    if(this.manifests)return this.park([{request,delivery}],ownerEpoch,20*60000);
    fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    const file=path.join(this.directory,createHash('sha256').update(delivery.id).digest('hex')+'.pending.json');
    const prior=this.prior(file);
    if(prior)return prior;
    return this.save(file,{state:'pending',request,delivery,ownerEpoch,at:this.clock(),retryAt:this.clock()+20*60000});
  }
  async resumeDue({guard,send,limit=2}) {
    // The old journal's directory is only the old journal's: a host that never deferred a
    // bubble has none, and its manifests are resumed all the same.
    if(this.resuming||(!this.manifests&&!fs.existsSync(this.directory)))return {state:'idle'};
    this.resuming=true;let handled=0;
    try {
      if(this.manifests) {
        // Unfinished groups of the old journal come along once; finished ones never do.
        const legacy=this.manifests.importLegacy(this.directory);
        if(!this.tail)return {...await this.manifests.resumeDue({guard,transport:send,limit}),imported:legacy.imported.length};
        // Unsettled tail intents are rolled forward first; after the sends, settle what they delivered and,
        // for a remainder no call could carry a decision for, ask on its own (bounded).
        await this.tail.recover().catch(()=>{});
        const resumed=await this.manifests.resumeDue({guard:this.tail.guard(guard),transport:send,limit});
        const tail=await this.tail.tick({guard}).catch(()=>({state:'failed'}));
        return {...resumed,imported:legacy.imported.length,tail};
      }
      const files=fs.readdirSync(this.directory).filter(n=>n.endsWith('.pending.json'));
      for(const name of files) {
        const file=path.join(this.directory,name);let entry=JSON.parse(fs.readFileSync(file,'utf8'));
        if(!['pending','prepared',...(entry.entries?['unconfirmed']:[])].includes(entry.state)||entry.retryAt>this.clock())continue;
        if(handled++>=limit)break;
        if(entry.entries){await (entry.wholeReply?this.resumeWholeGroup(file,entry,{guard,send}):this.resumeGroup(file,entry,{guard,send}));continue;}
        let permission=await guard(entry);
        if(permission==='wait')continue;
        if(permission==='cancel'){await this.call('share-cancel',{draft_id:entry.request.draft_id});this.save(file,{...entry,state:'canceled'});continue;}
        const checked=entry.state==='prepared'?entry.checked:await this.check(entry.request);
        permission=await guard(entry);
        if(permission!=='send')continue;
        if(checked.state==='pending'){this.save(file,{...entry,retryAt:this.clock()+20*60000});continue;}
        if(checked.state!=='ready'){this.save(file,{...entry,state:checked.state});continue;}
        // Freeze body and references before transport. A crash resumes the
        // same platform ID; an uncertain receipt never creates a new send.
        entry=this.save(file,{...entry,state:'prepared',checked,delivery:{...entry.delivery,text:checked.text??entry.request.text,references:checked.references??[]}});
        try {
          const receipt=await send(entry.delivery);
          this.save(file,{...entry,state:receipt.state==='accepted'&&receipt.messageId?'accepted':'unconfirmed',receipt});
        } catch {this.save(file,{...entry,state:'unconfirmed'});}
      }
      return {state:handled?'checked':'idle',checked:Math.min(handled,limit)};
    } finally {this.resuming=false;}
  }
  async resumeGroup(file,entry,{guard,send}) {
    for(const item of entry.entries.filter(e=>e.state==='unconfirmed')) {
      const receipt=(await this.outbox()).find(r=>r.id===item.delivery.id);
      if(receipt?.state!=='accepted'||!receipt.message_id)return;
      Object.assign(item,{state:'accepted',receipt});this.save(file,{...entry,state:'pending'});
    }
    const permission=await guard(entry);
    if(permission==='wait')return;
    if(permission==='cancel') {
      for(const item of entry.entries.filter(e=>e.state==='unsent'))await this.call('share-cancel',{draft_id:item.request.draft_id});
      this.save(file,{...entry,state:'canceled'});return;
    }
    const unsent=entry.entries.filter(e=>e.state==='unsent');
    const review=await this.checkGroup(unsent.map(e=>e.request));
    if(review.state!=='ready') {
      this.save(file,{...entry,state:review.state==='pending'?'pending':review.state,retryAt:this.clock()+60000});return;
    }
    for(const [index,item] of unsent.entries()) {
      if(await guard(entry)!=='send')return;
      const checked=review.checked[index];
      item.delivery={...item.delivery,text:checked.text??item.request.text,references:checked.references??[]};
      // A crash after transport starts must reconcile the original ID.
      item.state='unconfirmed';this.save(file,{...entry,state:'unconfirmed'});
      try {
        const receipt=await send(item.delivery);item.receipt=receipt;
        if(receipt.state!=='accepted'||!receipt.messageId){this.save(file,{...entry,state:'unconfirmed'});return;}
        item.state='accepted';this.save(file,{...entry,state:'pending'});
      }catch{this.save(file,{...entry,state:'unconfirmed'});return;}
    }
    this.save(file,{...entry,state:'accepted'});
  }
}
