import { replyEnvelope } from './reply-fixture.mjs'
// Dev-only scenario for her conversations and reaching out, mounted by dev/e2e-contact.mjs inside a real headless
// DSH. A fake WeChat stands in on the channels module's `kinTransports` service: the owner (小光), a friend (小明)
// and a family group write to her through it, and everything she sends is kept. Her voice is a fake speech worker
// and her meme library one sticker. Writes `config.out`.
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { format } from 'node:util'
import { LlmAdapter, ToolCallId, createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'

export const name = 'kin-e2e-contact'
export const inject = ['kin', 'llm', 'sessionController', 'sessions']

/** Transport send attempts, typing and receipt checks. Attempts are not deduplicated: this scenario must not resend a confirmed part. */
class FakeWechat {
  id = 'wechat'
  sent = []
  typing_ = []
  checked = []
  /** Parts whose send answers "unknown" (the request may or may not have landed), until the scenario has seen it so. */
  unknownText = '小明，在忙吗？'
  known = false
  start() {}
  close() {}
  status() { return { state: 'connected' } }
  async send(to, part, id) {
    if (part.text === 'Synthetic accepted paragraph.') await new Promise(resolve => setTimeout(resolve, 200))
    this.sent.push({ chat: to.chat, text: part.text ?? '', file: part.file, kind: part.kind, named: part.named, id, at: new Date().toISOString() })
    if (part.text === this.unknownText) return { state: 'unknown', reason: 'timeout' }
    return { state: 'accepted', platformId: 'wx-' + this.sent.length }
  }
  async check(to, id) {
    this.checked.push(id)
    return this.known ? { state: 'accepted', platformId: 'wx-checked' } : { state: 'unknown' }
  }
  async typing(to, on) { this.typing_.push({ chat: to.chat, on }) }
}

/**
 * Her chat lane in this run: a conversation turn gets a short reply (or, asked to pass something on, the relay
 * tool); a draft of her own message answers by who it is for. Every draft is kept for the report.
 */
class Chat extends LlmAdapter {
  drafts = []
  providerInfo() { return { id: 'kin-test', name: 'KIN test' } }
  async listModels() { return [] }
  async resolveModel(provider, model) {
    return { provider, id: model, name: model, inputModalities: ['text'], defaultMaxTokens: 2000, context: { contextWindow: 200_000 } }
  }
  async *stream(options) {
    if (options.purpose === 'compaction') {
      this.partialSummaries = (this.partialSummaries ?? 0) + 1
      return yield* words(['Primary Request and Intent', 'Key Technical Concepts', 'Files and Code', 'Errors and Fixes', 'Pending Jobs', 'Current Work', 'Next Step', 'Critical Context'].map(name => `## ${name}\n- Synthetic retained context.`).join('\n\n'))
    }
    if (options.model === 'scripted') return yield* appraisal(options)
    const texts = (options.messages ?? []).filter(m => m.source?.kind !== 'kin-context' || !m.source?.noteIds).map((m) => ({ role: m.role, text: (m.content ?? []).map((b) => b.text ?? '').join('') }))
    if (options.system?.includes('最后只输出一个 JSON 对象')) {
      const user = texts.at(-1).text
      this.drafts.push({ system: options.system, user })
      const decision = user.includes('对方是小明') ? { action: 'send', bubbles: ['小明，在忙吗？', '今天天气挺好的'] }
        : user.includes('群里的新消息') ? { action: 'wait', reason: '不打扰大家' }
        : { action: 'send', bubbles: ['想你啦，下班了吗'] }
      return yield* words(JSON.stringify(decision))
    }
    // What was said last: the newest words of a person, not her recall block, her own told words or DSH's runtime notes.
    const answered = texts.at(-1)?.role === 'tool'
    const said = texts.findLast((m) => m.role === 'user' && !/^(（|这条消息的来源编号|Current runtime context|Time sampled)/.test(m.text))?.text ?? ''
    if (said.includes('synthetic partial failure')) {
      this.partialCalls = (this.partialCalls ?? 0) + 1
      if (this.partialCalls > 1) return yield* words(replyEnvelope('Synthetic regenerated reply.'))
      yield { type: 'text-delta', index: 0, text: 'Synthetic accepted paragraph.\n\nUnfinished tail' }
      await new Promise(resolve => setTimeout(resolve, 30))
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'CONTEXT_WINDOW_EXCEEDED', message: 'Synthetic late overflow' } } }
      return
    }
    // What she read of another conversation, kept for the report.
    if (answered && texts.at(-1).text.includes('最近的话')) (this.looks ??= []).push(texts.at(-1).text)
    const relay = /转告小明：(.+)/.exec(said)
    const look = /看看(\S+?)那边|和(\S+?)聊了什么/.exec(said)
    if (!answered && look) return yield* call('kin_conversations', { who: look[1] ?? look[2] })
    if (!answered && relay) return yield* call('kin_tell', { to: '小明', text: '小光说' + relay[1] })
    if (!answered && said.includes('把报告发我')) return yield* call('kin_send_file', { path: this.report, asked: true })
    if (!answered && said.includes('发个表情')) return yield* call('kin_sticker', { meaning: '开心得转圈' })
    if (!answered && said.includes('用语音说')) return yield* call('kin_voice', { text: '晚安呀，早点睡', style: '轻声' })
    return yield* words(replyEnvelope(answered ? '好，我跟他说啦' : '收到啦～'))
  }
}

