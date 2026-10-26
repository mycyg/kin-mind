/** Read existing input and transport records; no message bodies or new ledger.
 * Completion in native history is not evidence that the owner received it. */
export function replyOutcomeFacts(inputs={},groups={}, {now=Date.now(),limit=8,includeAccepted=false}={}) {
  const facts=[];
  for(const g of Object.values(groups)) {
    const bubbles=g.bubbles??[],fragments=bubbles.flatMap(b=>b.fragments??[]);
    const unknown=fragments.filter(f=>['unknown','submitting'].includes(f.state)).length;
    const sent=bubbles.filter(b=>b.state==='accepted').length;
    const format=String(g.reason??'').startsWith('private-note-')||g.reason==='private-reply-envelope';
    const failed=bubbles.some(b=>['rejected','undeliverable'].includes(b.state));
    // A fully accepted group is already in the native history; only an unsettled one is a fact.
    const state=unknown?'send-unknown':format?'format-failed':failed?'send-failed':includeAccepted&&sent&&g.state!=='accepted'?'partially-accepted':null;
    if(!state)continue;
    facts.push({groupId:g.group_id,inputIds:[...new Set([g.reply_id,...(g.answered_input_ids??[])].filter(Boolean))],at:g.updated_at??g.created_at,
      state,sentBubbles:sent,unknownFragments:unknown,...(bubbles.some(b=>b.reason==='tts-failed')?{reason:'tts-failed',sentTextFragments:fragments.filter(f=>f.kind==='text'&&f.state==='accepted').length}:{})});
  }
  for(const input of Object.values(inputs))if(input.nativeFailure&&!input.answer&&!facts.some(f=>f.inputIds.includes(input.id)))
    facts.push({inputIds:[input.id],at:input.nativeFailure.at,state:'generation-failed',sentBubbles:input.delivered??0,unknownFragments:input.uncertainDeliveries?.length??0});
  return facts.filter(f=>Number.isFinite(f.at)&&now-f.at<86400000).sort((a,b)=>b.at-a.at).slice(0,limit);
}

const RECONCILE_EVERY_MS=5*60000,reconciledAt=new WeakMap();
/** Reconcile existing fragment IDs before describing an unknown send. This port
 * has no transport sender: it only reads receipts and applies their outcome.
 * Only groups from the last day are looked up, each at most once per interval. */
export async function readReplyOutcomeFacts(inputs,manifests,options) {
  const before=manifests?.status()?.groups??{},now=options?.now??Date.now(),seen=manifests?reconciledAt.get(manifests)??new Map():null;
  if(manifests){reconciledAt.set(manifests,seen);for(const [id,at] of seen)if(now-at>86400000)seen.delete(id);}
  for(const group of Object.values(before))if(now-(group.updated_at??group.created_at)<86400000&&(seen.get(group.group_id)??-Infinity)<=now-RECONCILE_EVERY_MS&&
    (group.bubbles??[]).some(b=>(b.fragments??[]).some(f=>['unknown','submitting'].includes(f.state)))) {
    seen.set(group.group_id,now);
    try{await manifests.reconcileGroup(group.group_id);}catch{/* Keep its unresolved facts; never infer delivery from a failed lookup. */}
  }
  const facts=replyOutcomeFacts(inputs,manifests?.status()?.groups??{},options);
  if(options?.includeAccepted)for(const fact of facts) {
    const group=manifests.load?.(fact.groupId);
    fact.acceptedBubbles=(group?.bubbles??[]).filter(b=>b.state==='accepted').map(b=>({id:b.bubble_id,text:b.text}));
  }
  return facts;
}
