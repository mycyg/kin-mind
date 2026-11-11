// Real DSH request/recovery/compaction loop on synthetic detached sessions, no network or contacts.
import assert from 'node:assert/strict'
import { Context } from '@deepseek-ai/cordis'
import LlmRuntime, { LlmAdapter, createUserMessage, createAssistantMessage, assistantStreamChunks } from '@deepseek-ai/dsh-llm'
import SessionStore, { Session, SessionId } from '@deepseek-ai/dsh-session'
import Projections from '@deepseek-ai/dsh-session-projection'
import Agents from '@deepseek-ai/dsh-agent'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import * as Executor from '@deepseek-ai/dsh-tool-executor-native'
import Tools from '@deepseek-ai/dsh-tools'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import TokenMeter from '@deepseek-ai/dsh-token-meter'
import { build } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
const out = mkdtempSync(join(import.meta.dirname, '../.budget-'))
await build({ entryPoints: [process.env.KIN_MODEL_SOURCE || join(import.meta.dirname, '../src/models.ts')], outfile: join(out, 'models.mjs'), bundle: true, packages: 'external', format: 'esm', platform: 'node' })
const { KinModel } = await import(join(out, 'models.mjs'))
await build({ entryPoints: [process.env.KIN_COMPACTION_SOURCE || join(import.meta.dirname, '../src/compaction.ts')], outfile: join(out, 'compaction.mjs'), bundle: true, packages: 'external', format: 'esm', platform: 'node' })
const { default: KinCompaction } = await import(join(out, 'compaction.mjs'))
const sections = ['Primary Request and Intent', 'Key Technical Concepts', 'Files and Code', 'Errors and Fixes', 'Pending Jobs', 'Current Work', 'Next Step', 'Critical Context']
const summary = sections.map(s => `## ${s}\n- Synthetic retained fact.`).join('\n\n')
let requests = [], partial = false
class Synthetic extends LlmAdapter {
  async resolveModel(provider, id) { return { provider, id, name: id, context: { contextWindow: 1048576 }, defaultMaxTokens: 128000 } }
  async *stream(options) {
    requests.push(options)
    const text = options.purpose === 'compaction' ? summary : partial ? 'Synthetic paragraph.\n\nUnfinished' : 'Synthetic answer.'
    yield { type: 'text-delta', index: 0, text }
    yield { type: 'block-end', index: 0, block: { type: 'text', text } }
    yield { type: 'finish', reason: partial && options.purpose !== 'compaction' ? { kind: 'error', failure: { code: 'CONTEXT_WINDOW_EXCEEDED', message: 'Synthetic late overflow' } } : { kind: 'stop' } }
  }
}
const ctx = new Context()
try {
  for (const plugin of [SessionStore, Projections, LlmRuntime, Agents, Tools, TokenMeter]) {
    if (plugin === Tools) await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false, includeRuntimeContext: true })
    await ctx.plugin(plugin)
  }
  await ctx.plugin(Executor)
  await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 10 })
  await ctx.plugin(KinCompaction, { thresholdRatio: 0.9, headroomTokens: 32768, maxTokens: 16384 })
  ctx.llm.registerAdapter(['synthetic'], new Synthetic())
  ctx.llm.registerAdapter(['kin'], new KinModel(ctx, () => [{ provider: 'synthetic', model: 'synthetic' }]))
  const seed = Session.create(SessionId('synthetic-seed'))
  for (let turn = 1; turn <= 4; turn++) {
    seed.append('turn/start', { turn })
    seed.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: '汉'.repeat(150000) }] }), { surfaceOp: 'append' })
    seed.append('step/start', { turn, step: 1 })
    seed.append('assistant/message', { turn, step: 1, stream: [], message: createAssistantMessage({ content: [{ type: 'text', text: 'Recorded synthetic answer.' }], source: { provider: 'kin', model: 'chat' } }) }, { surfaceOp: 'append' })
    seed.append('step/end', { turn, step: 1 }); seed.append('turn/end', { turn, reason: { kind: 'completed' } })
  }
  const { agent } = await ctx.agents.create({ sessionId: SessionId('synthetic-budget'), seed: seed.snapshotEvents(), agentOptions: { provider: 'kin', model: 'chat' } })
  let contextAdded = false
  ctx.on('agent/pre-step', async (_, next) => {
    const decision = await next()
    if (!contextAdded && decision.kind === 'enter') {
      contextAdded = true
      return { ...decision, messages: [...decision.messages, createUserMessage({ source: { kind: 'synthetic-context' }, content: [{ type: 'text', text: 'Synthetic runtime context appended after the user.' }] })] }
    }
    return decision
  })
  agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Latest synthetic request.' }] }))
  await agent.whenIdle()
  const events = agent.session.snapshotEvents()
  if (events.at(-1).data.reason.kind !== 'completed') console.error(JSON.stringify({events:events.slice(24).map(e=>({type:e.type,data:e.type.startsWith('compaction')?e.data:undefined})), requests:requests.map(r=>({purpose:r.purpose}))}))
  assert.equal(events.at(-1).type, 'turn/end')
  assert.equal(events.at(-1).data.reason.kind, 'completed', JSON.stringify(events.at(-1).data.reason))
  assert.equal(events.filter(e => e.type === 'compaction/summary').length, 1)
  assert.equal(requests.filter(r => r.purpose === 'compaction').length, 1)
  const answers = requests.filter(r => !r.purpose)
  assert.equal(answers.length, 1, 'the oversized attempt must never reach the underlying provider')
  assert.ok(JSON.stringify(answers[0].messages).length < 10000)
  const attempt = events.findIndex(e => e.type === 'assistant/attempt' && assistantStreamChunks(e.data.stream, 'finish').some(c => c.reason.kind === 'error' && c.reason.failure.code === 'CONTEXT_WINDOW_EXCEEDED'))
  assert.ok(attempt >= 0 && attempt < events.findIndex(e => e.type === 'compaction/start'), 'preflight overflow must precede compaction')
  assert.ok(answers[0].messages.some(m => m.role === 'user' && m.source?.kind === 'user' && m.content.some(b => b.type === 'text' && b.text === 'Latest synthetic request.')), 'current user remains a separate exact user message')
  assert.equal(seed.snapshotEvents().length, 24)
  console.log('✓ 90% complete-request preflight triggers real compaction then exactly one smaller successful provider request')
  requests = []; partial = true
  const before = agent.session.snapshotEvents().length
  agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Synthetic late failure.' }] }))
  await agent.whenIdle()
  const tail = agent.session.snapshotEvents().slice(before)
  assert.equal(requests.length, 1)
  assert.equal(tail.filter(e => e.type === 'compaction/start').length, 0)
  assert.equal(tail.at(-1).data.reason.kind, 'error')
  assert.equal(tail.at(-1).data.reason.error.code, 'CONTEXT_WINDOW_EXCEEDED')
  console.log('✓ actual agent error boundary retains the original partial-output error and never compacts or retries')
} finally { await ctx.fiber.dispose(); rmSync(out, { recursive: true, force: true }) }