function* words(text) {
  yield { type: 'block-start', index: 0, blockType: 'text' }
  yield { type: 'text-delta', index: 0, text }
  yield { type: 'block-end', index: 0, block: { type: 'text', text } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'stop' } }
}

let calls = 0
function* call(name, input) {
  const id = ToolCallId('call_' + ++calls)
  const args = JSON.stringify(input)
  yield { type: 'block-start', index: 0, blockType: 'tool-call' }
  yield { type: 'tool-call-delta', index: 0, id, name, argumentsDelta: args }
  yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name, arguments: args } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'tool-calls' } }
}

/** Her task lane: each review leaves her a little flustered (色色值 up) and wanting to reach out; other calls get an empty input. */
function* appraisal(options) {
  const tools = (options.tools ?? []).map((tool) => tool.name)
  const name = tools.includes('submit_appraisal') ? 'submit_appraisal' : tools[0]
  if (!name) return yield* words('{}')
  yield* call(name, name === 'submit_appraisal' ? {
    reason: '想起了刚才的聊天。', values: { mood: 62, flirtation: 85 },
    wishes: [{ kind: 'contact', topic: '想找对方说说话', content: '想问问对方今天过得怎么样', strength: 80, ttl_hours: 24, completion: '对方回了我' }],
  } : name === 'submit_compression' ? compression(options.messages) : {})
}

function compression(messages) {
  const user = [...messages].reverse().find((message) => message.role === 'user')
  const items = JSON.parse(user.content.map((block) => block.text ?? '').join('')).items ?? []
  return {
    entries: items.length ? [{ item_ids: items.map((item) => item.id), summary: items.map((item) => item.text).join('；').slice(0, 400) }] : [],
    omitted_ids: [],
  }
}

