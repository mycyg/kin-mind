/** Completion is tied to the authenticated input, never a model notice or an
 * unrelated/partial outbound bubble. An intentional silence remains distinct. */
export function replyProgress(previous,event,detail={},at=new Date().toISOString()) {
 if(event==='incoming')return {awaitingReplyInputId:detail.inputId??null,replyDisposition:'waiting',replyWaitingReason:null};
 if(event==='control-reply')return {lastControlReplyAt:at,lastControlMessageId:detail.messageId};
 if(!detail.inputId||detail.inputId!==previous.awaitingReplyInputId)return null;
 if(event==='share-held'||event==='reply-review'){
  const resolved=['silent','merged'].includes(detail.state);
  const complete=detail.state==='accepted';
  return {replyDisposition:detail.state,replyWaitingReason:detail.reason??null,
   ...(resolved||complete?{awaitingReplySince:null,lastResolvedInputId:detail.inputId,lastResolvedAt:at}:{}),
   ...(detail.state==='merged'?{mergedInto:detail.mergedInto??null}:{})};
 }
 if(event==='reply-choice'&&['silent','merged'].includes(detail.state))return {awaitingReplySince:null,replyDisposition:detail.state,lastResolvedInputId:detail.inputId,lastResolvedAt:at,mergedInto:detail.mergedInto??null};
 if(event==='reply-complete')return {awaitingReplySince:null,replyDisposition:'accepted',lastResolvedInputId:detail.inputId,lastResolvedAt:at};
 return null;
}
