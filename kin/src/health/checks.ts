/**
 * What her health checks look at, each problem said for her page: what is wrong, what was seen, what to do.
 * All of it reads; nothing here changes her or calls a model. Ported from the old host's liveness rules
 * (legacy/host/health.mjs `inspectLiveness`), its self-check (core/adapters/mobile-audit.mjs), its daily
 * check (legacy/host/daily-check.mjs) and the processes that are hers on DSH.
 */
import { accessSync, constants, readFileSync, statfsSync } from 'node:fs'
import type { Transports } from '../channels/transport.js'
import type { Heartbeat } from '../heartbeat.js'
import type { Host } from '../index.js'
import type { Mind } from '../mind.js'
import { LANES, type KinModel, type Route } from '../models.js'

export interface Finding {
  /** One problem keeps its key from check to check, so it is known since it first showed. */
  key: string
  level: 'error' | 'warn'
  title: string
  detail?: string
  todo?: string
}

const HOUR = 3_600_000
/** A round still going after this long is worth a look (the longest the old host allowed one, exploring, was 25 minutes). */
const STUCK_MS = HOUR
const ROUTE_TODO = '到设置的「她用的模型」里换一条能用的路线；模型的密钥和地址在 DSH 的设置里'
const RESTART_TODO = '在 DSH 的插件页把 KIN 关掉再打开'

/** Her heartbeat's lanes as her page says them; a lane another module adds is shown by its own name. */
export const LANE_NAMES: Record<string, string> = {
  review: '静下来想事情', daily: '每日复盘', enrichment: '整理记忆', preparation: '预热回忆', exploration: '出门探索', creation: '做东西',
  contact: '主动联系',
}
const laneName = (lane: string) => LANE_NAMES[lane] ?? lane

/** The store's liveness facts (kin_mind.operational_status `liveness`), the parts read here. */
interface Facts {
  embeddings?: { failed_24h: number; last_failure: { error: string } | null; waiting: number; oldest_waiting_hours: number | null }
  quarantine?: {
    count: number; new_24h: number; recoverable: number; recoverable_new_24h: number
    hours_since_commit: number | null; reasons: Record<string, number>
  }
  contact_failures?: { streak: { code?: string; count: number; times?: string[]; last_at?: string } | null } | null
}

/** Every five minutes: is she up, and is her work moving. */
export async function watch(host: Host): Promise<Finding[]> {
  const { mind, heartbeat, model, config, ctx } = host
  const found: Finding[] = []
  let facts: Facts | undefined
  try {
    facts = await mind.call<Facts>('liveness-facts')
  } catch (error) {
    found.push({
      key: 'mind', level: 'error', title: '她的心智没有回应', detail: (error as Error).message,
      todo: '常驻进程会自己重启。一直这样就看 DSH 日志里 kin 的报错，多半是 Python 环境（core/.venv）出了问题',
    })
  }
  // Frozen, her heartbeat and memory worker rest on purpose.
  if (!heartbeat.frozen) found.push(...beating(heartbeat), ...worker(mind))
  found.push(...await bridge(mind), ...models(model), ...rounds(heartbeat))
  if (facts) {
    found.push(...liveness(facts, heartbeat.frozen))
    const due = await mind.call<{ state?: string; reason?: string }>('review-due', { tick: false }).catch(() => undefined)
    if (due?.state === 'paused') found.push(paused(due.reason, config.dataDir))
  }
  found.push(...transports(ctx.get('kinTransports')))
  const unsettled = ctx.get('kinContact')?.settlementFailures() ?? []
  if (unsettled.length) found.push({
    key: 'delivery-settlement', level: 'warn', title: `${unsettled.length} 条投递结果尚未完成记账`,
    detail: unsettled.map(({ id, reason }) => `${id}: ${reason}`).join('；'),
    todo: '会继续记账，不会重发已确认送出的消息，也不挡住其他联系；看 DSH 日志里的会话错误',
  })
  return found
}

