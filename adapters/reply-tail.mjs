/** What becomes of the words of a reply that did not reach the owner (N4).
 *
 *  - When the owner writes again (or says stop) before a reply is out, the
 *    group stops at its next bubble boundary: a bubble that has begun is always
 *    finished, what has not begun is withdrawn and never sent by the host.
 *  - Those words, and the words of any reply the transport could not deliver,
 *    are owed to Kin: every one of them goes into the context of her next owner
 *    turn, and she decides whether and how to say them. No other model is asked,
 *    nothing is dropped unseen, and there is no count of tries.
 *  - A reply that could not go out as written (a private marker, a broken
 *    envelope, an unfinished turn) is owed as a fact, without its words.
 *  - A group with a fragment whose outcome is unknown is never withdrawn: it
 *    blocks itself and nothing else, and what became of it is not guessed.
 *  - The obligation ends once a turn that carried it was taken into the native
 *    session (`handed`); the group is then filed away.
 * `TransportManifests.finish` writes what is owed (`owedRemainder`); everything
 * durable lives in the manifests, written under their lease. This module's only
 * file of its own is `<manifests>/tail/stops.json`, the owner's literal stops. */
import path from 'node:path';
import {readJsonFile,writeJsonAtomic} from './atomic-json.mjs';
import {TERMINAL_GROUP_STATES,OWED_MAX_AGE_MS,legacyState,owedRemainder} from './transport-manifest.mjs';

export const TAIL_DEFAULTS=Object.freeze({stopsKept:8,stopsKeptMs:7*86400000,interruptWaitMs:3000});
/** Routes under which the prompt that carried the owed words reached the native session. */
export const HANDED_ROUTES=Object.freeze(['new-turn','merged-before-start','steered']);

const terminal=manifest=>TERMINAL_GROUP_STATES.includes(manifest.state);
const begun=bubble=>bubble.fragments.some(f=>f.state!=='unsent');
const open=bubble=>!['accepted','rejected','undeliverable','canceled'].includes(bubble.state);
const itemId=bubble=>bubble.draft_id??bubble.bubble_id;
const unknownIn=manifest=>manifest.bubbles.some(b=>b.fragments.some(f=>['unknown','submitting'].includes(f.state)));
const midway=manifest=>manifest.bubbles.some(b=>open(b)&&begun(b));
const actionOf=answer=>typeof answer==='string'?answer:answer?.action;
const within=(work,ms)=>{let timer;return Promise.race([work,new Promise(resolve=>{timer=setTimeout(resolve,ms);timer.unref?.();})]).finally(()=>clearTimeout(timer));};

export class ReplyTail {
  /** `ownerEpoch()` is the host's current owner epoch; `onEvent` receives
   * text-free status facts keyed by group. */
  constructor({manifests,clock=()=>Date.now(),ownerEpoch=null,onEvent=()=>{},hooks={},limits={}}) {
    Object.assign(this,{manifests,clock,ownerEpoch,onEvent,hooks});
    this.limits={...TAIL_DEFAULTS,...limits};
    this.latest=null;this.chain=Promise.resolve();
  }

  // ---- reading -----------------------------------------------------------

  groups() {
    return this.manifests.live().map(id=>{try{return this.manifests.read(id);}catch{return null;}})
      .filter(m=>m&&m.kind==='reply').sort((a,b)=>a.created_at-b.created_at||a.group_id.localeCompare(b.group_id));
  }
  idle(manifest){return !['held','claiming'].includes(this.manifests.leaseState(manifest.group_id).state);}
  /** For the next owner turn: what the owner never received, oldest first. It
   * carries reply text, so it is for the owner's own session, never for status or logs. */
  owed() {
    return this.groups().filter(m=>m.tail_owed?.items?.length).map(m=>{
      const ids=new Set(m.tail_owed.items),unsent=m.bubbles.filter(b=>ids.has(itemId(b)));
      return {group_id:m.group_id,reply_id:m.reply_id,reason:m.tail_owed.reason??m.reason??null,words:m.tail_owed.words!==false,
        sent:m.bubbles.filter(b=>b.state==='accepted').map(b=>b.text),unsent:m.tail_owed.words===false?[]:unsent.map(b=>b.text),count:unsent.length};
    });
  }
  /** Text-free. */
  status() {
    const all={};
    for(const m of this.groups()) {
      if(!m.tail_owed&&m.state!=='interrupted')continue;
      all[m.group_id]={state:m.state,reason:m.reason??null,...(m.tail_owed?{owed:{items:m.tail_owed.items.length,words:m.tail_owed.words!==false,since:m.tail_owed.since??null}}:{})};
    }
    return {groups:all,owed:Object.keys(all).filter(id=>all[id].owed).length,stops:this.stops().length};
  }
  report(manifest,tail) {
    try {
      if(typeof manifest==='string')manifest=this.manifests.read(manifest);
      if(manifest)this.onEvent({groupId:manifest.group_id,inputId:manifest.reply_id,state:legacyState(manifest),groupState:manifest.state,reason:manifest.reason??undefined,tail});
    } catch{/* Status reporting never decides delivery. */}
  }
  serial(work){const next=this.chain.then(()=>work());this.chain=next.catch(()=>{});return next;}
  /** Resolves when everything started so far has been done. */
  idleNow(){return this.chain;}

  // ---- the owner's messages ----------------------------------------------

  /** The newest owner message, in memory only: what stops a group that is being sent right now. */
  note({id=null}={}){if(id===null||this.latest?.id!==id)this.latest={id,at:this.clock()};}
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
  /** The newest thing the owner said after this group was written: a stop, or a message. */
  newerThan(manifest) {
    const stop=this.stops().findLast(s=>s.at>=manifest.created_at);
    if(stop)return {reason:'owner-stop',inputId:stop.input_id};
    if(!manifest.work&&this.latest&&this.latest.at>manifest.created_at)return {reason:'new-owner-input',inputId:this.latest.id};
    return null;
  }

