/**
 * Her calendar: a month of her days, with three kinds of time on one page. Ahead, what has a time set; open,
 * what is still to do (还没做的); behind, quiet marks where a day left something. A stretch of days she or the
 * owner marked lies across its days as a soft band, an anniversary twinkles on its date. The day you pick reads
 * like a page of her life, in order; each line is a door to its place in her little world, and any of them can
 * be taken into the chat (和她说说). Today or later, the owner can leave her a reminder or mark some days. The
 * same days show as a week on her collection tile and as a strip on 此刻.
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react'
import { inOrder, KINDS, timeOf, type Item, type Kind } from '../calendar.js'
import { api, useApi } from './api.js'
import { glide } from './glass.js'
import { Back, Bubble, Close, Sparkle } from './icons.js'
import { calendarDay, clock } from './time.js'
import type { Place } from './World.js'

interface Calendar { items: Item[]; partial: boolean }

/** Takes a line into her chat, the composer started with it. */
type Talk = (text: string) => void

/** Three families of marks, in her colours; each kind's place in her little world, where it has one. */
type Tint = 'rose' | 'peach' | 'lilac'
const LOOK: Record<Kind, { tint: Tint; door?: Place }> = {
  reminder: { tint: 'rose' }, promise: { tint: 'rose' }, held: { tint: 'rose' }, waiting: { tint: 'rose' }, sent: { tint: 'rose' },
  anniversary: { tint: 'rose' },
  letter: { tint: 'peach', door: 'letters' }, darkroom: { tint: 'peach', door: 'letters' }, diary: { tint: 'peach', door: 'diary' },
  outing: { tint: 'peach', door: 'wander' }, work: { tint: 'peach' }, period: { tint: 'peach' },
  plan: { tint: 'lilac', door: 'wishes' }, step: { tint: 'lilac', door: 'wishes' }, wish: { tint: 'lilac', door: 'wishes' },
  dream: { tint: 'lilac', door: 'dreams' }, review: { tint: 'lilac' }, routine: { tint: 'lilac' },
}
const LEGEND: [Tint, string][] = [['rose', '提醒和要说的话'], ['peach', '信、日记和外面的事'], ['lilac', '打算和梦']]
const OPEN_ORDER: Kind[] = ['work', 'plan', 'promise', 'wish']
const WEEK = ['一', '二', '三', '四', '五', '六', '日']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

