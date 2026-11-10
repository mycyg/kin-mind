/** Turn Core's `memory-context` answer into a short block the model reads like a note to itself. */

export interface Dimension {
  label: string
  baseline: number
}

/** One source a memory rests on, as Core's facts give it: when it was said, by whom, in whose conversation. */
interface Source {
  source_id?: string
  source_at?: string
  speaker_person_id?: string
  recipient_person_id?: string
}

interface Facts {
  basis?: string
  valid_from?: string
  occurred_at?: string
  /** `imagined` for a dream: never something that happened. */
  content_class?: string
  sources?: Source[]
}

/**
 * One JSON line of Core's context: a memory record, an event's digest, a graph entity, a compressed summary of
 * several, or something of her standing state (her habits with the owner, her traits, the owner's corrections).
 */
interface ContextItem {
  id?: string
  basis?: string
  text?: string
  facts?: Facts
  summary?: string
  evidence?: Facts[]
  /** The sources and records a standing item was set from. */
  source_ids?: string[]
  record_ids?: string[]
}

/** A remembered thing in words, with when it happened, who said it to whom, and what kind of knowing it is. */
interface Memory {
  /** Core's id for it: a record, an event, a source. */
  id: string | null
  text: string
  at: string | null
  /** "小美私下跟你说", "你在家庭群里说"…: who said it, to whom and where; '' when Core does not know. */
  who: string
  basis: string | null
}

/**
 * A recall she or the owner asks for (kin_recall, her page): Core's deep recall, which puts the question's own
 * words first, so an old exact match is not crowded out by what was said lately, embeddings or not. No model
 * ranks or compresses it: it runs in her resident mind like every short read. Core's read page spends about
 * half its budget on its own index; this one leaves room for a page of memories (she is shown their words).
 */
export const RECALL = { purpose: 'read', mode: 'deep', allow_model: false, budget: 4000 } as const

/** Her own id in Core's facts. */
export const KIN = 'agent:kin'

/** What a reader calls whoever an id names: a person, a group, her (`KIN`). */
export type Names = (id: string) => string

/** Her state as Core shows a chat turn (context.py, affect_projection). */
interface Affect {
  affect_layers?: { feeling?: string; undertone?: { text?: string }; lingering?: unknown }
  /** A score, or one marked for review. */
  dimensions?: Record<string, number | { value: number }>
  /** How she reads what is going on, from her latest review. */
  understanding?: unknown
  /** How she chose to be present for the next while, in her own words: her stance, what to stay with, what to leave alone. */
  expression?: string[]
  concerns?: unknown[]
}

type Answer = { rendered_text?: string; text?: string; moment?: string; omitted_ids?: string[] }

function itemsOf(answer: Answer): ContextItem[] {
  const items: ContextItem[] = []
  for (const line of (answer.rendered_text ?? answer.text ?? '').split('\n')) {
    if (!line.startsWith('{')) continue
    try { items.push(JSON.parse(line)) } catch { /* Core's header and notes are prose */ }
  }
  return items
}

/** What KIN remembers about a query, as the page lists it: the words, when, who said them, and whether it was a dream or her own thought. */
export function memoriesOf(answer: Answer, name: Names): Memory[] {
  return itemsOf(answer).filter((item) => !STANDING.has(item.id ?? '')).flatMap((item) => {
    const facts = item.facts ?? item.evidence?.[0]
    const speakers = [...new Set((facts?.sources ?? []).flatMap((source) => source.speaker_person_id ?? []))]
    // A message from someone else is kept with their name in front ("小美：…"); who said it says that already.
    const words = wordsOf(item)
    const named = speakers.length === 1 ? name(speakers[0]!) + '：' : ''
    const text = named && words.startsWith(named) ? words.slice(named.length) : words
    const basis = facts?.content_class === 'imagined' ? 'imagined' : item.basis ?? item.evidence?.[0]?.basis ?? null
    return text ? [{ id: item.id ?? null, text, at: atOf(facts), who: whoOf(facts, name), basis }] : []
  })
}

