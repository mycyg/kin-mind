/** Durable host events, separate from native chat input and send retries. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {writeJsonAtomic,createJsonExclusive,createFileExclusive,readJsonFile,quarantineFile} from './atomic-json.mjs';
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
  constructor({directory,call,clock=Date.now}) {this.directory=directory;this.call=call;this.clock=clock;this.running=false;}
  append(event) {
    if(!event.id||!event.kind||!event.at)throw Error('Memory event requires stable identity, kind and time');
    fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    const file=path.join(this.directory,hash(event.id)+'.json'),body=JSON.stringify(canonical(event));
    const receipt=path.join(this.directory,'receipts',hash(event.id)+'.json');
    if(fs.existsSync(receipt)) {
      // An unreadable receipt is a conflict, never a reason to ingest the event twice.
      if(readJsonFile(receipt).value?.digest!==hash(body))throw Error('Memory event ID conflicts with committed contents');
      return {state:'recorded',id:event.id};
    }
    if(fs.existsSync(file)) {
      if(fs.readFileSync(file,'utf8')!==body)throw Error('Memory event ID conflicts with earlier contents');
      return {state:'queued',id:event.id};
    }
    if(!createJsonExclusive(file,canonical(event))&&fs.readFileSync(file,'utf8')!==body)throw Error('Memory event ID conflicts with earlier contents');
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
    writeJsonAtomic(path.join(directory,path.basename(file)),{id:event.id,digest:hash(body)},{previous:false});
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
    ...(record.taskId?{task_id:record.taskId}:{}),...(record.memoryHistorical?{historical:true}:{}),
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
  let queued=0,unreadable=0;
  for(const name of fs.readdirSync(directory).filter(n=>n.endsWith('.json'))) {
    // One unreadable receipt of the sender's is skipped, never repaired from here.
    const file=path.join(directory,name),record=readJsonFile(file).value;
    if(!record){unreadable++;continue;}
    if(!record.id||!record.attemptedAt)continue;
    if(record.attemptedAt<historicalBefore&&!record.memoryHistorical){record.memoryHistorical=true;writeJsonAtomic(file,record,{previous:false});}
    journal.append(deliveryEvent(record));queued++;
  }
  return {queued,...(unreadable?{unreadable}:{})};
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
