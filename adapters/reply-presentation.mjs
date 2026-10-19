export const REPLY_MODES = ['text', 'voice', 'both'];
export const presentationSchema = {type:'object',additionalProperties:false,required:['mode','style'],properties:{
  mode:{type:'string',enum:REPLY_MODES},style:{type:'string',description:'简短的声音语气描述，不含札记、背景或要朗读的其他内容；无需调整时留空'}}};

export function replyPresentation(value) {
  return {mode:REPLY_MODES.includes(value?.mode)?value.mode:'text',style:typeof value?.style==='string'?value.style:''};
}

export const presentationInstructions = '公开回复可在顶层加入 presentation:{"mode":"text","style":""}。mode 选 text（文字）、voice（仅声音）、both（文字和声音），缺省为文字。按眼前情境自主选择，不每条都配音；小光当下指定只打字、只语音或两者时优先遵从，不能用旧偏好覆盖。声音固定同一声线，可用 style 简短描述自然、轻快、温柔或低落等语气，不填私人札记或背景。只有公开 bubbles 会被朗读；代码、链接、表格和工作成稿通常用文字。飞书可发音频消息，微信现有支持为音频文件附件，不称微信语音条。语音失败或部分发送以宿主返回的事实为准，不假称已送到。';
