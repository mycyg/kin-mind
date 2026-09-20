import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

const OUTBOX_ID=/^[A-Za-z0-9_-]{1,160}$/;
const RECEIPT_TOKEN=/^[A-Za-z0-9_.:-]{1,256}$/;
const SHA256=/^[a-f0-9]{64}$/;
const MAX_RECORD_BYTES=2*1024*1024;
// Real native tool records can exceed 8 MiB; keep the stream bounded without
// rejecting the current canonical rollout (whose largest line is ~8.7 MiB).
const MAX_LINE_BYTES=16*1024*1024;
const MEDIA_TYPES=new Set(['image','file','audio','video']);

const hash=value=>createHash('sha256').update(value).digest('hex');
const failure=code=>Object.assign(Error(code),{code});
const diagnostic=(toolId,code)=>({toolId,code});
const epoch=value=>Number.isFinite(value)&&Number.isFinite(new Date(value).getTime());
const iso=value=>epoch(value)?new Date(value).toISOString():null;
const time=value=>typeof value==='string'&&Number.isFinite(Date.parse(value))?Date.parse(value):null;
const contained=(root,file)=>{const relative=path.relative(root,file);return relative!==''&&!relative.startsWith('..'+path.sep)&&!path.isAbsolute(relative);};

async function completeLines(file,visit) {
  let stat;
  try{stat=await fs.promises.lstat(file);}catch{throw failure('native-rollout-unreadable');}
  if(!stat.isFile()||stat.isSymbolicLink())throw failure('native-rollout-unreadable');
  if(!stat.size)return;
  const input=fs.createReadStream(file,{start:0,end:stat.size-1,encoding:'utf8'});
  let tail='';
  try{
    for await(const chunk of input){
      tail+=chunk;let newline;
      while((newline=tail.indexOf('\n'))>=0){const line=tail.slice(0,newline);tail=tail.slice(newline+1);if(Buffer.byteLength(line)>MAX_LINE_BYTES)throw failure('native-rollout-line-too-large');if(line)await visit(line);}
      if(Buffer.byteLength(tail)>MAX_LINE_BYTES)throw failure('native-rollout-line-too-large');
    }
  }catch(error){if(error?.code?.startsWith?.('native-'))throw error;throw failure('native-rollout-unreadable');}
  // The canonical file may be observed during an append. An unterminated tail
  // is not a receipt and is deliberately left for the next read.
}

function parseRelevant(line,kind) {
  try{return JSON.parse(line);}catch{throw failure(kind);}
}

function toolReceipt(item) {
  if(item?.type!=='CommandExecution'||item.status!=='completed'||item.exit_code!==0||typeof item.stdout!=='string')return null;
  let receipt;try{receipt=JSON.parse(item.stdout.trim());}catch{return null;}
  if(!receipt||typeof receipt!=='object'||Array.isArray(receipt)||receipt.state!=='accepted')return null;
  const id=OUTBOX_ID.test(receipt.id??'')?receipt.id:undefined;
  const messageId=RECEIPT_TOKEN.test(receipt.messageId??'')?receipt.messageId:undefined;
  if(!id&&!messageId)return null;
  return {id,messageId,outputSha256:hash(item.stdout)};
}

async function nativeReceipts({file,sessionId,toolIds}) {
  const events=new Map(),meta=[];
  await completeLines(file,line=>{
    if(line.includes('"type":"session_meta"')) {
      const entry=parseRelevant(line,'native-rollout-invalid-session-meta');
      if(entry.type==='session_meta')meta.push(entry.payload?.id);
      return;
    }
    if(!line.includes('"type":"event_msg"')||!line.includes('"type":"item_completed"'))return;
    // Native JSONL writes the public item envelope before its fields. Requiring
    // this exact structural prefix keeps a quoted tool id inside assistant or
    // reasoning text from making that private content worth parsing at all.
    const possible=[...toolIds].find(id=>line.includes('"item":{"type":"CommandExecution","id":'+JSON.stringify(id)));if(!possible)return;
    const entry=parseRelevant(line,'native-rollout-invalid-tool-event'),payload=entry.payload??{},item=payload.item??{};
    if(entry.type!=='event_msg'||payload.type!=='item_completed'||item.id!==possible||!toolIds.has(item.id))return;
    const receipt=toolReceipt(item);if(!receipt)return;
    const startedAt=payload.started_at_ms,completedAt=payload.completed_at_ms;
    const value={toolId:item.id,threadId:payload.thread_id,turnId:payload.turn_id,startedAt,completedAt,...receipt};
    const list=events.get(item.id)??[];list.push(value);events.set(item.id,list);
  });
  if(meta.length!==1||meta[0]!==sessionId)throw failure('native-rollout-session-mismatch');
  return events;
}

