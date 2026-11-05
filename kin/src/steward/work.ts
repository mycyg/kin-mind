/**
 * The owner's work, done in DSH. When the owner asks KIN for real work — files to sort out, code to write,
 * something to look into — she hands it to a worker: a DSH session of its own in the owner's folder, running
 * the owner's own coding preset on her task lane, with the permission the owner chose for her helpers
 * (permission.ts). The owner can open the session there like any other. She follows it, can add a
 * word, have it carry on or stop it (steward/preset.ts), and when it ends the result comes back to the
 * conversation that asked, where she tells it in her own words. Each outcome joins her memory as
 * something that happened (a `task-result` runtime event), and work a restart cut off is told to her
 * rather than lost.
 *
 * Beside DSH's `subagent` tool, which she keeps for quick help inside a conversation: a subagent lives
 * under her conversation and never asks, while a worker is a session the owner can open and answer.
 */
import type {} from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq, type Session, type SessionId } from '@deepseek-ai/dsh-session'
import { existsSync, statSync } from 'node:fs'
import { isAbsolute, relative, resolve } from 'node:path'
import type { Host } from '../index.js'
import { PROVIDER } from '../models.js'
import { WORKER_PRESET, textOf } from './helpers.js'
import type { Permit } from './permission.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    kinSteward: Steward
  }
}

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    /** A fact about her delegated work, told to her by the host. */
    'kin-steward': { readonly kind: 'kin-steward'; readonly form: 'notice'; readonly summary: string }
  }
}

export interface Work {
  /** The worker's DSH session. */
  id: string
  /** The conversation it was asked in, which its result goes back to. */
  session: string
  label: string
  folder: string
  state: 'running' | 'done' | 'stopped' | 'failed' | 'interrupted'
  started: string
  ended?: string
  /** The worker's closing message. */
  result?: string
  /** For running work: the last thing the worker said or did. */
  progress?: string
}

/** What the KIN preset's steward plugin (steward/preset.ts) uses. */
export interface Steward {
  /** Start a worker in `folder` on `task`, for the conversation `session`. Throws what she should hear. */
  delegate(request: { session: string; label: string; task: string; folder: string }): Promise<Work>
  /** Open work first, then what ended lately, newest first. */
  list(): Work[]
  /** A word to a worker: steers it while it works, sets it going again once it has stopped. */
  tell(id: string, text: string): Promise<void>
  stop(id: string): void
  /** Puts the owner's 帮手的权限 on one of her conversations. */
  permit: Permit
}

const KEPT = 40
const ENDINGS: Record<string, Work['state']> = { completed: 'done', aborted: 'stopped', interrupted: 'interrupted' }
const SAID: Record<Work['state'], string> = { running: '还在做', done: '做完了', stopped: '停下了', failed: '没做成', interrupted: '断在了半路' }

