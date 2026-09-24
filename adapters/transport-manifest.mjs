/** The durable delivery record of one phone reply group: which bubbles it has,
 * how each frozen bubble is cut into transport fragments, and what is known
 * about every fragment. This module is the only writer of
 * `<directory>/<group_id>.json` (+ `.prev`, `quarantine/`, `done/`, `leases/`);
 * `<directory>/tail/` beside them belongs to `reply-tail.mjs`.
 *
 * Rules that everything below follows:
 *  - the final manifest is on disk before the first byte is sent, and a retry
 *    reads it back; nothing is ever cut or numbered a second time;
 *  - a fragment's state follows the transport's own receipt, which the
 *    transport writes before it submits. No receipt means the send never
 *    began; only a receipt that cannot tell blocks the group, and only it;
 *  - the lease decides who may write, never what was sent;
 *  - Python hears about whole bubbles only. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {readJsonFile,writeJsonAtomic,createJsonExclusive,quarantineFile} from './atomic-json.mjs';
import {acquireLease,inspectLease,removeLease,LeaseLost,LEASE_DEFAULTS} from './state-lease.mjs';
import {channelContract,classifyReceipt,normalizeReceipt,mayResend,fileReceipts,RECONCILE_FIRST_CODES} from './channel-contract.mjs';
import {fragmentText,fileFragment,verifyFragments} from './text-fragments.mjs';
import {deliveryEvent} from './memory-events.mjs';

export const MANIFEST_SCHEMA=1;
export const GROUP_STATES=Object.freeze(['draft','reviewed','sending','accepted','held','blocked-unknown','interrupted','retired','partial','undeliverable']);
export const TERMINAL_GROUP_STATES=Object.freeze(['accepted','retired','partial','undeliverable']);
export const BUBBLE_STATES=Object.freeze(['unsent','sending','accepted','unconfirmed','rejected','undeliverable','canceled']);
export const FRAGMENT_STATES=Object.freeze(['unsent','submitting','accepted','rejected','unknown','failed','canceled']);
const TERMINAL_BUBBLES=['accepted','rejected','undeliverable','canceled'];
const EVENT_FOR={accepted:'accepted',unconfirmed:'unconfirmed',canceled:'canceled',rejected:'canceled',undeliverable:'canceled'};
const SAFE_ID=/^[A-Za-z0-9_-]{1,160}$/;

const sha256=value=>createHash('sha256').update(value).digest('hex');
const iso=ms=>new Date(ms).toISOString();
const terminal=manifest=>TERMINAL_GROUP_STATES.includes(manifest.state);
const started=bubble=>bubble.fragments.some(f=>f.state!=='unsent');
const open=bubble=>!TERMINAL_BUBBLES.includes(bubble.state);
const supersededBeforeSend=receipt=>receipt?.state==='not-submitted'&&receipt.submissionStarted===false&&
  receipt.reason==='turn-superseded-before-send';
const executionNumber=value=>Number.isSafeInteger(value)&&value>=0;
const routingBasis=value=>JSON.stringify([
  [Object.hasOwn(value,'taskId'),value.taskId],
  [Object.hasOwn(value,'inputVersion'),value.inputVersion],
  [Object.hasOwn(value,'turnFence'),value.turnFence],
]);

/** Stored once in the manifest, never derived again. */
export function transportId(bubbleId,index,bodySha256){return 'kin-frag-'+sha256(bubbleId+'\0'+index+'\0'+bodySha256).slice(0,48);}

export function groupIdFor(entries) {
  const batch=entries[0].delivery.memoryBatchId;
  return typeof batch==='string'&&SAFE_ID.test(batch)?batch:'group-'+sha256(String(batch??entries[0].delivery.id)).slice(0,48);
}

function bubbleState(bubble) {
  if(['canceled','undeliverable'].includes(bubble.state))return bubble.state;
  const states=bubble.fragments.map(f=>f.state);
  if(!states.length)return 'unsent';
  if(states.includes('unknown'))return 'unconfirmed';
  if(states.every(s=>s==='accepted'))return 'accepted';
  if(states.includes('rejected'))return 'rejected';
  if(states.includes('failed'))return 'undeliverable';
  return states.some(s=>s==='accepted'||s==='submitting')?'sending':'unsent';
}

/** The state older callers know: what `ReplyGuard` used to return and what the
 * reply-progress events carry. The manifest's own state travels beside it. */
export function legacyState(manifest) {
  if(manifest.state==='retired')return ['silent','merged'].includes(manifest.reason)?manifest.reason:'canceled';
  return {draft:'pending',held:'pending',reviewed:'prepared',sending:'prepared','blocked-unknown':'unconfirmed'}[manifest.state]??manifest.state;
}

/** A manifest in the shape the host's guard and `deliverGroup` callers read. Carries bubble text. */
export function manifestView(manifest) {
  const shared={kind:manifest.kind,memoryBatchId:manifest.delivery_id,expectedBubbles:manifest.expected_bubbles,
    ...(Object.hasOwn(manifest,'taskId')?{taskId:manifest.taskId}:{}),
    ...(Object.hasOwn(manifest,'inputVersion')?{inputVersion:manifest.inputVersion}:{}),
    ...(Object.hasOwn(manifest,'turnFence')?{turnFence:manifest.turnFence}:{}),
    ...(Object.hasOwn(manifest,'sessionFence')?{sessionFence:manifest.sessionFence}:{})};
  const entries=manifest.bubbles.map(b=>({request:b.request,state:b.state,delivery:{...shared,id:b.bubble_id,text:b.text,references:b.references,draftId:b.draft_id},
    fragments:b.fragments.map(f=>({transport_id:f.transport_id,kind:f.kind,state:f.state,messageId:f.receipt?.messageId}))}));
  return {state:legacyState(manifest),groupState:manifest.state,groupId:manifest.group_id,reason:manifest.reason??undefined,ownerEpoch:manifest.ownerEpoch,retryAt:manifest.retryAt,
    createdAt:manifest.created_at,...(manifest.continued?{continuedAt:manifest.continued.at,continuedEpoch:manifest.continued.epoch}:{}),
    request:entries[0]?.request,delivery:entries[0]?.delivery,...(manifest.choice?{choice:manifest.choice}:{}),entries};
}

/** A group is kept in the live set while the fate of an unsent remainder is
 * still open: its own intent, an obligation that came back, or an older group
 * that is waiting for this one's outcome. `reply-tail.mjs` writes these fields. */
export function tailOpen(manifest) {
  return Boolean((manifest.tail_intent&&manifest.tail_intent.state!=='settled')||manifest.tail_owed?.items?.length||
    (manifest.continues??[]).some(c=>!c.done)||(manifest.review?.covers_remainder?.length&&!manifest.review.coverage_applied));
}

