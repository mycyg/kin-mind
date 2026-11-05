/**
 * Her health (PARITY 6): a fault watch every five minutes, a self-check every four hours and a daily check
 * twice a day, all read-only. What is wrong, since when and what to do goes on her page and into DSH's log;
 * nothing is sent anywhere. And the owner's freeze: her background life held, across restarts, until thawed.
 *
 * The old host's /freeze, /thaw and /busy held a release's dispatch and drained it; on DSH a release is the
 * plugin's own reload, so what is left is the owner's: freeze her heartbeat (every lane, her proactive contact
 * with it) and the memory worker, and see what is still under way.
 */
import type { Host } from '../index.js'
import { daily, LANE_NAMES, selfCheck, watch, type Finding } from './checks.js'

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const WATCH_MS = 5 * MINUTE
const SELF_MS = 4 * HOUR
/** The daily check runs at 06:40 and 18:40, as the old launchd job did; one missed while DSH was closed runs at the next watch. */
const DAILY_AT = [6, 40] as const

type Depth = 'watch' | 'self' | 'daily'
/** Which checks each run takes in. */
const COVERS: Record<Depth, Depth[]> = { watch: ['watch'], self: ['watch', 'self'], daily: ['watch', 'self', 'daily'] }

interface Problem extends Finding {
  /** The check that finds it: a run that takes that check in and no longer finds it has seen it clear. */
  from: Depth
  since: string
}

interface State {
  frozen: { since: string } | null
  problems: Record<string, Problem>
  /** Problems that cleared, latest last. */
  resolved: { title: string; since: string; until: string }[]
  checked: Partial<Record<Depth, string>>
  /** The daily checks, latest last: whether nothing was wrong, what was, and how the last day's reviews ended. */
  days: { at: string; ok: boolean; problems: string[]; reviews: Record<string, number> }[]
}

export function health(host: Host) {
  const { heartbeat, mind } = host
  const logger = host.ctx.logger('kin')
  const state = host.load<State>('health.json', { frozen: null, problems: {}, resolved: [], checked: {}, days: [] })
  // Frozen before a restart, she wakes frozen: her heartbeat has not started yet, so nothing runs in between.
  if (state.frozen) heartbeat.freeze(true)

  async function run(depth: Depth) {
    const found: [Depth, Finding[]][] = [['watch', await watch(host)]]
    if (depth !== 'watch') found.push(['self', await selfCheck(host)])
    const day = depth === 'daily' ? await daily(host) : undefined
    if (day) found.push(['daily', day.found])

    const at = new Date().toISOString()
    const seen = new Set<string>()
    for (const [from, findings] of found) {
      for (const finding of findings) {
        seen.add(finding.key)
        const known = state.problems[finding.key]
        if (!known) logger.warn('health: %s%s', finding.title, finding.detail ? ' — ' + finding.detail : '')
        state.problems[finding.key] = { ...finding, from, since: known?.since ?? at }
      }
    }
    for (const [key, problem] of Object.entries(state.problems)) {
      if (seen.has(key) || !COVERS[depth].includes(problem.from)) continue
      delete state.problems[key]
      logger.info('health: cleared: %s (since %s)', problem.title, problem.since)
      state.resolved = [...state.resolved, { title: problem.title, since: problem.since, until: at }].slice(-10)
    }
    for (const covered of COVERS[depth]) state.checked[covered] = at
    if (day) {
      const problems = Object.values(state.problems)
      const ok = !problems.some((problem) => problem.level === 'error')
      state.days = [...state.days, { at, ok, problems: problems.map((problem) => problem.title), reviews: day.reviews }].slice(-14)
      logger.info('health: daily check %s', ok ? 'passed' : 'found: ' + problems.map((problem) => problem.title).join('; '))
    }
    host.save('health.json', state)
  }

  /** The deepest check that is due: the daily one once its time has passed, the self-check every four hours, else the watch. */
  const due = (): Depth => {
    const now = Date.now()
    const last = (depth: Depth) => Date.parse(state.checked[depth] ?? '') || 0
    const slot = new Date(now).setHours(...DAILY_AT, 0, 0)
    const daily = slot > now ? slot - 12 * HOUR : now - slot >= 12 * HOUR ? slot + 12 * HOUR : slot
    if (last('daily') < daily) return 'daily'
    return now - last('self') >= host.ms(SELF_MS) ? 'self' : 'watch'
  }

  // One check at a time: one asked for from her page waits for the one under way.
  let checking = Promise.resolve()
  const check = (depth?: Depth) => (checking = checking.then(() => run(depth ?? due()))
    .catch((error: Error) => logger.warn('health check did not finish: %s', error.message)))
  void host.ready.then(() => check(), () => {})
  host.ctx.effect(() => {
    const timer = setInterval(() => void host.ready.then(() => check(), () => {}), host.ms(WATCH_MS))
    return () => clearInterval(timer)
  }, 'kin health watch')

  const view = () => ({
    frozen: state.frozen,
    problems: Object.values(state.problems)
      .sort((a, b) => (a.level === b.level ? a.since.localeCompare(b.since) : a.level === 'error' ? -1 : 1)),
    resolved: [...state.resolved].reverse(),
    checked: state.checked,
    days: [...state.days].reverse(),
    // What is under way now (the old /busy): her reply, her heartbeat's rounds, when the minute last beat.
    now: {
      replying: host.replying(),
      rounds: [...heartbeat.rounds].map(([lane, since]) => ({ lane, name: LANE_NAMES[lane] ?? lane, since })),
      lastMinute: heartbeat.lastMinute ?? null,
    },
    worker: mind.memoryWorker,
  })

  host.route('/api/kin/health', { GET: async () => view() })
  host.route('/api/kin/health/check', {
    POST: async () => {
      await check('self')
      return view()
    },
  })
  host.route('/api/kin/health/freeze', {
    POST: async (request) => {
      const { frozen } = await host.body<{ frozen: boolean }>(request)
      if (typeof frozen !== 'boolean') throw new Error('要说清楚是冻结还是解冻')
      if (frozen !== Boolean(state.frozen)) {
        state.frozen = frozen ? { since: new Date().toISOString() } : null
        heartbeat.freeze(frozen)
        host.save('health.json', state)
        host.log(frozen ? 'frozen by the owner: her heartbeat and memory worker rest' : 'thawed by the owner')
      }
      return view()
    },
  })
}
