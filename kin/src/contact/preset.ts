/**
 * The KIN preset's half of her conversations, mounted from preset.ts: what happens in her sessions reaches
 * her conversations (index.ts `kinContact`), she can tell someone else something (a relay, a reminder for them),
 * send a file, a voice message or a sticker into the conversation she is in, and look at her other conversations.
 * Whether and when is hers.
 */
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from './index.js'

export const name = 'kin-contact'
export const inject = ['kinContact', 'tools']

/** Each of these tools answers her in a few words: how it went. */
const answer = { schema: { type: 'string' } as const, render: (_args: unknown, value: string) => [{ type: 'text' as const, text: value }] }

export function apply(ctx: Context) {
  const contact = ctx.kinContact
  ctx.on('agent/status', ({ agent, status }) => contact.status(agent.session.id, status === 'running'))
  ctx.on('session/event', (session, event) => contact.event(session, event))

  ctx.tools.register(defineTool({
    name: 'kin_tell',
    description: '把话发给另一个人或群：你想告诉对方的、替人转告的、到点提醒的，你觉得该说就说。写你自己要发出去的原话；安静时段里会等时段过了再发。',
    parameters: {
      to: { type: 'string', required: true, description: '发给谁：联系人或群的名字' },
      text: { type: 'string', required: true, description: '要发给对方的话' },
    },
    output: answer,
    execute: (args, exec) => contact.tell(exec.agent?.session.id ?? '', exec.callId, args.to, args.text),
  }))

  ctx.tools.register(defineTool({
    name: 'kin_send_file',
    description: '把本机的一个文件或图片发到这段微信 / 飞书对话里。微信上文件和视频只发主人点名要的，图片照发。',
    parameters: {
      path: { type: 'string', required: true, description: '文件的完整路径' },
      asked: { type: 'boolean', description: '对方在这段对话里点名要的就是这个文件' },
    },
    output: answer,
    execute: (args, exec) => contact.send(exec.agent?.session.id ?? '', exec.callId, args.path, args.asked === true),
  }))

  // When a voice message fits, after the old host's reply presentation (core/adapters/reply-presentation.mjs).
  ctx.tools.register(defineTool({
    name: 'kin_voice',
    description: '用你的声音说一句，作为语音发到这段对话里（微信上是音频文件）。想让对方听见语气时用，比如哄人、撒娇、道晚安；'
      + '不必每条都说，代码、链接、长段说明还是打字。说了就不用再打一遍同样的字。',
    parameters: {
      text: { type: 'string', required: true, description: '直接说出口的话：自然口语，情绪放在措辞和标点里，不写表情符号和括号动作' },
      style: { type: 'string', description: '想用的语气，一句话；可不填' },
    },
    output: answer,
    execute: (args, exec) => contact.voice(exec.agent?.session.id ?? '', exec.callId, args.text, args.style),
  }))

  // Stickers by what they mean in the moment, after the old meme library's pragmatics (core/adapters/meme-library.mjs).
  ctx.tools.register(defineTool({
    name: 'kin_sticker',
    description: '从你的表情包里挑一张最合意思的发到这段对话里。同一张图在不同场合意思不同（反讽、装乖、接梗…），'
      + '按此刻想表达的找；不必每条都配，没有合适的就不发。',
    parameters: {
      meaning: { type: 'string', required: true, description: '这张表情此刻要表达的意思或情绪' },
    },
    output: answer,
    execute: (args, exec) => contact.sticker(exec.agent?.session.id ?? '', exec.callId, args.meaning),
  }))

  ctx.tools.register(defineTool({
    name: 'kin_conversations',
    description: '看看你在别处的对话：不填 who 就列出都有哪些（谁、在哪、最近什么时候说过话）；填一个人或群的名字，就看那边最近谁说了什么。'
      + '看到了不等于要转述：说不说、说多少由你判断，别人请你别外传的话替对方守着。',
    parameters: {
      who: { type: 'string', description: '人或群的名字' },
    },
    output: answer,
    execute: (args, exec) => contact.look(exec.agent?.session.id ?? '', args.who),
  }))
}
