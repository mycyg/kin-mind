// Real DSH conversation, Core persistence and delivery ledger; the only transport is an in-memory fake.
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { LlmAdapter, createUserMessage } from '@deepseek-ai/dsh-llm'
import { Scripted, until } from './scenario.mjs'
export const name = 'kin-reply-channel-test'
export const inject = ['kin', 'llm', 'sessionController', 'sessions']
const note = '我有点开心，想把这点暖意先留在心里。'
const wrap = (text, private_note = note) => JSON.stringify({ private_note, action: text ? 'send' : 'silent', bubbles: text ? [{ text }] : [] })
function* words(text, finish = { kind: 'stop' }) {
  for (const part of text.match(/.{1,9}/gs) ?? []) yield { type: 'text-delta', index: 0, text: part }
  yield { type: 'block-end', index: 0, block: { type: 'text', text } }
  yield { type: 'finish', reason: finish }
}
class Chat extends LlmAdapter {
  mode = 'valid'; calls = 0; requests = []; round = 0
  providerInfo() { return { id: 'reply-test', name: 'reply test' } }
  async listModels() { return [] }
  async resolveModel(provider, id) { return { provider, id, name: id, context: { contextWindow: 1000000 }, defaultMaxTokens: 2000 } }
  async *stream(request) {
    this.calls++; this.requests.push(request)
    const answered = request.messages.filter(m => m.source?.kind !== 'kin-context' && m.source?.kind !== 'runtime-context').at(-1)?.role === 'tool'
    if (this.mode === 'invalid') return yield* words('Thinking Process: Synthetic private analysis.\n\nNever publish this.')
    if (this.mode === 'late-error') return yield* words(wrap('Must not publish.'), { kind: 'error', failure: { code: 'SERVER', message: 'synthetic failed stream' } })
    if (this.mode === 'silent') return yield* words(wrap('', '我想先安静一会儿。'))
    if (this.mode === 'store-failure') return yield* words(wrap('Must not publish.', 'synthetic-store-failure'))
    if (this.mode === 'tools' && !answered) {
      for (const [index, name, args] of [[0, 'kin_note', { text: '我把这一小点在意记住了。' }], [1, 'read_private_notes', { limit: 2 }]]) {
        yield { type: 'block-end', index, block: { type: 'tool-call', id: 'synthetic-call-' + index, name, arguments: JSON.stringify(args) } }
      }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }; return
    }
    if (this.mode === 'tool-error' && !answered) {
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: 'synthetic-invalid', name: 'read_event_thread', arguments: '{"identifier":42}' } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }; return
    }
    yield { type: 'block-end', index: 1, block: { type: 'reasoning', text: 'Synthetic native reasoning, not a mood note.' } }
    yield* words(wrap(this.mode === 'tools' ? '记好啦，也翻到了。' : this.mode === 'tool-error' ? '这次没查到，我知道参数不对。' : '来啦，一起看看。'))
  }
}
export function apply(ctx, config) {
  const chat = new Chat(); ctx.llm.registerAdapter(['reply-test'], chat); ctx.llm.registerAdapter(['kin-test'], new Scripted())
  setTimeout(() => run(ctx, config, chat).then(report => writeFileSync(config.out, JSON.stringify(report, null, 2)), error => writeFileSync(config.out, JSON.stringify({ error: error.stack }, null, 2))), 300)
}
async function run(ctx, config, chat) {
  const sent = [], listeners = new Set(), transient = []
  const transport = { id: 'wechat', start() {}, close() {}, status: () => ({ state: 'connected' }), async typing() {}, async send(to, part, id) {
    const notes = await ctx.kin.mind.call('private-notes', { limit: 20 })
    sent.push({ text: part.text, id, noteAlreadyStored: notes.notes.some(n => n.text === note) })
    return { state: 'accepted', platformId: 'synthetic-' + sent.length }
  } }
  const fake = { get: id => id === 'wechat' ? transport : undefined, onInbound: fn => (listeners.add(fn), () => listeners.delete(fn)), stickers: () => [], usedSticker() {} }
  const impl = ctx.reflect._getImpl('kinTransports', true); impl.check = () => false; ctx.reflect.notify(['kinTransports']); impl.value = fake; impl.check = undefined; ctx.reflect.notify(['kinTransports'])
  await new Promise(r => setTimeout(r, 100))
  ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') transient.push(frame.chunk.text) })
  const call = ctx.kin.mind.call.bind(ctx.kin.mind); let coreInvalidCalls = 0
  ctx.kin.mind.call = (action, args, ...rest) => {
    if (action === 'private-note-save' && args.text === 'synthetic-store-failure') return Promise.reject(Error('synthetic storage unavailable'))
    if (action === 'chat-tool' && args.name === 'read_event_thread') coreInvalidCalls++
    return call(action, args, ...rest)
  }
  for (const fn of listeners) fn({ transport: 'wechat', id: 'synthetic-inbound', kind: 'direct', at: new Date().toISOString(), chat: 'synthetic-chat', sender: 'synthetic-owner', text: 'synthetic initial message' })
  const sessions = () => existsSync(join(config.dataDir, 'sessions.json')) && JSON.parse(readFileSync(join(config.dataDir, 'sessions.json'), 'utf8'))
  const conversation = await until(() => sessions()?.['wechat:synthetic-chat'], 30000); assert.ok(conversation)
  const { agent } = await ctx.sessionController.resolveAgent(conversation.sessionId)
  await until(() => sent.length, 30000); await agent.whenIdle()
  const report = { sent, checks: {} }
  const firstNotes = await call('private-notes', { limit: 20 })
  report.checks.initial = sent.length === 1 && sent[0].text === '来啦，一起看看。' && sent[0].noteAlreadyStored && firstNotes.notes.some(n => n.text === note)
  for (const mode of ['invalid', 'late-error', 'silent', 'store-failure', 'tools', 'tool-error']) {
    chat.mode = mode; const before = Number(agent.session.seq), count = sent.length, calls = chat.calls
    agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'synthetic case ' + mode }] }))
    await ctx.sessions.flush(agent.session); await agent.whenIdle()
    await new Promise(r => setTimeout(r, 300))
    const events = agent.session.snapshotEvents().slice(before)
    report[mode] = { sent: sent.slice(count).filter(s => !s.id.startsWith('notice:')), notices: sent.slice(count).filter(s => s.id.startsWith('notice:')), calls: chat.calls - calls, ended: events.filter(e => e.type === 'turn/end').map(e => e.data.reason.kind),
      publicText: events.filter(e => e.type === 'assistant/message').flatMap(e => e.data.message.content.filter(b => b.type === 'text').map(b => b.text)),
      tools: events.filter(e => e.type === 'assistant/message').flatMap(e => e.data.message.content.filter(b => b.type === 'tool-call').map(b => b.name)) }
  }
  const deliveryRows = Object.values(JSON.parse(readFileSync(join(config.dataDir, 'deliveries.json'), 'utf8')))
  report.deliveryAccepted = deliveryRows.filter(d => d.state === 'accepted' && !d.id.startsWith('notice:')).length
  report.coreInvalidCalls = coreInvalidCalls
  report.transient = transient
  report.assistantText = agent.session.snapshotEvents().filter(e => e.type === 'assistant/message').flatMap(e => e.data.message.content.filter(b => b.type === 'text').map(b => b.text))
  report.noteContexts = chat.requests.map(r => r.messages.filter(m => m.source?.noteIds).map(m => ({ ids: m.source.noteIds, text: m.content.map(b => b.text ?? '').join('') })))
  report.requestContract = chat.requests.every(r => r.messages.some(m => m.role === 'system' && JSON.stringify(m.content).includes('private_note')))
  return report
}
