/** Durable host events, separate from native chat input and send retries. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {writeJsonAtomic,createJsonExclusive,createFileExclusive,readJsonFile,quarantineFile} from './atomic-json.mjs';
import {readThroughArchive} from './state-pruner.mjs';
const hash=value=>createHash('sha256').update(value).digest('hex');
/** The retry ledger keeps one file per event that is still waiting, and never more
 * than this many. It carries IDs, counts and times only — never message text. */
export const ERROR_LEDGER_LIMIT=256;
const errorCode=error=>String(error?.name??'Error').replace(/[^\w-]/g,'').slice(0,64)||'Error';
function canonical(value) {
  if(Array.isArray(value))return value.map(canonical);
  if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])]));
  return value;
}
export class MemoryEventJournal {
  constructor({directory,call,clock=Date.now,archivedState={}}) {this.directory=directory;this.call=call;this.clock=clock;this.running=false;this.archivedState=archivedState;this.listeners=new Set();}
  /** Hear of every event this journal takes on. A listener never decides whether it is kept. */
  observe(listener){this.listeners.add(listener);return ()=>this.listeners.delete(listener);}
  notify(event){for(const listener of this.listeners)try{listener(event);}catch{/* Observers never refuse a durable event. */}}
  append(event) {
    if(!event.id||!event.kind||!event.at)throw Error('Memory event requires stable identity, kind and time');
    fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    const file=path.join(this.directory,hash(event.id)+'.json'),body=JSON.stringify(canonical(event));
    const receipt=path.join(this.directory,'receipts',hash(event.id)+'.json');
    // A receipt that was archived still says this event was ingested. Missing it
    // would ingest the same delivery a second time, which is the one thing the
    // receipt exists to stop, so the miss looks in the archive before giving up.
    const recorded=readThroughArchive(receipt,this.archivedState);
    if(recorded.state!=='missing') {
      // An unreadable receipt is a conflict, never a reason to ingest the event twice.
      if(recorded.value?.digest!==hash(body))throw Error('Memory event ID conflicts with committed contents');
      return {state:'recorded',id:event.id};
    }
    if(fs.existsSync(file)) {
      if(fs.readFileSync(file,'utf8')!==body)throw Error('Memory event ID conflicts with earlier contents');
      return {state:'queued',id:event.id};
    }
    if(!createJsonExclusive(file,canonical(event))&&fs.readFileSync(file,'utf8')!==body)throw Error('Memory event ID conflicts with earlier contents');
    this.notify(event);
    return {state:'queued',id:event.id};
  }
  errorFile(name){return path.join(this.directory,'errors',name);}
  /** The ledger never outlives the events it is about: entries whose event is gone are
   * removed, and only the most recently checked remain. Losing one only means its event
   * is retried sooner; no event is ever lost with it. */
  pruneErrors(keep=ERROR_LEDGER_LIMIT) {
    let names=[];
    try {names=fs.readdirSync(this.errorFile('')).filter(name=>name.endsWith('.json'));}
    catch(error){if(error.code==='ENOENT')return 0;throw error;}
    const drop=names.filter(name=>!fs.existsSync(path.join(this.directory,name))),gone=new Set(drop);
    const live=names.filter(name=>!gone.has(name));
    if(live.length>keep)drop.push(...live.map(name=>({name,at:readJsonFile(this.errorFile(name)).value?.checkedAt??0}))
      .sort((a,b)=>b.at-a.at).slice(keep).map(entry=>entry.name));
    for(const name of drop)for(const suffix of ['','.prev'])fs.rmSync(this.errorFile(name)+suffix,{force:true});
    return drop.length;
  }
  /** Queued events, oldest first. One unreadable file is moved aside, never allowed
   * to stop the rest: a single bad byte must not end memory ingestion. */
  queued() {
    const names=fs.existsSync(this.directory)?fs.readdirSync(this.directory).filter(n=>n.endsWith('.json')):[];
    const entries=[],quarantined=[];
    for(const name of names) {
      const file=path.join(this.directory,name),read=readJsonFile(file);
      if(read.state==='ok'&&read.value?.id&&read.value?.at)entries.push({file,event:read.value});
      else if(read.state==='corrupt'){const target=quarantineFile(file,path.join(this.directory,'quarantine'));if(target)quarantined.push(target);}
    }
    entries.sort((a,b)=>String(a.event.at).localeCompare(String(b.event.at))||String(a.event.id).localeCompare(String(b.event.id)));
    return {entries,quarantined};
  }
  snapshot() {return this.queued().entries.map(entry=>entry.event);}
  acknowledge(event) {
    const file=path.join(this.directory,hash(event.id)+'.json'),directory=path.join(this.directory,'receipts');
    const body=JSON.stringify(canonical(event));
    if(fs.existsSync(file)&&fs.readFileSync(file,'utf8')!==body)throw Error('Memory event changed before acknowledgement');
    // When this was settled, so that the receipt can later be told how old it is
    // without anyone reading the file's own timestamps — those are rewritten by
    // reconciliation and say nothing true about the record. Earlier code reads
    // only `digest` here, so the extra key costs a rollback nothing.
    writeJsonAtomic(path.join(directory,path.basename(file)),{id:event.id,digest:hash(body),at:new Date(this.clock()).toISOString()},{previous:false});
    fs.rmSync(file,{force:true});
    for(const suffix of ['','.prev'])fs.rmSync(this.errorFile(path.basename(file))+suffix,{force:true});
  }
  async deliver(event) {
    this.append(event);
    const owner=event.kind==='owner-message';
    const receipt=await this.call(owner?'ingest':'runtime-event',event);
    if(owner?!(receipt.source_id&&receipt.appraisal?.id):receipt.state!=='recorded')throw Error('Memory ingestion awaits a receipt');
    this.acknowledge(event);return receipt;
  }
  async drain(limit=24) {
    if(this.running)return {state:'busy'};
    this.running=true;let recorded=0,failed=0,attempted=0;
    try {
      const {entries,quarantined}=this.queued();
      entries.sort((a,b)=>Number(Boolean(a.event.historical))-Number(Boolean(b.event.historical)));
      for(const {file,event} of entries) {
        if(attempted>=limit)break;
        if(!fs.existsSync(file))continue;
        const errorFile=this.errorFile(path.basename(file));
        // An unreadable retry note only costs this event an earlier retry.
        const prior=readJsonFile(errorFile).value??{};
        if(prior.nextAt>this.clock())continue;
        attempted++;
        try {await this.deliver(event);recorded++;}
        catch(error){
          failed++;
          const attempts=(prior.attempts??0)+1;
          writeJsonAtomic(errorFile,{id:event.id,attempts,reason:errorCode(error),checkedAt:this.clock(),nextAt:this.clock()+(attempts===1?5:15)*60000},{previous:false});
        }
      }
      const pruned=this.pruneErrors();
      const pending=this.snapshot().length;
      return {state:pending?'pending':'drained',recorded,failed,pending,...(quarantined.length?{quarantined:quarantined.length}:{}),...(pruned?{pruned}:{})};
    } finally {this.running=false;}
  }
}
export function deliveryEvent(record,{channel='feishu',batchId,expectedBubbles,artifact}={}) {
  const state=record.state==='accepted'?'accepted':record.state==='unconfirmed'?'unconfirmed':record.state==='not-submitted'?'canceled':'prepared';
  const at=state==='accepted'?record.acceptedAt:['unconfirmed','canceled'].includes(state)?record.checkedAt:record.attemptedAt;
  if(!at)throw Error('Delivery observation needs the persisted receipt timestamp');
  return {id:`delivery:${channel}:${record.id}:${state}`,kind:'delivery',at,
    channel,delivery_id:batchId??record.memoryBatchId??record.id,bubble_id:record.id,
    ...(expectedBubbles??record.expectedBubbles?{expected_bubbles:expectedBubbles??record.expectedBubbles}:{}),
    text:record.text??'',state,...(record.messageId?{message_id:record.messageId}:{}),
    ...(artifact??record.artifact?{artifact:artifact??record.artifact}:{}),
    ...(Object.hasOwn(record,'taskId')?{task_id:record.taskId}:{}),
    ...(Number.isSafeInteger(record.inputVersion)?{input_version:record.inputVersion}:{}),
    ...(Number.isSafeInteger(record.turnFence)?{turn_fence:record.turnFence}:{}),
    ...(record.replyInputId?{reply_input_id:record.replyInputId}:{}),
    ...(record.memoryHistorical?{historical:true}:{}),
    ...(record.references?.length?{references:record.references}:{}),...(record.draftId?{draft_id:record.draftId}:{}),origin:record.kind??'direct'};
}
export function artifactFromBytes(media) {
  return {sha256:hash(media.data),name:media.name??'attachment',bytes:media.data.length};
}
export function snapshotArtifact(file,directory) {
  const stat=fs.statSync(file);
  if(!stat.isFile()||stat.size>50*1024*1024)throw Error('Artifact snapshot is outside the file budget');
  const bytes=fs.readFileSync(file),sha256=hash(bytes);
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const target=path.join(directory,sha256);
  // The snapshot is the proof that these were the bytes. It is written whole or
  // not at all, because a half-written one keeps a name that promises the rest.
  createFileExclusive(target,bytes);
  return {path:target,observed_path:file,sha256,name:path.basename(file),bytes:bytes.length};
}
/** Reconcile the local outbox/journal gap; never call a transport API here. */
export function reconcileOutbox({directory,journal,historicalBefore}) {
  if(!fs.existsSync(directory))return {queued:0};
  let queued=0,recorded=0,unreadable=0,failed=0;
  for(const name of fs.readdirSync(directory).filter(n=>n.endsWith('.json'))) {
    const file=path.join(directory,name),record=readJsonFile(file).value;
    if(!record){unreadable++;continue;}
    if(!record.id||!record.attemptedAt)continue;
    try {
      const event=deliveryEvent(record);
      // Recovery fills gaps. A committed event already owns its identity; later
      // outbox metadata is not another ingestion and must not rewrite that event.
      const receipt=path.join(journal.directory,'receipts',hash(event.id)+'.json');
      if(readThroughArchive(receipt,journal.archivedState??{}).value?.id===event.id){recorded++;continue;}
      if(record.attemptedAt<historicalBefore&&!record.memoryHistorical){record.memoryHistorical=true;writeJsonAtomic(file,record,{previous:false});}
      const result=journal.append(deliveryEvent(record));
      if(result?.state==='queued')queued++;else if(result?.state==='recorded')recorded++;
    } catch {failed++;} // One unresolved memory record cannot prevent chat startup.
  }
  return {queued,...(recorded?{recorded}:{}),...(unreadable?{unreadable}:{}),...(failed?{failed}:{})};
}
/** Credentials and opaque tokens never become memory. Private bodies are kept
 * as they were (they are the owner's own material); these patterns only remove
 * what would let anyone act as someone. */
