/**
 * Her calendar (client/Calendar.tsx): three kinds of time, gathered for a stretch of days.
 * - Ahead, what has a time set: reminders, replies she chose to give later, words waiting for quiet hours to
 *   end, letters' opening days, timed steps of her plans, when she means to think again.
 * - Open, what is still to do: her plans with their next step, work with her helpers, her wishes, what the
 *   two of them agreed on. One with a time of its own sits on its day as well.
 * - Behind, what a day held: diary pages, dreams, outings, steps and work that ended, what she sent on her own.
 * - Whole days: stretches of days she or the owner marked (a trip, an exam week; one day for a dated note), and
 *   the owner's anniversaries as Core keeps them.
 * Most of it is read from what she already keeps. Written here: a reminder the owner leaves on the page (a DSH
 * schedule into her desktop conversation), the marks (calendar.json), and her routines, DSH schedules into her
 * own conversation made with kin_routine (calendar-tools.ts). She reads the same gathering (`kinCalendar`,
 * `agenda`), so she and the owner look at one calendar.
 */
import type { ScheduleCatalogEntry } from '@deepseek-ai/dsh-schedule'
import type { SessionId } from '@deepseek-ai/dsh-session'
import type { Delivery } from './contact/deliveries.js'
import type { Host } from './index.js'
// Her page reads this file's words and order too (client/Calendar.tsx): nothing here may need Node to load.
import { stamp } from './render.js'
import type { Work } from './steward/work.js'
import type { Held } from './talk/index.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    kinCalendar: KinCalendar
  }
}

/** Her calendar for the main session: the things her page shows, for the days asked. */
export interface KinCalendar {
  /** `ahead`: only what is still to come or to do, without the days behind. */
  items(from: Date, to: Date, ahead?: boolean): Promise<Item[]>
  /** The stretch of days she is in, in a line for her context (a trip, an exam week under way); nothing when none is. */
  current(): string
  /** The marks not over yet, soonest first. */
  marks(): Mark[]
  /** Marks a stretch of days (`to` defaults to `from`: one day). */
  mark(mark: Pick<Mark, 'from' | 'text' | 'by'> & { to?: string }): Mark
  /** Takes a mark off; false when there was none by that id. */
  unmark(id: string): boolean
  /** Her routines: the DSH schedules `routine` made, into her own conversation. */
  routines(): Promise<ScheduleCatalogEntry[]>
  /** A routine of her own at `time` (HH:MM), every day or on `weekdays` (1 Monday … 7 Sunday). */
  routine(what: string, time: string, weekdays?: number[]): Promise<ScheduleCatalogEntry>
  /** Ends one of her routines; false when it is not one. */
  unroutine(id: string): Promise<boolean>
  /**
   * Brings her routines in line with the owner's freeze as it stands now: frozen, they rest with the rest of
   * her background life; thawed, each goes on from its next time. One change at a time, the latest last.
   */
  rest(): Promise<void>
}

/** A stretch of days on her calendar, or with `from === to` a dated note for one whole day. */
export interface Mark {
  id: string
  /** Its first and last day, YYYY-MM-DD. */
  from: string
  to: string
  /** What it is, in the words of whoever marked it. */
  text: string
  by: 'owner' | 'kin'
  at: string
}

/** What a thing on her calendar is, in a word: her page's tags, and `agenda`. */
export const KINDS = {
  reminder: '提醒', routine: '自己的打算', held: '晚点回', waiting: '待发', letter: '时光信', darkroom: '暗房日记', review: '再想想',
  step: '计划的一步', plan: '计划', work: '帮手的活', wish: '心愿', promise: '约好的',
  sent: '发出的话', diary: '日记', dream: '梦', outing: '出门', period: '记着的日子', anniversary: '纪念日',
} as const
export type Kind = keyof typeof KINDS

