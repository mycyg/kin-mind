/** KIN's settings, on her own page: who she is, which models she speaks and works with, which inner-life parts are on. */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, useApi, type Feature, type Route, type Settings as Data } from './api.js'
import { Channels } from './Channels.js'
import { Health } from './Health.js'
import { Close, Grip } from './icons.js'
import { ContactRules, People } from './People.js'
import { Permission } from './steward/Permission.js'
import { TalkSettings } from './Talk.js'
import { ago } from './time.js'

type Lane = keyof Data['models']['lanes']
type Catalog = Data['models']['catalog']

const LANES: { lane: Lane; title: string; hint: string; answered: string }[] = [
  { lane: 'chat', title: '聊天的她', hint: '回你消息时用，挑有感情、会说话的', answered: '刚刚是它在和你说话' },
  { lane: 'task', title: '做事的她', hint: '帮手、探索、心跳和后台整理用，挑稳、能干活的', answered: '刚刚是它在做事' },
]

// How hard a route thinks before answering. Unset, the chat lane thinks lightly (models.ts).
const EFFORTS: Record<string, string> = { off: '不想', low: '少想', medium: '想一想', high: '多想', max: '使劲想' }

const FEATURES: { key: Feature; title: string; hint: string }[] = [
  { key: 'dreams', title: '会做梦', hint: '安静的夜里她也许会做个梦，第二天还记得' },
  { key: 'diary_replies', title: '日记可以回复', hint: '你可以在她的日记下面回她，她会读到' },
  { key: 'sealed_entries', title: '时光信与暗房', hint: '写一封到某天才能拆的信；她也能把日记锁到某天' },
  { key: 'anniversaries', title: '记得纪念日', hint: '记得你们之间重要的日子' },
  { key: 'window_notes', title: '这一段的我们', hint: '一段聊天过去后，她给这一段写个小记' },
]

export function Settings() {
  const { data, error, reload } = useApi<Data>('api/kin/settings')
  const head = (
    <header className="kin-collection-head">
      <h2 className="kin-script">Settings</h2>
      <span>她是谁、用哪些模型、心里开着什么、认识谁</span>
    </header>
  )
  if (error) return <div className="kin-scroll kin-settings">{head}<p className="kin-quiet">设置没读出来：{error}</p></div>
  if (!data) return <div className="kin-scroll kin-settings">{head}<p className="kin-quiet">正在打开设置…</p></div>
  return (
    <div className="kin-scroll kin-settings">
      {head}
      <Persona text={data.persona} />
      <TalkSettings />
      <section className="kin-card">
        <h3 className="kin-card-title">她用的模型</h3>
        <p className="kin-hint">每条线按顺序试：开始回复前超时或报错，就换下一个。拖动排序；模型的密钥和地址在 DSH 的设置里配。</p>
        {data.models.budget && <p className="kin-hint">上下文预算 {data.models.budget.inputWindow.toLocaleString()} tokens，约 {data.models.budget.compactAt.toLocaleString()} 时压缩（90%）。回复空间另行预留；模型实际容量较小时会提前压缩。</p>}
        {LANES.map((lane) => (
          <RouteLane key={lane.lane} {...lane} data={data.models.lanes[lane.lane]} catalog={data.models.catalog} onSaved={reload} />
        ))}
      </section>
      <Permission />
      <Features features={data.features} onSaved={reload} />
      <Channels />
      <People />
      <ContactRules />
      <Health />
      <p className="kin-footnote">她的心智住在 {data.dataDir}</p>
    </div>
  )
}

function Persona({ text: loaded }: { text: string }) {
  // What her host has: the text the page loaded, then whatever was saved here since.
  const [saved, setSaved] = useState(loaded)
  const [text, setText] = useState(loaded)
  const [state, setState] = useState<'idle' | 'saved' | string>('idle')
  useEffect(() => { setSaved(loaded); setText(loaded) }, [loaded])
  const save = () => api('api/kin/persona', { text })
    .then(() => { setSaved(text); setState('saved') })
    .catch((reason: Error) => setState(reason.message))
  return (
    <section className="kin-card">
      <h3 className="kin-card-title">她是谁</h3>
      <textarea className="kin-persona" value={text} rows={10} aria-label="KIN 的人设"
        onChange={(event) => { setText(event.target.value); setState('idle') }} />
      <div className="kin-row-end">
        <span className="kin-dim">{state === 'saved' ? '记住啦，从她的下一句话开始' : state !== 'idle' ? state : ''}</span>
        <button type="button" className="kin-btn" data-primary="" onClick={save} disabled={text.trim() === saved.trim()}>保存</button>
      </div>
    </section>
  )
}

