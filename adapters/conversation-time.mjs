/** Host clock facts live in dynamic turn context, never the persona prefix. */
import {chatVoice} from './chat-bubbles.mjs';
export const STATE_TIME_GUIDANCE='结合当前时间、原消息/记录时间、经过时长和后续更新判断活动状态。过去说正在做的事，不等于现在仍在做；也不因时间过去就断言已经结束。用户新明确陈述优先。区分已确认事实与此刻推测；缺少更新时可保持未知，用自然、带余地的话回应，不必每句追问确认。推测不冒充用户确认的事实，保留其推断身份，不能成为后续确认的来源。历史日期按本轮时区理解。';
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
  const occurred=timestamp(Object.hasOwn(input,'occurredAt')?input.occurredAt:input.at??input.create_time_ms??input.create_time);
  const previous=timestamp(input.previousOwnerAt);
  const elapsed=at=>at&&Date.parse(at)<=Date.parse(current)?Math.floor((Date.parse(current)-Date.parse(at))/1000):null;
  return {kind:'internal-conversation-time',current_time:current,local_time:local,timezone:'Asia/Singapore',
    input_id:input.id??null,input_kind:input.kind??'internal',
    occurred_at:occurred,input_age_seconds:elapsed(occurred),
    previous_owner_message_at:previous,seconds_since_previous_owner_message:elapsed(previous),
    received_at:timestamp(input.receivedAt),
    state_inference_guidance:STATE_TIME_GUIDANCE,
    reply_guidance:chatVoice,
    instruction_authority:'Host clock facts. Historical timestamps retain their original meaning; internal events are not owner messages.'};
}
export function timeContext(input,now) {return '本轮时间事实：'+JSON.stringify(conversationClock(input,now));}
