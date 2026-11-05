/**
 * 此刻: how she is right now. Up top the feeling that stands out most, in a big number; the rest of
 * her heart at a glance; what she last made of things, in her own words, and when she means to
 * think again; what is on her mind and what she wants.
 */
import { useState } from 'react'
import { activeWish, openConcern, type Beat, type Dimension, type Home } from './api.js'
import { Contact } from './Contact.js'
import { HealthNote } from './Health.js'
import { Waves } from './icons.js'
import { ago, soon } from './time.js'
import { CONCERN_KINDS, Wishes } from './World.js'
import { Work } from './steward/Work.js'

export function Now({ home, error }: { home: Home | undefined; error?: string }) {
  const [all, setAll] = useState(false)
  if (!home) {
    return <div className="kin-scroll"><p className="kin-quiet kin-center">{error ? `她的心智还没醒来：${error}` : '正在看她此刻的样子…'}</p></div>
  }
  const { state, liveness } = home
  const feeling = home.overview.affect_layers?.feeling ?? state.affect_layers?.feeling?.text ?? '平静'
  const undertone = home.overview.affect_layers?.undertone?.text ?? state.affect_layers?.undertone?.text
  const dimensions = Object.entries(state.dimensions).map(([key, dimension]) => ({ key, ...dimension, lift: dimension.value - dimension.baseline }))
  const standout = [...dimensions].sort((a, b) => Math.abs(b.lift) - Math.abs(a.lift))
  // The number up top is whatever stands out most right now; on a quiet day, her mood.
  const hero = (standout[0] && Math.abs(standout[0].lift) >= 8 ? standout[0] : dimensions.find((d) => d.key === 'mood')) ?? standout[0]
  const rhythm = state.rhythm?.status && state.rhythm.status !== 'disabled' ? state.rhythm.phase : undefined
  const pulse = state.affect_layers?.vitals?.status === 'rhythm-disabled' ? undefined : state.affect_layers?.vitals?.heart_rate_bpm
  const thought = state.appraisal_summary
  const concerns = (state.concerns ?? []).filter(openConcern)
  const wishes = state.desires.filter(activeWish)
  const decisions = Object.values(liveness.contact_funnel?.idle_decisions_24h ?? {}).reduce((sum, n) => sum + n, 0)

  return (
    <div className="kin-scroll">
      <header className="kin-hero">
        <span className="kin-script">Now</span>
        {hero ? <strong className="kin-numeral">{Math.round(hero.value)}</strong> : null}
        <span className="kin-hero-label">{hero?.label ?? feeling}</span>
        <span className="kin-hero-sub">
          {[`此刻${feeling}${undertone && undertone !== feeling ? `，心底${undertone}` : ''}`, rhythm, pulse ? `心跳 ${pulse}` : '']
            .filter(Boolean).join(' · ')}
        </span>
      </header>

      <section className="kin-card kin-gauges" aria-label="她心里的各种感觉">
        {standout.slice(0, 8).map(({ key, ...dimension }) => <Gauge key={key} {...dimension} />)}
      </section>
      <button type="button" className="kin-link kin-more" onClick={() => setAll(!all)}>{all ? '收起' : '看看每一样'}</button>
      {all ? state.dimension_groups.map((group) => (
        <section key={group.id} className="kin-card">
          <h3 className="kin-card-title">{group.label}</h3>
          <div className="kin-gauges" data-bare="">
            {group.members.map((key) => state.dimensions[key]).filter(Boolean).map((dimension) => <Gauge key={dimension!.label} {...dimension!} />)}
          </div>
        </section>
      )) : null}

      {thought?.reason ? (
        <article className="kin-paper kin-thought">
          <h3><Waves size={17} />她刚想到的</h3>
          <p>{thought.reason}</p>
          <footer>
            <span>{thought.at ? `${ago(thought.at)}静下来想过` : ''}</span>
            {home.nextReview ? <span>她说 {soon(home.nextReview)}再想</span> : null}
          </footer>
        </article>
      ) : null}

      <section className="kin-card">
        <h3 className="kin-card-title">心事</h3>
        {concerns.length ? (
          <ul className="kin-list kin-list-scroll" tabIndex={0} aria-label="心事列表">
            {concerns.map((concern) => (
              <li key={concern.id}>
                {concern.kind ? <span className="kin-tag">{CONCERN_KINDS[concern.kind] ?? concern.kind}</span> : null}
                {concern.content ?? concern.topic}
              </li>
            ))}
          </ul>
        ) : <p className="kin-quiet">心里暂时没有挂着的事</p>}
      </section>
      <section className="kin-card">
        <h3 className="kin-card-title">心愿</h3>
        {wishes.length ? <Wishes wishes={wishes} /> : <p className="kin-quiet">还没有特别想做的事</p>}
      </section>
      <Contact />
      <Work />

      <p className="kin-footnote">
        {liveness.last?.contact_sent_at ? `上次主动找你：${ago(liveness.last.contact_sent_at)}` : '她还没有主动找过你'}
        {decisions ? ` · 这一天里想过 ${decisions} 次要不要找你` : ''}
        <br /><Heartbeat beats={home.heartbeat} thought={Boolean(thought?.reason)} />
        <HealthNote />
      </p>
    </div>
  )
}

/** One feeling: its number, a thin bar with where it usually sits, its name. */
function Gauge({ label, value, baseline, reason }: Pick<Dimension, 'label' | 'value' | 'baseline' | 'reason'>) {
  return (
    <div className="kin-gauge" title={reason}>
      <span className="kin-gauge-num">{Math.round(value)}</span>
      <span className="kin-bar"><i style={{ width: `${value}%` }} /><b style={{ left: `${baseline}%` }} /></span>
      <span className="kin-gauge-name">{label}</span>
    </div>
  )
}

/** Her heartbeat in a line: when she last stopped to think (unless her thought above already says), what she is doing about it now, and what else she got done. */
function Heartbeat({ beats, thought }: { beats: Home['heartbeat']; thought: boolean }) {
  const { review, daily, enrichment } = beats
  const done = (beat: Beat | undefined, what: string) => (beat?.done ? `${ago(beat.done)}${what}` : '')
  const last = thought ? '' : done(review, '静下来想过一遍') || '她还没静下来想过事情'
  const now = !review || review.state === 'complete' || review.state === 'idle' ? ''
    : review.state === 'running' ? '她正静下来想事情'
    : review.state === 'paused' ? '她先停一停'
    : review.state === 'failed' ? '上次想的时候卡住了'
    // Core holds her background thinking while a conversation is going on (a recall's foreground lease).
    : review.reason === 'deepseek-foreground-priority' ? '等你们聊完她再想'
    : '在等合适的时候想'
  const line = [last, now, done(daily, '回顾了这一天'), done(enrichment, '整理了记忆')].filter(Boolean)
  return line.length ? <span title={review?.reason}>{line.join(' · ')}</span> : null
}
