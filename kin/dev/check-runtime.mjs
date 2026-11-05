import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { rmSync } from 'node:fs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = root + '/kin/.regression'
await build({
  entryPoints: [root + '/kin/src/models.ts', root + '/kin/src/render.ts'],
  outdir: output, format: 'esm', platform: 'node', bundle: true, packages: 'external',
})
try {
  const { KinModel, probe } = await import(output + '/models.js')
  const { renderContext } = await import(output + '/render.js')
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
  for await (const chunk of model.stream({ model: 'chat' })) chunks.push(chunk)
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