const SECRET_TEXT=[
  /\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}/g,
  /\bgh[pousr]_[A-Za-z0-9]{20,}/g,
  /\bgithub_pat_[A-Za-z0-9_]{20,}/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}/g,
  /\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g,
  /((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{12,}/gi,
  /((?:api[_-]?key|secret|password|passwd|token|access[_-]?key|authorization|cookie)["']?\s*[:=]\s*["']?)[^\s"',;&]+/gi,
  /([?&](?:key|token|sig|signature|secret|access_token|auth)=)[^&\s"']+/gi,
  /\b[A-Za-z0-9+/_=-]{48,}\b/g,
];
const SECRET_KEY=/^(?:api[_-]?key|secret|password|passwd|token|access[_-]?key|authorization|cookie|credentials?|private[_-]?key)$/i;
/** Every field of a tool experience is bounded. */
export const TOOL_TEXT_LIMIT=4000;
export function redactToolText(value,limit=TOOL_TEXT_LIMIT) {
  let text=String(value??'');
  for(const pattern of SECRET_TEXT)text=text.replace(pattern,(match,prefix)=>typeof prefix==='string'&&match.startsWith(prefix)?prefix+'[redacted]':'[redacted]');
  return text.length>limit?text.slice(0,limit)+'…[truncated '+(text.length-limit)+' chars]':text;
}
function redactValue(value,depth=0) {
  if(typeof value==='string')return redactToolText(value);
  if(value===null||typeof value!=='object')return value;
  if(depth>=6)return '[nested]';
  if(Array.isArray(value))return value.slice(0,64).map(item=>redactValue(item,depth+1));
  return Object.fromEntries(Object.entries(value).slice(0,64).map(([key,item])=>[key,SECRET_KEY.test(key)?'[redacted]':redactValue(item,depth+1)]));
}
const bounded=value=>{const text=JSON.stringify(value??null);return text.length>TOOL_TEXT_LIMIT*2?redactToolText(text,TOOL_TEXT_LIMIT*2):value;};
/** The memory store's own recall tools: what they return is evidence already in the store, not a new observation. */
const MEMORY_SERVER='memorypalace';

/** One finished tool call as the eventmem `tool` host event the retired mobile
 * hook wrote for PostToolUse (namespace `host:codex`, source "Kin 工具活动"). */
export function toolExperience(call,{session,scope,scenario='companion'}) {
  if(!call?.id||typeof session!=='string'||!session)return null;
  const input=call.rawInput&&typeof call.rawInput==='object'?call.rawInput:{};
  if(typeof input.server==='string'&&input.server===MEMORY_SERVER)return null;
  const tool=typeof input.server==='string'&&typeof input.tool==='string'?'mcp__'+input.server+'__'+input.tool
    :call.kind==='execute'?'shell':call.kind&&call.kind!=='other'?call.kind:String(call.title||'tool');
  const arguments_=typeof input.server==='string'?input.arguments??{}:Object.keys(input).length?input:{title:call.title??null};
  const output=call.rawOutput!==undefined?call.rawOutput:(call.content??[]).map(item=>item?.content?.text??(item?.type==='diff'?'[diff '+(item.path??'')+']':'')).filter(Boolean).join('\n');
  const failed=call.status==='failed'||call.rawOutput?.isError===true||(Number.isInteger(call.rawOutput?.exit_code)&&call.rawOutput.exit_code!==0);
  const payload={session_id:session,tool_name:redactToolText(tool,128),tool_use_id:call.id,tool_input:bounded(redactValue(arguments_)),
    tool_response:bounded(redactValue(output)),isError:failed,host:'codex',scenario,hook_event_name:'PostToolUse',
    ...(scope?{scope}:{}),memory_context_managed:true,extract:false};
  payload.command_id=hash(JSON.stringify(['PostToolUse',session,call.id]));
  return {event:'tool',payload};
}
/** The store's durable intake for host events: the memory service replays
 * `<root>/host-spool/*.json` as receipts. One file per call, never rewritten. */
export function spoolHostEvent(root,record) {
  const directory=path.join(root,'host-spool');
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const file=path.join(directory,'kin-tool-'+hash(record.payload.session_id+'\0'+record.payload.tool_use_id).slice(0,40)+'.json');
  createJsonExclusive(file,record);
  return file;
}
/** Only an observed edit effect establishes creator provenance. */
export class ToolArtifactObserver {
  constructor({journal,task=()=>null,clock=()=>new Date().toISOString()}) {Object.assign(this,{journal,task,clock});this.before=new Map();}
  update(update) {
    if(!update.toolCallId)return;
    const id=update.toolCallId;
    const paths=[...(update.locations??[]).map(l=>l.path),update.rawInput?.path,update.rawInput?.file_path].filter(p=>typeof p==='string'&&path.isAbsolute(p));
    if(update.status==='failed'){this.before.delete(id);return;}
    if(update.status!=='completed') {
      const previous=this.before.get(id)??new Map();
      previous.kind=update.kind??previous.kind;
      for(const file of paths)if(!previous.has(file))previous.set(file,this.stat(file));
      this.before.set(id,previous);return;
    }
    const before=this.before.get(id);this.before.delete(id);
    for(const file of new Set([...paths,...(before?.keys()??[])])) {
      const after=this.stat(file),prior=before?.get(file);
      if(!after||after===prior)continue;
      const created=(update.kind??before?.kind)==='edit'&&before?.has(file);
      this.journal.append({id:`tool-artifact:${id}:${hash(file)}`,kind:created?'artifact-created':'artifact-observed',
        at:this.clock(),actor:created?'Kin':'unknown',tool_call_id:id,task_id:this.task(),artifact:snapshotArtifact(file,path.join(this.journal.directory,'artifacts'))});
    }
  }
  stat(file) {try {const s=fs.statSync(file);return s.isFile()?`${s.size}:${s.mtimeMs}`:null;}catch{return null;}}
}
/** How long a tool call Kin began may stay unfinished, and how many are kept at once. */
export const TOOL_ACTIVITY_PENDING_MS=30*60000,TOOL_ACTIVITY_PENDING_MAX=200;
/** What Kin does with a tool in a turn that answers the owner, kept as her tool
 * activity ("Kin 工具活动") through the memory store's host-event intake; this
 * replaces the native hook, which is gone. Whether a call is hers is decided when
 * it is first seen. Calls that never finish are forgotten after
 * TOOL_ACTIVITY_PENDING_MS, and at most TOOL_ACTIVITY_PENDING_MAX are kept (AD2-12). */
export class ToolActivityObserver {
  constructor({root,scope=null,session=()=>null,owner=()=>false,scenario='companion',now=()=>Date.now()}) {
    Object.assign(this,{root,scope,session,owner,scenario,now});this.calls=new Map();
  }
  update(update) {
    if(!update?.toolCallId)return;
    this.prune();
    const id=update.toolCallId,known=this.calls.get(id);
    let owner=known?.owner;
    if(owner===undefined)try{owner=Boolean(this.owner());}catch{owner=false;}
    const call={...known,id,owner,since:known?.since??this.now(),...Object.fromEntries(['title','kind','rawInput','rawOutput','content','status'].filter(key=>update[key]!==undefined).map(key=>[key,update[key]]))};
    if(!['completed','failed'].includes(update.status)){this.calls.set(id,call);return;}
    this.calls.delete(id);
    if(!owner)return;
    try {
      const record=toolExperience(call,{session:this.session(),scope:this.scope,scenario:this.scenario});
      if(record)spoolHostEvent(this.root,record);
    } catch{/* Tool activity is an observation; it never decides the turn. */}
  }
  prune() {
    const cutoff=this.now()-TOOL_ACTIVITY_PENDING_MS;
    for(const [id,call] of this.calls)if(!(call.since>=cutoff))this.calls.delete(id);
    while(this.calls.size>=TOOL_ACTIVITY_PENDING_MAX)this.calls.delete(this.calls.keys().next().value);
  }
}
/** One update stream to several observers; one that throws never stops the others. */
export function observeToolUpdates(...observers) {
  return {observers,update(update){for(const observer of observers)try{observer?.update(update);}catch{/* an observation never decides the turn */}}};
}

