/** What becomes of the unsent rest of a reply when the owner writes again
 * before it is out.
 *
 *  - Nothing is cancelled because a message arrived. The group is interrupted
 *    at its next bubble boundary and waits for a decision.
 *  - The decision is DeepSeek's and rides on a call that is made anyway: the
 *    routing call for the new message, else the review of the next reply
 *    (`remainder` in, `covers_remainder` out). It gets a call of its own only
 *    when neither ran. A literal owner stop needs no model at all.
 *  - A group with a fragment whose outcome is unknown is never offered to
 *    anyone: it blocks itself and nothing else.
 *  - The intent is written before anything is done, and recovery rolls it
 *    forward: recorded → applied (every named bubble retired, its reservation
 *    released, a canceled event sent) → linked (the next group carries
 *    `continues_reply_id`; the old bubbles' `superseded_by` moves from the
 *    intent to that group) → settled (by what that group really delivered).
 *  - "Into the next reply" is an intention, not a delivery. What the next
 *    reply did not cover comes back, at most `maxResurface` times; after that
 *    the only answers left are continue and supersede. What DeepSeek
 *    superseded or the owner stopped never comes back.
 * Everything durable lives in the manifests, written through
 * `TransportManifests` under its lease. This module's only file of its own is
 * `<manifests>/tail/stops.json`, the owner's literal stop commands. */
import path from 'node:path';
import {createHash} from 'node:crypto';
import {readJsonFile,writeJsonAtomic} from './atomic-json.mjs';
import {TERMINAL_GROUP_STATES,groupIdFor,legacyState,manifestView} from './transport-manifest.mjs';
import {TAIL_DECISIONS} from './mobile-reviewer.mjs';

export {TAIL_DECISIONS};
export const FORCED_TAIL_DECISIONS=Object.freeze(['continue','supersede']);
export const TAIL_INTENT_STATES=Object.freeze(['recorded','applied','linked','settled']);
export const TAIL_DEFAULTS=Object.freeze({maxResurface:2,dedicatedAfterMs:120000,decisionAttempts:5,decisionBaseMs:60000,decisionMaxMs:15*60000,
  linkWaitMs:10*60000,stopsKept:8,stopsKeptMs:7*86400000,view:Object.freeze({sent:4,unconfirmed:3,unsent:6,excerpt:280,message:1200})});

const sha=value=>createHash('sha256').update(value).digest('hex');
const terminal=manifest=>TERMINAL_GROUP_STATES.includes(manifest.state);
const begun=bubble=>bubble.fragments.some(f=>f.state!=='unsent');
const open=bubble=>!['accepted','rejected','undeliverable','canceled'].includes(bubble.state);
const itemId=bubble=>bubble.draft_id??bubble.bubble_id;
const unsentOf=manifest=>manifest.bubbles.filter(b=>open(b)&&!begun(b));
const midway=manifest=>manifest.bubbles.some(b=>open(b)&&begun(b));
const unknownIn=manifest=>manifest.bubbles.some(b=>b.fragments.some(f=>['unknown','submitting'].includes(f.state)));
const unsettled=manifest=>Boolean(manifest.tail_intent&&manifest.tail_intent.state!=='settled');
const cut=(text,limit)=>text.length>limit?{text:text.slice(0,limit),complete:false}:{text};
const actionOf=answer=>typeof answer==='string'?answer:answer?.action;

export class ReplyTail {
  /** `decide(view)` is the call of its own (the host wires the reviewer's `tail`);
   * `ownerEpoch()` is the host's current owner epoch; `onEvent` receives
   * text-free status facts keyed by group; `onWake` says a group became sendable. */
  constructor({manifests,clock=()=>Date.now(),decide=null,ownerEpoch=null,onEvent=()=>{},onWake=()=>{},hooks={},limits={}}) {
    Object.assign(this,{manifests,clock,decide,ownerEpoch,onEvent,onWake,hooks});
    this.limits={...TAIL_DEFAULTS,...limits,view:{...TAIL_DEFAULTS.view,...limits.view}};
    this.latest=null;this.queued=new Map();this.chain=Promise.resolve();
  }

  // ---- reading -----------------------------------------------------------

