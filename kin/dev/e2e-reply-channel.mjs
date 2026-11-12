// Real DSH/Core with an isolated fake channel. Never uses the user's profile, data or accounts.
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { bootDsh, stopDsh, signIn, repo, kinDir } from './env.mjs'
const out = join(repo, '.dev/e2e-reply-channel/report.json'), dataDir = join(repo, '.dev/e2e-reply-channel/kin-data')
const runtime = await bootDsh({ name: 'e2e-reply-channel', pace: 1, kinFiles: {
  'models.json': { chat: [{ provider: 'reply-test', model: 'chat' }], task: [{ provider: 'kin-test', model: 'scripted' }] },
  'people.json': { people: [{ id: 'person:person_a', name: '小光', proactive: false, accounts: { wechat: 'synthetic-owner' } }], groups: [] },
  'contact.json': { quietStart: 0, quietEnd: 0, waitForReply: false, minGapMinutes: 0, maxPerDay: 10 },
}, extraRows: `- insert:\n    - id: reply-channel-test\n      name: ${JSON.stringify(join(kinDir, 'dev/scenario-reply-channel.mjs'))}\n      config:\n        out: ${JSON.stringify(out)}\n        dataDir: ${JSON.stringify(dataDir)}\n` })
try {
  const api = await signIn(await runtime.url); await api.post('/api/kin/health/freeze', { frozen: true })
  for (const end = Date.now() + 180000; !existsSync(out) && Date.now() < end; await new Promise(r => setTimeout(r, 500))) {}
  assert.ok(existsSync(out), 'scenario did not complete')
  const report = JSON.parse(readFileSync(out, 'utf8')); assert.ok(!report.error, report.error)
  const checks = {
    'public text follows note persistence through real Core and the channel ledger': report.checks.initial && report.deliveryAccepted === 3,
    'the next model request receives the saved mood once, including native tool continuations': report.noteContexts[1]?.some(c => c.text.includes('先留在心里'))
      && report.noteContexts.some(rows => rows.some(c => c.text.includes('我把这一小点在意记住了。')))
      && report.noteContexts.every(rows => rows.flatMap(c => c.ids).length === new Set(rows.flatMap(c => c.ids)).size),
    'actual system prompt declares the JSON contract': report.requestContract,
    'ordinary analysis and late terminal errors never reach the channel': ['invalid', 'late-error'].every(k => report[k].sent.length === 0 && report[k].publicText.length === 0),
    'silent completes without sending a bubble': report.silent.sent.length === 0 && report.silent.ended.includes('completed'),
    'note persistence failure cannot regenerate or execute another route': report['store-failure'].sent.length === 0 && report['store-failure'].calls === 1 && report['store-failure'].ended.includes('error'),
    'parallel native tools complete and a second model step responds once': report.tools.tools.length === 2 && report.tools.calls === 2 && report.tools.sent.length === 1,
    'invalid Core arguments return a tool error before calling Core, without breaking the next step': report.coreInvalidCalls === 0 && report['tool-error'].calls === 2 && report['tool-error'].sent.length === 1,
    'only selected bubbles enter streamed and durable public text': !JSON.stringify([report.sent, report.transient, report.assistantText]).match(/private_note|Thinking Process|Synthetic native reasoning|先留在心里|Must not publish/),
  }
  for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
  if (!Object.values(checks).every(Boolean)) process.exitCode = 1
} finally { await stopDsh(runtime) }
