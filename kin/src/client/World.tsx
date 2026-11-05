/**
 * 她的小世界: a collection of tiles, each a little object of her life (a night sky for her dreams, an
 * envelope for letters, a framed diary, her wishes, where she wandered, her memories). A tile opens
 * into its place; the title glides along, and a round glass button brings you back.
 */
import { useState, type CSSProperties, type ReactNode } from 'react'
import {
  activeWish, api, useApi,
  type Diary, type DiaryEntry, type Dreams, type Explore, type Home, type Letters, type Memory, type Wish,
} from './api.js'
import { Back, Mail, Moon, Search } from './icons.js'
import { Outing } from './steward/Outing.js'
import { stamp } from '../render.js'
import { ago, calendarDay, clock, day, isoDay, night } from './time.js'

export type Place = 'dreams' | 'letters' | 'diary' | 'wishes' | 'wander' | 'memories'

const PLACES: Record<Place, { title: string; sub: string }> = {
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
const PLAN_STATES: Record<string, string> = { active: '进行中', waiting: '等着', done: '做完了', completed: '做完了', paused: '先放着' }

export function World({ home, place, go, openSettings }: {
  home: Home | undefined; place: Place | undefined; go: (place: Place | undefined) => void; openSettings: () => void
}) {
  if (!place) return <Collection home={home} open={go} />
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
      {place === 'wishes' ? <WishesView home={home} /> : null}
      {place === 'wander' ? <Wander /> : null}
      {place === 'memories' ? <Recall /> : null}
    </div>
  )
}

/** A tile's title and its place's title share a name, so one glides into the other. */
const named = (place: Place) => ({ viewTransitionName: 'kin-' + place }) as CSSProperties

function Collection({ home, open }: { home: Home | undefined; open: (place: Place) => void }) {
  const dreams = useApi<Dreams>('api/kin/dreams', 30_000)
  const letters = useApi<Letters>('api/kin/letters', 30_000)
  const diary = useApi<Diary>('api/kin/diary', 30_000)
  const explore = useApi<Explore>('api/kin/explore', 30_000)
  const wishes = home?.state.desires.filter(activeWish) ?? []
  const sealed = letters.data?.items.filter((item) => item.state === 'sealed').length ?? 0
  const dream = dreams.data?.dreams[0]
  const pages = diary.data?.entries.length ?? 0
  const wandered = explore.data?.explorations[0]

  const tile = (place: Place, note: ReactNode, art?: ReactNode) => (
    <button type="button" className="kin-tile" data-tile={place} onClick={() => open(place)} aria-label={PLACES[place].sub}>
      {art}
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
          <><i className="kin-art-moon" /><i className="kin-art-stars" /></>)}
        {tile('letters', sealed ? `${sealed} 封还封着` : '写一封以后才能拆的信', <><i className="kin-art-flap" /><i className="kin-art-seal" /></>)}
        {tile('diary', pages ? `${pages} 页` : '还没写', <i className="kin-art-frame" />)}
        {tile('wishes', '心愿', <strong className="kin-numeral kin-tile-count">{wishes.length}</strong>)}
        {tile('wander', wandered ? clip(wandered.title ?? wandered.text ?? '', 30) : '还没出门探索过', <i className="kin-art-horizon" />)}
        {tile('memories', '她记得你们聊过的事', <Constellation />)}
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

export function Wishes({ wishes }: { wishes: Wish[] }) {
  return (
    <ul className="kin-list kin-list-scroll" tabIndex={0}>
      {wishes.map((wish) => (
        <li key={wish.id}>
          {wish.kind ? <span className="kin-tag">{WISH_KINDS[wish.kind]}</span> : null}
          {wish.content ?? wish.topic}
          <span className="kin-dim"> · {WISH_STATES[wish.status] ?? wish.status}</span>
        </li>
      ))}
    </ul>
  )
}

