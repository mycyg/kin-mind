// Explicit synthetic envelopes through the real KIN output boundary. No API credentials or business tools.
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { BlockAssembler } from '@deepseek-ai/dsh-llm'
const dir = mkdtempSync(resolve('.reply-regression-'))
await build({ stdin: { contents: "export * from './reply-output.ts'; export * from './models.ts'", resolveDir: resolve('src') }, outfile: join(dir, 'bundle.mjs'), bundle: true, packages: 'external', format: 'esm', platform: 'node' })
const { parseReply, replyOutput, KinModel } = await import(join(dir, 'bundle.mjs'))
let failed = 0
const check = async (name, test) => { try { await test(); console.log('✓ ' + name) } catch (e) { failed++; console.error('✗ ' + name + '\n' + e.stack) } }
const envelope = (note = '我有点想笑，先把这份开心留着。', texts = ['来啦。', '一起看看。']) => JSON.stringify({ private_note: note, action: texts.length ? 'send' : 'silent', bubbles: texts.map(text => ({ text })) })
const text = value => [{ type: 'text-delta', index: 0, text: value }, { type: 'block-end', index: 0, block: { type: 'text', text: value } }]
const tool = (id = 'call_test', index = 1) => [{ type: 'block-end', index, block: { type: 'tool-call', id, name: 'lookup', arguments: '{"query":"synthetic"}' } }]
const stop = { type: 'finish', reason: { kind: 'stop' } }
const toolStop = { type: 'finish', reason: { kind: 'tool-calls' } }
const source = async function* (chunks) { yield* chunks }
async function collect(chunks, options = {}) {
  const output = [], notes = []; let empty
  for await (const c of replyOutput(source(chunks), { save: async (...args) => notes.push(args), accepted: value => { empty = value }, ...options })) output.push(c)
  const a = new BlockAssembler(); for (const c of output) a.push(c)
  return { output, notes, empty, blocks: a.blocks(), replay: a.replayState, finish: a.finish }
}
try {
  await check('strict objects reject missing fields, strings, extra bubble fields and text outside JSON', async () => {
    assert.equal(parseReply(envelope()).bubbles[0].text, '来啦。')
    for (const value of [envelope() + '\n旁白', 'Thinking Process\n' + envelope(), '```json\n' + envelope() + '\n```',
      envelope(''), '{}', '{"action":"send","bubbles":[{"text":"公开"}]}',
      '{"private_note":"","action":"send","bubbles":["公开"]}',
      '{"private_note":"","action":"send","bubbles":[{"text":"公开","note":"私密"}]}',
      '{"private_note":"","action":"silent","bubbles":[{"text":"公开"}]}',
      '{"private_note":"","action":"send","bubbles":[]}',
      '{"private_note":"","action":"send","bubbles":[{"text":"  "}]}',
      envelope().replace('"private_note":', '"unexpected":')]) assert.throws(() => parseReply(value), e => e.code === 'INVALID_REPLY_ENVELOPE')
  })
  await check('each bubble validates independently while newlines, quotes and emoji survive projection', async () => {
    const bubbles = ['第一行\n\n第二行「引号」🧠', '她说："好呀" 💭\n再见。']
    const result = await collect([...text(envelope('我有点舍不得。', bubbles)), stop])
    assert.equal(result.blocks[0].text, bubbles.join('\n\n'))
    const bad = JSON.parse(envelope()); bad.bubbles.push({ text: '公开', private_note: '不能绕过' })
    await assert.rejects(collect([...text(JSON.stringify(bad)), stop]), e => e.code === 'INVALID_REPLY_ENVELOPE')
  })
  await check('nothing is exposed until complete validation and confirmed note persistence', async () => {
    let terminal = false, saved = false, outputs = 0
    const body = envelope()
    async function* chunks() {
      for (const c of body) { assert.equal(outputs, 0); yield { type: 'text-delta', index: 0, text: c } }
      yield { type: 'block-end', index: 0, block: { type: 'text', text: body } }
      terminal = true; yield stop
    }
    const seen = []
    for await (const c of replyOutput(chunks(), { save: async (note, reply) => { assert.ok(terminal); assert.equal(outputs, 0); assert.equal(note, JSON.parse(body).private_note); assert.equal(reply, '来啦。\n\n一起看看。'); saved = true }, accepted() {} })) {
      assert.ok(saved); outputs++; seen.push(c)
    }
    assert.ok(!JSON.stringify(seen).includes('private_note')); assert.ok(!JSON.stringify(seen).includes('先把这份开心'))
    assert.deepEqual(seen.filter(c => c.type === 'text-delta').map(c => c.text), ['来啦。\n\n一起看看。'])
  })
  await check('truncation, terminal error, missing finish and late chunks expose no text or tool', async () => {
    for (const chunks of [[...text(envelope()), ...tool(), { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', message: 'synthetic error' } } }],
      [...text(envelope()), { type: 'finish', reason: { kind: 'max-tokens' } }], [...text(envelope())], [...text(envelope()), stop, ...tool()],
      [...text(envelope().slice(0, -1)), stop]]) {
      const visible = []; let saves = 0
      try { for await (const c of replyOutput(source(chunks), { save: async () => { saves++ }, accepted() {} })) visible.push(c) } catch (e) { assert.equal(e.code, 'INVALID_REPLY_ENVELOPE') }
      assert.ok(visible.every(c => c.type === 'finish')); assert.equal(saves, 0)
    }
  })
  await check('pure and parallel tools remain unchanged; narration needs the same envelope', async () => {
    const pure = await collect([...tool('one', 0), ...tool('two', 1), toolStop])
    assert.deepEqual(pure.blocks.map(b => b.id), ['one', 'two']); assert.equal(pure.notes.length, 0)
    const mixed = await collect([...text(envelope('我有点好奇。', ['我去查一下。'])), ...tool(), toolStop])
    assert.equal(mixed.blocks[0].text, '我去查一下。'); assert.equal(mixed.blocks[1].arguments, '{"query":"synthetic"}')
    await assert.rejects(collect([...text('我要分析并调用工具。'), ...tool(), toolStop]), e => e.code === 'INVALID_REPLY_ENVELOPE')
    await assert.rejects(collect([...tool('duplicate', 0), ...tool('duplicate', 1), toolStop]), e => e.code === 'INVALID_REPLY_ENVELOPE')
  })
  await check('genuine reasoning and native tool signatures stay aligned; private JSON text signatures do not', async () => {
    const chunks = [{ type: 'block-end', index: 0, block: { type: 'reasoning', text: 'Synthetic native reasoning.' } },
      { type: 'block-end', index: 1, block: { type: 'text', text: envelope('我有点开心。', ['公开答复']) } }, ...tool('signed', 2),
      { ...toolStop, replayState: { response: { kind: 'pi-ai', version: 2, api: 'anthropic-messages', provider: 'test', model: 'test', stopReason: 'toolUse' },
        blocks: [{ type: 'reasoning', thinkingSignature: 'genuine-test-signature' }, { type: 'text', textSignature: 'signature-of-private-json' }, { type: 'tool-call', thoughtSignature: 'genuine-tool-signature' }] } }]
    const result = await collect(chunks)
    assert.deepEqual(result.blocks.map(b => b.type), ['reasoning', 'text', 'tool-call'])
    assert.deepEqual(result.replay.blocks, [chunks.at(-1).replayState.blocks[0], { type: 'text' }, chunks.at(-1).replayState.blocks[2]])
    assert.equal(result.notes.length, 1); assert.equal(result.notes[0][0], '我有点开心。', 'reasoning is never stored as a mood note')
  })
  await check('silent is an explicit success; cancellation or storage failure releases nothing', async () => {
    const silent = await collect([...text(envelope('心里暖暖的。', [])), stop]); assert.equal(silent.empty, true); assert.equal(silent.blocks.length, 0); assert.equal(silent.notes.length, 1)
    for (const cancel of [false, true]) {
      const controller = new AbortController(); const out = []
      await assert.rejects(async () => {
        for await (const c of replyOutput(source([...text(envelope()), ...tool(), toolStop]), { signal: controller.signal, accepted() {}, save: async () => { if (cancel) controller.abort(); else throw Error('synthetic store failure') } })) out.push(c)
      }, e => e.code === (cancel ? 'ABORTED' : 'REPLY_NOTE_SAVE_FAILED'))
      assert.equal(out.length, 0)
    }
  })
  await check('one same-route repair sends validation feedback, keeps completed tools and never replays rejected calls', async () => {
    const requests = [], notes = []
    const completed = { role: 'tool', toolCallId: 'already-done', name: 'lookup', content: [{ type: 'text', text: 'Synthetic prior result' }] }
    const ctx = { kin: { scopeOf: () => ({}), mind: { call: async (_action, data) => { notes.push(data); return { state: 'saved' } } } }, llm: {
      resolveModelInfo: async (provider, id) => ({ provider, id }), async *stream(request) {
        requests.push(request)
        if (requests.length === 1) { yield* text('Synthetic invalid prose'); yield* tool('must-not-execute'); yield toolStop }
        else { yield* text(envelope()); yield stop }
      },
    } }
    const model = new KinModel(ctx, () => [{ provider: 'test', model: 'first', effort: 'off' }, { provider: 'test', model: 'fallback', effort: 'off' }])
    model.replyTargets.set('synthetic', { id: 'note:repair:1:2', sessionId: 'synthetic' })
    const output = []
    for await (const chunk of model.stream({ provider: 'kin', model: 'chat', sessionId: 'synthetic', messages: [completed] })) output.push(chunk)
    assert.equal(requests.length, 2); assert.ok(requests.every(r => r.model === 'first'))
    assert.deepEqual(requests[1].messages[0], completed)
    assert.match(requests[1].messages.at(-1).content[0].text, /必须是完整 JSON/)
    assert.match(requests[1].messages.at(-1).content[0].text, /不要重做历史中已经完成的工具/)
    assert.ok(!JSON.stringify(requests[1]).includes('Synthetic invalid prose'))
    assert.ok(!JSON.stringify(output).includes('must-not-execute')); assert.equal(notes.length, 1)
    assert.equal(model.last.chat.failed.length, 1); assert.match(model.last.chat.failed[0].reason, /INVALID_REPLY_ENVELOPE/)
  })
  await check('KIN fails over invalid output before exposure but never regenerates after a storage failure', async () => {
    for (const failSave of [false, true]) {
      const calls = [], saved = new Map(); let writes = 0
      const ctx = { kin: { scopeOf: () => ({ channel: 'app', conversation_key: 'synthetic', recipient_person_id: 'synthetic' }), mind: { call: async (action, data) => {
        assert.equal(action, 'private-note-save'); if (failSave) throw Error('storage offline')
        if (!saved.has(data.id)) { saved.set(data.id, data); writes++ } else assert.equal(saved.get(data.id).text, data.text)
        return { state: 'saved' }
      } } }, llm: { resolveModelInfo: async (provider, id) => ({ provider, id }), async *stream(request) {
        calls.push(request.model)
        yield* text(!failSave && request.model === 'bad' ? 'Thinking Process: private synthetic analysis' : envelope()); yield stop
      } } }
      const model = new KinModel(ctx, () => [{ provider: 'test', model: failSave ? 'first' : 'bad', effort: 'off' }, { provider: 'test', model: 'good', effort: 'off' }])
      model.replyTargets.set('synthetic', { id: 'note:synthetic:1:1', sessionId: 'synthetic', inputId: 'input-one' })
      const request = { provider: 'kin', model: 'chat', sessionId: 'synthetic', messages: [] }
      const out = []
      if (failSave) {
        await assert.rejects(async () => { for await (const c of model.stream(request)) out.push(c) }, e => e.code === 'REPLY_NOTE_SAVE_FAILED')
        assert.equal(calls.length, 1); assert.equal(out.length, 0); assert.equal(model.last.chat.answered, null)
      } else {
        for await (const c of model.stream(request)) out.push(c)
        assert.deepEqual(calls, ['bad', 'bad', 'good']); assert.equal(model.last.chat.answered.model, 'good')
        assert.ok(!JSON.stringify(out).includes('private synthetic analysis'))
        for await (const _ of model.stream(request)) {}
        assert.equal(writes, 1); assert.equal(saved.get('note:synthetic:1:1').input_id, 'input-one')
      }
    }
  })
} finally { rmSync(dir, { recursive: true, force: true }) }
if (failed) process.exitCode = 1