/** One thing on her calendar. Its words name nobody as 你, so the owner and she read the same line. */
export interface Item {
  id: string
  kind: Kind
  /** An instant, or a bare YYYY-MM-DD for what belongs to a whole day (a dream: the day its night began). Absent on an open thing with no time of its own. */
  at?: string
  title: string
  /** A second line: why, which plan, how it repeats, how it ended. */
  note?: string
  /** Already happened. */
  done?: true
  /** Still to do: on the 还没做的 list, and on its day when it has one. */
  open?: true
  /** A helper's work: the folder it is in. */
  folder?: string
  /** A reminder the owner set by hand: the DSH schedule behind it, which her page may remove. */
  schedule?: string
  /** A stretch of days: its last day (its first is `at`), its mark, which her page may take off, and who marked it. */
  until?: string
  mark?: string
  by?: Mark['by']
}

const HOUR = 3_600_000
const DAY = 24 * HOUR
const WEEK = '一二三四五六日'
const RRULE_DAYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU']
const pad = (n: number) => String(n).padStart(2, '0')
/** YYYY-MM-DD of a date, in local time. */
const dayKey = (date: Date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
const midnight = (day: string) => new Date(day + 'T00:00:00')
const dayAfter = (day: string) => {
  const date = midnight(day)
  date.setDate(date.getDate() + 1)
  return dayKey(date)
}
/** A day in words: 11月15日 周日. */
const dayWords = (day: string) => {
  const date = midnight(day)
  return `${date.getMonth() + 1}月${date.getDate()}日 周${WEEK[(date.getDay() + 6) % 7]}`
}
/** The days of a mark in words. */
export const daysOf = (mark: Pick<Mark, 'from' | 'to'>) => (mark.from === mark.to ? dayWords(mark.from) : `${dayWords(mark.from)} 到 ${dayWords(mark.to)}`)

/** When an item is, in ms; a whole-day one at its local midnight. */
export const timeOf = (item: Item) => Date.parse(item.at!.length === 10 ? item.at + 'T00:00:00' : item.at!)
/** When a whole-day item is over: the midnight after its (last) day. */
const over = (item: Item) => midnight(dayAfter(item.until ?? item.at!)).getTime()
const last = (item: Item) => timeOf(item) + (item.kind === 'dream' ? DAY - 1 : 0)
/** A day's order: what belongs to the whole day, then by the clock, and the night's dream last. */
export const inOrder = (a: Item, b: Item) => last(a) - last(b)

const say = (item: Item) => `${item.done ? '✓ ' : ''}${KINDS[item.kind]}：${item.title}${item.note ? `（${item.note}）` : ''}`

/**
 * Her calendar in plain lines for her to read: each day's things in order, what already happened marked ✓,
 * then what is still to do with no day of its own. Model-facing: 你 is Kin.
 */
export function agenda(items: Item[]): string {
  const lines: string[] = []
  let day = ''
  for (const item of items.filter((item) => item.at).sort(inOrder)) {
    const at = new Date(timeOf(item))
    const head = `${at.getFullYear()}年${at.getMonth() + 1}月${at.getDate()}日 周${WEEK[(at.getDay() + 6) % 7]}`
    if (head !== day) lines.push(day = head)
    const time = item.kind === 'dream' ? '夜里' : item.at!.length === 10 ? '全天' : `${pad(at.getHours())}:${pad(at.getMinutes())}`
    lines.push(`- ${time} ${say(item)}`)
  }
  const open = items.filter((item) => item.open && !item.at)
  if (open.length) lines.push('还没做、也没定日子的', ...open.map((item) => '- ' + say(item)))
  return lines.join('\n') || '这段日子里，你的日程上没有记着的事。'
}

/** A line of a longer text. */
const line = (text: string, length = 90) => {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > length ? flat.slice(0, length) + '…' : flat
}
const each = (n: number, unit: string) => (n === 1 ? '每' + unit : `每 ${n} ${unit}`)
const weekly = (weekdays: number[]) => '每周' + weekdays.map((day) => WEEK[day - 1]).join('、')

/** How a reminder repeats: in words, and for the plain rules its step from one time to the next. */
interface Repeat { words: string; ms?: number; days?: number; weekdays?: number[] }

export function repeatOf(task: ScheduleCatalogEntry): Repeat | undefined {
  switch (task.kind) {
    case 'every': {
      const seconds = task.everySeconds
      return { words: seconds % 86_400 === 0 ? each(seconds / 86_400, '天') : seconds % 3600 === 0 ? each(seconds / 3600, '小时') : each(Math.round(seconds / 60), '分钟'), ms: seconds * 1000 }
    }
    case 'daily': return { words: '每天', days: 1 }
    case 'weekly': return { words: weekly(task.weekdays), days: 1, weekdays: task.weekdays }
    case 'cron': return { words: '重复' }
    case 'rrule': {
      // Her own tools write RRULE, a one-time reminder as COUNT=1. The plain daily and weekly rules step on like
      // the typed kinds; DSH keeps the evaluator for the rest, so any other rule shows its next time only.
      const { FREQ, INTERVAL = '1', BYDAY, ...more } = Object.fromEntries(
        task.rrule.split('\n')[1]!.slice('RRULE:'.length).split(';').map((part) => part.split('=') as [string, string]))
      const n = Number(INTERVAL)
      if (more.COUNT === '1') return undefined
      if (Object.keys(more).length) return { words: '重复' }
      if (FREQ === 'DAILY') return { words: each(n, '天'), days: n }
      if (FREQ === 'WEEKLY' && !BYDAY) return { words: each(n, '周'), days: 7 * n }
      if (FREQ === 'WEEKLY' && n === 1) {
        const weekdays = BYDAY.split(',').map((day) => RRULE_DAYS.indexOf(day) + 1)
        return { words: weekly(weekdays), days: 1, weekdays }
      }
      return { words: '重复' }
    }
  }
}

/** When a reminder comes before `to`: its next time, then on along a plain repeat, one mark a day at most. Days are this machine's, which are the owner's. */
function times(task: ScheduleCatalogEntry, repeat: Repeat | undefined, to: number): string[] {
  const found: string[] = []
  for (let at = new Date(task.scheduledAt); at.getTime() < to;) {
    if (!repeat?.weekdays || repeat.weekdays.includes((at.getDay() + 6) % 7 + 1)) found.push(at.toISOString())
    if (repeat?.ms) {
      // The first of them on or after the next midnight.
      const wait = new Date(at).setHours(24, 0, 0, 0) - at.getTime()
      at = new Date(at.getTime() + Math.ceil(wait / repeat.ms) * repeat.ms)
    } else if (repeat?.days) {
      at = new Date(at)
      at.setDate(at.getDate() + repeat.days)
    } else break
  }
  return found
}

interface Step { id: string; goal: string; state: string; not_before?: string | null; execution?: { plan_run?: { finished_at?: string | null } | null } }
interface Plan { id: string; title?: string; goal?: string; status: string; updated_at?: string; steps: Step[] }

const STEP: Record<string, string> = { pending: '还没开始', ready: '准备做', running: '正在做', waiting: '在等时机', unconfirmed: '结果还没确认' }
const unfinished = (step: Step) => !['completed', 'abandoned'].includes(step.state)

/** A plan on her calendar: itself while it is open, its timed steps on their days, its finished steps where they ended. */
function planItems(plan: Plan): Item[] {
  const name = plan.title ?? plan.goal ?? ''
  const next = plan.steps.find(unfinished)
  const how = next && (STEP[next.state] ?? next.state)
  return [
    ...(plan.status === 'active' ? [{ id: 'plan:' + plan.id, kind: 'plan' as const, title: name, note: next && (next.goal === name ? how : `下一步：${next.goal} · ${how}`), open: true as const }] : []),
    ...plan.steps.flatMap((step): Item[] => {
      const item = { id: `step:${plan.id}:${step.id}`, kind: 'step' as const, title: step.goal, note: name === step.goal ? undefined : name }
      // Core keeps a finish time for what an executor ran; a step settled another way is dated by its plan once that is done.
      const ended = step.execution?.plan_run?.finished_at ?? (plan.status === 'completed' ? plan.updated_at : undefined)
      if (step.state === 'completed') return ended ? [{ ...item, at: ended, done: true }] : []
      return plan.status === 'active' && unfinished(step) && step.not_before ? [{ ...item, at: step.not_before }] : []
    }),
  ]
}

const WAITING: Record<string, string> = { waiting: '在等时机', in_progress: '在做了' }
const ENDED: Record<Work['state'], string> = { running: '还在做', done: '做完了', stopped: '停下了', failed: '没做成', interrupted: '断在了半路' }
const SENT: Partial<Record<Delivery['kind'], string>> = { contact: '主动发的', relay: '转告的', chime: '在群里说的' }
const OUTING: Record<string, string> = { complete: '带回了结果', failed: '没做成', interrupted: '中途停下', partial: '还没查完', running: '还在外面' }

export function calendar(host: Host) {
  const { mind } = host
  // Her finished plans, read whole from Core each time: kept a few minutes, so an open calendar does not
  // keep her mind's one resident process busy with it.
  let finished: { at: number; items: Item[] } | undefined
  // The marks, and the ids of the schedules kin_routine made.
  const book = host.load<{ marks: Mark[]; routines: string[] }>('calendar.json', { marks: [], routines: [] })
  const keep = () => host.save('calendar.json', book)
  let resting: Promise<void> = Promise.resolve()
  /**
   * Routines brought in line with the owner's freeze, one change at a time. Each reads the freeze when its turn
   * comes, not when it was asked: a thaw that follows a freeze closely is what holds.
   */
  const align = (routines: () => readonly string[]) => (resting = resting.then(async () => {
    const frozen = host.heartbeat.frozen
    for (const id of routines()) {
      await scheduler().edit({ id: id as ScheduleCatalogEntry['id'], action: 'update', patch: { active: !frozen } })
        .catch((error: Error) => host.log(`calendar: a routine was not ${frozen ? 'rested' : 'resumed'}: ${error.message}`))
    }
  }))
  const ours = (id: string) => book.routines.includes(id)

  // A one-time `at` into her desktop conversation is one the owner set by hand: her own tools only write RRULE.
  const byHand = (task: ScheduleCatalogEntry) => task.kind === 'at' && task.target.kind === 'session'
    && task.target.sessionId === host.conversations()['app']?.sessionId

  const scheduler = () => {
    const schedule = host.ctx.get('schedule')
    if (!schedule) throw new Error('这个 DSH 没有开定时提醒')
    return schedule
  }

  /**
   * What the owner writes on her calendar is the owner telling her: it joins her memory as the owner's words now,
   * so she can think ahead of the day (a word the evening before, asking how it went) instead of first hearing of
   * it when the time comes.
   */
  async function remember(text: string) {
    const { sessionId } = await host.conversation('app', { channel: 'app', title: '和 KIN 的聊天' })
    const scope = host.ctx.get('kin')!.scopeOf(sessionId)
    await mind.call('ingest', {
      id: 'calendar:' + crypto.randomUUID(), text, at: new Date().toISOString(),
      session: sessionId, speaker_person_id: scope.recipient_person_id, ...scope,
    }).then(() => host.heartbeat.nudge(), (error: Error) => host.log('calendar: saved but not remembered: ' + error.message))
  }

  function mark({ from, to = from, text, by }: Pick<Mark, 'from' | 'text' | 'by'> & { to?: string }): Mark {
    if (![from, to].every((day) => /^\d{4}-\d{2}-\d{2}$/.test(day) && !Number.isNaN(midnight(day).getTime()))) throw new Error('日期写成 2026-01-31 这样')
    if (to < from) throw new Error('结束的那天要在开始那天之后')
    if (!text.trim()) throw new Error('写一句这段日子是什么')
    const added: Mark = { id: crypto.randomUUID().slice(0, 8), from, to, text: text.trim(), by, at: new Date().toISOString() }
    book.marks.push(added)
    keep()
    return added
  }

  function unmark(id: string) {
    const index = book.marks.findIndex((mark) => mark.id === id)
    if (index < 0) return false
    book.marks.splice(index, 1)
    keep()
    return true
  }

  /** Everything between `from` and `to`, and what is open now; `ahead` leaves out what is behind. One source failing leaves the rest. */
  async function gather(from: number, to: number, ahead = false) {
    let partial = false
    const today = dayKey(new Date())
    const read = (what: string, source: () => Item[] | Promise<Item[]>) => Promise.resolve().then(source).catch((error: Error) => {
      host.log(`calendar: ${what} not read: ${error.message}`)
      partial = true
      return [] as Item[]
    })
    const behind = !ahead && from < Date.now()

    /** Her pages of one kind, which Core gives newest first, back to `from`. */
    const back = async <T extends { at: string }>(action: string, key: string) => {
      const found: T[] = []
      for (let cursor: number | null = 0; cursor !== null;) {
        const page: { cursor: number | null } & Record<string, any> = await mind.call(action, { limit: 50, cursor })
        found.push(...page[key])
        cursor = Date.parse(page[key].at(-1)?.at) >= from ? page.cursor : null
      }
      return found
    }

    const found = await Promise.all([
      // Reminders: DSH schedules that involve one of her conversations; the ones kin_routine made are her own routines.
      read('reminders', async () => {
        const hers = new Set<string>(Object.values(host.conversations()).map((entry) => entry.sessionId))
        const tasks = await host.ctx.get('schedule')?.catalog() ?? []
        return tasks.filter((task) => task.involvedSessionIds.some((id) => hers.has(id))).flatMap((task): Item[] => {
          const repeat = repeatOf(task)
          const item = { kind: (ours(task.id) ? 'routine' : 'reminder') as Kind, title: task.title, note: repeat?.words }
          return [
            ...(task.lastDelivery ? [{ ...item, id: `${task.id}@${task.lastDelivery.scheduledAt}`, at: task.lastDelivery.scheduledAt, done: true as const }] : []),
            ...(task.status === 'active' ? times(task, repeat, to).map((at) => ({ ...item, id: `${task.id}@${at}`, at, schedule: byHand(task) ? task.id : undefined })) : []),
          ]
        })
      }),
      read('held replies', () => host.load<Held[]>('replies.json', []).map((reply) => (
        { id: 'held:' + reply.inputId, kind: 'held', at: reply.due, title: `「${line(reply.text)}」`, note: reply.reason }))),
      // What waits for quiet hours to end, and what she sent on her own.
      read('deliveries', () => Object.values(host.load<Record<string, Delivery>>('deliveries.json', {})).flatMap((delivery): Item[] =>
        delivery.state === 'pending' && delivery.notBefore
          ? [{ id: 'waiting:' + delivery.id, kind: 'waiting', at: delivery.notBefore, title: delivery.excerpt ?? '', note: '等安静的时段过了再发' }]
          : delivery.state === 'accepted' && SENT[delivery.kind]
            ? [{ id: 'sent:' + delivery.id, kind: 'sent', at: delivery.acceptedAt ?? delivery.at, title: delivery.excerpt ?? '', note: SENT[delivery.kind], done: true }]
            : [])),
      read('work', () => Object.values(host.load<Record<string, Work>>('work.json', {})).map((item): Item => (item.state === 'running'
        ? { id: 'work:' + item.id, kind: 'work', at: item.started, title: item.label, note: ENDED.running, folder: item.folder, open: true }
        : { id: 'work:' + item.id, kind: 'work', at: item.ended, title: item.label, note: [ENDED[item.state], item.result && line(item.result, 160)].filter(Boolean).join('：'), folder: item.folder, done: true }))),
      // A letter or a darkroom page shows its day, never its words.
      read('letters', async () => ((await mind.call('letters')).items as { id: string; kind: string; state: string; unlock_at: string }[]).map((entry): Item => {
        const opened = entry.state === 'opened'
        return {
          id: 'letter:' + entry.id, kind: entry.kind === 'letter' ? 'letter' : 'darkroom', at: entry.unlock_at, done: opened || undefined,
          title: entry.kind === 'letter' ? (opened ? '这天拆开了一封信' : '一封这天才能拆的信') : (opened ? '暗房里的一篇日记，这天打开了' : '暗房里的一篇日记，这天才打开'),
        }
      })),
      read('plans', async () => ((await mind.call('autonomous-plans', { status: 'active' })).plans as Plan[]).flatMap(planItems)),
      book.marks.map((mark): Item => ({
        id: 'mark:' + mark.id, kind: 'period', at: mark.from, until: mark.to, title: mark.text, mark: mark.id, by: mark.by,
        note: mark.to === mark.from ? undefined : `到 ${dayWords(mark.to)}`, done: mark.to < today || undefined,
      })),
      // The owner's anniversaries that fall on these days (none while the owner has them off). Core's `to` is the day after.
      read('anniversaries', async () => ((await mind.call('anniversaries', { from: dayKey(new Date(from)), to: dayAfter(dayKey(new Date(to - 1))) }))
        .items as { date: string; title: string; years: number | null }[]).map((entry): Item => (
        { id: `anniversary:${entry.date}:${entry.title}`, kind: 'anniversary', at: entry.date, title: entry.title, note: entry.years ? `${entry.years} 周年` : undefined, done: entry.date < today || undefined }))),
      // When she means to think again, what she wants, and what the two of them agreed on.
      read('her state', async () => {
        const { state, memory } = await mind.call('read')
        return [
          ...(memory?.review ? [{ id: 'review', kind: 'review' as const, at: memory.review as string, title: '静下来再想一想' }] : []),
          ...(state.desires as { id: string; status: string; content?: string; topic?: string; expired?: boolean }[])
            .filter((wish) => ['wanted', 'waiting', 'in_progress'].includes(wish.status) && !wish.expired)
            .map((wish): Item => ({ id: 'wish:' + wish.id, kind: 'wish', title: wish.content ?? wish.topic ?? '', note: WAITING[wish.status], open: true })),
          ...((state.concerns ?? []) as { id: string; status?: string; kind?: string; content?: string; topic?: string; surface_after?: string | null }[])
            .filter((concern) => concern.kind === 'shared_plan' && !['resolved', 'archived'].includes(concern.status ?? ''))
            .map((concern): Item => ({ id: 'promise:' + concern.id, kind: 'promise', at: concern.surface_after ?? undefined, title: concern.content ?? concern.topic ?? '', open: true })),
        ]
      }),
      ...(behind ? [
        // Her finished plans. Core pages them oldest first, so reading the recent ones reads them all.
        read('finished plans', async () => {
          if (finished && Date.now() - finished.at < 5 * 60_000) return finished.items
          const plans: Plan[] = []
          for (let cursor: number | null = 0; cursor !== null;) {
            const page: { plans: Plan[]; next_cursor: number | null } = await mind.call('autonomous-plans', { status: 'completed', limit: 100, cursor })
            plans.push(...page.plans)
            cursor = page.next_cursor
          }
          finished = { at: Date.now(), items: plans.flatMap(planItems) }
          return finished.items
        }),
        read('diary', async () => (await back<{ source_id: string; at: string; topic?: string | null; text: string }>('diary', 'entries')).map((entry) => (
          { id: 'diary:' + entry.source_id, kind: 'diary', at: entry.at, title: line(entry.text), note: entry.topic ?? undefined, done: true }))),
        // A dream belongs to the night it came in, counted noon to noon as Core counts: its day is the one that night began on.
        read('dreams', async () => (await back<{ source_id: string; at: string; text: string }>('dreams', 'dreams')).map((dream) => {
          return { id: 'dream:' + dream.source_id, kind: 'dream', at: dayKey(new Date(Date.parse(dream.at) - 12 * HOUR)), title: line(dream.text), done: true }
        })),
        read('outings', async () => (await back<{ source_id: string; at: string; text?: string; title?: string; state?: string }>('explorations', 'activities')).map((outing) => (
          { id: 'outing:' + outing.source_id, kind: 'outing', at: outing.at, title: line(outing.title ?? outing.text ?? ''), note: OUTING[outing.state ?? ''], done: outing.state !== 'running' || undefined }))),
      ] : []),
    ])
    // What is open stays whatever the days asked: it is still to do. A whole day (a stretch of them) counts while any of it is in the days asked.
    return { items: found.flat().filter((item) => item.open || (item.at && timeOf(item) < to && (item.at.length === 10 ? over(item) : timeOf(item) + 1) > from)), partial }
  }

  host.ctx.provide('kinCalendar', {
    items: async (from, to, ahead) => (await gather(from.getTime(), to.getTime(), ahead)).items,
    // Model-facing: 你 is Kin. What the owner marked is named as the owner's words.
    current() {
      const today = dayKey(new Date())
      const under = book.marks.filter((mark) => mark.from <= today && today <= mark.to).map((mark) => {
        const what = mark.by === 'owner' ? `小光在日程上记着「${mark.text}」` : mark.text
        return what + (mark.to !== today ? `，到 ${dayWords(mark.to)}` : mark.from === today ? '，就在今天' : '，今天是最后一天')
      })
      return under.length ? `（这段日子：${under.join('；')}）` : ''
    },
    marks: () => book.marks.filter((mark) => mark.to >= dayKey(new Date())).sort((a, b) => a.from.localeCompare(b.from)),
    mark,
    unmark,
    routines: async () => (await scheduler().catalog()).filter((task) => ours(task.id)),
    async routine(what, time, weekdays) {
      if (!what.trim()) throw new Error('写一句到时候想做什么')
      if (!/^\d{1,2}:\d{2}$/.test(time)) throw new Error('时间写成 21:30 这样')
      const clock = time.padStart(5, '0') + ':00'
      const time_zone = Intl.DateTimeFormat().resolvedOptions().timeZone
      const own = await host.conversation('own', { channel: 'own', title: 'KIN 自己的事：出门和创作' })
      const when = (weekdays?.length ? weekly([...weekdays].sort((a, b) => a - b)) : '每天') + ' ' + clock.slice(0, 5)
      const task = await scheduler().create({
        title: what.trim().slice(0, 120),
        // Model-facing: 你 is Kin. DSH frames it as a scheduled message; this says whose it is.
        prompt: `你给自己定下的打算到时候了（${when}）：「${what.trim()}」。这是你自己的对话，不是谁发来的消息；这一轮是你自己的，`
          + '在这里说的话不会发给任何人，想告诉谁，用 kin_tell。这个打算不想要了，可以用 kin_routine 去掉。',
        timing: weekdays?.length ? { kind: 'weekly', weekly: { time: clock, time_zone, weekdays } } : { kind: 'daily', daily: { time: clock, time_zone } },
        target: { kind: 'session', sessionId: own.sessionId as SessionId },
      })
      book.routines.push(task.id)
      keep()
      // One made while she is frozen waits for the thaw like the others.
      if (host.heartbeat.frozen) await align(() => [task.id])
      return task
    },
    rest: () => align(() => book.routines),
    async unroutine(id) {
      if (!ours(id)) return false
      await scheduler().edit({ id: id as ScheduleCatalogEntry['id'], action: 'delete' })
      book.routines = book.routines.filter((routine) => routine !== id)
      keep()
      return true
    },
  } satisfies KinCalendar)

  host.route('/api/kin/calendar', {
    GET: async (request) => {
      const query = new URL(request.url).searchParams
      return gather(Date.parse(query.get('from')!), Date.parse(query.get('to')!), query.has('ahead'))
    },
    // The owner leaves her a reminder for a time (`at`, `text`), or takes back one left here (`remove`); marks a
    // stretch of days (`from`, `to`, `text`), or takes one off (`unmark`).
    POST: async (request) => {
      const { at, text, remove, from, to, unmark: off } = await host.body<{ at: string; text: string; remove: string; from: string; to: string; unmark: string }>(request)
      if (off) return { removed: unmark(off) }
      if (from) {
        const added = mark({ from, to, text: text ?? '', by: 'owner' })
        await remember(`我在你的日程页上记了一段日子：${daysOf(added)}「${added.text}」`)
        return { saved: true }
      }
      const schedule = scheduler()
      if (remove) {
        const task = await schedule.view({ id: remove as ScheduleCatalogEntry['id'] })
        if ('code' in task || !byHand(task)) throw new Error('这个提醒不是在日程页上定的')
        await schedule.edit({ id: task.id, action: 'delete' })
        return { removed: true }
      }
      if (!at || !text?.trim()) throw new Error('写一句要提醒什么，再选个时间')
      const { sessionId } = await host.conversation('app', { channel: 'app', title: '和 KIN 的聊天' })
      await schedule.create({
        title: text.trim().slice(0, 120),
        prompt: `小光在你的日程页上给这个时间留了一句提醒：「${text.trim()}」。时间到了，去告诉小光。`,
        timing: { kind: 'at', at },
        target: { kind: 'session', sessionId: sessionId as SessionId },
      })
      await remember(`我在你的日程页上记了一条：${stamp(at)}「${text.trim()}」`)
      return { saved: true }
    },
  })
}
