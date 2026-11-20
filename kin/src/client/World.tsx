/**
 * 她的小世界: a collection of tiles, each a little object of her life (a night sky for her dreams, an
 * envelope for letters, a framed diary, her wishes, where she wandered, her memories, the week on her
 * calendar). A tile opens into its place; the title glides along, and a round glass button brings you back.
 */
import { useState, type CSSProperties, type ReactNode } from 'react'
import {
  activeWish, api, useApi, useDiary,
  type CreationArtifact, type CreationItem, type Diary, type DiaryEntry, type Dreams, type Explore, type Home, type Letters, type Memory, type Wish,
} from './api.js'
import { CalendarView, dayKey, useWeek } from './Calendar.js'
import { Back, Bubble, Craft, Mail, Moon, Search, Sparkle } from './icons.js'
import { Outing } from './steward/Outing.js'
import { stamp } from '../render.js'
import { ago, calendarDay, clock, day, isoDay, night } from './time.js'

export type Place = 'dreams' | 'letters' | 'diary' | 'wishes' | 'wander' | 'memories' | 'calendar'

const PLACES: Record<Place, { title: string; sub: string }> = {
  calendar: { title: 'Calendar', sub: '等在前面的，还没做的，和过去的日子留下的' },
  dreams: { title: 'Dreams', sub: '她做过的梦' },
  letters: { title: 'Letters', sub: '到了日子才能拆的信' },
  diary: { title: 'Diary', sub: '她写下的这些天' },
  wishes: { title: 'Wishes', sub: '她想做的事，和在做的计划' },
  wander: { title: 'Wander', sub: '她想弄明白的，和出门找到的' },
  memories: { title: 'Memories', sub: '问问她记不记得' },
}

export const CONCERN_KINDS: Record<string, string> = { care: '牵挂', anticipation: '期待', curiosity: '好奇', distress: '难受', shared_plan: '约好的' }
const WISH_KINDS: Record<string, string> = { contact: '想找你', explore: '想弄明白', create: '想做点东西' }
const EXPLORE_STATES: Record<string, string> = { complete: '带回了结果', failed: '没做成', interrupted: '中途停下', partial: '还没查完', running: '正在查' }
const WISH_STATES: Record<string, string> = { wanted: '想', waiting: '等时机', in_progress: '在做了' }
// Memories that are not things that happened say so.
const BASIS: Record<string, string> = { imagined: '梦里', internal_thought: '她想的' }
const STEP_STATES: Record<string, string> = { pending: '还没开始', ready: '准备做', running: '正在做', waiting: '等时机', completed: '已完成', abandoned: '已放下' }
const CONTACT_HOLDS: Record<string, string> = { 'enough-today': '今天主动联系已达上限', 'quiet-hours': '正在安静时段', 'waiting-for-reply': '等对方回复', 'asked-to-wait': '对方请她稍等', 'just-talked': '刚聊过，稍后再联系', off: '主动联系已关闭', 'proactive-off': '此对象未开启主动联系', paused: '模型暂时不可用，稍后重试', 'not-acquainted': '还未建立对话' }
const PLAN_STATES: Record<string, string> = { active: '进行中', waiting: '等着', done: '做完了', completed: '做完了', paused: '先放着' }

export function World({ home, place, go, openSettings, talk }: {
  home: Home | undefined; place: Place | undefined; go: (place: Place | undefined) => void; openSettings: () => void
  /** Takes a line into her chat, the composer started with it (the calendar's 和她说说). */
  talk: (text: string) => void
}) {
  // The day her calendar opens on when a timed step of a plan leads there; from her collection it opens on today.
  const [day, setDay] = useState<string>()
  if (!place) return <Collection home={home} open={(next) => { setDay(undefined); go(next) }} />
  return (
    <div className="kin-scroll">
      <header className="kin-detail-head">
        <button type="button" className="kin-round kin-glass" onClick={() => go(undefined)} aria-label="回到小世界"><Back size={20} /></button>
        <div>
          <h2 className="kin-script" style={named(place)}>{PLACES[place].title}</h2>
          <p>{PLACES[place].sub}</p>
        </div>
      </header>
      {place === 'dreams' ? <DreamsView openSettings={openSettings} /> : null}
      {place === 'letters' ? <LettersView openSettings={openSettings} /> : null}
      {place === 'diary' ? <DiaryView openSettings={openSettings} /> : null}
      {place === 'wishes' ? <WishesView home={home} toDay={(at) => { setDay(dayKey(new Date(at))); go('calendar') }} talk={talk} /> : null}
      {place === 'wander' ? <Wander talk={talk} /> : null}
      {place === 'memories' ? <Recall /> : null}
      {place === 'calendar' ? <CalendarView go={go} day={day} talk={talk} /> : null}
    </div>
  )
}