  groups() {
    return this.manifests.live().map(id=>{try{return this.manifests.read(id);}catch{return null;}})
      .filter(m=>m&&m.kind==='reply').sort((a,b)=>a.created_at-b.created_at||a.group_id.localeCompare(b.group_id));
  }
  idle(manifest){return !['held','claiming'].includes(this.manifests.leaseState(manifest.group_id).state);}
  /** The remainder nobody has decided about: what has not begun, and what came back uncovered. */
  remainder(manifest){const owed=new Set(manifest.tail_owed?.items??[]);return manifest.bubbles.filter(b=>owed.has(itemId(b))||(open(b)&&!begun(b)));}
  /** Something is still unsent or owed, nothing about it is unknown or mid-way, and no decision is in flight. */
  undecided(manifest) {
    if(unsettled(manifest)||unknownIn(manifest)||midway(manifest))return false;
    return (!terminal(manifest)&&unsentOf(manifest).length>0)||Boolean(manifest.tail_owed?.items?.length);
  }
  /** Waiting for a decision. */
  awaiting(manifest){return this.undecided(manifest)&&(manifest.state==='interrupted'||Boolean(manifest.tail_owed?.items?.length));}
  /** Not interrupted yet, but it will be at its next pass and nothing stands between it and that pass. */
  interruptible(manifest) {
    return !manifest.work&&['draft','reviewed','sending','held'].includes(manifest.state)&&this.undecided(manifest)&&
      Boolean(this.latest)&&this.latest.at>Math.max(manifest.created_at,manifest.continued?.at??0)&&this.idle(manifest);
  }
  /** A newer reply that answers for this remainder and whose outcome is still open. One that
   * is itself blocked on an unknown fragment blocks only itself. */
  covering(manifest,groups) {
    return groups.some(g=>g.group_id!==manifest.group_id&&!g.continuation&&g.state!=='blocked-unknown'&&(g.continues??[]).some(c=>c.group_id===manifest.group_id&&!c.done));
  }
  candidates({virtual=false}={}) {
    const groups=this.groups();
    return groups.filter(m=>(this.awaiting(m)||(virtual&&this.interruptible(m)))&&!this.covering(m,groups)&&!this.stopFor(m)).sort((a,b)=>b.created_at-a.created_at);
  }
  /** Changes whenever anything about what was or was not sent changes: a decision made about one view never lands on another. */
  key(manifest) {
    return manifest.group_id+':'+sha(JSON.stringify([manifest.bubbles.map(b=>[b.state,b.fragments.map(f=>f.state)]),manifest.tail_owed?.items??[],
      manifest.tail_owed?.resurfaced??0,Boolean(manifest.tail_owed?.forced),manifest.tail_intent?.id??null])).slice(0,16);
  }
  allowed(manifest){return manifest.tail_owed?.forced?FORCED_TAIL_DECISIONS:TAIL_DECISIONS;}
  /** What DeepSeek sees: excerpts and receipts, no identifiers. */
  view(manifest) {
    const v=this.limits.view,rest=this.remainder(manifest),sent=manifest.bubbles.filter(b=>b.state==='accepted'),lost=manifest.bubbles.filter(b=>['rejected','undeliverable'].includes(b.state));
    return {reason:manifest.tail_owed?.items?.length?'remainder-not-covered':manifest.state==='interrupted'?manifest.reason??'new-owner-input':'new-owner-input',
      sent:sent.slice(-v.sent).map(b=>{const receipt=b.fragments.find(f=>f.state==='accepted')?.receipt??{};return {...cut(b.text,v.excerpt),receipt:{messageId:receipt.messageId??null,acceptedAt:receipt.acceptedAt??null}};}),
      unconfirmed:lost.slice(0,v.unconfirmed).map(b=>({...cut(b.text,v.excerpt),state:b.state})),
      unsent:rest.slice(0,v.unsent).map(b=>cut(b.text,v.excerpt)),
      ...(sent.length>v.sent||rest.length>v.unsent?{omitted:{sent:Math.max(0,sent.length-v.sent),unsent:Math.max(0,rest.length-v.unsent)}}:{}),
      ...(manifest.tail_owed?.resurfaced?{resurfaced:manifest.tail_owed.resurfaced}:{}),decisions:[...this.allowed(manifest)]};
  }
  facts(intent) {
    return {decision:intent.decision,carrier:intent.carrier,intentId:intent.id,intentState:intent.state,newInputId:intent.new_input_id??null,round:intent.round,
      ...(intent.linked_group?{linkedGroup:intent.linked_group}:{}),...(intent.outcome?{outcome:intent.outcome.state}:{})};
  }
  report(manifest,tail) {
    try {
      if(typeof manifest==='string')manifest=this.manifests.read(manifest);
      if(manifest)this.onEvent({groupId:manifest.group_id,inputId:manifest.reply_id,state:legacyState(manifest),groupState:manifest.state,reason:manifest.reason??undefined,tail});
    } catch{/* Status reporting never decides delivery. */}
  }
  /** The newest owner message, in memory only: what interrupts a group that is being sent right now. */
  note({id=null,text=null}={}){if(id===null||this.latest?.id!==id)this.latest={id,text:typeof text==='string'?text:null,at:this.clock()};}
  serial(work){const next=this.chain.then(()=>work());this.chain=next.catch(()=>{});return next;}
  /** Resolves when everything started so far has been done. */
  idleNow(){return this.chain;}

