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
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import { readFileSync } from 'node:fs'
import { extname, relative, resolve, sep } from 'node:path'
import type { Part, Transport, Transports } from '../channels/transport.js'
import type { Host } from '../index.js'
import { Conversations, fileOf, IMAGE_TYPES, partsOf } from './conversations.js'
import { Deliveries, type Delivery } from './deliveries.js'
import { look } from './look.js'
import { EMPTY, isGroup, nameOf, OWNER, revise, type People } from './people.js'
import { Reaching } from './proactive.js'
import { corePolicy, DEFAULT_RULES, quiet, quietEnds, reviseRules, type Activity, type Rules } from './rules.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    kinContact: ContactService
    kinTransports: Transports
  }
}

/** What the KIN preset's per-session half asks of her conversations. */
export interface ContactService {
  /** A turn began or ended in one of her sessions. */
  status(sessionId: string, running: boolean): void
  /** Something was written in one of her sessions. */
  event(session: Session, event: SessionEvent): void
  /** Her words for someone else, through their conversation; answers how it went, in words for her. */
  tell(sessionId: string, callId: string, to: string, text: string): Promise<string>
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
}

/** What the host itself tells the owner when something of hers did not go through (owner-notice.mjs). Never her words. */
const NOTICES = {
  stopped: '刚才这条回复没能完成，原消息还在，不用重复发。',
  unknown: '刚才那条的结果还没确认，先别重复发。',
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
      if (delivery.kind === 'reply' || delivery.kind === 'reminder') note(delivery.recipient, { answered: new Date().toISOString() })
      else {
        const reached = [...(activity(delivery.recipient).reached ?? []), new Date().toISOString()].slice(-24)
        note(delivery.recipient, { reached })
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
      if (delivery.state === 'unknown') notice(delivery, 'unknown')
      if (delivery.state === 'needs-review' && accepted.length) notice(delivery, 'partial')
    }
    deliveries.mark(delivery.id, { remembered: true })
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

  host.ctx.provide('kinContact', {
    status: (sessionId, running) => conversations.typing(sessionId, running),
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
    async tell(sessionId, callId, to, text) {
      const target = find(to)
      if (!target) return `联系人里没有「${to}」。可以转告的有：${everyone().map(nameOf).join('、')}`
      const place = reaching.placeOf(target.id)
      if (!place) return `${nameOf(target)}还没在任何地方和你说过话，现在没法转告。`
      const delivery = deliveries.open({
        id: `relay:${sessionId}:${callId}`, kind: 'relay', key: place.key, recipient: target.id, transport: place.channel, to: place.address,
        parts: partsOf(text), ...(quiet(rules) ? { notBefore: quietEnds(rules).toISOString() } : {}),
      })
      const sent = await conversations.queue(place.key, () => deliveries.send(delivery.id, () => reaching.may(delivery))) as Delivery
      if (sent.state === 'accepted') return `已经告诉${nameOf(target)}了。`
      if (sent.state === 'unknown') return '发出去了，但还没确认对方收到；会按原来的编号核对，不用再发。'
      if (sent.state === 'cancelled' || sent.state === 'needs-review') return `没能发出去：${sent.reason ?? '对方的平台拒收了'}`
      if (sent.notBefore) return `现在是安静时段，${new Date(sent.notBefore).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })}以后发给${nameOf(target)}。`
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
    look: (sessionId, who) => look(host, people, sessionId, who),
    names: () => everyone().map(nameOf),
  } satisfies ContactService)

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
      people = revise(await host.body<People>(request))
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
