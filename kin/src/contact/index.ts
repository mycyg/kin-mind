/**
 * M6: the people she talks with, her conversations on other platforms, and reaching out on her own.
 * - people.ts: who's who (the owner sets it on her page); Core knows each by an id, and every mind call
 *   in a conversation names whose it is, so what is the owner's alone stays the owner's;
 * - conversations.ts: arrivals into her sessions, her words back out; deliveries.ts: everything sent, once;
 * - proactive.ts: her minute for reaching out, in her own voice (draft.ts), within the owner's rules (rules.ts).
 * - look.ts: her other conversations, as she looks at them from the one she is in.
 * The preset's half (preset.ts here) watches her sessions and gives her the relay, file, voice, sticker and
 * conversations tools, through `kinContact`.
 */
import type { AssistantStreamFrame } from '@deepseek-ai/dsh-agent'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import { readFileSync } from 'node:fs'
import { extname, relative, resolve, sep } from 'node:path'
import type { Part, Transport, Transports } from '../channels/transport.js'
import type { Host } from '../index.js'
import { Conversations, fileOf, IMAGE_TYPES, partsOf, PLATFORMS } from './conversations.js'
import { Deliveries, type Delivery } from './deliveries.js'
import { look } from './look.js'
import { EMPTY, isGroup, nameOf, OWNER, revise, type People } from './people.js'
import { Reaching, type Place } from './proactive.js'
import { asked, corePolicy, DEFAULT_RULES, quiet, quietEnds, reached, reviseRules, type Activity, type Rules } from './rules.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    kinContact: ContactService
    kinTransports: Transports
  }
}

/** What the KIN preset's per-session half asks of her conversations. */
export interface ContactService {
  /** Output is queued or waiting for a platform receipt, even if the model has finished. */
  readonly sending: boolean
  /** Result-recording failures stay visible while other conversations continue normally. */
  settlementFailures(): { id: string; reason: string }[]
  /** A turn began or ended in one of her sessions. */
  status(sessionId: string, running: boolean): void
  /** Something was written in one of her sessions. */
  event(session: Session, event: SessionEvent): void
  /** She is writing in one of her sessions: on a platform, each finished paragraph goes out. */
  stream(session: Session, frame: AssistantStreamFrame): void
  /** Her words for someone else, through their conversation (`via`: which of them, when there are several); answers how it went, in words for her. */
  tell(sessionId: string, callId: string, to: string, text: string, via?: string): Promise<string>
  /** When and where someone last spoke to her: a turn nobody started (a reminder, word of her helpers) may find them elsewhere. */
  heardFrom(recipient: string): { at: string; where: string } | undefined
  /**
   * The person she is talking with asked her to leave them be: until a time (`15:00`, `2026-01-02 09:00`) or for
   * some minutes; 0 minutes takes it back. Answers what now holds, in words for her.
   */
  quiet(sessionId: string, until?: string, minutes?: number): string
  /** Until when someone asked her to leave them be, in words, while that holds. */
  askedToWait(recipient: string): string | undefined
  /** A file into the WeChat / Feishu conversation she is in; `asked`: the person she is talking with asked for it. */
  send(sessionId: string, callId: string, file: string, asked: boolean): Promise<string>
  /** Her voice saying `text` (`style`: how she says it), in the conversation she is in. */
  voice(sessionId: string, callId: string, text: string, style?: string): Promise<string>
  /** The sticker from her meme library that best fits `meaning`, in the conversation she is in. */
  sticker(sessionId: string, callId: string, meaning: string): Promise<string>
  /** Her other conversations from the one she is in (look.ts): all of them, or the latest said in those with `who`. */
  look(sessionId: string, who?: string): Promise<string>
  /** The people and groups she can tell things to. */
  names(): string[]
  /** Bound destinations and agendas for her main heartbeat review. */
  reviewTargets(): Promise<Record<string, unknown>>
}

/** What the host itself tells the owner when something of hers did not go through (owner-notice.mjs). Never her words. */
const NOTICES = {
  stopped: '刚才这条回复没能完成，原消息还在，不用重复发。',
  unknown: '刚才有一条回复核对并重试了几次，仍未确认送达。你可以看一下有没有收到。',
  partial: '刚才只送达了一部分，已送达的不会重发。',
}
const HOUR = 3_600_000
const MEDIA_TYPES: Record<string, string> = { ...IMAGE_TYPES, '.ogg': 'audio/ogg' }