function RouteLane({ lane, title, hint, answered, data, catalog, onSaved }: {
  lane: Lane; title: string; hint: string; answered: string
  data: Data['models']['lanes'][Lane]; catalog: Catalog; onSaved: () => void
}) {
  const [routes, setRoutes] = useState(data.routes)
  const [dragging, setDragging] = useState<number | null>(null)
  const [choice, setChoice] = useState('')
  const [problem, setProblem] = useState<string>()
  const latest = useRef(routes)
  latest.current = routes
  const from = useRef<number | null>(null)
  const rows = useRef(new Map<string, HTMLLIElement>())
  const before = useRef(new Map<string, DOMRect>())
  useEffect(() => setRoutes(data.routes), [data.routes])

  // Rows glide from where they were to where they are now.
  useLayoutEffect(() => {
    for (const [key, row] of rows.current) {
      const was = before.current.get(key)
      const now = row.getBoundingClientRect()
      if (was && Math.abs(was.top - now.top) > 1) {
        row.animate([{ transform: `translateY(${was.top - now.top}px)` }, { transform: 'none' }],
          { duration: 180, easing: 'cubic-bezier(.2,.8,.3,1)' })
      }
    }
    before.current.clear()
  }, [routes])

  const save = (next: Route[]) => api('api/kin/models', { lane, routes: next })
    .then(() => { setProblem(undefined); onSaved() })
    .catch((reason: Error) => setProblem(reason.message))
  const move = (index: number, to: number): Route[] | undefined => {
    if (to < 0 || to >= latest.current.length || index === to) return
    for (const [key, row] of rows.current) before.current.set(key, row.getBoundingClientRect())
    const next = [...latest.current]
    next.splice(to, 0, ...next.splice(index, 1))
    setRoutes(next)
    return next
  }
  const changed = () => keyOf(latest.current) !== keyOf(data.routes)
  const add = () => {
    if (!choice) return
    const [provider, model] = JSON.parse(choice) as [string, string]
    const next = [...routes, { provider, model }]
    setRoutes(next)
    setChoice('')
    void save(next)
  }
  const has = (provider: string, model: string) => routes.some((r) => r.provider === provider && r.model === model)
  const failed = (route: Route) => data.last?.failed.find((f) => same(f.route, route))
  // 测一下: a tiny call through exactly this route; what it said stays on the row.
  const [tests, setTests] = useState<Record<string, 'testing' | Probe>>({})
  const test = (key: string, route: Route) => {
    setTests((all) => ({ ...all, [key]: 'testing' }))
    api<Probe>('api/kin/models/test', route)
      .catch((reason: Error) => ({ ok: false, ms: 0, reason: reason.message }))
      .then((result) => setTests((all) => ({ ...all, [key]: result })))
  }

  return (
    <div className="kin-lane">
      <div className="kin-lane-head">
        <span className="kin-lane-title">{title}</span>
        <span className="kin-dim">{hint}</span>
      </div>
      {routes.length ? (
        <ol className="kin-routes">
          {routes.map((route, index) => {
            const key = route.provider + '/' + route.model
            const miss = failed(route)
            return (
              <li key={key} ref={(row) => { if (row) rows.current.set(key, row); else rows.current.delete(key) }}
                className="kin-route" draggable tabIndex={0} data-dragging={dragging === index ? '' : undefined}
                aria-label={`第 ${index + 1} 条：${nameOf(route, catalog)}。按住 Alt 加上下方向键移动`}
                onDragStart={(event) => {
                  from.current = index
                  setDragging(index)
                  event.dataTransfer.effectAllowed = 'move'
                  event.dataTransfer.setData('text/plain', key)
                }}
                onDragOver={(event) => {
                  event.preventDefault()
                  const at = from.current
                  if (at === null || at === index) return
                  // Only once the pointer is past this row's middle: the row that just gave way glides back
                  // under the pointer, and must not trade places again on its way.
                  const box = event.currentTarget.getBoundingClientRect()
                  const middle = box.top + box.height / 2
                  if (at < index ? event.clientY < middle : event.clientY > middle) return
                  move(at, index)
                  from.current = index
                  setDragging(index)
                }}
                onDragEnd={() => {
                  from.current = null
                  setDragging(null)
                  if (changed()) void save(latest.current)
                }}
                onKeyDown={(event) => {
                  if (!event.altKey || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return
                  event.preventDefault()
                  const next = move(index, index + (event.key === 'ArrowUp' ? -1 : 1))
                  if (next) void save(next)
                }}>
                <span className="kin-grip"><Grip size={14} /></span>
                <span className="kin-route-n">{index + 1}</span>
                <span className="kin-route-name">{nameOf(route, catalog)}{route.toolInputLimit && <small title="此转发路线携带工具时的实测输入限制；超限会提前转到备用路线。"> · 工具输入上限 {route.toolInputLimit.toLocaleString()}</small>}</span>
                <span className="kin-route-tools">
                  <Effort lane={lane} route={route} catalog={catalog}
                    onChange={(effort) => {
                      const next = routes.map((r, i) => (i === index ? { ...r, effort } : r))
                      setRoutes(next)
                      void save(next)
                    }} />
                  <button type="button" className="kin-test" draggable={false} disabled={tests[key] === 'testing'}
                    onMouseDown={(event) => event.stopPropagation()} onClick={() => test(key, route)}>测一下</button>
                </span>
                {(() => {
                  const tried = tests[key]
                  if (tried) {
                    const note = tried === 'testing' ? '正在试…' : tried.ok ? `通了 · ${seconds(tried.ms)}` : `没通：${tried.reason ?? ''}`
                    return <span className="kin-route-note" data-ok={tried !== 'testing' && tried.ok ? '' : undefined}
                      data-miss={tried !== 'testing' && !tried.ok ? '' : undefined} title={note}>{note}</span>
                  }
                  const note = data.last?.answered && same(data.last.answered, route) ? `${answered} · ${ago(data.last.at)}`
                    : miss ? `上次没接上：${miss.reason}` : index === 0 ? '先找它' : '备用'
                  return <span className="kin-route-note" data-miss={miss ? '' : undefined} title={note}>{note}</span>
                })()}
                <button type="button" className="kin-icon-btn" aria-label="去掉这条路线"
                  onClick={() => { const next = routes.filter((_, i) => i !== index); setRoutes(next); void save(next) }}>
                  <Close size={14} />
                </button>
              </li>
            )
          })}
        </ol>
      ) : (
        <p className="kin-quiet">还没排路线，现在用 DSH 的默认模型{data.fallback ? `（${nameOf(data.fallback, catalog)}）` : ''}。</p>
      )}
      <div className="kin-inline-form">
        <select value={choice} onChange={(event) => setChoice(event.target.value)} aria-label={`给「${title}」加一条路线`}>
          <option value="">{catalog.length ? '加一条路线…' : 'DSH 里还没有可用的模型'}</option>
          {catalog.map((group) => (
            <optgroup key={group.id} label={group.name}>
              {group.models.filter((m) => !has(group.id, m.id)).map((m) => (
                <option key={m.id} value={JSON.stringify([group.id, m.id])}>{m.name}</option>
              ))}
            </optgroup>
          ))}
        </select>
        <button type="button" className="kin-btn" onClick={add}>加上</button>
      </div>
      {problem ? <p className="kin-error">{problem}</p> : null}
    </div>
  )
}

function Effort({ lane, route, catalog, onChange }: {
  lane: Lane; route: Route; catalog: Catalog; onChange: (effort: string | undefined) => void
}) {
  const efforts = catalog.find((g) => g.id === route.provider)?.models.find((m) => m.id === route.model)?.reasoning?.efforts ?? []
  if (!efforts.length) return <span />
  const light = lane === 'chat' && efforts.some((e) => e.id === 'low')
  return (
    <select className="kin-effort" value={route.effort ?? ''} aria-label="想多久再回答" title="想多久再回答"
      onChange={(event) => onChange(event.target.value || undefined)}
      onMouseDown={(event) => event.stopPropagation()} draggable={false}>
      <option value="">{light ? '默认 · 少想' : '默认'}</option>
      {efforts.map((e) => <option key={e.id} value={e.id}>{EFFORTS[e.id] ?? e.name}</option>)}
    </select>
  )
}

function Features({ features, onSaved }: { features: Record<Feature, boolean>; onSaved: () => void }) {
  const [state, setState] = useState(features)
  useEffect(() => setState(features), [features])
  const flip = (key: Feature) => {
    const value = !state[key]
    setState({ ...state, [key]: value })
    api('api/kin/features', { [key]: value }).then(onSaved).catch(() => setState(features))
  }
  return (
    <section className="kin-card">
      <h3 className="kin-card-title">她的内心</h3>
      {FEATURES.map(({ key, title, hint }) => (
        <div key={key} className="kin-switch-row">
          <div><p>{title}</p><span className="kin-dim">{hint}</span></div>
          <button type="button" role="switch" aria-checked={state[key]} aria-label={title} className="kin-switch" onClick={() => flip(key)} />
        </div>
      ))}
    </section>
  )
}

interface Probe { ok: boolean; ms: number; reason?: string }
const seconds = (ms: number) => (ms < 1000 ? `${ms} 毫秒` : `${(ms / 1000).toFixed(1)} 秒`)
const same = (a: Route, b: Route) => a.provider === b.provider && a.model === b.model
const keyOf = (routes: Route[]) => routes.map((r) => `${r.provider}/${r.model}/${r.effort ?? ''}/${r.toolInputLimit ?? ''}`).join('\n')

function nameOf(route: Route, catalog: Catalog): string {
  const group = catalog.find((g) => g.id === route.provider)
  const model = group?.models.find((m) => m.id === route.model)
  return `${group?.name ?? route.provider} · ${model?.name ?? route.model}`
}
