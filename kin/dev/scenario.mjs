// Dev-only scenario, mounted by dev/e2e.mjs inside a real headless DSH. Drives KIN through DSH's own
// session APIs, then writes what happened to `config.out` for the harness to check.
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { format } from 'node:util'
import { LlmAdapter, ToolCallId, createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'

export const name = 'kin-e2e'
export const inject = ['kin', 'llm', 'sessionController', 'sessions']

/**
 * The task lane's model in this run (models.json routes `kin/task` here): it answers Core's
 * appraisal with a small change of mood and every other tool with an empty input, and keeps
 * what it was asked so the report can say what a background review sent.
 */
export class Scripted extends LlmAdapter {
  requests = []
  providerInfo() { return { id: 'kin-test', name: 'KIN test' } }
  async listModels() { return [] }
  async resolveModel(provider, model) {
    return { provider, id: model, name: model, inputModalities: ['text'], defaultMaxTokens: 2000, context: { contextWindow: 200_000 } }
  }
  async *stream(options) {
    const tools = (options.tools ?? []).map((tool) => tool.name)
    this.requests.push({ tools, system: options.system ?? '' })
    const name = tools.includes('submit_appraisal') ? 'submit_appraisal' : tools[0]
    const id = ToolCallId('call_' + this.requests.length)
    const args = JSON.stringify(name === 'submit_appraisal' ? { reason: '听说对方下班很累，有点心疼。', values: { mood: 60 } }
      : name === 'submit_compression' ? compression(options.messages) : {})
    if (name) {
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'tool-call-delta', index: 0, id, name, argumentsDelta: args }
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name, arguments: args } }
    } else {
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text: '{}' }
      yield { type: 'block-end', index: 0, block: { type: 'text', text: '{}' } }
    }
    yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
    yield { type: 'finish', reason: { kind: name ? 'tool-calls' : 'stop' } }
  }
}

/** Core's compression request answered as a compressor that keeps everything: one summary of every item. */
function compression(messages) {
  const user = [...messages].reverse().find((message) => message.role === 'user')
  const items = JSON.parse(user.content.map((block) => block.text ?? '').join('')).items ?? []
  return {
    entries: items.length ? [{ item_ids: items.map((item) => item.id), summary: items.map((item) => item.text).join('；').slice(0, 400) }] : [],
    omitted_ids: [],
  }
}

/** KIN's log lines in a file beside the report: headless DSH prints none of them. */
export function keepLog(ctx, file) {
  ctx.effect(() => ctx.logger.exporter({
    levels: { default: 3 },
    export: (message) => {
      if (message.name.startsWith('kin')) appendFileSync(file, `${new Date(message.ts).toISOString()} ${message.type} ${format(...message.args)}\n`)
    },
  }), 'kin log for the e2e harness')
}

export function apply(ctx, config) {
  keepLog(ctx, config.out + '.log')
  const scripted = new Scripted()
  ctx.llm.registerAdapter(['kin-test'], scripted)
  setTimeout(() => run(ctx, config, scripted).then(
    (report) => writeFileSync(config.out, JSON.stringify(report, null, 2)),
    (error) => writeFileSync(config.out, JSON.stringify({ error: error.stack ?? String(error) }, null, 2)),
  ), 300)
}

