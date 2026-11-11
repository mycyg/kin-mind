/** dsh-kin host plugin: KIN's mind, persona, model routes and page routes. The KIN preset (cordis.patch.yml) does the per-session work. */
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-connection'
import type {} from '@deepseek-ai/dsh-api-session-controller'
import type {} from '@deepseek-ai/dsh-workspace'
import z from '@deepseek-ai/schemastery'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startBridge, writeModelFile, type Bridge } from './bridge.js'
import { calendar } from './calendar.js'
import { channels } from './channels/index.js'
import type { Address } from './channels/transport.js'
import { contact } from './contact/index.js'
import { callOf, EMPTY, OWNER, rosterOf, type People } from './contact/people.js'
import { health } from './health/index.js'
import { Heartbeat } from './heartbeat.js'
import { Mind } from './mind.js'
import { CONTEXT_WINDOW, PRESSURE_RATIO, HEADROOM_TOKENS, OUTPUT_BUDGET } from './model-budget.js'
import { KinModel, LANES, PROVIDER, type Lane, type Route } from './models.js'
import { KIN, memoriesOf, RECALL, type Dimension } from './render.js'
import { DEFAULT_PERSONA, personaText } from './persona.js'
import { steward } from './steward/index.js'
import { talk } from './talk/index.js'
import { Timeline } from './timeline.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    kin: Kin
  }
}

/** What the KIN preset's per-session plugin uses. */
export interface Kin {
  readonly mind: Mind
  /** Her persona and who she knows, as her models read it. */
  persona(): string
  /** What she calls whoever an id in her memories names (contact/people.ts `callOf`); herself is 你. */
  nameOf(id: string): string
  dimensions(): Record<string, Dimension>
  /** Which channel a session speaks on; sessions KIN did not open are desktop chats. */
  channelOf(sessionId: string): string
  /**
   * Whose conversation a session is, as Core takes it with each call (`recipient_person_id`, `conversation_key`):
   * its channel, its person or group, and its key. Sessions KIN did not open are the owner's desktop chats.
   */
  scopeOf(sessionId: string): Scope
  /** First session log position not yet written to memory. */
  recorded(sessionId: string): number
  markRecorded(sessionId: string, seq: number): void
  /** A conversation of hers started or finished a reply; background work waits while one is under way. */
  replying(sessionId: string, running: boolean): void
  /** Something new was said: the heartbeat looks again now rather than at its next minute. */
  nudge(): void
  /** Where the time of her replies goes, for her health page. */
  readonly timeline: Timeline
  /** When the owner last had her page open and in front (it asks for her state only then); 0 when not yet in this run. */
  seen(): number
  /** Memories and sources found deleted since she was shown them, and when: her lanes leave out the context blocks that rest on them. */
  readonly erased: ReadonlyMap<string, number>
  /** Core says these are deleted: kept with the moment it was learned, across restarts. */
  forgot(ids: readonly string[]): void
  /** What each summary of a conversation rests on and when its writing began (models.ts `summaries`). */
  readonly summaries: Map<string, { at: number; rests: Set<string> }>
}

/**
 * What KIN's feature modules (steward, contact, channels, health…) work with. Each module is a
 * `(host: Host) => void` listed in MODULES below; preset-side code that needs one provides its own service.
 */
export interface Host {
  readonly ctx: Context
  readonly config: Config
  readonly mind: Mind
  readonly heartbeat: Heartbeat
  /** Her two model lanes; `last` says how each lane's latest call went. */
  readonly model: KinModel
  /** The loopback relay Core reaches the plugin through; `serve` adds a route beside its model calls. */
  readonly bridge: Bridge
  /** Settles once her mind is up; routes already wait for it. */
  readonly ready: Promise<void>
  /** Whether one of her conversations is in the middle of a reply. */
  replying(): boolean
  /** Where the time of her replies goes, for her health page. */
  readonly timeline: Timeline
  /** The DSH session of one of her conversations ('app', 'wechat:<chat>', …), opened on first use. */
  conversation(key: string, options: Omit<Conversation, 'sessionId'> & { title: string }): Promise<Conversation>
  /** Her conversations so far, by key. */
  conversations(): Readonly<Record<string, Conversation>>
  /**
   * A platform chat that is no longer whose it was (the owner changed that account or chat): it keeps its
   * session and what was said, and is a place to reach no one until someone the directory knows speaks there again.
   */
  leave(key: string): void
  /** Her persona as the model reads it; saving one applies from her next reply, Core's copy included. */
  persona(): string
  savePersona(text: string): Promise<void>
  /** The people and groups the owner set up (contact/): her persona names them, Core's copy included. */
  introduce(people: People): void
  route(path: string, handlers: Partial<Record<'GET' | 'POST', Handler>>): void
  body<T>(request: Request): Promise<Partial<T>>
  /** A path in her data directory. */
  file(name: string): string
  load<T>(name: string, empty: T): T
  save(name: string, value: unknown): void
  log(message: string): void
  /** One of the host's waits, in milliseconds, at the dev pace (Config `pace`, 1 in use). */
  ms(duration: number): number
}