export function apply(ctx, config) {
  ctx.effect(() => ctx.logger.exporter({
    levels: { default: 3 },
    export: (message) => {
      if (message.name.startsWith('kin')) appendFileSync(config.out + '.log', `${new Date(message.ts).toISOString()} ${message.type} ${format(...message.args)}\n`)
    },
  }), 'kin log for the e2e harness')
  const wechat = new FakeWechat()
  const listeners = new Set()
  // Her voice and her meme library: a speech worker that writes a short Ogg file, and one sticker of a cat.
  const spoken = []
  const sticker = join(config.dataDir, 'memes', 'originals', 'cat.png')
  mkdirSync(dirname(sticker), { recursive: true })
  writeFileSync(sticker, Buffer.from(PNG, 'base64'))
  const fake = {
    get: (id) => (id === 'wechat' ? wechat : undefined),
    onInbound: (listener) => (listeners.add(listener), () => listeners.delete(listener)),
    async speak(text, id, style) {
      const file = join(config.dataDir, 'voice', id.replace(/[^\w-]/g, '_') + '.ogg')
      mkdirSync(dirname(file), { recursive: true })
      writeFileSync(file, 'OggS synthetic voice')
      spoken.push({ text, id, style })
      return file
    },
    stickers: (words) => (words.includes('开心') ? [{ id: 'cat', file: sticker, description: '小猫开心得转圈' }] : []),
    usedSticker: () => {},
  }
  const chat = new Chat()
  ctx.llm.registerAdapter(['kin-test'], chat)
  let n = 0
  const arrive = (message) => {
    for (const listener of listeners) listener({ transport: 'wechat', id: 'm' + ++n, kind: 'direct', at: new Date().toISOString(), ...message })
  }
  setTimeout(() => standIn(ctx, fake).then(() => run(ctx, config, { wechat, chat, arrive, spoken })).then(
    (report) => writeFileSync(config.out, JSON.stringify(report, null, 2)),
    (error) => writeFileSync(config.out, JSON.stringify({ error: error.stack ?? String(error) }, null, 2)),
  ), 300)
}

