/** Host clock facts live in dynamic turn context, never the persona prefix. */
export function timestamp(value) {
  if(value===null||value===undefined||value==='')return null;
  // Platform epochs can be seconds or milliseconds; ISO strings need a zone.
  if(typeof value==='string'&&/^\d+$/.test(value))value=Number(value);
  if(typeof value==='number'){if(value<=0)return null;value=value<1e11?value*1000:value;}
  else if(typeof value!=='string'||!/(?:Z|[+-]\d\d:\d\d)$/.test(value))return null;
  const parsed=new Date(value);
  return Number.isFinite(parsed.valueOf())?parsed.toISOString():null;
}
export function conversationClock(input={},now=new Date().toISOString()) {
  const current=timestamp(now);if(!current)throw Error('A valid host time is required');
  const local=new Date(Date.parse(current)+8*3600000).toISOString().replace('Z','+08:00');
  return {kind:'internal-conversation-time',current_time:current,local_time:local,timezone:'Asia/Singapore',
    input_id:input.id??null,input_kind:input.kind??'internal',
    occurred_at:timestamp(Object.hasOwn(input,'occurredAt')?input.occurredAt:input.at??input.create_time_ms??input.create_time),
    received_at:timestamp(input.receivedAt),
    instruction_authority:'Host clock facts. Historical timestamps retain their original meaning; internal events are not owner messages.'};
}
export function timeContext(input,now) {return '本轮时间事实：'+JSON.stringify(conversationClock(input,now));}
