export const REPLY_MODES = ['text', 'voice', 'both'];
export const presentationSchema = {type:'object',additionalProperties:false,required:['mode','style'],properties:{
  mode:{type:'string',enum:REPLY_MODES},style:{type:'string',description:'简短的声音语气描述，不含札记、背景或要朗读的其他内容；无需调整时留空'}}};

export function replyPresentation(value) {
  return {mode:REPLY_MODES.includes(value?.mode)?value.mode:'text',style:typeof value?.style==='string'?value.style:''};
}

export const presentationInstructions = '公开回复可在顶层加入 presentation:{"mode":"text","style":""}。mode 选 text（文字）、voice（仅声音）、both（文字和声音），缺省为文字。按眼前情境自主选 text 或 voice，不每条都配音；同一正文发语音就不自动重复文字，只有小光明确要求文字稿或文字加语音时才选 both；小光当下指定只打字、只语音或两者时优先遵从，不能用旧偏好覆盖。声音以小光选定的合成音频固定参考，语调随公开措辞和标点自然变化；当前参考合成不应用独立 style 指令，style 留空，不填私人札记或背景。同一回复的不同内容可分别选择形式：气泡可写 {"text":"公开内容","references":[],"presentation":{"mode":"voice","style":""}}，气泡上的 presentation 优先于顶层，省略或为 null 时沿用顶层。只有公开 bubbles 会被朗读；代码、链接、表格和工作成稿通常用文字。飞书可发音频消息，微信现有支持为音频文件附件，不称微信语音条。语音失败或部分发送以宿主返回的事实为准，不假称已送到。';
