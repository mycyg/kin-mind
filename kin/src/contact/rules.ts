/**
 * The owner's rules for her reaching out, and what has happened with each person that they read.
 * Settings, not her judgment: whether and what to say stays hers (Core and the model); these only say
 * when she must keep quiet. Defaults are the old KIN's (owner-activity.mjs, eventmem's ContactPolicy).
 */

export interface Rules {
  /** She may reach out on her own at all. */
  enabled: boolean
  /** Quiet hours, local: from `quietStart` o'clock until `quietEnd` o'clock (equal: none). */
  quietStart: number
  quietEnd: number
  /** At most this many times a day to one person, and at least this far apart. */
  maxPerDay: number
  minGapMinutes: number
  /** Not again until they have answered the last one. */
  waitForReply: boolean
  timeZone: string
}

export const DEFAULT_RULES: Rules = {
  enabled: true, quietStart: 22, quietEnd: 8, maxPerDay: 3, minGapMinutes: 60, waitForReply: true,
  timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
}

/** What happened with one person (or group), as the rules read it. */
export interface Activity {
  /** They last said something to her, and in which conversation. */
  heard?: string
  via?: string
  /** She last answered them. */
  answered?: string
  /** When she reached out on her own, most recent last (a day's worth is enough). */
  reached?: string[]
  /** Delivery ids counted above, saved together so retrying a result never counts it again. */
  reachedDeliveries?: Record<string, string>
  /** Technical failures in a row reaching them, and the latest. */
  failures?: { count: number; at: string }
  /** They asked her not to come to them until then (kin_quiet): their word, not a rule of the owner's. */
  quietUntil?: string
}

/** Until when someone asked her to leave them be, while that still holds. */
export function asked(activity: Activity | undefined, at = new Date()): Date | undefined {
  const until = activity?.quietUntil ? new Date(activity.quietUntil) : undefined
  return until && until > at ? until : undefined
}

/** Count a confirmed contact once, at its original receipt time, even after a restart or a later failure. */
export function reached(activity: Activity, id: string, at: string): Partial<Activity> | undefined {
  if (activity.reachedDeliveries?.[id]) return
  return {
    reached: [...(activity.reached ?? []), at].sort().slice(-24),
    reachedDeliveries: { ...activity.reachedDeliveries, [id]: at },
  }
}

/** Why she may not reach out now, or nothing when she may. */
export type Hold = 'off' | 'proactive-off' | 'not-acquainted' | 'quiet-hours' | 'asked-to-wait' | 'waiting-for-reply' | 'just-talked' | 'enough-today'

/**
 * The rules between one contact and the next; quiet hours and the switch hold everything. Chiming into a
 * group answers what is being said there, so only her own last word there keeps the gap.
 */
export function hold(rules: Rules, activity: Activity | undefined, now = new Date(), { chime = false } = {}): Hold | undefined {
  if (!rules.enabled) return 'off'
  if (!activity?.heard) return 'not-acquainted'
  if (quiet(rules, now)) return 'quiet-hours'
  if (asked(activity, now)) return 'asked-to-wait'
  const reached = activity.reached ?? []
  const last = reached.at(-1)
  if (rules.waitForReply && !chime && last && last > activity.heard) return 'waiting-for-reply'
  const latest = Math.max(...(chime ? [last] : [activity.heard, activity.answered, last]).map((at) => (at ? Date.parse(at) : 0)))
  if (now.getTime() - latest < rules.minGapMinutes * 60_000) return 'just-talked'
  if (reached.filter((at) => day(rules, new Date(at)) === day(rules, now)).length >= rules.maxPerDay) return 'enough-today'
}

export function quiet(rules: Rules, at = new Date()): boolean {
  const { quietStart: start, quietEnd: end } = rules
  const hour = Number(new Intl.DateTimeFormat('en-GB', { timeZone: rules.timeZone, hour: 'numeric', hourCycle: 'h23' }).format(at))
  return start !== end && (start > end ? hour >= start || hour < end : hour >= start && hour < end)
}

/** When the quiet hours around `at` end, for what waits for them. Local hours begin on a quarter hour in every zone. */
export function quietEnds(rules: Rules, at = new Date()): Date {
  const QUARTER = 15 * 60_000
  let end = Math.ceil(at.getTime() / QUARTER) * QUARTER
  while (quiet(rules, new Date(end))) end += QUARTER
  return new Date(end)
}

function day(rules: Rules, at: Date) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: rules.timeZone }).format(at)
}

/** The owner's edit, each value kept only when it is a valid one. */
export function reviseRules(current: Rules, next: Partial<Rules>): Rules {
  const hour = (value: unknown, fallback: number) => (Number.isInteger(value) && (value as number) >= 0 && (value as number) <= 23 ? value as number : fallback)
  const count = (value: unknown, fallback: number) => (Number.isInteger(value) && (value as number) >= 0 && (value as number) <= 1440 ? value as number : fallback)
  let timeZone = current.timeZone
  try { if (typeof next.timeZone === 'string') timeZone = new Intl.DateTimeFormat('en', { timeZone: next.timeZone }).resolvedOptions().timeZone } catch { /* not a zone */ }
  return {
    enabled: typeof next.enabled === 'boolean' ? next.enabled : current.enabled,
    quietStart: hour(next.quietStart, current.quietStart),
    quietEnd: hour(next.quietEnd, current.quietEnd),
    maxPerDay: count(next.maxPerDay, current.maxPerDay),
    minGapMinutes: count(next.minGapMinutes, current.minGapMinutes),
    waitForReply: typeof next.waitForReply === 'boolean' ? next.waitForReply : current.waitForReply,
    timeZone,
  }
}

/** The same rules in the file Core reads beside its config, so her reviews see what holds her back (state.py `_contact_view`). */
export function corePolicy(rules: Rules) {
  return {
    enabled: rules.enabled, timeZone: rules.timeZone, quietStartHour: rules.quietStart, quietEndHour: rules.quietEnd,
    minimumGapHours: rules.minGapMinutes / 60, waitForReply: rules.waitForReply, maxPerDay: rules.maxPerDay,
  }
}
