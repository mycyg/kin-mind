import {createHash} from 'node:crypto';
import {splitChatText} from './chat-bubbles.mjs';
import {CHANNEL_CONTRACTS,channelContract} from './channel-contract.mjs';
import {fragmentText,fileFragment} from './text-fragments.mjs';
import {transportId} from './transport-manifest.mjs';

/** Verdicts that refuse a whole group. None of them applies to one bubble. */
const REFUSED=['duplicate','silent','merged'];
const BEGUN=['pending','accepted','unconfirmed'];
const HOLD_BASE_MS=60000,HOLD_MAX_MS=15*60000;

/** The journal freezes content and IDs before sending. Unknown sends only reconcile. */
export function createContactBatch({read,write,send,receipt=()=>null,eligible=()=>true,preflight=async()=>({state:'ready'}),verifyFile=async()=>{throw Error('File delivery is not configured');},contracts={},maxHolds=6,now=()=>Date.now()}) {
  const active=new Map();
  const digest=value=>createHash('sha256').update(value).digest('hex');
  // Proactive contact leaves through the Feishu bridge; a channel with no
  // published contract is measured by that one, with injected corrections.
  const textLimit=channel=>channelContract(CHANNEL_CONTRACTS[channel]?channel:'feishu',contracts[channel]).text;
  /** One entry per fragment, each with its own frozen ID: ordinary journal
   * entries, so the previous batch code replays the same bodies in the same
   * order. Content that no cut can fit stays whole and says why. */
  function cut(item,limit) {
    const plan=fragmentText(item.text,limit);
    if(plan.kind!=='text')return [{...item,oversize:{reason:plan.reason,name:fileFragment(item.text).name}}];
    if(plan.fragments.length<2)return [item];
    return plan.fragments.map(part=>({...item,id:transportId(item.id,part.index,part.body_sha256),text:item.text.slice(part.start,part.end),
      references:part.index?[]:item.references,part:{of:item.id,index:part.index,count:plan.fragments.length,body_sha256:part.body_sha256}}));
  }
  /** Freeze the reviewed bodies and references, then cut what the channel
   * cannot carry. A group that has already begun is never cut again. */
  function freeze(batch,reviewed,verdict) {
    if(verdict.checked&&verdict.checked.length!==reviewed.length)throw Error('contact-batch-review-incomplete');
    const limit=textLimit(batch.channel),begun=batch.items.some(x=>BEGUN.includes(x.state)),items=[];
    for(const item of batch.items) {
      const index=reviewed.indexOf(item);
      if(index<0){items.push(item);continue;}
      const checked=verdict.checked?.[index]??{};
      if(Array.isArray(checked.references))item.references=checked.references;
      if(checked.text?.trim()&&checked.text!==item.text){item.originalText=item.text;item.text=checked.text;}
      items.push(...(begun?[item]:cut(item,limit)));
    }
    batch.items=items;batch.review={at:now(),...(verdict.review_id?{id:verdict.review_id}:{})};delete batch.reason;
  }
  async function run({id,text,bubbles,files=[],references=[],channel,guard=()=>true,superseded=false}) {
    let batch=await read(id);
    const parts=bubbles??(text===undefined?null:splitChatText(text));
    const contentDigest=parts?digest(JSON.stringify(files.length?{parts,files}:parts)):null;
    if(batch) {
      if(parts&&batch.digest!==contentDigest)throw Error('contact-batch-content-conflict');
      if(references.length&&batch.referenceDigest&&batch.referenceDigest!==digest(JSON.stringify(references)))throw Error('contact-batch-reference-conflict');
    } else {
      if(!Array.isArray(parts)||!parts.length||parts.some(x=>typeof x!=='string'||!x.trim()))throw Error('contact-batch-empty');
      if(!Array.isArray(files)||files.length>24)throw Error('contact-batch-invalid-files');
      batch={id,channel,referenceDigest:digest(JSON.stringify(references)),digest:contentDigest,state:'pending',items:parts.map((text,index)=>({
        id:'kin-bubble-'+digest(id+'\0'+index).slice(0,48),text,references:references[index]??[],state:'unsent'}))};
      batch.items.push(...files.map((file,index)=>({id:'kin-file-'+digest(id+'\0'+index+'\0'+file.sha256).slice(0,48),file,state:'unsent'})));
      await write(id,batch);
    }
    // Reconcile first: what the transport wrote down outranks the journal.
    for(const item of batch.items) {
      if(['accepted','canceled'].includes(item.state))continue;
      if(item.state==='unsent'&&superseded){item.state='canceled';await write(id,batch);continue;}
      if(item.state!=='unsent') {
        const known=await receipt(item.id,batch.channel);
        if(known?.state==='accepted'&&known.messageId)Object.assign(item,{state:'accepted',messageId:known.messageId});
        else {batch.state='unconfirmed';await write(id,batch);return result(batch);}
        await write(id,batch);
      }
    }
    // One review of the whole group, before its first bubble is exposed: the
    // group is released, held with a visible reason, or refused as a whole. A
    // verdict never cancels a remainder the owner was already promised, and a
    // group that is already reviewed is never charged for a second one.
    if(!batch.review&&batch.items.some(x=>x.state==='unsent')) {
      if(!await eligible()||!await guard()){batch.state='pending';await write(id,batch);return result(batch);}
      if(batch.reviewNotBefore&&now()<batch.reviewNotBefore){batch.state='pending';return result(batch);}
      for(const item of batch.items.filter(x=>x.file&&x.state==='unsent')) {
        const checked=await verifyFile(item.file);
        if(checked.state!=='ready')return hold(batch,id,checked,false);   // a local check, free to repeat
      }
      const reviewed=batch.items.filter(x=>!x.file&&x.state==='unsent');
      const entries=reviewed.map((item,bubble_index)=>({draft_id:batch.id,text:item.text,references:item.references??[],bubble_index}));
      const body=entries.map(e=>e.text).join('\n\n');
      // `text` and `references` describe the same group as `entries`: a reviewer
      // that only knows the older shape still judges the whole body, never a part.
      const verdict=entries.length?await preflight({draft_id:batch.id,channel:batch.channel,entries,text:body,batch_text:body,
        references:entries.flatMap(e=>e.references)}):{state:'ready'};
      if(REFUSED.includes(verdict.state))return refuse(batch,id,verdict.reason??verdict.state);
      if(verdict.state!=='ready')return hold(batch,id,verdict,true);
      freeze(batch,reviewed,verdict);await write(id,batch);
    }
    for(const item of batch.items) {
      if(item.state==='unsent') {
        // A new owner input can arrive while the semantic review is running.
        if(!await eligible()||!await guard()){batch.state='pending';await write(id,batch);return result(batch);}
        item.state='pending';await write(id,batch);
        try {
          const sent=await send({id:item.id,text:item.text,file:item.file,channel:batch.channel,memoryBatchId:batch.id,expectedBubbles:batch.items.length,references:item.references,draftId:batch.id,
            // Unsplittable: the whole body as one file, with no words of the host's own.
            // A sender that understands `media` delivers the file INSTEAD of the text, never both;
            // `text` rides along only so that a sender that does not still has a body.
            ...(item.oversize?{media:{type:'file',name:item.oversize.name,data:Buffer.from(item.text,'utf8')}}:{})});
          if(sent?.state!=='accepted'||!sent.messageId)throw Error('receipt-unconfirmed');
          Object.assign(item,{state:'accepted',messageId:sent.messageId});
        } catch {item.state='unconfirmed';batch.state='unconfirmed';await write(id,batch);return result(batch);}
      }
      await write(id,batch);
    }
    batch.state=batch.items.some(x=>x.state==='accepted')?'accepted':'canceled';
    if(batch.state==='accepted')delete batch.reason;
    await write(id,batch);return result(batch);
  }
  /** Held as a whole: the reason stays visible and the next pass asks again.
   * A review that spends a model call is asked a bounded number of times; after
   * that the group is refused rather than held for ever. A verdict that named
   * nothing (`transient`: the reviewer was never reached) judged nothing, so it
   * backs off the same way but spends none of those tries. A verdict that names
   * its own retry time is obeyed; otherwise the wait doubles up to a quarter of
   * an hour. Eligibility, the guard and file checks cost nothing and are free. */
  async function hold(batch,id,verdict,paid) {
    const judged=paid&&verdict.transient!==true;
    if(judged&&(batch.holds=(batch.holds??0)+1)>=maxHolds)return refuse(batch,id,'contact-review-held-too-long');
    if(paid)batch.backoffs=(batch.backoffs??0)+1;
    batch.reviewNotBefore=verdict.retryAt??now()+(verdict.retryAfterMs??Math.min(HOLD_MAX_MS,HOLD_BASE_MS*2**Math.max(0,(batch.backoffs??1)-1)));
    batch.reason=verdict.reason;batch.state='pending';await write(id,batch);return result(batch);
  }
  /** Refused as a whole: every bubble that was never exposed is canceled under
   * one reason, and that reason stays on the finished batch. What was already
   * sent stays sent; the review precedes the first bubble, so usually nothing was. */
  async function refuse(batch,id,reason) {
    for(const item of batch.items.filter(x=>x.state==='unsent'))Object.assign(item,{state:'canceled',reason});
    batch.reason=reason;batch.state=batch.items.some(x=>x.state==='accepted')?'accepted':'canceled';
    await write(id,batch);return result(batch);
  }
  function result(batch){const ids=batch.items.filter(x=>x.state==='accepted').map(x=>x.messageId);return {
    id:batch.id,state:batch.state,reason:batch.reason,channel:batch.channel,messageId:batch.state==='accepted'?ids[0]:undefined,
    messageIds:ids,acceptedBubbles:ids.length,totalBubbles:batch.items.length,
    canceledBubbles:batch.items.filter(x=>x.state==='canceled').length,
    partial:batch.items.some(x=>x.state==='canceled'),visibility:'unverified'};}
  return request=>{
    if(active.has(request.id))return active.get(request.id);
    const pending=run(request).finally(()=>active.delete(request.id));active.set(request.id,pending);return pending;
  };
}