export function contact(host: Host) {
  let people = host.load<People>('people.json', EMPTY)
  host.introduce(people)
  let rules: Rules = { ...DEFAULT_RULES, ...host.load<Partial<Rules>>('contact.json', {}) }
  const activities = host.load<Record<string, Activity>>('activity.json', {})
  const activity = (id: string) => activities[id] ?? {}
  const note = (id: string, change: Partial<Activity>) => {
    activities[id] = { ...activity(id), ...change }
    host.save('activity.json', activities)
  }
  // Core reads the rules beside its config, so her reviews see what holds her back.
  const writePolicy = () => host.save('proactive-policy.json', corePolicy(rules))
  writePolicy()

  let transports: Transports | undefined
  const transportOf = (id: string): Transport | undefined => (id === 'app' ? conversations.app : transports?.get(id))
  const deliveries = new Deliveries(host, transportOf, (delivery) => settled(delivery))
  const conversations = new Conversations(host, () => people, () => deliveries, transportOf,
    (id, at, key) => note(id, { heard: at, via: key }),
    (_key, startedBy) => (startedBy === 'schedule' ? { kind: 'reminder', ...(quiet(rules) ? { notBefore: quietEnds(rules).toISOString() } : {}) } : { kind: 'reply' }))
  const reaching = new Reaching(host, () => people, () => rules, activity, note, deliveries, conversations)
  host.ctx.effect(() => () => conversations.close(), 'kin conversations')

  // The platforms come and go with the channels module; what arrives on any of them is hers to place.
  host.ctx.inject(['kinTransports'], (ctx) => {
    transports = ctx.kinTransports
    ctx.effect(() => ctx.kinTransports.onInbound((message) => conversations.receive(message)), 'kin arrivals')
    ctx.effect(() => () => { transports = undefined }, 'kin transports gone')
  })

  /** A delivery that is no longer pending: Core settles its attempt, her memory and the conversation hear what she sent. */
  async function settled(delivery: Delivery) {
    const accepted = delivery.sent.filter((sent) => sent.state === 'accepted')
    const ids = accepted.map((sent, index) => sent.platformId ?? `${delivery.id}:${index}`)
    const words = delivery.parts.slice(0, accepted.length).map((part) => ('text' in part ? part.text : '')).join('\n\n')
    const scope = { recipient_person_id: delivery.recipient, conversation_key: delivery.key }
    const told = delivery.state === 'accepted' || (delivery.state === 'needs-review' && accepted.length > 0)
    if (delivery.kind === 'contact') {
      const state = { accepted: 'accepted', 'needs-review': accepted.length ? 'accepted' : 'unconfirmed', unknown: 'unconfirmed', cancelled: 'canceled', pending: '' }[delivery.state]
      await host.mind.call('settle', {
        attempt_id: delivery.id, state, ...scope,
        ...(state === 'accepted' ? { message_id: ids[0], message_ids: ids, partial: accepted.length < delivery.parts.length, canceled_bubbles: delivery.parts.length - accepted.length } : {}),
        ...(state === 'canceled' ? { aborted_before_send: true, reason: delivery.reason ?? 'Delivery refused before sending' } : {}),
        ...(state === 'unconfirmed' ? { reason: 'Receipt requires reconciliation' } : {}),
      })
    }
    if (told && delivery.kind !== 'notice') {
      if (delivery.kind === 'reply' || delivery.kind === 'reminder') {
        const at = delivery.acceptedAt ?? delivery.at
        if (at > (activity(delivery.recipient).answered ?? '')) note(delivery.recipient, { answered: at })
      }
      else {
        const change = reached(activity(delivery.recipient), delivery.id, delivery.acceptedAt ?? delivery.at)
        if (change) note(delivery.recipient, change)
        // Her words are hers in memory, and the conversation they went to knows she said them.
        await host.mind.call('runtime-event', {
          id: delivery.id, kind: 'delivery', at: delivery.at, text: words, state: 'accepted', message_id: ids[0], message_ids: ids,
          channel: delivery.transport, delivery_id: delivery.id, origin: delivery.kind, expected_bubbles: delivery.parts.length, ...scope,
        })
        // A conversation not opened in this home yet (a delivery carried over from an old one) has no session to tell.
        const session = host.conversations()[delivery.key]?.sessionId
        if (delivery.transport !== 'app' && session) {
          await conversations.say(session, 'told:' + delivery.id, words, delivery.kind === 'relay' ? '你转告给对方的话，已送达' : delivery.kind === 'chime' ? '你在群里说的话，已送达' : '你主动发给对方的消息，已送达')
        }
        // After a send her review looks again.
        host.heartbeat.nudge()
      }
    }
    if (delivery.kind === 'reply' && delivery.recipient === OWNER && delivery.transport !== 'app') {
      if (delivery.state === 'needs-review') notice(delivery, accepted.length ? 'partial' : 'unknown')
      if (delivery.state === 'cancelled') notice(delivery, 'stopped')
    }
    deliveries.mark(delivery.id, { remembered: true })
  }

  /**
   * The owner changed or took away someone's account, or a group's chat, on a platform: the conversation held
   * under the old one is theirs no longer, so nothing of hers goes there (a reply, a reminder, something told
   * or sent on her own), and what was waiting to go there does not go.
   */
  function retire(before: People, after: People) {
    const boundOf = (directory: People, id: string) => {
      const entry = [...directory.people, ...directory.groups].find((known) => known.id === id)
      return entry && ('accounts' in entry ? entry.accounts : entry.chats)
    }
    for (const [key, conversation] of Object.entries(host.conversations())) {
      const id = conversation.recipient ?? OWNER
      // An account filled in for the first time changes no one's place (a chat carried over from an old home keeps its address).
      const was = boundOf(before, id)?.[conversation.channel]
      if (!conversation.address || !was || was === boundOf(after, id)?.[conversation.channel]) continue
      host.leave(key)
      // Where they last spoke is no longer where she would look for them (the owner is found on the desktop again).
      if (activity(id).via === key) note(id, { via: undefined })
      for (const delivery of deliveries.list()) {
        if (delivery.key === key && (delivery.state === 'pending' || delivery.state === 'unknown')) deliveries.withdraw(delivery.id)
      }
    }
  }

  /** A notice to the owner where it happened, under an id of its own so it is said once. */
  function notice(about: Pick<Delivery, 'id' | 'key' | 'transport' | 'to'>, kind: keyof typeof NOTICES) {
    const delivery = deliveries.open({
      id: `notice:${kind}:${about.id}`, kind: 'notice', key: about.key, recipient: OWNER, transport: about.transport, to: about.to,
      parts: [{ text: '【系统通知】' + NOTICES[kind] }],
    })
    void conversations.queue(about.key, () => deliveries.send(delivery.id))
  }

  /**
   * One part of hers into the conversation she is in, answered in words for her. On a platform it goes as a reply;
   * a sticker or voice message in her desktop chat is shown there once her turn is over. What a sticker or voice
   * message said joins her memory as her words.
   */
  async function here(sessionId: string, id: string, part: Part, said?: { words: string; media: 'image' | 'voice' }) {
    const scope = host.ctx.get('kin')!.scopeOf(sessionId)
    const conversation = host.conversations()[scope.conversation_key]
    const remember = () => said && host.mind.call('observe', { id, text: said.words, at: new Date().toISOString(), ...scope })
      .catch((error: Error) => host.log(`contact: ${id} not remembered: ${error.message}`))
    if (!conversation?.address) {
      if (!said || !('file' in part)) return '这段对话不在微信或飞书上，告诉对方文件在哪就好。'
      const media = { kind: said.media, file: relative(host.config.dataDir, part.file) }
      void conversations.say(sessionId, id, said.words, said.media === 'voice' ? '你发的语音' : '你发的表情', media)
        .then(remember, (error: Error) => host.log(`contact: ${id} not shown: ${error.message}`))
      return '发了。'
    }
    const delivery = deliveries.open({
      id, kind: 'reply', key: scope.conversation_key, recipient: scope.recipient_person_id, transport: conversation.channel,
      to: conversation.address, parts: [part],
    })
    const sent = await conversations.queue(scope.conversation_key, () => deliveries.send(delivery.id)) as Delivery
    if (sent.state === 'accepted' || sent.state === 'unknown') void remember()
    if (sent.state === 'accepted') return '发过去了。'
    if (sent.state === 'unknown') return '发出去了，但还没确认对方收到；会按原来的编号核对，不用再发。'
    return `没能发出去：${sent.reason ?? '对方的平台拒收了'}`
  }

  /** The names she can tell things to: every person and group, as the owner named them. */
  const everyone = () => [...people.people, ...people.groups]
  const find = (to: string) => everyone().find((entry) => entry.id === to || nameOf(entry) === to.trim())
  /** Where a conversation is, in a word: 桌面, 微信, 飞书. */
  const whereOf = (channel: string) => (channel === 'app' ? '桌面' : PLATFORMS[channel] ?? channel)
  /** Someone's conversations with her, each a place she can reach them: the desktop chat and the platform chats. */
  const placesOf = (id: string): Place[] => Object.entries(host.conversations()).flatMap(([key, conversation]): Place[] =>
    (conversation.recipient ?? OWNER) !== id ? []
      : key === 'app' ? [{ key, channel: 'app', address: { chat: 'app', kind: 'direct' } }]
        : conversation.address ? [{ key, channel: conversation.channel, address: conversation.address }] : [])

  host.ctx.provide('kinContact', {
    reviewTargets: () => reaching.reviewTargets(),
    get sending() { return conversations.pending || deliveries.sending },
    settlementFailures: () => deliveries.list().flatMap((delivery) =>
      delivery.settlementError ? [{ id: delivery.id, reason: delivery.settlementError }] : []),
    status(sessionId, running) {
      conversations.typing(sessionId, running)
      if (!running) conversations.rest(sessionId)
    },
    stream: (session, frame) => conversations.stream(session, frame),
    event(session, event) {
      const scope = host.ctx.get('kin')!.scopeOf(session.id)
      // Her own conversations (steward's `own`) reach no one: only the app and a platform chat are places to find someone.
      const reachable = scope.channel === 'app' || Boolean(host.conversations()[scope.conversation_key]?.address)
      if (event.type === 'user/message' && event.data.source.kind === 'user' && reachable) {
        note(scope.recipient_person_id, { heard: new Date(event.time).toISOString(), via: scope.conversation_key })
      } else if (event.type === 'assistant/message') {
        conversations.forward(session)
      } else if (event.type === 'turn/end' && event.data.reason.kind === 'error' && scope.recipient_person_id === OWNER && scope.channel !== 'app') {
        const conversation = host.conversations()[scope.conversation_key]
        if (conversation?.address) notice({ id: `${session.id}:${event.seq}`, key: scope.conversation_key, transport: conversation.channel, to: conversation.address }, 'stopped')
      }
    },
    async tell(sessionId, callId, to, text, via) {
      const target = find(to)
      if (!target) return `联系人里没有「${to}」。可以转告的有：${everyone().map(nameOf).join('、')}`
      const places = placesOf(target.id)
      const place = via?.trim() ? places.find((place) => [place.channel, whereOf(place.channel)].includes(via.trim())) : reaching.placeOf(target.id)
      if (!place && via?.trim() && places.length) return `${nameOf(target)}在「${via.trim()}」没有和你的对话。能发到：${places.map((place) => whereOf(place.channel)).join('、')}`
      if (!place) return `${nameOf(target)}还没在任何地方和你说过话，现在没法转告。`
      // Told to the conversation she is in, her words would wait for this very turn to end.
      const from = host.ctx.get('kin')!.scopeOf(sessionId)
      if (place.key === from.conversation_key) return `你就在和${nameOf(target)}的这段对话里，直接说就好。`
      // Frozen by the owner, nothing she starts herself goes out: a turn of her own (a routine come due) reaches no one until the thaw.
      if (host.heartbeat.frozen && from.channel === 'own') return '小光把你的后台冻结了，这会儿不往外发；等解冻以后再说。'
      const delivery = deliveries.open({
        id: `relay:${sessionId}:${callId}`, kind: 'relay', key: place.key, recipient: target.id, transport: place.channel, to: place.address,
        parts: partsOf(text), ...(quiet(rules) ? { notBefore: quietEnds(rules).toISOString() } : {}),
      })
      const sent = await conversations.queue(place.key, () => deliveries.send(delivery.id, () => reaching.may(delivery))) as Delivery
      if (sent.state === 'accepted') return `已经告诉${nameOf(target)}了。`
      if (sent.state === 'unknown') return '发出去了，但还没确认对方收到；会按原来的编号核对，不用再发。'
      if (sent.state === 'cancelled' || sent.state === 'needs-review') return `没能发出去：${sent.reason ?? '对方的平台拒收了'}`
      // A wait they asked for is read each time it might go (`may`), so taking it back lets this go at the next minute.
      const wait = asked(activity(target.id))
      if (wait && !(sent.notBefore && new Date(sent.notBefore) > wait)) return `${nameOf(target)}说过先别找，${clock(wait)}以后发给${nameOf(target)}；${nameOf(target)}说不用等了就马上发。`
      if (sent.notBefore) return `现在是安静时段，${clock(new Date(sent.notBefore))}以后发给${nameOf(target)}。`
      return `${nameOf(target)}那边的渠道暂时没连上，连上就发。`
    },
    send(sessionId, callId, file, asked) {
      const { recipient_person_id: recipient } = host.ctx.get('kin')!.scopeOf(sessionId)
      return here(sessionId, `file:${sessionId}:${callId}`, fileOf(file, asked && recipient === OWNER))
    },
    async voice(sessionId, callId, text, style) {
      const id = `voice:${sessionId}:${callId}`
      // The same id speaks once: a retried call sends the audio already made.
      const file = await (transports?.speak(text, id, style) ?? Promise.reject(new Error('渠道还没起来'))).catch((error: Error) => error)
      if (file instanceof Error) return `语音没说出来：${file.message}`
      return here(sessionId, id, { file, kind: 'voice' }, { words: text, media: 'voice' })
    },
    async sticker(sessionId, callId, meaning) {
      const [best] = transports?.stickers(meaning) ?? []
      if (!best) return '表情库里没有合适的，这次就不发了。'
      const answer = await here(sessionId, `sticker:${sessionId}:${callId}`, { file: best.file, kind: 'image' },
        { words: `[表情：${best.description}]`, media: 'image' })
      transports?.usedSticker(best.id)
      return `${answer}（${best.description}）`
    },
    heardFrom(recipient) {
      const { heard, via } = activity(recipient)
      const conversation = via ? host.conversations()[via] : undefined
      if (!heard || !conversation) return
      return { at: heard, where: whereOf(conversation.channel) + (conversation.address?.kind === 'group' ? '群' : '') }
    },
    quiet(sessionId, until, minutes) {
      const { recipient_person_id: id } = host.ctx.get('kin')!.scopeOf(sessionId)
      const who = nameOf(find(id))
      if (minutes === 0) {
        note(id, { quietUntil: undefined })
        return `好，${who}那边不用等了，照常就行。`
      }
      const at = minutes ? new Date(Date.now() + minutes * 60_000) : timeOf(until)
      if (!at || !(at.getTime() > Date.now())) return '没看懂到什么时候：until 写钟点「15:00」或带日期的「2026-01-02 09:00」，或者用 minutes 写多少分钟。'
      note(id, { quietUntil: at.toISOString() })
      return `记下了：到 ${clock(at)} 为止你不主动找${who}，替别人转告的话也等到那之后；${who}来找你，你照常回。`
    },
    askedToWait(recipient) {
      const until = asked(activity(recipient))
      return until && clock(until)
    },
    look: (sessionId, who) => look(host, people, sessionId, who),
    names: () => everyone().map(nameOf),
  } satisfies ContactService)

  // What she already sent is kept every minute, in the middle of a reply too: receipts asked about again, what
  // waited tried again, each behind whatever is waiting to go in its conversation. Nothing new is thought here.
  host.heartbeat.every('deliveries', () => deliveries.tick((delivery) => reaching.may(delivery), (key, work) => conversations.queue(key, work)),
    { whileReplying: true })

  // Someone else's review can think for minutes, in a lane of its own.
  host.heartbeat.every('others', () => reaching.othersReview())

  // Her minute for reaching out, and every hour the words of a deleted source leave what is kept here.
  let swept = 0
  host.heartbeat.every('contact', async () => {
    const answer = await reaching.round()
    if (Date.now() - swept > host.ms(HOUR)) {
      swept = Date.now()
      await conversations.sweep()
    }
    return answer
  })

  host.route('/api/kin/people', {
    GET: async () => ({ ...people, strangers: conversations.strangers }),
    POST: async (request) => {
      const next = revise(await host.body<People>(request))
      retire(people, next)
      people = next
      host.save('people.json', people)
      host.introduce(people)
      return people
    },
  })

  // Her stickers and voice messages, for her desktop chat to show (`kin-said` media): only what lies in those two folders.
  host.route('/api/kin/media', {
    GET: async (request) => {
      const file = resolve(host.config.dataDir, new URL(request.url).searchParams.get('file') ?? '')
      if (![host.file('voice'), host.file('memes')].some((dir) => file.startsWith(dir + sep))) throw new Error('not hers to show')
      return new Response(readFileSync(file), { headers: { 'content-type': MEDIA_TYPES[extname(file).toLowerCase()] ?? 'application/octet-stream' } })
    },
  })

  host.route('/api/kin/contact', {
    GET: async () => {
      const liveness = await host.mind.call('liveness-facts')
      return {
        rules, status: reaching.status, activity: activities, alerts: alerts(liveness.last ?? {}),
        deliveries: deliveries.list().slice(0, 30).map(({ parts: _parts, ...delivery }) => ({ ...delivery, name: nameOf(everyone().find((e) => e.id === delivery.recipient)) })),
        people: Object.fromEntries(everyone().map((entry) => [entry.id, { name: nameOf(entry), group: isGroup(entry.id) }])),
      }
    },
    POST: async (request) => {
      rules = reviseRules(rules, await host.body<Rules>(request))
      host.save('contact.json', rules)
      writePolicy()
      return rules
    },
  })

  // The owner takes back a wait someone asked of her, from her page.
  host.route('/api/kin/contact/wait', {
    POST: async (request) => {
      const { id } = await host.body<{ id: string }>(request)
      if (id && activities[id]) note(id, { quietUntil: undefined })
      return { ok: true }
    },
  })
}

