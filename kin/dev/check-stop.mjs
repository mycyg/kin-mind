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
  await Promise.all(conversations.arrivals.values())
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
  conversations.arrivals.set('owner-chat', new Promise(resolve => { finishArrival = resolve }))
  conversations.flush = async () => { flushed++ }
  conversations.schedule('owner-chat', 1)
  await new Promise(resolve => setTimeout(resolve, 15))
  assert.equal(flushed, 0, 'a burst must not flush while an already-arrived message is still being ingested')
  let finishNextArrival
  const nextArrival = new Promise(resolve => { finishNextArrival = resolve })
  conversations.arrivals.set('owner-chat', conversations.arrivals.get('owner-chat').then(() => nextArrival))
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

  // Paragraphs go out as she writes them, each once; the finished message sends only what is left.
  const out = [], told = [], events = [{ type: 'user/message', data: { source: { kind: 'kin-channel' } } }]
  const said = (turn, step, text) => events.push({ type: 'assistant/message', data: { turn, step, message: { content: [{ type: 'text', text }] } } })
  const session = { id: 'live-chat', get seq() { return events.length }, eventAt: seq => events[seq] }
  let refusing = false
  const liveHost = {
    ...host, timeline: { landed: () => {} }, mind: { call: async () => ({}) },
    ctx: { get: name => name === 'kin' ? { scopeOf: () => ({}) } : name === 'sessionController' ? { resolveAgent: async () => ({ agent: { session } }) } : undefined },
    conversations: () => ({ 'wechat:owner': { sessionId: 'live-chat', channel: 'wechat', recipient: 'person:person_a', address: { chat: 'owner', kind: 'direct' } } }),
  }
  const sentLive = new Deliveries(liveHost, () => ({ send: async (_to, part, id) => { if (refusing) return { state: 'failed', reason: 'refused' }; out.push(`${id} ${part.text}`); return { state: 'accepted', platformId: id } } }), async delivery => sentLive.mark(delivery.id, { remembered: true }))
  const live = new Conversations(liveHost, directory, () => sentLive, () => undefined, () => {}, () => ({ kind: 'reply' }))
  live.say = async (_session, id, text) => { told.push(`${id} ${text}`) }
  const settle = () => new Promise(resolve => setTimeout(resolve, 5))
  const write = (attemptId, text) => live.stream(session, { type: 'chunk', attemptId, chunk: { type: 'text-delta', index: 0, text } })
  live.stream(session, { type: 'start', attemptId: 'a1', turn: 1, step: 1 })
  write('a1', '第一段\n\n第二')
  await settle()
  assert.deepEqual(out, ['reply:live-chat:live:1:0 第一段'], 'a finished paragraph goes before the message is whole; an unfinished one waits')
  write('a1', '段\n\n第三段')
  said(1, 1, '第一段\n\n第二段\n\n第三段')
  live.forward(session)
  await settle()
  assert.deepEqual(out.map(sent => sent.split(' ')[1]), ['第一段', '第二段', '第三段'])
  assert.equal(new Set(out.map(sent => sent.split(':').slice(0, 4).join(':'))).size, 1, 'one message is one delivery')
  live.forwarded['live-chat'] = 0
  live.forward(session)
  await settle()
  assert.equal(out.length, 3, 'catching up after a restart sends nothing twice')
  // An attempt that broke off after a paragraph went out, written again differently: the new message goes whole, the stray words are told to her.
  live.stream(session, { type: 'start', attemptId: 'a2', turn: 1, step: 2 })
  write('a2', '甲\n\n乙')
  await settle()
  live.stream(session, { type: 'start', attemptId: 'a3', turn: 1, step: 2 })
  write('a3', '丙\n\n丁')
  said(1, 2, '丙\n\n丁')
  live.forward(session)
  await settle()
  assert.deepEqual(out.slice(3).map(sent => sent.split(' ')[1]), ['甲', '丙', '丁'])
  assert.deepEqual(told, ['told:reply:live-chat:live:2 甲'])
  // One paragraph is one message, sent when it is whole, under its session position as before.
  live.stream(session, { type: 'start', attemptId: 'a4', turn: 2, step: 1 })
  write('a4', '只有一句')
  said(2, 1, '只有一句')
  live.forward(session)
  live.rest('live-chat')
  await settle()
  assert.deepEqual(out.slice(6), ['reply:live-chat:3:0 只有一句'])
  assert.equal(told.length, 1)
  // A paragraph the platform refused, then the message written again: only the new words go, at once or at a later minute, and nothing is told as said.
  refusing = true
  live.stream(session, { type: 'start', attemptId: 'a5', turn: 3, step: 1 })
  write('a5', '被拒收的旧段\n\n没写完')
  await settle()
  refusing = false
  live.stream(session, { type: 'start', attemptId: 'a6', turn: 3, step: 1 })
  said(3, 1, '重写的整条')
  live.forward(session)
  live.rest('live-chat')
  await settle()
  await sentLive.tick(() => true)
  await settle()
  assert.deepEqual(out.slice(7), ['reply:live-chat:4:0 重写的整条'], 'words taken back never go, not even at a later minute')
  assert.equal(told.length, 1, 'what no platform took is not told to her as said')
  assert.equal(sentLive.get('reply:live-chat:live:4').state, 'cancelled')
  // A paragraph that went out, then a restart before the message formed: told to her once, and nothing is sent again.
  live.stream(session, { type: 'start', attemptId: 'a7', turn: 4, step: 1 })
  write('a7', '发出去的半截\n\n后面')
  await settle()
  const restarted = new Conversations(liveHost, directory, () => sentLive, () => undefined, () => {}, () => ({ kind: 'reply' }))
  restarted.say = async (_session, id, text) => { told.push(`${id} ${text}`) }
  await settle()
  assert.deepEqual(told.slice(1), ['told:reply:live-chat:live:5 发出去的半截'])
  assert.equal(out.length, 9, 'catching up from the start of the session sends nothing twice')
  assert.equal(sentLive.list().filter(delivery => delivery.written && !delivery.written.known).length, 0)
  console.log('PASS: paragraphs go out as she writes them, each once; a message written again takes back what had not gone and tells her what had; a restart tells her the half that went')

  // An arrival her mind could not take in is kept and taken in when it can: the platform does not send it twice.
  let away = true, takenIn = 0
  const keptFiles = {}
  const arrivalHost = {
    ...host, ms: () => 1, load: (name, empty) => keptFiles[name] ?? empty, save: (name, value) => { keptFiles[name] = structuredClone(value) },
    conversation: async () => ({ sessionId: 'owner-chat' }),
    mind: { call: async () => { if (away) throw new Error('mind away'); takenIn++; return { source_id: 'source-1' } } },
  }
  const arriving = new Conversations(arrivalHost, () => ({ people: [{ id: 'person:person_a', name: '甲', accounts: { wechat: 'owner' } }], groups: [] }),
    () => ({ list: () => [] }), () => undefined, () => {}, () => ({}))
  arriving.flush = async () => {}
  arriving.receive({ transport: 'wechat', id: 'in-1', chat: 'owner', sender: 'owner', kind: 'direct', text: 'synthetic hello', at: new Date().toISOString() })
  await Promise.all(arriving.arrivals.values())
  assert.equal(takenIn, 0)
  assert.equal(keptFiles['inbox.json'].arriving.length, 1, 'what could not be taken in is kept')
  away = false
  await new Promise(resolve => setTimeout(resolve, 20))
  await Promise.all(arriving.arrivals.values())
  assert.equal(takenIn, 1)
  assert.equal(keptFiles['inbox.json'].arriving.length, 0)
  assert.equal(keptFiles['inbox.json'].bursts['wechat:owner'].length, 1, 'it joins its conversation as any arrival does')
  arriving.close()
  console.log('PASS: an arrival her mind could not take in is kept and taken in once it can')
} finally {
  rmSync(output, { recursive: true, force: true })
}
