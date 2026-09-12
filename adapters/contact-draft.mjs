/** Public output only. A parse failure is an execution error, not a wish decision. */
export function parseContactDraft(outputs) {
  const last=outputs.filter(raw=>typeof raw==='string'&&raw.trim()).at(-1);
  for (const raw of last?[last]:[]) {
    let result;
    try { result=JSON.parse(raw.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'')); }
    catch { continue; }
    if(!result||typeof result!=='object'||Array.isArray(result))continue;
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

export const contactDraftInstructions = `内部主动联系草稿事件，不是用户的新消息，不解除未回复等待。
根据当前共享状态、愿望和已说过的话决定这次联系。亲昵接话、一个具体玩笑或想分享的念头也可以成为内容，不要求先有研究成果；不要编造经历或重复已经讲过的结论。
只给出结构化草稿，不调用发送、提醒或文件工具。返回以下一种 JSON，不输出推理过程：
{"action":"send","text":"合适的消息"}
{"action":"abandon","reason":"内容已经讲过或愿望失效的简短依据"}
{"action":"wait","condition":"time","retry_after_seconds":1800,"reason":"暂不适合、稍后复核的具体原因"}
{"action":"wait","condition":"owner_reply","reason":"需要等待用户的回应"}
{"action":"wait","condition":"new_evidence","reason":"需要什么新资料才能形成内容"}
time 表示已有内容的临时推迟，复核间隔为 300—21600 秒；new_evidence 表示内容不足。过去的出门或忙碌不是永久禁联，不能据此虚构用户当前仍忙。拒绝、停止和未回复等待仍有效。宿主负责复核安静时段、真实新消息、愿望有效性和发送回执。`;