const pad = (n: number) => String(n).padStart(2, '0')
/** YYYY-MM-DD of a date, in local time. */
export const dayKey = (date: Date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
const dateOf = (day: string) => new Date(`${day}T00:00:00`)
const dayOf = (item: Item) => dayKey(new Date(timeOf(item)))
const shift = (date: Date, days: number) => new Date(date.getFullYear(), date.getMonth(), date.getDate() + days)
const weekday = (date: Date) => '周' + WEEK[(date.getDay() + 6) % 7]
const markOf = (item: Item) => LOOK[item.kind].tint + (item.done ? ' past' : '')
const whenOf = (item: Item) => (item.kind === 'dream' ? '夜里' : item.at!.length === 10 ? '全天' : clock(item.at!))
const routeOf = (first: Date, days: number, ahead = false) =>
  `api/kin/calendar?from=${first.toISOString()}&to=${shift(first, days).toISOString()}${ahead ? '&ahead' : ''}`

/** Her dated things by local day, each day in order; a stretch of days on each of its days, first. */
function byDay(items: Item[]): Map<string, Item[]> {
  const days = new Map<string, Item[]>()
  const put = (day: string, item: Item) => days.set(day, [...(days.get(day) ?? []), item])
  for (const item of items.filter((item) => item.at).sort(inOrder)) {
    if (!item.until) put(dayOf(item), item)
    else for (let day = item.at!; day <= item.until; day = dayKey(shift(dateOf(day), 1))) put(day, item)
  }
  return days
}

/** The stretches of days across one week: the column each starts in, how many it spans, and whether it goes on past either edge. */
function bandsOf(week: Date[], periods: Item[]) {
  const days = week.map(dayKey)
  return periods.filter((item) => item.at! <= days[6]! && item.until! >= days[0]!).map((item) => {
    const from = Math.max(0, days.indexOf(item.at!))
    const to = item.until! > days[6]! ? 6 : days.indexOf(item.until!)
    return { item, from, span: to - from + 1, before: item.at! < days[0]!, after: item.until! > days[6]! }
  })
}

/** How far a day is from today, the way a person says it. */
function near(day: string, today: string): string {
  const days = Math.round((dateOf(day).getTime() - dateOf(today).getTime()) / 86_400_000)
  return ({ '-2': '前天', '-1': '昨天', 0: '今天', 1: '明天', 2: '后天' } as Record<string, string>)[days] ?? (days > 0 ? `${days} 天后` : `${-days} 天前`)
}

/** The weeks of a month, Monday first: whole weeks, so the first and last may reach into its neighbours. */
function weeksOf(month: Date): Date[][] {
  const lead = (month.getDay() + 6) % 7
  const rows = Math.ceil((lead + new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()) / 7)
  return Array.from({ length: rows }, (_, week) => Array.from({ length: 7 }, (_, day) => shift(month, week * 7 + day - lead)))
}

export function CalendarView({ go, day, talk }: { go: (place: Place) => void; day?: string; talk: Talk }) {
  const today = dayKey(new Date())
  const [picked, setPicked] = useState(day ?? today)
  const [month, setMonth] = useState(() => new Date(dateOf(picked).getFullYear(), dateOf(picked).getMonth(), 1))
  // Which way the month last turned, so the new one glides in from that side.
  const [turned, setTurned] = useState<'next' | 'prev'>()
  const grid = useRef<HTMLDivElement>(null)
  const keyed = useRef(false)
  const weeks = useMemo(() => weeksOf(month), [month])
  const { data, error, reload } = useApi<Calendar>(routeOf(weeks[0]![0]!, weeks.length * 7), 60_000)
  const days = useMemo(() => byDay(data?.items ?? []), [data])
  const periods = (data?.items ?? []).filter((item) => item.until)
  const festive = (data?.items ?? []).some((item) => item.kind === 'anniversary')
  const open = (data?.items ?? []).filter((item) => item.open).sort((a, b) => OPEN_ORDER.indexOf(a.kind) - OPEN_ORDER.indexOf(b.kind))
  const cells = weeks.flat().map(dayKey)
  const lens = cells.indexOf(picked)

  const show = (next: Date, pick?: string) => glide(() => {
    setTurned(next > month ? 'next' : 'prev')
    setMonth(next)
    if (pick) setPicked(pick)
  })
  const pick = (date: Date) => {
    const first = new Date(date.getFullYear(), date.getMonth(), 1)
    if (first.getTime() === month.getTime()) setPicked(dayKey(date))
    else show(first, dayKey(date))
  }
  // Arrow keys walk the days, across the month's edge too; the day walked to takes the focus.
  const walk = (event: KeyboardEvent) => {
    const step = ({ ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 } as Record<string, number>)[event.key]
    const from = (event.target as HTMLElement).dataset.day
    if (!step || !from) return
    event.preventDefault()
    keyed.current = true
    pick(shift(dateOf(from), step))
  }
  useEffect(() => {
    if (!keyed.current) return
    keyed.current = false
    grid.current?.querySelector<HTMLElement>(`[data-day="${picked}"]`)?.focus()
  }, [picked])

  return (
    <>
      <section className="kin-card kin-cal-month">
        <header className="kin-cal-head">
          <strong className="kin-numeral">{month.getMonth() + 1}</strong>
          <div>
            <h3 className="kin-script">{MONTHS[month.getMonth()]}</h3>
            <span>{month.getFullYear()} 年</span>
          </div>
          <nav className="kin-cal-turns" aria-label="翻月份">
            {dayKey(month).slice(0, 7) === today.slice(0, 7) && picked === today ? null
              : <button type="button" className="kin-chip" aria-label="回到今天" onClick={() => pick(dateOf(today))}>今天</button>}
            <button type="button" className="kin-cal-turn" aria-label="上个月" onClick={() => show(new Date(month.getFullYear(), month.getMonth() - 1, 1))}><Back size={18} /></button>
            <button type="button" className="kin-cal-turn" data-next="" aria-label="下个月" onClick={() => show(new Date(month.getFullYear(), month.getMonth() + 1, 1))}><Back size={18} /></button>
          </nav>
        </header>
        <div role="grid" aria-label={`${month.getFullYear()}年${month.getMonth() + 1}月`} ref={grid} onKeyDown={walk}>
          <div className="kin-cal-names" role="row">
            {WEEK.map((name) => <span key={name} role="columnheader" aria-label={'周' + name}>{name}</span>)}
          </div>
          <div className="kin-cal-weeks" role="rowgroup" key={dayKey(month)} data-from={turned}>
            {lens < 0 ? null
              : <span className="kin-cal-lens" aria-hidden="true" style={{ '--kin-cal-col': lens % 7, '--kin-cal-row': Math.floor(lens / 7) } as CSSProperties} />}
            {weeks.map((week) => (
              <div key={dayKey(week[0]!)} className="kin-cal-row" role="row">
                {bandsOf(week, periods).map(({ item, from, span, before, after }) => (
                  <span key={item.id} className="kin-cal-band" aria-hidden="true" data-by={item.by} data-done={item.done ? '' : undefined}
                    data-before={before ? '' : undefined} data-after={after ? '' : undefined}
                    style={{ '--kin-cal-from': from, '--kin-cal-span': span } as CSSProperties} />
                ))}
                {week.map((date) => {
                  const key = dayKey(date)
                  const list = days.get(key) ?? []
                  // One dot for each family of things the day holds: what is still ahead first, then what is behind.
                  // A stretch of days has its band and an anniversary its twinkle instead.
                  const marks = [...new Set(list.filter((item) => !item.until && item.kind !== 'anniversary').map(markOf))]
                    .sort((a, b) => Number(a.endsWith('past')) - Number(b.endsWith('past')))
                  return (
                    <button key={key} type="button" role="gridcell" className="kin-cal-cell" data-day={key}
                      data-out={date.getMonth() === month.getMonth() ? undefined : ''} data-today={key === today ? '' : undefined}
                      aria-selected={key === picked} aria-current={key === today ? 'date' : undefined}
                      tabIndex={key === (lens < 0 ? dayKey(month) : picked) ? 0 : -1}
                      aria-label={`${date.getMonth() + 1}月${date.getDate()}日 ${weekday(date)}${key === today ? '，今天' : ''}${list.length ? `，${list.length} 件事` : ''}`}
                      onClick={() => pick(date)}>
                      <span className="kin-numeral">{date.getDate()}</span>
                      <span className="kin-cal-dots">{marks.slice(0, 4).map((mark) => <i key={mark} className="kin-cal-dot" data-mark={mark} />)}</span>
                      {list.some((item) => item.kind === 'anniversary') ? <span className="kin-cal-spark" aria-hidden="true"><Sparkle size={10} /></span> : null}
                    </button>
                  )
                })}
              </div>
            ))}
          </div>
        </div>
        <p className="kin-cal-legend">
          {LEGEND.map(([tint, words]) => <span key={tint}><i className="kin-cal-dot" data-mark={tint} />{words}</span>)}
          <span><i className="kin-cal-dot" data-mark="rose past" />空心的已经过去</span>
          {periods.length ? <span><i className="kin-cal-swatch" />记着的日子</span> : null}
          {festive ? <span><span className="kin-cal-spark"><Sparkle size={10} /></span>纪念日</span> : null}
        </p>
      </section>

      <DayPage key={picked} day={picked} today={today} items={days.get(picked) ?? []} loading={!data && !error} go={go} talk={talk} reload={reload} />
      {error ? <p className="kin-quiet">没读出来：{error} <button type="button" className="kin-link" onClick={() => void reload()}>再试一次</button></p> : null}

      <section className="kin-card kin-cal-open">
        <h3 className="kin-card-title">还没做的</h3>
        {open.length ? (
          <ul className="kin-list kin-list-scroll" tabIndex={0} aria-label="还没做的事">
            {open.map((item) => (
              <li key={item.id}>
                <Line item={item} go={go} />
                {item.at ? <button type="button" className="kin-link" aria-label={`看 ${calendarDay(dayOf(item))} 那天`} onClick={() => pick(dateOf(dayOf(item)))}>{calendarDay(dayOf(item))}</button> : null}
                <TalkAbout item={item} talk={talk} />
              </li>
            ))}
          </ul>
        ) : <p className="kin-quiet">{data ? '眼下没有搁着的事。' : '正在看她手头还有什么…'}</p>}
      </section>
      {data?.partial ? <p className="kin-footnote">有几样这次没读出来，过一会儿会再看。</p> : null}
    </>
  )
}

/** One day as a page of her life: what it holds, in order; today or later, a line the owner can leave her, or some days to mark. */
function DayPage({ day, today, items, loading, go, talk, reload }: {
  day: string; today: string; items: Item[]; loading: boolean; go: (place: Place) => void; talk: Talk; reload: () => unknown
}) {
  const date = dateOf(day)
  const [problem, setProblem] = useState<string>()
  const ahead = items.filter((item) => !item.done).length
  // Takes back a reminder left on this page (`remove`) or a mark (`unmark`).
  const remove = (what: { remove: string } | { unmark: string }) => {
    api('api/kin/calendar', what)
      .then(() => { setProblem(undefined); void reload() })
      .catch((reason: Error) => setProblem('没取消掉：' + reason.message))
  }
  return (
    <section className="kin-card kin-cal-day" aria-label={`${calendarDay(day)}这一天`}>
      <header className="kin-cal-day-head">
        <strong className="kin-numeral">{date.getDate()}</strong>
        <div>
          <h3>{calendarDay(day)} · {weekday(date)}</h3>
          <span>{[near(day, today), ahead ? `还有 ${ahead} 件在前面` : '', items.length - ahead ? `留下了 ${items.length - ahead} 件` : ''].filter(Boolean).join(' · ')}</span>
        </div>
      </header>
      {items.length ? (
        <ol className="kin-cal-line">
          {items.map((item) => (
            <li key={item.id} className="kin-cal-entry">
              <span className="kin-numeral kin-cal-when">{whenOf(item)}</span>
              {item.kind === 'anniversary' ? <span className="kin-cal-spark" aria-hidden="true"><Sparkle size={12} /></span>
                : <i className="kin-cal-dot" data-mark={item.until ? 'band' + (item.done ? ' past' : '') : markOf(item)} data-by={item.by} />}
              <Line item={item} go={go} />
              <span className="kin-cal-acts">
                <TalkAbout item={item} talk={talk} />
                {item.schedule ? <button type="button" className="kin-icon-btn" aria-label="取消这个提醒" onClick={() => remove({ remove: item.schedule! })}><Close size={14} /></button> : null}
                {item.mark ? <button type="button" className="kin-icon-btn" aria-label="把这段日子从日程上去掉" onClick={() => remove({ unmark: item.mark! })}><Close size={14} /></button> : null}
              </span>
            </li>
          ))}
        </ol>
      ) : <p className="kin-quiet">{loading ? '正在翻这一天…' : day < today ? '这一天没留下什么。' : day === today ? '今天还没有什么事。' : '这一天还空着。'}</p>}
      {problem ? <p className="kin-error">{problem}</p> : null}
      {day >= today ? <Leave day={day} today={today} onSaved={reload} /> : null}
    </section>
  )
}

/** What one thing is, in a line or two. Her own words keep their material (a diary line on paper, a dream on night glass); one with a place in her little world leads there. */
function Line({ item, go }: { item: Item; go: (place: Place) => void }) {
  const { door } = LOOK[item.kind]
  const slip = item.kind === 'diary' ? ' kin-cal-slip kin-paper' : item.kind === 'dream' ? ' kin-cal-slip kin-dream' : ''
  // A stretch of days says who marked it.
  const note = [item.note, item.by && (item.by === 'kin' ? '她记的' : '你记的')].filter(Boolean).join(' · ')
  const body = (
    <>
      <span className="kin-cal-title"><span className="kin-tag">{KINDS[item.kind]}</span>{item.title}</span>
      {note ? <span className="kin-dim">{note}</span> : null}
      {item.folder ? <span className="kin-cal-where" title={item.folder}>在 …/{item.folder.split('/').filter(Boolean).slice(-2).join('/')}</span> : null}
    </>
  )
  return door
    ? <button type="button" className={'kin-cal-what kin-cal-door' + slip} data-kind={item.kind} data-done={item.done ? '' : undefined} onClick={() => go(door)}>{body}</button>
    : <div className="kin-cal-what" data-kind={item.kind} data-done={item.done ? '' : undefined}>{body}</div>
}

/** 和她说说: opens her chat with a line about this thing, for the owner to go on with. */
function TalkAbout({ item, talk }: { item: Item; talk: Talk }) {
  return (
    <button type="button" className="kin-icon-btn kin-cal-talk" data-label="和她说说" aria-label={`和她说说「${item.title}」`}
      onClick={() => talk(`关于日程上的「${item.title}」：`)}>
      <Bubble size={14} />
    </button>
  )
}

/** What the owner can leave on a day today or later: a reminder for a time, or a stretch of days marked from here. */
function Leave({ day, today, onSaved }: { day: string; today: string; onSaved: () => unknown }) {
  const [marking, setMarking] = useState(false)
  return (
    <div className="kin-cal-leave">
      <div className="kin-cal-modes" role="group" aria-label="在这天留点什么">
        <button type="button" aria-pressed={!marking} onClick={() => setMarking(false)}>让她提醒</button>
        <button type="button" aria-pressed={marking} onClick={() => setMarking(true)}>记一段日子</button>
      </div>
      {marking ? <MarkDays day={day} onSaved={onSaved} /> : <Remind day={day} today={today} onSaved={onSaved} />}
    </div>
  )
}

/** Some days the owner marks, from this one on: they lie across the month, and she hears of them now, in the owner's words. */
function MarkDays({ day, onSaved }: { day: string; onSaved: () => unknown }) {
  const [from, setFrom] = useState(day)
  const [to, setTo] = useState(day)
  const [text, setText] = useState('')
  const [problem, setProblem] = useState<string>()
  const [saving, setSaving] = useState(false)
  const save = () => {
    if (saving) return
    if (!text.trim()) return setProblem('先写一句，这几天是什么日子')
    if (!from || !to) return setProblem('选好从哪天到哪天')
    setSaving(true)
    api('api/kin/calendar', { from, to, text: text.trim() })
      // Words typed while it was on its way stay.
      .then(() => { setText((now) => (now === text ? '' : now)); setProblem(undefined); void onSaved() })
      .catch((reason: Error) => setProblem('没记上：' + reason.message))
      .finally(() => setSaving(false))
  }
  return (
    <>
      <div className="kin-inline-form kin-cal-remind">
        <span className="kin-cal-range">
          <input type="date" className="kin-cal-date" value={from} aria-label="从哪天"
            onChange={(event) => { setFrom(event.target.value); if (to < event.target.value) setTo(event.target.value); setProblem(undefined) }} />
          <i aria-hidden="true" />
          <input type="date" className="kin-cal-date" value={to} min={from} aria-label="到哪天"
            onChange={(event) => { setTo(event.target.value); setProblem(undefined) }} />
        </span>
        <input value={text} maxLength={120} placeholder="这几天是…出差、考试周、放假" aria-label="这几天是什么日子"
          onChange={(event) => { setText(event.target.value); setProblem(undefined) }}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) save() }} />
        <button type="button" className="kin-btn" data-primary="" onClick={save} disabled={saving}>记下</button>
      </div>
      {problem ? <p className="kin-error">{problem}</p> : null}
    </>
  )
}