  // ---- the router's port: quick, local, never throwing --------------------

  /** The interrupted reply the routing call for this message should carry, if there is one. Read-only. */
  pending({id,text}={}) {
    try {
      this.note({id,text});
      const manifest=this.candidates({virtual:true})[0];
      return manifest?{key:this.key(manifest),reply:this.view(manifest)}:null;
    } catch{return null;}
  }
  /** DeepSeek's answer from the routing call. Only the intent is written here; releasing reservations happens behind the owner's message. */
  async decided({inputId=null,key,tail}={}) {
    const groupId=String(key??'').split(':')[0];
    try {
      const manifest=this.manifests.read(groupId);
      if(!manifest||this.key(manifest)!==key)return this.miss(groupId,inputId,'group-changed');
      if(!this.allowed(manifest).includes(tail?.decision))return this.miss(groupId,inputId,'decision-not-allowed');
      const intent=this.intent(manifest,{decision:tail.decision,carrier:'classify',receipt:tail.receipt??null,newInputId:inputId,basis:{reason:String(tail.reason??'').slice(0,300)}});
      const state=await this.record(groupId,intent,{key});
      if(state==='busy'){this.queued.set(groupId,{intent,key});return {state:'queued',groupId,intentId:intent.id};}
      if(state!=='recorded')return this.miss(groupId,inputId,state);
      void this.serial(()=>this.advance(groupId)).catch(()=>{});
      return {state:'recorded',groupId,intentId:intent.id};
    } catch{return {state:'failed',groupId};}
  }
  /** No routing call carried a decision (timeout, attachment, command). The remainder waits for its next carrier. */
  missed({inputId=null,key=null,reason='not-classified'}={}) {
    try{this.note({id:inputId});return key?this.miss(String(key).split(':')[0],inputId,reason):{state:'missed',reason};}catch{return {state:'missed',reason};}
  }
  miss(groupId,inputId,reason){this.report(groupId,{event:'decision-missed',reason,newInputId:inputId});return {state:'missed',groupId,reason};}
  /** The owner's literal stop. Durable before anything else, so a restart cannot lose it; no model is asked. */
  async stopped({inputId=null}={}) {
    try {
      this.note({id:inputId});
      const now=this.clock(),kept=this.stops().filter(s=>s.input_id!==inputId&&now-s.at<this.limits.stopsKeptMs);
      writeJsonAtomic(this.stopsFile(),{stops:[...kept,{input_id:inputId,at:now}].slice(-this.limits.stopsKept)},{previous:false});
      void this.serial(()=>this.advanceAll()).catch(()=>{});
      return {state:'recorded'};
    } catch{return {state:'failed'};}
  }
  stopsFile(){return path.join(this.manifests.directory,'tail','stops.json');}
  stops(){const read=readJsonFile(this.stopsFile());return read.state==='ok'&&Array.isArray(read.value?.stops)?read.value.stops.filter(s=>Number.isFinite(s?.at)):[];}
  /** The stop command this group has not answered yet: given after the group was written. */
  stopFor(manifest){return this.stops().findLast(s=>s.at>=manifest.created_at&&s.at>(manifest.tail_stop?.at??-1))??null;}

  // ---- the guard: what a new message does to a group that is being sent ---

  /** Wraps the host's guard. A bare 'cancel' used to mean "a newer message arrived"; it now
   * interrupts. An explicit `{action:'cancel',reason}` still cancels. */
  guard(inner=async()=>'send') {
    return async view=>{
      const stop=this.stops().findLast(s=>s.at>=(view.createdAt??Infinity));
      if(stop)return {action:'interrupt',reason:'owner-stop',inputId:stop.input_id};
      const answer=await inner(view);
      const newer=this.latest&&!view.request?.work&&this.latest.at>Math.max(view.createdAt??0,view.continuedAt??0)?this.latest:null;
      if(answer==='cancel') {
        // It was already ruled (by DeepSeek, or by the operator) that this remainder goes on. Only something newer than
        // that ruling reopens the question: a message noted since, or an owner epoch that moved since.
        const epoch=this.ownerEpoch?.(),moved=view.continuedEpoch!==undefined&&epoch!==undefined&&epoch!==null&&epoch!==view.continuedEpoch;
        if(view.continuedAt&&!newer&&!moved)return 'send';
        return {action:'interrupt',reason:'input-or-session-superseded',inputId:this.latest?.id??null};
      }
      if(actionOf(answer)==='send'&&newer)return {action:'interrupt',reason:'new-owner-input',inputId:newer.id};
      return answer;
    };
  }