export function work(host: Host, permit: Permit) {
  const { ctx, mind } = host
  const ledger = host.load<Record<string, Work>>('work.json', {})
  const save = () => {
    const ended = Object.values(ledger).filter((item) => item.state !== 'running').sort((a, b) => (b.ended ?? '').localeCompare(a.ended ?? ''))
    for (const old of ended.slice(KEPT)) delete ledger[old.id]
    host.save('work.json', ledger)
  }
  const controller = () => ctx.get('sessionController')!
  const live = (id: string) => ctx.get('agents')?.get(id as SessionId)
  const agentOf = async (id: string) => {
    const resolved = await controller().resolveAgent(id as SessionId)
    if (!('agent' in resolved)) throw new Error('帮手的会话打不开了：' + resolved.error.code)
    return resolved.agent
  }

  /** The outcome joins her memory as something that happened. */
  const remember = (item: Work, status: string) => mind.call('runtime-event', {
    id: `work:${item.id}:${item.ended}`, kind: 'task-result', at: item.ended, task_id: item.id, session: item.session,
    ...ctx.get('kin')?.scopeOf(item.session), operation_status: status, account_basis: 'model',
    label: item.label, folder: item.folder, text: item.result || item.label,
  }).catch((error: Error) => host.log('work result not remembered: ' + error.message))

  /** She hears how it ended in the conversation that asked, and says it in her own words. */
  const tellHer = async (item: Work, what: string) => {
    const agent = await agentOf(item.session)
    agent.followup(createUserMessage({
      content: [{ type: 'text', text: `宿主事实：你交给帮手的「${item.label}」（在 ${item.folder}，帮手 ${item.id}）${what}` }],
      source: { kind: 'kin-steward', form: 'notice', summary: `交给帮手的「${item.label}」${SAID[item.state]}` },
    }))
  }

  const settle = (item: Work, session: Session) => {
    const ending = lastTurn(session)
    Object.assign(item, {
      state: ENDINGS[ending?.reason.kind ?? ''] ?? 'failed', ended: new Date().toISOString(),
      result: ending?.said.slice(0, 2000) || undefined,
    })
    save()
    void remember(item, ending?.reason.kind ?? 'unknown')
    const said = item.result ? `它最后说：\n${item.result}` : '它没留下话。'
    void tellHer(item, `${SAID[item.state]}。${said}`).catch((error: Error) => host.log('work result not told: ' + error.message))
  }

  // A worker that was set going and is quiet again has ended its work, however it went.
  ctx.on('agent/status', ({ agent, status }) => {
    const item = ledger[agent.session.id]
    if (status === 'running' && item) agent.model.select({ provider: PROVIDER, model: 'task' })
    if (status === 'idle' && item?.state === 'running') settle(item, agent.session)
  })

  const progress = (id: string) => {
    const session = live(id)?.session
    let answered = false
    for (let seq = Number(session?.seq ?? 0) - 1; session && seq >= 0; seq--) {
      const event = session.eventAt(SessionSeq(seq))
      if (event?.type === 'approval/decided') answered = true
      if (event?.type === 'approval/asked' && !answered) return '在等主人在 DSH 里批准一个操作'
      if (event?.type === 'assistant/message') {
        const said = textOf(event.data.message.content)
        if (said) return said.slice(-160)
      }
      if (event?.type === 'tool/call') return '在用 ' + event.data.name
    }
  }

  ctx.provide('kinSteward', {
    async delegate({ session, label, task, folder }) {
      if (!isAbsolute(folder) || !existsSync(folder) || !statSync(folder).isDirectory()) {
        throw new Error(`没有「${folder}」这个文件夹。问清楚主人要在哪个文件夹做（绝对路径）再交出去。`)
      }
      if (!relative(host.config.dataDir, resolve(folder)).startsWith('..')) {
        throw new Error('这是你自己的数据目录，不是干活的地方。问清楚主人要在哪个文件夹做。')
      }
      const { sessionId } = await controller().create({ cwd: folder, agentPreset: WORKER_PRESET })
      const item: Work = { id: sessionId, session, label, folder, state: 'running', started: new Date().toISOString() }
      // In the ledger before its first request, so that request goes to her task lane.
      ledger[sessionId] = item
      save()
      await controller().rename({ sessionId, title: label })
      const agent = await agentOf(sessionId)
      permit(agent.session)
      agent.followup(createUserMessage({
        content: [{ type: 'text', text: task + '\n\n（做完时用几句话说清楚做了什么、结果放在哪。）' }],
        source: { kind: 'agent-message', form: 'relay', senderSessionId: session as SessionId },
      }))
      return item
    },
    list: () => Object.values(ledger)
      .sort((a, b) => Number(b.state === 'running') - Number(a.state === 'running') || b.started.localeCompare(a.started))
      .map((item) => (item.state === 'running' ? { ...item, progress: progress(item.id) } : item)),
    async tell(id, text) {
      const item = ledger[id]
      if (!item) throw new Error('没有这个帮手。')
      const agent = await agentOf(id)
      const message = createUserMessage({
        content: [{ type: 'text', text }], source: { kind: 'agent-message', form: 'relay', senderSessionId: item.session as SessionId },
      })
      if (agent.status === 'running') return agent.send(message, 'next-step', true)
      Object.assign(item, { state: 'running', started: new Date().toISOString() })
      delete item.ended
      save()
      agent.followup(message)
    },
    stop(id) {
      if (ledger[id]?.state !== 'running') throw new Error('这件活已经不在做了。')
      // It ends its turn; the idle that follows settles it and she is told.
      live(id)?.cancel({ kind: 'user' }, { keepInbox: true })
    },
    permit,
  } satisfies Steward)

  // Work a previous run of DSH left going has no turn any more: she hears of it in the conversation
  // that asked, and decides whether to have it carry on.
  void host.ready.then(async () => {
    for (const item of Object.values(ledger)) {
      if (item.state !== 'running' || live(item.id)?.status === 'running') continue
      Object.assign(item, { state: 'interrupted', ended: new Date().toISOString() })
      save()
      void remember(item, 'interrupted')
      await tellHer(item, '因为 DSH 重启停在了半路，没有做完。要它接着做，用 kin_work_tell 跟它说一声；要不要接着做、要不要告诉对方，你来定。')
        .catch((error: Error) => host.log('open work not told: ' + error.message))
    }
  })

  // Her page: the work she has going, in which folder, and a way to stop it.
  host.route('/api/kin/work', { GET: async () => ({ work: ctx.kinSteward.list().slice(0, 12) }) })
  host.route('/api/kin/work/stop', {
    POST: async (request) => {
      const { id } = await host.body<{ id: string }>(request)
      ctx.kinSteward.stop(id ?? '')
      return { stopping: true }
    },
  })
}

/** How a session's last turn ended and what it last said. */
function lastTurn(session: Session) {
  let reason: { kind: string } | undefined
  for (let seq = Number(session.seq) - 1; seq >= 0; seq--) {
    const event = session.eventAt(SessionSeq(seq))
    if (!reason && event?.type === 'turn/end') reason = event.data.reason
    if (reason && event?.type === 'assistant/message') {
      const said = textOf(event.data.message.content)
      if (said) return { reason, said }
    }
    if (reason && event?.type === 'turn/start') return { reason, said: '' }
  }
  return reason && { reason, said: '' }
}
