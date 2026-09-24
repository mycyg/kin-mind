import fs from 'node:fs/promises';
import {createHash, randomUUID} from 'node:crypto';
import {channelContract,classifyReceipt,normalizeReceipt} from './channel-contract.mjs';
import {fragmentText} from './text-fragments.mjs';

const hash = value => createHash('sha256').update(value).digest('hex');
export const deliveryIds = entry => Array.from({length:1+(entry.artifacts?.length??0)},(_,i)=>'handoff-'+hash(entry.id+':'+i).slice(0,48));
/** One command identifier per distinct receipt content: the store refuses a
 * reused identifier with other content, and repeats the same one idempotently. */
const commandId=(kind,entry,messageIds)=>(kind+':'+hash(JSON.stringify([entry.id,messageIds])).slice(0,32));

/** What each part's own receipt proves, read the way every sender here reads
 * one (AD2-20, T1-04): accepted with a message ID, refused by the platform,
 * proven never submitted (`absent` / `never-started`), or unknown. */
async function partStates(ids,lookup) {
  return Promise.all(ids.map(async id=>{
    let receipt;
    try{receipt=normalizeReceipt(await lookup(id));}catch{receipt={state:'unreadable'};}
    return {id,receipt,kind:classifyReceipt(receipt)};
  }));
}

// send/lookup must be bound to the configured owner, never an address in task data.
// Native/model completion is separate from acceptance of every transport part.
export async function deliverHandoff(entry,{settle,send,lookup,read=path=>fs.readFile(path),contract=channelContract('feishu')}) {
  const ids=deliveryIds(entry);
  if(['sending','unconfirmed'].includes(entry.state)) {
    const parts=await partStates(ids,lookup),accepted=parts.filter(p=>p.kind==='accepted');
    if(accepted.length===ids.length)
      return settle(entry.id,{command_id:'reconcile:'+entry.id,state:'accepted',message_id:accepted[0].receipt.messageId,message_ids:accepted.map(p=>p.receipt.messageId)});
    // An attempt that stopped half-way (a restart): it is settled as unknown
    // first, and the next pass reads each part's receipt again.
    if(entry.state==='sending')return settle(entry.id,{command_id:'interrupted:'+entry.id,state:'unconfirmed'});
    // Only a receipt that cannot tell stops the handoff: it is looked up again, never resent.
    if(parts.some(p=>p.kind==='unknown'))return {state:'unconfirmed',id:entry.id};
    // The platform refused a part: final for that part and for the handoff.
    if(parts.some(p=>p.kind==='rejected')) {
      const messageIds=accepted.map(p=>p.receipt.messageId);
      return settle(entry.id,{command_id:commandId('rejected',entry,messageIds),state:'failed',message_ids:messageIds});
    }
    // Every other part is accepted or proven never submitted: only the missing
    // ones go out, each under its original ID.
    return transmit(entry,{settle,send,lookup,read,contract,done:new Map(accepted.map(p=>[p.id,p.receipt.messageId]))});
  }
  if(entry.state!=='pending')return {state:entry.state,id:entry.id};
  // Competing consumers use distinct claim commands; the store's state check
  // elects one. Outbound message IDs remain stable across every inspection.
  await settle(entry.id,{command_id:'attempt:'+randomUUID(),state:'sending'});
  return transmit(entry,{settle,send,lookup,read,contract,done:new Map()});
}

async function transmit(entry,{settle,send,lookup,read,contract,done}) {
  const ids=deliveryIds(entry),body=entry.title+'\n'+entry.text;
  // A text past the channel's limit goes whole, as one file, like a reply bubble would.
  const plan=fragmentText(body,contract.text);
  const parts=[plan.kind==='text'&&plan.fragments.length===1?{id:ids[0],text:body,kind:'handoff-'+entry.kind}
    :{id:ids[0],media:{type:'file',data:Buffer.from(body,'utf8'),name:'handoff.txt'},kind:'handoff-'+entry.kind}];
  try {
    for(const [i,artifact] of (entry.artifacts??[]).entries()) {
      if(done.has(ids[i+1])){parts.push({id:ids[i+1]});continue;}
      const data=await read(artifact.path);
      if(hash(data)!==artifact.sha256||data.length>contract.file.maxBytes)throw Error('Artifact changed or exceeds channel limit');
      parts.push({id:ids[i+1],media:{type:'file',data,name:artifact.name},kind:'handoff-result'});
    }
  } catch {
    return settle(entry.id,{command_id:'invalid-artifact:'+entry.id,state:'failed'});
  }
  const messageIds=[];
  for(const part of parts) {
    if(done.has(part.id)){messageIds.push(done.get(part.id));continue;}
    let receipt=null;
    try{receipt=normalizeReceipt(await send(part));}catch{/* The transport's own receipt decides. */}
    if(classifyReceipt(receipt)!=='accepted') {
      try{receipt=normalizeReceipt(await lookup(part.id))??receipt;}catch{receipt={state:'unreadable'};}
    }
    const kind=classifyReceipt(receipt);
    if(kind==='accepted'){messageIds.push(receipt.messageId);continue;}
    if(kind==='rejected')return settle(entry.id,{command_id:commandId('rejected',entry,messageIds),state:'failed',message_ids:messageIds});
    // Never submitted, or unknown: the next pass reads every receipt again and
    // sends only what provably never left.
    return settle(entry.id,{command_id:commandId('uncertain',entry,messageIds),state:'unconfirmed',message_ids:messageIds});
  }
  return settle(entry.id,{command_id:'receipt:'+entry.id,state:'accepted',message_id:messageIds[0],message_ids:messageIds});
}
