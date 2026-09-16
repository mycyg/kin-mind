import {createHash} from 'node:crypto';
import {splitChatText} from './chat-bubbles.mjs';

/** The journal freezes content and IDs before sending. Unknown sends only reconcile. */
export function createContactBatch({read,write,send,receipt=()=>null,eligible=()=>true,preflight=async()=>({state:'ready'}),now=()=>Date.now()}) {
  const active=new Map();
  const digest=value=>createHash('sha256').update(value).digest('hex');
  async function run({id,text,bubbles,references=[],channel,guard=()=>true,superseded=false}) {
    let batch=await read(id);
    const parts=bubbles??(text===undefined?null:splitChatText(text));
    if(batch) {
      if(parts&&batch.digest!==digest(JSON.stringify(parts)))throw Error('contact-batch-content-conflict');
      if(references.length&&batch.referenceDigest&&batch.referenceDigest!==digest(JSON.stringify(references)))throw Error('contact-batch-reference-conflict');
    } else {
      if(!Array.isArray(parts)||!parts.length||parts.some(x=>typeof x!=='string'||!x.trim()))throw Error('contact-batch-empty');
      batch={id,channel,referenceDigest:digest(JSON.stringify(references)),digest:digest(JSON.stringify(parts)),state:'pending',items:parts.map((text,index)=>({
        id:'kin-bubble-'+digest(id+'\0'+index).slice(0,48),text,references:references[index]??[],state:'unsent'}))};
      await write(id,batch);
    }
    // Reconcile first, then review every unsent bubble before exposing an
    // introduction. A late review failure must not strand the promised body.
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
    const checkedItems=new Map();
    for(const [index,item] of batch.items.entries()) {
      if(item.state==='unsent') {
        if(!await eligible()||!await guard()){batch.state='pending';await write(id,batch);return result(batch);}
        if(item.reviewNotBefore&&now()<item.reviewNotBefore){batch.state='pending';return result(batch);}
        const checked=await preflight({draft_id:batch.id,text:item.text,references:item.references??[],channel:batch.channel,
          batch_text:batch.items.map(i=>i.text).join('\n\n'),bubble_index:index});
        if(['duplicate','silent','merged'].includes(checked.state)) {
          for(const pending of batch.items.filter(i=>i.state==='unsent'))Object.assign(pending,{state:'canceled',reason:checked.reason??checked.state});
          batch.reason=checked.reason??checked.state;await write(id,batch);break;
        }
        if(checked.state!=='ready'){item.reviewNotBefore=now()+(checked.retryAfterMs??60000);batch.reason=checked.reason;batch.state='pending';await write(id,batch);return result(batch);}
        checkedItems.set(item.id,checked);
      }
    }
    for(const item of batch.items) {
      if(item.state==='unsent') {
        const checked=checkedItems.get(item.id);
        item.references=checked.references??item.references??[];
        if(checked.text&&checked.text!==item.text){item.originalText=item.text;item.text=checked.text;}
        // A new owner input can arrive while semantic preflight is running.
        if(!await eligible()||!await guard()){batch.state='pending';await write(id,batch);return result(batch);}
        item.state='pending';await write(id,batch);
        try {
          const sent=await send({id:item.id,text:item.text,channel:batch.channel,memoryBatchId:batch.id,expectedBubbles:batch.items.length,references:item.references,draftId:batch.id});
          if(sent?.state!=='accepted'||!sent.messageId)throw Error('receipt-unconfirmed');
          Object.assign(item,{state:'accepted',messageId:sent.messageId});
        } catch {item.state='unconfirmed';batch.state='unconfirmed';await write(id,batch);return result(batch);}
      }
      await write(id,batch);
    }
    batch.state=batch.items.some(x=>x.state==='accepted')?'accepted':'canceled';delete batch.reason;await write(id,batch);return result(batch);
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
