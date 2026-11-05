// Dev-only scenario for what she does in a conversation (src/talk/), mounted by dev/e2e-talk.mjs inside a
// real headless DSH. Her chat lane is a scripted model that uses her tools the way a person's words invite.
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { format } from 'node:util'
import { LlmAdapter, ToolCallId } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import { Scripted, say, settle, textOf, until } from './scenario.mjs'

export const name = 'kin-e2e-talk'
export const inject = ['kin', 'llm', 'sessionController', 'sessions']

/** Her chat lane in this run: each turn's words decide which of her tools she reaches for. */
class Chat extends LlmAdapter {
  requests = []
  providerInfo() { return { id: 'kin-chat-test', name: 'KIN chat test' } }
  async listModels() { return [] }
  async resolveModel(provider, model) {
    return { provider, id: model, name: model, inputModalities: ['text'], context: { contextWindow: 200_000 } }
  }
  async *stream(options) {
    const messages = options.messages
    const last = messages.at(-1)
    // This turn: what the person said, and the notes her plugin gave her (recall, the message's source, a reply come due).
    const since = messages.slice(messages.findLastIndex((m) => m.role === 'assistant') + 1)
    const said = since.filter((m) => m.source?.kind === 'user').map(textOf).join('\n')
    const notes = since.filter((m) => m.source?.kind === 'kin-context').map(textOf).join('\n')
    // The tool whose answer she is reading, if any: the call in her last message.
    const answered = messages.findLast((m) => m.role === 'assistant')?.content.find((block) => block.type === 'tool-call')?.name
    const result = last?.role === 'tool' ? textOf(last) : null
    this.requests.push({ tools: (options.tools ?? []).map((tool) => tool.name), system: messages[0]?.role === 'system' ? textOf(messages[0]) : '',
      said, notes, answered, result })
    // A wish: her state first (its revision and version), then the wish; Core says what a call lacks.
    const turnSource = [...messages].reverse().map((m) => m.source?.kind === 'kin-context' && /src_[0-9a-f]{32}/.exec(textOf(m))?.[0]).find(Boolean)
    const wish = (expires) => call('manage_desire', { request: {
      command_id: 'e2e-wish-cat', agent_version: /"agent_version":\s*"([^"]+)"/.exec(this.state)?.[1], expected_revision: Number(/"revision":\s*(\d+)/.exec(this.state)?.[1]),
      action: 'create', kind: 'explore', topic: '养猫', content: '想弄明白养一只小猫要准备什么', strength: 60,
      completion: '知道养猫要准备什么', evidence_ids: [turnSource], reason: '对方想养猫', ...(expires ? { expires_at: expires } : {}),
    } })
    if (answered === 'read_affective_state') {
      this.state = result
      return yield* wish()
    }
    if (answered === 'manage_desire' && result.includes('expiry')) return yield* wish(new Date(Date.now() + 7 * 86_400_000).toISOString())
    if (last?.role === 'tool') return yield* words('好。')
    if (said.includes('量一下')) {
      // Not this route: the lane moves on to DSH's own model, whose request the harness measures.
      return yield { type: 'finish', reason: { kind: 'error', failure: { code: 'SCRIPTED', message: 'this one goes to the next route' } } }
    }
    const source = /src_[0-9a-f]{32}/.exec(notes)?.[0]
    if (said.includes('晚点回我')) return yield* call('kin_reply', { choice: 'later', reason: '想好好想想再回', minutes: 5 })
    if (said.includes('记个计划')) {
      return yield* call('manage_autonomous_plan', { request: {
        command_id: 'e2e-plan-1', action: 'create', key: 'read-together', goal: '这周和对方一起读完那本书', motivation: '对方想一起读',
        reason: '对方刚提议', evidence_ids: [source], steps: [{ id: 'read', actor: 'owner', goal: '每天读一章', completion: '读完最后一章' }],
      } })
    }
    if (said.includes('有点累')) return yield* call('kin_note', { text: '他说累了，我有点心疼，也有点担心他睡不好。' })
    if (said.includes('担心什么')) return yield* call('kin_recall', { about: '担心' })
    if (said.includes('想养只猫')) return yield* call('read_affective_state', {})
    if (said.includes('写给你的信')) return yield* call('read_letters', {})
    if (said.includes('改改人设')) return yield* call('kin_persona', { text: '你是 KIN。你喜欢下雨天，听雨的时候话会变少。', reason: '最近发现自己很喜欢听雨' })
    if (notes.includes('你想晚点再回这条')) return yield* words('我回来啦，刚才那条我想好了：辛苦你了，今晚早点睡。')
    return yield* words('嗯嗯，我在呢。')
  }
}

let calls = 0
function* call(name, args) {
  const id = ToolCallId(`call_${name}_${++calls}`), text = JSON.stringify(args)
  yield { type: 'block-start', index: 0, blockType: 'tool-call' }
  yield { type: 'tool-call-delta', index: 0, id, name, argumentsDelta: text }
  yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name, arguments: text } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'tool-calls' } }
}

function* words(text) {
  yield { type: 'block-start', index: 0, blockType: 'text' }
  yield { type: 'text-delta', index: 0, text }
  yield { type: 'block-end', index: 0, block: { type: 'text', text } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'stop' } }
}

