import {chatVoice} from './chat-bubbles.mjs';

/** The decision is the one JSON object the concatenated output ends with, fence
 * marks aside. It is found by trying each `{` from the last one backwards until
 * a slice parses, so commentary in front of it — balanced or not — cannot
 * swallow it. Concatenating first means a decision split across segments still
 * arrives whole; requiring it to end the output means a stale earlier draft
 * followed by unusable text is never revived. */
function finalObject(text) {
  const end=text.replace(/[\s`~]*$/,'').length;
  if(text[end-1]!=='}')return null;
  for(let start=text.lastIndexOf('{',end-1);start>=0;start=text.lastIndexOf('{',start-1)) {
    try {
      const value=JSON.parse(text.slice(start,end));
      if(value&&typeof value==='object'&&!Array.isArray(value))return value;
    } catch { /* Not where the decision begins; a wider one may still be there. */ }
  }
  return null;
}

/** How long Kin may put a contact off (N9): five minutes to three days. A wait past either
 * end is taken to that end and kept, never refused (kin_mind.state keeps the same range). */
export const CONTACT_WAIT_SECONDS={min:300,max:259200};

/** What a wait or an abandon says when the draft gave no reason: the host's words, and only that. */
export const UNSTATED_REASON=Object.freeze({wait:'草稿选择先等，没有写明原因',abandon:'草稿选择放下，没有写明原因'});
/** How much of a reason is kept, in characters; a longer one is cut there, never refused. */
export const REASON_MAX_CHARS=1200;
const WAIT_CONDITIONS=new Set(['time','new_evidence','owner_reply']);

/** A decision, or null when the output is not one. No body is ever shortened:
 * content the channel cannot carry in one message is fragmented downstream. A decision names its
 * action: an object with text and no action is not a send (AD2-17).
 *
 * The strict output schema (host boundaries.mjs) carries every field for every action, so what it
 * cannot rule out is read here the one safe way, never refused as a format failure: a fork draft
 * never got through with a wait that named no condition, a wait or an abandon without a reason, or
 * a send with no bubbles, and each such failure counted towards setting the wish aside (2026-09-27).
 * A blank wish id names none; a blank bubble says nothing and goes with its references; a send left
 * with no words is still a send, which the host records as empty output; a reason left blank is
 * said to be so in the host's words; a wait with no condition waits for new material, as the store's
 * own default does (kin_mind.state.ContactDecision). Her action is never changed. */
function decide(result) {
  // Which of the offered wishes this decision acts on (N11). Unnamed, null or empty (a strict
  // output schema always carries the field) means all of them.
  let chosen={};
  if(result.desire_ids!==undefined&&result.desire_ids!==null) {
    const ids=result.desire_ids;
    if(!Array.isArray(ids)||!ids.every(x=>typeof x==='string'))return null;
    const named=[...new Set(ids.map(x=>x.trim()).filter(Boolean))];
    if(named.length)chosen={desire_ids:named};
  }
  if(result.action==='send') {
    if(result.bubbles!==undefined&&result.bubbles!==null) {
      if(!Array.isArray(result.bubbles))return null;
      // A bubble is a string, or {text, references} when it cites what it shares.
      const items=result.bubbles.map(x=>typeof x==='string'?{text:x}:x&&typeof x==='object'&&typeof x.text==='string'
        &&(x.references===undefined||x.references===null||Array.isArray(x.references))?{text:x.text,references:x.references??[]}:null);
      if(items.some(x=>x===null))return null;
      const kept=items.filter(x=>x.text.trim());
      if(kept.length||typeof result.text!=='string') {
        const bubbles=kept.map(x=>x.text.trim());
        return {action:'send',text:bubbles.join('\n\n'),bubbles,
          ...(kept.some(x=>x.references)?{references:kept.map(x=>x.references??[])}:{}),...chosen};
      }
    }
    return {action:'send',text:typeof result.text==='string'?result.text.trim():'',...chosen};
  }
  if(!['wait','abandon'].includes(result.action))return null;
  if(result.reason!==undefined&&result.reason!==null&&typeof result.reason!=='string')return null;
  // By code points, so a cut never splits a character.
  const reason=Array.from((result.reason??'').trim()).slice(0,REASON_MAX_CHARS).join('').trim()||UNSTATED_REASON[result.action];
  if(result.action==='abandon')return {action:'abandon',reason,...chosen};
  const condition=result.condition??'new_evidence';
  if(!WAIT_CONDITIONS.has(condition))return null;
  const requested=result.retry_after_seconds??1800;
  if(condition==='time'&&(typeof requested!=='number'||!Number.isFinite(requested)))return null;
  const seconds=Math.min(CONTACT_WAIT_SECONDS.max,Math.max(CONTACT_WAIT_SECONDS.min,Math.round(requested)));
  return {action:'wait',reason,condition,...(condition==='time'?{retry_after_seconds:seconds}:{}),...chosen};
}

/** Public output only. A parse failure is an execution error, not a wish decision. */
export function parseContactDraft(outputs) {
  const result=finalObject((Array.isArray(outputs)?outputs:[outputs]).filter(raw=>typeof raw==='string'&&raw.trim()).join(''));
  const decision=result?decide(result):null;
  if(!decision)throw new Error('contact-draft-invalid-result');
  return decision;
}

export const contactDraftInstructions = `${chatVoice}\n内部主动联系草稿事件，不是用户的新消息，不伪造用户回复。
引用已有探索发现或作品要点时，使用带引用的气泡：{"action":"send","bubbles":[{"text":"公开短句","references":[{"unit_id":"读取到的发现编号","version":1,"mode":"new","reason":"简短依据"}]}]}。没有引用的日常亲昵继续使用字符串气泡。已分享发现使用development/reflection/reminiscence/retelling，说明与先前的关系；普通改写仍属于旧结论。内部编号与依据只在结构字段中，公开text保持自然口语。
当前状态中的expression是与普通聊天和桌面共享的当轮表达倾向。结合最多三条相关心事接话，保持它们的判断性质；一条愿望发送完成后，关联心事仍可等待后续结果。rhythm描述随互动形成的表达节奏。
这是你先前记下的联系愿望，此刻说不说、怎么说、等多久由你判断。此刻已就绪的愿望会一起给你：挑一条、几条一起说，或都先不说；用 desire_ids 写明这次针对哪几条，不写就是全部。亲昵接话、具体玩笑、胡思乱想、想撒娇或闲扯都可以成为内容，聊天不必追求意义，也不需要用户先问或给分享时机。猜想和想象按其身份表达，不编造经历。
结果未知的发送会作为事实列出：它们可能已经送到，宿主按原编号对账；不要把同一条换个编号重说，想说新的、不同的内容可以说。
我结合共同记忆、最近聊天、她的作息和当前心思判断联系时机。忙碌、夜间或未回复不自动阻止我开口；想她时可以撒娇式呼唤，也可以决定等等，不必另编新话题。具体时长以interaction_timing为准。只有某项具体事情必须等一个答案才能推进时，才选择owner_reply；撒娇呼唤本身可以发出。
只给出结构化草稿，不调用发送、提醒或文件工具。返回以下一种 JSON，不输出推理过程：
{"action":"send","desire_ids":["这次说的愿望编号"],"bubbles":["..."]}
{"action":"abandon","desire_ids":["..."],"reason":"内容已经讲过或愿望失效的简短依据"}
{"action":"wait","desire_ids":["..."],"condition":"time","retry_after_seconds":1800,"reason":"暂不适合、稍后复核的具体原因"}
{"action":"wait","desire_ids":["..."],"condition":"owner_reply","reason":"需要等待用户的回应"}
{"action":"wait","desire_ids":["..."],"condition":"new_evidence","reason":"需要什么新资料才能形成内容"}
time 表示已有内容的临时推迟，复核间隔为 300—259200 秒（五分钟到三天）；new_evidence 表示内容不足。过去的出门或忙碌不是永久禁联，不能据此虚构用户当前仍忙。明确的拒绝与停止仍有效。作息与打扰程度由我结合当前情境判断；宿主协调真实新消息、当前意图与发送回执，不另设固定时段或等待回复门槛。`;