  // ---- the guard: what a new message does to a group that is being sent ---

  /** Wraps the host's guard. A newer owner message or stop, or the host's bare
   * 'cancel', stops the group at its next bubble boundary. */
  guard(inner=async()=>'send') {
    return async view=>{
      const stop=this.stops().findLast(s=>s.at>=(view.createdAt??Infinity));
      if(stop)return {action:'interrupt',reason:'owner-stop',inputId:stop.input_id};
      const answer=await inner(view);
      if(answer==='cancel')return {action:'interrupt',reason:'input-or-session-superseded',inputId:this.latest?.id??null};
      const newer=this.latest&&!view.request?.work&&this.latest.at>(view.createdAt??0)?this.latest:null;
      if(actionOf(answer)==='send'&&newer)return {action:'interrupt',reason:'new-owner-input',inputId:newer.id};
      return answer;
    };
  }

  // ---- the obligation ------------------------------------------------------

  /** Before an owner message is submitted: stop what is still waiting to go out
   * from before it, wait (bounded) for groups being sent to reach a boundary,
   * and settle what that left owed. `active` are the passes running now. */
  async interruptFor({inputId=null,active=[],waitMs=this.limits.interruptWaitMs}={}) {
    this.note({id:inputId});
    if(active.length)await within(Promise.allSettled(active),waitMs);
    await within(this.recover().catch(()=>{}),waitMs);
  }
  /** A turn that carried the owed words was taken into the native session:
   * the obligation ends and the groups are filed away. */
  handed({groups=[],inputId=null}={}) {
    return this.serial(async()=>{
      let count=0;
      for(const groupId of groups) {
        let done=false;
        await this.manifests.mutate(groupId,manifest=>{
          if(!manifest.tail_owed)return false;
          manifest.tail_handed={at:this.clock(),input_id:inputId,items:manifest.tail_owed.items,reason:manifest.tail_owed.reason??null};
          delete manifest.tail_owed;done=true;
        },{operatorOnly:true});
        if(!done)continue;
        count++;this.report(groupId,{event:'handed',newInputId:inputId});
        try{await this.manifests.run(groupId);}catch{/* The next resume files it. */}
      }
      return {state:'handed',groups:count};
    });
  }
  /** Roll everything forward as far as it goes now. Never asks a model. */
  recover(){return this.serial(()=>this.advanceAll());}
  /** After a pass over a group: an interruption becomes an obligation at once. */
  after(result){return result?.manifest?.state==='interrupted'?this.recover():Promise.resolve();}
  tick(){return this.recover();}

  async advanceAll() {
    const summary={withdrawn:0,migrated:0};
    // One group never stops the others; whatever failed is tried again by the next pass.
    for(const manifest of this.groups())try {
      if(this.legacy(manifest)){await this.migrate(manifest);summary.migrated++;}
      const current=this.manifests.read(manifest.group_id)??manifest;
      if(terminal(current)||unknownIn(current)||midway(current))continue;
      const newer=current.state==='interrupted'?{reason:current.reason??'new-owner-input'}:this.newerThan(current);
      if(!newer||!this.idle(current))continue;
      const result=await this.manifests.retireRemainder(current.group_id,{reason:newer.reason});
      if(result.manifest&&terminal(result.manifest)){summary.withdrawn++;this.report(result.manifest,{event:'withdrawn',reason:newer.reason,newInputId:newer.inputId??current.interrupted?.by??null});}
      await this.hooks.afterStep?.('withdrawn',{groupId:current.group_id});
    } catch{/* next pass */}
    return summary;
  }

  // ---- what an older version of this module left behind -------------------

  /** Decisions, links and counts of the model-decided tail. */
  legacy(manifest){return Boolean((manifest.tail_intent&&manifest.tail_intent.state!=='settled')||(manifest.continues??[]).some(c=>!c.done)||
    (manifest.review?.covers_remainder?.length&&!manifest.review.coverage_applied)||manifest.tail_owed?.forced!==undefined||manifest.tail_owed?.resurfaced!==undefined);}
  /** Everything an old intent still promised to deliver becomes owed to Kin; links and counts are closed. */
  async migrate(manifest) {
    await this.manifests.mutate(manifest.group_id,current=>{
      const intent=current.tail_intent,items=new Set(current.tail_owed?.items??[]);
      if(intent&&intent.state!=='settled') {
        if(!intent.stopped)for(const item of intent.items??[])items.add(item);
        Object.assign(intent,{state:'settled',settled_at:this.clock(),outcome:{state:'migrated'}});
      }
      for(const link of current.continues??[])link.done=true;
      if(current.review?.covers_remainder?.length)current.review.coverage_applied=true;
      // What an old intent had not yet withdrawn is withdrawn by the next pass like any interrupted group.
      const fresh=this.clock()-current.created_at<=OWED_MAX_AGE_MS;
      const withdrawn=fresh?current.bubbles.filter(b=>items.has(itemId(b))&&!open(b)).map(itemId):[];
      if(withdrawn.length)current.tail_owed={items:withdrawn,since:current.tail_owed?.since??this.clock(),reason:current.reason??null,words:true};
      else delete current.tail_owed;
      if(current.state==='interrupted'||(intent&&!terminal(current)&&current.bubbles.some(b=>open(b)&&!begun(b))))
        Object.assign(current,{state:'interrupted',reason:current.reason??'new-owner-input',retryAt:0});
    },{operatorOnly:true});
  }
}
export {owedRemainder};