/** What stands for now rather than something remembered: Core gives these with every context, not once a session. */
const STANDING = new Set(['affect', 'conversation-habits', 'self-traits', 'trait-corrections', 'expression', 'current-intent', 'host-runtime', 'current-reply-context', 'activity-query-result'])

/** The strings a value holds: those under a key that names words (and bare ones in a list), or with `any` every string. */
function strings(value: unknown, any = false): string[] {
  if (typeof value === 'string') return any && value.trim() ? [value.trim()] : []
  if (Array.isArray(value)) return value.flatMap((entry) => strings(entry, any || typeof entry === 'string'))
  if (!value || typeof value !== 'object') return []
  return Object.entries(value).flatMap(([key, entry]) => strings(entry, any || ['text', 'content', 'title', 'summary', 'instruction', 'goal', 'topic'].includes(key)))
}

/** The words of a value whose shape is not one read here, so nothing Core gave is dropped unseen: its texts, else every string in it. */
const wordsIn = (value: unknown) => (strings(value).length ? strings(value) : strings(value, true)).join('；')

/** Her standing state besides her mood, a line each: the owner's habits with her, her traits, what the owner set right. 你 is Kin. */
function standing(items: ContextItem[], affect: Affect | undefined): string[] {
  return items.flatMap((item) => {
    if (!item.text || item.id === 'affect' || !STANDING.has(item.id ?? '')) return []
    let value: any
    try { value = JSON.parse(item.text) } catch { return [item.text] }
    const texts = (list: unknown) => strings(list).join('；')
    switch (item.id) {
      case 'conversation-habits': {
        const { reply_choice, exploration_paused, exploration_directions, exploration_frequency, exploration_min_interval_minutes, ...rest } = value ?? {}
        const habits = [
          reply_choice === 'autonomous' ? '回不回、什么时候回，交给你自己定' : '',
          exploration_paused ? '先别出门探索' : '',
          exploration_directions?.length ? `出门时多看看：${texts(exploration_directions)}` : '',
          exploration_frequency && exploration_frequency !== 'emotion-driven' ? `出门的节奏：${exploration_frequency}` : '',
          exploration_min_interval_minutes ? `两次出门至少隔 ${exploration_min_interval_minutes} 分钟` : '',
          ...Object.entries(rest).map(([key, entry]) => `${key}：${typeof entry === 'string' ? entry : JSON.stringify(entry)}`),
        ].filter(Boolean)
        return habits.length ? [`小光给你定下的习惯：${habits.join('；')}。`] : []
      }
      case 'self-traits':
        return [value?.established?.length ? `你身上已经成形的特质：${texts(value.established)}。` : '', value?.candidate?.length ? `还在成形的：${texts(value.candidate)}。` : ''].filter(Boolean)
      case 'trait-corrections':
        return texts(value?.items) ? [`小光纠正过你的：${texts(value.items)}。`] : []
      case 'expression':
        return !affect?.expression?.length && texts(value) ? [`你想这样在场：${texts(value)}。`] : []
      default:
        return wordsIn(value) ? [wordsIn(value)] : []
    }
  })
}

/**
 * Who said it, to whom and where, from the sources a memory rests on: one person's words never read as
 * another's, and what was said in private shows as said in private, wherever she recalls it.
 */
function whoOf(facts: Facts | undefined, name: Names): string {
  const said = (facts?.sources ?? []).flatMap(({ speaker_person_id: speaker, recipient_person_id: to }) => {
    if (!speaker) return []
    if (to?.startsWith('group:')) return [`${name(speaker)}在${name(to)}里说`]
    const listener = speaker === KIN ? (to ? name(to) : '') : name(KIN)
    return [listener ? `${name(speaker)}私下跟${listener}说` : `${name(speaker)}说`]
  })
  return [...new Set(said)].join('，')
}

// An event's digest names what was said and what came of it; a correction or an open question is marked as one.
const DIGEST: [string, string][] = [['narrative', ''], ['conclusions', ''], ['corrections', '更正：'], ['pending', '还悬着：'], ['unresolved', '还悬着：']]