async function run(ctx, config, scripted) {
  ctx.logger('kin-e2e').info('scenario starts')
  await ctx.kin.mind.call('liveness-facts')
  const report = {}
  const mood = async () => (await ctx.kin.mind.call('read')).state.dimensions.mood.value
  report.moodBefore = await mood()

  // A KIN conversation, as if typed in the desktop app.
  const home = join(config.dataDir, 'KIN')
  mkdirSync(home, { recursive: true })
  const kin = await ctx.sessionController.create({ cwd: home, agentPreset: 'kin' })
  report.kinPreset = kin.agentPreset
  const first = await say(ctx, kin.sessionId, '今天下班好累，想吃火锅')
  await settle()
  const second = await say(ctx, kin.sessionId, '我刚才说想吃什么来着？')
  await settle()
  report.kinTurns = [first, second]
  report.kinRoute = await routeOf(ctx, kin.sessionId)
  report.recorded = JSON.parse(readFileSync(join(config.dataDir, 'recorded.json'), 'utf8'))[kin.sessionId] ?? 0
  report.recall = await ctx.kin.mind.call('memory-context', { query: '火锅', purpose: 'read', mode: 'light' })
    .then((answer) => answer.rendered_text ?? answer.text ?? "")

  // An ordinary coding session next to it must not see or feed KIN.
  const workDir = join(config.dataDir, '..', 'work')
  mkdirSync(workDir, { recursive: true })
  const work = await ctx.sessionController.create({ cwd: workDir, agentPreset: 'standard' })
  report.workPreset = work.agentPreset
  report.workTurn = await say(ctx, work.sessionId, '帮我看看这个目录里有什么文件')
  await settle()
  report.workRoute = await routeOf(ctx, work.sessionId)
  report.workRecorded = JSON.parse(readFileSync(join(config.dataDir, 'recorded.json'), 'utf8'))[work.sessionId] ?? null
  report.workInMemory = await ctx.kin.mind.call('memory-context', { query: '目录 文件', purpose: 'read', mode: 'light' })
    .then((answer) => (answer.rendered_text ?? answer.text ?? "").includes('帮我看看这个目录'))

  // Her heartbeat: once the conversation has been quiet a while (Core holds background thinking for
  // three minutes after a recall), what was said is reviewed on the task lane, in her voice.
  report.review = await until(() => scripted.requests.find((request) => request.tools.includes('submit_appraisal')), 300_000)
  report.moodAfter = await until(async () => { const value = await mood(); return value !== report.moodBefore ? value : undefined })
  // What she took in is hers in another conversation too.
  report.laterRecall = await until(async () => {
    const answer = await ctx.kin.mind.call('memory-context', { query: '晚饭想吃什么', purpose: 'chat', mode: 'light', session: 'e2e-another-chat', budget: 800 })
    const text = answer.rendered_text ?? answer.text ?? ''
    return text.includes('火锅') ? text : undefined
  }, 120_000)
  return report
}

/** The first truthy answer within a minute and a half, or null. */
export async function until(probe, ms = 90_000) {
  for (const end = Date.now() + ms; Date.now() < end; await new Promise((resolve) => setTimeout(resolve, 500))) {
    const value = await probe()
    if (value) return value
  }
  return null
}

export async function say(ctx, sessionId, text) {
  const resolved = await ctx.sessionController.resolveAgent(sessionId)
  if (!resolved.agent) throw new Error('cannot resolve agent: ' + JSON.stringify(resolved.error))
  const agent = resolved.agent
  const start = Number(agent.session.seq)
  agent.followup(createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } }))
  await ctx.sessions.flush(agent.session)
  await agent.whenIdle()
  const events = []
  for (let seq = start; seq < Number(agent.session.seq); seq++) {
    const event = agent.session.eventAt(SessionSeq(seq))
    if (event?.type === 'user/message') events.push({ type: event.type, source: event.data.source.kind, text: textOf(event.data) })
    else if (event?.type === 'assistant/message') events.push({ type: event.type, text: textOf(event.data.message) })
    else if (event) events.push({ type: event.type })
  }
  return { said: text, events }
}

/** The provider/model the session's latest model request went to. */
async function routeOf(ctx, sessionId) {
  const { agent } = await ctx.sessionController.resolveAgent(sessionId)
  const config = agent?.session.requestHeader()?.config
  return config ? `${config.provider}/${config.model}` : null
}

// Recording runs on the idle transition; give the mind a moment to store it.
export const settle = () => new Promise((resolve) => setTimeout(resolve, 2500))

export function textOf(message) {
  return message.content.filter((block) => block.type === 'text').map((block) => block.text).join('\n')
}
