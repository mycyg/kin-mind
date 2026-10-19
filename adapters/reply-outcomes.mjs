/** Read existing input and transport records; no message bodies or new ledger.
 * Completion in native history is not evidence that the owner received it. */
export function replyOutcomeFacts(inputs={},groups={}, {now=Date.now(),limit=8}={}) {
  const facts=[];
  for(const g of Object.values(groups)) {
    const bubbles=g.bubbles??[],fragments=bubbles.flatMap(b=>b.fragments??[]);
    const unknown=fragments.filter(f=>['unknown','submitting'].includes(f.state)).length;
    const sent=bubbles.filter(b=>b.state==='accepted').length;
    const format=String(g.reason??'').startsWith('private-note-')||g.reason==='private-reply-envelope';
    const failed=bubbles.some(b=>['rejected','undeliverable'].includes(b.state));
    const state=unknown?'send-unknown':format?'format-failed':failed?'send-failed':null;
    if(!state)continue;
    facts.push({groupId:g.group_id,inputIds:[...new Set([g.reply_id,...(g.answered_input_ids??[])].filter(Boolean))],at:g.updated_at??g.created_at,
      state,sentBubbles:sent,unknownFragments:unknown});
  }
  for(const input of Object.values(inputs))if(input.nativeFailure&&!input.answer&&!facts.some(f=>f.inputIds.includes(input.id)))
    facts.push({inputIds:[input.id],at:input.nativeFailure.at,state:'generation-failed',sentBubbles:input.delivered??0,unknownFragments:input.uncertainDeliveries?.length??0});
  return facts.filter(f=>Number.isFinite(f.at)&&now-f.at<86400000).sort((a,b)=>b.at-a.at).slice(0,limit);
}

/** Reconcile existing fragment IDs before describing an unknown send. This port
 * has no transport sender: it only reads receipts and applies their outcome. */
export async function readReplyOutcomeFacts(inputs,manifests,options) {
  const before=manifests?.status()?.groups??{};
  for(const group of Object.values(before))if((group.bubbles??[]).some(b=>(b.fragments??[]).some(f=>['unknown','submitting'].includes(f.state))))
    try{await manifests.reconcileGroup(group.group_id);}catch{/* Keep its unresolved facts; never infer delivery from a failed lookup. */}
  return replyOutcomeFacts(inputs,manifests?.status()?.groups??{},options);
}