/** Status without any message text: safe for health output and logs. */
export function manifestSummary(manifest) {
  const intent=manifest.tail_intent,owed=manifest.tail_owed;
  return {group_id:manifest.group_id,state:manifest.state,reason:manifest.reason??null,reply_id:manifest.reply_id,channel:manifest.channel,
    created_at:manifest.created_at,updated_at:manifest.updated_at,retryAt:manifest.retryAt,revision:manifest.revision,leaseGeneration:manifest.leaseGeneration,
    ...(manifest.continues_reply_id?{continues_reply_id:manifest.continues_reply_id}:{}),
    ...(intent?{tail_intent:{id:intent.id,state:intent.state,...(intent.decision?{decision:intent.decision,carrier:intent.carrier??null,linked_group:intent.linked_group??null}:{})}}:{}),
    ...(owed?.items?.length?{tail_owed:{items:owed.items.length,resurfaced:owed.resurfaced??0,forced:Boolean(owed.forced)}}:{}),
    ...(manifest.holds?{holds:manifest.holds}:{}),...(manifest.parked?{parked:manifest.parked}:{}),
    bubbles:manifest.bubbles.map(b=>({bubble_id:b.bubble_id,draft_id:b.draft_id,state:b.state,reason:b.reason??null,...(b.superseded_by?{superseded_by:b.superseded_by}:{}),
      fragments:b.fragments.map(f=>({transport_id:f.transport_id,index:f.index,kind:f.kind,state:f.state}))}))};
}

