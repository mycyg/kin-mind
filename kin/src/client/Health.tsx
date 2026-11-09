/**
 * Her health, quietly: on her settings page whether all is well, what is wrong since when and what to do,
 * what is under way, the freeze switch and when she was last looked over; on 此刻 one line, only when
 * she is frozen or something needs a look.
 */
import { useState } from 'react'
import { api, useApi } from './api.js'
import { ago, clock, day } from './time.js'

interface Problem { key: string; level: 'error' | 'warn'; title: string; detail?: string; todo?: string; since: string }
interface View {
  frozen: { since: string } | null
  problems: Problem[]
  resolved: { title: string; since: string; until: string }[]
  checked: { watch?: string; self?: string; daily?: string }
  days: { at: string; ok: boolean; problems: string[]; reviews: Record<string, number> }[]
  now: { replying: boolean; delivering: boolean; rounds: { lane: string; name: string; since: string }[]; lastMinute: string | null }
}

const ROUTE = 'api/kin/health'
const when = (at: string) => `${day(at)} ${clock(at)}`
const minutes = (since: string) => `${Math.max(1, Math.round((Date.now() - Date.parse(since)) / 60_000))} 分钟`

export function Health() {
  const { data, error, reload } = useApi<View>(ROUTE, 30_000)
  const [checking, setChecking] = useState(false)
  const [problem, setProblem] = useState<string>()
  const ask = (route: string, body: object) => api(route, body).then(reload, (reason: Error) => setProblem(reason.message))
  const title = <h3 className="kin-card-title">她的状况</h3>
  if (!data) {
    return (
      <section className="kin-card">
        {title}
        <p className="kin-quiet">{error ? `读不出来：${error}。她的心智没醒来时就会这样，看 DSH 日志里 kin 的报错` : '正在看…'}</p>
      </section>
    )
  }
  const { frozen, problems, resolved, checked, days, now } = data
  const errors = problems.filter((p) => p.level === 'error').length
  const [latest] = days
  const streak = days.findIndex((d) => d.ok)
  const misses = streak === -1 ? days.length : streak
  const doing = [now.replying ? '回你的话' : '', now.delivering ? '发送消息、等待回执' : '', ...now.rounds.map((round) => `${round.name}（${minutes(round.since)}）`)].filter(Boolean)

  return (
    <section className="kin-card kin-health" data-level={errors ? 'error' : problems.length ? 'warn' : 'fine'}>
      {title}
      <div className="kin-health-head">
        <i className="kin-health-dot" aria-hidden="true" />
        <div>
          <p>{problems.length ? `有 ${problems.length} 件事要看看` : '一切安好'}</p>
          <span className="kin-dim kin-health-times">
            {checked.watch ? <span>5 分钟一看 · {ago(checked.watch)}</span> : null}
            {checked.self ? <span>自检 · {ago(checked.self)}</span> : null}
          </span>
        </div>
        <button type="button" className="kin-btn" disabled={checking}
          onClick={() => { setChecking(true); void ask(`${ROUTE}/check`, {}).finally(() => setChecking(false)) }}>
          {checking ? '自检中…' : '现在自检'}
        </button>
      </div>

      {problems.length ? (
        <ul className="kin-list kin-problems">
          {problems.map((p) => (
            <li key={p.key} data-level={p.level}>
              <div className="kin-problem-head"><strong>{p.title}</strong><span className="kin-dim">{when(p.since)} 起</span></div>
              {p.detail ? <p className="kin-problem-detail">{p.detail}</p> : null}
              {p.todo ? <p className="kin-problem-todo">怎么办：{p.todo}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="kin-switch-row">
        <div>
          <p>{frozen ? `冻结着，从${when(frozen.since)} 起` : '冻结她的后台'}</p>
          <span className="kin-dim">冻结后她不再自己想事情、整理记忆、出门探索或主动找你，重启后也一样；和你聊天照常</span>
        </div>
        <button type="button" role="switch" aria-checked={Boolean(frozen)} aria-label="冻结她的后台" className="kin-switch"
          onClick={() => void ask(`${ROUTE}/freeze`, { frozen: !frozen })} />
      </div>
      {problem ? <p className="kin-error">{problem}</p> : null}

      <p className="kin-health-line">
        {doing.length ? `${frozen ? '冻结前开始的还在收尾：' : '此刻在'}${doing.join('、')}` : '此刻没在忙'}
        {!frozen && now.lastMinute ? ` · 心跳 ${ago(now.lastMinute)}` : ''}
      </p>
      {latest ? (
        <p className="kin-health-line">
          每日检查 · {when(latest.at)} {latest.ok ? '通过' : '没通过'}{misses >= 2 ? `，已经连续 ${misses} 次` : ''}
          {' · '}{reviewsOf(latest.reviews)}
        </p>
      ) : null}
      {resolved.length ? (
        <details className="kin-health-past">
          <summary>最近好了的 {resolved.length} 件</summary>
          <ul className="kin-list">
            {resolved.map((r) => <li key={r.until + r.title}>{r.title}<span className="kin-dim">　{when(r.since)} — {day(r.until) === day(r.since) ? clock(r.until) : when(r.until)}</span></li>)}
          </ul>
        </details>
      ) : null}
    </section>
  )
}

/** One quiet line under 此刻, only when she is frozen or something is wrong. */
export function HealthNote() {
  const { data } = useApi<View>(ROUTE, 60_000)
  const errors = data?.problems.filter((p) => p.level === 'error').length ?? 0
  const line = [data?.frozen ? `她的后台从${when(data.frozen.since)} 起冻结着` : '', errors ? `有 ${errors} 件事要看看，在设置里` : '']
    .filter(Boolean).join(' · ')
  return line ? <><br /><span className="kin-health-note">{line}</span></> : null
}

/** How her reviews of the last day ended. */
function reviewsOf(reviews: Record<string, number>): string {
  const done = (reviews.committed ?? 0) + (reviews.reused ?? 0) + (reviews.revalidated ?? 0)
  const missed = (reviews.failed ?? 0) + (reviews.quarantined ?? 0) + (reviews.abandoned ?? 0)
  const waiting = reviews.waiting ?? 0
  if (!done && !missed) return waiting ? `过去一天有 ${waiting} 次评估在等聊天结束` : '过去一天没有要想的事'
  return `过去一天想完 ${done} 件事${missed ? `，${missed} 次没想成` : ''}${waiting ? `，${waiting} 次等待` : ''}`
}
