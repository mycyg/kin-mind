import { replyEnvelope } from './reply-fixture.mjs'
// Dev-only, mounted by dev/e2e-memory.mjs: her lanes played by a script, a turn in her desktop chat, and a
// conversation of hers read back as the model saw it (what was said, her recall, her tools' answers).
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import { Scripted, keepLog, say, textOf } from './scenario.mjs'

class Memory extends Scripted {
  /** What her reviews were given: the system prompt (her persona, who she knows) and the request. */
  reviews = []

  async *stream(options) {
    if (options.model !== 'chat') {
      if ((options.tools ?? []).some((tool) => tool.name === 'submit_appraisal')) {
        this.reviews.push(JSON.stringify({ system: options.system, messages: options.messages }))
      }
      return yield* super.stream(options)
    }
    const messages = (options.messages ?? []).filter(m => !m.source?.noteIds)
    // Her own minute's draft: not now.
    if (options.system?.includes('最后只输出一个 JSON 对象')) return yield* reply('{"action":"wait","condition":"review","reason":"先不打扰"}')
    if (messages.at(-1)?.role === 'tool') return yield* reply(replyEnvelope('嗯，我想起来了。'))
    const words = textOf(messages.findLast((m) => m.role === 'user' && !/^(（以下是你此刻|这条消息的来源编号)/.test(textOf(m))) ?? { content: [] })
    if (words.includes('还记得')) return yield* call('kin_recall', { about: '出差 杭州 回中国 坐飞机' })
    return yield* reply(replyEnvelope('好呀～'))
  }
}

function* reply(text) {
  yield { type: 'block-start', index: 0, blockType: 'text' }
  yield { type: 'text-delta', index: 0, text }
  yield { type: 'block-end', index: 0, block: { type: 'text', text } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'stop' } }
}

let calls = 0
function* call(name, input) {
  const id = ToolCallId('memory_' + ++calls)
  const args = JSON.stringify(input)
  yield { type: 'block-start', index: 0, blockType: 'tool-call' }
  yield { type: 'tool-call-delta', index: 0, id, name, argumentsDelta: args }
  yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name, arguments: args } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'tool-calls' } }
}

export const name = 'kin-e2e-memory'
export const inject = ['kin', 'llm', 'sessionController', 'sessions', 'connection']

export function apply(ctx, config) {
  keepLog(ctx, config.log)
  const model = new Memory()
  ctx.llm.registerAdapter(['kin-test'], model)
  const route = (path, handle) => ctx.effect(() => ctx.connection.fetch.register({
    path, methods: ['POST'], requestBody: 'buffered',
    async fetch(request) {
      try {
        return Response.json(await handle(await request.json()))
      } catch (error) {
        return Response.json({ error: error.stack ?? String(error) }, { status: 500 })
      }
    },
  }), 'kin e2e memory ' + path)
  route('/api/kin-memory/say', ({ session, text }) => say(ctx, session, text))
  route('/api/kin-memory/session', async ({ session }) => {
    const { agent } = await ctx.sessionController.resolveAgent(session)
    const events = []
    for (let seq = 0; seq < Number(agent.session.seq); seq++) {
      const event = agent.session.eventAt(SessionSeq(seq))
      if (event) events.push(JSON.stringify(event.data, (_key, value) => (typeof value === 'bigint' ? String(value) : value)))
    }
    return events
  })
  route('/api/kin-memory/reviews', async () => model.reviews)
}
