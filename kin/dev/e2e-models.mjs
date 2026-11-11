// Request-boundary regressions through KIN, then real DSH adapters over a local provider. No live credentials or user data.
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { dirname, resolve, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'node:http'
import { Context } from '@deepseek-ai/cordis'
import LlmRuntime, { BlockAssembler, createAssistantMessage, createToolResultMessage, createUserMessage, ToolCallId } from '@deepseek-ai/dsh-llm'
import * as PiAi from '@deepseek-ai/dsh-llm-pi-ai'
import { DeepSeekAdapter, resolveAdapterOptions } from '@deepseek-ai/dsh-llm-deepseek'

const kin = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = mkdtempSync(join(kin, '.model-regression-'))
await build({ entryPoints: [process.env.KIN_MODEL_SOURCE || join(kin, 'src/models.ts')], outfile: join(output, 'models.js'), format: 'esm', platform: 'node', bundle: true, packages: 'external' })
const { KinModel } = await import(join(output, 'models.js'))
let failures = 0
async function check(name, run) {
  try { await run(); console.log('✓ ' + name) } catch (error) { failures++; console.error('✗ ' + name + '\n' + error.stack) }
}
const route = (model, provider = 'kin-relay') => ({ provider, model, effort: model.startsWith('deepseek') ? 'max' : 'medium' })
const user = (text = 'Continue the task.') => createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } })
const assistant = (content, response, source = { provider: 'kin', model: 'task' }) => createAssistantMessage({
  content, source: { ...source, ...(response ? { replayState: { response, blocks: content.map(({ type }) => ({ type })) } } : {}) },
})
const call = (id, name = 'lookup', args = '{"query":"synthetic"}') => ({ type: 'tool-call', id: ToolCallId(id), name, arguments: args })
const result = (id, text = 'Synthetic result', isError = false) => createToolResultMessage({ callId: ToolCallId(id), content: [{ type: 'text', text }], isError })
const codexResponse = { kind: 'pi-ai', version: 2, api: 'openai-codex-responses', provider: 'openai-codex', model: 'gpt-test', stopReason: 'toolUse' }
const tools = [{ name: 'lookup', description: 'Synthetic read-only lookup', parameters: {
  type: 'object', properties: { query: { anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }], default: null },
    mixed: { anyOf: [{ type: 'object' }, { type: 'string' }, { type: 'null' }] },
    nested: { type: 'array', items: { anyOf: [{ type: 'integer', minimum: 0 }, { type: 'null' }] } } }, required: [],
} }]
async function capture(routes, options, reject = () => false) {
  const requests = []
  const model = new KinModel({ llm: { async *stream(request) {
    requests.push(request)
    if (reject(request)) throw Error('synthetic network reset')
    yield { type: 'text-delta', index: 0, text: 'ok' }
    yield { type: 'finish', reason: { kind: 'stop' } }
  } } }, () => routes)
  for await (const _ of model.stream({ provider: 'kin', model: 'task', ...options })) { /* drive real fallback */ }
  return { requests, model }
}
try {
  await check('chat waits 30s for a token, not a block header; thinking and hidden envelope tokens cancel that deadline', async () => {
    const set = globalThis.setTimeout, clear = globalThis.clearTimeout
    const scheduled = new Map()
    globalThis.setTimeout = (fn, ms, ...args) => {
      if (ms !== 30000 && ms !== 60000) return set(fn, ms, ...args)
      const handle = { ms, fn }; scheduled.set(handle, handle); return handle
    }
    globalThis.clearTimeout = handle => { if (!scheduled.delete(handle)) clear(handle) }
    try {
      for (const thinking of [true, false]) {
        let calls = 0
        const model = new KinModel({ llm: { async *stream(request) {
          calls++
          assert.equal([...scheduled.values()][0].ms, 30000)
          yield { type: 'block-start', index: 0, blockType: thinking ? 'reasoning' : 'text' }
          assert.equal(scheduled.size, 1, 'headers are not tokens')
          yield { type: thinking ? 'reasoning-delta' : 'text-delta', index: 0, text: thinking ? 'thinking' : '🧠\nreasoning' }
          assert.equal(scheduled.size, 0, 'reasoning must not trigger a no-first-token switch')
          yield { type: 'text-delta', index: thinking ? 1 : 0, text: thinking ? 'answer' : '🪒answer' }
          yield { type: 'finish', reason: { kind: 'stop' } }
        } } }, () => [route('first'), route('second')])
        for await (const _ of model.stream({ provider: 'kin', model: 'chat', messages: [user()] })) {}
        assert.equal(calls, 1)
        assert.equal(model.last.chat.answered.model, 'first')
      }
      let calls = 0
      const timeout = new KinModel({ llm: { async *stream(request) {
        if (++calls === 1) {
          const timer = [...scheduled.values()][0]; assert.equal(timer.ms, 30000); timer.fn()
          assert.equal(request.signal.aborted, true); throw Error('aborted')
        }
        yield { type: 'text-delta', index: 0, text: 'ok' }
        yield { type: 'finish', reason: { kind: 'stop' } }
      } } }, () => [route('first'), route('second')])
      for await (const _ of timeout.stream({ provider: 'kin', model: 'chat', messages: [user()] })) {}
      assert.equal(calls, 2)
      assert.match(timeout.last.chat.failed[0].reason, /30 秒未收到首个 token/)
      const error = await capture([route('first'), route('second')], { messages: [user()] }, r => r.model === 'first')
      assert.match(error.model.last.task.failed[0].reason, /synthetic network reset/)
      assert.ok(!error.model.last.task.failed[0].reason.includes('秒'))
    } finally { globalThis.setTimeout = set; globalThis.clearTimeout = clear }
  })
  await check('Gemini normalizes nested nullable schemas and tool updates, preserving mixed unions and durable definitions', async () => {
    const before = JSON.stringify(tools), message = user()
    const { requests: [sent] } = await capture([route('gemini-test')], { messages: [message], tools,
      toolHistory: { tools, updates: [{ messageId: 'synthetic-update', additions: tools }] } })
    assert.deepEqual(sent.tools[0].parameters.properties.query, { type: 'string', minLength: 1, default: null, nullable: true })
    assert.equal(sent.tools[0].parameters.properties.nested.items.type, 'integer')
    assert.equal(sent.tools[0].parameters.properties.nested.items.nullable, true)
    assert.deepEqual(sent.tools[0].parameters.properties.mixed, tools[0].parameters.properties.mixed)
    assert.deepEqual(sent.toolHistory.updates[0].additions, sent.tools)
    assert.equal(sent.messages[0], message)
    assert.equal(JSON.stringify(tools), before)
    const other = await capture([route('other')], { messages: [message], tools })
    assert.equal(other.requests[0].tools, tools)
  })
  await check('Claude remaps complete call/result ids consistently, without collisions or stored-history edits', async () => {
    const history = [user(), assistant([call('call_alpha|fc_a'), call('call_beta'), call('toolu_kin_1')], codexResponse),
      result('call_alpha|fc_a'), result('call_beta', 'failed as reported', true), result('toolu_kin_1')]
    const before = JSON.stringify(history)
    const { requests: [sent] } = await capture([route('claude-test')], { messages: history })
    const ids = sent.messages[1].content.map(b => b.id)
    assert.equal(new Set(ids).size, 3)
    assert.ok(ids.every(id => /^toolu_[a-zA-Z0-9_-]+$/.test(id)))
    assert.equal(ids[2], 'toolu_kin_1')
    for (const [i, message] of sent.messages.slice(2).entries()) {
      assert.equal(message.toolCallId, ids[i]); assert.equal(message.source.callId, ids[i])
    }
    assert.equal(sent.messages[3].isError, true)
    assert.equal(JSON.stringify(history), before)
  })
  await check('network fallback preserves effort and exact completed foreign tool records, without fabricated thinking', async () => {
    const first = user(), last = user('New instruction after results')
    const history = [first, assistant([call('call_one'), call('call_two')], codexResponse), result('call_one'), result('call_two', 'failure data', true), last]
    const before = JSON.stringify(history)
    const { requests, model } = await capture([route('gpt-test', 'openai-codex'), route('deepseek-test', 'deepseek-official')], { messages: history }, r => r.provider === 'openai-codex')
    assert.equal(model.last.task.answered.provider, 'deepseek-official')
    const sent = requests.at(-1)
    assert.equal(sent.reasoningEffort, 'max')
    assert.equal(sent.messages[0], first); assert.equal(sent.messages.at(-1), last)
    const record = sent.messages.find(m => m.role === 'user' && m.content[0]?.text?.startsWith('Historical tool execution record'))
    assert.ok(record)
    const data = JSON.parse(record.content[0].text.split('\n').slice(1).join('\n'))
    assert.deepEqual(data.calls, history[1].content.map(({ id, name, arguments: args }) => ({ id, name, arguments: args })))
    assert.deepEqual(data.results.map(r => r.isError), [false, true])
    assert.equal(data.results[1].content[0].text, 'failure data')
    assert.ok(!sent.messages.some(m => m.content.some(b => b.type === 'reasoning')))
    assert.equal(JSON.stringify(history), before)
    const repeated = await capture([route('deepseek-test', 'deepseek-official')], { messages: history })
    assert.deepEqual(repeated.requests[0].messages, sent.messages)
  })
  await check('incomplete, duplicate, or interleaved tool results never become completed execution records', async () => {
    for (const history of [
      [assistant([call('a'), call('b')], codexResponse), result('a')],
      [assistant([call('a'), call('a')], codexResponse), result('a'), result('a')],
      [assistant([call('a')], codexResponse), user('interrupted'), result('a')],
      [assistant([call('a')], codexResponse), result('a'), result('a')],
      [assistant([call('a')], codexResponse), result('a'), result('unmatched')],
    ]) {
      const { requests: [sent] } = await capture([route('deepseek-test', 'deepseek-official')], { messages: history })
      assert.ok(!JSON.stringify(sent.messages).includes('Historical tool execution record'))
      assert.equal(sent.messages.length, history.length)
      assert.equal(sent.messages[0].content[0].type, 'tool-call')
    }
  })
  await check('foreign tool records retain structured image/file attachments as well as exact textual results', async () => {
    const image = { type: 'image', attachment: { attachmentId: 'synthetic-image', mediaType: 'image/png', bytes: 1, width: 1, height: 1 } }
    const file = { type: 'file', attachment: { attachmentId: 'synthetic-file', mediaType: 'text/plain', bytes: 1, fileName: 'synthetic.txt' } }
    const returned = { ...result('foreign'), content: [{ type: 'text', text: 'Actual result with attachments' }, image, file] }
    const history = [assistant([call('foreign')], codexResponse), returned]
    const { requests: [sent] } = await capture([route('deepseek-test', 'deepseek-official')], { messages: history })
    assert.equal(sent.messages[0].role, 'user')
    assert.equal(sent.messages[0].content[1], image)
    assert.equal(sent.messages[0].content[2], file)
    assert.ok(sent.messages[0].content[0].text.includes('Actual result with attachments'))
  })
  await check('native source and genuine DeepSeek reasoning signatures survive KIN lane replay', async () => {
    const ds = createAssistantMessage({ content: [{ type: 'reasoning', text: 'Actual synthetic reasoning' }, call('native')], source: { provider: 'kin', model: 'task', replayState: { response: { kind: 'deepseek-messages', version: 1, model: 'deepseek-test' }, blocks: [{ type: 'reasoning', signature: 'synthetic-signature' }, { type: 'tool-call' }] } } })
    const history = [user(), ds, result('native')]
    const before = JSON.stringify(history)
    const { requests: [sent] } = await capture([route('deepseek-test', 'deepseek-official')], { messages: history })
    assert.equal(sent.messages[1].source.provider, 'deepseek-official')
    assert.equal(sent.messages[1].source.model, 'deepseek-test')
    assert.equal(sent.messages[1].source.replayState.blocks[0].signature, 'synthetic-signature')
    assert.deepEqual(sent.messages[1].content, ds.content)
    assert.equal(JSON.stringify(history), before)
    const pi = await capture([route('gpt-test', 'openai-codex')], { messages: [assistant([call('pi')], codexResponse)] })
    assert.equal(pi.requests[0].messages[0].source.provider, 'openai-codex')
    assert.equal(pi.requests[0].messages[0].source.model, 'gpt-test')
  })

  // The real adapters serialize requests and parse a real SSE stream over loopback. Assertions inspect wire bodies.
  const wires = []
  let reply = 'text'
  const server = createServer(async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk
    const body = JSON.parse(raw); wires.push({ path: req.url, body })
    const send = event => res.write('data: ' + JSON.stringify(event) + '\n\n')
    res.writeHead(200, { 'content-type': 'text/event-stream' })
    if (req.url.endsWith('/messages')) {
      send({ type: 'message_start', message: { id: 'msg_test', type: 'message', role: 'assistant', model: body.model, content: [], usage: { input_tokens: 1, output_tokens: 0 } } })
      send({ type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } })
      send({ type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: 'Real synthetic reasoning from the test provider.' } })
      send({ type: 'content_block_delta', index: 0, delta: { type: 'signature_delta', signature: 'synthetic-native-signature' } })
      send({ type: 'content_block_stop', index: 0 })
      send({ type: 'content_block_start', index: 1, content_block: reply === 'tool' ? { type: 'tool_use', id: 'toolu_native', name: 'lookup', input: {} } : { type: 'text', text: '' } })
      send({ type: 'content_block_delta', index: 1, delta: reply === 'tool' ? { type: 'input_json_delta', partial_json: '{"query":"synthetic"}' } : { type: 'text_delta', text: 'ok' } })
      send({ type: 'content_block_stop', index: 1 })
      send({ type: 'message_delta', delta: { stop_reason: reply === 'tool' ? 'tool_use' : 'end_turn' }, usage: { output_tokens: 2 } })
      send({ type: 'message_stop' })
    } else {
      const item = reply === 'tool' ? { type: 'function_call', id: 'fc_test', call_id: 'call_test', name: 'lookup', arguments: '{"query":"synthetic"}' }
        : { type: 'message', id: 'msg_test', role: 'assistant', content: [{ type: 'output_text', text: 'ok', annotations: [] }] }
      send({ type: 'response.created', response: { id: 'resp_test', model: body.model, status: 'in_progress', output: [] } })
      send({ type: 'response.output_item.added', output_index: 0, item: reply === 'tool' ? { ...item, arguments: '' } : { ...item, content: [] } })
      if (reply === 'tool') send({ type: 'response.function_call_arguments.delta', item_id: item.id, output_index: 0, delta: item.arguments })
      else {
        send({ type: 'response.content_part.added', item_id: item.id, output_index: 0, content_index: 0, part: { type: 'output_text', text: '', annotations: [] } })
        send({ type: 'response.output_text.delta', item_id: item.id, output_index: 0, content_index: 0, delta: 'ok' })
      }
      send({ type: 'response.output_item.done', output_index: 0, item })
      send({ type: 'response.completed', response: { id: 'resp_test', model: body.model, status: 'completed', output: [item], usage: { input_tokens: 1, output_tokens: 2, total_tokens: 3 } } })
    }
    res.end()
  })
  await new Promise(done => server.listen(0, '127.0.0.1', done))
  const baseURL = `http://127.0.0.1:${server.address().port}/v1`
  const ctx = new Context()
  const oldKey = process.env.KIN_MODEL_TEST_KEY
  process.env.KIN_MODEL_TEST_KEY = 'synthetic-key'
  try {
    await ctx.plugin(LlmRuntime)
    await ctx.plugin(PiAi, { providers: { 'kin-relay': { api: 'openai-responses', baseURL, apiKeyEnv: 'KIN_MODEL_TEST_KEY',
      models: ['gemini-test', 'claude-test'].map(id => ({ id, contextWindow: 200000, maxTokens: 2000, reasoningEfforts: { medium: 'medium' } })) } } })
    const deepseek = new DeepSeekAdapter({ options: () => resolveAdapterOptions({ baseURL, models: [{ id: 'deepseek-test', contextWindow: 200000, maxTokens: 2000 }] }),
      resolveAuth: async () => ({ headers: { authorization: 'Bearer synthetic-key' } }), resolveUserId: () => '00000000-0000-4000-8000-000000000001',
      prepareExtensions: async () => ({ fields: {}, accepted: () => {} }) })
    ctx.llm.registerAdapter(['deepseek-official'], deepseek)
    let selected = route('gemini-test')
    const routed = new KinModel(ctx, () => [selected])
    ctx.llm.registerAdapter(['kin'], routed)
    const run = async (messages) => {
      const a = new BlockAssembler()
      for await (const chunk of ctx.llm.stream({ provider: 'kin', model: 'task', messages, tools, maxTokens: 256 })) a.push(chunk)
      assert.ok(!['error', 'aborted'].includes(a.finish.kind), JSON.stringify(a.finish))
      return { content: a.blocks(), source: { provider: 'kin', model: 'task', replayState: a.replayState }, finish: a.finish }
    }
    await check('real Gemini Responses adapter sends nullable wire schema and completes a model tool/result round trip', async () => {
      selected = route('gemini-test'); reply = 'tool'
      const initial = user('Use lookup once.'), answer = await run([initial])
      const emitted = answer.content.find(b => b.type === 'tool-call')
      assert.equal(emitted.name, 'lookup'); assert.deepEqual(JSON.parse(emitted.arguments), { query: 'synthetic' })
      assert.equal(wires.at(-1).body.tools[0].parameters.properties.query.type, 'string')
      assert.equal(wires.at(-1).body.tools[0].parameters.properties.query.nullable, true)
      assert.equal(wires.at(-1).body.reasoning.effort, 'medium')
      reply = 'text'; const done = await run([initial, createAssistantMessage(answer), result(emitted.id)])
      assert.equal(done.finish.kind, 'stop')
      assert.ok(wires.at(-1).body.input.some(x => x.type === 'function_call_output'))
    })
    await check('real Claude Responses wire uses paired toolu ids after a Codex history fallback', async () => {
      selected = route('claude-test'); reply = 'text'
      await run([user(), assistant([call('call_foreign|fc_foreign')], codexResponse), result('call_foreign|fc_foreign')])
      const input = wires.at(-1).body.input, called = input.find(x => x.type === 'function_call'), returned = input.find(x => x.type === 'function_call_output')
      assert.match(called.call_id, /^toolu_/); assert.equal(called.call_id, returned.call_id)
    })
    await check('Claude Responses replays full dialogue without its own opaque Responses reasoning', async () => {
      selected = route('claude-test'); reply = 'text'
      let prior = assistant([{ type: 'reasoning', text: 'private synthetic reasoning' }, { type: 'text', text: 'previous visible answer' }],
        { kind: 'pi-ai', version: 2, api: 'openai-responses', provider: 'kin-relay', model: 'claude-test', stopReason: 'stop' })
      prior = { ...prior, source: { ...prior.source, replayState: { ...prior.source.replayState, blocks: [{ type: 'reasoning', thinkingSignature: JSON.stringify({ type: 'reasoning', id: 'rs_synthetic', summary: [], encrypted_content: 'synthetic-opaque' }) }, { type: 'text' }] } } }
      const history = [user(), prior, user('Continue')], before = JSON.stringify(history)
      await run(history)
      const input = wires.at(-1).body.input
      assert.ok(!input.some(x => ['reasoning', 'compaction'].includes(x.type)), 'Claude relay rejects Responses replay state')
      assert.ok(JSON.stringify(input).includes('previous visible answer'))
      assert.ok(!JSON.stringify(input).includes('private synthetic reasoning'))
      assert.equal(JSON.stringify(history), before, 'durable native history remains unchanged')
    })
    await check('real DeepSeek Messages accepts foreign completed data without thinking fabrication and replays native signatures next round', async () => {
      selected = route('deepseek-test', 'deepseek-official'); reply = 'tool'
      const history = [user(), assistant([call('call_foreign')], codexResponse), result('call_foreign', 'read failed', true)]
      const answer = await run(history), body = wires.at(-1).body
      assert.equal(body.thinking.type, 'enabled'); assert.equal(body.output_config.effort, 'max')
      assert.ok(!body.messages.some(m => m.role === 'assistant' && m.content.some(b => b.type === 'tool_use')))
      assert.ok(JSON.stringify(body.messages).includes('read failed'))
      const emitted = answer.content.find(b => b.type === 'tool-call'); assert.ok(emitted)
      assert.equal(answer.source.replayState.response.kinRoute.provider, 'deepseek-official')
      reply = 'text'; await run([...history, createAssistantMessage(answer), result(emitted.id)])
      const native = wires.at(-1).body.messages.find(m => m.role === 'assistant' && m.content.some(b => b.type === 'tool_use'))
      assert.equal(native.content.find(b => b.type === 'thinking').signature, 'synthetic-native-signature')
    })
  } finally {
    if (oldKey === undefined) delete process.env.KIN_MODEL_TEST_KEY; else process.env.KIN_MODEL_TEST_KEY = oldKey
    server.closeAllConnections(); await new Promise(done => server.close(done))
    await ctx.fiber.dispose()
  }
} finally { rmSync(output, { recursive: true, force: true }) }
if (failures) process.exitCode = 1