  // ---- the next group -----------------------------------------------------

  /** Older groups whose remainder the group about to be created answers for. */
  continuesFor(entries) {
    try {
      const own=groupIdFor(entries);
      // Waiting for a decision (or about to), withdrawn and not yet pointed at a successor, or about to be withdrawn by an owner stop.
      return this.groups().filter(m=>m.group_id!==own&&(this.awaiting(m)||this.interruptible(m)||(!terminal(m)&&!unsettled(m)&&this.stopFor(m)&&this.remainder(m).length>0)||
        (unsettled(m)&&m.tail_intent.decision!=='continue'&&!m.tail_intent.linked_group&&['recorded','applied'].includes(m.tail_intent.state))))
        .reverse().map(m=>({group_id:m.group_id,intent_id:unsettled(m)?m.tail_intent.id:null}));          // newest first: `continues_reply_id` names the reply that was cut off last
    } catch{return [];}
  }
  /** What the review of that group is asked to look for. Item IDs are the old bubbles' draft
   * IDs, which is what lets the review hand a covered item's reservation over. */
  remainderFor(groupId) {
    try {
      const group=this.manifests.read(groupId);
      if(!group?.continues?.length||group.continuation)return null;
      const items=[];let first=null;
      for(const link of group.continues) {
        const old=this.manifests.read(link.group_id),intent=old?.tail_intent;
        if(!old||this.stopFor(old))continue;               // what the owner stopped is not owed to anybody
        const wanted=new Set([...(unsettled(old)&&intent.decision==='rewrite_remainder'&&!intent.stopped?intent.items:[]),...(old.tail_owed?.items??[]),
          ...(this.undecided(old)?this.remainder(old).map(itemId):[])]);
        for(const bubble of old.bubbles)if(wanted.has(itemId(bubble))&&items.length<64){items.push({id:itemId(bubble),text:bubble.text,references:bubble.references??[]});first??=old.group_id;}
      }
      return items.length?{reply_id:first,items}:null;
    } catch{return null;}
  }
  /** A new group exists: links that were waiting for it are made now. */
  linkNew(draft){return this.serial(async()=>{for(const link of draft?.continues??[])await this.advance(link.group_id);});}
  /** Roll every unsettled intent forward as far as it goes now. Never asks a model. */
  recover(){return this.serial(()=>this.advanceAll());}
  /** After a pass over a group: everything that pass made possible. */
  after(result){return result?.manifest?this.recover():Promise.resolve();}
  /** The periodic pass: roll forward, settle, and — bounded — ask on its own when nothing else could. */
  tick({guard=null}={}){return this.serial(()=>this.advanceAll({dedicated:true,guard}));}
  /** For the next turn's context: what was withdrawn in favour of the next reply and is not covered yet.
   * Carries reply text; it is for the owner's own session, never for status or logs. */
  owed() {
    return this.groups().flatMap(m=>{
      const intent=m.tail_intent,ids=new Set([...(unsettled(m)&&intent.decision==='rewrite_remainder'&&!intent.stopped?intent.items:[]),...(m.tail_owed?.items??[])]);
      return ids.size?[{group_id:m.group_id,reply_id:m.reply_id,resurfaced:m.tail_owed?.resurfaced??intent?.resurfaced??0,
        sent:m.bubbles.filter(b=>b.state==='accepted').map(b=>b.text),unsent:m.bubbles.filter(b=>ids.has(itemId(b))).map(b=>b.text)}]:[];
    });
  }
  /** Text-free. */
  status() {
    const groups=this.groups(),all={};
    for(const m of groups) {
      const intent=m.tail_intent,waits=this.awaiting(m)?(this.stopFor(m)?'owner-stop':this.covering(m,groups)?'next-reply':'decision'):unsettled(m)?{recorded:'retirement',applied:'link',linked:'settlement'}[intent.state]:null;
      if(!waits&&!intent&&!m.continues_reply_id&&!m.parked)continue;
      all[m.group_id]={state:m.state,reason:m.reason??null,awaiting:waits,...(intent?{intent:this.facts(intent)}:{}),
        ...(m.tail_owed?.items?.length?{owed:{items:m.tail_owed.items.length,resurfaced:m.tail_owed.resurfaced??0,forced:Boolean(m.tail_owed.forced)}}:{}),
        ...(m.continues_reply_id?{continues_reply_id:m.continues_reply_id}:{}),...(m.tail_wait?{decision_attempts:m.tail_wait.attempts}:{}),...(m.parked?{parked:m.parked}:{})};
    }
    return {groups:all,awaiting:Object.keys(all).filter(id=>all[id].awaiting==='decision'),stops:this.stops().length};
  }

