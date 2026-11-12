// Validate all real Core schemas against synthetic values; no business tool is executed.
import assert from 'node:assert/strict'
import { createMcpToolDefinition } from '@deepseek-ai/dsh-mcp-client'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { build } from 'esbuild'
const dir = mkdtempSync(resolve('.tool-regression-'))
let failed = 0
const check = async (name, test) => { try { await test(); console.log('✓ ' + name) } catch (e) { failed++; console.error('✗ ' + name + '\n' + e.stack) } }
try {
  await build({ entryPoints: [resolve('src/tool-arguments.ts')], outfile: join(dir, 'validator.mjs'), bundle: true, packages: 'external', format: 'esm', platform: 'node' })
  const { checkedToolCall, checkedTool } = await import(join(dir, 'validator.mjs'))
  const tools = JSON.parse(execFileSync(resolve('../core/.venv/bin/python'), ['-c',
    "from kin_mind.chat_tools import tools; from types import SimpleNamespace; from pathlib import Path; import json,sys; print(json.dumps(tools(SimpleNamespace(db=SimpleNamespace(root=Path(sys.argv[1]))))))", dir],
    { env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: resolve('../core/src') }, encoding: 'utf8' }))
  const deref = (s, root) => s.$ref ? s.$ref.slice(2).split('/').reduce((v, key) => v[key], root) : s
  function example(raw, root) {
    const s = deref(raw, root)
    if (s.const !== undefined) return s.const
    if (s.enum) return s.enum[0]
    if (s.anyOf) return example(s.anyOf.find(v => v.type !== 'null') ?? s.anyOf[0], root)
    switch (s.type) {
      case 'object': return Object.fromEntries((s.required ?? []).map(k => [k, example(s.properties[k], root)]))
      case 'array': return Array.from({ length: s.minItems ?? 0 }, () => example(s.items, root))
      case 'integer': case 'number': return s.minimum ?? 0
      case 'string': return 'synthetic'.padEnd(s.minLength ?? 0, 'x')
      case 'boolean': return false
      case 'null': return null
      default: return {}
    }
  }
  let validCount = 0, rejectedCount = 0
  await check('all Core schemas validate before dispatch without coercion or default insertion', async () => {
    assert.ok(tools.length >= 27)
    for (const tool of tools) {
      const schema = tool.inputSchema, args = example(schema, schema), before = JSON.stringify(args)
      let calls = 0
      const result = await checkedToolCall(schema, args, async () => { calls++; return { content: [{ type: 'text', text: 'synthetic result' }], isError: true } })
      assert.equal(calls, 1, tool.name); assert.equal(JSON.stringify(args), before); assert.equal(result.isError, true); validCount++
      for (const name of schema.required ?? []) {
        const missing = structuredClone(args); delete missing[name]
        assert.throws(() => checkedToolCall(schema, missing, async () => { calls++ }), e => e.code === 'INVALID_ARGS', tool.name)
        assert.equal(calls, 1); rejectedCount++
      }
      for (const [name, raw] of Object.entries(schema.properties ?? {})) {
        const p = deref(raw, schema), types = new Set(p.anyOf?.map(s => deref(s, schema).type) ?? [p.type])
        const wrong = !types.has('array') ? [] : !types.has('boolean') ? true : {}
        assert.throws(() => checkedToolCall(schema, { ...args, [name]: wrong }, async () => { calls++ }), e => e.code === 'INVALID_ARGS', tool.name + '.' + name)
        assert.equal(calls, 1); rejectedCount++
      }
    }
  })
  await check('actual MCP wrapper rejects primitive roots before normalization and preserves its output contract', async () => {
    for (const tool of tools) {
      let dispatches = 0
      const original = createMcpToolDefinition({}, { name: tool.name, rawName: tool.name, inputSchema: tool.inputSchema,
        call: async () => { dispatches++; return { content: [{ type: 'text', text: 'Synthetic result' }] } } })
      const definition = checkedTool(original)
      assert.equal(definition.output, original.output); assert.equal(definition.projectContent, original.projectContent)
      for (const args of [null, 'synthetic', 42, false, []]) {
        assert.throws(() => definition.execute(args, {}), e => e.code === 'INVALID_ARGS')
        assert.equal(dispatches, 0)
      }
      await definition.execute(example(tool.inputSchema, tool.inputSchema), {})
      assert.equal(dispatches, 1)
    }
  })
  await check('mixed object/string/null unions and open-object contracts stay valid', async () => {
    const schema = tools.find(t => t.name === 'read_event_thread').inputSchema
    for (const focus of [{ topic: 'synthetic' }, 'synthetic', null]) {
      let calls = 0
      await checkedToolCall(schema, { identifier: 'synthetic', activity_focus: focus, synthetic_extension: 'open root' }, async () => { calls++ })
      assert.equal(calls, 1)
    }
    for (const focus of [42, true, []]) assert.throws(() => checkedToolCall(schema, { identifier: 'synthetic', activity_focus: focus }, async () => assert.fail('must not dispatch')), e => e.code === 'INVALID_ARGS')
  })
  await check('nested references, range constraints and closed objects reject before side effects', async () => {
    const schema = { type: 'object', additionalProperties: false, required: ['request'], properties: { request: { $ref: '#/$defs/Input' } }, $defs: { Input: {
      type: 'object', additionalProperties: false, required: ['count', 'text'], properties: { count: { type: 'integer', minimum: 1, maximum: 3 }, text: { type: 'string', minLength: 2 } },
    } } }
    for (const args of [{ request: { count: '2', text: 'ok' } }, { request: { count: 4, text: 'ok' } }, { request: { count: 2, text: 'x' } },
      { request: { count: 2, text: 'ok', extra: true } }, { request: { count: 2, text: 'ok' }, extra: true }]) {
      assert.throws(() => checkedToolCall(schema, args, async () => assert.fail('must not dispatch')), e => e.code === 'INVALID_ARGS')
    }
  })
  console.log(`Core schema matrix: ${validCount} valid dispatches, ${rejectedCount} invalid dispatches blocked`)
} finally { rmSync(dir, { recursive: true, force: true }) }
if (failed) process.exitCode = 1