/** A tile's title and its place's title share a name, so one glides into the other. */
const named = (place: Place) => ({ viewTransitionName: 'kin-' + place }) as CSSProperties

const isFresh = (iso?: string | null) => Boolean(iso && Date.now() - Date.parse(iso) < 86_400_000)

function Collection({ home, open }: { home: Home | undefined; open: (place: Place) => void }) {
  const dreams = useApi<Dreams>('api/kin/dreams', 30_000)
  const letters = useApi<Letters>('api/kin/letters', 30_000)
  const diary = useApi<Diary>('api/kin/diary', 30_000)
  const explore = useApi<Explore>('api/kin/explore', 30_000)
  const creations = useApi<{ creations: CreationItem[] }>('api/kin/creations', 60_000)
  const wishes = home?.state.desires.filter(activeWish) ?? []
  const sealed = letters.data?.items.filter((item) => item.state === 'sealed').length ?? 0
  const dream = dreams.data?.dreams[0]
  const pages = diary.data?.total ?? diary.data?.entries.length ?? 0
  const wandered = explore.data?.explorations[0]
  const week = useWeek()
  const crafted = creations.data?.creations ?? []
  const letterFresh = Boolean(letters.data?.items.some((item) => item.state === 'opened' && isFresh(item.opened_at)))

  const tile = (place: Place, note: ReactNode, art?: ReactNode, fresh?: boolean) => (
    <button type="button" className="kin-tile" data-tile={place} onClick={() => open(place)} aria-label={PLACES[place].sub}>
      {art}
      {fresh ? <span className="kin-tile-fresh" title="最近 24 小时有新动静"><Sparkle size={11} />新</span> : null}
      <span className="kin-script kin-tile-title" style={named(place)}>{PLACES[place].title}</span>
      <span className="kin-tile-note">{note}</span>
    </button>
  )

  return (
    <div className="kin-scroll">
      <header className="kin-collection-head">
        <h2 className="kin-script">Her World</h2>
        <span>她的小世界</span>
      </header>
      <div className="kin-bento">
        {tile('dreams',
          dream ? `${night(dream.at)}：${clip(dream.text, 34)}` : dreams.data?.generation_enabled === false ? '她还不会做梦' : '还没有梦',
          <><i className="kin-art-moon" /><i className="kin-art-stars" /></>, isFresh(dream?.at))}
        {tile('letters', sealed ? `${sealed} 封还封着` : '写一封以后才能拆的信', <><i className="kin-art-flap" /><i className="kin-art-seal" /></>, letterFresh)}
        {tile('diary', diary.error ? '日记暂时没读出来，点开重试' : !diary.data ? '正在翻日记…' : pages ? `${pages} 页` : '还没写', <i className="kin-art-frame" />, isFresh(diary.data?.entries[0]?.at))}
        {tile('wishes', crafted.length ? `心愿 · ${crafted.length} 件手作` : '心愿', <strong className="kin-numeral kin-tile-count">{wishes.length}</strong>, isFresh(crafted[0]?.finished_at))}
        {tile('wander', wandered ? clip(wandered.title ?? wandered.text ?? '', 30) : '还没出门探索过', <i className="kin-art-horizon" />, isFresh(wandered?.at))}
        {tile('memories', '她记得你们聊过的事', <Constellation />)}
        {tile('calendar', week.note, week.art)}
      </div>
    </div>
  )
}

const clip = (text: string, length: number) => (text.length > length ? text.slice(0, length) + '…' : text)

