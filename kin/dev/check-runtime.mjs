import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { rmSync } from 'node:fs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = root + '/kin/.regression'
await build({
  entryPoints: [root + '/kin/src/models.ts', root + '/kin/src/render.ts', root + '/kin/src/model-output.ts'],
  outdir: output, format: 'esm', platform: 'node', bundle: true, packages: 'external',
})
try {
  const { KinModel, probe } = await import(output + '/models.js')
  const { renderContext } = await import(output + '/render.js')
  const { modelOutput, answerText } = await import(output + '/model-output.js')
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
  console.log('PASS: fallback before usable output; abandoned reasoning discarded; Core moment retained and deduplicated')
} finally {
  rmSync(output, { recursive: true, force: true })
}