/** Every four hours, beside the watch: what the store says of work left half done, and whether her one-shot processes start. */
export async function selfCheck({ mind }: Host): Promise<Finding[]> {
  const found: Finding[] = []
  const status = await mind.call<{ autonomy?: { stale_running_explorations?: number; expired_running_executions?: number } }>('operational-status')
    .catch(() => undefined)
  const stale = status?.autonomy?.stale_running_explorations ?? 0
  if (stale) found.push({ key: 'explorations', level: 'warn', title: `${stale} 次探索标着在跑，却已经没有进程了`, todo: RESTART_TODO + '，启动时会收拾好' })
  const expired = status?.autonomy?.expired_running_executions ?? 0
  if (expired) found.push({ key: 'executions', level: 'warn', title: `${expired} 个计划步骤过了期限还标着在跑`, todo: RESTART_TODO + '，启动时会收拾好' })
  try {
    // A process of its own, as her reviews, diary and dreams run: if this one cannot start, neither can they.
    const lifecycle = await mind.run<{ digests: Record<string, number>; digest_failures?: Record<string, number> }>('lifecycle-status')
    const failed = lifecycle.digests.failed ?? 0
    const missing = lifecycle.digest_failures?.missing_evidence ?? 0
    const exhausted = lifecycle.digest_failures?.exhausted ?? 0
    if (missing) found.push({ key: 'digests-evidence', level: 'warn', title: `${missing} 段经历缺少有效依据，摘要未生成`,
      todo: '需要检查这些经历的原始记录是否仍然有效；反复重试或换模型不能补回依据' })
    if (exhausted) found.push({ key: 'digests-exhausted', level: 'warn', title: `${exhausted} 段经历的摘要已用完自动重试`,
      todo: '查看摘要任务的具体错误，排除原因后再重试' })
    if (failed > missing + exhausted) found.push({ key: 'digests', level: 'warn', title: `${failed - missing - exhausted} 段经历的摘要没写成`,
      todo: '尚在自动重试范围内；持续失败时查看摘要任务的具体错误和模型路线' })
  } catch (error) {
    found.push({
      key: 'mind-run', level: 'error', title: '她的后台进程起不来', detail: (error as Error).message,
      todo: '评估、日记、梦都要起这样的进程。看 DSH 日志里 kin 的报错，检查 Python 环境（core/.venv）',
    })
  }
  return found
}

/** Twice a day, beside both: the disk she lives on, and how her reviews of the last day went. */
export async function daily({ mind, config }: Host): Promise<{ found: Finding[]; reviews: Record<string, number> }> {
  const found: Finding[] = []
  const { bavail, bsize } = statfsSync(config.dataDir)
  const gib = (bavail * bsize) / 1024 ** 3
  if (gib < 5) {
    found.push({
      key: 'disk', level: gib < 1 ? 'error' : 'warn', title: `磁盘只剩 ${gib.toFixed(1)} GiB`,
      todo: '清理一下磁盘：记忆库写不进去时，她会记不住新的事',
    })
  }
  try {
    accessSync(config.dataDir, constants.W_OK)
  } catch {
    found.push({ key: 'data-dir', level: 'error', title: '她的数据目录写不进去', detail: config.dataDir, todo: '检查这个目录的权限' })
  }
  // A process of its own; one that cannot start is the self-check's to say.
  const attempts = await mind.run<{ attempts: { outcome: string; charged?: boolean; finished_at?: string | null; error?: string; error_detail?: { code?: string }; waiting_reason?: string }[] }>('appraisal-attempts', { limit: 200 })
    .then((answer) => answer.attempts, () => [])
  const since = Date.now() - 24 * HOUR
  const reviews: Record<string, number> = {}
  for (const attempt of attempts) {
    const { finished_at } = attempt
    const outcome = [attempt.error, attempt.error_detail?.code, attempt.waiting_reason].includes('deepseek-foreground-priority')
      && attempt.outcome === 'failed' && attempt.charged !== true ? 'waiting' : attempt.outcome
    if (Date.parse(finished_at ?? '') >= since) reviews[outcome] = (reviews[outcome] ?? 0) + 1
  }
  const failed = (reviews.failed ?? 0) + (reviews.quarantined ?? 0) + (reviews.abandoned ?? 0)
  if (failed && failed === Object.entries(reviews).reduce((sum, [outcome, n]) => sum + (outcome === 'waiting' ? 0 : n), 0)) {
    found.push({ key: 'reviews', level: 'error', title: '这一天她一次评估也没做成', detail: `试了 ${failed} 次`, todo: '先看做事线能不能接上模型' })
  }
  return { found, reviews }
}

