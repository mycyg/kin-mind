export const REPLY_MODES = ['text', 'voice', 'both'];
export const presentationSchema = {type:'object',additionalProperties:false,required:['mode','style'],properties:{
  mode:{type:'string',enum:REPLY_MODES},style:{type:'string',description:'当前参考克隆不应用独立语气指令，留空；仅兼容支持指令的其他音色配置，不含札记或其他朗读内容'}}};

/** A voice bubble longer than this goes as text: about a minute of speech, past which the local
 * model's output budget and timeout can cut it short. */
export const VOICE_MAX_CHARS=300;
export const spokenMode=(mode,text)=>mode!=='text'&&String(text??'').length>VOICE_MAX_CHARS?'text':mode;

export function replyPresentation(value) {
  return {mode:REPLY_MODES.includes(value?.mode)?value.mode:'text',style:typeof value?.style==='string'?value.style:''};
}

export const presentationInstructions = `在这一次回复中，先决定每个公开气泡是打字还是说话，再按选定形式组织公开措辞；不先写一版打字内容再决定要不要朗读，不另开规划步骤，也不公开选择过程。顶层 presentation 放在 bubbles 前，mode 选 text（文字）、voice（仅声音）、both（文字和声音），缺省为文字。按眼前情境自主选 text 或 voice，不每条都配音；同一正文发语音就不自动重复文字，只有小光明确要求文字稿或文字加语音时才选 both；小光当下指定只打字、只语音或两者时优先遵从，不能用旧偏好覆盖。
voice 或 both 的正文就是要直接说出口的话：用自然口语和完整意思接话，有情境适合的语气、重音和停顿；可以用“嗯”“哎”“好呀”等语气词，但不强制每句添加，不堆拟声词或夸张拉长音。情绪放在措辞和标点里，不写 emoji、颜文字、括号动作或舞台说明，不用星号动作、表情标签或“翻译：”旁白来表达。不要把打字句子简单删掉符号就当口语化。text 继续自然打字聊天，按语境使用表情，不必照搬语音写法。代码、链接、表格和工作成稿通常用文字；同一回复的不同内容可分别选择形式。
声音以小光选定的合成音频固定参考，语调随公开措辞和标点自然变化；当前参考合成不应用独立 style 指令，style 留空，不填私人札记或背景。气泡可写 {"presentation":{"mode":"voice","style":""},"text":"嗯，好呀。听你这么说，我还挺开心的。","references":[]}，气泡上的 presentation 优先于顶层，省略或为 null 时沿用顶层。语音示例：{"private_note":"这句只留在心里。","presentation":{"mode":"voice","style":""},"action":"send","bubbles":["哎，终于忙完啦。今天先歇一会儿，好不好？"]}。示例只说明形式，不套用示例情绪或台词。只有公开 bubbles 会被朗读。飞书可发音频消息，微信现有支持为音频文件附件，不称微信语音条。语音失败或部分发送以宿主返回的事实为准，不假称已送到。`;