/** A time as she is told it: the clock, with the date when it is not today. */
function clock(at: Date) {
  const today = at.toDateString() === new Date().toDateString()
  return at.toLocaleString('zh-CN', { ...(today ? {} : { month: 'numeric', day: 'numeric' }), hour: '2-digit', minute: '2-digit', hour12: false })
}

/** `15:00` (today, or tomorrow once that has passed) or `2026-01-02 09:00`, local time. */
function timeOf(text = ''): Date | undefined {
  const [, date, hour, minute] = /^(?:(\d{4}-\d{1,2}-\d{1,2})[ T])?(\d{1,2})[:：](\d{2})$/.exec(text.trim()) ?? []
  if (!hour) return
  const now = new Date()
  const [year, month, day] = date ? date.split('-').map(Number) : [now.getFullYear(), now.getMonth() + 1, now.getDate()]
  const at = new Date(year!, month! - 1, day!, Number(hour), Number(minute))
  if (!date && at <= now) at.setDate(at.getDate() + 1)
  return at
}

/** What has gone quiet for too long in her life (mind-host.mjs, health.mjs): no new wish in 48 hours, no exploring or reaching out in 72. */
function alerts(last: { wish_created_at?: string | null; exploration_at?: string | null; contact_sent_at?: string | null }) {
  const since = (at: string | null | undefined, hours: number) => !at || Date.now() - Date.parse(at) > hours * HOUR
  return [
    since(last.wish_created_at, 48) ? '两天没有新的心愿了' : '',
    since(last.exploration_at, 72) ? '三天没有出门探索了' : '',
    since(last.contact_sent_at, 72) ? '三天没有主动找过人了' : '',
  ].filter(Boolean)
}
