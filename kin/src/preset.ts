/**
 * Mounted inside the KIN agent preset, so every listener here sees KIN's sessions only:
 * agent events are scope-filtered by agent. Ordinary coding sessions never reach this file.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type {} from '@deepseek-ai/dsh-compaction'
import { createUserMessage, type UserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from './index.js'
import { OWNER } from './contact/people.js'
import * as contact from './contact/preset.js'
import { PROVIDER } from './models.js'
import { RECALL, renderContext, stamp } from './render.js'
import * as steward from './steward/preset.js'
import * as talk from './talk/preset.js'
import { spoken, textOf } from './turn.js'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    /**
     * What was said on WeChat / Feishu, relayed into KIN's session for that conversation. Core took each
     * message in as it arrived (contact/conversations.ts): `inputs` are their ids there, `source` the last one's source.
     */
    'kin-channel': {
      readonly kind: 'kin-channel'; readonly channel: string; readonly speaker: string
      readonly inputs: readonly string[]; readonly source: string
    }
    'kin-context': {
      readonly kind: 'kin-context'
      readonly form: 'snapshot'
      readonly sections: readonly { readonly name: string; readonly text: string }[]
    }
  }
}

export const name = 'kin-preset'
export const inject = ['kin', 'agents', 'systemPrompt', 'tools']

