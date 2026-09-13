import {chatVoice} from './chat-bubbles.mjs';
/** Public output only. A parse failure is an execution error, not a wish decision. */
export function parseContactDraft(outputs) {
  const last=outputs.filter(raw=>typeof raw==='string'&&raw.trim()).at(-1);
  for (const raw of last?[last]:[]) {
    let result;
    try { result=JSON.parse(raw.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'')); }
    catch { continue; }
    if(!result||typeof result!=='object'||Array.isArray(result))continue;
    if(result.action==='send'&&Array.isArray(result.bubbles)) {
      if(result.bubbles.length&&result.bubbles.every(x=>typeof x==='string'&&x.trim())&&result.bubbles.join('\n\n').length<=3000)
        return {action:'send',text:result.bubbles.map(x=>x.trim()).join('\n\n'),bubbles:result.bubbles.map(x=>x.trim())};
      continue;
    }
    // Older hosts can finish an already-started draft during a rolling upgrade.
    if(result.text===null&&!result.action)return {action:'wait',condition:'new_evidence',reason:'Legacy empty draft; a new related source is required'};
    if((result.action==='send'||!result.action)&&typeof result.text==='string'&&result.text.trim()&&result.text.length<=3000)
      return {action:'send',text:result.text.trim()};
    if(!['wait','abandon'].includes(result.action)||typeof result.reason!=='string'||!result.reason.trim()||result.reason.length>1200)continue;
    if(result.action==='abandon')return {action:'abandon',reason:result.reason.trim()};
    if(!['time','new_evidence','owner_reply'].includes(result.condition))continue;
    const seconds=result.retry_after_seconds??1800;
    if(result.condition==='time'&&(!Number.isSafeInteger(seconds)||seconds<300||seconds>21600))continue;
    return {action:'wait',reason:result.reason.trim(),condition:result.condition,...(result.condition==='time'?{retry_after_seconds:seconds}:{})};
  }
  throw new Error('contact-draft-invalid-result');
}

export const contactDraftInstructions = `${chatVoice}\n内部主动联系草稿事件，不是用户的新消息，不伪造用户回复。
当前愿望已由DeepSeek评估为想说的话，你负责接着当前语境把它说出来。亲昵接话、具体玩笑、胡思乱想、想撒娇或闲扯都可以成为内容，聊天不必追求意义，也不需要用户先问或给分享时机。只有出现新的明确不适合条件、重复或失效，才推迟或放弃；不再重做价值筛选。猜想和想象按其身份表达，不编造经历。
未回复等待是用户可修改的联系偏好，以当前 contact.wait_for_reply 为准。为 false 时，我可以继续分享，也可以在久未回应、想她的时候撒娇式呼唤，不必另编新话题。具体时长以interaction_timing为准，时机结合当前情绪判断。只有某项具体事情必须等一个答案才能推进时，才选择owner_reply；撒娇呼唤本身可以发出。
只给出结构化草稿，不调用发送、提醒或文件工具。返回以下一种 JSON，不输出推理过程：
{"action":"send","bubbles":["..."]}
{"action":"abandon","reason":"内容已经讲过或愿望失效的简短依据"}
{"action":"wait","condition":"time","retry_after_seconds":1800,"reason":"暂不适合、稍后复核的具体原因"}
{"action":"wait","condition":"owner_reply","reason":"需要等待用户的回应"}
{"action":"wait","condition":"new_evidence","reason":"需要什么新资料才能形成内容"}
time 表示已有内容的临时推迟，复核间隔为 300—21600 秒；new_evidence 表示内容不足。过去的出门或忙碌不是永久禁联，不能据此虚构用户当前仍忙。拒绝与停止仍有效，未回复等待采用当前偏好。宿主负责复核安静时段、真实新消息、愿望有效性和发送回执。`;
