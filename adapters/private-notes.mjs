import {chatEnvelope} from './chat-bubbles.mjs';
import {replyPresentation,presentationInstructions} from './reply-presentation.mjs';

export const privateNoteInstructions = presentationInstructions + '\n旧式独立草稿使用一个 JSON 对象：{"presentation":{"mode":"text","style":""},"action":"send","bubbles":["公开正文"]}。气泡可为字符串或 {text,presentation?,references?,bubble_id?}；没有引用时省略 references。可选 private_note 只存私密记忆，不发送；没有札记就省略，不必为完成回复编写。不要在 JSON 外另写内容。共享主会话使用它当前指定的消息数组接口。';

/** Host purpose selects this contract, never a keyword in the model's prose. */
export function parsePrivateReply(text) {
  if(typeof text!=='string')return {state:'invalid',reason:'private-note-envelope-invalid'};
  const body=text.trim(),fenced=/^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```$/i.exec(body);
  let value;
  try {value=JSON.parse(fenced?fenced[1]:body);}catch{return {state:'invalid',reason:'private-note-envelope-incomplete'};}
  if(!value||Array.isArray(value)||typeof value!=='object'||Object.keys(value).some(k=>!['private_note','action','bubbles','presentation','input_ids','silent_input_ids'].includes(k)))
    return {state:'invalid',reason:'private-note-envelope-invalid'};
  if(value.private_note!=null&&(typeof value.private_note!=='string'||Array.from(value.private_note).length>4000))
    return {state:'invalid',reason:'private-note-missing'};
  if(value.input_ids!=null&&(!Array.isArray(value.input_ids)||!value.input_ids.length||value.input_ids.some(id=>typeof id!=='string'||!id)||new Set(value.input_ids).size!==value.input_ids.length))
    return {state:'invalid',reason:'reply-inputs-invalid'};
  if(value.silent_input_ids!=null&&(!Array.isArray(value.silent_input_ids)||value.silent_input_ids.some(id=>typeof id!=='string'||!id)||new Set(value.silent_input_ids).size!==value.silent_input_ids.length||value.silent_input_ids.some(id=>value.input_ids?.includes(id))))
    return {state:'invalid',reason:'reply-inputs-invalid'};
  const note=typeof value.private_note==='string'&&value.private_note.trim()?{private_note:value.private_note.trim()}:{};
  const ownership={...(value.input_ids?{input_ids:value.input_ids}:{}),...(value.silent_input_ids?{silent_input_ids:value.silent_input_ids}:{})};
  if(value.action==='silent'&&Array.isArray(value.bubbles)&&value.bubbles.length===0)
    return {state:'silent',...note,...ownership};
  const envelope=chatEnvelope(JSON.stringify({action:value.action,bubbles:value.bubbles}));
  return envelope?.state==='send'?{...envelope,...ownership,...note,...(value.presentation?{presentation:replyPresentation(value.presentation)}:{})}:{state:'invalid',reason:'private-note-bubbles-invalid'};
}

export function publicReplyText(text) {
  const parsed=parsePrivateReply(text);
  return parsed.state==='send'?parsed.bubbles.join('\n\n'):'';
}