async function run(ctx, config, { wechat, chat, arrive, spoken }) {
  const report = {}
  await ctx.kin.mind.call('liveness-facts')
  const sessions = () => JSON.parse(readFileSync(join(config.dataDir, 'sessions.json'), 'utf8'))
  const deliveries = () => Object.values(JSON.parse(readFileSync(join(config.dataDir, 'deliveries.json'), 'utf8')))
  const app = await until(() => existsSync(join(config.dataDir, 'sessions.json')) && sessions().app?.sessionId)

  // The owner at the desk.
  await say(ctx, app, '今天下班好累')

  // 小明 writes twice within twenty seconds (one second at the dev pace): one message to her, and her answer goes back on WeChat.
  arrive({ chat: 'wx-ming', sender: 'wx-ming', senderName: 'Ming', text: '在吗' })
  // Same tick: both really arrive inside the burst window even when the runner is overloaded.
  arrive({ chat: 'wx-ming', sender: 'wx-ming', senderName: 'Ming', text: '我今天看到星星了' })
  report.ming = await until(async () => {
    const key = sessions()['wechat:wx-ming']
    const turns = key && (await events(ctx, key.sessionId)).filter((e) => e.source === 'kin-channel')
    return turns?.length && wechat.sent.some((s) => s.chat === 'wx-ming') ? { turns, conversation: key } : undefined
  }, 60_000)
  report.typing = await until(() => {
    const typing = wechat.typing_.filter((t) => t.chat === 'wx-ming')
    return typing.some(t => t.on) && typing.at(-1)?.on === false ? typing : undefined
  }, 30_000) ?? []

  // The family group: plain talk is heard but not answered; an @ is answered, with what was said before it.
  arrive({ chat: 'family@chatroom', kind: 'group', sender: 'wx-ming', text: '今晚吃什么' })
  await pause(4000)
  report.groupQuiet = { turns: await groupTurns(ctx, sessions), sent: wechat.sent.filter((s) => s.chat === 'family@chatroom').length }
  arrive({ chat: 'family@chatroom', kind: 'group', sender: 'wx-owner', mentioned: true, text: '@KIN 你觉得呢' })
  report.groupMentioned = await until(async () => {
    const turns = await groupTurns(ctx, sessions)
    return turns.length && wechat.sent.some((s) => s.chat === 'family@chatroom') ? { turns, sent: wechat.sent.filter((s) => s.chat === 'family@chatroom') } : undefined
  }, 30_000)

  // A relay: the owner asks her to pass something on to 小明.
  await say(ctx, app, '帮我转告小明：晚上一起吃饭')
  report.relay = await until(() => wechat.sent.find((s) => s.chat === 'wx-ming' && s.text.includes('晚上一起吃饭')), 30_000)
  report.relayTold = await until(async () => (await events(ctx, sessions()['wechat:wx-ming'].sessionId)).find((e) => e.source === 'kin-said'), 30_000)

  // Once the conversations have been quiet a while, her reviews (the owner's, and 小明's in his own view) leave her
  // wanting to reach out: to the owner at the desk, to 小明 on WeChat, where the first part's outcome is unknown.
  // Observe the uncertain friend delivery first. The shared review may reach the friend before the owner;
  // waiting for the owner's independent draft here could cross a retry deadline on a busy runner.
  report.mingUnknown = await until(() => deliveries().find((d) => d.kind === 'contact' && d.recipient !== 'person:person_a' && d.state === 'unknown'), 240_000)
  wechat.known = true
  report.ownerContact = await until(async () => (await events(ctx, app)).find((e) => e.source === 'kin-said' && e.text.includes('想你啦')), 480_000)
  report.drafts = chat.drafts.map((d) => ({ who: /对方是(\S+?)。/.exec(d.user)?.[1], intimate: d.user.includes('色色值') }))

  // In the owner's view her intimate side shows; in 小明's it does not.
  const view = async (recipient) => (await ctx.kin.mind.call('memory-context', {
    query: '今天', purpose: 'chat', mode: 'light', recipient_person_id: recipient, conversation_key: 'probe:' + recipient,
  })).rendered_text ?? ''
  report.ownerView = (await view('person:person_a')).includes('flirtation')
  report.mingView = (await view(report.ming?.conversation.recipient)).includes('flirtation')

  // The unknown part is asked about by its own id after a minute, then the rest of that message goes.
  // Unknown-check retries are covered separately in check-stop.mjs; here the first check can confirm it.
  report.reconciled = await until(() => {
    const delivery = deliveries().find((d) => d.id === report.mingUnknown?.id)
    return delivery?.state === 'accepted' ? delivery : undefined
  }, 480_000)
  report.mingSends = wechat.sent.filter((s) => s.chat === 'wx-ming')
  report.checked = wechat.checked
  report.drafts = chat.drafts.map((d) => ({ who: /对方是(\S+?)。/.exec(d.user)?.[1], intimate: d.user.includes('色色值') }))
  report.contactCore = (await ctx.kin.mind.call('liveness-facts')).last ?? null

  // Quiet hours: the harness sets them around now; a relay asked for then waits for them to end.
  writeFileSync(config.out + '.phase', 'quiet')
  await until(() => existsSync(config.out + '.quiet'), 60_000)
  await say(ctx, app, '帮我转告小明：明天见')
  report.quietRelay = await until(() => deliveries().find((d) => d.kind === 'relay' && d.state === 'pending' && d.notBefore), 30_000)
  report.quietSent = wechat.sent.some((s) => s.text.includes('明天见'))

  // The owner on WeChat: a picture to look at, and a file asked for by name, which goes out as one.
  const photo = join(config.dataDir, 'photo.png')
  writeFileSync(photo, Buffer.from(PNG, 'base64'))
  chat.report = join(config.dataDir, '报告.pdf')
  writeFileSync(chat.report, 'synthetic report')
  arrive({ chat: 'wx-owner', sender: 'wx-owner', text: '把报告发我', files: [{ path: photo, kind: 'image' }] })
  report.ownerWechat = await until(async () => {
    const owner = sessions()['wechat:wx-owner']
    const turn = owner && (await events(ctx, owner.sessionId)).find((e) => e.source === 'kin-channel')
    const file = wechat.sent.find((s) => s.chat === 'wx-owner' && s.file)
    return turn && file ? { turn, file } : undefined
  }, 60_000)

  // A sticker and a voice message, as she chooses them: on WeChat as an image and a voice part, in her desktop chat shown.
  arrive({ chat: 'wx-owner', sender: 'wx-owner', text: '发个表情给我' })
  report.sticker = await until(() => wechat.sent.find((s) => s.chat === 'wx-owner' && s.kind === 'image'), 60_000)
  arrive({ chat: 'wx-owner', sender: 'wx-owner', text: '用语音说晚安' })
  report.voice = await until(() => wechat.sent.find((s) => s.chat === 'wx-owner' && s.kind === 'voice'), 60_000)
  report.spoken = spoken
  await say(ctx, app, '发个表情给我')
  report.appSticker = await until(async () => (await events(ctx, app)).find((e) => e.source === 'kin-said' && e.media), 30_000)

  // Her other conversations, looked at from the owner's desktop chat and from 小明's: who said what there, and when.
  // The tool returns the latest 12 lines; keep this assertion's source recent even on slower runners.
  await say(ctx, app, '今天下班好累')
  await say(ctx, app, '看看小明那边在聊什么')
  arrive({ chat: 'wx-ming', sender: 'wx-ming', text: '你和小光聊了什么' })
  report.looks = await until(() => (chat.looks?.length >= 2 ? chat.looks : undefined), 60_000)
  // A late overflow is buffered: recovery may regenerate, but no failed paragraph reaches the transport.
  const ownerSession = sessions()['wechat:wx-owner'].sessionId
  const { agent: ownerAgent } = await ctx.sessionController.resolveAgent(ownerSession)
  await ownerAgent.whenIdle()
  const beforePartial = Number(ownerAgent.session.seq)
  chat.partialSummaries = 0
  arrive({ chat: 'wx-owner', sender: 'wx-owner', text: 'synthetic partial failure' })
  await until(() => chat.partialCalls > 0, 60000)
  await ownerAgent.whenIdle()
  const partialEvents = ownerAgent.session.snapshotEvents().slice(beforePartial)
  report.partialFailure = { calls: chat.partialCalls, summaries: chat.partialSummaries,
    sent: wechat.sent.filter(s => s.text.startsWith('Synthetic ')),
    delivery: await until(() => deliveries().find(d => d.parts?.some(p => p.text === 'Synthetic regenerated reply.') && d.sent?.some(s => s.state === 'accepted')), 10000),
    ended: partialEvents.filter(e => e.type === 'turn/end').map(e => e.data.reason.kind),
    compactions: partialEvents.filter(e => e.type === 'compaction/start').length,
    attempts: partialEvents.filter(e => e.type === 'assistant/attempt').length }
  report.sessions = sessions()
  return report
}