/** The minute that keeps her inner life going has stopped: it did not start, or its timer is gone. */
function beating(heartbeat: Heartbeat): Finding[] {
  if (heartbeat.lastMinute && Date.now() - Date.parse(heartbeat.lastMinute) < 3 * 60_000) return []
  return [{
    key: 'heartbeat', level: 'error', title: '她的心跳停了',
    detail: heartbeat.lastMinute ? undefined : '这次启动后还没跳过：启动时的恢复多半没做完',
    todo: RESTART_TODO + '；DSH 日志里搜 heartbeat 能看到原因',
  }]
}

/** eventmem's worker (memory jobs, upkeep, schedules): down now, or ending again and again. */
function worker(mind: Mind): Finding[] {
  const { pid, ends } = mind.memoryWorker
  const todo = '一直起不来就看 DSH 日志里 “mind memory worker” 的报错'
  const last = ends.at(-1)
  if (!pid) return [{ key: 'worker', level: 'error', title: '她的记忆整理停了', detail: last ? `工作者退出了（${last.how}），半分钟后会自己再起` : undefined, todo }]
  const lately = ends.filter((end) => Date.now() - Date.parse(end.at) < HOUR)
  if (lately.length < 3) return []
  return [{ key: 'worker', level: 'error', title: '她的记忆整理反复中断', detail: `一小时里退出了 ${lately.length} 次，最近一次：${last!.how}`, todo }]
}

/** Core's model calls come back through the loopback relay its model file names (bridge.ts); without a key it answers 401. */
async function bridge(mind: Mind): Promise<Finding[]> {
  try {
    const url = JSON.parse(readFileSync(mind.modelFile, 'utf8')).providers.kin.baseUrl as string
    const response = await fetch(url + '/messages', { method: 'POST', signal: AbortSignal.timeout(5_000) })
    if (response.status === 401) return []
    throw new Error(`answered ${response.status}`)
  } catch (error) {
    const reason = (error as { cause?: { code?: string } }).cause?.code ?? (error as Error).message
    return [{
      key: 'bridge', level: 'error', title: '她的后台连不上模型', detail: `评估、日记、梦和记忆整理走的中转没有回应（${reason}）`, todo: RESTART_TODO,
    }]
  }
}

/** Her two model lanes, by how their latest call went. */
function models(model: KinModel): Finding[] {
  return LANES.flatMap((lane): Finding[] => {
    const last = model.last[lane]
    if (!last?.failed.length) return []
    const name = lane === 'chat' ? '聊天线' : '做事线'
    const misses = last.failed.map((miss) => `${say(miss.route)}：${miss.reason}`).join('；')
    if (!last.answered) return [{ key: 'model:' + lane, level: 'error', title: `${name}接不上模型`, detail: misses, todo: ROUTE_TODO }]
    return [{
      key: 'model:' + lane, level: 'warn', title: `${name}前面的路线不通`, detail: `换到 ${say(last.answered)} 才接上。${misses}`,
      todo: '设置的「她用的模型」里可以调整顺序',
    }]
  })
}

/** Her heartbeat's lanes: failing round after round, or one round going on and on. */
function rounds(heartbeat: Heartbeat): Finding[] {
  const found: Finding[] = []
  for (const [lane, beat] of Object.entries(heartbeat.beats)) {
    if ((beat?.failures ?? 0) < 3) continue
    found.push({
      key: 'lane:' + lane, level: 'error', title: `「${laneName(lane)}」连续 ${beat!.failures} 次没做成`, detail: beat!.reason,
      todo: '多半是做事线接不上模型；DSH 日志里有每次的原因',
    })
  }
  for (const [lane, since] of heartbeat.rounds) {
    const minutes = Math.round((Date.now() - Date.parse(since)) / 60_000)
    if (minutes * 60_000 < STUCK_MS) continue
    found.push({ key: 'stuck:' + lane, level: 'warn', title: `「${laneName(lane)}」做了 ${minutes} 分钟还没完`, todo: '再等等；一直不动就' + RESTART_TODO })
  }
  return found
}

