// Dev-only, mounted by dev/e2e-world.mjs in both of its DSH runs: her chat and task lanes played by a script, and two
// dev routes for the harness — a turn in her desktop chat (as the page's chat box sends one) and a conversation of hers
// read back in full (everything the model saw there: what was said, her recall, her tools' answers).
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import { Scripted, keepLog, say, textOf } from './scenario.mjs'

export const name = 'kin-e2e-world'
export const inject = ['kin', 'llm', 'sessionController', 'sessions', 'connection']

/** Each review leaves her a little flustered (色色值 up) and wanting to reach out. */
const APPRAISAL = {
  reason: '想起了刚才的聊天。', values: { mood: 62, flirtation: 85 },
  wishes: [{ kind: 'contact', topic: '想找对方说说话', content: '想问问对方今天过得怎么样', strength: 80, ttl_hours: 24, completion: '对方回了我' }],
}
/** Her recall block and the source line the host adds after the words of a turn. */
const CONTEXT = /^(（以下是你此刻的内在状态|这条消息的来源编号)/

class World extends Scripted {
  /** What her reviews and her drafts were given, for the harness. */
  seen = { appraisals: [], drafts: [] }

  async *stream(options) {
    const tools = (options.tools ?? []).map((tool) => tool.name)
    if (options.model === 'chat') return yield* this.chat(options)
    const all = JSON.stringify(options.messages)
    if (tools.includes('submit_appraisal')) {
      this.seen.appraisals.push(all)
      return yield* call('submit_appraisal', APPRAISAL)
    }
    // The owner's workers: one sorts the notes; the other is still at the bills when DSH stops.
    if (tools.includes('write') && all.includes('核对账单')) return yield* hang(options.signal)
    if (tools.includes('write') && all.includes('整理')) {
      return yield* (all.includes('"name":"write"') ? reply('整理好了：清单写在 整理结果.md。')
        : call('write', { file_path: '整理结果.md', content: '- 周一的笔记\n- 周三的笔记\n' }))
    }
    yield* super.stream(options)
  }

  async *chat(options) {
    const messages = options.messages ?? []
    if (options.system?.includes('最后只输出一个 JSON 对象')) {
      const facts = textOf(messages.at(-1))
      this.seen.drafts.push(facts)
      return yield* reply(JSON.stringify(facts.includes('对方是小光')
        ? { action: 'send', bubbles: ['想你啦，今天过得怎么样？'] } : { action: 'wait', reason: '先不打扰' }))
    }
    // A tool answered: say something about it, from all she has seen this turn (her recall and the tool's answer).
    if (messages.at(-1)?.role === 'tool') {
      const asked = JSON.stringify(messages.at(-2))
      const turn = JSON.stringify(messages.slice(messages.findLastIndex((m, i) => m.role === 'assistant' && i < messages.length - 2) + 1))
      if (asked.includes('kin_note')) return yield* reply('放心，我替你保密。')
      if (asked.includes('kin_delegate')) return yield* reply('交给帮手啦，做好了告诉你。')
      if (asked.includes('kin_tell')) return yield* reply('好，我跟小红说了。')
      if (asked.includes('悄悄话')) return yield* reply('小光挺好的呀～')
      return yield* reply(turn.includes('杭州') ? '你说过下周三要去杭州出差呀，别怕，我陪着你。' : '这个我想不起来了。')
    }
    // The words of this turn, without the recall the host adds after them.
    const words = messages.slice(messages.findLastIndex((m) => m.role === 'assistant') + 1)
      .filter((m) => m.role === 'user' && m.source?.kind !== 'kin-context').map(textOf).filter((text) => !CONTEXT.test(text)).join('\n')
    if (words.includes('宿主事实')) {
      return yield* reply(words.includes('做完了') ? '整理好啦，清单写在你那个文件夹的 整理结果.md 里了。'
        : words.includes('重启') ? '刚才 DSH 重启，核对账单的活断在半路了，要我让它接着做吗？' : '嗯。')
    }
    if (words.includes('去哪儿出差')) return yield* call('kin_recall', { about: '出差 坐飞机' })
    if (words.includes('怕坐飞机')) return yield* call('kin_note', { text: '主人偷偷说怕坐飞机，我要替主人保密' })
    if (words.includes('悄悄话')) return yield* call('kin_recall', { about: '小光 坐飞机 悄悄话' })
    if (words.includes('整理')) {
      return yield* call('kin_delegate', { title: '整理笔记', folder: this.folder, task: '把这个文件夹里的笔记整理成一份清单，写进 整理结果.md。' })
    }
    if (words.includes('核对账单')) {
      return yield* call('kin_delegate', { title: '核对账单', folder: this.folder, task: '核对账单：把这个文件夹里的账单逐笔核对一遍，有对不上的列出来。' })
    }
    const relay = /转告小红：(.+)/.exec(words)
    if (relay) return yield* call('kin_tell', { to: '小红', text: '小光说' + relay[1] })
    if (words.includes('你说呢')) return yield* reply('吃火锅吧！')
    if (words.includes('去忙')) return yield* reply('嗯嗯，去吧～')
    return yield* reply('好呀，路上小心～')
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
  const id = ToolCallId('world_' + ++calls)
  const args = JSON.stringify(input)
  yield { type: 'block-start', index: 0, blockType: 'tool-call' }
  yield { type: 'tool-call-delta', index: 0, id, name, argumentsDelta: args }
  yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name, arguments: args } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'tool-calls' } }
}

/** A worker that has started answering and is still at it until its turn is stopped. */
async function* hang(signal) {
  yield { type: 'block-start', index: 0, blockType: 'text' }
  yield { type: 'text-delta', index: 0, text: '我一笔一笔看……' }
  await new Promise((resolve) => (signal?.aborted ? resolve() : signal?.addEventListener('abort', resolve, { once: true })))
}

export function apply(ctx, config) {
  keepLog(ctx, config.log)
  const world = Object.assign(new World(), { folder: config.folder })
  ctx.llm.registerAdapter(['kin-test'], world)
  const route = (path, handle) => ctx.effect(() => ctx.connection.fetch.register({
    path, methods: ['POST'], requestBody: 'buffered',
    async fetch(request) {
      try {
        return Response.json(await handle(await request.json()))
      } catch (error) {
        return Response.json({ error: error.stack ?? String(error) }, { status: 500 })
      }
    },
  }), 'kin e2e world ' + path)
  route('/api/kin-world/say', ({ session, text }) => say(ctx, session, text))
  route('/api/kin-world/session', async ({ session }) => {
    const { agent } = await ctx.sessionController.resolveAgent(session)
    const events = []
    for (let seq = 0; seq < Number(agent.session.seq); seq++) {
      const event = agent.session.eventAt(SessionSeq(seq))
      if (event) events.push({ type: event.type, source: event.data?.source?.kind, text: event.type === 'user/message' ? textOf(event.data)
        : event.type === 'assistant/message' ? textOf(event.data.message) : '', json: JSON.stringify(event.data, (_key, value) => (typeof value === 'bigint' ? String(value) : value)) })
    }
    return events
  })
  route('/api/kin-world/model', async () => world.seen)
}