export interface Conversation {
  sessionId: string
  /** `app`, or the platform it is on. */
  channel: string
  /** Core's name for the person or group it is with; the owner when unset. */
  recipient?: string
  /** The chat on that platform. */
  address?: Address
}

/** The conversation a mind call belongs to (Core's RECIPIENT_ACTIONS). */
export interface Scope {
  channel: string
  recipient_person_id: string
  conversation_key: string
}

type Handler = (request: Request) => Promise<unknown>

/** Her feature modules, each applied once with the host. */
const MODULES: ((host: Host) => void)[] = [channels, health, talk, steward, contact, calendar]

export interface Config {
  dataDir: string
  python: string
  coreSource: string
  tokenizerCache?: string
  /** Dev only: every wait of the host's and Core's foreground lease pass this many times as long (1 in use). */
  pace: number
}

// Installed from this repository, the mind sits next to the plugin.
const repo = (path: string) => fileURLToPath(new URL('../../' + path, import.meta.url))

export const Config: z<Config> = z.object({
  dataDir: z.string().default(join(homedir(), '.kin')).description('KIN 的心智数据目录（记忆库、人设、会话表）'),
  python: z.string().default(repo('core/.venv/bin/python')).description('装好 kin-mind 的 Python 3.13'),
  coreSource: z.string().default(repo('core/src')).description('KIN 心智源码目录（core/src）'),
  tokenizerCache: z.string().description('tiktoken 的 cl100k_base 缓存目录；不填则按字节估算 token'),
  pace: z.number().default(1).description('开发用：宿主的各种等待按这个倍数过（1 为正常）'),
})

export const name = 'kin'
export const inject = ['connection', 'llm', 'workspaceRegistry']

/** The inner-life switches the settings page offers; each is a Core memory setting. */
const FEATURES = ['dreams', 'diary_replies', 'sealed_entries', 'anniversaries', 'window_notes'] as const

