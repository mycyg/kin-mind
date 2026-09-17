import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {TransportManifests,manifestView} from './transport-manifest.mjs';

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
 * below behaves exactly as it did before the manifest existed. */
export class ReplyGuard {
  constructor({call,directory,outbox=()=>[],clock=()=>Date.now(),wholeReplyReview=false,onOutcome=()=>{},transportManifest=true,
    manifestDirectory,channel='feishu',contracts,receipt,emit,lease,role,hooks,sleep,retry}) {
    Object.assign(this,{call,directory,outbox,clock,wholeReplyReview,onOutcome,channel});this.active=new Map();
    if(transportManifest)this.manifests=new TransportManifests({directory:manifestDirectory??path.join(directory,'reply-manifests'),clock,contracts,emit,lease,role,hooks,sleep,retry,
      // The host may hand over a direct reader of `<outbox>/<transport id>.json`; outbox evidence is the fallback.
      receipt:receipt??(async id=>(await this.outbox()).find(r=>r.id===id)??null),
      cancelShare:draftId=>this.call('share-cancel',{draft_id:draftId}),
      review:(requests,context)=>this.reviewGroup(requests,context),
      onOutcome:detail=>this.onOutcome(detail)});
  }
  check(request) {
    const key=createHash('sha256').update(JSON.stringify(request)).digest('hex');
    if(this.active.has(key))return this.active.get(key);
    const run=this.inspect(request,key).finally(()=>this.active.delete(key));this.active.set(key,run);return run;
  }
  async inspect(request,key) {
    const file=path.join(this.directory,key+'.json');fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    let old;try{old=JSON.parse(fs.readFileSync(file,'utf8'));}catch{}
    if(old?.state==='silent'||old?.state==='merged')return old;
    if(old?.state==='pending'&&old.retryAt>this.clock())return old;
    let result;
    try {
      if(!request.work&&request.reply_id) {
        const choice=await this.call('reply-status',{input_id:request.reply_id});
        if(['silent','merged'].includes(choice.action))return this.save(file,{state:choice.action,choice});
      }
      result=await this.call('share-preflight',{...request,outbox:await this.outbox(),allow_model:true});
    } catch {result={state:'pending',reason:'share-review-unavailable'};}
    return this.save(file,{...result,...(result.state==='pending'?{retryAfterMs:60000,retryAt:this.clock()+60000}:{})});
  }
  save(file,value){const temporary=file+'.'+process.pid+'.tmp';fs.writeFileSync(temporary,JSON.stringify(value),{mode:0o600});fs.renameSync(temporary,file);return value;}
  async checkGroup(requests,{frozen=false}={}) {
    if(this.wholeReplyReview){
      const key='group-'+createHash('sha256').update(JSON.stringify([requests,frozen])).digest('hex');
      if(this.active.has(key))return this.active.get(key);
      const run=this.inspectGroup(requests,frozen).finally(()=>this.active.delete(key));
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
  async inspectGroup(entries,frozen) {
    try {
      for(const inputId of new Set(entries.filter(e=>!e.work&&e.reply_id).map(e=>e.reply_id))) {
        const choice=await this.call('reply-status',{input_id:inputId});
        if(['silent','merged'].includes(choice.action))return {state:choice.action,choice};
      }
      const result=await this.call('share-preflight-group',{entries,frozen,outbox:await this.outbox(),allow_model:!frozen});
      // A semantic hold cannot stand in for a voluntary silence receipt.
      return result.state==='ready'?result:{...result,state:'pending',retryAt:this.clock()+60000};
    }catch{return {state:'pending',reason:'whole-reply-review-unavailable',retryAt:this.clock()+60000};}
  }
  /** The review the manifest asks for. Whole-reply mode validates the entire
   * group (`frozen` once a final review is stored); bubble-by-bubble mode only
   * ever looks at bubbles that have not begun. */
  async reviewGroup(requests,{frozen=false,pending=requests.map((_,i)=>i)}={}) {
    if(this.wholeReplyReview)return this.checkGroup(requests,{frozen});
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
    const draft=this.manifests.createDraft({entries,ownerEpoch,channel});
    return this.report(await this.manifests.run(draft.group_id,{guard,transport:send}),draft.group_id);
  }
  async deliverGroup(entries,ownerEpoch,review,{guard,send,channel=this.channel}) {
    if(this.manifests) {
      const draft=this.manifests.createDraft({entries,ownerEpoch,channel});
      return this.report(await this.manifests.run(draft.group_id,{guard,transport:send,initialReview:review}),draft.group_id);
    }
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
    return manifestView(this.manifests.createDraft({entries,ownerEpoch,channel:this.channel,hold:{reason:'share-review-pending',retryAt:this.clock()+delay}}));
  }
  defer(request,delivery,ownerEpoch) {
    if(this.manifests)return this.park([{request,delivery}],ownerEpoch,20*60000);
    fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    const file=path.join(this.directory,createHash('sha256').update(delivery.id).digest('hex')+'.pending.json');
    let prior;try{prior=JSON.parse(fs.readFileSync(file,'utf8'));}catch{}
    if(prior)return prior;
    return this.save(file,{state:'pending',request,delivery,ownerEpoch,at:this.clock(),retryAt:this.clock()+20*60000});
  }
  async resumeDue({guard,send,limit=2}) {
    if(this.resuming||!fs.existsSync(this.directory))return {state:'idle'};
    this.resuming=true;let handled=0;
    try {
      if(this.manifests) {
        // Unfinished groups of the old journal come along once; finished ones never do.
        const legacy=this.manifests.importLegacy(this.directory);
        return {...await this.manifests.resumeDue({guard,transport:send,limit}),imported:legacy.imported.length};
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