/** A line the owner leaves her for a time on this day: it reaches her in the desktop conversation then. */
function Remind({ day, today, onSaved }: { day: string; today: string; onSaved: () => unknown }) {
  const [text, setText] = useState('')
  // Later today, the coming hour; another day, the morning.
  const [time, setTime] = useState(() => (day === today ? (new Date().getHours() < 23 ? `${pad(new Date().getHours() + 1)}:00` : '23:59') : '09:00'))
  const [problem, setProblem] = useState<string>()
  const [saving, setSaving] = useState(false)
  const save = () => {
    if (saving) return
    const at = new Date(`${day}T${time}`)
    if (!text.trim()) return setProblem('先写一句，到时候要她提醒什么')
    if (!(at.getTime() > Date.now())) return setProblem('选一个还没到的时间')
    setSaving(true)
    api('api/kin/calendar', { at: at.toISOString(), text: text.trim() })
      // Words typed while it was on its way stay.
      .then(() => { setText((now) => (now === text ? '' : now)); setProblem(undefined); void onSaved() })
      .catch((reason: Error) => setProblem('没定上：' + reason.message))
      .finally(() => setSaving(false))
  }
  return (
    <>
      <div className="kin-inline-form kin-cal-remind">
        <input type="time" className="kin-cal-time" value={time} aria-label="几点提醒" onChange={(event) => { setTime(event.target.value); setProblem(undefined) }} />
        <input value={text} maxLength={120} placeholder="到时候让她提醒你…" aria-label="提醒什么"
          onChange={(event) => { setText(event.target.value); setProblem(undefined) }}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) save() }} />
        <button type="button" className="kin-btn" data-primary="" onClick={save} disabled={saving}>定下</button>
      </div>
      {problem ? <p className="kin-error">{problem}</p> : null}
    </>
  )
}

