/**
 * 此刻: whom she reaches out to and why not, in words. Each person and group with what held her back this
 * minute (her own choice, or the owner's rules), what has gone quiet too long, and what she sent lately.
 */
import { useApi } from './api.js'
import { OWNER, type Rules } from './People.js'
import { ago, clock, soon } from './time.js'

interface Reach { state: string; reason?: string; at: string }
interface Delivery {
  id: string; kind: string; name: string; state: string; at: string; excerpt?: string; notBefore?: string; checkAt?: string; reason?: string
}
interface Contact {
  rules: Rules
  status: Record<string, Reach>
  activity: Record<string, { reached?: string[] }>
  alerts: string[]
  deliveries: Delivery[]
  people: Record<string, { name: string; group: boolean }>
}

// Why she did not reach someone this minute, said the way she would.
const WHY: Record<string, string> = {
  off: '你让她先别主动找人',
  'proactive-off': '没让她主动找',
  'not-acquainted': '还没在哪儿说过话',
  'quiet-hours': '安静时段，她不出声',
  'waiting-for-reply': '在等回上一条',
  'just-talked': '刚聊过不久',
  'enough-today': '今天找得够多了',
  paused: '接连出了几次技术问题，先缓一缓',
  'no-actionable-desire': '心里暂时没有想说的',
  'action-appraisal-pending': '还在想要不要说',
  'attempt-in-progress': '有一条正在发',
  'draft-decision': '想了想，决定先不说',
  'chose-to-wait': '想了想，没插话',
  'nothing-new': '群里没有新话',
  'draft-failed': '想说的话没写成',
  'Draft or delivery conditions changed': '对方刚说了话，先听着',
}
const KINDS: Record<string, string> = { reply: '回复', contact: '主动', chime: '插话', relay: '转告', reminder: '提醒', notice: '系统通知' }
const STATES: Record<string, string> = { pending: '等着发', accepted: '送到了', unknown: '待确认', cancelled: '没发出', 'needs-review': '只发出一部分' }

export function Contact() {
  const { data } = useApi<Contact>('api/kin/contact', 30_000)
  if (!data) return null
  const targets = Object.entries(data.people)
  const sent = data.deliveries.filter((delivery) => delivery.kind !== 'reply').slice(0, 6)
  return (
    <section className="kin-card kin-reach">
      <h3 className="kin-card-title">主动联系</h3>
      {data.alerts.length ? <p className="kin-reach-alert">{data.alerts.join(' · ')}</p> : null}
      <ul className="kin-list kin-list-scroll" tabIndex={0}>
        {targets.map(([id, { name, group }]) => {
          const status = data.status[id]
          const last = data.activity[id]?.reached?.at(-1)
          const note = !status ? '还没轮到她想'
            : status.state === 'complete' ? '刚刚找过'
            : status.state === 'failed' ? `出了点问题：${status.reason ?? ''}`
            : group && status.reason === 'proactive-off' ? '只在有人 @ 她时说话'
            : WHY[status.reason ?? ''] ?? status.reason ?? '在等合适的时候'
          return (
            <li key={id} className="kin-reach-row">
              <span className="kin-reach-name">{name}{group ? <span className="kin-tag">群</span> : id === OWNER ? <span className="kin-tag">你</span> : null}</span>
              <span className="kin-dim" title={status?.reason}>{note}</span>
              <span className="kin-dim">{last ? `上次 ${ago(last)}` : ''}</span>
            </li>
          )
        })}
      </ul>
      {sent.length ? (
        <ul className="kin-list kin-sent kin-list-scroll" tabIndex={0}>
          {sent.map((delivery) => (
            <li key={delivery.id}>
              <span className="kin-tag">{KINDS[delivery.kind] ?? delivery.kind}</span>
              <span>给{delivery.name}</span>
              {delivery.excerpt ? <span className="kin-sent-words">「{delivery.excerpt}」</span> : null}
              <span className="kin-dim"> · {STATES[delivery.state] ?? delivery.state}
                {delivery.state === 'pending' && delivery.notBefore ? `，${soon(delivery.notBefore)}发` : ''}
                {delivery.state === 'unknown' && delivery.checkAt ? `，${soon(delivery.checkAt)}再问` : ''}
                {' · '}{clock(delivery.at)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <p className="kin-dim kin-reach-rules">
        {data.rules.enabled ? `安静时段 ${data.rules.quietStart}:00–${data.rules.quietEnd}:00 · 每人每天最多 ${data.rules.maxPerDay} 次` : '你让她先别主动找人'}
      </p>
    </section>
  )
}
