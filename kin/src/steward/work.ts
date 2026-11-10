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
 * Work worth splitting she hands to a team: the worker is then the Lead of a DSH Agent Team in that folder
 * (her crew preset, cordis.patch.yml), calls teammates of its own and shares a task board with them. The
 * teammates are its children: DSH gives them its preset, its model route and its permission. A team's work
 * ends when the whole team has gone quiet, and a stop stops all of it.
 *
 * While work runs she is kept in the picture without asking: a worker that waits for the owner's approval in
 * DSH is told to her after a short while, and the owner may answer it to her instead of in DSH (`answer`); each
 * of her turns starts with what has changed in the work that conversation handed out (`brief`).
 *
 * Beside DSH's `subagent` tool, which she keeps for quick help inside a conversation: a subagent lives
 * under her conversation and never asks, while a worker is a session the owner can open and answer.
 */
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'
import type {} from '@deepseek-ai/dsh-experimental-agent-team'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq, type Session, type SessionId } from '@deepseek-ai/dsh-session'
import type { ApprovalOutcome } from '@deepseek-ai/dsh-user-approval'
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
  /** The worker leads a DSH Agent Team: teammates of its own in the same folder. */
  team?: boolean
  /** The worker's approvals she has been told are waiting, by DSH's id for each. */
  told?: string[]
  /** The worker's closing message. */
  result?: string
  /** What its ending still owes: its place in her memory (how it ended, in Core's word) and word of it in the conversation that asked. */
  owed?: { memory?: string; word?: true }
  /** For running work: the last thing the worker said or did. */
  progress?: string
  /** For a running team: its members and task board. */
  crew?: Crew
}

/** A team as DSH has it now: who is on it, and its shared task board. */
export interface Crew {
  members: { name: string; status: 'running' | 'inactive' | 'provisioning' | 'failed'; job?: string }[]
  tasks: { title: string; status: 'pending' | 'in_progress' | 'completed' | 'deleted'; owner?: string }[]
}

/** What the KIN preset's steward plugin (steward/preset.ts) uses. */
export interface Steward {
  /**
   * Start a worker in `folder` on `task`, for the conversation `session`; with `team`, as the lead of a team
   * (the returned work says whether this DSH could give it one). Throws what she should hear.
   */
  delegate(request: { session: string; label: string; task: string; folder: string; team?: boolean }): Promise<Work>
  /** Open work first, then what ended lately, newest first. */
  list(): Work[]
  /** A word to a worker: steers it while it works, sets it going again once it has stopped. */
  tell(id: string, text: string): Promise<void>
  stop(id: string): void
  /**
   * What changed in the work this conversation handed out since she was last told here, in a few lines for
   * her context as a turn starts (preset.ts); nothing when nothing did.
   */
  brief(session: string): string
  /**
   * The owner's answer, given in the conversation `session`, to one approval she was told a worker of that
   * conversation waits for (`approval`: the number she was told it by): allowed once or refused, as a click in
   * DSH's prompt would. An approval no longer waiting is never answered as another one. Returns how it went, in a line for her.
   */
  answer(session: string, approval: string, allow: boolean): Promise<string>
  /** Puts the owner's 帮手的权限 on one of her conversations. */
  permit: Permit
}