/** This week for her collection tile: its seven days with a dot where something is ahead, and a word on what is next. */
export function useWeek(): { note: string; art: ReactNode } {
  const now = new Date()
  const monday = shift(now, -((now.getDay() + 6) % 7))
  const { data } = useApi<Calendar>(routeOf(monday, 7, true), 60_000)
  const today = dayKey(now)
  const days = byDay((data?.items ?? []).filter((item) => !item.done && item.at && (item.until ?? dayOf(item)) >= today))
  const left = days.get(today)?.length
  const next = [...days.keys()].sort().find((day) => day > today)
  return {
    note: left ? `今天还有 ${left} 件事` : next ? `下一件在${weekday(dateOf(next))}` : '这一周前面还空着',
    art: (
      <i className="kin-cal-strip" aria-hidden="true">
        {Array.from({ length: 7 }, (_, index) => shift(monday, index)).map((date, index) => (
          <b key={index} data-today={dayKey(date) === today ? '' : undefined} data-past={dayKey(date) < today ? '' : undefined}>
            <small>{WEEK[index]}</small>
            <span className="kin-numeral">{date.getDate()}</span>
            <i className="kin-cal-dot" data-mark={dayKey(date) >= today && days.has(dayKey(date)) ? 'rose' : 'none'} />
          </b>
        ))}
      </i>
    ),
  }
}

