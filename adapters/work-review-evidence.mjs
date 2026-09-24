import path from 'node:path';
import {readThroughArchive} from './state-pruner.mjs';

const SAFE_ID=/^[\w:.-]{1,200}$/;
/** Head and tail of an oversized body: what is missing is stated, never dropped in silence. */
const excerpt=(text,max=4000)=>typeof text==='string'&&text.length>max
  ?text.slice(0,Math.ceil(max*2/3))+'\n[…'+(text.length-max)+' chars…]\n'+text.slice(text.length-Math.floor(max/3)):text;

/** Evidence for the facts summary of one open task, read by id only: one inbox
 * record per input, one outbox record per delivery, the manifest of a held reply.
 * Nothing here scans a directory or a rollout (AD1-12, AD2-19); one unreadable
 * record is named as missing instead of failing the rest; and nothing here changes
 * or withdraws a draft (N3). Owner-bound readers are injected by the private host. */
export function workEvidence({sessionId,inputDirectory,failedInputDirectory=null,outboxDirectory,lastReply=async()=>null,manifests=null,archivedState={}}) {
  const read=file=>{try{return readThroughArchive(file,archivedState).value??null;}catch{return null;}};
  const store=()=>typeof manifests==='function'?manifests():manifests;
  const source=id=>{
    let value=read(path.join(inputDirectory,id+'.json'));
    if(!value&&failedInputDirectory) {
      const failed=read(path.join(failedInputDirectory,id+'.json'));
      try{value=typeof failed?.raw==='string'?JSON.parse(failed.raw):failed?.raw??null;}catch{value=null;}
    }
    return value?.id===id&&value.canonicalSessionId===sessionId&&typeof value.text==='string'?value:null;
  };
  async function collect(snapshot) {
    const task=snapshot.task,ids=[...task.inputIds,...(task.contextInputIds??[])].filter(id=>SAFE_ID.test(id));
    const inputs=ids.map(id=>{
      const route=snapshot.inputs.find(record=>record?.id===id);
      const context={id,kind:route?.kind??'owner',requirement:task.inputIds.includes(id),state:route?.state??null};
      // Only the owner's own authenticated words are quoted; the host's inputs are named.
      if(route?.kind&&route.kind!=='owner')return context;
      const found=source(id);
      return found?{...context,text:excerpt(found.text),at:found.createdAt??null}:{...context,missing:true};
    });
    const outputs=[],unsentDrafts=[];
    for(const [id,delivery] of Object.entries(task.deliveries??{})) {
      const key=delivery.outboxId??id,message=SAFE_ID.test(key)?read(path.join(outboxDirectory,key+'.json')):null;
      if(message) {
        outputs.push({id,state:message.state??delivery.state,messageId:message.messageId??delivery.messageId??null,
          ...(message.text?{text:excerpt(message.text)}:{}),...(message.media?{file:{type:message.media.type,name:message.media.name}}:{})});
        continue;
      }
      let found=null;try{found=store()?.findBubble?.(id)??null;}catch{found=null;}
      if(found&&['draft','held'].includes(found.manifest?.state)&&found.bubble?.state==='unsent')
        unsentDrafts.push({id,replyId:found.bubble.request?.reply_id??null,text:excerpt(found.bubble.text??'')});
      else outputs.push({id,state:found?.bubble?.state??delivery.state,messageId:delivery.messageId??null});
    }
    let reply=null;try{reply=await lastReply();}catch{reply=null;}
    const tools={};for(const tool of Object.values(task.tools??{}))tools[tool.status]=(tools[tool.status]??0)+1;
    return {input:{task:{id:task.id,status:task.status,inputVersion:task.inputVersion,summary:task.summary,stopReason:task.stopReason??null,tools},
      inputs,outputs,unsentDrafts,lastPublicReply:reply?.text?{text:excerpt(reply.text),status:reply.status??null}:null},
      facts:{unsentDraftIds:unsentDrafts.map(draft=>draft.id),undelivered:outputs.filter(o=>['rejected','undeliverable'].includes(o.state)).map(o=>o.id)}};
  }
  return {collect};
}
