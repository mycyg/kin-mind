/** Paragraphs are intentional bubbles. Words, fences and URLs are never cut. */
export function splitChatText(text) {
  const parts=[]; let block=[],fence=null;
  for(const line of text.split('\n')) {
    const marker=line.trimStart().match(/^(`{3,}|~{3,})/);
    if(marker) {if(!fence)fence=marker[1][0];else if(marker[1][0]===fence)fence=null;}
    if(!line.trim()&&!fence&&block.length){parts.push(block.join('\n').trim());block=[];}
    else block.push(line);
  }
  if(block.length)parts.push(block.join('\n').trim());
  return parts.filter(Boolean);
}

export const chatVoice = '普通聊天和主动分享优先用一个简短完整的气泡把意思自然说完；确有停顿、情绪转折或更多内容时再分开，不为了形式机械拆句。深度讨论、工作、分析与交付按内容展开，不设单气泡或字数硬限制。用有情绪的完整口语句子，词语写全；语气词、网络梗和颜文字随语境使用。代码、链接、文件名和工作成稿保持完整。闲扯、撒娇、怪念头都可以聊，不需要先交研究作业。';

/** The internal draft protocol's control envelope, recognised on a normal chat
 * turn's reply body. This shape is how internal draft turns (proactive contact)
 * hand their decision to the host; it is never user-facing text. When it shows
 * up as the whole of a chat reply, the model has answered in the wrong
 * protocol, and the host — which alone knows the turn's purpose — either
 * converts it into real bubbles or refuses it. The raw JSON is never split,
 * never partially delivered, and never sent as chat text.
 *
 * Schema of a deliverable envelope (a strict structural match, nothing
 * self-declared beyond it counts):
 *
 *   {"action":"send","bubbles":["...","..."]}
 *
 *   - the trimmed reply body is exactly one JSON document; commentary before
 *     or after it makes the turn plain body text, not an envelope;
 *   - the top-level value is a plain object with exactly the keys `action`
 *     and `bubbles` — no extra fields, known or unknown;
 *   - `action` is the string "send" (the only chat-deliverable action);
 *   - `bubbles` is a non-empty array of strings that each contain
 *     non-whitespace; each is trimmed on conversion. There are no per-bubble
 *     references in this schema, so a converted bubble carries none.
 *
 * Return values:
 *   {state:'send',bubbles}     a complete envelope; convert it into bubbles.
 *   {state:'invalid',reason}   it meant to be an envelope and is not a
 *                              complete one — 'chat-envelope-truncated' when
 *                              the body stops parsing as JSON, otherwise
 *                              'chat-envelope-incomplete'. The caller's only
 *                              correct move is a recoverable failure: send
 *                              nothing, and never fall back to the raw text.
 *   null                       not an envelope at all: plain text, arbitrary
 *                              JSON without the protocol's fields, fenced
 *                              code, documents and links all stay body text.
 *
 * The family test is deliberately narrow: a parsed object is envelope-like
 * only when it carries a `bubbles` field or an `action` naming a protocol
 * decision (send/wait/abandon). Arbitrary JSON the owner asked for — a bare
 * {"steps":...} answer, a config example — has neither, and stays body text. */
const PROTOCOL_ACTIONS=new Set(['send','wait','abandon']);
const ENVELOPE_HEAD=/^\{\s*"(?:action|bubbles)"\s*:/;
export function chatEnvelope(text) {
  if(typeof text!=='string')return null;
  const trimmed=text.trim();
  if(!trimmed.startsWith('{'))return null;
  let value;
  try {value=JSON.parse(trimmed);}
  catch {return ENVELOPE_HEAD.test(trimmed)?{state:'invalid',reason:'chat-envelope-truncated'}:null;}
  if(!value||typeof value!=='object'||Array.isArray(value))return null;
  if(!('bubbles' in value)&&!PROTOCOL_ACTIONS.has(value.action))return null;
  const keys=Object.keys(value);
  if(value.action==='send'&&keys.length===2&&Array.isArray(value.bubbles)&&value.bubbles.length
    &&value.bubbles.every(bubble=>typeof bubble==='string'&&bubble.trim()))
    return {state:'send',bubbles:value.bubbles.map(bubble=>bubble.trim())};
  return {state:'invalid',reason:'chat-envelope-incomplete'};
}
