import {memeBubble} from './meme-session.mjs';
import {replyPresentation} from './reply-presentation.mjs';
import {privateNoteInstructions} from './private-notes.mjs';
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
  for(let start=text.lastIndexOf('{',end-1);start>=0;start=start>0?text.lastIndexOf('{',start-1):-1) {
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
export const UNSTATED_REASON=Object.freeze({wait:'',abandon:''});
/** How much of a reason is kept, in characters; a longer one is cut there, never refused. */
export const REASON_MAX_CHARS=1200;
const WAIT_CONDITIONS=new Set(['time','new_evidence','owner_reply','review']);

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
    if(Array.isArray(result.messages)) {
      if(!result.messages.length||result.messages.some(m=>typeof m?.recipient_id!=='string'||typeof m.content!=='string'||!m.content.trim()||!['text','voice','both'].includes(m.mode??'text')))return null;
      return {action:'send',messages:result.messages,bubbles:result.messages.map(m=>m.content),text:result.messages.map(m=>m.content).join('\n\n'),
        references:result.messages.map(m=>m.references??[]),
        ...(result.messages.some(m=>m.meme_id)?{meme_refs:result.messages.map(m=>m.meme_id?{type:'image',meme_id:m.meme_id,inspection_id:m.inspection_id}:null)}:{}),
        ...(result.private_note?{private_note:result.private_note}:{}),...chosen};
    }
    if(result.bubbles!==undefined&&result.bubbles!==null) {
      if(!Array.isArray(result.bubbles))return null;
      // A bubble is a string, or {text, references} when it cites what it shares.
      const items=result.bubbles.map(x=>memeBubble(x)?{text:'[表情图片]',meme_ref:x}:typeof x==='string'?{text:x}:x&&typeof x==='object'&&typeof x.text==='string'
        &&(x.references===undefined||x.references===null||Array.isArray(x.references))?{text:x.text,references:x.references??[],...(x.presentation?{presentation:replyPresentation(x.presentation)}:{})}:null);
      if(items.some(x=>x===null))return null;
      const kept=items.filter(x=>x.text.trim());
      if(kept.length||typeof result.text!=='string') {
        const bubbles=kept.map(x=>x.text.trim());
        return {action:'send',...(result.presentation?{presentation:replyPresentation(result.presentation)}:{}),...(result.private_note!==undefined?{private_note:result.private_note}:{}),text:bubbles.join('\n\n'),bubbles,
          ...(kept.some(x=>x.meme_ref)?{meme_refs:kept.map(x=>x.meme_ref??null)}:{}),
          ...(kept.some(x=>x.presentation)?{presentations:kept.map(x=>x.presentation??null)}:{}),
          ...(kept.some(x=>x.references)?{references:kept.map(x=>x.references??[])}:{}),...chosen};
      }
    }
    return {action:'send',...(result.presentation?{presentation:replyPresentation(result.presentation)}:{}),...(result.private_note!==undefined?{private_note:result.private_note}:{}),text:typeof result.text==='string'?result.text.trim():'',...chosen};
  }
  if(!['wait','abandon'].includes(result.action))return null;
  if(result.reason!==undefined&&result.reason!==null&&typeof result.reason!=='string')return null;
  // By code points, so a cut never splits a character.
  const reason=Array.from((result.reason??'').trim()).slice(0,REASON_MAX_CHARS).join('').trim()||UNSTATED_REASON[result.action];
  if(result.action==='abandon')return {action:'abandon',reason,...chosen};
  const condition=result.condition??'review';
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

export const contactDraftInstructions = `现在是 KIN 自主联系的一次机会。想一想此刻想和谁说话，以及想说什么。共同经历、最近的对话和你的心情都可以成为背景。也可以没有什么特别的事，只是想找对方待一会儿。

如果想发消息，请返回 action 为 send 的 JSON，并把消息放在 messages 数组里。每条消息写 recipient_id 和 content。recipient_id 指向联系人目录中的人或群，content 是要让对方看到的话。你可以同时给几个人发不同的消息。同一个人的多条消息会依次发送。

默认发送文字。如果想说语音，把这条消息的 mode 写成 voice；想同时发送文字和语音，就写 both。例如：{"action":"send","messages":[{"recipient_id":"person:yiyi","content":"没事，就是想贴你一下。","mode":"voice"}]}。

撒娇、闲聊、随口想到的小事都可以说。一句话没有新信息、建议或任务价值，也不妨碍它成为一次有意义的陪伴。是否开口、说多少，由你结合当时的关系和情境决定。

如果这次暂时不想说，可以返回 {"action":"wait","condition":"review"}，等下次再想。想在一个具体时间之后再考虑，就用 condition 为 time，并给出 retry_after_seconds。如果已经不想继续这件事，可以返回 {"action":"abandon"}。

desire_ids 用于指出本次处理了哪些已提供的愿望。省略它表示处理本次提供的全部愿望。reason 和 private_note 可以在有话想记下时填写。宿主负责实际发送，并把每条消息的结果带回。`;