  // ---- the intent sequence ------------------------------------------------

  intent(manifest,{decision,carrier,receipt=null,newInputId=null,basis=null,items=null,linkedGroup=null}) {
    const round=(manifest.tail_round??0)+1;
    return {id:'tail-'+sha([manifest.group_id,round,carrier,newInputId??'',decision].join('\0')).slice(0,32),decision,carrier,receipt,new_input_id:newInputId,state:'recorded',
      at:this.clock(),round,items:items??this.remainder(manifest).map(itemId),resurfaced:manifest.tail_owed?.resurfaced??0,...(basis?{basis}:{}),...(linkedGroup?{linked_group:linkedGroup}:{})};
  }
  /** Step 1: the decision is on disk before anything is done about it. 'recorded' | 'changed' | 'busy' | 'missing'. */
  async record(groupId,intent,{key=null,also=null}={}) {
    let outcome='changed';
    const result=await this.manifests.run(groupId,{operatorOnly:true,operator:async(manifest,context)=>{
      if(unsettled(manifest)||(key&&this.key(manifest)!==key))return;
      if(manifest.tail_intent)manifest.tail_history=[...(manifest.tail_history??[]),manifest.tail_intent].slice(-8);
      Object.assign(manifest,{tail_intent:intent,tail_round:intent.round});delete manifest.tail_wait;
      // What this decision takes over is no longer an open obligation of its own.
      const rest=(manifest.tail_owed?.items??[]).filter(item=>!intent.items.includes(item));
      if(rest.length)manifest.tail_owed={...manifest.tail_owed,items:rest};else delete manifest.tail_owed;
      also?.(manifest);
      await this.manifests.touch(manifest,context);outcome='recorded';
      await this.hooks.afterStep?.('recorded',{groupId,intent});
    }});
    if(result.busy||result.lost)return 'busy';
    if(result.missing)return 'missing';
    if(outcome==='recorded')this.report(result.manifest,{event:'decision',...this.facts(intent)});
    return outcome;
  }
  /** Roll one group's intent as far forward as it can go now. */
  async advance(groupId) {
    for(let step=0;step<4;step++) {
      const before=this.manifests.read(groupId)?.tail_intent;
      if(!before||before.state==='settled')return;
      if(before.state==='recorded')await this.apply(groupId);
      else if(before.state==='applied')await this.link(groupId);
      else await this.settle(groupId);
      const after=this.manifests.read(groupId)?.tail_intent;
      if(!after||after.id!==before.id||after.state===before.state)return;
    }
  }
  /** `continue` can simply go on while every named bubble is still unsent and the frozen review does not refuse it. */
  inPlace(manifest,intent) {
    return intent.decision==='continue'&&!terminal(manifest)&&manifest.reason!=='frozen-remainder-needs-new-group'&&
      intent.items.every(item=>manifest.bubbles.some(b=>itemId(b)===item&&open(b)&&!begun(b)));
  }
  /** Step 2. `continue`: the old group goes on, same bubbles, same transport IDs. Anything else: retire the
   * named bubbles — `share-cancel` for every draft ID, then the canceled events; both repeatable. */
  async apply(groupId) {
    let done=null;
    const result=await this.manifests.run(groupId,{operator:async(manifest,context)=>{
      const intent=manifest.tail_intent;
      if(intent?.state!=='recorded')return;
      if(!intent.stopped&&this.inPlace(manifest,intent)) {
        this.manifests.resumeAsWritten(manifest,{ownerEpoch:this.ownerEpoch?.(),by:intent.new_input_id});
        Object.assign(intent,{state:'settled',settled_at:this.clock(),outcome:{state:'continued'}});
        await this.manifests.touch(manifest,context);done='settled';
        await this.hooks.afterStep?.('settled',{groupId,intent});return;
      }
      await this.manifests.retire(manifest,context,{reason:intent.carrier==='owner-stop'||intent.stopped?'owner-stop':'tail-'+intent.decision,superseded_by:intent.id,only:intent.items});
      await this.manifests.flush(manifest,context);
      if(this.manifests.owes(manifest))return;            // a reservation or an event is still owed: the next pass tries again
      // A remainder that came back was retired rounds ago: it answers to this intent now, until the link names its successor.
      for(const bubble of manifest.bubbles)if(bubble.state==='canceled'&&intent.items.includes(itemId(bubble)))bubble.superseded_by=intent.id;
      Object.assign(intent,{state:'applied',applied_at:this.clock()});
      await this.manifests.touch(manifest,context);done='applied';
      await this.hooks.afterStep?.('applied',{groupId,intent});
    }});
    if(done)this.report(result.manifest,{event:done==='settled'?'continued':'applied',...this.facts(result.manifest.tail_intent)});
    if(done==='settled')try{this.onWake(groupId);}catch{/* The periodic resume sends it anyway. */}
  }
  /** `continue` for bubbles that can no longer go on in place: the same words as a group of their own, reviewed afresh. */
  continuation(manifest,intent) {
    const items=manifest.bubbles.filter(b=>intent.items.includes(itemId(b)));
    if(!items.length)return null;
    const batch=manifest.delivery_id+'-c'+intent.round;
    const entries=items.map(bubble=>{
      const draft=itemId(bubble)+'-c'+intent.round;
      return {request:{...bubble.request,draft_id:draft,text:bubble.text},delivery:{id:'kin-cont-'+sha(bubble.bubble_id+'\0'+intent.round).slice(0,48),text:bubble.text,kind:manifest.kind,
        draftId:draft,memoryBatchId:batch,expectedBubbles:items.length,references:bubble.references??[],
        ...(Object.hasOwn(manifest,'taskId')?{taskId:manifest.taskId}:{}),
        ...(Object.hasOwn(manifest,'inputVersion')?{inputVersion:manifest.inputVersion}:{}),
        ...(Object.hasOwn(manifest,'turnFence')?{turnFence:manifest.turnFence}:{})}};
    });
    return this.manifests.createDraft({entries,ownerEpoch:this.ownerEpoch?.()??manifest.ownerEpoch,channel:manifest.channel,
      continues:[{group_id:manifest.group_id,intent_id:intent.id}],continuation:{of:manifest.group_id,intent_id:intent.id,items:items.map(itemId)}});
  }
  /** Step 3: the old bubbles' `superseded_by` moves from the intent to the group that answers for them. */
  async link(groupId) {
    const manifest=this.manifests.read(groupId),intent=manifest?.tail_intent;
    if(intent?.state!=='applied')return;
    let target=intent.linked_group??null,woke=false;
    // A stop that arrived after "continue" outranks it: nothing is written out again.
    if(!target&&intent.decision==='continue'&&intent.stopped)return this.finish(groupId,'applied',{state:'superseded',linked:false});
    if(!target&&intent.decision==='continue'){target=this.continuation(manifest,intent)?.group_id??null;woke=Boolean(target);}
    target??=this.groups().filter(g=>g.group_id!==groupId&&!g.continuation&&g.created_at>=intent.at&&(g.continues??[]).some(c=>c.group_id===groupId))[0]?.group_id??null;
    if(!target) {
      // Nothing has answered yet. A withdrawn remainder does not wait for ever to be pointed at a successor.
      if(intent.decision!=='rewrite_remainder'&&this.clock()-(intent.applied_at??intent.at)>=this.limits.linkWaitMs)await this.finish(groupId,'applied',{state:'superseded',linked:false});
      return;
    }
    let linked=false;
    const result=await this.manifests.mutate(groupId,current=>{
      const now=current.tail_intent;
      if(now?.id!==intent.id||now.state!=='applied')return false;
      for(const bubble of current.bubbles)if(bubble.superseded_by===now.id)bubble.superseded_by=target;
      if(current.retired?.superseded_by===now.id)current.retired.superseded_by=target;
      Object.assign(now,{state:'linked',linked_group:target,linked_at:this.clock()});linked=true;
    });
    if(!linked)return;
    this.report(result.manifest,{event:'linked',...this.facts(result.manifest.tail_intent)});
    if(woke)try{this.onWake(target);}catch{/* The periodic resume sends it anyway. */}
    await this.hooks.afterStep?.('linked',{groupId,intent:result.manifest.tail_intent});
  }
  /** Step 4: only what the linked group really delivered settles anything. */
  async settle(groupId) {
    const manifest=this.manifests.read(groupId),intent=manifest?.tail_intent;
    if(intent?.state!=='linked')return;
    const target=this.manifests.read(intent.linked_group);
    if(target&&!terminal(target))return;
    const accepted=new Set((target?.bubbles??[]).filter(b=>b.state==='accepted').map(itemId)),covered={};
    if(intent.decision==='continue')(target?.continuation?.items??[]).forEach((item,index)=>{const bubble=target.bubbles[index];if(bubble?.state==='accepted')covered[item]=itemId(bubble);});
    else if(intent.decision==='rewrite_remainder')for(const claim of target?.review?.covers_remainder??[])
      if(intent.items.includes(claim.item_id)&&accepted.has(claim.covered_by))covered[claim.item_id]??=claim.covered_by;
    const missing=intent.items.filter(item=>!covered[item]),returns=intent.decision==='rewrite_remainder'&&!intent.stopped;
    const outcome=intent.decision==='supersede'||intent.stopped?{state:'superseded'}:
      {state:missing.length?(returns?'owed':'undelivered'):(intent.decision==='continue'?'continued':'covered'),covered:Object.keys(covered),...(missing.length?{[returns?'owed':'undelivered']:missing}:{})};
    await this.finish(groupId,'linked',outcome,{covered,owed:returns?missing:[]});
    if(target&&this.manifests.isLive(target.group_id))await this.release(target);
  }
  async finish(groupId,from,outcome,{covered={},owed=[]}={}) {
    let settled=false;
    const result=await this.manifests.mutate(groupId,manifest=>{
      const intent=manifest.tail_intent,now=this.clock();
      if(intent?.state!==from)return false;
      for(const bubble of manifest.bubbles)if(covered[itemId(bubble)])bubble.covered_by=covered[itemId(bubble)];
      Object.assign(intent,{state:'settled',settled_at:now,outcome});settled=true;
      if(!owed.length)return;
      // It comes back, a bounded number of times. After that it can only be continued or superseded.
      const count=intent.resurfaced??0,forced=count>=this.limits.maxResurface||Boolean(manifest.tail_owed?.forced);
      manifest.tail_owed={items:[...new Set([...(manifest.tail_owed?.items??[]),...owed])],resurfaced:Math.max(manifest.tail_owed?.resurfaced??0,forced?count:count+1),forced,since:now};
    });
    if(!settled)return;
    const facts=this.facts(result.manifest.tail_intent),again=result.manifest.tail_owed;
    this.report(result.manifest,{event:'settled',...facts});
    if(owed.length)this.report(result.manifest,{event:again.forced?'choice-required':'resurfaced',...facts,resurfaced:again.resurfaced,forced:again.forced});
    await this.hooks.afterStep?.('settled',{groupId,intent:result.manifest.tail_intent});
  }