export function apply(ctx: Context, config: Config) {
  const log = (message: string) => ctx.logger('kin').info(message)
  mkdirSync(config.dataDir, { recursive: true })
  const mind = new Mind({ ...config, log })
  ctx.effect(() => () => mind.close(), 'kin mind process')

  const file = (name: string) => join(config.dataDir, name)
  const load = <T>(name: string, empty: T): T => (existsSync(file(name)) ? JSON.parse(readFileSync(file(name), 'utf8')) : empty)
  const save = (name: string, value: unknown) => writeFileSync(file(name), JSON.stringify(value, null, 2) + '\n')

  if (!existsSync(file('persona.md'))) writeFileSync(file('persona.md'), DEFAULT_PERSONA)
  let persona = readFileSync(file('persona.md'), 'utf8')
  const sessions = load<Record<string, Conversation>>('sessions.json', {})
  const recorded = load<Record<string, number>>('recorded.json', {})
  const routes = load<Record<Lane, Route[]>>('models.json', { chat: [], task: [] })

  // KIN's conversations ask for `kin/chat` (preset.ts) and her helpers for `kin/task` (cordis.patch.yml);
  // this is where each walks its own route list.
  const model = new KinModel(ctx, (lane) => routes[lane] ?? [])
  // Deletions learned in an earlier run still hold: when each was learned says which summaries were written before it.
  for (const [id, at] of Object.entries(load<Record<string, number>>('erased.json', {}))) model.erased.set(id, at)
  ctx.llm.registerAdapter([PROVIDER], model)

  // Core's own model calls (reviews, diary, dreams, memory work) come back to the task lane through here.
  const bridge = startBridge(ctx, log)
  ctx.effect(() => () => bridge.close(), 'kin model bridge')
  const replying = new Set<string>()
  const timeline = new Timeline()
  let seen = 0
  const heartbeat = new Heartbeat(mind, () => replying.size > 0, log, config.pace,
    async () => ctx.get('kinContact')?.reviewTargets() ?? {})
  ctx.effect(() => () => heartbeat.close(), 'kin heartbeat')

  // Who she knows goes with her persona, so each of her models -- her replies, her reviews -- reads the ids
  // her memories carry (who said it, in whose conversation) as the people they are.
  let people: People = EMPTY
  const knowing = () => personaText(persona) + '\n\n' + rosterOf(people)

  // Core's reviews, diary pages and dreams are written in her voice: Core reads the persona from its own store.
  const syncPersona = async () => {
    const path = join(mind.root, 'persona-policy.json')
    const policy = existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : {}
    writeFileSync(path, JSON.stringify({ ...policy, scope: mind.scope,
      version: 'dsh-' + createHash('sha256').update(knowing()).digest('hex').slice(0, 12),
      core: knowing(), voice: '', maintenance: '', requires_owner_confirmation: false,
    }, null, 2) + '\n')
    await mind.call('persona-save', { text: knowing() })
  }

  // Each conversation has a long-lived DSH session in her KIN workspace.
  const opening = new Map<string, Promise<Conversation>>()
  const conversation: Host['conversation'] = (key, { title, ...entry }) => {
    if (sessions[key]) {
      // Whose chat it is follows the owner's directory, which may change; a chat that was left is one again once someone it knows speaks there.
      if (entry.recipient !== sessions[key].recipient || (entry.address && !sessions[key].address)) {
        sessions[key] = { ...sessions[key], ...entry }
        save('sessions.json', sessions)
      }
      return Promise.resolve(sessions[key])
    }
    if (!opening.has(key)) {
      opening.set(key, (async () => {
        const controller = ctx.get('sessionController')
        if (!controller) throw new Error('this DSH composition has no session controller')
        const home = file('KIN')
        mkdirSync(home, { recursive: true })
        const workspace = await ctx.workspaceRegistry.create(home, 'KIN')
        const { sessionId } = await controller.create({ workspaceId: workspace.id, agentPreset: 'kin' })
        // A title of its own: DSH would otherwise spend a model call naming the conversation.
        await controller.rename({ sessionId, title })
        sessions[key] = { sessionId, ...entry }
        save('sessions.json', sessions)
        return sessions[key]
      })().finally(() => opening.delete(key)))
    }
    return opening.get(key)!
  }

  let dimensions: Record<string, Dimension> = {}
  const ready = (async () => {
    writeModelFile(mind.modelFile, await bridge.url, bridge.key)
    // A fresh mind's first memory is its own awakening, so it is true when recalled.
    await mind.call('ensure-initialized', { text: '我在这台电脑上醒来了。从今天起，我住在这里。' })
    await syncPersona()
    // DSH compacts KIN's conversations itself and the preset tells Core when it does, so Core need
    // not ration recall over a conversation's whole life (its 12K-token cap would end recall for good).
    await mind.call('configure-memory', { native_window_context: true })
    // Restore the addresses of established chats before her first contact round; no old message is replayed.
    for (const [key, entry] of Object.entries(load<Record<string, Omit<Conversation, 'sessionId'> & { title: string }>>('conversation-import.json', {}))) {
      if (!sessions[key]) await conversation(key, entry)
    }
    // What a previous run left half done is settled before her state is read and her minute starts.
    await heartbeat.start().catch((error: Error) => ctx.logger('kin').warn('heartbeat did not start: %s', error.message))
    const { state } = await mind.call('read')
    dimensions = Object.fromEntries(Object.entries(state.dimensions as Record<string, Dimension>)
      .map(([key, value]) => [key, { label: value.label, baseline: value.baseline }]))
  })()
  ready.catch((error) => ctx.logger('kin').warn('mind is not ready: %s', error.message))

  ctx.provide('kin', {
    mind,
    persona: knowing,
    nameOf: (id) => (id === KIN ? '你' : callOf(people, id)),
    dimensions: () => dimensions,
    channelOf: (sessionId) => Object.values(sessions).find((entry) => entry.sessionId === sessionId)?.channel ?? 'app',
    scopeOf(sessionId) {
      const [key, entry] = Object.entries(sessions).find(([, entry]) => entry.sessionId === sessionId) ?? ['app', { channel: 'app' }]
      return { channel: entry.channel, recipient_person_id: entry.recipient ?? OWNER, conversation_key: key }
    },
    recorded: (sessionId) => recorded[sessionId] ?? 0,
    markRecorded(sessionId, seq) {
      recorded[sessionId] = seq
      writeFileSync(file('recorded.json'), JSON.stringify(recorded) + '\n')
    },
    replying(sessionId, running) {
      if (running) replying.add(sessionId)
      else replying.delete(sessionId)
    },
    nudge: () => heartbeat.nudge(),
    timeline,
    seen: () => seen,
    erased: model.erased,
    forgot(ids) {
      if (!ids.length) return
      for (const id of ids) model.erased.set(id, Date.now())
      save('erased.json', Object.fromEntries(model.erased))
    },
    summaries: model.summaries,
  } satisfies Kin)

  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json; charset=utf-8' },
  })
  const route = (path: string, handlers: Partial<Record<'GET' | 'POST', Handler>>) =>
    ctx.effect(() => ctx.connection.fetch.register({
      path, methods: Object.keys(handlers) as ('GET' | 'POST')[], requestBody: 'buffered',
      async fetch(request) {
        try {
          await ready
          const answer = await handlers[request.method as 'GET' | 'POST']!(request)
          return answer instanceof Response ? answer : json(answer)
        } catch (error) {
          return json({ error: (error as Error).message }, 500)
        }
      },
    }), 'kin route ' + path)
  const body = async <T>(request: Request) => (await request.json()) as Partial<T>

  // 此刻: how she is, what is on her mind, what she wants, and the conversation with her.
  route('/api/kin/home', {
    GET: async () => {
      // Her page asks only while it is open and in front (client/api.ts), so asking is the owner being there.
      seen = Date.now()
      const [overview, read, liveness, plans] = await Promise.all([
        mind.call('state-overview', { query: '' }), mind.call('read'), mind.call('liveness-facts'), mind.call('autonomous-plans', { status: 'active' }),
      ])
      return {
        overview, state: read.state, drives: read.drives, liveness, plans: plans.plans,
        // When she means to think things over again: her own choice in her last review.
        heartbeat: heartbeat.beats, nextReview: read.memory?.review ?? null, chat: sessions['app'] ?? null,
      }
    },
  })

  // The desktop conversation with KIN, talked to in KIN's own chat box.
  route('/api/kin/chat', { POST: () => conversation('app', { channel: 'app', title: '和 KIN 的聊天' }) })

  // Her inner world. Diary replies and sealed letters are the owner's own words, kept as such.
  route('/api/kin/diary', { GET: () => mind.call('diary', { limit: 30 }) })
  route('/api/kin/diary/reply', {
    POST: async (request) => {
      const { reflection_id, text } = await body<{ reflection_id: string; text: string }>(request)
      const answer = await mind.call('diary-reply', { reflection_id, text, command_id: randomUUID() })
      // What the owner wrote under her diary is hers to read now, not at the next minute.
      heartbeat.nudge()
      return answer
    },
  })
  route('/api/kin/dreams', { GET: () => mind.call('dreams', { limit: 30 }) })
  route('/api/kin/letters', {
    GET: () => mind.call('letters'),
    POST: async (request) => {
      const { text, unlock_at } = await body<{ text: string; unlock_at: string }>(request)
      return mind.call('seal-letter', { text, unlock_at, command_id: randomUUID() })
    },
  })
  route('/api/kin/explore', {
    GET: async () => {
      const [read, plans, explorations] = await Promise.all([
        mind.call('read'), mind.call('autonomous-plans', { status: 'active' }), mind.call('explorations', { limit: 20 }),
      ])
      return {
        wishes: (read.state.desires as { kind?: string }[]).filter((wish) => wish.kind === 'explore'),
        explorations: explorations.activities, findings: read.findings, cadence: read.exploration_cadence, plans: plans.plans,
      }
    },
  })
  // Her memories as the owner reads them on her page: "你" is the owner, "她" is KIN.
  const forOwner = (id: string) => (id === OWNER ? '你' : id === KIN ? '她' : callOf(people, id))
  route('/api/kin/recall', {
    POST: async (request) => {
      const { query } = await body<{ query: string }>(request)
      if (!query?.trim()) return { memories: [] }
      return { memories: memoriesOf(await mind.call('memory-context', { query, ...RECALL }), forOwner) }
    },
  })
  // The owner's 记错了 on one of them: what the owner wrote joins her memory as the owner's own words, and the
  // memory carries it from now on (Core's correct_memory, the tool she uses herself).
  route('/api/kin/correct', {
    POST: async (request) => {
      const { id, text, memory } = await body<{ id: string; text: string; memory: string }>(request)
      if (!id || !text?.trim()) throw new Error('写一句哪里记错了')
      const scope = { channel: 'app', recipient_person_id: OWNER, conversation_key: 'app' }
      const { source_id } = await mind.call('ingest', {
        id: 'correction:' + randomUUID(), text: `更正你记的「${(memory ?? '').slice(0, 80)}」：${text.trim()}`, at: new Date().toISOString(),
        session: sessions['app']?.sessionId, speaker_person_id: OWNER, ...scope,
      })
      const answer = await mind.call('chat-tool', { name: 'correct_memory', arguments: { memory_id: id, correction: text.trim(), evidence_id: source_id }, ...scope })
      if (answer.isError) throw new Error(answer.content?.[0]?.text ?? '没改成')
      heartbeat.nudge()
      return { corrected: true }
    },
  })

  // Settings live on KIN's own page: who she is, which models she speaks with, which inner-life parts are on.
  route('/api/kin/settings', {
    GET: async () => {
      const [read, catalog] = await Promise.all([
        mind.call('read'),
        ctx.get('sessionController')?.modelCatalog().catch(() => null) ?? null,
      ])
      const settings = read.memory.settings as Record<string, boolean>
      return {
        persona: personaText(persona),
        features: Object.fromEntries(FEATURES.map((key) => [key, settings[key] === true])),
        models: {
          budget: { inputWindow: CONTEXT_WINDOW, compactAt: Math.floor(CONTEXT_WINDOW * PRESSURE_RATIO), outputBudget: OUTPUT_BUDGET, headroom: HEADROOM_TOKENS },
          lanes: Object.fromEntries(LANES.map((lane) => [lane, {
            routes: routes[lane] ?? [], last: model.last[lane] ?? null, fallback: catalog?.default ?? null,
          }])),
          catalog: catalog?.groups.filter((group) => group.id !== PROVIDER) ?? [],
        },
        dataDir: config.dataDir,
      }
    },
  })
  // Persona edits apply from KIN's next reply: the prompt variable reads this value at render.
  const savePersona = async (text: string) => {
    persona = text.trim() + '\n'
    writeFileSync(file('persona.md'), persona)
    await syncPersona()
  }
  route('/api/kin/persona', {
    POST: async (request) => {
      const { text } = await body<{ text: string }>(request)
      if (typeof text !== 'string' || !text.trim()) throw new Error('人设不能为空')
      await savePersona(text)
      return { saved: true }
    },
  })
  route('/api/kin/features', {
    POST: async (request) => {
      const changes = await body<Record<string, boolean>>(request)
      const allowed = Object.fromEntries(Object.entries(changes)
        .filter(([key, value]) => (FEATURES as readonly string[]).includes(key) && typeof value === 'boolean'))
      await mind.call('configure-memory', allowed)
      return { saved: true }
    },
  })
  route('/api/kin/models', {
    POST: async (request) => {
      const { lane, routes: next } = await body<{ lane: Lane; routes: Route[] }>(request)
      const valid = Array.isArray(next) && next.every((r) => typeof r?.provider === 'string' && typeof r?.model === 'string'
        && r.provider !== PROVIDER && (r.effort === undefined || typeof r.effort === 'string')
        && (r.toolInputLimit === undefined || (Number.isSafeInteger(r.toolInputLimit) && r.toolInputLimit > 8192)))
      if (!lane || !LANES.includes(lane) || !valid) throw new Error('模型路线格式不对')
      routes[lane] = next.map(({ provider, model, effort, toolInputLimit }) => ({ provider, model, ...(effort ? { effort } : {}), ...(toolInputLimit ? { toolInputLimit } : {}) }))
      save('models.json', routes)
      return { saved: true }
    },
  })

  const host: Host = {
    ctx, config, mind, heartbeat, model, bridge, ready, replying: () => replying.size > 0, timeline, conversation, conversations: () => sessions,
    leave(key) {
      if (!sessions[key]?.address) return
      const { address: _left, ...kept } = sessions[key]
      sessions[key] = kept
      save('sessions.json', sessions)
    },
    persona: () => personaText(persona), savePersona, route, body, file, load, save, log, ms: (duration) => duration * config.pace,
    introduce(next) {
      people = next
      void ready.then(syncPersona).catch((error: Error) => log('persona sync failed: ' + error.message))
    },
  }
  for (const module of MODULES) module(host)

  log(`KIN is home at ${config.dataDir}`)
}