/**
 * The fake takes the place of the channels module's transports (through cordis internals, dev only): the service
 * is gone for a moment, so whoever listens there subscribes again, to the fake.
 */
async function standIn(ctx, fake) {
  const impl = ctx.reflect._getImpl('kinTransports', true)
  impl.check = () => false
  ctx.reflect.notify(['kinTransports'])
  impl.value = fake
  impl.check = undefined
  ctx.reflect.notify(['kinTransports'])
  await pause(100)
}

async function groupTurns(ctx, sessions) {
  const group = sessions()['wechat:family@chatroom']
  return group ? (await events(ctx, group.sessionId)).filter((e) => e.source === 'kin-channel') : []
}

async function say(ctx, sessionId, text) {
  const { agent } = await ctx.sessionController.resolveAgent(sessionId)
  agent.followup(createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } }))
  await ctx.sessions.flush(agent.session)
  await pause(500)
  await agent.whenIdle()
}

async function events(ctx, sessionId) {
  const { agent } = await ctx.sessionController.resolveAgent(sessionId)
  const found = []
  for (let seq = 0; seq < Number(agent.session.seq); seq++) {
    const event = agent.session.eventAt(SessionSeq(seq))
    if (event?.type === 'user/message') {
      found.push({
        source: event.data.source.kind, text: textOf(event.data), media: event.data.source.media,
        images: event.data.content.filter((block) => block.type === 'image').length,
      })
    }
    else if (event?.type === 'assistant/message') found.push({ source: 'assistant', text: textOf(event.data.message) })
  }
  return found
}

async function until(probe, ms = 90_000) {
  for (const end = Date.now() + ms; Date.now() < end; await pause(500)) {
    const value = await probe()
    if (value) return value
  }
  return null
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
/** A one-pixel PNG. */
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='

function textOf(message) {
  return message.content.filter((block) => block.type === 'text').map((block) => block.text).join('\n')
}
