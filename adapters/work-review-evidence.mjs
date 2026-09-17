import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {atomicJson} from './mobile-router.mjs';

const read=file=>{try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch{return null;}};
const digest=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');

/** Owner-bound evidence readers are injected by the private host. No model
 * is allowed to supply a filesystem path, recipient or delivery receipt.
 * `manifests` is the host's `TransportManifests` (or a function returning it,
 * because the router starts before the reply guard does): a deferred reply is
 * a held manifest there, and the old `.pending.json` journal stays readable
 * for a host that runs with the manifest switched off. */
export function workEvidence({sessionId,inputDirectory,outboxDirectory,deferredDirectory,lastReply,wishes=async()=>[],cancelShare,failedInputDirectory,reconciliationFile,verifyReplacement,manifests=null}) {
  const deferredFile=id=>path.join(deferredDirectory,createHash('sha256').update(id).digest('hex')+'.pending.json');
  const outbox=id=>read(path.join(outboxDirectory,id+'.json'));
  const deferredView=entry=>entry?{state:entry.state,request:entry.request,delivery:entry.delivery,ownerEpoch:entry.ownerEpoch}:null;
  const store=()=>typeof manifests==='function'?manifests():manifests;
  const began=bubble=>bubble.fragments.some(f=>f.state!=='unsent');
  // A deferred group is one unit: nothing of it reviewed, nothing of it handed to a transport.
  const deferred=manifest=>['draft','held'].includes(manifest.state)&&!manifest.review&&!manifest.bubbles.some(b=>began(b)||b.state!=='unsent'||b.fragments.some(f=>outbox(f.transport_id)));
  // Changes when anything about the group's bodies or what was sent of it changes; bookkeeping (retry times, revisions) does not count.
  const manifestProof=manifest=>digest({group:manifest.group_id,ownerEpoch:manifest.ownerEpoch,bubbles:manifest.bubbles.map(b=>[b.bubble_id,b.draft_id,b.body_sha256,b.state,b.fragments.map(f=>f.state)])});
  const accepted=bubble=>bubble.fragments.filter(f=>f.state==='accepted'&&f.receipt?.messageId).map(f=>String(f.receipt.messageId));
  async function collect(snapshot) {
    const ids=[...snapshot.task.inputIds,...(snapshot.task.contextInputIds??[])];
    const allMessages=fs.existsSync(outboxDirectory)?fs.readdirSync(outboxDirectory).filter(f=>f.endsWith('.json')).map(f=>read(path.join(outboxDirectory,f))).filter(Boolean):[];
    const byMessage=new Map(allMessages.filter(r=>r.state==='accepted'&&r.messageId).map(r=>[r.messageId,r]));
    const acceptedFiles=Object.values(snapshot.task.deliveries??{}).filter(d=>d.state==='accepted'&&d.messageId)
      .map(d=>byMessage.get(d.messageId)).filter(r=>r?.artifact);
    const inputs=ids.map(id=>{
      if(!/^[\w:-]+$/.test(id))throw Error('Invalid host input id');
      let source=read(path.join(inputDirectory,id+'.json'));
      const route=snapshot.inputs.find(x=>x.id===id);
      if(!source&&failedInputDirectory&&route?.state==='failed-before-submit') {
        const failed=read(path.join(failedInputDirectory,id+'.json'));
        try{source=typeof failed?.raw==='string'?JSON.parse(failed.raw):failed?.raw;}catch{}
      }
      if(!source||source.id!==id||source.canonicalSessionId!==sessionId||!source.senderId||typeof source.text!=='string')throw Error('Authenticated input missing');
      return{id,text:source.text,createdAt:source.createdAt,sourceHash:digest(source),classification:route?.reason,kind:route?.kind,workRequirement:snapshot.task.inputIds.includes(id),submissionState:route?.state};
    });
    const receipts={},outputs=[],cancellableDeferred=[],deferredProofs={},deferredGroups={};
    for(const [id,delivery] of Object.entries(snapshot.task.deliveries??{})) {
      const reconciliation=reconciliationFile?read(reconciliationFile)?.deliveries?.[id]:null;
      const message=outbox(delivery.outboxId??reconciliation?.attemptId??id)??byMessage.get(delivery.messageId);
      if(reconciliation&&verifyReplacement) {
        const proof=await verifyReplacement(reconciliation,message);
        if(proof?.state==='not-submitted'&&proof.fulfilledBy?.length) {
          receipts[id]=proof;outputs.push({id,failedAttempt:message?.media,replacement:proof,receivedByServer:false});continue;
        }
      }
      if(message?.stage==='upload-failed'&&message.submissionStarted===false&&verifyReplacement) {
        const groups=new Map();
        for(const item of acceptedFiles){
          const match=/^(.*)\.(\d{3})$/.exec(item.media?.name??'');
          if(match){const list=groups.get(match[1])??[];list.push({record:item,part:Number(match[2])});groups.set(match[1],list);}
        }
        const candidates=[...acceptedFiles.map(r=>[r.id]),...([...groups.values()].map(parts=>{
          parts.sort((a,b)=>a.part-b.part);return parts.every((p,i)=>p.part===i+1)?parts.map(p=>p.record.id):[];
        }).filter(g=>g.length))];
        let proof;
        for(const replacementIds of candidates){try{proof=await verifyReplacement({attemptId:message.id,replacementIds},message);break;}catch{/* Other artifacts are not equivalent evidence. */}}
        if(proof?.fulfilledBy?.length){receipts[id]=proof;outputs.push({id,failedAttempt:message.media,replacement:proof,receivedByServer:false});continue;}
      }
      if(message?.state==='accepted'&&message.messageId&&message.messageId===delivery.messageId) {
        receipts[id]={state:'accepted',messageId:message.messageId,sourceHash:digest(message)};
        outputs.push({id,text:message.text??'',file:message.media?{type:message.media.type,name:message.media.name}:null,receivedByServer:true});
      } else if(id.startsWith('file-tool:')&&delivery.state==='accepted'&&delivery.messageId&&snapshot.task.tools[id.slice(10)]?.status==='completed') {
        receipts[id]={state:'accepted',messageId:delivery.messageId,source:'native-host-tool-receipt'};
        outputs.push({id,receivedByServer:true,kind:'file-tool'});
      } else {
        // A deferred reply is a held manifest; the delivery ID is one of its bubbles. The old journal is the fallback.
        const found=!message?store()?.findBubble(id)??null:null,entry=found?null:read(deferredFile(id));
        // Nothing here explains this delivery yet. A bubble the host retired is the one
        // case worth looking for among the settled groups: without it this delivery stays
        // "unconfirmed" for ever and the work lock is never released on its merits.
        const filed=!message&&!found&&entry?.state!=='pending'?store()?.findBubble(id,{settled:true})??null:null;
        const gone=[found,filed].find(m=>m?.bubble.state==='canceled');
        const request=found?found.bubble.request:entry?.request,inputIndex=ids.indexOf(request?.reply_id);
        const ordinary=found?deferred(found.manifest)&&found.manifest.kind==='reply'&&found.manifest.taskId===snapshot.task.id      // a manifest bubble is text by construction
          :entry?.state==='pending'&&entry.delivery?.id===id&&entry.delivery?.taskId===snapshot.task.id&&entry.delivery.kind==='reply'&&!entry.delivery.media&&!entry.delivery.artifact;
        const cancellable=!message&&Boolean(ordinary)&&inputIndex>=0&&inputIndex<ids.length-1;
        const delivered=found&&found.bubble.state==='accepted'?accepted(found.bubble):[];
        if(delivered.length) {
          // Every fragment reached the platform; the router's own record names one of their message IDs.
          receipts[id]={state:'accepted',messageId:delivered.includes(String(delivery.messageId))?String(delivery.messageId):delivered[0],source:'transport-manifest'};
          outputs.push({id,text:found.bubble.text,file:null,receivedByServer:true});
        } else if(gone&&!cancellable) {
          // Retired before it began: settled, and never an obligation to deliver again.
          const reason=/^[a-z0-9-]{1,64}$/.test(gone.bubble.reason??gone.manifest.retired?.reason??'')?gone.bubble.reason??gone.manifest.retired.reason:'retired';
          receipts[id]={state:'retired',reason,source:'transport-manifest'};
          outputs.push({id,retired:true,reason,receivedByServer:false});
        } else receipts[id]={state:cancellable?'not-submitted':message?.state??'unconfirmed',messageId:message?.messageId};
        if(cancellable) {
          const value={id,replyId:request.reply_id,text:request.text,reason:'A later authenticated input superseded this unsent ordinary reply candidate; classify its actual content before discarding.'};
          cancellableDeferred.push(value);deferredProofs[id]=found?manifestProof(found.manifest):digest(deferredView(entry));
          if(found)deferredGroups[id]=found.manifest.group_id;
        }
      }
    }
    const reply=await lastReply();
    const background=(await wishes()).filter(w=>w.kind==='explore'&&['wanted','waiting','in_progress'].includes(w.status)&&!w.expired&&!w.needs_review)
      .map(w=>({id:w.id,kind:w.kind,status:w.status,topic:w.topic,content:w.content,sourceInputIds:(w.evidence??[]).map(e=>e.source_key).filter(id=>ids.includes(id))})).filter(w=>w.sourceInputIds.length);
    const toolEntries=Object.entries(snapshot.task.tools??{});
    return {input:{task:{id:snapshot.task.id,inputVersion:snapshot.task.inputVersion,summary:snapshot.task.summary,completionProposal:snapshot.task.completion??null,stopReason:snapshot.task.stopReason,
      toolSummary:{total:toolEntries.length,completed:toolEntries.filter(([,t])=>t.status==='completed').length},
      tools:Object.fromEntries(toolEntries.filter(([,t])=>t.status!=='completed'))},inputs,outputs,
      lastPublicReply:reply?{text:reply.text,status:reply.status,turnId:reply.turnId}:null,
      backgroundWishes:background,cancellableDeferred},receipts,deferredProofs,...(Object.keys(deferredGroups).length?{deferredGroups}:{})};
  }
  /** A deferred group is cancelled as the unit it was deferred as: the reservation of every one of
   * its bubbles is released, not only the first one's. */
  async function cancelManifest(groupId,id,{reviewId,evidence}) {
    const transports=store(),seen=transports?.read(groupId);
    // Another bubble of this group was discarded by the same review a moment ago: the group is already gone (and may be filed away).
    const gone=manifest=>manifest.retired?.reason==='superseded-ordinary-reply'&&manifest.bubbles.every(b=>b.state==='canceled');
    if(!seen||outbox(id)||!seen.bubbles.some(b=>b.bubble_id===id))throw Error('Deferred message changed or transport began');
    let outcome=gone(seen)?'canceled':'changed';
    if(outcome!=='canceled') {
      if(manifestProof(seen)!==evidence.deferredProofs?.[id])throw Error('Deferred message changed or transport began');
      const result=await transports.run(groupId,{operator:async(manifest,context)=>{
        // Under the lease nothing else can begin a send: what is checked here stays true while the group is retired.
        if(gone(manifest)){outcome='canceled';return;}
        if(!deferred(manifest)||manifestProof(manifest)!==evidence.deferredProofs[id])return;
        manifest.work_review_id=reviewId;
        await transports.retire(manifest,context,{reason:'superseded-ordinary-reply'});outcome='canceled';
      }});
      if(result.busy||result.lost||result.missing||outcome!=='canceled')throw Error('Deferred message changed or transport began');
    }
    // Cancellation releases the reservations; it never turns them into coverage. The manifest releases
    // them itself and retries; a manifest store without that wiring gets them released here.
    const left=transports.read(groupId)?.bubbles.filter(b=>b.draft_id&&!b.share_canceled)??[];
    if(left.length&&transports.cancelShare)throw Error('Deferred message reservations are not all released yet');
    if(left.length&&!cancelShare)throw Error('Share cancellation unavailable');
    for(const bubble of left)await cancelShare(bubble.draft_id);
    return{state:'canceled-before-send',reviewId};
  }
  async function cancelDeferred(id,{reviewId,evidence}) {
    const groupId=evidence.deferredGroups?.[id];
    if(groupId)return cancelManifest(groupId,id,{reviewId,evidence});
    const file=deferredFile(id),entry=read(file);
    if(outbox(id)||!entry||entry.state!=='pending'||digest(deferredView(entry))!==evidence.deferredProofs?.[id])throw Error('Deferred message changed or transport began');
    if(!cancelShare)throw Error('Share cancellation unavailable');
    // A deferred group holds one reservation per bubble: release every one of them, not only the first.
    for(const draftId of new Set((entry.entries??[entry]).map(item=>item.request?.draft_id).filter(Boolean)))await cancelShare(draftId);
    // Cancellation releases the reservation; it never turns it into coverage.
    if(outbox(id)||digest(deferredView(read(file)))!==evidence.deferredProofs[id])throw Error('Deferred message changed while canceling');
    atomicJson(file,{...entry,state:'canceled',reason:'superseded-ordinary-reply',workReviewId:reviewId});
    return{state:'canceled-before-send',reviewId};
  }
  return{collect,cancelDeferred};
}