/** The store's own rules for work that does not move (the old host's OPS-02 limits). */
function liveness({ embeddings, quarantine, contact_failures }: Facts, frozen: boolean): Finding[] {
  const found: Finding[] = []
  const embedTodo = '看看本地嵌入服务；没有它时她按字词回忆，聊天不受影响'
  if (embeddings?.failed_24h) {
    found.push({
      key: 'embeddings-failed', level: 'warn', title: '有记忆没能向量化',
      detail: `这一天失败 ${embeddings.failed_24h} 次${embeddings.last_failure ? '：' + embeddings.last_failure.error : ''}`, todo: embedTodo,
    })
  }
  // Frozen, the memory worker rests and its jobs wait on purpose.
  if (!frozen && embeddings?.waiting && (embeddings.oldest_waiting_hours ?? 0) >= 0.5) {
    found.push({
      key: 'embeddings-stalled', level: 'error', title: '记忆向量化卡住了',
      detail: `${embeddings.waiting} 条在等，最早的等了 ${embeddings.oldest_waiting_hours} 小时`, todo: embedTodo,
    })
  }
  if (quarantine?.count) {
    const reasons = Object.entries(quarantine.reasons).map(([reason, n]) => `${reason} ×${n}`).join('、')
    // Rows only an outage set aside go back by themselves after the next review that commits.
    const burst = quarantine.new_24h - quarantine.recoverable_new_24h >= 5
    found.push(burst
      ? { key: 'quarantine', level: 'error', title: '她的评估接连被搁置', detail: `这一天有 ${quarantine.new_24h} 次（${reasons}）`, todo: '先看做事线能不能接上模型' }
      : {
          key: 'quarantine', level: 'warn', title: `有 ${quarantine.count} 次评估被搁置`, detail: reasons,
          todo: quarantine.recoverable === quarantine.count ? '做事线恢复后，它们会自己放回去'
            : '可以在终端用 Core 的 appraisal-triage 看看哪些该放回、哪些该结束',
        })
    if (quarantine.recoverable && (quarantine.hours_since_commit ?? 0) >= 12) {
      found.push({
        key: 'reviews-stalled', level: 'error', title: `她已经 ${Math.round(quarantine.hours_since_commit!)} 小时没想完一件事`,
        detail: `${quarantine.recoverable} 次评估在等做事线恢复`, todo: ROUTE_TODO,
      })
    }
  }
  // Three technical failures of the same kind within six hours, the last within two (the old host's `contact-failing`).
  const streak = contact_failures?.streak
  const last = Date.parse(streak?.last_at ?? '')
  const within = (streak?.times ?? []).filter((at) => last - Date.parse(at) <= 6 * HOUR).length
  if (streak && Date.now() - last <= 2 * HOUR && within >= 3) {
    found.push({
      key: 'contact-failing', level: 'error', title: '她想找你，接连没发出去', detail: `连续 ${streak.count} 次因为 ${streak.code ?? '技术故障'} 没发出`,
      todo: '看看聊天线的模型和渠道能不能用',
    })
  }
  return found
}

/** Core holds her reviews itself: an operator's pause in mind-config.json, or history being compacted. */
function paused(reason: string | undefined, dataDir: string): Finding {
  return reason === 'host-maintenance'
    ? { key: 'review-paused', level: 'warn', title: '她的评估被暂停了', detail: 'mind-config.json 里打开了 review_paused', todo: `去掉 ${dataDir}/mind-config.json 里的 review_paused` }
    : { key: 'review-paused', level: 'warn', title: '她在整理过去的记录，评估先停一停', detail: reason, todo: '整理完会自己接着来' }
}

/** WeChat and Feishu, once her channels are there (the `kinTransports` service). */
function transports(running: Transports | undefined): Finding[] {
  return ([['wechat', '微信'], ['feishu', '飞书']] as const).flatMap(([id, name]): Finding[] => {
    const status = running?.get(id)?.status()
    if (status?.state === 'failed') return [{ key: 'transport:' + id, level: 'error', title: `${name}断开了`, detail: status.reason, todo: `到渠道设置里重新连接${name}` }]
    if (status?.state === 'login-required') return [{ key: 'transport:' + id, level: 'warn', title: `${name}要重新登录`, todo: '到渠道设置里扫码登录' }]
    return []
  })
}

const say = (route: Route) => `${route.provider}/${route.model}`