/** 此刻's quiet strip: what is still ahead today, and the next thing after it. Nothing on an empty day. */
export function Today({ open }: { open: () => void }) {
  const start = dateOf(dayKey(new Date()))
  const { data } = useApi<Calendar>(routeOf(start, 14, true), 60_000)
  const today = dayKey(start)
  const ahead = (data?.items ?? []).filter((item) => item.at && !item.done).sort(inOrder)
  // A stretch of days is on each of its days.
  const left = ahead.filter((item) => (item.until ? item.at! <= today && today <= item.until : dayOf(item) === today)
    && (item.at!.length === 10 || timeOf(item) >= Date.now()))
  const next = ahead.find((item) => dayOf(item) > today)
  if (!left.length) return null
  return (
    <button type="button" className="kin-glass kin-cal-today" onClick={open} title="打开日程">
      <span className="kin-cal-today-date"><strong className="kin-numeral">{start.getDate()}</strong><small>今天</small></span>
      <span className="kin-cal-today-lines">
        {left.slice(0, 2).map((item) => <span key={item.id}><b className="kin-numeral">{whenOf(item)}</b>{KINDS[item.kind]} · {item.title}</span>)}
        {next ? <span className="kin-dim">之后：{near(dayOf(next), today)}{next.at!.length === 10 ? '' : ' ' + whenOf(next)} · {KINDS[next.kind]} · {next.title}</span> : null}
      </span>
    </button>
  )
}