export function apply(ctx: Context) {
  const { kin } = ctx
  const warn = (what: string) => (error: Error) => ctx.logger('kin').warn('%s: %s', what, error.message)

  // Her conversations on other platforms and the relay tool (contact/preset.ts).
  ctx.plugin(contact)

  // The persona row renders `{{kin_persona}}`; the text lives in the data directory and can change live.
  ctx.effect(() => ctx.systemPrompt.variable('kin_persona', () => kin.persona()), 'kin persona variable')

  // The feeling line each conversation last showed her, so an unchanged mood is not repeated every turn.
  const feelings = new Map<string, string>()

  // Before the first step of every turn: what KIN feels and remembers about what was just said.
  // Every block stays in the conversation, so each turn adds only what is new: Core leaves out what it
  // already gave this session, and a fixed budget keeps one turn's share small.
  ctx.on('agent/pre-step', async ({ agent, step, signal }, next) => {
    const decision = await next()
    if (decision.kind === 'reject' || signal.aborted || step !== 1) return decision
    const query = decision.messages.map(textOf).join('\n').trim()
    const session = agent.session.id
    // Recall in this conversation's own view: what is the owner's alone (her intimacy) is shown to the owner only.
    const { channel, ...scope } = kin.scopeOf(session)
    const recalled = await kin.mind
      .call('memory-context', { query, purpose: 'chat', mode: 'light', session, ...scope })
      .then((answer) => {
        const { text, feeling } = renderContext(answer, kin.dimensions(), kin.nameOf, { shown: feelings.get(session) })
        if (feeling) feelings.set(session, feeling)
        return text
      })
      .catch((error) => (warn('recall skipped')(error), ''))
    // What was just said joins her memory now: after her recall, so it is not recalled back to her, and
    // before she answers, so what she does with it this turn (a plan made of it, a reply held back) can name it.
    const source = await hear(session, decision.messages).catch((error) => (warn('not recorded yet')(error), ''))
    const here = `（当前对话：${kin.nameOf(scope.recipient_person_id)}（${channel === 'app' ? '桌面' : channel}，${scope.recipient_person_id.startsWith('group:') ? '群聊' : '私聊'}））`
    const text = [here, recalled, source && `这条消息的来源编号：${source}`].filter(Boolean).join('\n')
    if (!text) return decision
    return {
      ...decision,
      messages: [...decision.messages, createUserMessage({
        content: [{ type: 'text', text }],
        source: { kind: 'kin-context', form: 'snapshot', sections: [{ name: 'kin-context', text }] },
      })],
    }
  }, { prepend: true })

  // When a turn settles, what was said joins KIN's memory, in log order.
  // Her background work waits while she is replying, and looks again once she has.
  ctx.on('agent/status', ({ agent, status }) => {
    // DSH fixes a step's model before agent/request; select her lane when the turn starts.
    if (status === 'running') agent.model.select({ provider: PROVIDER, model: 'chat' })
    kin.replying(agent.session.id, status === 'running')
    if (status === 'idle') void record(agent).then(() => kin.nudge(), warn('recording failed'))
  })

  // Messages heard as their turn started (by message id, which is how Core knows them), not to be taken in twice.
  const taken = new Set<string>()

  /** The owner's words join her memory; what came from a channel already did as it arrived. */
  async function hear(session: string, messages: UserMessage[]) {
    let source = ''
    for (const message of messages) {
      if (message.source.kind === 'kin-channel') {
        source = message.source.source
        continue
      }
      const text = spoken(message) ? textOf(message) : ''
      if (!text) continue
      const at = new Date().toISOString()
      ;({ source_id: source } = await kin.mind.call('ingest', {
        id: message.id, text, at, session, speaker_person_id: OWNER, ...kin.scopeOf(session),
      }))
      taken.add(message.id)
    }
    return source
  }

  async function record(agent: Agent) {
    const session = agent.session
    const end = Number(session.seq)
    const scope = kin.scopeOf(session.id)
    for (let seq = kin.recorded(session.id); seq < end; seq++) {
      const event = session.eventAt(SessionSeq(seq))
      const at = event && new Date(event.time).toISOString()
      const id = `${session.id}:${seq}`
      if (event?.type === 'user/message' && event.data.source.kind === 'user') {
        const text = textOf(event.data)
        if (text && !taken.delete(event.data.id)) {
          await kin.mind.call('ingest', { id: event.data.id, text, at, session: session.id, speaker_person_id: OWNER, ...scope })
        }
      } else if (event?.type === 'assistant/message') {
        const text = textOf(event.data.message)
        if (text) await kin.mind.call('observe', { id, text, at, ...scope })
      } else if (event?.type === 'compaction/end' && !event.data.error) {
        // DSH summarized the older turns, the recall blocks in them included: Core may offer those
        // memories again, and her mood is worth saying once more.
        await kin.mind.call('memory-compact-ack', {
          session: session.id, actual_session: session.id, epoch: event.data.compactionId, completed: true,
        })
        feelings.delete(session.id)
      }
      kin.markRecorded(session.id, seq + 1)
    }
  }

  // Handing the owner's work to DSH workers and keeping track of it (steward/).
  ctx.plugin(steward)

  ctx.tools.register(defineTool({
    name: 'kin_recall',
    description: '回想和某件事、某个人、某段时间有关的记忆，连同你当时心里记下的；每条带着是谁说的、跟谁说的、哪天说的，和它的编号。'
      + '当前对话里已经给过你的不用再查；对方问起很久以前的事、或你想确认细节时再用。'
      + '有人说你记错了，或你自己发现记错了：用它找到那条，再用 correct_memory 把它改对。',
    parameters: {
      about: { type: 'string', required: true, description: '想回想的内容，用自然语言描述' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      // Her memories, and the notes she kept to herself about it (Core's private notes), as this conversation may see them.
      const session = exec.agent?.session.id ?? ''
      const { channel: _channel, ...scope } = kin.scopeOf(session)
      const [answer, { notes }] = await Promise.all([
        kin.mind.call('memory-context', { query: args.about, ...RECALL, session, ...scope }),
        kin.mind.call<{ notes: { at: string; text: string }[] }>('private-notes', { query: args.about, limit: 3, ...scope }),
      ])
      const mine = notes.map((note) => `- ${stamp(note.at)}：${note.text}`)
      return [renderContext(answer, kin.dimensions(), kin.nameOf, { ids: true }).text, mine.length ? '你当时心里记下的（只有你自己知道）：\n' + mine.join('\n') : '']
        .filter(Boolean).join('\n') || '没想起什么相关的。'
    },
  }))

  // Her own tools in conversation: plans, holding a reply back, notes to herself, a change to who she is.
  ctx.plugin(talk)
}
