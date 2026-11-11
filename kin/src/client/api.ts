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

/** One route's latest answer, reloaded on demand and, when asked, every `everyMs`. */
export function useApi<T>(route: string, everyMs?: number) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string>()
  // Only the latest request lands: a slower earlier one (last month's page, an older poll) is dropped.
  const latest = useRef(0)
  const reload = useCallback(() => {
    const asked = ++latest.current
    return api<T>(route)
      .then((value) => { if (asked === latest.current) { setData(value); setError(undefined) } })
      .catch((reason: Error) => { if (asked === latest.current) setError(reason.message) })
  }, [route])
  useEffect(() => {
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
  }, [reload, everyMs])
  return { data, error, reload }
}

export interface Dimension { label: string; group: string; value: number; baseline: number; reason?: string }
export interface Group { id: string; label: string; members: string[] }
export interface Wish {
  id: string; status: string; kind?: 'contact' | 'explore' | 'create'
  topic?: string; content?: string; strength?: number; expired?: boolean
}
export interface Concern { id: string; status?: string; kind?: string; content?: string; topic?: string; intensity?: number }
export interface Plan { id: string; title?: string; goal?: string; status: string; updated_at?: string; steps?: { id: string; goal: string; state: string; not_before?: string | null }[] }

export interface Home {
  overview: { affect_layers?: { feeling?: string; undertone?: { text?: string } } }
  state: {
    dimensions: Record<string, Dimension>
    dimension_groups: Group[]
    desires: Wish[]
    concerns: Concern[]
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
export interface Diary { entries: DiaryEntry[]; replies: 'enabled' | 'disabled' }
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

export interface Route { provider: string; model: string; effort?: string; toolInputLimit?: number }
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
    budget?: { inputWindow: number; compactAt: number; outputBudget: number; headroom: number }
    lanes: { chat: LaneState; task: LaneState }
    catalog: { id: string; name: string; models: { id: string; name: string; reasoning?: { efforts: { id: string; name: string }[] } }[] }[]
  }
  dataDir: string
}
export type Feature = 'dreams' | 'diary_replies' | 'sealed_entries' | 'anniversaries' | 'window_notes'

/** Concerns and wishes that are still with her. */
export const openConcern = (concern: Concern) => !['resolved', 'archived'].includes(concern.status ?? '')
export const activeWish = (wish: Wish) => ['wanted', 'waiting', 'in_progress'].includes(wish.status) && !wish.expired