export class TransportManifests {
  constructor({directory,clock=()=>Date.now(),contracts={},receipt,emit,cancelShare,review,onOutcome=()=>{},role='service',lease={},hooks={},
    sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),retry={},keepLive=()=>false}) {
    Object.assign(this,{directory,clock,contracts,receipt,emit,cancelShare,review,onOutcome,role,hooks,sleep,keepLive});
    this.lease={...LEASE_DEFAULTS,heartbeat:true,...lease};
    this.retry={baseMs:60000,maxMs:15*60000,maxFailures:6,sideEffectAttempts:5,maxHolds:6,...retry};
    this.lastSubmitAt=null;this.notified=new Map();
  }
  file(id){return path.join(this.directory,id+'.json');}
  /** Settled groups are filed by month, so the live set stays small and old months can be pruned whole. */
  doneFile(id,at=this.clock()){return path.join(this.directory,'done',iso(at).slice(0,7),id+'.json');}
  doneMonths() {
    try{return fs.readdirSync(path.join(this.directory,'done')).filter(name=>/^\d{4}-\d{2}$/.test(name)).sort().reverse();}
    catch(error){if(error.code==='ENOENT')return [];throw error;}
  }
  findDone(id){return this.doneMonths().map(month=>path.join(this.directory,'done',month,id+'.json')).find(file=>fs.existsSync(file))??null;}
  contract(manifest){return channelContract(manifest.channel,this.contracts[manifest.channel]);}

  // ---- storage ---------------------------------------------------------

  valid(manifest,id) {
    if(manifest?.schema!==MANIFEST_SCHEMA||manifest.group_id!==id||!GROUP_STATES.includes(manifest.state)||!Array.isArray(manifest.bubbles)||!manifest.bubbles.length)return false;
    if((Object.hasOwn(manifest,'inputVersion')&&!executionNumber(manifest.inputVersion))||
      (Object.hasOwn(manifest,'turnFence')&&!executionNumber(manifest.turnFence)))return false;
    return manifest.bubbles.every(b=>typeof b.text==='string'&&Array.isArray(b.fragments)&&BUBBLE_STATES.includes(b.state)&&sha256(b.text)===b.body_sha256&&
      b.fragments.every(f=>FRAGMENT_STATES.includes(f.state))&&(!b.fragments.length||verifyFragments(b.text,b.fragments)===null));
  }
  /** The manifest that counts: the current file, unless it is unreadable or a
   * lower-generation write landed over a higher one; then `.prev`. A lease
   * holder also repairs the files; anyone else only reads. */
  load(id,lease=null) {
    const file=this.file(id),current=readJsonFile(file),previous=readJsonFile(file+'.prev');
    const good=result=>result.state==='ok'&&this.valid(result.value,id);
    if(!good(current)&&!good(previous)) {
      if(current.state==='missing'&&previous.state==='missing'){const filed=this.findDone(id),done=filed?readJsonFile(filed):null;return done&&good(done)?done.value:null;}
      // Another version's manifest is not damage: leave it for the code that wrote it.
      if(current.state==='ok'&&Number.isSafeInteger(current.value?.schema)&&current.value.schema!==MANIFEST_SCHEMA)return null;
      for(const name of [file,file+'.prev'])quarantineFile(name,path.join(this.directory,'quarantine'));
      return null;
    }
    const stale=good(current)&&good(previous)&&(previous.value.leaseGeneration??0)>(current.value.leaseGeneration??0);
    if(good(current)&&!stale)return current.value;
    if(lease) {
      lease.assertHeld();
      if(current.state!=='missing') {
        const kept=path.join(this.directory,'quarantine',id+'.json.'+(stale?'stale':'corrupt')+'.g'+lease.generation+'.'+process.pid);
        fs.mkdirSync(path.dirname(kept),{recursive:true,mode:0o700});fs.copyFileSync(file,kept);fs.chmodSync(kept,0o600);
      }
      writeJsonAtomic(file,previous.value,{previous:false});
    }
    return previous.value;
  }
  /** Every write goes through here: refused once the lease is lost, close to
   * expiry, or outranked by what is already on disk. */
  async save(manifest,lease) {
    lease.assertHeld();
    const disk=readJsonFile(this.file(manifest.group_id));
    if(disk.state==='ok'&&(disk.value?.leaseGeneration??0)>lease.generation)throw new LeaseLost('newer-generation-on-disk');
    Object.assign(manifest,{leaseGeneration:lease.generation,revision:(manifest.revision??0)+1,
      updated_at:this.clock(),writer:{pid:lease.owner.pid,role:lease.owner.role}});
    await this.hooks.beforeSave?.(manifest);
    writeJsonAtomic(this.file(manifest.group_id),manifest);
    const mark=manifest.state+'\0'+(manifest.reason??'');
    if(this.notified.get(manifest.group_id)!==mark) {
      this.notified.set(manifest.group_id,mark);if(this.notified.size>256)this.notified.delete(this.notified.keys().next().value);
      try{this.onOutcome({state:legacyState(manifest),groupState:manifest.state,groupId:manifest.group_id,inputId:manifest.reply_id,reason:manifest.reason??undefined});}catch{/* Status reporting never decides delivery. */}
    }
  }
  isLive(id){return fs.existsSync(this.file(id))||fs.existsSync(this.file(id)+'.prev');}
  acquire(id,floor){return acquireLease({...this.lease,directory:path.join(this.directory,'leases'),id,role:this.role,clock:this.clock,floor});}
  live() {
    let names=[];
    try{names=fs.readdirSync(this.directory);}catch(error){if(error.code!=='ENOENT')throw error;}
    return names.filter(name=>name.endsWith('.json')).map(name=>name.slice(0,-5)).filter(id=>SAFE_ID.test(id));
  }

  // ---- creation --------------------------------------------------------

  /** Persist the group before anything else happens to it (`draft`: the text
   * exists, nothing is reviewed or cut yet). The same entries give the same
   * manifest back. `continues` names the older groups whose unsent remainder
   * this group answers for; it is part of the very first write, so the link
   * exists from the moment the group does. */
  createDraft({entries,ownerEpoch,channel='feishu',hold=null,continues=null,continuation=null}) {
    if(!Array.isArray(entries)||!entries.length||entries.some(e=>!e?.request||!e.delivery?.id))throw Error('A reply group needs bubbles with stable IDs');
    const ids=entries.map(e=>e.delivery.id),first=entries[0],now=this.clock();
    if((Object.hasOwn(first.delivery,'inputVersion')&&!executionNumber(first.delivery.inputVersion))||
      (Object.hasOwn(first.delivery,'turnFence')&&!executionNumber(first.delivery.turnFence)))throw Error('Reply routing versions must be non-negative safe integers');
    if(entries.some(entry=>routingBasis(entry.delivery)!==routingBasis(first.delivery)))throw Error('A reply group cannot mix routing versions');
    let id=groupIdFor(entries);
    const existing=this.load(id);
    // A batch ID that already names other bubbles: keep both, under separate groups.
    if(existing&&existing.bubbles.map(b=>b.bubble_id).join('\0')!==ids.join('\0'))id=(id+'-b'+sha256(ids.join('\0')).slice(0,12)).slice(-160);
    else if(existing&&routingBasis(existing)!==routingBasis(first.delivery))throw Error('Reply routing basis conflicts with its durable manifest');
    const manifest={schema:MANIFEST_SCHEMA,group_id:id,delivery_id:first.delivery.memoryBatchId??id,reply_id:first.request.reply_id??null,channel,
      kind:first.delivery.kind??'reply',work:Boolean(first.request.work),
      ...(Object.hasOwn(first.delivery,'taskId')?{taskId:first.delivery.taskId}:{}),
      ...(Object.hasOwn(first.delivery,'inputVersion')?{inputVersion:first.delivery.inputVersion}:{}),
      ...(Object.hasOwn(first.delivery,'turnFence')?{turnFence:first.delivery.turnFence}:{}),
      ...(Object.hasOwn(first.delivery,'sessionFence')?{sessionFence:first.delivery.sessionFence}:{}),ownerEpoch,
      expected_bubbles:first.delivery.expectedBubbles??entries.length,state:hold?'held':'draft',reason:hold?.reason??null,leaseGeneration:0,revision:0,review:null,
      created_at:now,updated_at:now,retryAt:hold?.retryAt??0,failures:0,
      ...(continues?.length?{continues_reply_id:continues[0].group_id,continues:continues.map(c=>({group_id:c.group_id,intent_id:c.intent_id??null}))}:{}),
      ...(continuation?{continuation}:{}),
      bubbles:entries.map((entry,order)=>{const text=entry.delivery.text??entry.request.text;
        return {bubble_id:entry.delivery.id,draft_id:entry.request.draft_id,order,request:entry.request,text,body_sha256:sha256(text),references:entry.delivery.references??[],state:'unsent',fragments:[]};})};
    const loaded=this.load(id);
    if(loaded) {
      if(routingBasis(loaded)!==routingBasis(first.delivery))throw Error('Reply routing basis conflicts with its durable manifest');
      return loaded;
    }
    if(createJsonExclusive(this.file(id),manifest))return manifest;
    const raced=this.load(id);
    if(raced&&routingBasis(raced)!==routingBasis(first.delivery))throw Error('Reply routing basis conflicts with its durable manifest');
    return raced;
  }

  /** Freeze bodies, references and fragment identities from a passed review.
   * After this the group is final: later passes only read it. */
  finalize(manifest,review) {
    if(review.checked?.length!==manifest.bubbles.length)throw Error('Whole reply review is incomplete');
    const contract=this.contract(manifest);
    for(const [index,bubble] of manifest.bubbles.entries()) {
      if(started(bubble)||!open(bubble))continue;
      const checked=review.checked[index]??{},text=checked.text??bubble.request.text??bubble.text;
      const bodyHash=sha256(text);
      if(checked.text_hash&&checked.text_hash!==bodyHash)throw Error('Reviewed reply body does not match its hash');
      Object.assign(bubble,{text,body_sha256:bodyHash,references:checked.references??bubble.references??[]});
      if(!text.trim()){Object.assign(bubble,{state:'undeliverable',reason:'empty-body',fragments:[]});continue;}
      const plan=bubble.legacy?{kind:'text',fragments:[{index:0,start:0,end:text.length,body_sha256:bubble.body_sha256}]}:fragmentText(text,contract.text);
      if(plan.kind==='text')bubble.fragments=plan.fragments.map(f=>({transport_id:bubble.legacy?bubble.bubble_id:transportId(bubble.bubble_id,f.index,f.body_sha256),...f,kind:'text',state:'unsent',receipt:null}));
      else {
        // Unsplittable: the complete bubble goes as one file, with no words of the host's own.
        const whole=fileFragment(text);
        bubble.fragments=[{transport_id:transportId(bubble.bubble_id,0,whole.body_sha256),...whole,state:'unsent',receipt:null}];
        if(Buffer.byteLength(text,'utf8')>contract.file.maxBytes)Object.assign(bubble,{state:'undeliverable',reason:'file-exceeds-channel-limit'});
      }
    }
    // What this reply is reported to cover of an older group's remainder: IDs only.
    // It is a claim; only this group's own receipts settle it.
    const claims=Array.isArray(review.covers_remainder)?review.covers_remainder.filter(c=>c?.item_id&&c.covered_by).map(c=>({item_id:String(c.item_id),covered_by:String(c.covered_by)})):[];
    manifest.review={id:review.review_id??null,mode:review.mode??'semantic',checked_hashes:manifest.bubbles.map(b=>b.body_sha256),
      ...(claims.length?{covers_remainder:claims}:{}),...(Array.isArray(review.remainder_owed)?{remainder_owed:review.remainder_owed.map(String)}:{})};
    Object.assign(manifest,{state:'reviewed',reason:null,retryAt:0});this.unhold(manifest);
  }
  /** A passed review, or a new reason to look again, gives the group a fresh, bounded set of tries. */
  unhold(manifest){delete manifest.holds;delete manifest.parked;delete manifest.review_progress;}

  // ---- one pass over one group -----------------------------------------

  /** Reconcile, review if still needed, then send what the manifest says is
   * unsent. Returns `{manifest,worked}`, or `{busy:true}` when another entrant
   * owns the group: a second entrant changes nothing at all. */
  async run(id,{guard=async()=>'send',transport=null,review=this.review,initialReview=null,operator=null,resume=false,operatorOnly=false}={}) {
    const seen=this.load(id);
    if(!seen)return {missing:true};
    if(!this.isLive(id))return {manifest:seen,worked:false};          // settled and filed away: nothing left to decide
    const lease=this.acquire(id,seen.leaseGeneration??0);
    if(!lease)return {busy:true,manifest:seen};
    const context={lease,guard,review,resume,transport:typeof transport==='function'?{send:transport}:transport,worked:false,failedEffects:new Set()};
    context.canSend=typeof context.transport?.send==='function';
    context.receipt=async fragmentId=>normalizeReceipt(await (context.transport?.receipt?.(fragmentId)??this.receipt?.(fragmentId)??null));
    let manifest=seen;
    try {
      manifest=this.load(id,lease);
      if(!manifest)return {missing:true};
      if(!this.isLive(id)){removeLease({directory:path.join(this.directory,'leases'),id});return {manifest,worked:false};}
      if(operator)await operator(manifest,context);
      // `operatorOnly`: a change that must be quick and local (it may run on the
      // owner's input path) touches neither receipts nor the memory host.
      if(!operatorOnly){await this.pass(manifest,context,initialReview);await this.settle(manifest,context);}
      return {manifest,worked:context.worked};
    } catch(error) {
      if(error instanceof LeaseLost)return {lost:true,manifest:this.load(id)??manifest};
      throw error;
    } finally {lease.release();}
  }

  async pass(manifest,context,initialReview) {
    await this.flush(manifest,context);
    if(terminal(manifest))return;
    await this.reconcile(manifest,context);
    if(await this.blocked(manifest,context))return;
    if(!manifest.bubbles.some(open))return this.finish(manifest,context);      // needs no transport: a receipt or an operator settled the last bubble
    if(manifest.state==='interrupted'||!context.canSend)return;
    if(!manifest.review&&initialReview?.state==='ready'&&!manifest.bubbles.some(started)){this.finalize(manifest,initialReview);await this.touch(manifest,context);}
    // A bubble that has begun is always finished; only then does anyone get to stop the group.
    for(const bubble of manifest.bubbles.filter(b=>open(b)&&started(b)))if(await this.sendBubble(manifest,bubble,context)!=='sent')return;
    if(!manifest.bubbles.some(open))return this.finish(manifest,context);
    if(!await this.permitted(manifest,context))return;
    // A parked group is only ever stopped by the timer, never reviewed by it; anyone who asks for it by name is a new reason.
    if(manifest.parked){if(context.resume)return;this.unhold(manifest);}
    const requests=manifest.bubbles.map(b=>b.request),pending=manifest.bubbles.flatMap((b,i)=>open(b)&&!started(b)?[i]:[]);
    const verdict=context.review?await context.review(requests,{frozen:Boolean(manifest.review),pending,groupId:manifest.group_id,sent:this.sentEvidence(manifest)}):{state:'ready',checked:requests.map(()=>({}))};
    context.worked=true;
    if(['silent','merged'].includes(verdict.state)){manifest.choice=verdict.choice;return this.retire(manifest,context,{reason:verdict.state});}
    if(verdict.state!=='ready')return this.refused(manifest,context,verdict);
    if(!manifest.review){this.finalize(manifest,verdict);await this.save(manifest,context.lease);}
    else if(manifest.holds||manifest.parked)this.unhold(manifest);
    // A review reads every request of the group. Whatever it reserved for a bubble that was withdrawn before is released once more.
    for(const bubble of manifest.bubbles)if(bubble.state==='canceled'&&bubble.share_canceled)bubble.share_canceled=false;
    for(const bubble of manifest.bubbles.filter(open)) {
      if(!await this.permitted(manifest,context))return;
      if(await this.sendBubble(manifest,bubble,context)!=='sent')return;
    }
    return this.finish(manifest,context);
  }
  async touch(manifest,context){context.worked=true;await this.save(manifest,context.lease);}

  /** What this group already handed to a transport, in the shape the review reads an outbox row. */
  sentEvidence(manifest) {
    return manifest.bubbles.filter(b=>b.state==='accepted'||(open(b)&&started(b))).map(b=>{
      const last=b.fragments.filter(f=>f.state==='accepted').at(-1)?.receipt;
      return {id:b.bubble_id,draft_id:b.draft_id,text:b.text,state:b.state==='accepted'?'accepted':'unconfirmed',references:b.references??[],message_id:last?.messageId,at:last?.acceptedAt??b.state_at};
    });
  }

  /** A review that is not ready. Held, not lost, and not a model call a minute
   * for ever either: every refusal doubles the wait, and after `maxHolds` of
   * them the group is parked until there is a new reason to ask (a new owner
   * input, an explicit delivery, an operator). A review that could not be
   * reached at all (`transient`) judged nothing: it keeps the growing wait but
   * never parks the group. Two answers are no hold at all. */
  async refused(manifest,context,verdict) {
    const reason=verdict.reason??'share-review-pending',now=this.clock();
    if(verdict.route==='interrupt') {       // the remainder cannot go out as written: a tail decision, not a wait
      Object.assign(manifest,{state:'interrupted',reason,retryAt:0,interrupted:{at:now,by:null}});return this.save(manifest,context.lease);
    }
    if(verdict.route==='undeliverable') {   // asking again can never help: end it where status shows it
      for(const bubble of manifest.bubbles.filter(b=>open(b)&&!started(b)))Object.assign(bubble,{state:'undeliverable',reason,state_at:iso(now)});
      return this.finish(manifest,context);
    }
    const progressed=Number(verdict.chunks?.reviewed)>(manifest.review_progress??0);      // chunks already paid for are progress, not a refusal
    const holds=(manifest.holds??0)+(progressed?0:1),parked=!verdict.transient&&holds>=this.retry.maxHolds;
    Object.assign(manifest,{state:'held',reason,holds,retryAt:parked?0:Math.max(verdict.retryAt??0,now+Math.min(this.retry.maxMs,this.retry.baseMs*2**Math.max(0,holds-1)))});
    if(progressed)manifest.review_progress=Number(verdict.chunks.reviewed);
    if(parked)manifest.parked={at:now,reason:'review-hold-limit',holds};
    return this.save(manifest,context.lease);
  }

  /** The guard speaks at bubble boundaries only: send | wait | cancel | interrupt. */
  async permitted(manifest,context) {
    const answer=await context.guard(manifestView(manifest)),action=typeof answer==='string'?answer:answer?.action;
    if(action==='send')return true;
    if(action==='cancel')await this.retire(manifest,context,{reason:answer?.reason??'input-or-session-superseded'});
    else if(action==='interrupt'){Object.assign(manifest,{state:'interrupted',reason:answer?.reason??'new-input',retryAt:0,interrupted:{at:this.clock(),by:answer?.inputId??null}});await this.touch(manifest,context);}
    return false;
  }

  /** Bring every in-flight or unknown fragment in line with its receipt. */
  async reconcile(manifest,context) {
    let changed=false;
    for(const bubble of manifest.bubbles)for(const fragment of bubble.fragments) {
      if(!['submitting','unknown'].includes(fragment.state))continue;
      const receipt=await context.receipt(fragment.transport_id),kind=classifyReceipt(receipt),before=fragment.state;
      if(kind==='accepted'||kind==='rejected') {
        Object.assign(fragment,{state:kind,receipt});
        // An imported bubble this layer never submitted was sent, and reported to memory, by the old sender.
        if(kind==='accepted'&&bubble.legacy&&!fragment.attempts)bubble.emitted={...bubble.emitted,accepted:true};
      }
      else if(kind==='unknown')Object.assign(fragment,{state:'unknown',receipt});
      // No receipt, or one proving nothing was submitted: the send never began.
      // A fragment already known to be unknown keeps the stronger earlier evidence.
      else if(before==='submitting')Object.assign(fragment,{state:fragment.kind==='file'&&kind==='never-started'?'failed':'unsent',receipt});
      changed||=fragment.state!==before;
    }
    // An unknown fragment is resubmitted under its own ID at most once, and only
    // while the platform's deduplication window provably still covers it.
    for(const bubble of manifest.bubbles)for(const fragment of bubble.fragments) {
      if(fragment.state!=='unknown'||!context.canSend||!mayResend(this.contract(manifest),fragment,this.clock()))continue;
      fragment.resentAt=this.clock();this.refresh(manifest);await this.touch(manifest,context);
      let returned=null;
      try{returned=normalizeReceipt(await context.transport.send({...this.delivery(manifest,bubble,fragment),resend:{firstSubmitAt:fragment.firstSubmitAt}}));}catch{/* The receipt decides. */}
      const receipt=classifyReceipt(returned)==='accepted'?returned:await context.receipt(fragment.transport_id);
      // Only an acceptance settles a resend: a refusal of a duplicate says nothing about the first attempt.
      if(classifyReceipt(receipt)==='accepted')Object.assign(fragment,{state:'accepted',receipt});
      changed=true;
    }
    if(changed){this.refresh(manifest);await this.touch(manifest,context);}
  }

  /** `blocked-unknown` stops this group and nothing else. */
  async blocked(manifest,context) {
    const unknown=manifest.bubbles.some(b=>b.fragments.some(f=>f.state==='unknown'));
    if(unknown&&manifest.state!=='blocked-unknown'){Object.assign(manifest,{state:'blocked-unknown',reason:'delivery-outcome-unknown',retryAt:0});await this.touch(manifest,context);}
    if(!unknown&&manifest.state==='blocked-unknown'){Object.assign(manifest,{state:'sending',reason:null});await this.touch(manifest,context);}
    if(unknown)await this.flush(manifest,context);
    return unknown;
  }

  /** Recompute bubble states from their fragments; stamp each change once. */
  refresh(manifest) {
    for(const bubble of manifest.bubbles) {
      const next=bubbleState(bubble);
      if(['rejected','undeliverable'].includes(next))for(const f of bubble.fragments)if(f.state==='unsent')f.state='canceled';
      if(next===bubble.state)continue;
      bubble.state=next;bubble.state_at=iso(this.clock());
      if(next==='undeliverable')bubble.reason??='file-channel-failed';
    }
  }

  delivery(manifest,bubble,fragment) {
    const body=bubble.text.slice(fragment.start,fragment.end);
    const base={id:fragment.transport_id,kind:manifest.kind,draftId:bubble.draft_id,memoryBatchId:manifest.delivery_id,expectedBubbles:manifest.expected_bubbles,
      references:bubble.references,
      ...(Object.hasOwn(manifest,'taskId')?{taskId:manifest.taskId}:{}),
      ...(Object.hasOwn(manifest,'inputVersion')?{inputVersion:manifest.inputVersion}:{}),
      ...(Object.hasOwn(manifest,'turnFence')?{turnFence:manifest.turnFence}:{}),
      ...(Object.hasOwn(manifest,'sessionFence')?{sessionFence:manifest.sessionFence}:{}),
      // Python hears about bubbles from this layer; the transport stays silent.
      memory:false,bubbleId:bubble.bubble_id,fragment:{index:fragment.index,count:bubble.fragments.length,start:fragment.start,end:fragment.end,body_sha256:fragment.body_sha256}};
    return fragment.kind==='file'?{...base,media:{type:'file',name:fragment.name,data:Buffer.from(body,'utf8')}}:{...base,text:body};
  }

  /** The old turn was refused before transport submission. Cancel its unsent
   * remainder while keeping any earlier accepted fragment as partial evidence. */
  async cancelSuperseded(manifest,bubble,fragment,receipt,context) {
    if(bubble.fragments.some(f=>f!==fragment&&['submitting','unknown'].includes(f.state)))throw Error('Another fragment still needs reconciliation');
    const at=this.clock(),reason='turn-superseded-before-send',state_at=iso(at);
    Object.assign(fragment,{state:'canceled',receipt});
    for(const rest of bubble.fragments)if(rest.state==='unsent')rest.state='canceled';
    Object.assign(bubble,{state:'canceled',reason,state_at});
    for(const rest of manifest.bubbles.filter(b=>open(b)&&!started(b)))Object.assign(rest,{state:'canceled',reason,state_at});
    if(manifest.bubbles.some(open)){await this.touch(manifest,context);return;}
    if(!manifest.bubbles.some(b=>b.fragments.some(f=>f.state==='accepted')))manifest.retired={reason,at};
    await this.finish(manifest,context);
  }

  /** 'sent' when the bubble reached a final state, 'stop' when the group must wait. */
  async sendBubble(manifest,bubble,context) {
    const contract=this.contract(manifest);
    for(const fragment of bubble.fragments) {
      if(fragment.state!=='unsent')continue;
      // What the transport wrote down outranks what this manifest remembers.
      let receipt=await context.receipt(fragment.transport_id),kind=classifyReceipt(receipt);
      if(supersededBeforeSend(receipt)){await this.cancelSuperseded(manifest,bubble,fragment,receipt,context);return 'stop';}
      if(kind==='absent'||kind==='never-started') {
        const gap=contract.maxSendsPerSecond?Math.ceil(1000/contract.maxSendsPerSecond):0,wait=this.lastSubmitAt===null?0:this.lastSubmitAt+gap-this.clock();
        if(wait>0)await this.sleep(wait);
        const now=this.lastSubmitAt=this.clock();
        Object.assign(fragment,{state:'submitting',firstSubmitAt:fragment.firstSubmitAt??now,attempts:(fragment.attempts??0)+1});
        Object.assign(manifest,{state:'sending',reason:null,retryAt:0});this.refresh(manifest);
        await this.touch(manifest,context);          // refused ⇒ nothing is sent
        let returned=null,refused=false;
        try{returned=normalizeReceipt(await context.transport.send(this.delivery(manifest,bubble,fragment)));}
        catch(error){refused=RECONCILE_FIRST_CODES.includes(error?.code);/* Otherwise the durable receipt decides. */}
        await this.hooks.afterSend?.({manifest,bubble,fragment,receipt:returned});
        receipt=returned??await context.receipt(fragment.transport_id);kind=classifyReceipt(receipt);
        // The transport holds a receipt this layer cannot see: never mistake that for "nothing was sent".
        if(refused&&kind==='absent'){receipt={state:'unreadable'};kind='unknown';}
      }
      if(supersededBeforeSend(receipt)){await this.cancelSuperseded(manifest,bubble,fragment,receipt,context);return 'stop';}
      if(kind==='accepted'){Object.assign(fragment,{state:'accepted',receipt});manifest.failures=0;}
      else if(kind==='rejected')Object.assign(fragment,{state:'rejected',receipt});
      else if(kind==='unknown')Object.assign(fragment,{state:'unknown',receipt});
      else if(fragment.kind==='file')Object.assign(fragment,{state:'failed',receipt});
      else {
        // The send never began. Try again later; a transport that stays unavailable ends the group, never leaves it pending.
        Object.assign(fragment,{state:'unsent',receipt});manifest.failures=(manifest.failures??0)+1;
        if(manifest.failures>=this.retry.maxFailures) {
          for(const rest of manifest.bubbles.filter(open))Object.assign(rest,{state:'undeliverable',reason:'transport-unavailable',state_at:iso(this.clock())});
          this.refresh(manifest);await this.finish(manifest,context);return 'stop';
        }
        manifest.retryAt=this.clock()+Math.min(this.retry.maxMs,this.retry.baseMs*2**(manifest.failures-1));
        this.refresh(manifest);await this.touch(manifest,context);return 'stop';
      }
      this.refresh(manifest);await this.touch(manifest,context);
      if(kind==='unknown'){await this.blocked(manifest,context);return 'stop';}
      if(!open(bubble))break;
    }
    await this.flush(manifest,context);
    return 'sent';
  }

  /** Retire every bubble that has not begun (or, with `only`, those of them
   * named by draft ID). What was sent stays sent. */
  async retire(manifest,context,{reason,superseded_by,only}={}) {
    const named=only?new Set(only):null;
    for(const bubble of manifest.bubbles.filter(b=>open(b)&&!started(b)&&(!named||named.has(b.draft_id??b.bubble_id))))
      Object.assign(bubble,{state:'canceled',reason,state_at:iso(this.clock()),...(superseded_by?{superseded_by}:{})});
    manifest.retired={reason,at:this.clock(),...(superseded_by?{superseded_by}:{})};
    context.worked=true;
    if(manifest.bubbles.some(open)){await this.save(manifest,context.lease);return;}
    await this.finish(manifest,context);
  }

  async finish(manifest,context) {
    this.refresh(manifest);
    const accepted=manifest.bubbles.filter(b=>b.state==='accepted').length,reached=accepted>0||manifest.bubbles.some(b=>b.fragments.some(f=>f.state==='accepted'));
    const state=accepted===manifest.bubbles.length?'accepted':manifest.retired?'retired':reached?'partial':'undeliverable';
    Object.assign(manifest,{state,reason:state==='accepted'?null:state==='retired'?manifest.retired.reason:manifest.bubbles.find(b=>b.reason)?.reason??'delivery-incomplete',retryAt:0});
    context.worked=true;await this.save(manifest,context.lease);
    await this.flush(manifest,context);
  }

  bubbleEvent(manifest,bubble,kind) {
    const receipts=bubble.fragments.filter(f=>f.state==='accepted').map(f=>f.receipt??{}),at=bubble.event_at[kind];
    const event=deliveryEvent({id:bubble.bubble_id,text:bubble.text,kind:manifest.kind,memoryBatchId:manifest.delivery_id,expectedBubbles:manifest.expected_bubbles,
      ...(Object.hasOwn(manifest,'taskId')?{taskId:manifest.taskId}:{}),
      ...(Object.hasOwn(manifest,'inputVersion')?{inputVersion:manifest.inputVersion}:{}),
      ...(Object.hasOwn(manifest,'turnFence')?{turnFence:manifest.turnFence}:{}),
      references:bubble.references,draftId:bubble.draft_id,replyInputId:manifest.reply_id,state:kind==='canceled'?'not-submitted':kind,acceptedAt:at,checkedAt:at,
      ...(kind==='accepted'?{messageId:receipts[0]?.messageId}:{})},{channel:manifest.channel});
    return kind==='accepted'&&receipts.length>1?{...event,message_ids:receipts.map(r=>r.messageId)}:event;
  }
  /** Bubble-level side effects, at least once and in order: release a dead
   * bubble's reservation, then tell Python. Both are idempotent on the other
   * side; each is tried a bounded number of times. */
  async flush(manifest,context) {
    let changed=false;
    for(const bubble of manifest.bubbles) {
      const kind=EVENT_FOR[bubble.state];
      if(!kind)continue;
      const tries=bubble.side_effect_failures??0;
      if(tries>=this.retry.sideEffectAttempts||context.failedEffects.has(bubble.bubble_id))continue;
      try {
        if(kind==='canceled'&&this.cancelShare&&bubble.draft_id&&!bubble.share_canceled){await this.cancelShare(bubble.draft_id);bubble.share_canceled=true;changed=true;}
        if(this.emit&&!bubble.emitted?.[kind]) {
          const last=bubble.fragments.filter(f=>f.state==='accepted').at(-1)?.receipt;
          bubble.event_at={...bubble.event_at,[kind]:bubble.event_at?.[kind]??(kind==='accepted'?last?.acceptedAt:null)??bubble.state_at??iso(this.clock())};
          await this.emit(this.bubbleEvent(manifest,bubble,kind));
          bubble.emitted={...bubble.emitted,[kind]:true};changed=true;
        }
      } catch{bubble.side_effect_failures=tries+1;context.failedEffects.add(bubble.bubble_id);changed=true;}
    }
    if(changed)await this.save(manifest,context.lease);
  }
  /** Bubble-level side effects that are still due and still worth trying. */
  owes(manifest) {
    return manifest.bubbles.some(b=>{const kind=EVENT_FOR[b.state];
      return kind&&(b.side_effect_failures??0)<this.retry.sideEffectAttempts&&((this.emit&&!b.emitted?.[kind])||(kind==='canceled'&&this.cancelShare&&b.draft_id&&!b.share_canceled));});
  }
  /** Nothing more will happen to a settled terminal group: move it out of the live set. */
  async settle(manifest,context) {
    // The operator's tool has no memory host: what it settles is reported, released and filed by the service's next pass.
    if(this.role==='cli'||!terminal(manifest)||tailOpen(manifest)||this.owes(manifest)||this.keepLive(manifest))return;
    context.lease.assertHeld();
    const filed=this.doneFile(manifest.group_id);
    fs.mkdirSync(path.dirname(filed),{recursive:true,mode:0o700});
    fs.renameSync(this.file(manifest.group_id),filed);
    fs.rmSync(this.file(manifest.group_id)+'.prev',{force:true});
    removeLease({directory:path.join(this.directory,'leases'),id:manifest.group_id});
  }

  // ---- entry points ------------------------------------------------------

  /** Retire the unsent remainder now (owner stop, superseded, tail decision). */
  async retireRemainder(id,options){return this.run(id,{operator:(manifest,context)=>terminal(manifest)?null:this.retire(manifest,context,options)});}
  /** An interrupted group goes on as written: the same bubbles under the same
   * transport IDs. `ownerEpoch` is the epoch the decision was made in, so the
   * input that interrupted the group does not interrupt it a second time. */
  resumeAsWritten(manifest,{ownerEpoch,by=null}={}) {
    if(manifest.state==='interrupted')Object.assign(manifest,{state:manifest.review?'sending':'held',reason:null,retryAt:0});
    const known=ownerEpoch!==undefined&&ownerEpoch!==null;
    Object.assign(manifest,{continued:{at:this.clock(),by,...(known?{epoch:ownerEpoch}:{})},...(known?{ownerEpoch}:{})});this.unhold(manifest);
  }
  async continueGroup(id,options={}) {
    return this.run(id,{operator:async(manifest,context)=>{
      if(manifest.state!=='interrupted')return;
      this.resumeAsWritten(manifest,options);await this.touch(manifest,context);
    }});
  }
  /** Change manifest fields this module does not interpret (tail intents, links) under the lease. */
  async mutate(id,change,{operatorOnly=false}={}){return this.run(id,{operatorOnly,operator:async(manifest,context)=>{if(await change(manifest,context)!==false)await this.touch(manifest,context);}});}
  /** A new reason to ask again: a held or parked group gets a fresh, bounded set of review tries. */
  async unpark(id){return this.mutate(id,manifest=>{if(!manifest.parked&&!manifest.holds)return false;this.unhold(manifest);manifest.retryAt=0;},{operatorOnly:true});}
  /** Re-read receipts only; never sends. The exit from `blocked-unknown` once the transport's receipt can tell. */
  async reconcileGroup(id,{receipt}={}){return this.run(id,{transport:receipt?{receipt}:null});}
  /** An operator states what really happened to one unknown fragment. */
  async resolve(id,fragmentId,{outcome,messageId,at}={}) {
    if(!['accepted','rejected','not-submitted'].includes(outcome)||(outcome==='accepted'&&!messageId))throw Error('An operator outcome is accepted with a platform message ID, rejected, or proven not-submitted');
    return this.run(id,{operator:async(manifest,context)=>{
      const fragment=manifest.bubbles.flatMap(b=>b.fragments).find(f=>f.transport_id===fragmentId);
      if(!fragment||!['unknown','submitting'].includes(fragment.state))throw Error('Only a fragment with an unknown outcome can be resolved');
      if(outcome==='not-submitted') {
        const prior=normalizeReceipt(fragment.receipt),bubble=manifest.bubbles.find(b=>b.fragments.includes(fragment));
        if(!this.receipt||prior?.state!=='blocked'||prior.reason!=='turn-superseded-before-send'||prior.submissionStarted===true)
          throw Error('A stored before-send refusal and an external receipt reader are required');
        if(manifest.bubbles.length!==1||bubble.fragments.length!==1||tailOpen(manifest)||manifest.continues?.length||manifest.continuation)
          throw Error('Only an isolated single-fragment refusal can be retired as not-submitted');
        if(manifest.bubbles.some(b=>b.fragments.some(f=>f.state==='accepted')))throw Error('Accepted fragments must retain their partial delivery history');
        const external=classifyReceipt(await context.receipt(fragmentId));
        if(!['absent','never-started'].includes(external))throw Error('External receipt does not prove no submission');
        await this.cancelSuperseded(manifest,bubble,fragment,{state:'not-submitted',submissionStarted:false,reason:prior.reason,source:'operator'},context);
        return;
      }
      const stamp=at??iso(this.clock());
      Object.assign(fragment,{state:outcome,receipt:{state:outcome,source:'operator',...(outcome==='accepted'?{messageId:String(messageId),acceptedAt:stamp}:{checkedAt:stamp})}});
      this.refresh(manifest);await this.touch(manifest,context);
    }});
  }

  /** Resume every due group, oldest first. One unreadable or stuck group never
   * stops the others, and only groups that needed real work count against `limit`. */
  async resumeDue({guard,transport,review,limit=2}={}) {
    const groups=[];let handled=0;
    const due=this.live().map(id=>{try{return this.load(id);}catch{return null;}}).filter(Boolean).sort((a,b)=>a.created_at-b.created_at||a.group_id.localeCompare(b.group_id));
    for(const manifest of due) {
      if(manifest.state==='interrupted'||manifest.retryAt>this.clock())continue;
      if(manifest.state==='blocked-unknown'&&await this.stillBlocked(manifest,transport))continue;
      // A parked group costs nothing per tick: only a guard that wants it stopped gets it a pass.
      if(manifest.parked&&!await this.guardStops(manifest,guard))continue;
      if(handled>=limit)break;
      try {
        const result=await this.run(manifest.group_id,{guard,transport,review,resume:true});
        if(result.worked)handled++;
        groups.push({group_id:manifest.group_id,state:result.busy?'busy':result.lost?'lease-lost':result.manifest?.state??'missing'});
      } catch(error){groups.push({group_id:manifest.group_id,state:'failed',reason:error.name??'Error'});}
    }
    return {state:handled?'checked':'idle',checked:handled,groups};
  }

  async guardStops(manifest,guard) {
    if(!guard)return false;
    try{const answer=await guard(manifestView(manifest));return ['cancel','interrupt'].includes(typeof answer==='string'?answer:answer?.action);}catch{return false;}
  }

  /** True while no receipt of a blocked group says anything new and no resend is allowed. */
  async stillBlocked(manifest,transport) {
    if(this.owes(manifest))return false;
    const read=async id=>normalizeReceipt(await (transport?.receipt?.(id)??this.receipt?.(id)??null));
    for(const fragment of manifest.bubbles.flatMap(b=>b.fragments).filter(f=>f.state==='unknown')) {
      if(['accepted','rejected'].includes(classifyReceipt(await read(fragment.transport_id))))return false;
      if(typeof (transport?.send??transport)==='function'&&mayResend(this.contract(manifest),fragment,this.clock()))return false;
    }
    return true;
  }

  /** Keyed by group, so an older group's cancellation or continuation stays visible beside the current one. */
  status({limit=32}={}) {
    const live=this.live().map(id=>{try{return this.load(id);}catch{return null;}}).filter(Boolean);
    const done=[];
    for(const month of this.doneMonths()) {
      if(done.length>=limit)break;
      const folder=path.join(this.directory,'done',month);
      done.push(...fs.readdirSync(folder).filter(n=>n.endsWith('.json')).map(n=>({id:n.slice(0,-5),at:fs.statSync(path.join(folder,n)).mtimeMs})).sort((a,b)=>b.at-a.at));
    }
    const recent=done.slice(0,Math.max(0,limit-live.length)).map(d=>this.load(d.id)).filter(Boolean);
    const all=[...live,...recent].sort((a,b)=>b.updated_at-a.updated_at).slice(0,limit);
    return {groups:Object.fromEntries(all.map(m=>[m.group_id,manifestSummary(m)])),live:live.length,
      blocked:live.filter(m=>m.state==='blocked-unknown').map(m=>m.group_id),
      undelivered:all.filter(m=>m.bubbles.some(b=>['rejected','undeliverable'].includes(b.state))).map(m=>m.group_id)};
  }
  read(id){return this.load(id);}
  /** The live set first. With `settled`, the filed months are searched too, newest
   * first and bounded: a bubble whose group was already filed away is still the
   * evidence of what became of it, and the alternative is calling it unconfirmed
   * for ever. A miss only leaves the caller where it was. */
  findBubble(bubbleId,{settled=false,limit=400}={}) {
    for(const id of this.live()){const manifest=this.load(id),bubble=manifest?.bubbles.find(b=>b.bubble_id===bubbleId);if(bubble)return {manifest,bubble};}
    if(!settled)return null;
    let left=limit;
    for(const month of this.doneMonths()) {
      const directory=path.join(this.directory,'done',month);
      let names=[];
      try{names=fs.readdirSync(directory);}catch(error){if(error.code!=='ENOENT')throw error;}
      for(const name of names.filter(n=>n.endsWith('.json'))) {
        if(left--<=0)return null;
        const filed=readJsonFile(path.join(directory,name));
        const bubble=filed.state==='ok'?filed.value?.bubbles?.find(b=>b.bubble_id===bubbleId):null;
        if(bubble)return {manifest:filed.value,bubble};
      }
    }
    return null;
  }
  leaseState(id){return inspectLease({...this.lease,directory:path.join(this.directory,'leases'),id,clock:this.clock});}

  // ---- legacy `.pending.json` ------------------------------------------

  /** Import reply groups the old guard left unfinished, then mark the old file
   * `migrated` (a state its resume filter ignores). Groups that already ended
   * are never imported: no old reply is ever replayed. Imported bubbles keep
   * their bubble ID as transport ID, because that is where their receipts are. */
  importLegacy(directory) {
    const imported=[],skipped=[];let names=[];
    try{names=fs.readdirSync(directory).filter(n=>n.endsWith('.pending.json'));}catch(error){if(error.code!=='ENOENT')throw error;}
    for(const name of names) {
      const file=path.join(directory,name),read=readJsonFile(file),entry=read.value;
      if(read.state!=='ok'||!entry?.delivery?.id||!entry.request){if(read.state!=='ok')skipped.push(name);continue;}
      if(!['pending','prepared','unconfirmed'].includes(entry.state))continue;
      const items=entry.entries??[{request:entry.request,delivery:entry.delivery,state:entry.state==='unconfirmed'?'unconfirmed':'unsent',receipt:entry.receipt}];
      if(!items.length||items.some(item=>!item?.request||!item.delivery?.id)){skipped.push(name);continue;}
      const ids=items.map(item=>item.delivery.id),now=this.clock();
      let id=groupIdFor(items).startsWith('group-')?'legacy-'+sha256(name).slice(0,48):groupIdFor(items);
      // Two old files of one batch stay two groups; neither hides the other's bubble.
      const holder=this.load(id);
      if(holder&&!ids.every(bubbleId=>holder.bubbles.some(b=>b.bubble_id===bubbleId)))id=(id+'-b'+sha256(ids.join('\0')).slice(0,12)).slice(-160);
      if(!this.load(id)) {
        const reviewed=Boolean(entry.review||entry.checked);
        const bubbles=items.map((item,order)=>{
          const text=item.delivery.text??item.request.text,body=sha256(text),state={unconfirmed:'submitting',accepted:'accepted'}[item.state]??'unsent';
          const fragments=reviewed||state!=='unsent'?[{transport_id:item.delivery.id,index:0,start:0,end:text.length,body_sha256:body,kind:'text',state,receipt:normalizeReceipt(item.receipt)}]:[];
          // The old sender already told Python about a bubble it saw accepted.
          return {bubble_id:item.delivery.id,draft_id:item.request.draft_id,order,request:item.request,text,body_sha256:body,references:item.delivery.references??[],legacy:true,
            state:'unsent',fragments,...(state==='accepted'?{emitted:{accepted:true}}:{})};
        });
        const firstDelivery=items[0].delivery;
        const manifest={schema:MANIFEST_SCHEMA,group_id:id,delivery_id:firstDelivery.memoryBatchId??id,reply_id:entry.request.reply_id??null,channel:'feishu',kind:firstDelivery.kind??'reply',
          work:Boolean(entry.request.work),
          ...(Object.hasOwn(firstDelivery,'taskId')?{taskId:firstDelivery.taskId}:{}),
          ...(executionNumber(firstDelivery.inputVersion)?{inputVersion:firstDelivery.inputVersion}:{}),
          ...(executionNumber(firstDelivery.turnFence)?{turnFence:firstDelivery.turnFence}:{}),
          ...(Object.hasOwn(firstDelivery,'sessionFence')?{sessionFence:firstDelivery.sessionFence}:{}),ownerEpoch:entry.ownerEpoch,
          expected_bubbles:items[0].delivery.expectedBubbles??items.length,state:reviewed?'reviewed':'held',reason:reviewed?null:entry.reason??'share-review-pending',
          leaseGeneration:0,revision:0,review:reviewed?{id:entry.review?.review_id??null,checked_hashes:bubbles.map(b=>b.body_sha256)}:null,
          created_at:entry.at??now,updated_at:now,retryAt:entry.retryAt??0,failures:0,origin:'legacy-import',bubbles};
        this.refresh(manifest);
        createJsonExclusive(this.file(id),manifest);
      }
      writeJsonAtomic(file,{...entry,state:'migrated',migratedTo:id,migratedAt:now},{previous:false});
      imported.push(id);
    }
    return {imported,skipped};
  }
}

