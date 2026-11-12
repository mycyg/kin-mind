import { replyEnvelope } from './reply-fixture.mjs'
// Dev-only scenario for dev/e2e-steward.mjs: KIN as chief steward inside a real headless DSH. Her chat
// lane and her task lane are scripted here: she hands the owner's work to a DSH worker, has a quick
// subagent jot something down in the owner's folder, explores a question through Core's DSH backend,
// and makes the step of a plan she decided on. DSH's Auto review asks the same script, which allows.
import { spawnSync } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { format } from 'node:util'
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import { Scripted, say, settle, textOf, until } from './scenario.mjs'

export const name = 'kin-e2e-steward'
export const inject = ['kin', 'kinSteward', 'llm', 'permissionPresets', 'sessionController', 'sessions']

const FINDINGS = { summary: '钟表靠擒纵机构一格一格放走发条的力', findings: ['擒纵轮每次只放过一个齿'], sources: [],
  open_questions: [], perspective: 'kin_reflection', topic_tags: ['钟表'], evidence_map: null }
const CLOCK = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'

/** Her chat lane and her task lane, played by a script; Core's own calls fall through to the base script. */
class Steward extends Scripted {
  chatTools = []
  workerLanes = []
  /** Each Auto review: its request size in characters, and whether it was about the note. */
  reviews = []
  async *stream(options) {
    const tools = (options.tools ?? []).map((tool) => tool.name)
    const all = JSON.stringify(options.messages)
    if (String(options.system ?? '').startsWith('REVIEW_POLICY')) {
      this.reviews.push({ chars: options.system.length + all.length, note: all.includes('便签') })
      return yield* reply('{"risk":"low","decision":"allow"}')
    }
    if (options.model === 'chat') {
      this.chatTools = tools
      this.chatRequest = { tools: options.tools, system: options.system }
      // What was said since her last answer: the owner's words, the notices DSH or the host added, her recall.
      const said = options.messages.slice(options.messages.findLastIndex((m) => m.role === 'assistant') + 1)
        .filter((m) => m.role === 'user').map(textOf).join('\n')
      // Each ask is acted on once: her recall and DSH's notices can repeat its words later.
      if (said.includes('宿主事实') && said.includes('做完了')) return yield* publicReply('整理好啦，清单写在你那个文件夹的 整理结果.md 里了。')
      if (said.includes('记一笔') && !this.jotted && tools.includes('subagent')) {
        this.jotted = true
        return yield* call('subagent', { description: '记一笔', prompt: `在 ${this.workDir}/便签.md 写一句：周五交周报。`, cwd: this.workDir })
      }
      if (said.includes('整理') && !said.includes('宿主事实') && !this.delegated && tools.includes('kin_delegate')) {
        // With no folder named she guesses a relative one, which is refused.
        this.delegated = said.includes(this.workDir)
        return yield* call('kin_delegate', { title: '整理笔记', folder: this.delegated ? this.workDir : 'notes', task: '把这个文件夹里的笔记整理成一份清单，写进 整理结果.md。' })
      }
      return yield* publicReply('嗯，知道了。')
    }
    if (tools.includes('structured_output')) {
      return yield* (all.includes('"name":"write"')
        ? call('structured_output', { summary: '画好了一只小钟', artifacts: ['clock.svg'], verification: ['打开看过，是一只钟'], remaining: [] })
        : call('write', { file_path: 'clock.svg', content: CLOCK }))
    }
    if (tools.includes('review_creation_completion')) {
      const ids = [...new Set([...all.matchAll(/artifact_id\\?":\s*\\?"([^"\\]+)/g)].map((match) => match[1]))]
      return yield* call('review_creation_completion', { complete: true, reason: '看过了，确实是一只小钟', artifact_ids: ids })
    }
    if (all.includes('探索给定的、有来源的问题')) return yield* reply(JSON.stringify(FINDINGS))
    if (tools.includes('write') && all.includes('便签')) {
      return yield* (all.includes('"name":"write"') ? reply('记好了。')
        : call('write', { file_path: join(this.workDir, '便签.md'), content: '周五交周报\n' }))
    }
    if (tools.includes('write') && all.includes('整理')) {
      this.workerLanes.push(options.model)
      const result = options.messages.findLast((m) => m.role === 'tool')
      if (result) this.workerWrite = textOf(result)
      return yield* (all.includes('"name":"write"') ? reply('整理好了：清单写在 整理结果.md。')
        : call('write', { file_path: '整理结果.md', content: '- 周一的笔记\n- 周三的笔记\n' }))
    }
    yield* super.stream(options)
  }
}

function* reply(text) {
  yield { type: 'block-start', index: 0, blockType: 'text' }
  yield { type: 'text-delta', index: 0, text }
  yield { type: 'block-end', index: 0, block: { type: 'text', text } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'stop' } }
}

let calls = 0
function* call(name, input) {
  const id = ToolCallId('steward_' + ++calls)
  const args = JSON.stringify(input)
  yield { type: 'block-start', index: 0, blockType: 'tool-call' }
  yield { type: 'tool-call-delta', index: 0, id, name, argumentsDelta: args }
  yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name, arguments: args } }
  yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'tool-calls' } }
}

export function apply(ctx, config) {
  ctx.effect(() => ctx.logger.exporter({
    levels: { default: 3 },
    export: (message) => {
      if (message.name.startsWith('kin')) appendFileSync(config.out + '.log', `${new Date(message.ts).toISOString()} ${message.type} ${format(...message.args)}\n`)
    },
  }), 'kin log for the e2e harness')
  const scripted = Object.assign(new Steward(), { workDir: config.workDir })
  ctx.llm.registerAdapter(['kin-test'], scripted)
  setTimeout(() => run(ctx, config, scripted).then(
    (report) => writeFileSync(config.out, JSON.stringify(report, null, 2)),
    (error) => writeFileSync(config.out, JSON.stringify({ error: error.stack ?? String(error) }, null, 2)),
  ), 300)
}

async function run(ctx, config, scripted) {
  const mind = ctx.kin.mind
  await mind.call('liveness-facts')
  const report = { taskEvents: [] }
  // Test-only: Core may commit completion before the host receives plan-result and publishes its page state.
  const completionDelay = Number(process.env.KIN_STEWARD_SETTLE_DELAY_MS || 0)
  if (completionDelay > 0) {
    const runMind = mind.run.bind(mind)
    mind.run = async (action, ...args) => {
      const answer = await runMind(action, ...args)
      if (action === 'plan-result' && answer.state === 'completed') await new Promise(resolve => setTimeout(resolve, completionDelay))
      return answer
    }
  }
  const callMind = mind.call.bind(mind)
  mind.call = (action, args) => {
    if (action === 'runtime-event' && args?.kind === 'task-result') report.taskEvents.push(args)
    return callMind(action, args)
  }
  // Work a previous DSH left going (seeded in work.json) is no longer running.
  report.reconciled = await until(() => ctx.kinSteward.list().find((item) => item.id === 'gone-worker' && item.state !== 'running')?.state, 30_000)

  const home = join(config.dataDir, 'KIN')
  mkdirSync(home, { recursive: true })
  mkdirSync(config.workDir, { recursive: true })
  const kin = await ctx.sessionController.create({ cwd: home, agentPreset: 'kin' })
  report.kinTools = scripted.chatTools

  // No folder named: no worker starts, and she is told to ask which one.
  report.noFolder = await say(ctx, kin.sessionId, '帮我整理一下桌面上的东西')
  report.kinTools = scripted.chatTools
  report.ledgerAfterNoFolder = ctx.kinSteward.list().filter((item) => item.id !== 'gone-worker').length

  // A folder named: a DSH session of its own works there, on her task lane, and the result comes back to her.
  report.delegated = await say(ctx, kin.sessionId, `帮我把 ${config.workDir} 里的笔记整理一下`)
  report.work = await until(() => ctx.kinSteward.list().find((item) => item.label === '整理笔记' && item.state !== 'running'), 120_000)
  report.workerWrite = scripted.workerWrite
  report.file = existsSync(join(config.workDir, '整理结果.md')) ? readFileSync(join(config.workDir, '整理结果.md'), 'utf8') : null
  report.toldOwner = await until(async () => {
    await settle()
    const events = sessionEvents(await agentOf(ctx, kin.sessionId))
    const notice = events.findIndex((event) => event.type === 'user/message' && event.data.source.kind === 'kin-steward')
    const answer = notice >= 0 && events.slice(notice).find((event) => event.type === 'assistant/message' && textOf(event.data.message))
    return answer ? textOf(answer.data.message) : undefined
  }, 60_000)
  // The worker asked the scripted model through KIN's task lane (kin/task → kin-test/scripted).
  report.workerLanes = scripted.workerLanes
  // The worker was set up with the owner's 帮手的权限 (Auto, the default when DSH has it).
  report.workerPreset = report.work && ctx.permissionPresets.current((await agentOf(ctx, report.work.id)).session)
  report.workInMemory = await until(async () => {
    const answer = await mind.call('memory-context', { query: '整理结果', purpose: 'read', mode: 'light' })
    const text = answer.rendered_text ?? answer.text ?? ''
    return text.includes('整理结果') ? text : undefined
  }, 60_000)

  // Another person's work is reported and remembered in that same conversation.
  const friend = JSON.parse(readFileSync(join(config.dataDir, 'sessions.json'), 'utf8'))['app:friend']
  const friendWork = await ctx.kinSteward.delegate({ session: friend.sessionId, label: '朋友的整理', folder: config.workDir, task: '整理一份测试清单。' })
  report.friendWork = await until(() => ctx.kinSteward.list().find((item) => item.id === friendWork.id && item.state !== 'running'), 60_000)
  await until(() => report.taskEvents.some((event) => event.task_id === friendWork.id), 10_000)

  // A quick helper inside the conversation: DSH's subagent, which inherits her conversation's Auto and
  // so may write in the owner's folder, each step reviewed first.
  await say(ctx, kin.sessionId, `帮我记一笔到 ${config.workDir}：周五交周报`)
  const note = join(config.workDir, '便签.md')
  report.note = await until(() => existsSync(note) && readFileSync(note, 'utf8'), 120_000)
  report.reviews = scripted.reviews
  report.chatRequest = scripted.chatRequest

  // What her reviews would have left: a question to explore and a plan step to make. After the
  // conversation, since a new word from the owner sends a decided step back to her review.
  const seeded = spawnSync(config.python, [join(config.kinDir, 'dev/seed-mind.py'), join(config.dataDir, 'mind-config.json')], {
    env: { ...process.env, PYTHONPATH: config.coreSource }, encoding: 'utf8',
  })
  report.seeded = seeded.status === 0 ? JSON.parse(seeded.stdout.trim().split('\n').pop()) : seeded.stderr.slice(-2000)

  // Her own outings, on her minute: an exploration through Core's DSH backend, and the plan step.
  report.exploration = await until(async () => {
    const { activities } = await mind.call('explorations', { limit: 5 })
    return activities.find((item) => item.state === 'complete')
  }, 600_000)
  report.creation = await until(async () => {
    const { plans } = await mind.call('autonomous-plans', { status: 'completed', history: false })
    const step = plans?.flatMap((plan) => plan.steps ?? []).find((s) => s.id === 'make')
    return step?.state === 'completed' ? step : undefined
  }, 600_000)
  return report
}

async function agentOf(ctx, sessionId) {
  return (await ctx.sessionController.resolveAgent(sessionId)).agent
}

function sessionEvents(agent) {
  const events = []
  for (let seq = 0; agent && seq < Number(agent.session.seq); seq++) {
    const event = agent.session.eventAt(SessionSeq(seq))
    if (event) events.push(event)
  }
  return events
}

function* publicReply(text) { yield* reply(replyEnvelope(text)) }
