/**
 * 此刻: how she is right now. Up top the feeling that stands out most, in a big number; the rest of
 * her heart at a glance; what she last made of things, in her own words, and when she means to
 * think again; what is left of today on her calendar; what is on her mind and what she wants.
 */
import { useState } from 'react'
import { activeWish, openConcern, type Beat, type Dimension, type Home } from './api.js'
import { Today } from './Calendar.js'
import { Contact } from './Contact.js'
import { HealthNote } from './Health.js'
import { Bubble, ChevronDown, Waves } from './icons.js'
import { ago, soon } from './time.js'
import { CONCERN_KINDS, Wishes, type Place } from './World.js'
import { Work } from './steward/Work.js'

export function Now({ home, error, go, talk }: { home: Home | undefined; error?: string; go: (place: Place) => void; talk?: (text: string) => void }) {
  const [all, setAll] = useState(false)
  const [showEasing, setShowEasing] = useState(false)
  if (error) {
    return <div className="kin-scroll"><p className="kin-quiet kin-center">{`她的心智还没醒来：${error}`}</p></div>
  }
  if (!home) {
    return (
      <div className="kin-scroll kin-skeletons" aria-label="正在看她此刻的样子">
        <div className="kin-hero kin-skeleton-hero">
          <span className="kin-skeleton-pill" style={{ width: 64 }} />
          <span className="kin-skeleton-num" />
          <span className="kin-skeleton-line" style={{ width: '48%' }} />
        </div>
        <div className="kin-card kin-skeleton-card">
          <div className="kin-skeleton-grid">
            {Array.from({ length: 8 }, (_, idx) => <span key={idx} className="kin-skeleton-gauge" />)}
          </div>
        </div>
        <div className="kin-card kin-skeleton-card">
          <span className="kin-skeleton-line" style={{ width: '32%' }} />
          <span className="kin-skeleton-line" style={{ width: '88%' }} />
          <span className="kin-skeleton-line" style={{ width: '74%' }} />
        </div>
      </div>
    )
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
  const activeConcerns = concerns.filter((c) => c.status !== 'easing')
  const easingConcerns = concerns.filter((c) => c.status === 'easing')
  const easingOmitted = state.easing_omitted ?? 0
  const easingTotal = easingConcerns.length + easingOmitted
  const wishes = state.desires.filter(activeWish)
  const decisions = Object.values(liveness.contact_funnel?.idle_decisions_24h ?? {}).reduce((sum, n) => sum + n, 0)
  const statusLine = [`此刻${feeling}${undertone && undertone !== feeling ? `，心底${undertone}` : ''}`, rhythm].filter(Boolean).join(' · ')

  return (
    <div className="kin-scroll">
      <header className="kin-hero">
        <span className="kin-script">Now</span>
        {hero ? <strong className="kin-numeral">{Math.round(hero.value)}</strong> : null}
        <span className="kin-hero-label">{hero?.label ?? feeling}</span>
        <span className="kin-hero-sub">
          {statusLine}
          {pulse ? <>{statusLine ? ' · ' : ''}<span className="kin-pulse-wrap"><i className="kin-pulse-dot" aria-hidden="true" />心跳 {pulse}</span></> : null}
        </span>
      </header>

      <section className="kin-card" aria-label="她心里的各种感觉">
        <GaugeBoard items={standout.slice(0, 8)} bare />
      </section>
      <button type="button" className="kin-link kin-more" onClick={() => setAll(!all)}>{all ? '收起' : '看看每一样'}</button>
      {all ? state.dimension_groups.map((group) => (
        <section key={group.id} className="kin-card">
          <h3 className="kin-card-title">{group.label}</h3>
          <GaugeBoard items={group.members.map((key) => state.dimensions[key]).filter(Boolean) as Dimension[]} bare />
        </section>
      )) : null}

      {thought?.reason ? (
        <article className="kin-paper kin-thought">
          <h3><Waves size={17} />她刚想到的</h3>
          <p>{thought.reason}</p>
          <footer>
            <span>{thought.at ? `${ago(thought.at)}静下来想过` : ''}</span>
            {home.nextReview ? <span>她说 {soon(home.nextReview)}再想</span> : null}
            {talk ? (
              <button type="button" className="kin-say-btn" onClick={() => talk(`看到你刚想到的事：「${thought.reason}」，想和你聊聊这个`)}>
                <Bubble size={13} /> 和她说说
              </button>
            ) : null}
          </footer>
        </article>
      ) : null}

      <Today open={() => go('calendar')} />
      <section className="kin-card">
        <h3 className="kin-card-title">心事</h3>
        {activeConcerns.length ? (
          <ul className="kin-list kin-list-scroll" tabIndex={0} aria-label="心事列表">
            {activeConcerns.map((concern) => (
              <li key={concern.id} className="kin-list-item">
                <span className="kin-list-body">
                  {concern.kind ? <span className="kin-tag">{CONCERN_KINDS[concern.kind] ?? concern.kind}</span> : null}
                  {concern.content ?? concern.topic}
                </span>
                {talk ? (
                  <button type="button" className="kin-say-btn" onClick={() => talk(`看到你在挂念「${concern.content ?? concern.topic}」，想和你聊聊`)}>
                    <Bubble size={13} /> 和她说说
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : <p className="kin-quiet">{easingTotal ? '此刻没有新挂着的心事' : '心里暂时没有挂着的事'}</p>}
        {easingTotal > 0 ? (
          <>
            <button type="button" className="kin-link kin-easing-toggle" onClick={() => setShowEasing((v) => !v)}>
              <ChevronDown size={13} className={showEasing ? 'kin-rot' : undefined} />
              {showEasing ? '收起正在慢慢放下的心事' : `还有 ${easingTotal} 件正在慢慢放下的心事`}
            </button>
            {showEasing ? (
              <>
                <ul className="kin-list kin-list-scroll kin-list-easing" tabIndex={0} aria-label="慢慢放下的心事">
                  {easingConcerns.map((concern) => (
                    <li key={concern.id} className="kin-list-item">
                      <span className="kin-list-body">
                        {concern.kind ? <span className="kin-tag">{CONCERN_KINDS[concern.kind] ?? concern.kind}</span> : null}
                        {concern.content ?? concern.topic}
                      </span>
                      {talk ? (
                        <button type="button" className="kin-say-btn" onClick={() => talk(`想起你之前挂心的「${concern.content ?? concern.topic}」，现在好些了吗？`)}>
                          <Bubble size={13} /> 和她说说
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
                {easingOmitted > 0 ? <p className="kin-dim kin-easing-more">其余 {easingOmitted} 件早先的挂念已悄悄沉淀下来</p> : null}
              </>
            ) : null}
          </>
        ) : null}
      </section>
      <section className="kin-card">
        <h3 className="kin-card-title">心愿</h3>
        {wishes.length ? <Wishes wishes={wishes} talk={talk} /> : <p className="kin-quiet">还没有特别想做的事</p>}
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

function GaugeBoard({ items, bare }: { items: Dimension[]; bare?: boolean }) {
  const [picked, setPicked] = useState<string>()
  const active = items.find((item) => item.label === picked && item.reason)
  return (
    <>
      <div className="kin-gauges" data-bare={bare ? '' : undefined}>
        {items.map((dimension) => (
          <Gauge key={dimension.label} {...dimension} selected={active?.label === dimension.label}
            onSelect={dimension.reason ? () => setPicked((now) => (now === dimension.label ? undefined : dimension.label)) : undefined} />
        ))}
      </div>
      {active?.reason ? <p className="kin-gauge-why"><strong>{active.label}：</strong>{active.reason}</p> : null}
    </>
  )
}

/** One feeling: its number, baseline delta, a thin bar with where it usually sits, its name. */
function Gauge({ label, value, baseline, reason, selected, onSelect }: Pick<Dimension, 'label' | 'value' | 'baseline' | 'reason'> & { selected?: boolean; onSelect?: () => void }) {
  const delta = Math.round(value - baseline)
  const deltaText = Math.abs(delta) >= 1 ? (delta > 0 ? `+${delta}` : `${delta}`) : null
  return (
    <button type="button" className="kin-gauge" title={reason} data-selected={selected ? '' : undefined}
      data-clickable={reason ? '' : undefined} disabled={!reason} onClick={onSelect}>
      <span className="kin-gauge-top">
        <span className="kin-gauge-num">{Math.round(value)}</span>
        {deltaText ? <span className="kin-gauge-delta" data-sign={delta > 0 ? 'up' : 'down'}>{deltaText}</span> : null}
      </span>
      <span className="kin-bar"><i style={{ width: `${value}%` }} /><b style={{ left: `${baseline}%` }} /></span>
      <span className="kin-gauge-name">{label}</span>
    </button>
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
