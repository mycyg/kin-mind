/** Durable host events, separate from native chat input and send retries. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
const hash=value=>createHash('sha256').update(value).digest('hex');
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
      if(JSON.parse(fs.readFileSync(receipt,'utf8')).digest!==hash(body))throw Error('Memory event ID conflicts with committed contents');
      return {state:'recorded',id:event.id};
    }
    if(fs.existsSync(file)) {
      if(fs.readFileSync(file,'utf8')!==body)throw Error('Memory event ID conflicts with earlier contents');
      return {state:'queued',id:event.id};
    }
    const temporary=file+'.'+process.pid+'.tmp';
    fs.writeFileSync(temporary,body,{mode:0o600});
    try {fs.linkSync(temporary,file);}catch(error){
      if(error.code!=='EEXIST'||fs.readFileSync(file,'utf8')!==body)throw error;
    } finally {fs.unlinkSync(temporary);}
    return {state:'queued',id:event.id};
  }
  snapshot() {
    const files=fs.existsSync(this.directory)?fs.readdirSync(this.directory).filter(n=>n.endsWith('.json')):[];
    const entries=[];
    for(const name of files){try{entries.push(JSON.parse(fs.readFileSync(path.join(this.directory,name),'utf8')));}catch(error){if(error.code!=='ENOENT')throw error;}}
    return entries.sort((a,b)=>a.at.localeCompare(b.at)||a.id.localeCompare(b.id));
  }
  acknowledge(event) {
    const file=path.join(this.directory,hash(event.id)+'.json'),directory=path.join(this.directory,'receipts');
    const body=JSON.stringify(canonical(event));
    if(fs.existsSync(file)&&fs.readFileSync(file,'utf8')!==body)throw Error('Memory event changed before acknowledgement');
    fs.mkdirSync(directory,{recursive:true,mode:0o700});
    const receipt=path.join(directory,path.basename(file)),temporary=receipt+'.'+process.pid+'.tmp';
    fs.writeFileSync(temporary,JSON.stringify({id:event.id,digest:hash(body)}),{mode:0o600});fs.renameSync(temporary,receipt);
    fs.rmSync(file,{force:true});fs.rmSync(path.join(this.directory,'errors',path.basename(file)),{force:true});
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
      const files=fs.existsSync(this.directory)?fs.readdirSync(this.directory).filter(n=>n.endsWith('.json')):[];
      const entries=files.map(n=>({file:path.join(this.directory,n),event:JSON.parse(fs.readFileSync(path.join(this.directory,n),'utf8'))}));
      entries.sort((a,b)=>Number(Boolean(a.event.historical))-Number(Boolean(b.event.historical))||a.event.at.localeCompare(b.event.at)||a.event.id.localeCompare(b.event.id));
      for(const {file,event} of entries) {
        if(attempted>=limit)break;
        if(!fs.existsSync(file))continue;
        const errorFile=path.join(this.directory,'errors',path.basename(file));
        const prior=fs.existsSync(errorFile)?JSON.parse(fs.readFileSync(errorFile,'utf8')):{};
        if(prior.nextAt>this.clock())continue;
        attempted++;
        try {await this.deliver(event);recorded++;}
        catch(error){
          failed++;fs.mkdirSync(path.dirname(errorFile),{recursive:true,mode:0o700});
          const attempts=(prior.attempts??0)+1;
          fs.writeFileSync(errorFile,JSON.stringify({id:event.id,attempts,reason:error.name??'Error',checkedAt:this.clock(),nextAt:this.clock()+(attempts===1?5:15)*60000}),{mode:0o600});
        }
      }
      const pending=this.snapshot().length;
      return {state:pending?'pending':'drained',recorded,failed,pending};
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
  try{fs.writeFileSync(target,bytes,{flag:'wx',mode:0o600});}catch(error){if(error.code!=='EEXIST')throw error;}
  return {path:target,observed_path:file,sha256,name:path.basename(file),bytes:bytes.length};
}
/** Reconcile the local outbox/journal gap; never call a transport API here. */
export function reconcileOutbox({directory,journal,historicalBefore}) {
  if(!fs.existsSync(directory))return {queued:0};
  let queued=0;
  for(const name of fs.readdirSync(directory).filter(n=>n.endsWith('.json'))) {
    const file=path.join(directory,name),record=JSON.parse(fs.readFileSync(file,'utf8'));
    if(!record.id||!record.attemptedAt)continue;
    if(record.attemptedAt<historicalBefore&&!record.memoryHistorical){record.memoryHistorical=true;const temp=file+'.memory-'+process.pid;fs.writeFileSync(temp,JSON.stringify(record),{mode:0o600});fs.renameSync(temp,file);}
    journal.append(deliveryEvent(record));queued++;
  }
  return {queued};
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
