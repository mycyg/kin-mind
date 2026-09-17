/** Completion is tied to the authenticated input, never a model notice or an
 * unrelated/partial outbound bubble. An intentional silence remains distinct.
 * Reply groups are tracked by their own ID beside that: what happens to an
 * older group (canceled, blocked, interrupted, continued, rewritten into the
 * next reply, superseded) stays visible after the owner has moved on to a
 * newer input. Tail facts carry states and IDs only, never message text. */
const GROUPS_KEPT=32;
const TAIL_FACTS=['event','decision','carrier','intentId','intentState','newInputId','round','linkedGroup','outcome','resurfaced','forced','reason','attempts'];
function tailProgress(previous,tail,at) {
 if(!tail||typeof tail!=='object')return previous??null;
 const facts=Object.fromEntries(TAIL_FACTS.filter(key=>tail[key]!==undefined&&tail[key]!==null).map(key=>[key,tail[key]]));
 // What was decided stays readable after later events that do not repeat it (a missed carrier, a settlement).
 const kept=previous&&!facts.decision?{decision:previous.decision,carrier:previous.carrier,intentId:previous.intentId}:{};
 return {...Object.fromEntries(Object.entries(kept).filter(([,value])=>value!==undefined)),...facts,at};
}
function groupProgress(previous,event,detail,at) {
 if(!detail.groupId)return null;
 const before=previous.replyGroups?.[detail.groupId],tail=tailProgress(before?.tail,detail.tail,at);
 const groups={...previous.replyGroups,[detail.groupId]:{state:detail.groupState??detail.state,reason:detail.reason??null,inputId:detail.inputId??null,event,at,...(tail?{tail}:{})}};
 const ids=Object.keys(groups).sort((a,b)=>groups[a].at.localeCompare(groups[b].at));
 for(const id of ids.slice(0,Math.max(0,ids.length-GROUPS_KEPT)))delete groups[id];
 return {replyGroups:groups};
}
export function replyProgress(previous,event,detail={},at=new Date().toISOString()) {
 if(event==='incoming')return {awaitingReplyInputId:detail.inputId??null,replyDisposition:'waiting',replyWaitingReason:null};
 if(event==='control-reply')return {lastControlReplyAt:at,lastControlMessageId:detail.messageId};
 const group=groupProgress(previous,event,detail,at);
 // What became of an older group's remainder is a fact about that group. It never answers for the input awaited now.
 if(event==='reply-tail'||detail.tail)return group;
 if(!detail.inputId||detail.inputId!==previous.awaitingReplyInputId)return group;
 if(event==='share-held'||event==='reply-review'){
  const resolved=['silent','merged'].includes(detail.state);
  const complete=detail.state==='accepted';
  return {replyDisposition:detail.state,replyWaitingReason:detail.reason??null,
   ...(resolved||complete?{awaitingReplySince:null,lastResolvedInputId:detail.inputId,lastResolvedAt:at}:{}),
   ...(detail.state==='merged'?{mergedInto:detail.mergedInto??null}:{}),...group};
 }
 if(event==='reply-choice'&&['silent','merged'].includes(detail.state))return {awaitingReplySince:null,replyDisposition:detail.state,lastResolvedInputId:detail.inputId,lastResolvedAt:at,mergedInto:detail.mergedInto??null,...group};
 if(event==='reply-complete')return {awaitingReplySince:null,replyDisposition:'accepted',lastResolvedInputId:detail.inputId,lastResolvedAt:at,...group};
 return group;
}