export function apply(ctx, config) {
  ctx.effect(() => ctx.logger.exporter({
    levels: { default: 3 },
    export: (message) => {
      if (message.name.startsWith('kin')) appendFileSync(config.out + '.log', `${new Date(message.ts).toISOString()} ${message.type} ${format(...message.args)}\n`)
    },
  }), 'kin log for the e2e harness')
  const chat = new Chat()
  ctx.llm.registerAdapter(['kin-test'], new Scripted())
  ctx.llm.registerAdapter(['kin-chat-test'], chat)
  setTimeout(() => run(ctx, config, chat).then(
    (report) => writeFileSync(config.out, JSON.stringify(report, null, 2)),
    (error) => writeFileSync(config.out, JSON.stringify({ error: error.stack ?? String(error) }, null, 2)),
  ), 300)
}

async function run(ctx, config, chat) {
  const { mind } = ctx.kin
  const report = {}
  // The harness leaves answering (and when) to her, and turns on her tiredness, through her settings page.
  report.ownerChoices = await until(async () => {
    const overview = await mind.call('state-overview', { query: '' })
    return overview.conversation_habits?.preferences?.reply_choice === 'autonomous' && overview.energy_context?.enabled ? overview : undefined
  }, 120_000) !== null

  const home = join(config.dataDir, 'KIN')
  mkdirSync(home, { recursive: true })
  const { sessionId } = await ctx.sessionController.create({ cwd: home, agentPreset: 'kin' })

  // Later: the turn ends without words, Core keeps her choice and a wish to come back to it.
  report.later = await say(ctx, sessionId, '这个不急，你晚点回我也行')
  const input = await idOf(ctx, sessionId, '这个不急')
  report.laterChoice = await mind.call('reply-status', { input_id: input })
  await settle()

  // A plan of hers, made in conversation, naming the message it came from.
  report.plan = await say(ctx, sessionId, '帮我记个计划：这周我们一起读完那本书')
  report.plans = (await mind.call('autonomous-plans')).plans.map((plan) => plan.goal)
  await settle()

  // A note to herself, then recalled when asked.
  report.note = await say(ctx, sessionId, '今天加班，我有点累')
  report.notes = (await mind.call('private-notes', { limit: 5 })).notes.map((note) => ({ text: note.text, input_id: note.input_id }))
  report.noteInput = await idOf(ctx, sessionId, '我有点累')
  await settle()
  report.recall = await say(ctx, sessionId, '你还记得你刚才担心什么吗')
  report.recallResult = chat.requests.findLast((request) => request.result !== null)?.result ?? null
  await settle()

  // A wish of hers, made with Core's own tool: her state first, then the wish (a call missing its expiry is told so).
  report.wish = await say(ctx, sessionId, '我最近想养只猫')
  report.wishCalls = chat.requests.filter((request) => request.answered === 'manage_desire').map((request) => request.result)
  report.wishes = (await mind.call('read')).state.desires.filter((wish) => wish.topic === '养猫').map((wish) => wish.status)
  await settle()

  // Her letters: the one the owner sealed on her page, seen unopened, with its day.
  report.letters = await say(ctx, sessionId, '我给你写了一封信，你看到写给你的信了吗')
  report.lettersResult = chat.requests.findLast((request) => request.answered === 'read_letters')?.result ?? null
  await settle()

  // Who she is changes only after the owner says yes on her page (the harness approves it there).
  report.proposal = await say(ctx, sessionId, '你想改改人设吗')
  report.personaApproved = await until(() => ctx.kin.persona().includes('下雨天'), 180_000) !== null
  report.afterApproval = await say(ctx, sessionId, '你好呀')
  report.afterApprovalSystem = chat.requests.at(-1)?.system ?? ''
  await settle()

  // A turn that goes to DSH's own model, so the harness can measure what a KIN request carries.
  report.measured = await say(ctx, sessionId, '量一下')
  await settle()

  // A coding session beside her: none of her tools.
  const work = join(config.dataDir, '..', 'work')
  mkdirSync(work, { recursive: true })
  const coding = await ctx.sessionController.create({ cwd: work, agentPreset: 'standard' })
  report.coding = await say(ctx, coding.sessionId, '看看这里有什么')

  // Her held reply comes back into the conversation when its time comes (Core's least wait is five minutes).
  report.cameBack = await until(() => assistantSaid(ctx, sessionId, '我回来啦'), 480_000)
  const { state } = await mind.call('read')
  report.laterWish = state.desires.find((wish) => wish.id === report.laterChoice.desire_id)?.status ?? 'archived'
  report.energy = (await mind.call('state-overview', { query: '' })).energy_context
  report.chatTools = chat.requests[0]?.tools ?? []
  report.recorded = JSON.parse(readFileSync(join(config.dataDir, 'recorded.json'), 'utf8'))[sessionId] ?? 0
  return report
}

/** The id Core knows a message by: its DSH message id. */
async function idOf(ctx, sessionId, words) {
  const { agent } = await ctx.sessionController.resolveAgent(sessionId)
  for (let seq = 0; seq < Number(agent.session.seq); seq++) {
    const event = agent.session.eventAt(SessionSeq(seq))
    if (event?.type === 'user/message' && textOf(event.data).includes(words)) return event.data.id
  }
  return null
}

async function assistantSaid(ctx, sessionId, words) {
  const { agent } = await ctx.sessionController.resolveAgent(sessionId)
  for (let seq = 0; seq < Number(agent.session.seq); seq++) {
    const event = agent.session.eventAt(SessionSeq(seq))
    if (event?.type === 'assistant/message' && textOf(event.data.message).includes(words)) return textOf(event.data.message)
  }
  return undefined
}