function wordsOf(item: ContextItem): string {
  if (item.summary) return item.summary
  // A graph entity line names who or what, not something that happened.
  if (!item.text || item.id?.startsWith('graph_entity_')) return ''
  if (!item.text.startsWith('{')) return item.text
  try {
    const digest = JSON.parse(item.text) as Record<string, { text?: string }[] | undefined>
    const parts = DIGEST.flatMap(([part, mark]) => (Array.isArray(digest[part]) ? digest[part]! : []).flatMap((entry) => (entry.text ? [mark + entry.text] : [])))
    // Not a digest at all: its own words, rather than nothing. (A digest with nothing in it yet says nothing.)
    return DIGEST.some(([part]) => part in digest) ? parts.join('；') : wordsIn(digest)
  } catch {
    return item.text
  }
}

/** What a memory is when it is not something that happened or was said, as she is told it. */
const KNOWING: Record<string, string> = { imagined: '（梦里的，不是真发生过的事）', internal_thought: '（你自己心里想的）' }

/**
 * The block KIN reads before replying. `feeling` is her standing state, her mood first and then what else
 * stands (the owner's habits with her, her traits, what the owner set right); a caller that already showed her
 * that same state this conversation passes it as `shown` and it is left out, so a quiet chat does not repeat it
 * every turn. Memories Core already gave this session do not come back. A dream is marked as one, and when
 * Core could not fit everything related she is told this is not all of it.
 * With `ids`, each memory carries Core's id for it, for her tools that read further or set it right.
 * `rests`: the memories and sources the block's words rest on, for taking it down when one of them is deleted.
 */
export function renderContext(answer: Answer, dimensions: Record<string, Dimension>, name: Names,
  { now = Date.now(), shown, ids }: { now?: number; shown?: string; ids?: boolean } = {}) {
  const items = itemsOf(answer)
  const affect = items.find((item) => item.id === 'affect')
  const mood = affect?.text ? JSON.parse(affect.text) as Affect : undefined
  const feeling = [
    answer.moment?.replaceAll('用户甲', name('person:person_a')) || (mood ? renderAffect(mood, dimensions) : ''),
    ...standing(items, mood),
  ].filter(Boolean).join('\n')
  const sections = feeling && feeling !== shown ? [feeling] : []
  const memories = memoriesOf(answer, name)
  if (memories.length) {
    sections.push('想起来的事：\n' + memories.map((memory) => {
      const from = [when(memory.at, now), memory.who].filter(Boolean).join('，')
      return `- ${from ? from + '：' : ''}${KNOWING[memory.basis ?? ''] ?? ''}${memory.text}${ids && memory.id ? `（${memory.id}）` : ''}`
    }).join('\n'))
  }
  if (answer.omitted_ids?.length) {
    sections.push(`（相关的${sections.length ? '还' : ''}有 ${answer.omitted_ids.length} 条没读到，${sections.length ? '这里不是全部' : '这次一条也没放进来'}；需要时用 kin_recall 说得更具体些再找。）`)
  }
  const text = sections.length ? '（以下是你此刻的内在状态和想起来的事，只给你自己参考，不要照着念出来。）\n' + sections.join('\n') : ''
  const rests = [...new Set(items.filter((item) => item.id !== 'affect').flatMap((item) => [
    item.id ?? '', ...(item.source_ids ?? []), ...(item.record_ids ?? []),
    ...[item.facts, ...(item.evidence ?? [])].flatMap((facts) => (facts?.sources ?? []).flatMap((source) => source.source_id ?? [])),
  ]).filter(Boolean))]
  return { text, feeling, rests }
}

/**
 * Her state in her own terms first: what she feels and what lingers, how she chose to be present
 * (her review decided it, not a rule here), how she reads things; then the scores that stand out, as facts.
 */
