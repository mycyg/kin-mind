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

/** A decision, or null when the output is not one. No body is ever shortened:
 * content the channel cannot carry in one message is fragmented downstream. */
function decide(result) {
  if(result.action==='send'&&Array.isArray(result.bubbles)) {
    if(result.bubbles.length&&result.bubbles.every(x=>x&&typeof x.text==='string'&&x.text.trim()&&Array.isArray(x.references??[]))) {
      const bubbles=result.bubbles.map(x=>x.text.trim());
      return {action:'send',text:bubbles.join('\n\n'),bubbles,references:result.bubbles.map(x=>x.references??[])};
    }
    if(result.bubbles.length&&result.bubbles.every(x=>typeof x==='string'&&x.trim())) {
      const bubbles=result.bubbles.map(x=>x.trim());
      return {action:'send',text:bubbles.join('\n\n'),bubbles};
    }
    return null;
  }
  // Older hosts can finish an already-started draft during a rolling upgrade.
  if(result.text===null&&!result.action)return {action:'wait',condition:'new_evidence',reason:'Legacy empty draft; a new related source is required'};
  if((result.action==='send'||!result.action)&&typeof result.text==='string'&&result.text.trim())return {action:'send',text:result.text.trim()};
  if(!['wait','abandon'].includes(result.action)||typeof result.reason!=='string'||!result.reason.trim()||result.reason.length>1200)return null;
  if(result.action==='abandon')return {action:'abandon',reason:result.reason.trim()};
  if(!['time','new_evidence','owner_reply'].includes(result.condition))return null;
  const seconds=result.retry_after_seconds??1800;
  if(result.condition==='time'&&(!Number.isSafeInteger(seconds)||seconds<300||seconds>21600))return null;
  return {action:'wait',reason:result.reason.trim(),condition:result.condition,...(result.condition==='time'?{retry_after_seconds:seconds}:{})};
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
当前愿望已由DeepSeek评估为想说的话，你负责接着当前语境把它说出来。亲昵接话、具体玩笑、胡思乱想、想撒娇或闲扯都可以成为内容，聊天不必追求意义，也不需要用户先问或给分享时机。只有出现新的明确不适合条件、重复或失效，才推迟或放弃；不再重做价值筛选。猜想和想象按其身份表达，不编造经历。
未回复等待是用户可修改的联系偏好，以当前 contact.wait_for_reply 为准。为 false 时，我可以继续分享，也可以在久未回应、想她的时候撒娇式呼唤，不必另编新话题。具体时长以interaction_timing为准，时机结合当前情绪判断。只有某项具体事情必须等一个答案才能推进时，才选择owner_reply；撒娇呼唤本身可以发出。
只给出结构化草稿，不调用发送、提醒或文件工具。返回以下一种 JSON，不输出推理过程：
{"action":"send","bubbles":["..."]}
{"action":"abandon","reason":"内容已经讲过或愿望失效的简短依据"}
{"action":"wait","condition":"time","retry_after_seconds":1800,"reason":"暂不适合、稍后复核的具体原因"}
{"action":"wait","condition":"owner_reply","reason":"需要等待用户的回应"}
{"action":"wait","condition":"new_evidence","reason":"需要什么新资料才能形成内容"}
time 表示已有内容的临时推迟，复核间隔为 300—21600 秒；new_evidence 表示内容不足。过去的出门或忙碌不是永久禁联，不能据此虚构用户当前仍忙。拒绝与停止仍有效，未回复等待采用当前偏好。宿主负责复核安静时段、真实新消息、愿望有效性和发送回执。`;
