import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = root + '/kin/.stop-check'
await build({ entryPoints: [root + '/kin/src/contact/conversations.ts', root + '/kin/src/contact/deliveries.ts', root + '/kin/src/contact/rules.ts', root + '/kin/src/contact/proactive.ts'], outdir: output, bundle: true, platform: 'node', format: 'esm', packages: 'external' })
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
  const { reached } = await import(output + '/rules.js')
  const { Reaching } = await import(output + '/proactive.js')
  const receiptAt = new Date(Date.now() - 3_600_000).toISOString()
  const recorded = {}, failures = []
  let repaired = false, sentCount = 0, candidateChecks = 0
  const recoveryHost = {
    ...host, log: text => failures.push(text), conversations: () => ({}),
    mind: { call: async action => { if (action === 'candidate') { candidateChecks++; return { eligible: false, reason: 'nothing-ready' } } return {} } },
    load: (name, empty) => structuredClone(recorded[name] ?? empty),
    save: (name, value) => { recorded[name] = structuredClone(value) },
  }
  const activity = () => recorded['activity.json'] ?? {}
  let recovery
  const restored = () => new Deliveries(recoveryHost, () => ({ send: async () => { sentCount++; return { state: 'accepted', platformId: 'safe-receipt' } } }), async delivery => {
    const change = reached(activity(), delivery.id, delivery.acceptedAt ?? delivery.at)
    if (change) recoveryHost.save('activity.json', { ...activity(), ...change })
    if (delivery.id === 'broken-session' && !repaired) throw new Error('conversation cannot be opened')
    recovery.mark(delivery.id, { remembered: true })
  })
  recovery = restored()
  for (const id of ['broken-session', 'other-person']) {
    recovery.open({ id, kind: 'contact', key: id, recipient: 'person:person_a', transport: 'test', to: {}, parts: [{ text: 'synthetic message' }] })
    recovery.mark(id, { state: 'accepted', at: receiptAt, acceptedAt: receiptAt, sent: [{ state: 'accepted', platformId: id }], remembered: false })
  }
  await recovery.tick(() => true)
  assert.equal(recovery.get('broken-session').state, 'accepted')
  assert.equal(recovery.get('broken-session').remembered, false)
  assert.equal(recovery.get('broken-session').settlementError, 'conversation cannot be opened')
  assert.equal(recovery.get('other-person').remembered, true, 'one failed settlement must not block the next person')
  assert.equal(activity().reached.length, 2, 'distinct delivery ids count separately even at the same timestamp')
  recovery = restored()
  await recovery.tick(() => true)
  assert.equal(activity().reached.length, 2, 'a restarted result retry cannot count the same contact again')
  assert.deepEqual(activity().reached, [receiptAt, receiptAt], 'old receipts must not look like contacts made now')
  assert.equal(failures.length, 1, 'an unchanged error remains visible without logging every minute')
  const reaching = new Reaching(recoveryHost,
    () => ({ people: [{ id: 'person:person_a', proactive: true }], groups: [] }),
    () => ({ enabled: true, quietStart: 0, quietEnd: 0, minGapMinutes: 0, maxPerDay: 3, waitForReply: false, timeZone: 'UTC' }),
    () => ({ heard: receiptAt }), () => {}, recovery, {})
  assert.equal((await reaching.round()).reason, 'nothing-ready')
  assert.equal(candidateChecks, 1, 'a failed receipt writeback must not skip the owner contact decision')
  repaired = true
  await recovery.tick(() => true)
  assert.equal(recovery.get('broken-session').remembered, true)
  assert.equal(recovery.get('broken-session').settlementError, undefined)
  assert.equal(activity().reached.length, 2)
  assert.equal(sentCount, 0, 'accepted platform messages must never be resent while repairing their records')
  let liveSends = 0
  const liveFailure = new Deliveries({ ...host, load: (_name, empty) => empty }, () => ({
    send: async () => { liveSends++; return { state: 'accepted', platformId: 'live-once' } },
  }), async () => { throw new Error('recording unavailable') })
  liveFailure.open({ id: 'live-failure', kind: 'contact', key: 'owner-chat', recipient: 'person:person_a', transport: 'test', to: {}, parts: [{ text: 'synthetic send' }] })
  const acceptedDespiteRecording = await liveFailure.send('live-failure')
  assert.equal(acceptedDespiteRecording.state, 'accepted')
  assert.equal(acceptedDespiteRecording.settlementError, 'recording unavailable')
  const acceptedAt = acceptedDespiteRecording.acceptedAt
  await liveFailure.tick(() => true)
  assert.equal(liveFailure.get('live-failure').acceptedAt, acceptedAt)
  assert.equal(liveSends, 1)
  let releaseRecording, beganRecording
  const startedRecording = new Promise(resolve => { beganRecording = resolve })
  const recordingDone = new Promise(resolve => { releaseRecording = resolve })
  let recordings = 0
  const concurrent = new Deliveries(recoveryHost, () => ({ send: async () => ({ state: 'accepted', platformId: 'once' }) }), async delivery => {
    recordings++; beganRecording(); await recordingDone
    concurrent.mark(delivery.id, { remembered: true })
  })
  concurrent.open({ id: 'concurrent', kind: 'contact', key: 'owner-chat', recipient: 'person:person_a', transport: 'test', to: {}, parts: [{ text: 'hello' }] })
  const outgoing = concurrent.send('concurrent')
  await startedRecording
  const minute = concurrent.tick(() => true)
  releaseRecording()
  await Promise.all([outgoing, minute])
  assert.equal(recordings, 1, 'a minute during receipt recording must join the in-flight settlement')
  assert.ok(concurrent.get('concurrent').acceptedAt)
  console.log('PASS: failed recording is isolated, retries count each delivery once across restart, owner decisions continue, and accepted messages never resend')
  let finishArrival, flushed = 0
  conversations.arrivals = new Promise(resolve => { finishArrival = resolve })
  conversations.flush = async () => { flushed++ }
  conversations.schedule('owner-chat', 1)
  await new Promise(resolve => setTimeout(resolve, 15))
  assert.equal(flushed, 0, 'a burst must not flush while an already-arrived message is still being ingested')
  let finishNextArrival
  const nextArrival = new Promise(resolve => { finishNextArrival = resolve })
  conversations.arrivals = conversations.arrivals.then(() => nextArrival)
  finishArrival()
  await new Promise(resolve => setTimeout(resolve, 15))
  assert.equal(flushed, 0, 'arrivals queued while waiting must finish too')
  conversations.schedule('owner-chat', 1)
  finishNextArrival()
  await new Promise(resolve => setTimeout(resolve, 15))
  assert.equal(flushed, 1, 'only the latest timer may flush the combined burst')
  console.log('PASS: slow ingestion cannot split a burst; superseded timers do not flush')
  conversations.close()
  console.log('PASS: channel stop ends the current conversation and its running workers, leaving other sessions alone')
} finally {
  rmSync(output, { recursive: true, force: true })
}