/** Operator entry point: look at groups, re-read receipts, or state what
 * happened to a fragment nobody can tell about. It never sends and never
 * prints message text. */
export async function operatorMain(argv,{clock=()=>Date.now(),print=console.log}={}) {
  const [command,...rest]=argv,option=name=>{const index=rest.indexOf('--'+name);return index<0?undefined:rest[index+1];};
  const directory=option('dir');
  if(!directory||!['status','reconcile','resolve','retry','continue','retire'].includes(command))throw Error('Usage: status|reconcile|resolve|retry|continue|retire --dir <reply-manifests> [--receipts <dir,dir>] [--group <id>] [--fragment <transport id> --outcome accepted|rejected|not-submitted --message-id <id>] [--reason <token>]');
  const receipts=option('receipts')?fileReceipts(option('receipts').split(',')):undefined;
  const manifests=new TransportManifests({directory,clock,role:'cli',receipt:receipts,lease:{heartbeat:false}});
  const show=result=>result.busy?{state:'busy'}:result.lost?{state:'lease-lost'}:result.missing?{state:'missing'}:manifestSummary(result.manifest);
  if(command==='status')return print(JSON.stringify(manifests.status(),null,2));
  // The operator is a new reason: a group parked after its review tries ran out is asked about again by the service.
  if(command==='retry')return print(JSON.stringify(show(await manifests.unpark(option('group'))),null,2));
  // The operator's own exits for a group that waits for a tail decision nobody will make (the switch was turned off, the model stays away).
  if(command==='continue')return print(JSON.stringify(show(await manifests.continueGroup(option('group'))),null,2));
  if(command==='retire')return print(JSON.stringify(show(await manifests.retireRemainder(option('group'),{reason:/^[a-z0-9-]{1,64}$/.test(option('reason')??'')?option('reason'):'operator-retired'})),null,2));
  if(command==='resolve')return print(JSON.stringify(show(await manifests.resolve(option('group'),option('fragment'),{outcome:option('outcome'),messageId:option('message-id')})),null,2));
  const ids=option('group')?[option('group')]:manifests.live();
  const results={};
  for(const id of ids)results[id]=show(await manifests.reconcileGroup(id));
  return print(JSON.stringify(results,null,2));
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)await operatorMain(process.argv.slice(2));