  // ---- the periodic pass --------------------------------------------------

  async advanceAll({dedicated=false,guard=null}={}) {
    const summary={advanced:0,dedicated:0};
    for(const [groupId,queued] of [...this.queued]) {
      let state='changed';
      try{state=await this.record(groupId,queued.intent,{key:queued.key});}catch{/* Dropped: the remainder waits for its next carrier. */}
      if(state!=='busy')this.queued.delete(groupId);
    }
    // One group never stops the others; whatever failed is tried again by the next pass.
    for(const manifest of this.groups())try {
      await this.answerStop(manifest);
      if(unsettled(this.manifests.read(manifest.group_id)??manifest)){await this.advance(manifest.group_id);summary.advanced++;}
    } catch{/* next pass */}
    for(const group of this.groups())try{if(group.review?.covers_remainder?.length&&!group.review.coverage_applied)await this.applyCoverage(group);}catch{/* next pass */}
    for(const group of this.groups())try{await this.release(group);}catch{/* next pass */}
    if(dedicated)try{summary.dedicated=await this.askAlone(guard);}catch{/* next pass */}
    return summary;
  }
  /** The owner's stop: what is still unsent is withdrawn for good, and what had come back stays away. */
  async answerStop(manifest) {
    const stop=this.stopFor(manifest);
    if(!stop||(terminal(manifest)&&!manifest.tail_owed&&!unsettled(manifest)))return;
    const mark=current=>{current.tail_stop={input_id:stop.input_id,at:stop.at};};
    if(unsettled(manifest)) {
      // Whatever was decided before, the stop outranks it: a remainder promised to the next reply is no longer owed, one that was to go on does not.
      await this.manifests.mutate(manifest.group_id,current=>{mark(current);if(unsettled(current))current.tail_intent.stopped={input_id:stop.input_id,at:stop.at};},{operatorOnly:true});
      return;
    }
    const items=this.remainder(manifest).map(itemId);
    if(!items.length){await this.manifests.mutate(manifest.group_id,mark,{operatorOnly:true});return;}
    if(!this.idle(manifest))return;                       // being sent right now: the guard stops it at the next bubble boundary
    // Decided when the owner said it, not when the host got round to it: the reply written after the stop is its successor.
    const intent={...this.intent(manifest,{decision:'supersede',carrier:'owner-stop',newInputId:stop.input_id,basis:{reason:'owner-stop-command'},items}),at:stop.at};
    await this.record(manifest.group_id,intent,{also:mark});
  }
  /** The fallback carrier. What the next reply's review reports as covered is retired in its favour;
   * what it does not mention stays exactly as it was and still waits for a decision. */
  async applyCoverage(group) {
    let complete=true;
    for(const link of group.continues??[]) {
      const old=this.manifests.read(link.group_id);
      if(!old||!this.undecided(old))continue;
      const pending=new Set(this.remainder(old).map(itemId)),items=[...new Set(group.review.covers_remainder.filter(c=>pending.has(c.item_id)).map(c=>c.item_id))];
      if(!items.length)continue;
      const intent=this.intent(old,{decision:'rewrite_remainder',carrier:'review',receipt:{review_id:group.review.id??null,group_id:group.group_id},newInputId:group.reply_id??null,items,linkedGroup:group.group_id});
      const state=await this.record(old.group_id,intent);
      if(state==='busy')complete=false;
      else if(state==='recorded')await this.advance(old.group_id);
    }
    if(complete)await this.manifests.mutate(group.group_id,current=>{if(!current.review||current.review.coverage_applied)return false;current.review.coverage_applied=true;},{operatorOnly:true});
  }
  /** A newer group stays in the live set only while an older one still depends on its outcome. */
  async release(group) {
    if(!(group.continues??[]).some(c=>!c.done))return;
    const ready=terminal(group)&&!(group.review?.covers_remainder?.length&&!group.review.coverage_applied),done=[];
    for(const link of group.continues) {
      if(link.done)continue;
      const old=this.manifests.read(link.group_id),intent=old?.tail_intent;
      const waits=old&&unsettled(old)&&(intent.linked_group===group.group_id||(!intent.linked_group&&intent.decision!=='continue'&&group.created_at>=intent.at));
      if(!old||(ready&&!waits))done.push(link.group_id);
    }
    if(done.length)await this.manifests.mutate(group.group_id,current=>{for(const link of current.continues??[])if(done.includes(link.group_id))link.done=true;},{operatorOnly:true});
  }
  /** A call of its own: only for a remainder that waited `dedicatedAfterMs` without any carrier deciding,
   * while the owner's turn is not running; one call per pass, a bounded number of tries with a growing wait. */
  async askAlone(guard) {
    if(!this.decide)return 0;
    const now=this.clock(),groups=this.groups();
    for(const manifest of groups.filter(m=>this.awaiting(m)&&!this.covering(m,groups)&&!this.stopFor(m))) {
      const since=manifest.tail_owed?.since??manifest.interrupted?.at??manifest.updated_at,wait=manifest.tail_wait??{attempts:0,next_at:0};
      const attempts=this.latest&&wait.last_at&&this.latest.at>wait.last_at?0:wait.attempts;       // a new owner message is a new reason to ask
      if(now-since<this.limits.dedicatedAfterMs||attempts>=this.limits.decisionAttempts||(attempts&&wait.next_at>now))continue;
      if(guard&&actionOf(await guard(manifestView(manifest)))==='wait')continue;
      const key=this.key(manifest);
      await this.manifests.mutate(manifest.group_id,current=>{current.tail_wait={attempts:attempts+1,last_at:now,next_at:now+Math.min(this.limits.decisionMaxMs,this.limits.decisionBaseMs*2**attempts)};},{operatorOnly:true});
      let answer=null;
      try{answer=await this.decide({interruptedReply:this.view(manifest),newMessage:this.latest?.text&&this.latest.at>manifest.created_at?this.latest.text.slice(0,this.limits.view.message):null});}
      catch{/* Counted above; asked again later, a bounded number of times. */}
      if(!answer||!this.allowed(manifest).includes(answer.decision)){this.report(manifest.group_id,{event:'decision-failed',carrier:'dedicated',attempts:attempts+1});return 1;}
      const intent=this.intent(manifest,{decision:answer.decision,carrier:'dedicated',receipt:answer.receipt??null,newInputId:manifest.interrupted?.by??this.latest?.id??null,basis:{reason:String(answer.reason??'').slice(0,300)}});
      if(await this.record(manifest.group_id,intent,{key})==='recorded')await this.advance(manifest.group_id);
      return 1;
    }
    return 0;
  }
}
