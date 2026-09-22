import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {atomicJson} from './mobile-router.mjs';
import {FINAL_NON_DELIVERY} from './work-lock-review.mjs';
import {readThroughArchive} from './state-pruner.mjs';

const digest=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const NATIVE_DELIVERY_GAPS=new Set(['native-tool-outbox-not-unique','native-tool-outbox-invalid','native-tool-artifact-invalid']);
const NATIVE_DELIVERY_CONFLICTS=new Set(['native-tool-receipt-ambiguous','native-tool-event-invalid','native-tool-proof-conflict']);

/** Owner-bound evidence readers are injected by the private host. No model
 * is allowed to supply a filesystem path, recipient or delivery receipt.
 * `manifests` is the host's `TransportManifests` (or a function returning it,
 * because the router starts before the reply guard does): a deferred reply is
 * a held manifest there, and the old `.pending.json` journal stays readable
 * for a host that runs with the manifest switched off. */
export function workEvidence({sessionId,inputDirectory,outboxDirectory,deferredDirectory,lastReply,wishes=async()=>[],cancelShare,failedInputDirectory,reconciliationFile,verifyReplacement,manifests=null,archivedState={},toolDeliveries=async()=>({proofs:[]})}) {
  // Every lookup here asks for one record by name, so each of them can afford to
  // look in the archive after missing. The directory scan below deliberately does
  // not: walking the archive as well would undo the reason anything was moved
  // there. What that scan needs is protected by the reference set instead —
  // a delivery this task still holds is a record the pruner never moves.
  const read=file=>readThroughArchive(file,archivedState).value??null;
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
  /** A transport's own reason travels as a static code, never as a message. */
  const code=(value,fallback)=>/^[a-z0-9-]{1,64}$/.test(value??'')?value:fallback;
  async function collect(snapshot) {
    const ids=[...snapshot.task.inputIds,...(snapshot.task.contextInputIds??[])];
    const allMessages=fs.existsSync(outboxDirectory)?fs.readdirSync(outboxDirectory).filter(f=>f.endsWith('.json')).map(f=>read(path.join(outboxDirectory,f))).filter(Boolean):[];
    const byMessage=new Map();
    for(const record of allMessages.filter(r=>r.messageId)){
      const list=byMessage.get(record.messageId)??[];list.push(record);byMessage.set(record.messageId,list);
    }
    const messageById=id=>{
      const records=byMessage.get(id)??[];
      if(records.length>1)throw Error('Platform message identity collision');
      return records[0];
    };
    const acceptedFiles=Object.values(snapshot.task.deliveries??{}).filter(d=>d.state==='accepted'&&d.messageId)
      .map(d=>messageById(d.messageId)).filter(r=>r?.artifact);
    const inputs=ids.map(id=>{
      if(!/^[\w:-]+$/.test(id))throw Error('Invalid host input id');
      const route=snapshot.inputs.find(x=>x.id===id);
      const context={id,classification:route?.reason,kind:route?.kind,workRequirement:route?.state==='accepted'&&route?.kind!=='handoff'&&snapshot.task.inputIds.includes(id),submissionState:route?.state};
      // Internal continuations are already part of the task. Unsubmitted chat
      // is not a new work requirement and has no accepted handset source.
      if(route?.kind==='handoff'||!snapshot.task.inputIds.includes(id)&&route?.state!=='accepted')return {...context,workRequirement:false};
      let source=read(path.join(inputDirectory,id+'.json'));
      if(!source&&failedInputDirectory&&route?.state==='failed-before-submit') {
        const failed=read(path.join(failedInputDirectory,id+'.json'));
        try{source=typeof failed?.raw==='string'?JSON.parse(failed.raw):failed?.raw;}catch{}
      }
      if(!source||source.id!==id||source.canonicalSessionId!==sessionId||!(source.senderId||source.wechatMessage?.from_user_id)||typeof source.text!=='string')throw Error('Authenticated input missing');
      return{...context,text:source.text,createdAt:source.createdAt,sourceHash:digest(source)};
    });
    const receipts={},outputs=[],cancellableDeferred=[],deferredProofs={},deferredGroups={};
    for(const [id,delivery] of Object.entries(snapshot.task.deliveries??{})) {
      const reconciliation=reconciliationFile?read(reconciliationFile)?.deliveries?.[id]:null;
      const messageByOutbox=outbox(delivery.outboxId??reconciliation?.attemptId??id);
      const messageByPlatform=delivery.messageId?messageById(delivery.messageId):null;
      const message=messageByOutbox??messageByPlatform;
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
        // Refused by the platform, or given up on by the transport: a final non-delivery.
        const refused=[found,filed].find(m=>FINAL_NON_DELIVERY.includes(m?.bubble.state));
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
          const reason=code(gone.bubble.reason??gone.manifest.retired?.reason,'retired');
          receipts[id]={state:'retired',reason,source:'transport-manifest'};
          outputs.push({id,retired:true,reason,receivedByServer:false});
        } else if(refused&&!cancellable) {
          // This bubble will never arrive. The review is told so, and told why, instead of
          // seeing an unconfirmed delivery it can only answer by keeping the lock.
          const reason=code(refused.bubble.reason,'transport-'+refused.bubble.state);
          receipts[id]={state:refused.bubble.state,reason,source:'transport-manifest'};
          outputs.push({id,undelivered:true,reason,receivedByServer:false});
        } else receipts[id]={state:cancellable?'not-submitted':message?.state??'unconfirmed',messageId:message?.messageId};
        if(cancellable) {
          const value={id,replyId:request.reply_id,text:request.text,reason:'A later authenticated input superseded this unsent ordinary reply candidate; classify its actual content before discarding.'};
          cancellableDeferred.push(value);deferredProofs[id]=found?manifestProof(found.manifest):digest(deferredView(entry));
          if(found)deferredGroups[id]=found.manifest.group_id;
        }
      }
    }
    // The host joins original completed tool results to current platform receipts.
    // A model's completion claim or a matching filename is not a delivery proof.
    // Re-read this on every collect, including the pre-commit evidence check.
    const native=await toolDeliveries(snapshot);
    if(!native||!Array.isArray(native.proofs)||(native.diagnostics!==undefined&&!Array.isArray(native.diagnostics)))throw Error('Invalid native delivery evidence');
    const deliveryEvidenceGaps=[],diagnosticSeen=new Set();
    for(const diagnostic of native.diagnostics??[]) {
      const keys=diagnostic&&typeof diagnostic==='object'&&!Array.isArray(diagnostic)?Object.keys(diagnostic).sort():[];
      const key=keys.length===2&&keys[0]==='code'&&keys[1]==='toolId'?diagnostic.toolId+'\u0000'+diagnostic.code:'';
      if(!key||typeof diagnostic.toolId!=='string'||!/^[\w:-]+$/.test(diagnostic.toolId)
        ||snapshot.task.tools?.[diagnostic.toolId]?.status!=='completed'
        ||(!NATIVE_DELIVERY_GAPS.has(diagnostic.code)&&!NATIVE_DELIVERY_CONFLICTS.has(diagnostic.code))
        ||diagnosticSeen.has(key))throw Error('Invalid native delivery evidence');
      diagnosticSeen.add(key);
      if(NATIVE_DELIVERY_CONFLICTS.has(diagnostic.code))throw Error('Native delivery evidence integrity conflict');
      deliveryEvidenceGaps.push({toolId:diagnostic.toolId,code:diagnostic.code});
    }
    deliveryEvidenceGaps.sort((a,b)=>a.toolId.localeCompare(b.toolId)||a.code.localeCompare(b.code));
    const nativeSeen=new Set(),nativeMessages=new Set();
    for(const proof of native.proofs) {
      if(proof?.state!=='accepted'||proof.source!=='native-tool-outbox'
        ||proof.sessionId!==sessionId||proof.taskId!==snapshot.task.id
        ||typeof proof.id!=='string'||!/^[\w:-]+$/.test(proof.id)
        ||typeof proof.messageId!=='string'||!proof.messageId
        ||snapshot.task.tools?.[proof.toolId]?.status!=='completed'
        ||!/^[a-f0-9]{64}$/.test(proof.sourceHash??'')
        ||!/^[a-f0-9]{64}$/.test(proof.artifact?.sha256??'')
        ||!Number.isSafeInteger(proof.artifact?.bytes)||proof.artifact.bytes<=0
        ||!['image','file','audio','video'].includes(proof.artifact?.type)
        ||typeof proof.artifact?.name!=='string')throw Error('Invalid native delivery proof');
      const id='native-tool:'+proof.id,proofHash=digest(proof);
      if(nativeSeen.has(id))throw Error('Duplicate native delivery proof');
      nativeSeen.add(id);
      if(nativeMessages.has(proof.messageId))throw Error('Duplicate native delivery message');
      nativeMessages.add(proof.messageId);

      // One platform message is one result. If the router already owns that accepted
      // delivery, the independently verified artifact enriches the same output; it
      // never creates a second result under the supplemental namespace.
      const deliveryOwners=Object.entries(snapshot.task.deliveries??{})
        .filter(([,delivery])=>delivery?.messageId===proof.messageId).map(([owner])=>owner);
      const receiptOwners=Object.entries(receipts)
        .filter(([,receipt])=>receipt?.messageId===proof.messageId).map(([owner])=>owner);
      const owners=[...new Set([...deliveryOwners,...receiptOwners])];
      if(Object.hasOwn(receipts,id)&&(!owners.length||owners.length!==1||owners[0]!==id))
        throw Error('Native delivery identity collision');
      if(owners.length) {
        if(owners.length!==1)throw Error('Native delivery message collision');
        const owner=owners[0],delivery=snapshot.task.deliveries?.[owner];
        if(delivery?.state!=='accepted'||delivery.messageId!==proof.messageId)
          throw Error('Native delivery message collision');
        const output=outputs.find(item=>item.id===owner),message=messageById(proof.messageId);
        if(output?.toolId&&output.toolId!==proof.toolId)throw Error('Native delivery tool collision');
        if(owner.startsWith('file-tool:')&&owner.slice(10)!==proof.toolId)
          throw Error('Native delivery tool collision');
        const originalFile={
          type:output?.file?.type??message?.media?.type??message?.artifact?.type,
          name:output?.file?.name??message?.media?.name??message?.artifact?.name,
          sha256:output?.file?.sha256??message?.artifact?.sha256,
          bytes:output?.file?.bytes??message?.artifact?.bytes??message?.media?.bytes,
        };
        if(output?.file||message?.media||message?.artifact) {
          for(const key of ['type','name','sha256','bytes'])
            if(originalFile[key]!=null&&originalFile[key]!==proof.artifact[key])
              throw Error('Native delivery artifact collision');
        }
        receipts[owner]={...receipts[owner],state:'accepted',messageId:proof.messageId,
          nativeSourceHash:proof.sourceHash,proofHash};
        const enriched={toolId:proof.toolId,file:{...proof.artifact},receivedByServer:true};
        if(output)Object.assign(output,enriched);
        else outputs.push({id:owner,text:'',...enriched});
        continue;
      }
      // Do not let supplemental evidence overwrite a router-owned delivery.
      receipts[id]={state:'accepted',messageId:proof.messageId,source:'native-tool-outbox',
        sourceHash:proof.sourceHash,proofHash};
      outputs.push({id,toolId:proof.toolId,text:'',file:{...proof.artifact},receivedByServer:true});
    }
    const reply=await lastReply();
    const background=(await wishes()).filter(w=>w.kind==='explore'&&['wanted','waiting','in_progress'].includes(w.status)&&!w.expired&&!w.needs_review)
      .map(w=>({id:w.id,kind:w.kind,status:w.status,topic:w.topic,content:w.content,sourceInputIds:(w.evidence??[]).map(e=>e.source_key).filter(id=>ids.includes(id))})).filter(w=>w.sourceInputIds.length);
    const toolEntries=Object.entries(snapshot.task.tools??{});
    return {input:{task:{id:snapshot.task.id,inputVersion:snapshot.task.inputVersion,summary:snapshot.task.summary,completionProposal:snapshot.task.completion??null,stopReason:snapshot.task.stopReason,
      toolSummary:{total:toolEntries.length,completed:toolEntries.filter(([,t])=>t.status==='completed').length},
      tools:Object.fromEntries(toolEntries.filter(([,t])=>t.status!=='completed'))},inputs,outputs,
      lastPublicReply:reply?{text:reply.text,status:reply.status,turnId:reply.turnId}:null,
      backgroundWishes:background,cancellableDeferred,...(deliveryEvidenceGaps.length?{deliveryEvidenceGaps}:{})},receipts,deferredProofs,...(Object.keys(deferredGroups).length?{deferredGroups}:{})};
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