function WishesView({ home }: { home: Home | undefined }) {
  const wishes = home?.state.desires.filter(activeWish) ?? []
  const plans = home?.plans.filter((plan) => plan.status === 'active') ?? []
  return (
    <>
      <Outing kind="creation" />
      <section className="kin-card">
        {wishes.length ? <Wishes wishes={wishes} /> : <Quiet>还没有特别想做的事。想做什么，是她自己在心里慢慢长出来的。</Quiet>}
      </section>
      {plans.length ? (
        <section className="kin-card">
          <h3 className="kin-card-title">在做的计划</h3>
          <ul className="kin-list kin-list-scroll" tabIndex={0}>
            {plans.map((plan) => (
              <li key={plan.id}>
                {plan.title ?? plan.goal}<span className="kin-dim"> · {PLAN_STATES[plan.status] ?? plan.status}{plan.steps?.some((step) => step.state === 'completed') ? ` · 已完成 ${plan.steps.filter((step) => step.state === 'completed').length}/${plan.steps.length} 步` : ''}</span>
                {plan.steps?.filter((step) => !['completed', 'abandoned'].includes(step.state)).map((step) => <p key={step.id} className="kin-plan-step">
                  <span className="kin-tag">{STEP_STATES[step.state] ?? step.state}</span>{step.goal}
                  {step.not_before && !['completed', 'abandoned'].includes(step.state) ? <span className="kin-dim"> · {calendarDay(step.not_before)} {clock(step.not_before)}</span> : null}
                </p>)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  )
}

function Wander() {
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
      {curious.length ? <section className="kin-card"><h3 className="kin-card-title">想弄明白的</h3><Wishes wishes={curious} /></section> : null}
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
          <ul className="kin-list kin-list-scroll" tabIndex={0}>{findings.map((item, i) => <li key={item.id ?? i}>{item.result!.summary}</li>)}</ul>
        </section>
      ) : null}
    </>
  )
}

function DiaryView({ openSettings }: { openSettings: () => void }) {
  const { data, error, reload } = useApi<Diary>('api/kin/diary', 30_000)
  if (error) return <Failed error={error} retry={reload} />
  if (!data) return <Quiet>正在翻她的日记本…</Quiet>
  const replying = data.replies === 'enabled'
  return (
    <>
      {data.entries.length ? data.entries.map((entry) => (
        <DiaryPage key={entry.source_id} entry={entry} replying={replying} onReplied={reload} />
      )) : <Quiet>她还没写日记。安静下来的时候，她会写下这一天。</Quiet>}
      {!replying && data.entries.length ? (
        <p className="kin-footnote">想回她的日记？<button type="button" className="kin-link" onClick={openSettings}>在设置里打开「日记可以回复」</button></p>
      ) : null}
    </>
  )
}

function DiaryPage({ entry, replying, onReplied }: { entry: DiaryEntry; replying: boolean; onReplied: () => void }) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string>()
  const reply = () => {
    if (!text.trim()) return
    api('api/kin/diary/reply', { reflection_id: entry.source_id, text: text.trim() })
      .then(() => { setText(''); setError(undefined); onReplied() })
      .catch((reason: Error) => setError(reason.message))
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
          <button type="button" className="kin-btn" onClick={reply}>回她</button>
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
  if (error) return <Failed error={error} retry={reload} />
  if (!data) return <Quiet>正在看信箱…</Quiet>
  const seal = () => {
    if (!text.trim()) return setProblem('先写点什么吧')
    api('api/kin/letters', { text: text.trim(), unlock_at: unlock })
      .then(() => { setText(''); setProblem(undefined); void reload() })
      .catch(() => setProblem('没封上。拆信的日子要在明天到一年之内'))
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
            <button type="button" className="kin-seal-btn" onClick={seal} aria-label="封好这封信">封</button>
          </div>
          {problem ? <p className="kin-error">{problem}</p> : null}
        </section>
      ) : (
        <Quiet>时光信还没打开。<button type="button" className="kin-link" onClick={openSettings}>在设置里打开「时光信与暗房」</button></Quiet>
      )}
      {data.items.map((item) => (
        <div key={item.id} className="kin-envelope" data-state={item.state}>
          <Mail size={18} />
          <div>
            <p>{item.kind === 'letter'
              ? (item.state === 'sealed' ? `一封 ${calendarDay(item.unlock_at)} 才能拆的信` : `${day(item.created_at)}写的信，已经拆开了`)
              : (item.state === 'sealed' ? `她锁起来的一篇日记，${calendarDay(item.unlock_at)} 才打开` : '她锁起来的日记打开了，在日记里')}</p>
            <span className="kin-dim">{item.kind === 'letter' ? '你' : '她'}写于 {day(item.created_at)}</span>
          </div>
        </div>
      ))}
    </>
  )
}

/** One memory: what, who said it to whom, when; and the owner's 记错了, which she keeps with it from then on. */
function Remembered({ memory, onCorrected }: { memory: Memory; onCorrected: () => void }) {
  const [text, setText] = useState<string>()
  const [error, setError] = useState<string>()
  const correct = () => {
    if (!text?.trim()) return
    api('api/kin/correct', { id: memory.id, text: text.trim(), memory: memory.text })
      .then(() => { setText(undefined); setError(undefined); onCorrected() })
      .catch((reason: Error) => setError(reason.message))
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
          <button type="button" className="kin-btn" onClick={correct}>告诉她</button>
        </div>
      ) : null}
      {error ? <p className="kin-error">{error}</p> : null}
    </li>
  )
}

function Recall() {
  const [query, setQuery] = useState('')
  const [found, setFound] = useState<{ query: string; memories: Memory[] }>()
  const [busy, setBusy] = useState(false)
  const ask = (value = query) => {
    if (!value.trim()) return
    setQuery(value)
    setBusy(true)
    api<{ memories: Memory[] }>('api/kin/recall', { query: value.trim() })
      .then(({ memories }) => setFound({ query: value.trim(), memories }))
      .catch(() => setFound({ query: value.trim(), memories: [] }))
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
      {!busy && found ? (
        found.memories.length ? (
          <ul className="kin-memories">
            {found.memories.map((memory, i) => <Remembered key={memory.id ?? i} memory={memory} onCorrected={() => ask(found.query)} />)}
          </ul>
        ) : <Quiet>关于「{found.query}」，她没想起什么。</Quiet>
      ) : null}
      {!found && !busy ? <Quiet>她记得你们聊过的事。输入一个词、一个人或一段时间试试。</Quiet> : null}
    </>
  )
}
