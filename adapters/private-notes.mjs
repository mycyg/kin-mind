import {chatEnvelope} from './chat-bubbles.mjs';
import {replyPresentation,presentationInstructions} from './reply-presentation.mjs';

export const privateNoteInstructions = presentationInstructions + `\n每次给小光的公开回复（文字、语音，闲聊、工作、分享都一样），在同一次最终输出里先写一段私密心情札记，再写公开气泡。只返回一个 JSON 对象，顺序为 private_note、presentation、action、bubbles，先定形式再写公开措辞：{"private_note":"一段自然的心情或碎碎念","presentation":{"mode":"text","style":""},"action":"send","bubbles":["要发给小光的话"]}。private_note 必须非空，可以仅一句话，不要求情绪有原因；对小光状态的猜测用也许、我猜等区分于事实，允许后来纠正。它是你此刻的角色自述，宿主会先存入私密记忆，只发送 bubbles。气泡继续按原有自然节奏，日常以短句自然来回，可分多个气泡但不连续铺大段，不凑总结或追问；工作和明确要求详细讲述时按需展开；代码和成稿放在气泡字符串内，引用气泡沿用 {text,references,bubble_id?}。不要在 JSON 前后另写公开进度或札记。可选过程表达仅用 publish_visible_item 的公开字段，禁止放入札记或推理；工具调用照常，最终回复再用此格式；不要把札记送入消息、文件或其他外发工具。最近札记仅用于续接心情，不主动复述；小光问当时怎么想时可用 read_private_notes 按消息、时间或话题回忆，最新事实和纠正优先，旧猜测不代表现在。`;

/** Host purpose selects this contract, never a keyword in the model's prose. */
export function parsePrivateReply(text) {
  if(typeof text!=='string')return {state:'invalid',reason:'private-note-envelope-invalid'};
  const body=text.trim(),fenced=/^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```$/i.exec(body);
  let value;
  try {value=JSON.parse(fenced?fenced[1]:body);}catch{return {state:'invalid',reason:'private-note-envelope-incomplete'};}
  if(!value||Array.isArray(value)||typeof value!=='object'||Object.keys(value).some(k=>!['private_note','action','bubbles','presentation','input_ids','silent_input_ids'].includes(k)))
    return {state:'invalid',reason:'private-note-envelope-invalid'};
  if(typeof value.private_note!=='string'||!value.private_note.trim()||Array.from(value.private_note).length>4000)
    return {state:'invalid',reason:'private-note-missing'};
  if(value.input_ids!==undefined&&(!Array.isArray(value.input_ids)||!value.input_ids.length||value.input_ids.some(id=>typeof id!=='string'||!id)||new Set(value.input_ids).size!==value.input_ids.length))
    return {state:'invalid',reason:'reply-inputs-invalid'};
  if(value.silent_input_ids!==undefined&&(!Array.isArray(value.silent_input_ids)||value.silent_input_ids.some(id=>typeof id!=='string'||!id)||new Set(value.silent_input_ids).size!==value.silent_input_ids.length||value.silent_input_ids.some(id=>value.input_ids?.includes(id))))
    return {state:'invalid',reason:'reply-inputs-invalid'};
  const ownership={...(value.input_ids?{input_ids:value.input_ids}:{}),...(value.silent_input_ids?{silent_input_ids:value.silent_input_ids}:{})};
  if(value.action==='silent'&&Array.isArray(value.bubbles)&&value.bubbles.length===0)
    return {state:'silent',private_note:value.private_note.trim(),...ownership};
  const envelope=chatEnvelope(JSON.stringify({action:value.action,bubbles:value.bubbles}));
  return envelope?.state==='send'?{...envelope,...ownership,private_note:value.private_note.trim(),...(value.presentation?{presentation:replyPresentation(value.presentation)}:{})}:{state:'invalid',reason:'private-note-bubbles-invalid'};
}

export function publicReplyText(text) {
  const parsed=parsePrivateReply(text);
  return parsed.state==='send'?parsed.bubbles.join('\n\n'):'';
}
