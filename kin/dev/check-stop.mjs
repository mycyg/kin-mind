import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = root + '/kin/.stop-check'
await build({ entryPoints: [root + '/kin/src/contact/conversations.ts', root + '/kin/src/contact/deliveries.ts'], outdir: output, bundle: true, platform: 'node', format: 'esm', packages: 'external' })
try {
  const { Conversations } = await import(output + '/conversations.js')
  const stopped = []
  let cancelled = false
  const steward = {
    list: () => [
      { id: 'mine', session: 'owner-chat', state: 'running' },
      { id: 'finished', session: 'owner-chat', state: 'done' },
      { id: 'theirs', session: 'friend-chat', state: 'running' },
    ],
    stop: id => stopped.push(id),
  }
  const controller = { resolveAgent: async () => ({ agent: { cancel: () => { cancelled = true } } }) }
  const host = {
    ctx: { get: name => name === 'kinSteward' ? steward : name === 'sessionController' ? controller : undefined },
    ready: Promise.resolve(), load: (_name, empty) => empty, save: () => {}, log: () => {},
    conversations: () => ({ 'wechat:owner': { sessionId: 'owner-chat' } }),
  }
  const directory = () => ({ people: [{ id: 'person:person_a', accounts: { wechat: 'owner' } }], groups: [] })
  const conversations = new Conversations(host, directory, () => ({}), () => undefined, () => {}, () => ({}))
  conversations.receive({ transport: 'wechat', id: 'stop-1', chat: 'owner', sender: 'owner', kind: 'direct', text: '停止任务', at: new Date().toISOString() })
  await conversations.arrivals
  assert.equal(cancelled, true)
  assert.deepEqual(stopped, ['mine'])
  // A finished model turn can still have queued output waiting for platform receipts.
  let releaseFirst, releaseSecond
  const firstReceipt = new Promise(resolve => { releaseFirst = resolve })
  const secondReceipt = new Promise(resolve => { releaseSecond = resolve })
  const first = conversations.queue('owner-chat', () => firstReceipt)
  const second = conversations.queue('owner-chat', () => secondReceipt)
  assert.equal(conversations.pending, true)
  releaseFirst()
  await first
  assert.equal(conversations.pending, true, 'the first completion must not hide the queued second send')
  releaseSecond()
  await second
  assert.equal(conversations.pending, false)
  await conversations.queue('owner-chat', async () => { throw new Error('refused') })
  assert.equal(conversations.pending, false)

  host.ms = () => 0
  const { Deliveries } = await import(output + '/deliveries.js')
  let receipt
  const pending = new Promise(resolve => { receipt = resolve })
  const deliveries = new Deliveries(host, () => ({ send: () => pending }), async () => {})
  deliveries.open({ id: 'test', kind: 'reply', key: 'owner-chat', recipient: 'person:person_a', transport: 'test', to: {}, parts: [{ text: 'hello' }] })
  const sent = deliveries.send('test')
  assert.equal(deliveries.sending, true)
  receipt({ state: 'accepted', platformId: 'receipt-1' })
  await sent
  assert.equal(deliveries.sending, false)
  assert.equal(deliveries.get('test').state, 'accepted')
  console.log('PASS: queued and active output remains busy until its platform receipt settles')
  const attempts = [], notices = []
  let recovered = false
  const ledger = {}
  const savedHost = { ...host, load: (name, empty) => structuredClone(ledger[name] ?? empty), save: (name, value) => { ledger[name] = structuredClone(value) } }
  const restore = () => new Deliveries(savedHost, () => ({
    check: async () => ({ state: 'unknown' }),
    send: async (_to, part, id, retry) => {
      attempts.push({ id, retry })
      return part.text === 'first' || recovered ? { state: 'accepted', platformId: id } : { state: 'unknown' }
    },
  }), async delivery => { if (delivery.state === 'needs-review') notices.push(delivery.id); uncertain.mark(delivery.id, { remembered: true }) })
  let uncertain = restore()
  uncertain.open({ id: 'retry', kind: 'reply', key: 'owner-chat', recipient: 'person:person_a', transport: 'test', to: {}, parts: [{ text: 'first' }, { text: 'second' }] })
  await uncertain.send('retry')
  assert.equal(uncertain.get('retry').state, 'unknown')
  assert.equal(notices.length, 0)
  await uncertain.tick(() => true)
  await uncertain.tick(() => true)
  assert.equal(notices.length, 0)
  uncertain = restore() // Restart after two attempts: the budget and accepted first part survive.
  await uncertain.tick(() => true)
  assert.equal(uncertain.get('retry').state, 'needs-review')
  assert.equal(attempts.filter(x => x.id === 'retry:0').length, 1)
  assert.equal(attempts.filter(x => x.id === 'retry:1' && x.retry).length, 3)
  assert.deepEqual(notices, ['retry'])
  await uncertain.tick(() => true)
  assert.equal(attempts.length, 5)
  assert.deepEqual(notices, ['retry'])
  uncertain.open({ id: 'recover', kind: 'reply', key: 'owner-chat', recipient: 'person:person_a', transport: 'test', to: {}, parts: [{ text: 'second' }, { text: 'last' }] })
  await uncertain.send('recover')
  recovered = true
  await uncertain.tick(() => true)
  assert.equal(uncertain.get('recover').state, 'accepted')
  assert.equal(attempts.filter(x => x.id === 'recover:0').length, 2)
  assert.equal(attempts.filter(x => x.id === 'recover:1').length, 1)
  console.log('PASS: unknown receipts retry the same part three times; confirmed parts do not repeat; recovery continues the remainder')
  conversations.close()
  console.log('PASS: channel stop ends the current conversation and its running workers, leaving other sessions alone')
} finally {
  rmSync(output, { recursive: true, force: true })
}