function renderAffect(affect: Affect, dimensions: Record<string, Dimension>): string {
  const layers = affect.affect_layers ?? {}
  const feeling = layers.feeling ?? layers.undertone?.text
  const lingering = textOf(layers.lingering)
  const understanding = textOf(affect.understanding)
  const notable = Object.entries(affect.dimensions ?? {})
    .map(([key, score]) => ({ key, value: typeof score === 'number' ? score : score.value, dimension: dimensions[key] }))
    .filter((entry) => entry.dimension && Math.abs(entry.value - entry.dimension.baseline) >= 12)
    .sort((a, b) => Math.abs(b.value - b.dimension!.baseline) - Math.abs(a.value - a.dimension!.baseline))
    .slice(0, 4)
    .map((entry) => `${entry.dimension!.label}${entry.value > entry.dimension!.baseline ? '偏高' : '偏低'}（${entry.value}）`)
  const parts = [feeling ? `你此刻的感受：${feeling}。` : '']
  if (lingering) parts.push(`心里还留着：${lingering}。`)
  if (affect.expression?.length) parts.push(`你想这样在场：${affect.expression.join('；')}。`)
  if (understanding) parts.push(`你对眼下的理解：${understanding}。`)
  if (notable.length) parts.push(`比平时明显的：${notable.join('、')}。`)
  const concerns = (affect.concerns ?? []).map(concernText).filter(Boolean)
  if (concerns.length) parts.push(`心里挂着：${concerns.join('；')}。`)
  return parts.filter(Boolean).join('')
}

function concernText(concern: unknown): string {
  if (concern && typeof concern === 'object' && 'content' in concern) return String(concern.content ?? '')
  return textOf(concern)
}

/** Words from a value Core gives as text or as an object carrying its text. */
function textOf(value: unknown): string {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object') {
    const entry = value as Record<string, unknown>
    return String(entry.text ?? entry.title ?? entry.summary ?? '')
  }
  return ''
}

function atOf(facts: Facts | undefined): string | null {
  return facts?.sources?.[0]?.source_at ?? facts?.occurred_at ?? facts?.valid_from ?? null
}

/**
 * A moment as every surface of hers shows it: local date, weekday and time, "2026-11-05 周四 14:20". What was
 * said then ("下周三", "月底") she works out from it and the time it is now.
 */
export function stamp(at: number | string): string {
  const date = new Date(at)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} 周${'日一二三四五六'[date.getDay()]} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** The times in Core's answers that say when something was said or happened (not when it was stored). */
const WHEN = new Set(['at', 'occurred_at', 'source_at', 'valid_from', 'event_at', 'created_at'])

/**
 * Core's JSON answer (a line, or JSON lines, also nested in a string) with each of its WHEN instants stamped
 * beside it as `<key>_local`: her memory tools show times as her recall does.
 */
export function localTimes(text: string): string {
  const walk = (value: unknown): unknown => {
    if (typeof value === 'string') return /^[{[]/m.test(value) ? localTimes(value) : value
    if (Array.isArray(value)) return value.map(walk)
    if (!value || typeof value !== 'object') return value
    return Object.fromEntries(Object.entries(value).flatMap(([key, item]) => [[key, walk(item)],
      ...(WHEN.has(key) && typeof item === 'string' && /^\d{4}-\d\d-\d\dT/.test(item) ? [[key + '_local', stamp(item)]] : [])]))
  }
  return text.split('\n').map((line) => {
    if (!/^[{[]/.test(line)) return line
    try { return JSON.stringify(walk(JSON.parse(line))) } catch { return line }
  }).join('\n')
}

/** When something was said: its stamp, and how long ago while that is still days. */
export function when(at: string | number | null, now = Date.now()): string {
  if (!at) return ''
  const time = new Date(at).getTime()
  const ago = relativeTime(time, now)
  return ago.includes('月') ? stamp(time) : `${stamp(time)}（${ago}）`
}

export function relativeTime(at: number, now = Date.now()): string {
  const minutes = Math.round((now - at) / 60_000)
  if (minutes < 2) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.round(hours / 24)
  if (days === 1) return '昨天'
  if (days < 7) return `${days} 天前`
  const date = new Date(at)
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`
}