async function readJson(file,code) {
  let stat,text;
  try{stat=await fs.promises.lstat(file);if(!stat.isFile()||stat.isSymbolicLink()||stat.size>MAX_RECORD_BYTES)throw failure(code);text=await fs.promises.readFile(file,'utf8');}
  catch(error){if(error?.code===code)throw error;throw failure(code);}
  try{return JSON.parse(text);}catch{throw failure(code);}
}

async function outboxRecords(directory,receipts) {
  const byId=new Map(),byMessage=new Map(),wantedIds=new Set(),wantedMessages=new Set();
  for(const receipt of receipts){if(receipt.id)wantedIds.add(receipt.id);if(receipt.messageId)wantedMessages.add(receipt.messageId);}
  let entries;try{entries=await fs.promises.readdir(directory,{withFileTypes:true});}catch{throw failure('native-tool-outbox-unreadable');}
  const records=[];
  for(const entry of entries.sort((a,b)=>a.name.localeCompare(b.name))) {
    if(!entry.isFile()||!entry.name.endsWith('.json'))continue;
    const base=entry.name.slice(0,-5);
    const record=await readJson(path.join(directory,entry.name),'native-tool-outbox-unreadable');
    if(!OUTBOX_ID.test(base)||record.id!==base)throw failure('native-tool-outbox-identity-mismatch');
    records.push(record);
    if(wantedIds.has(base)&&typeof record.messageId==='string')wantedMessages.add(record.messageId);
  }
  for(const record of records){
    if(wantedIds.has(record.id))byId.set(record.id,record);
    if(typeof record.messageId==='string'&&wantedMessages.has(record.messageId)){
      const list=byMessage.get(record.messageId)??[];list.push(record);byMessage.set(record.messageId,list);
    }
  }
  return {byId,byMessage};
}

function matchingOutbox(receipt,index) {
  if(receipt.id){const record=index.byId.get(receipt.id);if(!record||!RECEIPT_TOKEN.test(record.messageId??''))return null;if(receipt.messageId&&record.messageId!==receipt.messageId)return null;return (index.byMessage.get(record.messageId)??[]).length===1?record:null;}
  const records=index.byMessage.get(receipt.messageId)??[];return records.length===1?records[0]:null;
}

function validOutbox(record,event) {
  if(!record||record.state!=='accepted'||record.stage!=='platform-accepted'||record.submissionStarted!==true||record.kind!=='reply')return false;
  if(!RECEIPT_TOKEN.test(record.messageId??'')||!record.artifact||!record.media)return false;
  if(!SHA256.test(record.artifact.sha256??'')||!Number.isSafeInteger(record.artifact.bytes)||record.artifact.bytes<=0)return false;
  if(record.artifact.bytes!==record.media.bytes||record.artifact.name!==record.media.name)return false;
  if(!MEDIA_TYPES.has(record.media.type))return false;
  if(typeof record.artifact.name!=='string'||record.artifact.name.length===0||record.artifact.name.length>255||path.basename(record.artifact.name)!==record.artifact.name)return false;
  const points=[event.startedAt,time(record.attemptedAt),time(record.uploadedAt),time(record.submittedAt),time(record.acceptedAt),event.completedAt];
  return points.every(Number.isFinite)&&points.every((value,index)=>index===0||value>=points[index-1]);
}

async function rootsFor(outboxDirectory,artifactRoots) {
  const values=artifactRoots??[path.dirname(outboxDirectory)];
  if(!Array.isArray(values)||!values.length||values.some(value=>typeof value!=='string'||!path.isAbsolute(value)))throw failure('native-tool-artifact-roots-invalid');
  const roots=[];
  for(const value of values){try{roots.push(await fs.promises.realpath(value));}catch{throw failure('native-tool-artifact-root-unreadable');}}
  return roots;
}

async function verifyArtifact(record,roots) {
  if(typeof record.artifact.path!=='string'||!path.isAbsolute(record.artifact.path))return null;
  let link,real,stat;
  try{link=await fs.promises.lstat(record.artifact.path);real=await fs.promises.realpath(record.artifact.path);stat=await fs.promises.stat(real);}
  catch{throw failure('native-tool-artifact-unreadable');}
  if(link.isSymbolicLink()||!stat.isFile()||!roots.some(root=>contained(root,real)))return null;
  if(stat.size!==record.artifact.bytes)return null;
  const digest=createHash('sha256');
  try{for await(const chunk of fs.createReadStream(real))digest.update(chunk);}catch{throw failure('native-tool-artifact-unreadable');}
  const sha256=digest.digest('hex');
  return sha256===record.artifact.sha256?{sha256,bytes:stat.size}:null;
}

