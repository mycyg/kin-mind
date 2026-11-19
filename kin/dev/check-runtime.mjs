import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { assertObjectJsonSchema } from '@deepseek-ai/dsh-tools'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = root + '/kin/.regression'
await build({
  entryPoints: [
    root + '/kin/src/models.ts',
    root + '/kin/src/render.ts',
    root + '/kin/src/model-output.ts',
    root + '/kin/src/model-budget.ts',
    root + '/kin/src/health/checks.ts',
    root + '/kin/src/steward/create.ts',
    root + '/kin/src/contact/rules.ts',
  ],
  outdir: output, format: 'esm', platform: 'node', bundle: true, packages: 'external',
})
try {
  const { KinModel, probe } = await import(output + '/models.js')
  const { renderContext } = await import(output + '/render.js')
  const { modelOutput, answerText } = await import(output + '/model-output.js')
  const { requestThreshold } = await import(output + '/model-budget.js')
  const { watch } = await import(output + '/health/checks.js')
  const { CREATION_SCHEMA, inspectCreationOutput } = await import(output + '/steward/create.js')
  const { hold } = await import(output + '/contact/rules.js')
  const expected = '你好呀\n\n今天过得怎么样？'
  const envelopes = [
    '🧠\nPrivate test reasoning.\n🪒' + expected,
    '💭 Private test reasoning.\n\n' + expected,
  ]
  async function* streamText(text, split) {
    yield { type: 'block-start', index: 0, blockType: 'text' }
    for (const part of [text.slice(0, split), text.slice(split)]) yield { type: 'text-delta', index: 0, text: part }
    yield { type: 'block-end', index: 0, block: { type: 'text', text } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
  async function checkedText(text, split, expected) {
    const chunks = []
    for await (const chunk of modelOutput(streamText(text, split))) chunks.push(chunk)
    assert.equal(chunks.filter(c => c.type === 'text-delta').map(c => c.text).join(''), expected)
    assert.equal(chunks.find(c => c.type === 'block-end').block.text, expected)
    assert.ok(!JSON.stringify(chunks).includes('Private test reasoning'))
  }
  for (const wrapped of envelopes) {
    assert.equal(answerText(wrapped), expected)
    for (let split = 0; split <= wrapped.length; split++) await checkedText(wrapped, split, expected)
    // The live stream must stay private even when every UTF-16 code unit arrives separately.
    async function* characterStream() {
      for (let i = 0; i < wrapped.length; i++) yield { type: 'text-delta', index: 0, text: wrapped[i] }
      yield { type: 'block-end', index: 0, block: { type: 'text', text: wrapped } }
    }
    const chunks = []
    for await (const chunk of modelOutput(characterStream())) chunks.push(chunk)
    assert.equal(chunks.filter(c => c.type === 'text-delta').map(c => c.text).join(''), expected)
    assert.ok(!JSON.stringify(chunks).includes('Private test reasoning'))
  }
  for (const normal of ['🧠', '💭', '🧠 很聪明', '正文里的🪒是剃刀', '正文里的💭是表情', 'hello']) {
    for (let split = 0; split <= normal.length; split++) await checkedText(normal, split, normal)
  }
  for (const unfinished of ['🧠\nPrivate test reasoning', '💭 Private test reasoning', '💭 Private test reasoning\n']) {
    assert.equal(answerText(unfinished), '')
    for (let split = 0; split <= unfinished.length; split++) await checkedText(unfinished, split, '')
  }
  // Independent text blocks may reuse indices after completion; tool/reasoning blocks pass through unchanged.
  async function* mixedStream() {
    yield* streamText(envelopes[1], 3)
    yield* streamText('ordinary reply', 2)
    yield { type: 'block-end', index: 1, block: { type: 'tool-call', id: 'test', name: 'test_tool', arguments: {} } }
  }
  const mixed = []
  for await (const chunk of modelOutput(mixedStream())) mixed.push(chunk)
  assert.equal(mixed.filter(c => c.type === 'text-delta').map(c => c.text).join(''), expected + 'ordinary reply')
  assert.equal(mixed.at(-1).block.name, 'test_tool')
  let replayed
  const replayModel = new KinModel({ llm: { async *stream(options) {
    replayed = options.messages
    yield* streamText(envelopes[1], 1)
  } } }, () => [{ provider: 'test', model: 'wrapped', effort: 'low' }])
  const user = { role: 'user', content: [{ type: 'text', text: envelopes[1] }] }
  const replayChunks = []
  for await (const chunk of replayModel.stream({ model: 'chat', messages: [user,
    { role: 'assistant', content: [{ type: 'text', text: envelopes[1] }], source: { replayState: { raw: true } } },
  ] })) replayChunks.push(chunk)
  assert.equal(replayed[0], user, 'never rewrite user-authored text')
  assert.equal(replayed[1].content[0].text, expected)
  assert.equal(replayed[1].source.replayState, undefined, 'discard stale provider replay data after cleaning')
  assert.ok(!JSON.stringify(replayChunks).includes('Private test reasoning'))
  console.log('PASS: both relay thinking envelopes stay private at every split, character-by-character and when unfinished; ordinary emoji and tools retained')
  const tried = []
  const ctx = { llm: { async *stream(options) {
    tried.push(options.model)
    if (options.model === 'throws') throw Error('network down')
    if (options.model === 'empty') return
    if (options.model === 'invalid-tool') {
      yield { type: 'block-start', index: 0, blockType: 'reasoning' }
      yield { type: 'reasoning-delta', index: 0, text: 'uncommitted thought' }
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'BAD_JSON', message: 'invalid tool JSON' } } }
      return
    }
    if (options.model === 'finish') {
      yield { type: 'finish', reason: { kind: 'stop' } }
      return
    }
    yield { type: 'text-delta', text: 'ready' }
    yield { type: 'finish', reason: { kind: 'stop' } }
  } } }
  const routes = ['throws', 'empty', 'finish', 'invalid-tool', 'ready'].map(model => ({ provider: 'test', model, effort: 'max' }))
  const model = new KinModel(ctx, () => routes)
  const chunks = []
  for await (const chunk of model.stream({ model: 'chat', messages: [] })) chunks.push(chunk)
  assert.deepEqual(tried, routes.map(route => route.model))
  assert.equal(model.last.chat.answered.model, 'ready')
  assert.equal(model.last.chat.failed.length, 4)
  assert.ok(!JSON.stringify(chunks).includes('uncommitted thought'))
  assert.equal((await probe(ctx, { provider: 'test', model: 'finish' })).ok, false)
  assert.equal((await probe(ctx, { provider: 'test', model: 'ready' })).ok, true)

  const moment = '此刻的状态：很想念用户甲。'
  const shown = renderContext({ moment, rendered_text: '' }, {}, id => id === 'person:person_a' ? '主人' : id)
  assert.match(shown.text, /很想念主人/)
  assert.equal(renderContext({ moment }, {}, () => '主人', { shown: shown.feeling }).text, '')

  // Small-window request threshold remains usable (>= 50%) while preserving large-window reserve behavior.
  assert.equal(requestThreshold(128_000, 128_000), 76_800)
  assert.equal(requestThreshold(200_000, 128_000), 120_000)
  assert.ok(requestThreshold(648_000, 128_000) > 400_000)

  // Health watch suppresses false route-fallback warnings when failed and answered resolved to the same provider/model.
  const sameRouteFindings = await watch({
    mind: { call: async () => ({}), memoryWorker: { pid: process.pid, ends: [] }, modelFile: '' },
    heartbeat: { frozen: true, beats: {}, rounds: new Map() },
    model: {
      last: {
        chat: {
          answered: { provider: 'p1', model: 'm1', effort: 'high' },
          failed: [{ route: { provider: 'p1', model: 'm1', effort: 'low' }, reason: 'retry' }],
        },
        task: undefined,
      },
    },
    config: { dataDir: output },
    ctx: { get: () => undefined },
  })
  assert.equal(sameRouteFindings.some(f => f.key === 'model:chat'), false)

  const fallbackFindings = await watch({
    mind: { call: async () => ({}), memoryWorker: { pid: process.pid, ends: [] }, modelFile: '' },
    heartbeat: { frozen: true, beats: {}, rounds: new Map() },
    model: {
      last: {
        chat: {
          answered: { provider: 'p2', model: 'm2', effort: 'high' },
          failed: [{ route: { provider: 'p1', model: 'm1', effort: 'high' }, reason: 'timeout' }],
        },
        task: undefined,
      },
    },
    config: { dataDir: output },
    ctx: { get: () => undefined },
  })
  assert.equal(fallbackFindings.find(f => f.key === 'model:chat')?.level, 'warn')

  // Creator schema complies with DSH object-root subset and classifies empty artifacts distinctly.
  assertObjectJsonSchema(CREATION_SCHEMA)
  const ws = join(output, 'creator-ws')
  mkdirSync(ws, { recursive: true })
  writeFileSync(join(ws, 'sketch.txt'), '..\n##\n')
  assert.deepEqual(
    inspectCreationOutput({ summary: 'text only', artifacts: [], verification: [], remaining: [] }, ws, 'r1', {}),
    { ok: false, reason: 'empty-artifacts' },
  )
  assert.deepEqual(
    inspectCreationOutput({ artifacts: ['sketch.txt'] }, ws, 'r1', {}),
    { ok: false, reason: 'invalid-result-shape' },
  )
  const validCreated = inspectCreationOutput({ summary: 'cat sketch', artifacts: ['sketch.txt'], verification: [], remaining: [] }, ws, 'r1', {})
  assert.equal(validCreated.ok, true)
  assert.equal(validCreated.result.artifacts[0].relative, 'sketch.txt')

  // Proactive contact gap applies full minGapMinutes between outreach sends and only a 5-min buffer after normal chat replies.
  const gapRules = { enabled: true, quietStart: 0, quietEnd: 0, minGapMinutes: 30, maxPerDay: 10, waitForReply: false, timeZone: 'UTC' }
  const heardAt = '2026-01-01T12:00:00Z'
  assert.equal(hold(gapRules, { heard: heardAt, answered: heardAt }, new Date('2026-01-01T12:04:00Z')), 'just-talked')
  assert.equal(hold(gapRules, { heard: heardAt, answered: heardAt }, new Date('2026-01-01T12:10:00Z')), undefined)
  assert.equal(hold(gapRules, { heard: '2026-01-01T10:00:00Z', reached: [heardAt] }, new Date('2026-01-01T12:10:00Z')), 'just-talked')

  console.log('PASS: fallback before usable output; abandoned reasoning discarded; Core moment retained and deduplicated; budget thresholds, health route, creation schema & contact gap checks verified')
} finally {
  rmSync(output, { recursive: true, force: true })
}
