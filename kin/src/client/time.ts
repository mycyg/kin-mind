/** Times the way a person says them: 今天 21:14, 昨天, 11月3日. */

const pad = (n: number) => String(n).padStart(2, '0')
const startOfDay = (at: number) => new Date(new Date(at).toDateString()).getTime()

export function clock(at: number | string): string {
  const date = new Date(at)
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function day(at: number | string, now = Date.now()): string {
  const time = new Date(at).getTime()
  const days = Math.round((startOfDay(now) - startOfDay(time)) / 86_400_000)
  if (days === 0) return '今天'
  if (days === 1) return '昨天'
  if (days === 2) return '前天'
  const date = new Date(time)
  const sameYear = date.getFullYear() === new Date(now).getFullYear()
  return `${sameYear ? '' : date.getFullYear() + '年'}${date.getMonth() + 1}月${date.getDate()}日`
}

export function ago(at: number | string | null | undefined, now = Date.now()): string {
  if (!at) return ''
  const minutes = Math.round((now - new Date(at).getTime()) / 60_000)
  if (minutes < 2) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return day(at, now)
}

/** How soon a coming time is: 马上, 20 分钟后, 3 小时后, 2 天后. */
export function soon(at: number | string | null | undefined, now = Date.now()): string {
  if (!at) return ''
  const minutes = Math.round((new Date(at).getTime() - now) / 60_000)
  if (minutes < 2) return '马上'
  if (minutes < 60) return `${minutes} 分钟后`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时后`
  return `${Math.round(hours / 24)} 天后`
}

/** The night a time belongs to, counted noon to noon as Core counts dreams: 3am today is 昨天夜里. */
export function night(at: number | string): string {
  return day(new Date(at).getTime() - 12 * 3_600_000) + '夜里'
}

/** A calendar date or timestamp said as 12月5日 in local time. */
export function calendarDay(value: string): string {
  const date = new Date(value.length === 10 ? `${value}T00:00:00` : value)
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

/** YYYY-MM-DD of a day `offset` days from today, in local time. */
export function isoDay(offset = 0): string {
  const date = new Date(Date.now() + offset * 86_400_000)
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}