const KEPT = 40
const ENDINGS: Record<string, Work['state']> = { completed: 'done', aborted: 'stopped', interrupted: 'interrupted' }
const SAID: Record<Work['state'], string> = { running: '还在做', done: '做完了', stopped: '停下了', failed: '没做成', interrupted: '断在了半路' }
/** Her crew's preset (cordis.patch.yml): the owner's coding tools and DSH's Agent Teams, for a team's lead and its teammates. */
const TEAM_PRESET = 'kin-crew'
/** An approval may wait this long before she is told: the owner may be sitting right in front of DSH. */
const APPROVAL_GRACE_MS = 20_000
/** A team's work has ended once the whole team has been quiet this long: a teammate's closing word wakes its lead again. */
const QUIET_MS = 5_000
/** How often what an ending still owes is tried again, and for how long. */
const OWED_RETRY_MS = 60_000
const OWED_KEEP_MS = 24 * 3_600_000
const LEAD = '（这件活由你带队：能拆开并行做的部分，用 spawn_teammate 叫队友分头做、用任务板分工；等队友都做完、你检查过，再收尾。）\n'
const MEMBER: Record<Crew['members'][number]['status'], string> = { running: '在做', inactive: '歇着', provisioning: '刚叫来', failed: '没叫来' }

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
  // DSH's team service as a worker has it: the owner's own Agent Teams when that is switched on, else her crew preset's.
  const teams = (agent: Agent) => ctx.get('agentTeams') ?? ctx.get('agentPresets')?.serviceFor(agent, 'agentTeams')

  /** A team's members as DSH lists them, lead first; none for work one worker does, or a lead that is not loaded. */
  const roster = (item: Work) => {
    const lead = item.team ? live(item.id) : undefined
    try {
      return (lead && teams(lead)?.listMembers(lead)) || []
    } catch {
      // DSH refuses team reads once a team's own records stopped replaying: the work goes on as one worker's.
      return []
    }
  }
  const crewOf = (item: Work): Crew | undefined => {
    const members = roster(item)
    const lead = live(item.id)
    if (!members.length || !lead) return
    return {
      members: members.map(({ name, status, description }) => ({ name, status, ...(description ? { job: description } : {}) })),
      tasks: teams(lead)!.listTasks(lead).map(({ subject, status, ownerName }) => ({ title: subject, status, ...(ownerName ? { owner: ownerName } : {}) })),
    }
  }

  // Teams being stopped, the quiet each running team is waiting out, approvals waiting out their grace, and
  // what each running work last read as when she was briefed on it.
  const stopping = new Set<string>()
  const quiet = new Map<string, NodeJS.Timeout>()
  const asking = new Map<string, NodeJS.Timeout>()
  const briefed = new Map<string, string>()
  ctx.effect(() => () => {
    for (const timer of [...quiet.values(), ...asking.values()]) clearTimeout(timer)
  }, 'kin steward waits')

  /** The outcome joins her memory as something that happened. */
  const remember = (item: Work, status: string) => mind.call('runtime-event', {
    id: `work:${item.id}:${item.ended}`, kind: 'task-result', at: item.ended, task_id: item.id, session: item.session,
    ...ctx.get('kin')?.scopeOf(item.session), operation_status: status, account_basis: 'model',
    label: item.label, folder: item.folder, text: item.result || item.label,
  })

  /** She hears of it in the conversation that asked (how it ended, unless said otherwise), and says it in her own words. */
  const tellHer = async (item: Work, what: string, summary = SAID[item.state]) => {
    const agent = await agentOf(item.session)
    agent.followup(createUserMessage({
      content: [{ type: 'text', text: `宿主事实：你交给帮手的「${item.label}」（在 ${item.folder}，帮手 ${item.id}）${what}` }],
      source: { kind: 'kin-steward', form: 'notice', summary: `交给帮手的「${item.label}」${summary}` },
    }))
  }

  /** How an ending is told to her. */
  const closing = (item: Work) => (item.state === 'interrupted'
    ? '因为 DSH 重启停在了半路，没有做完。要它接着做，用 kin_work_tell 跟它说一声；要不要接着做、要不要告诉对方，你来定。'
    : `${SAID[item.state]}。${item.result ? `它最后说：\n${item.result}` : '它没留下话。'}`)

  // An ending reaches her memory and the conversation that asked. What did not get through (her mind away, the
  // conversation not open) stays owed on the work itself and is tried again each minute, for a day.
  const concluding = new Set<string>()
  const conclude = async (item: Work, again = false) => {
    const owed = item.owed
    if (!owed || concluding.has(item.id)) return
    concluding.add(item.id)
    const missed = (what: string) => (error: Error) => { if (!again) host.log(`work result not ${what} yet: ${error.message}`) }
    try {
      await Promise.all([
        owed.memory && remember(item, owed.memory).then(() => { delete owed.memory }, missed('remembered')),
        owed.word && tellHer(item, closing(item)).then(() => { delete owed.word }, missed('told')),
      ])
      const late = Date.now() - Date.parse(item.ended!) > host.ms(OWED_KEEP_MS)
      if (late) host.log(`work result given up after a day: ${item.id}`)
      if (late || (!owed.memory && !owed.word)) delete item.owed
      save()
    } finally {
      concluding.delete(item.id)
    }
  }
  const owing = setInterval(() => { for (const item of Object.values(ledger)) void conclude(item, true) }, host.ms(OWED_RETRY_MS))
  owing.unref()
  ctx.effect(() => () => clearInterval(owing), 'kin steward owed results')

  const settle = (item: Work, session: Session) => {
    clearTimeout(quiet.get(item.id))
    quiet.delete(item.id)
    briefed.delete(item.id)
    const ending = lastTurn(session)
    Object.assign(item, {
      // A stopped team's lead may have ended its own last turn well before the stop.
      state: stopping.delete(item.id) ? 'stopped' : ENDINGS[ending?.reason.kind ?? ''] ?? 'failed', ended: new Date().toISOString(),
      result: ending?.said.slice(0, 2000) || undefined, owed: { memory: ending?.reason.kind ?? 'unknown', word: true },
    })
    save()
    void conclude(item)
  }

  // A worker that was set going and is quiet again has ended its work, however it went. A team has when its
  // lead and every teammate are quiet and stay so: a teammate's turns count as its team's.
  ctx.on('agent/status', ({ agent, status }) => {
    const item = ledger[agent.session.id]
    if (status === 'running' && item) agent.model.select({ provider: PROVIDER, model: 'task' })
    const work = item ?? ledger[agent.session.header.parentSession ?? '']
    if (work?.state !== 'running') return
    if (!work.team) {
      if (status === 'idle' && item) settle(item, agent.session)
      return
    }
    clearTimeout(quiet.get(work.id))
    // Whatever sets a stopped team going again (a teammate's closing word reaching its lead) ends as it starts.
    if (status === 'running' && stopping.has(work.id)) return agent.cancel({ kind: 'user' })
    if (status !== 'idle') return
    quiet.set(work.id, setTimeout(() => void agentOf(work.id).then((lead) => {
      if (work.state === 'running' && !roster(work).some((member) => member.status === 'running' || member.status === 'provisioning')) settle(work, lead.session)
    }, (error: Error) => host.log('team work not settled: ' + error.message)), host.ms(QUIET_MS)).unref())
  })

  // A worker waiting for the owner's approval in DSH would wait unnoticed: once it has waited a while she is
  // told where the work was asked for, each approval once.
  ctx.on('session/event', (session, event) => {
    const item = ledger[session.id]
    if (!item) return
    if (event.type === 'approval/decided') {
      clearTimeout(asking.get(event.data.id))
      asking.delete(event.data.id)
    } else if (event.type === 'approval/asked' && item.state === 'running' && !item.told?.includes(event.data.id)) {
      const { id, reason } = event.data
      asking.set(id, setTimeout(() => {
        asking.delete(id)
        item.told = [...(item.told ?? []), id]
        save()
        void tellHer(item, `停下来在等批准（批准编号 ${handle(id)}）：它要${use(session, event.data)}${reason ? `，缘由：${reason}` : ''}。`
          + '要小光答复它才会接着做：他可以在 DSH 里打开这个帮手的会话点同意或拒绝，也可以就在这儿说让不让；他在这儿说了，你用 kin_work_answer 带上这个批准编号替他答复。', '在等小光批准')
          .catch((error: Error) => host.log('waiting approval not told: ' + error.message))
      }, host.ms(APPROVAL_GRACE_MS)).unref())
    }
  })

  // DSH answers an approval only inside its `approval/request` waterfall, and withdraws its prompt only when the
  // ask's signal aborts. So a worker's ask goes on to DSH's prompt as a copy under a signal of hers: whichever
  // answers first, the prompt or the owner's word to her (`answer`), settles it and withdraws the other.
  const waiting = new Map<string, { item: Work; session: Session; asked: { toolName: string; callId?: string }; settle: (outcome: ApprovalOutcome) => void }>()
  const relayed = new WeakSet<object>()
  ctx.on('approval/request', function (req, next) {
    const item = relayed.has(req) ? undefined : ledger[req.agent.session.id]
    const asked = item?.state === 'running' && askedFor(req.agent.session, req, waiting)
    if (!item || !asked) return next()
    const withdraw = new AbortController()
    const prompt = { ...req, signal: req.signal ? AbortSignal.any([req.signal, withdraw.signal]) : withdraw.signal }
    relayed.add(prompt)
    const here = new Promise<ApprovalOutcome>((settle) => waiting.set(asked.id, { item, session: req.agent.session, asked, settle }))
    return Promise.race([ctx.waterfall(this, 'approval/request', prompt, async () => 'unavailable' as const), here]).finally(() => {
      waiting.delete(asked.id)
      withdraw.abort()
    })
  }, { prepend: true })

  const progress = (id: string) => {
    const session = live(id)?.session
    let answered = false
    for (let seq = Number(session?.seq ?? 0) - 1; session && seq >= 0; seq--) {
      const event = session.eventAt(SessionSeq(seq))
      if (event?.type === 'approval/decided') answered = true
      if (event?.type === 'approval/asked' && !answered) return '在等小光批准一个操作'
      if (event?.type === 'assistant/message') {
        const said = textOf(event.data.message.content)
        if (said) return said.slice(-160)
      }
      if (event?.type === 'tool/call') return '在用 ' + event.data.name
    }
  }

  ctx.provide('kinSteward', {
    async delegate({ session, label, task, folder, team }) {
      if (!isAbsolute(folder) || !existsSync(folder) || !statSync(folder).isDirectory()) {
        throw new Error(`没有「${folder}」这个文件夹。问清楚主人要在哪个文件夹做（绝对路径）再交出去。`)
      }
      if (!relative(host.config.dataDir, resolve(folder)).startsWith('..')) {
        throw new Error('这是你自己的数据目录，不是干活的地方。问清楚主人要在哪个文件夹做。')
      }
      // A lead needs DSH's team tools. With the owner's own Agent Teams on, every session has them; otherwise her
      // crew preset carries them, when this DSH could load it.
      const crew = !team ? undefined : ctx.get('agentTeams') ? WORKER_PRESET
        : await ctx.get('agentPresets')?.resolve(TEAM_PRESET).then((preset) => (preset.id === TEAM_PRESET && !preset.broken ? TEAM_PRESET : undefined), () => undefined)
      const { sessionId } = await controller().create({ cwd: folder, agentPreset: crew ?? WORKER_PRESET })
      const item: Work = { id: sessionId, session, label, folder, state: 'running', started: new Date().toISOString() }
      // In the ledger before its first request, so that request goes to her task lane.
      ledger[sessionId] = item
      save()
      await controller().rename({ sessionId, title: label })
      const agent = await agentOf(sessionId)
      permit(agent.session)
      if (crew && teams(agent)) {
        item.team = true
        save()
      }
      agent.followup(createUserMessage({
        content: [{ type: 'text', text: task + '\n\n' + (item.team ? LEAD : '') + '（做完时用几句话说清楚做了什么、结果放在哪。）' }],
        source: { kind: 'agent-message', form: 'relay', senderSessionId: session as SessionId },
      }))
      return item
    },
    list: () => Object.values(ledger)
      .sort((a, b) => Number(b.state === 'running') - Number(a.state === 'running') || b.started.localeCompare(a.started))
      .map((item) => {
        if (item.state !== 'running') return item
        const crew = crewOf(item)
        return { ...item, progress: progress(item.id), ...(crew ? { crew } : {}) }
      }),
    async tell(id, text) {
      const item = ledger[id]
      if (!item) throw new Error('没有这个帮手。')
      const agent = await agentOf(id)
      const message = createUserMessage({
        content: [{ type: 'text', text }], source: { kind: 'agent-message', form: 'relay', senderSessionId: item.session as SessionId },
      })
      if (agent.status === 'running') return agent.send(message, 'next-step', true)
      // A word after a stop means carry on; a team's lead may be quiet while its work still runs.
      stopping.delete(id)
      if (item.state !== 'running') {
        Object.assign(item, { state: 'running', started: new Date().toISOString() })
        delete item.ended
        save()
      }
      agent.followup(message)
    },
    stop(id) {
      const item = ledger[id]
      if (item?.state !== 'running') throw new Error('这件活已经不在做了。')
      // A team stops whole: each teammate's turn ends too, and the quiet that follows settles the work.
      if (item.team) {
        stopping.add(id)
        for (const member of roster(item)) if (member.role === 'teammate') live(member.id)?.cancel({ kind: 'user' })
      }
      // It ends its turn; the idle that follows settles it and she is told.
      live(id)?.cancel({ kind: 'user' }, { keepInbox: true })
    },
    async answer(session, approval, allow) {
      // Only what she was told of, in this conversation, and still waiting: the owner's word was about that one.
      const open = [...waiting].filter(([id, { item }]) => item.session === session && item.state === 'running' && item.told?.includes(id))
      const others = (left: typeof open) => (left.length ? `还在等批准的：${left.map(([id, { asked, session: worker }]) => `${handle(id)}（${use(worker, asked)}）`).join('；')}。` : '')
      const found = open.find(([id]) => handle(id) === handle(approval.trim()))
      if (!found) return `没有答复：批准编号 ${approval} 已经不在等了（在 DSH 里处理过了，或者帮手停了）。` + (others(open) || '这段对话交出去的活里，眼下没有在等批准的。')
      const [id, { item, session: worker, asked, settle }] = found
      waiting.delete(id)
      settle(allow ? 'allowed-once' : 'rejected')
      return (allow ? `同意了：「${item.label}」的帮手这一次可以${use(worker, asked)}。` : `拒绝了：「${item.label}」的帮手不能${use(worker, asked)}。`)
        + others(open.filter((ask) => ask !== found))
    },
    brief(session) {
      const lines = Object.values(ledger).flatMap((item) => {
        if (item.session !== session || item.state !== 'running') return []
        const crew = crewOf(item)
        const now = [progress(item.id), crew && crewWords(crew)].filter(Boolean).join('；')
        if (!now || briefed.get(item.id) === now) return []
        briefed.set(item.id, now)
        return [`- 「${item.label}」：${now}`]
      })
      return lines.length ? '你交给帮手的活，眼下：\n' + lines.join('\n') : ''
    },
    permit,
  } satisfies Steward)

  // Work a previous run of DSH left going has no turn any more: she hears of it in the conversation
  // that asked, and decides whether to have it carry on.
  // An ending a previous run still owed her is given now as well.
  void host.ready.then(async () => {
    for (const item of Object.values(ledger)) {
      if (item.state === 'running' && live(item.id)?.status !== 'running') {
        Object.assign(item, { state: 'interrupted', ended: new Date().toISOString(), owed: { memory: 'interrupted', word: true } })
        save()
      }
      await conclude(item)
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

/** A team in a few words: who is on it, how its task board stands, and what is being done. */
export function crewWords(crew: Crew): string {
  const members = crew.members.map((member) => `${member.name === 'lead' ? '队长' : member.name}${MEMBER[member.status]}`).join('、')
  const doing = crew.tasks.filter((task) => task.status === 'in_progress').slice(0, 3).map((task) => task.title.slice(0, 30) + (task.owner ? `（${task.owner}）` : ''))
  const done = crew.tasks.filter((task) => task.status === 'completed').length
  return `队里${members}` + (crew.tasks.length ? `；任务板 ${crew.tasks.length} 件、做完 ${done} 件` : '') + (doing.length ? `，在做：${doing.join('、')}` : '')
}

/** An approval as she is told it and names it back: the start of DSH's id for it. */
const handle = (id: string) => id.slice(0, 8)

/** What an approval is for: its tool, and the call's arguments as the worker wrote them. */
function use(session: Session, { toolName, callId }: { toolName: string; callId?: string }): string {
  const call = callId ? wanted(session, callId) : ''
  return `用 ${toolName}${call ? `（${call}）` : ''}`
}

/**
 * The ask DSH logged for this request just before putting it to the answerers: this turn's oldest one for the
 * same tool and call that is neither decided nor taken.
 */
function askedFor(session: Session, { toolName, callId }: { toolName: string; callId?: string }, taken: Map<string, unknown>) {
  const decided = new Set<string>()
  let asked
  for (let seq = Number(session.seq) - 1; seq >= 0; seq--) {
    const event = session.eventAt(SessionSeq(seq))
    if (event?.type === 'turn/start') break
    if (event?.type === 'approval/decided') decided.add(event.data.id)
    if (event?.type === 'approval/asked' && event.data.toolName === toolName && event.data.callId === callId
      && !decided.has(event.data.id) && !taken.has(event.data.id)) asked = event.data
  }
  return asked
}

/** What a tool call of this turn asked for, as the worker wrote it, cut short. */
function wanted(session: Session, callId: string): string {
  for (let seq = Number(session.seq) - 1; seq >= 0; seq--) {
    const event = session.eventAt(SessionSeq(seq))
    if (event?.type === 'tool/call' && event.data.callId === callId) return event.data.arguments.slice(0, 200)
    if (event?.type === 'turn/start') break
  }
  return ''
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