/**
 * Join a task-owned terminal native tool receipt to the accepted outbox media
 * it caused. The canonical rollout and host directories are caller-owned
 * authority. Returned proofs contain no command, tool output, message body or
 * filesystem path.
 */
export async function readNativeToolDeliveries({file,sessionId,task,outboxDirectory,artifactRoots}={}) {
  if(typeof file!=='string'||!path.isAbsolute(file)||!RECEIPT_TOKEN.test(sessionId??'')||!task||!RECEIPT_TOKEN.test(task.id??'')||typeof outboxDirectory!=='string'||!path.isAbsolute(outboxDirectory))throw failure('native-tool-delivery-input-invalid');
  const completed=new Set(Object.entries(task.tools??{}).filter(([,tool])=>tool?.status==='completed').map(([id])=>id).filter(id=>RECEIPT_TOKEN.test(id)));
  const events=await nativeReceipts({file,sessionId,toolIds:completed}),diagnostics=[],candidates=[];
  for(const toolId of [...completed].sort()){
    const list=events.get(toolId)??[];
    if(list.length>1){diagnostics.push(diagnostic(toolId,'native-tool-receipt-ambiguous'));continue;}
    if(list.length===1){const event=list[0];if(event.threadId!==sessionId||!epoch(event.startedAt)||!epoch(event.completedAt)||event.startedAt>event.completedAt){diagnostics.push(diagnostic(toolId,'native-tool-event-invalid'));continue;}candidates.push(event);}
  }
  if(!candidates.length)return {proofs:[],diagnostics};
  const index=await outboxRecords(outboxDirectory,candidates),roots=await rootsFor(outboxDirectory,artifactRoots),proofs=[];
  for(const event of candidates) {
    const record=matchingOutbox(event,index);
    if(!record){diagnostics.push(diagnostic(event.toolId,'native-tool-outbox-not-unique'));continue;}
    if(event.id&&record.id!==event.id||event.messageId&&record.messageId!==event.messageId||!validOutbox(record,event)){diagnostics.push(diagnostic(event.toolId,'native-tool-outbox-invalid'));continue;}
    const actual=await verifyArtifact(record,roots);
    if(!actual){diagnostics.push(diagnostic(event.toolId,'native-tool-artifact-invalid'));continue;}
    const sourceHash=hash(JSON.stringify({sessionId,taskId:task.id,tool:{id:event.toolId,turnId:event.turnId,startedAt:event.startedAt,completedAt:event.completedAt,outputSha256:event.outputSha256},
      outbox:{id:record.id,state:record.state,stage:record.stage,kind:record.kind,messageId:record.messageId,attemptedAt:record.attemptedAt,uploadedAt:record.uploadedAt,submittedAt:record.submittedAt,acceptedAt:record.acceptedAt,
        artifact:{sha256:record.artifact.sha256,bytes:record.artifact.bytes,name:record.artifact.name},media:{type:record.media.type,name:record.media.name,bytes:record.media.bytes}},artifact:actual}));
    proofs.push({id:record.id,outboxId:record.id,toolId:event.toolId,sessionId,taskId:task.id,messageId:record.messageId,state:'accepted',source:'native-tool-outbox',sourceHash,
      artifact:{sha256:actual.sha256,bytes:actual.bytes,type:record.media.type,name:record.artifact.name},toolStartedAt:iso(event.startedAt),toolCompletedAt:iso(event.completedAt),acceptedAt:new Date(time(record.acceptedAt)).toISOString()});
  }
  proofs.sort((a,b)=>a.toolCompletedAt.localeCompare(b.toolCompletedAt)||a.toolId.localeCompare(b.toolId));
  const idCount=new Map(),messageCount=new Map();
  for(const proof of proofs){idCount.set(proof.id,(idCount.get(proof.id)??0)+1);messageCount.set(proof.messageId,(messageCount.get(proof.messageId)??0)+1);}
  const unique=[];
  for(const proof of proofs){if(idCount.get(proof.id)>1||messageCount.get(proof.messageId)>1)diagnostics.push(diagnostic(proof.toolId,'native-tool-proof-conflict'));else unique.push(proof);}
  return {proofs:unique,diagnostics};
}
