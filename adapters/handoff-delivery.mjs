import fs from 'node:fs/promises';
import {createHash, randomUUID} from 'node:crypto';

const hash = value => createHash('sha256').update(value).digest('hex');
export const deliveryIds = entry => Array.from({length:1+(entry.artifacts?.length??0)},(_,i)=>'handoff-'+hash(entry.id+':'+i).slice(0,48));

// send/lookup must be bound to the configured owner, never an address in task data.
// Native/model completion is separate from acceptance of every transport part.
export async function deliverHandoff(entry,{settle,send,lookup,read=path=>fs.readFile(path)}) {
  const ids=deliveryIds(entry);
  if(['sending','unconfirmed'].includes(entry.state)) {
    const receipts=await Promise.all(ids.map(lookup));
    if(receipts.every(r=>r?.state==='accepted'&&r.messageId)) {
      return settle(entry.id,{command_id:'reconcile:'+entry.id,state:'accepted',message_id:receipts[0].messageId,message_ids:receipts.map(r=>r.messageId)});
    }
    if(entry.state==='sending')return settle(entry.id,{command_id:'interrupted:'+entry.id,state:'unconfirmed'});
    return {state:'unconfirmed',id:entry.id};
  }
  if(entry.state!=='pending')return {state:entry.state,id:entry.id};
  // Competing consumers use distinct claim commands; the store's state check
  // elects one. Outbound message IDs remain stable across every inspection.
  await settle(entry.id,{command_id:'attempt:'+randomUUID(),state:'sending'});
  const parts=[{id:ids[0],text:entry.title+'\n'+entry.text,kind:'handoff-'+entry.kind}];
  try {
    for(const [i,artifact] of (entry.artifacts??[]).entries()) {
      const data=await read(artifact.path);
      if(hash(data)!==artifact.sha256||data.length>50*1024*1024)throw Error('Artifact changed or exceeds channel limit');
      parts.push({id:ids[i+1],media:{type:'file',data,name:artifact.name},kind:'handoff-result'});
    }
  } catch {
    return settle(entry.id,{command_id:'invalid-artifact:'+entry.id,state:'failed'});
  }
  const receipts=[];
  try {
    for(const part of parts) {
      const receipt=await send(part);
      if(receipt.state!=='accepted'||!receipt.messageId)throw Error('Channel receipt unconfirmed');
      receipts.push(receipt.messageId);
    }
    return settle(entry.id,{command_id:'receipt:'+entry.id,state:'accepted',message_id:receipts[0],message_ids:receipts});
  } catch {
    return settle(entry.id,{command_id:'uncertain:'+entry.id,state:'unconfirmed',message_ids:receipts});
  }
}
