import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createSystemMessage, createUserMessage } from '@deepseek-ai/dsh-llm'
import { Session } from '@deepseek-ai/dsh-session'
import { sessionFormatCatalog } from '@deepseek-ai/dsh-session-format-catalog'

const conversationOutput = fileURLToPath(new URL('../.session-head-conversations-check.mjs', import.meta.url))
const output = fileURLToPath(new URL('../.session-head-check.mjs', import.meta.url))
await build({ entryPoints: [fileURLToPath(new URL('../src/contact/session-head.ts', import.meta.url))], outfile: output, bundle: true, platform: 'node', format: 'esm', packages: 'external' })
await build({ entryPoints: [fileURLToPath(new URL('../src/contact/conversations.ts', import.meta.url))], outfile: conversationOutput, bundle: true, platform: 'node', format: 'esm', packages: 'external' })
try {
  const { assertReceiptSurfaceReady, ReceiptSurfaceNotReadyError } = await import(output)
  const header = { version: 4, id: 'session-head-regression', createdAt: 1, cwd: '/workspace/project', isSeeded: false, delegationDepth: 0 }
  const user = () => ({ type: 'user/message', time: 2, data: createUserMessage({ content: [{ type: 'text', text: 'Synthetic delivery receipt' }], source: { kind: 'kin-said', id: 'delivery-test' } }), surfaceOp: 'append' })
  const turn = [
    { type: 'turn/start', time: 3, data: { turn: 1 } },
    { type: 'step/start', time: 4, data: { turn: 1, step: 1 } },
    { type: 'system/message', time: 5, data: { turn: 1, step: 1, message: createSystemMessage('Synthetic system prompt') }, surfaceOp: 'append' },
    { type: 'step/end', time: 6, data: { turn: 1, step: 1 } },
    { type: 'turn/end', time: 7, data: { turn: 1 } },
  ]
  const number = events => events.map((event, seq) => ({ ...event, seq }))
  const validate = events => {
    const restore = sessionFormatCatalog.createRestore({ type: 'session', ...header }, { recovery: 'strict', validation: 'current' })
    for (const event of events) restore.decodeRow(event)
    return restore.finish()
  }
  const late = number([user(), ...turn])
  assert.throws(() => validate(late), /system\/message requires a protected first surface head/)
  const blank = Session.create(header.id, [], header, 0)
  const blankBefore = JSON.stringify(blank.snapshotEvents())
  assert.throws(() => assertReceiptSurfaceReady(blank), error => error instanceof ReceiptSurfaceNotReadyError && /receipt write deferred/.test(error.message))
  assert.equal(JSON.stringify(blank.snapshotEvents()), blankBefore)
  const earlyNotice = Session.create(header.id, number([user()]), header, 0)
  assert.throws(() => assertReceiptSurfaceReady(earlyNotice), /receipt write deferred/)
  const readyEvents = number(turn)
  validate(readyEvents)
  const ready = Session.create(header.id, readyEvents, header, 0)
  assert.doesNotThrow(() => assertReceiptSurfaceReady(ready))
  validate(number([...turn, user()]))
  const emptyHead = structuredClone(turn)
  emptyHead[2].data.message = createSystemMessage('')
  assert.doesNotThrow(() => assertReceiptSurfaceReady(Session.create(header.id, number(emptyHead), header, 0)))
  const { Conversations } = await import(conversationOutput)
  let appended = 0, flushed = 0, maintained = 0
  const appSession = Session.create(header.id, [], header, 0)
  appSession.append = () => { appended++; throw new Error('Unexpected append') }
  const agent = { session: appSession, whenIdle: async () => {}, runMaintenance: async job => { maintained++; return job() } }
  const host = {
    ready: Promise.resolve(), load: (_name, fallback) => fallback,
    save: () => {}, log: () => {},
    conversation: async () => ({ sessionId: header.id }),
    conversations: () => ({}),
    ctx: { get: name => name === 'sessionController' ? { resolveAgent: async () => ({ agent }) } : name === 'sessions' ? { flush: async () => { flushed++ } } : undefined },
  }
  const conversations = new Conversations(host, () => ({ people: [], groups: [] }), () => ({}), () => undefined, () => {}, () => ({ kind: 'reply' }))
  const receipt = await conversations.app.send({ kind: 'direct', chat: 'synthetic' }, { text: 'Synthetic app message' }, 'synthetic-delivery')
  assert.equal(receipt.state, 'failed')
  assert.match(receipt.reason, /receipt write deferred/)
  assert.equal(maintained, 1, 'definite non-delivery must not retry the maintenance append')
  assert.equal(appended, 0)
  assert.equal(flushed, 0)
  conversations.close()
  // Real Conversations methods with a committed in-memory event log and a failing
  // persistence boundary. A flush failure does not undo the successful append.
  function persistenceFixture() {
    const events = number(structuredClone(turn))
    const counts = { append: 0, flush: 0, maintenance: 0 }
    const session = {
      get seq() { return events.length },
      surface: { nodes: [2] },
      eventAt: seq => events[Number(seq)],
      append: (type, data, intent) => {
        counts.append++
        const event = { type, data, ...intent, seq: events.length, time: Date.now() }
        events.push(event)
        return event
      },
    }
    const fixtureAgent = { session, whenIdle: async () => {}, runMaintenance: async job => { counts.maintenance++; return job() } }
    const fixtureHost = {
      ...host,
      ctx: { get: name => name === 'sessionController' ? { resolveAgent: async () => ({ agent: fixtureAgent }) } : name === 'sessions' ? {
        flush: async () => { if (++counts.flush === 1) throw new Error('Synthetic persistence failure') },
      } : undefined },
    }
    const fixtureConversations = new Conversations(fixtureHost, () => ({ people: [], groups: [] }), () => ({}), () => undefined, () => {}, () => ({ kind: 'reply' }))
    const notices = id => events.filter(event => event.type === 'user/message' && event.data.source.kind === 'kin-said' && event.data.source.id === id)
    return { conversations: fixtureConversations, events, counts, notices }
  }
  const direct = persistenceFixture()
  await assert.rejects(direct.conversations.say(header.id, 'direct-receipt', 'Synthetic text', 'Synthetic note'), /Synthetic persistence failure/)
  assert.deepEqual(direct.counts, { append: 1, flush: 1, maintenance: 1 })
  assert.equal(direct.notices('direct-receipt').length, 1)
  const directSeq = await direct.conversations.say(header.id, 'direct-receipt', 'Synthetic text', 'Synthetic note')
  assert.equal(directSeq, '5')
  assert.deepEqual(direct.counts, { append: 1, flush: 2, maintenance: 1 })
  assert.equal(direct.notices('direct-receipt').length, 1)
  validate(direct.events)
  direct.conversations.close()

  const appRetry = persistenceFixture()
  const sendApp = () => appRetry.conversations.app.send({ kind: 'direct', chat: 'synthetic' }, { text: 'Synthetic app text' }, 'app-receipt')
  await assert.rejects(sendApp(), /Synthetic persistence failure/)
  assert.deepEqual(appRetry.counts, { append: 1, flush: 1, maintenance: 1 })
  // More than the former 200-event lookup window elapses before reconciliation.
  for (let index = 0; index < 205; index++) {
    const event = structuredClone(user())
    event.data.source.id = `later-synthetic-${index}`
    appRetry.events.push({ ...event, seq: appRetry.events.length })
  }
  const recovered = await sendApp()
  assert.equal(recovered.state, 'accepted')
  assert.equal(recovered.platformId, '5')
  assert.equal(appRetry.notices('app-receipt').length, 1)
  assert.deepEqual(appRetry.counts, { append: 1, flush: 2, maintenance: 1 })
  validate(appRetry.events)
  appRetry.conversations.close()
  console.log('PASS: successful appends survive failed flushes without duplicate notices, including retries after 200 events')
  console.log('PASS: receipts defer until the native system head exists; strict DSH restore accepts the ready path')
} finally {
  rmSync(output, { force: true })
  rmSync(conversationOutput, { force: true })
}
