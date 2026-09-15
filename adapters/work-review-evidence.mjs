import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {atomicJson} from './mobile-router.mjs';

const read=file=>{try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch{return null;}};
const digest=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');

/** Owner-bound evidence readers are injected by the private host. No model
 * is allowed to supply a filesystem path, recipient or delivery receipt. */
export function workEvidence({sessionId,inputDirectory,outboxDirectory,deferredDirectory,lastReply,wishes=async()=>[],cancelShare}) {
  const deferredFile=id=>path.join(deferredDirectory,createHash('sha256').update(id).digest('hex')+'.pending.json');
  const outbox=id=>read(path.join(outboxDirectory,id+'.json'));
  const deferredView=entry=>entry?{state:entry.state,request:entry.request,delivery:entry.delivery,ownerEpoch:entry.ownerEpoch}:null;
  async function collect(snapshot) {
    const ids=snapshot.task.inputIds;
    const inputs=ids.map(id=>{
      if(!/^[\w:-]+$/.test(id))throw Error('Invalid host input id');
      const source=read(path.join(inputDirectory,id+'.json'));
      if(!source||source.id!==id||source.canonicalSessionId!==sessionId||!source.senderId||typeof source.text!=='string')throw Error('Authenticated input missing');
      const route=snapshot.inputs.find(x=>x.id===id);
      return{id,text:source.text,createdAt:source.createdAt,sourceHash:digest(source),classification:route?.reason,kind:route?.kind};
    });
    const receipts={},outputs=[],cancellableDeferred=[],deferredProofs={};
    for(const [id,delivery] of Object.entries(snapshot.task.deliveries??{})) {
      const message=outbox(id);
      if(message?.state==='accepted'&&message.messageId&&message.messageId===delivery.messageId) {
        receipts[id]={state:'accepted',messageId:message.messageId,sourceHash:digest(message)};
        outputs.push({id,text:message.text??'',file:message.media?{type:message.media.type,name:message.media.name}:null,receivedByServer:true});
      } else if(id.startsWith('file-tool:')&&delivery.state==='accepted'&&delivery.messageId&&snapshot.task.tools[id.slice(10)]?.status==='completed') {
        receipts[id]={state:'accepted',messageId:delivery.messageId,source:'native-host-tool-receipt'};
        outputs.push({id,receivedByServer:true,kind:'file-tool'});
      } else {
        const entry=read(deferredFile(id)),inputIndex=ids.indexOf(entry?.request?.reply_id);
        const cancellable=!message&&entry?.state==='pending'&&entry.delivery?.id===id&&entry.delivery?.taskId===snapshot.task.id&&entry.delivery.kind==='reply'&&!entry.delivery.media&&!entry.delivery.artifact&&inputIndex>=0&&inputIndex<ids.length-1;
        receipts[id]={state:cancellable?'not-submitted':message?.state??'unconfirmed',messageId:message?.messageId};
        if(cancellable) {
          const value={id,replyId:entry.request.reply_id,text:entry.request.text,reason:'A later authenticated input superseded this unsent ordinary reply candidate; classify its actual content before discarding.'};
          cancellableDeferred.push(value);deferredProofs[id]=digest(deferredView(entry));
        }
      }
    }
    const reply=await lastReply();
    const background=(await wishes()).filter(w=>w.kind==='explore'&&['wanted','waiting','in_progress'].includes(w.status)&&!w.expired&&!w.needs_review)
      .map(w=>({id:w.id,kind:w.kind,status:w.status,topic:w.topic,content:w.content,sourceInputIds:(w.evidence??[]).map(e=>e.source_key).filter(id=>ids.includes(id))})).filter(w=>w.sourceInputIds.length);
    return {input:{task:{id:snapshot.task.id,inputVersion:snapshot.task.inputVersion,summary:snapshot.task.summary,completionProposal:snapshot.task.completion??null,stopReason:snapshot.task.stopReason,tools:snapshot.task.tools},inputs,outputs,
      lastPublicReply:reply?{text:reply.text,status:reply.status,turnId:reply.turnId}:null,
      backgroundWishes:background,cancellableDeferred},receipts,deferredProofs};
  }
  async function cancelDeferred(id,{reviewId,evidence}) {
    const file=deferredFile(id),entry=read(file);
    if(outbox(id)||!entry||entry.state!=='pending'||digest(deferredView(entry))!==evidence.deferredProofs?.[id])throw Error('Deferred message changed or transport began');
    if(!cancelShare)throw Error('Share cancellation unavailable');
    await cancelShare(entry.request.draft_id);
    // Cancellation releases the reservation; it never turns it into coverage.
    if(outbox(id)||digest(deferredView(read(file)))!==evidence.deferredProofs[id])throw Error('Deferred message changed while canceling');
    atomicJson(file,{...entry,state:'canceled',reason:'superseded-ordinary-reply',workReviewId:reviewId});
    return{state:'canceled-before-send',reviewId};
  }
  return{collect,cancelDeferred};
}
