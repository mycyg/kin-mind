// Real DSH compaction transaction and session replay, synthetic model only; no network or contact sends.
import assert from 'node:assert/strict'
import { Context } from '@deepseek-ai/cordis'
import LlmRuntime, { LlmAdapter, createUserMessage, createAssistantMessage, createSystemMessage } from '@deepseek-ai/dsh-llm'
import SessionStore, { Session, SessionId } from '@deepseek-ai/dsh-session'
import { CompactionId } from '@deepseek-ai/dsh-compaction'
import TokenMeter from '@deepseek-ai/dsh-token-meter'
import Basic from '@deepseek-ai/dsh-compaction-basic'
import Kin from '../lib/compaction.js'
const Engine = process.env.KIN_COMPACTION_BASELINE ? Basic : Kin
const sections = ['Primary Request and Intent', 'Key Technical Concepts', 'Files and Code', 'Errors and Fixes', 'Pending Jobs', 'Current Work', 'Next Step', 'Critical Context']
const valid = sections.map((s, i) => `## ${s}\n- ${i === 0 ? 'Earlier promise: bring the blue notebook on Friday. New correction: meeting is Saturday.' : i === 7 ? 'The speaker prefers quiet mornings; the latest topic was tea.' : '(none)'}`).join('\n\n')
let response = valid, finish = 'stop', requests = [], failures = 0, toolOutput = false
class Synthetic extends LlmAdapter {
  async resolveModel(provider, id) { return { provider, id, name: id, context: { contextWindow: 100000 } } }
  async *stream(options) {
    requests.push(options)
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: response }
    yield { type: 'block-end', index: 0, block: { type: 'text', text: response } }
    if (toolOutput) {
      yield { type: 'block-start', index: 1, blockType: 'tool-call' }
      yield { type: 'block-end', index: 1, block: { type: 'tool-call', id: 'synthetic-call', name: 'send_message', arguments: '{}' } }
    }
    yield { type: 'finish', reason: { kind: finish } }
  }
}
function seed() {
  const session = Session.create(SessionId('synthetic-continuity-' + Math.random()))
  session.append('system/message', { turn: 1, step: 1, message: createSystemMessage('You are the conversational companion. Answer the speaker warmly.') }, { surfaceOp: 'append' })
  for (let turn = 1; turn <= 3; turn++) {
    session.append('turn/start', { turn })
    session.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text:
      (turn === 1 ? '<compacted-summary>Earlier promise: bring the blue notebook on Friday. The speaker prefers quiet mornings.</compacted-summary>' : turn === 2 ? 'Correction: meeting is Saturday. Today we discussed tea.' : 'Newest tail: please keep the ticket receipt.') + '\n' + 'Synthetic older detail. '.repeat(400) }] }), { surfaceOp: 'append' })
    session.append('step/start', { turn, step: 1 })
    if (turn === 1) session.append('request/header', { header: { config: { provider: 'synthetic', model: 'synthetic' } }, reason: 'initial' })
    session.append('assistant/message', { turn, step: 1, stream: [], message: createAssistantMessage({ source: { provider: 'synthetic', model: 'synthetic' }, content: [{ type: 'text', text: 'Recorded response. '.repeat(100) }] }) }, { surfaceOp: 'append' })
    session.append('step/end', { turn, step: 1 }); session.append('turn/end', { turn, reason: { kind: 'completed' } })
  }
  return session
}
function harness() {
  const ctx = new Context()
  new LlmRuntime(ctx); new SessionStore(ctx); new TokenMeter(ctx)
  // Detached synthetic sessions: durability is verified by JSON round-trip and fresh Session replay below.
  ctx.sessions.flush = async () => false
  ctx.llm.registerAdapter(['synthetic'], new Synthetic())
  const engine = new Engine(ctx, { auto: false, headroomTokens: 1024, maxTokens: 2048, retainTokens: 4000 })
  const session = seed()
  const agent = { session, options: { provider: 'synthetic', model: 'synthetic' }, runMaintenance: fn => fn(new AbortController().signal) }
  return { ctx, engine, session, agent }
}
async function check(name, fn) { try { await fn(); console.log('✓ ' + name) } catch (error) { failures++; console.error('✗ ' + name + '\n' + error.stack) } }
await check('ordinary conversational output is rejected without losing the prior checkpoint or newer messages', async () => {
  const { ctx, engine, session, agent } = harness()
  try {
    response = 'Of course, a cup of tea sounds lovely.'
    const before = session.deriveMessages(), events = session.snapshotEvents().length
    await assert.rejects(engine.compactNow(agent, new AbortController().signal), /checkpoint|summary/)
    assert.deepEqual(session.deriveMessages(), before)
    assert.equal(session.snapshotEvents().slice(events).filter(e => e.type === 'compaction/summary').length, 0)
  } finally { await ctx.fiber.dispose() }
})
await check('cumulative checkpoint and untouched tail survive replacement and session replay', async () => {
  const { ctx, engine, session, agent } = harness()
  try {
    response = valid; requests = []
    session.append('turn/start', { turn: 4 })
    const before = [...session.snapshotEvents()]
    const tail = session.deriveMessages().slice(-2)
    const result = await engine.compactRegion(session.surface.nodes[1], session.surface.nodes[4], agent, new AbortController().signal)
    session.append('turn/end', { turn: 4, reason: { kind: 'completed' } })
    assert.deepEqual(session.deriveMessages().slice(-2), tail)
    assert.ok(result)
    const request = requests.at(-1)
    assert.equal(request.tools, undefined); assert.equal(request.toolHistory, undefined)
    assert.equal(request.messages[0].role, 'system')
    assert.match(request.messages[0].content[0].text, /archivist/)
    assert.ok(request.messages.slice(1).every(message => message.role === 'user'))
    const source = JSON.stringify(request.messages)
    assert.match(source, /blue notebook/); assert.match(source, /meeting is Saturday/)
    assert.deepEqual(session.snapshotEvents().slice(0, before.length), before, 'raw history is append-only')
    const replay = Session.create(session.id, JSON.parse(JSON.stringify(session.snapshotEvents())))
    assert.deepEqual(replay.deriveMessages(), session.deriveMessages())
    const visible = JSON.stringify(replay.deriveMessages())
    assert.match(visible, /blue notebook/); assert.match(visible, /meeting is Saturday/)
    assert.match(visible, /quiet mornings/); assert.match(visible, /ticket receipt/)
  } finally { await ctx.fiber.dispose() }
})
await check('missing sections, empty checkpoints and truncated output never replace the surface', async () => {
  for (const [text, reason] of [[valid.replace('## Current Work', '## Wrong'), 'stop'], [sections.map(s => `## ${s}\n- (none)`).join('\n'), 'stop'], [valid, 'max-tokens']]) {
    const { ctx, engine, session, agent } = harness()
    try { response = text; finish = reason; const before = session.deriveMessages(); await assert.rejects(engine.compactNow(agent, new AbortController().signal)); assert.deepEqual(session.deriveMessages(), before) }
    finally { finish = 'stop'; await ctx.fiber.dispose() }
  }
})
await check('source identities and attached media survive task isolation without exposing tool execution', async () => {
  const { ctx, engine, agent } = harness()
  try {
    response = valid; requests = []
    const source = { kind: 'compact-checkpoint', compactionId: CompactionId('synthetic-checkpoint') }
    const image = { type: 'image', attachment: { attachmentId: 'synthetic-image', mediaType: 'image/png', bytes: 1, width: 1, height: 1 } }
    const message = createUserMessage({ source, content: [{ type: 'text', text: 'Earlier cumulative facts.' }, image] })
    const recall = createUserMessage({ source: { kind: 'kin-context', form: 'snapshot', sections: [], rests: ['synthetic-deleted-id'] }, content: [{ type: 'text', text: 'A recall with deletion tracking.' }] })
    await engine.summarize({ messages: [message, recall], tools: [{ name: 'send_message', description: 'must not run', parameters: { type: 'object', properties: {} } }] }, agent)
    const sent = requests.at(-1)
    assert.deepEqual(sent.messages[1].source, source)
    assert.deepEqual(sent.messages[2].source, recall.source)
    assert.equal(sent.tools, undefined); assert.equal(sent.toolHistory, undefined)
    assert.match(sent.messages[1].content[0].text, /Earlier cumulative facts/)
    assert.deepEqual(sent.messages[1].content[1], image)
    toolOutput = true
    await assert.rejects(engine.summarize({ messages: [message] }, agent), /text-only checkpoint/)
    toolOutput = false
  } finally { await ctx.fiber.dispose() }
})
process.exitCode = failures ? 1 : 0
