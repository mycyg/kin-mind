/** The page's side of KIN's host routes (src/index.ts), and the shapes Core answers with. */
import { useCallback, useEffect, useRef, useState } from 'react'

// Browser-relative routes: the page's carrier resolves them against its own base.
export async function api<T>(route: string, body?: unknown): Promise<T> {
  const response = await fetch(route, body === undefined
    ? { headers: { accept: 'application/json' } }
    : { method: 'POST', headers: { accept: 'application/json', 'content-type': 'application/json' }, body: JSON.stringify(body) })
  const value = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(value.error ?? `${response.status} ${response.statusText}`)
  return value as T
}

// In-memory SWR cache across tab switches so returning to a section renders immediately with zero flash.
const cache = new Map<string, unknown>()
export function invalidateApi(prefix?: string) {
  if (!prefix) return cache.clear()
  for (const key of cache.keys()) if (key.startsWith(prefix)) cache.delete(key)
}

/** One route's latest answer, cached across unmounts, reloaded on demand and, when asked, every `everyMs`. */
export function useApi<T>(route: string | undefined, everyMs?: number) {
  const [data, setData] = useState<T | undefined>(() => (route ? (cache.get(route) as T | undefined) : undefined))
  const [error, setError] = useState<string>()
  // Only the latest request lands: a slower earlier one (last month's page, an older poll) is dropped.
  const latest = useRef(0)
  useEffect(() => {
    setData(route ? (cache.get(route) as T | undefined) : undefined)
    setError(undefined)
  }, [route])
  const reload = useCallback((overrideRoute?: unknown) => {
    const target = typeof overrideRoute === 'string' ? overrideRoute : route
    if (!target) return Promise.resolve()
    const asked = ++latest.current
    return api<T>(target)
      .then((value) => {
        cache.set(target, value)
        if (route && target !== route) cache.set(route, value)
        if (asked === latest.current) { setData(value); setError(undefined) }
      })
      .catch((reason: Error) => { if (asked === latest.current) setError(reason.message) })
  }, [route])
  useEffect(() => {
    if (!route) return
    void reload()
    if (!everyMs) return
    const refresh = () => { if (!document.hidden && document.hasFocus()) void reload() }
    const timer = setInterval(refresh, everyMs)
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      clearInterval(timer)
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [route, reload, everyMs])
  return { data, error, reload }
}

export interface Dimension { label: string; group: string; value: number; baseline: number; reason?: string }
export interface Group { id: string; label: string; members: string[] }
export interface Wish {
  id: string; status: string; kind?: 'contact' | 'explore' | 'create'
  topic?: string; content?: string; strength?: number; expired?: boolean
}
export interface Concern { id: string; status?: string; kind?: string; content?: string; topic?: string; intensity?: number }
export interface Plan { id: string; title?: string; goal?: string; status: string; updated_at?: string; steps?: { id: string; goal: string; state: string; not_before?: string | null; execution?: { host_gate?: { state: string; reason: string | null; max_per_day: number; sent_today: number } | null } }[] }

export interface Home {
  overview: { affect_layers?: { feeling?: string; undertone?: { text?: string } } }
  state: {
    dimensions: Record<string, Dimension>
    dimension_groups: Group[]
    desires: Wish[]
    concerns: Concern[]
    easing_omitted?: number
    rhythm?: { status?: string; phase?: string }
    affect_layers?: { feeling?: { text?: string }; undertone?: { text?: string }; vitals?: { heart_rate_bpm?: number; status?: string } }
    /** Her latest review: what she made of things, in her words, and when. */
    appraisal_summary?: { at?: string; reason?: string } | null
  }
  liveness: {
    last?: { contact_sent_at?: string | null; wish_created_at?: string | null; exploration_at?: string | null }
    contact_funnel?: { idle_decisions_24h?: Record<string, number> }
  }
  plans: Plan[]
  /** Her background lanes' latest rounds (heartbeat.ts); a lane that never ran is absent. */
  heartbeat: Partial<Record<'review' | 'daily' | 'enrichment' | 'preparation', Beat>>
  /** When she means to think things over again, as she set it herself. */
  nextReview: string | null
  chat: { sessionId: string } | null
}

export interface CreationArtifact { path: string; name: string; bytes: number }
export interface CreationItem {
  id: string
  plan_id: string
  title: string
  step_goal?: string
  summary?: string
  finished_at: string
  artifacts: CreationArtifact[]
}
export interface TraitItem {
  id: string
  category: string
  text: string
  revision: number
  status: 'candidate' | 'established' | 'revoked' | 'needs_review' | string
  reason?: string
  updated_at?: string
}
export interface ElsewhereTurn {
  id: string
  role: string
  text: string
  at: string
  channel: string
  kind?: string
  state?: string
}

export interface Beat {
  state: string
  reason?: string
  at: string
  /** When a round of this lane last completed. */
  done?: string
}

export interface DiaryEntry {
  source_id: string; at: string; topic?: string | null; text: string
  replies: { source_id: string; at: string; text: string }[]
}
export interface Diary { entries: DiaryEntry[]; replies: 'enabled' | 'disabled'; cursor: number | null; total: number }
/** Diary pages stay visible on a later failure. A request owns the cursor until it settles. */
export function useDiary() {
  const [data, setData] = useState<Diary>()
  const [error, setError] = useState<string>()
  const [loading, setLoading] = useState(false)
  const current = useRef<Diary>()
  const pages = useRef(1)
  const busy = useRef(false)
  const generation = useRef(0)
  const read = useCallback(async (more = false) => {
    if (busy.current || (more && current.current?.cursor == null)) return
    busy.current = true
    setLoading(true)
    const version = ++generation.current
    try {
      let next: Diary
      if (more) {
        const page = await api<Diary>('api/kin/diary?cursor=' + current.current!.cursor)
        const entries = new Map(current.current!.entries.map((entry) => [entry.source_id, entry]))
        for (const entry of page.entries) entries.set(entry.source_id, entry)
        next = { ...page, entries: [...entries.values()] }
      } else {
        next = await api<Diary>('api/kin/diary')
        for (let i = 1; i < pages.current && next.cursor != null; i++) {
          const page = await api<Diary>('api/kin/diary?cursor=' + next.cursor)
          const entries = new Map(next.entries.map((entry) => [entry.source_id, entry]))
          for (const entry of page.entries) entries.set(entry.source_id, entry)
          next = { ...page, entries: [...entries.values()] }
        }
      }
      if (version !== generation.current) return
      if (more) pages.current++
      current.current = next
      setData(next)
      setError(undefined)
    } catch (reason) {
      if (version === generation.current) setError((reason as Error).message)
    } finally {
      if (version === generation.current) { busy.current = false; setLoading(false) }
    }
  }, [])
  useEffect(() => {
    busy.current = false
    void read()
    return () => { generation.current++; busy.current = false }
  }, [read])
  return { data, error, loading, reload: () => { void read() }, more: () => { void read(true) } }
}

export interface Dreams { generation_enabled: boolean; dreams: { source_id: string; at: string; text: string }[] }
export interface Letter {
  id: string; kind: 'letter' | 'diary'; state: 'sealed' | 'opened'
  unlock_at: string; created_at: string; opened_at: string | null
}
export interface Letters { items: Letter[]; enabled: boolean }
export interface Explore {
  wishes: Wish[]
  explorations: { source_id: string; at: string; text?: string; title?: string; state?: string }[]
  findings: { id?: string; result?: { summary?: string } | null }[]
  plans: Plan[]
}
/** One memory of hers as the page lists it; `who` is who said it to whom ("小美私下跟她说"). */
export interface Memory { id: string | null; text: string; at: string | null; who: string; basis: string | null }

export interface Route { provider: string; model: string; effort?: string; toolInputLimit?: number; longInputWaitMs?: number }
/** One of KIN's two model lanes: chat (her replies) and task (everything she does). */
export interface LaneState {
  routes: Route[]
  /** DSH's default model, which an empty lane uses. */
  fallback: Route | null
  last: { at: string; answered: Route | null; failed: { route: Route; reason: string }[] } | null
}
export interface Settings {
  persona: string
  features: Record<Feature, boolean>
  models: {
    budget?: { inputWindow: number; compactAt: number; idleCompactAt?: number; outputBudget: number; headroom: number }
    lanes: { chat: LaneState; task: LaneState }
    catalog: { id: string; name: string; models: { id: string; name: string; reasoning?: { efforts: { id: string; name: string }[] } }[] }[]
  }
  dataDir: string
}
export type Feature = 'dreams' | 'diary_replies' | 'sealed_entries' | 'anniversaries' | 'window_notes' | 'recall_admission'

/** Concerns and wishes that are still with her. */
export const openConcern = (concern: Concern) => !['resolved', 'archived'].includes(concern.status ?? '')
export const activeWish = (wish: Wish) => ['wanted', 'waiting', 'in_progress'].includes(wish.status) && !wish.expired