/** A few stars and the lines between them: her memories, joined up. */
function Constellation() {
  const stars: [number, number][] = [[14, 66], [30, 36], [48, 54], [64, 22], [82, 44], [96, 16]]
  return (
    <svg className="kin-art-constellation" viewBox="0 0 110 80" aria-hidden="true">
      <polyline points={stars.map(([x, y]) => `${x},${y}`).join(' ')} />
      {stars.map(([x, y]) => <circle key={`${x}-${y}`} cx={x} cy={y} r="2.2" />)}
    </svg>
  )
}

function Quiet({ children }: { children: ReactNode }) {
  return <p className="kin-quiet">{children}</p>
}

function Failed({ error, retry }: { error: string; retry: () => void }) {
  return <p className="kin-quiet">没读出来：{error} <button type="button" className="kin-link" onClick={retry}>再试一次</button></p>
}

export function Wishes({ wishes, talk }: { wishes: Wish[]; talk?: (text: string) => void }) {
  return (
    <ul className="kin-list kin-list-scroll" tabIndex={0}>
      {wishes.map((wish) => (
        <li key={wish.id} className="kin-list-item">
          <span className="kin-list-body">
            {wish.kind ? <span className="kin-tag">{WISH_KINDS[wish.kind]}</span> : null}
            {wish.content ?? wish.topic}
            <span className="kin-dim"> · {WISH_STATES[wish.status] ?? wish.status}</span>
          </span>
          {talk ? (
            <button type="button" className="kin-say-btn" onClick={() => talk(`看到你的心愿「${wish.content ?? wish.topic}」，想和你聊聊这个`)}>
              <Bubble size={13} /> 和她说说
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  )
}

function WishesView({ home, toDay, talk }: { home: Home | undefined; toDay: (at: string) => void; talk?: (text: string) => void }) {
  const wishes = home?.state.desires.filter(activeWish) ?? []
  const plans = home?.plans.filter((plan) => plan.status === 'active') ?? []
  return (
    <>
      <Outing kind="creation" />
      <section className="kin-card">
        {wishes.length ? <Wishes wishes={wishes} talk={talk} /> : <Quiet>还没有特别想做的事。想做什么，是她自己在心里慢慢长出来的。</Quiet>}
      </section>
      <Creations talk={talk} />
      {plans.length ? (
        <section className="kin-card">
          <h3 className="kin-card-title">在做的计划</h3>
          <ul className="kin-list kin-list-scroll" tabIndex={0}>
            {plans.map((plan) => (
              <li key={plan.id}>
                {plan.title ?? plan.goal}<span className="kin-dim"> · {PLAN_STATES[plan.status] ?? plan.status}{plan.steps?.some((step) => step.state === 'completed') ? ` · 已完成 ${plan.steps.filter((step) => step.state === 'completed').length}/${plan.steps.length} 步` : ''}</span>
                {plan.steps?.filter((step) => !['completed', 'abandoned'].includes(step.state)).map((step) => <p key={step.id} className="kin-plan-step">
                  <span className="kin-tag">{STEP_STATES[step.state] ?? step.state}</span>{step.goal}
                  {step.execution?.host_gate?.state === 'blocked' ? <span className="kin-dim"> · {CONTACT_HOLDS[step.execution.host_gate.reason ?? ''] ?? '暂时无法主动联系'}</span> : null}
                  {step.not_before && !['completed', 'abandoned'].includes(step.state) ? (
                    <> · <button type="button" className="kin-link" aria-label={`在日程上看 ${calendarDay(step.not_before)}`} onClick={() => toDay(step.not_before!)}>{calendarDay(step.not_before)} {clock(step.not_before)}</button></>
                  ) : null}
                </p>)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  )
}

function artifactKind(name: string): 'image' | 'frame' | 'text' | 'link' {
  const dot = name.lastIndexOf('.')
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : ''
  if (['.png', '.jpg', '.jpeg', '.webp', '.gif'].includes(ext)) return 'image'
  if (['.html', '.htm', '.svg'].includes(ext)) return 'frame'
  if (['.md', '.txt', '.json', '.csv', '.py', '.js'].includes(ext)) return 'text'
  return 'link'
}

const formatBytes = (bytes: number) => (bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1048576).toFixed(1)} MB`)

function Creations({ talk }: { talk?: (text: string) => void }) {
  const { data, error, reload } = useApi<{ creations: CreationItem[] }>('api/kin/creations', 60_000)
  const [preview, setPreview] = useState<{ itemId: string; artifact: CreationArtifact }>()
  const [texts, setTexts] = useState<Record<string, { text?: string; error?: string; loading?: boolean }>>({})

  if (error) return <Failed error={error} retry={reload} />
  if (!data || !data.creations.length) return null

  const openArtifact = (itemId: string, artifact: CreationArtifact) => {
    if (preview?.itemId === itemId && preview.artifact.path === artifact.path) return setPreview(undefined)
    setPreview({ itemId, artifact })
    if (artifactKind(artifact.name) === 'text' && !texts[artifact.path]) {
      setTexts((now) => ({ ...now, [artifact.path]: { loading: true } }))
      void fetch('api/kin/creations/file?path=' + encodeURIComponent(artifact.path))
        .then(async (res) => {
          if (!res.ok) throw new Error(`${res.status}`)
          return res.text()
        })
        .then((text) => setTexts((now) => ({ ...now, [artifact.path]: { text } })))
        .catch(() => setTexts((now) => ({ ...now, [artifact.path]: { error: '没能打开这件文件' } })))
    }
  }

  return (
    <section className="kin-card">
      <h3 className="kin-card-title"><Craft size={15} /> 她的手作匣</h3>
      <div className="kin-creations">
        {data.creations.map((item) => (
          <article key={item.id} className="kin-creation">
            <div className="kin-creation-head">
              <strong>{item.title}</strong>
              {item.finished_at ? <span className="kin-dim">{ago(item.finished_at)}</span> : null}
            </div>
            {item.summary ? <p className="kin-creation-summary">{item.summary}</p> : null}
            {item.artifacts.length ? (
              <div className="kin-creation-files">
                {item.artifacts.map((art) => {
                  const active = preview?.itemId === item.id && preview.artifact.path === art.path
                  return (
                    <button key={art.path} type="button" className="kin-chip" data-active={active ? '' : undefined} onClick={() => openArtifact(item.id, art)}>
                      {art.name}{art.bytes ? ` · ${formatBytes(art.bytes)}` : ''}
                    </button>
                  )
                })}
              </div>
            ) : null}
            {preview?.itemId === item.id ? (() => {
              const art = preview.artifact
              const src = 'api/kin/creations/file?path=' + encodeURIComponent(art.path)
              const kind = artifactKind(art.name)
              if (kind === 'image') return <div className="kin-creation-preview"><img src={src} alt={art.name} /></div>
              if (kind === 'frame') return <div className="kin-creation-preview"><iframe className="kin-creation-frame" sandbox="allow-scripts" src={src} title={art.name} /></div>
              if (kind === 'text') {
                const entry = texts[art.path]
                return (
                  <div className="kin-paper kin-creation-preview">
                    {entry?.loading ? <span className="kin-dim">正在翻开…</span>
                      : entry?.error ? <span className="kin-error">{entry.error}</span>
                      : <pre className="kin-creation-text">{entry?.text ?? ''}</pre>}
                  </div>
                )
              }
              return <div className="kin-creation-preview"><a className="kin-link" href={src} target="_blank" rel="noopener noreferrer">在新窗口打开 {art.name}</a></div>
            })() : null}
            {talk ? (
              <div className="kin-creation-foot">
                <button type="button" className="kin-say-btn" onClick={() => talk(`刚看了你做的「${item.title}」，好喜欢！和我讲讲你是怎么做的吧`)}>
                  <Bubble size={13} /> 和她聊聊这件作品
                </button>
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  )
}

function Wander({ talk }: { talk?: (text: string) => void }) {
  const { data, error, reload } = useApi<Explore>('api/kin/explore', 30_000)
  if (error) return <Failed error={error} retry={reload} />
  if (!data) return <Quiet>正在翻她的探索笔记…</Quiet>
  const curious = data.wishes.filter(activeWish)
  const findings = data.findings.filter((item) => item.result?.summary)
  if (!curious.length && !data.explorations.length && !findings.length) {
    return <><Outing kind="exploration" /><Quiet>她还没出门探索过。有想弄明白的事时，她会自己去查，回来讲给你听。</Quiet></>
  }
  return (
    <>
      <Outing kind="exploration" />
      {curious.length ? <section className="kin-card"><h3 className="kin-card-title">想弄明白的</h3><Wishes wishes={curious} talk={talk} /></section> : null}
      {data.explorations.length ? (
        <section className="kin-card">
          <h3 className="kin-card-title">探索过的</h3>
          <ul className="kin-list kin-list-scroll" tabIndex={0}>
            {data.explorations.map((item) => <li key={item.source_id}>{item.title ?? item.text}<span className="kin-dim"> · {ago(item.at)}{item.state ? ` · ${EXPLORE_STATES[item.state] ?? item.state}` : ''}</span></li>)}
          </ul>
        </section>
      ) : null}
      {findings.length ? (
        <section className="kin-card">
          <h3 className="kin-card-title">带回来的发现</h3>
          <ul className="kin-list kin-list-scroll" tabIndex={0}>
            {findings.map((item, i) => (
              <li key={item.id ?? i} className="kin-list-item">
                <span className="kin-list-body">{item.result!.summary}</span>
                {talk ? (
                  <button type="button" className="kin-say-btn" onClick={() => talk(`看到你出门带回来的发现「${clip(item.result!.summary!, 28)}」，想听你多说说`)}>
                    <Bubble size={13} /> 和她聊聊
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  )
}

function DiaryView({ openSettings }: { openSettings: () => void }) {
  const { data, error, loading, reload, more } = useDiary()
  if (!data) return error ? <Failed error={error} retry={reload} /> : <Quiet>正在翻她的日记本…</Quiet>
  const replying = data.replies === 'enabled'
  return (
    <>
      {data.entries.length ? data.entries.map((entry) => (
        <DiaryPage key={entry.source_id} entry={entry} replying={replying} onReplied={reload} />
      )) : <Quiet>她还没写日记。安静下来的时候，她会写下这一天。</Quiet>}
      {error ? <Failed error={error} retry={data.cursor != null ? more : reload} /> : null}
      {data.cursor != null ? <p className="kin-footnote"><button type="button" className="kin-btn" disabled={loading} onClick={more}>{loading ? '正在翻下一页…' : '看更早的日记'}</button></p> : null}
      {!replying && data.entries.length ? (
        <p className="kin-footnote">想回她的日记？<button type="button" className="kin-link" onClick={openSettings}>在设置里打开「日记可以回复」</button></p>
      ) : null}
    </>
  )
}

function DiaryPage({ entry, replying, onReplied }: { entry: DiaryEntry; replying: boolean; onReplied: () => void }) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string>()
  const [sending, setSending] = useState(false)
  const reply = () => {
    if (sending || !text.trim()) return
    setSending(true)
    api('api/kin/diary/reply', { reflection_id: entry.source_id, text: text.trim() })
      // Words typed while it was on its way stay.
      .then(() => { setText((now) => (now === text ? '' : now)); setError(undefined); onReplied() })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setSending(false))
  }
  return (
    <article className="kin-paper">
      <header className="kin-page-head">
        <span>{day(entry.at)} {clock(entry.at)}</span>
        {entry.topic ? <span className="kin-tag">{entry.topic}</span> : null}
      </header>
      <p className="kin-voice">{entry.text}</p>
      {entry.replies.map((answer) => (
        <p key={answer.source_id} className="kin-reply"><span>你回她</span>{answer.text}</p>
      ))}
      {replying ? (
        <div className="kin-inline-form">
          <input value={text} placeholder="回她一句…" aria-label="回复这篇日记" onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) reply() }} />
          <button type="button" className="kin-btn" onClick={reply} disabled={sending}>回她</button>
        </div>
      ) : null}
      {error ? <p className="kin-error">{error}</p> : null}
    </article>
  )
}

function DreamsView({ openSettings }: { openSettings: () => void }) {
  const { data, error, reload } = useApi<Dreams>('api/kin/dreams', 30_000)
  if (error) return <Failed error={error} retry={reload} />
  if (!data) return <Quiet>正在想她梦见过什么…</Quiet>
  return (
    <>
      {data.dreams.map((dream) => (
        <article key={dream.source_id} className="kin-dream">
          <header className="kin-page-head"><Moon size={13} /><span>{night(dream.at)}</span></header>
          <p className="kin-voice">{dream.text}</p>
        </article>
      ))}
      {!data.dreams.length ? (
        data.generation_enabled ? <Quiet>还没有梦。安静的夜里，她也许会梦到点什么。</Quiet>
          : <Quiet>她还不会做梦。<button type="button" className="kin-link" onClick={openSettings}>在设置里打开「会做梦」</button></Quiet>
      ) : null}
    </>
  )
}

function LettersView({ openSettings }: { openSettings: () => void }) {
  const { data, error, reload } = useApi<Letters>('api/kin/letters', 30_000)
  const [text, setText] = useState('')
  const [unlock, setUnlock] = useState(isoDay(30))
  const [problem, setProblem] = useState<string>()
  const [sealing, setSealing] = useState(false)
  const [bodies, setBodies] = useState<Record<string, { loading?: boolean; text?: string; error?: string }>>({})
  const [openIds, setOpenIds] = useState<Record<string, boolean>>({})
  if (error) return <Failed error={error} retry={reload} />
  if (!data) return <Quiet>正在看信箱…</Quiet>
  const toggleLetter = (id: string) => {
    const next = !openIds[id]
    setOpenIds((now) => ({ ...now, [id]: next }))
    if (next && !bodies[id]?.text && !bodies[id]?.loading) {
      setBodies((now) => ({ ...now, [id]: { loading: true } }))
      void api<{ text: string }>('api/kin/letters/read', { id })
        .then((res) => setBodies((now) => ({ ...now, [id]: { text: res.text } })))
        .catch((err: Error) => setBodies((now) => ({ ...now, [id]: { error: err.message } })))
    }
  }
  const seal = () => {
    if (sealing) return
    if (!text.trim()) return setProblem('先写点什么吧')
    setSealing(true)
    api('api/kin/letters', { text: text.trim(), unlock_at: unlock })
      // Words written while it was on its way stay.
      .then(() => { setText((now) => (now === text ? '' : now)); setProblem(undefined); void reload() })
      .catch(() => setProblem('没封上。拆信的日子要在明天到一年之内'))
      .finally(() => setSealing(false))
  }
  return (
    <>
      {data.enabled ? (
        <section className="kin-paper kin-writing">
          <textarea className="kin-letter" value={text} rows={5} placeholder="写给那一天的她…"
            onChange={(event) => { setText(event.target.value); setProblem(undefined) }} />
          <div className="kin-inline-form">
            <label className="kin-date">那天才能拆
              <input type="date" value={unlock} min={isoDay(1)} max={isoDay(365)} onChange={(event) => setUnlock(event.target.value)} />
            </label>
            <button type="button" className="kin-seal-btn" onClick={seal} disabled={sealing} aria-label="封好这封信">封</button>
          </div>
          {problem ? <p className="kin-error">{problem}</p> : null}
        </section>
      ) : (
        <Quiet>时光信还没打开。<button type="button" className="kin-link" onClick={openSettings}>在设置里打开「时光信与暗房」</button></Quiet>
      )}
      {data.items.map((item) => {
        const opened = item.state === 'opened'
        const expanded = Boolean(openIds[item.id])
        const body = bodies[item.id]
        return (
          <div key={item.id} className="kin-envelope" data-state={item.state}>
            <Mail size={18} />
            <div className="kin-envelope-body">
              <div className="kin-envelope-head">
                <div>
                  <p>{item.kind === 'letter'
                    ? (item.state === 'sealed' ? `一封 ${calendarDay(item.unlock_at)} 才能拆的信` : `${day(item.created_at)}写的信，已经拆开了`)
                    : (item.state === 'sealed' ? `她锁起来的一篇日记，${calendarDay(item.unlock_at)} 才打开` : '她锁起来的日记打开了')}</p>
                  <span className="kin-dim">{item.kind === 'letter' ? '你' : '她'}写于 {day(item.created_at)}</span>
                </div>
                {opened ? (
                  <button type="button" className="kin-btn" onClick={() => toggleLetter(item.id)}>
                    {expanded ? '收起' : '拆开重读'}
                  </button>
                ) : null}
              </div>
              {opened && expanded ? (
                <div className="kin-paper kin-letter-body">
                  {body?.loading ? <p className="kin-dim">正在拆开这页信纸…</p>
                    : body?.error ? <p className="kin-error">{body.error}</p>
                    : <p className="kin-voice">{body?.text ?? ''}</p>}
                </div>
              ) : null}
            </div>
          </div>
        )
      })}
    </>
  )
}

/** One memory: what, who said it to whom, when; and the owner's 记错了, which she keeps with it from then on. */
function Remembered({ memory, onCorrected }: { memory: Memory; onCorrected: () => void }) {
  const [text, setText] = useState<string>()
  const [error, setError] = useState<string>()
  const [sending, setSending] = useState(false)
  const correct = () => {
    if (sending || !text?.trim()) return
    setSending(true)
    api('api/kin/correct', { id: memory.id, text: text.trim(), memory: memory.text })
      // Words typed while it was on its way keep the form open.
      .then(() => { setText((now) => (now === text ? undefined : now)); setError(undefined); onCorrected() })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setSending(false))
  }
  return (
    <li className="kin-card">
      <p>{memory.basis && BASIS[memory.basis] ? <span className="kin-tag">{BASIS[memory.basis]}</span> : null}{memory.text}</p>
      <span className="kin-dim">{[memory.at ? stamp(memory.at) : '', memory.who].filter(Boolean).join(' · ')}</span>
      {memory.id && text === undefined ? <button type="button" className="kin-link kin-misremembered" onClick={() => setText('')}>记错了</button> : null}
      {text !== undefined ? (
        <div className="kin-inline-form">
          <input autoFocus value={text} placeholder="哪里不对？对的是…" aria-label="更正这条记忆" onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) correct()
              if (event.key === 'Escape') setText(undefined)
            }} />
          <button type="button" className="kin-btn" onClick={correct} disabled={sending}>告诉她</button>
        </div>
      ) : null}
      {error ? <p className="kin-error">{error}</p> : null}
    </li>
  )
}

function Recall() {
  const [query, setQuery] = useState('')
  const [found, setFound] = useState<{ query: string; memories: Memory[] }>()
  const [error, setError] = useState<string>()
  const [busy, setBusy] = useState(false)
  const ask = (value = query) => {
    if (!value.trim()) return
    setQuery(value)
    setError(undefined)
    setBusy(true)
    api<{ memories: Memory[] }>('api/kin/recall', { query: value.trim() })
      .then(({ memories }) => setFound({ query: value.trim(), memories }))
      .catch((reason: Error) => {
        setError(reason.message || '回忆读不出来')
        setFound(undefined)
      })
      .finally(() => setBusy(false))
  }
  return (
    <>
      <div className="kin-search kin-glass">
        <Search size={16} />
        <input value={query} placeholder="问问她记不记得…" aria-label="回忆"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) ask() }} />
      </div>
      {busy ? <Quiet>她在想…</Quiet> : null}
      {!busy && error ? <Failed error={error} retry={() => ask()} /> : null}
      {!busy && !error && found ? (
        found.memories.length ? (
          <ul className="kin-memories">
            {found.memories.map((memory, i) => <Remembered key={memory.id ?? i} memory={memory} onCorrected={() => ask(found.query)} />)}
          </ul>
        ) : <Quiet>关于「{found.query}」，她没想起什么。</Quiet>
      ) : null}
      {!found && !busy && !error ? <Quiet>她记得你们聊过的事。输入一个词、一个人或一段时间试试。</Quiet> : null}
    </>
  )
}
