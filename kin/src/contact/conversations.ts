import { answerText } from '../model-output.js'
/**
 * Her conversations on WeChat and Feishu, each one DSH session (a key per chat: `wechat:<chat>`, `feishu:<chat>`):
 * - what arrives is placed (whose chat, who is speaking), remembered with who said it, and, after the
 *   owner's twenty seconds of quiet (owner-input-batch.mjs), goes into the session as one message, with the
 *   pictures in it to look at and where any other file was saved; said while she is in the middle of a turn
 *   there, it reaches her at her next step rather than after the whole turn (as the old host steered it);
 * - each chat's arrivals are taken in their own order, so a voice message being transcribed holds up no other chat;
 * - in a group she answers when @-mentioned; the rest is heard and remembered, and comes with the next @;
 * - 停止任务 / 取消当前任务 / /停 stop what she is doing in that conversation;
 * - what she says there goes back out through the platform as a delivery (deliveries.ts), "typing" while she
 *   writes, and paragraph by paragraph as she writes them: a finished paragraph does not wait for the message;
 * - what she sent from outside a conversation is told to it, as her own words (`say`), and that is how
 *   her desktop chat receives a message she sends on her own.
 */
import type { AssistantStreamFrame } from '@deepseek-ai/dsh-agent'
import type { ImageMediaType } from '@deepseek-ai/dsh-attachment'
import { createUserMessage, type ContentBlock } from '@deepseek-ai/dsh-llm'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import { readFile } from 'node:fs/promises'
import { extname } from 'node:path'
import type { Address, Inbound, Part, Transport } from '../channels/transport.js'
import type { Conversation, Host } from '../index.js'
import { PART_WORDS, type Deliveries, type Delivery } from './deliveries.js'
import { isGroup, nameOf, OWNER, placeOf, type People } from './people.js'
import { assertReceiptSurfaceReady, ReceiptSurfaceNotReadyError } from './session-head.js'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    /**
     * Her own words, sent from outside this conversation (on her own, a relay), told to it once they went out;
     * or a sticker or voice message of hers in her desktop chat, with its file (in her data directory) to show.
     */
    'kin-said': {
      readonly kind: 'kin-said'; readonly id: string; readonly text: string
      readonly media?: { readonly kind: 'image' | 'voice'; readonly file: string }
    }
  }
}

/** The owner's quiet between bubbles that makes them one message; an @ in a group goes at once. */
const BURST_MS = 20_000
const MENTION_MS = 1_000
/** An arrival her mind could not take in is tried again this often, for this long. */
const RETAKE_MS = 60_000
const KEEP_ARRIVAL_MS = 24 * 3_600_000
const STOP = /^(停止任务|取消当前任务|\/停)$/
/** How much of a group's talk waits for her next @ or chime. */
const BACKLOG = 40

/** One heard message, as she will read it and as Core keeps it, and the pictures that came with it. */
interface Heard {
  id: string; text: string; at: string; speaker?: string; source: string; images?: string[]; considered?: boolean
  /** When it reached this host, for her reply timeline. */
  arrived?: number
}

/** The step she is writing in a platform conversation, as its paragraphs go out. */
interface Writing {
  turn: number
  step: number
  /** The attempt followed. One made again after words already went out is not: its message, once whole, says what is left. */
  attempt: string
  /** The delivery its paragraphs join. */
  id: string
  /** Her words so far, by block. */
  blocks: Map<number, string>
  /** Paragraphs handed over so far. */
  out: number
}

interface Inbox {
  /** Arrivals not taken in yet, as the platform gave them: a platform does not send one twice. */
  arriving: Inbound[]
  /** Messages waiting for the quiet that ends a burst, by conversation. */
  bursts: Record<string, Heard[]>
  /** A group's talk since she last spoke there, by conversation. */
  backlogs: Record<string, Heard[]>
  /** Platform message ids already taken in, newest last. */
  seen: string[]
  /** Who wrote to her that the owner has not placed yet, for her page. */
  strangers: (Omit<Inbound, 'text' | 'files' | 'id'>)[]
}

export class Conversations {
  private readonly inbox: Inbox
  private readonly timers = new Map<string, NodeJS.Timeout>()
  private readonly forwarded: Record<string, number>
  /** Arrivals still being taken in, by chat. */
  private readonly arrivals = new Map<string, Promise<void>>()
  private readonly sending = new Map<string, Promise<unknown>>()
  /** What she is writing right now, by session. */
  private readonly writing = new Map<string, Writing>()
  /** Deliveries whose stray words are being told to her. */
  private readonly telling = new Set<string>()
  private retaking?: NodeJS.Timeout
  private closed = false
  /** A burst her session took whose keeping there has not held yet. */
  private readonly handed = new Map<string, { burst: Heard[]; backlog: Heard[] }>()

  constructor(
    private readonly host: Host,
    private readonly directory: () => People,
    private readonly deliveries: () => Deliveries,
    private readonly transportOf: (id: string) => Transport | undefined,
    /** Someone was heard from, in this conversation. */
    private readonly heard: (recipient: string, at: string, key: string) => void,
    /** A delivery of a reply, reminder or relay, decided from the conversation that made it. */
    private readonly replyKind: (key: string, startedBy: string) => { kind: 'reply' | 'reminder'; notBefore?: string },
  ) {
    this.inbox = { arriving: [], ...host.load<Partial<Inbox>>('inbox.json', {}) } as Inbox
    for (const list of ['bursts', 'backlogs'] as const) this.inbox[list] ??= {}
    for (const list of ['seen', 'strangers'] as const) this.inbox[list] ??= []
    this.forwarded = host.load<Record<string, number>>('forwarded.json', {})
    // Once her mind is up: what was waiting for its quiet when she last stopped goes, what had arrived and was
    // not taken in is, and words that went out as she wrote and are in no message of hers are told to her.
    void host.ready.then(() => {
      for (const key of Object.keys(this.inbox.bursts)) this.schedule(key, MENTION_MS)
      for (const message of [...this.inbox.arriving]) this.receive(message)
      return this.recover()
    }).catch((error: Error) => host.log(`contact: catching up after a restart: ${error.message}`))
  }

  get pending() {
    return this.sending.size > 0
  }

  get strangers() {
    return this.inbox.strangers
  }

  backlog(key: string): Heard[] {
    return this.inbox.backlogs[key] ?? []
  }

  /** The group talk she has now thought about chiming into. */
  considered(key: string, ids: string[]) {
    for (const heard of this.backlog(key)) if (ids.includes(heard.id)) heard.considered = true
    this.save()
  }

  close() {
    this.closed = true
    for (const timer of this.timers.values()) clearTimeout(timer)
    this.timers.clear()
    clearTimeout(this.retaking)
  }

  /**
   * One arrival from a platform, taken in its chat's order. It is kept until it is: her mind away for a moment
   * (a restart, a busy store) loses nothing, it is tried again each minute, for a day.
   */
  receive(message: Inbound) {
    const key = `${message.transport}:${message.chat}`
    const same = (kept: Inbound) => kept.transport === message.transport && kept.id === message.id
    if (!this.inbox.arriving.some(same)) {
      this.inbox.arriving.push(message)
      this.save()
    }
    const arrived = Date.now()
    const taken: Promise<void> = (this.arrivals.get(key) ?? Promise.resolve()).then(() => this.take(message, arrived))
      .then(() => true, (error: Error) => {
        this.host.log(`contact: an arrival on ${message.transport} was not taken in: ${error.message}`)
        return Date.now() - Date.parse(message.at) > KEEP_ARRIVAL_MS
      })
      .then((settled) => {
        if (settled) {
          this.inbox.arriving = this.inbox.arriving.filter((kept) => !same(kept))
          this.save()
        } else if (!this.closed) {
          this.retaking ??= setTimeout(() => {
            this.retaking = undefined
            for (const kept of [...this.inbox.arriving]) this.receive(kept)
          }, this.host.ms(RETAKE_MS)).unref()
        }
      })
      .finally(() => { if (this.arrivals.get(key) === taken) this.arrivals.delete(key) })
    this.arrivals.set(key, taken)
  }

  private async take(message: Inbound, arrived: number) {
    const mark = `${message.transport}:${message.id}`
    if (this.inbox.seen.includes(mark)) return
    const place = placeOf(this.directory(), message)
    if (!place) {
      const { text: _text, files: _files, id: _id, ...who } = message
      this.inbox.strangers = [who, ...this.inbox.strangers.filter((s) => s.transport !== who.transport || s.sender !== who.sender || s.chat !== who.chat)].slice(0, 8)
      return this.save()
    }
    const { recipient, speaker } = place
    const key = `${message.transport}:${message.chat}`
    const group = isGroup(recipient.id)
    for (const file of message.files ?? []) {
      if (file.kind === 'voice' && !file.transcript) {
        file.transcript = await this.host.ctx.get('kinTransports')?.hear?.(file.path)
          .catch((error: Error) => { this.host.log(`voice not transcribed: ${error.message}`); return undefined })
      }
    }
    const words = wordsOf(message)
    if (STOP.test(words) && (!group || speaker?.id === OWNER)) {
      this.inbox.seen = [...this.inbox.seen, mark].slice(-500)
      return this.stop(key)
    }
    // She knows who is speaking: anyone but the owner in the owner's own chat is named.
    const text = group || recipient.id !== OWNER ? `${speaker ? nameOf(speaker) : message.senderName || '有人'}：${words}` : words
    const conversation = await this.host.conversation(key, {
      channel: message.transport, recipient: recipient.id, address: { chat: message.chat, kind: message.kind },
      title: group ? `${nameOf(recipient)}（群）` : `和${nameOf(recipient)}的${PLATFORMS[message.transport] ?? message.transport}`,
    })
    const remembered = await this.host.mind.call<{ source_id: string }>('ingest', {
      id: mark, text, at: message.at, channel: message.transport, session: conversation.sessionId,
      recipient_person_id: recipient.id, conversation_key: key, conversation_kind: group ? 'group' : 'p2p',
      ...(speaker ? { speaker_person_id: speaker.id } : {}),
    })
    this.inbox.seen = [...this.inbox.seen, mark].slice(-500)
    this.heard(recipient.id, message.at, key)
    const images = (message.files ?? []).flatMap((file) => (file.kind === 'image' ? [file.preview ?? file.path] : []))
    const heard: Heard = { id: mark, text, at: message.at, speaker: speaker?.id, source: remembered.source_id, arrived, ...(images.length ? { images } : {}) }
    if (group && !message.mentioned) {
      this.inbox.backlogs[key] = [...this.backlog(key), heard].slice(-BACKLOG)
      return this.save()
    }
    ;(this.inbox.bursts[key] ??= []).push(heard)
    this.save()
    this.schedule(key, group ? MENTION_MS : BURST_MS)
  }

  private schedule(key: string, ms: number) {
    if (this.closed) return
    clearTimeout(this.timers.get(key))
    const timer = setTimeout(() => void (async () => {
      // An arrival may still be waiting on Core. Its completed intake resets this timer.
      let arriving
      do { arriving = this.arrivals.get(key); await arriving } while (arriving !== this.arrivals.get(key))
      if (this.timers.get(key) !== timer) return
      this.timers.delete(key)
      await this.flush(key)
    })().catch((error: Error) => {
      this.host.log(`contact: ${key} did not reach her: ${error.message}`)
      // Still waiting in its burst: tried again, unless something newer already set its own time.
      if (!this.timers.has(key)) this.schedule(key, RETAKE_MS)
    }), this.host.ms(ms)).unref()
    this.timers.set(key, timer)
  }

  /** A burst into its session as one message, with the group's talk since she last spoke there. */
  private async flush(key: string): Promise<void> {
    const conversation = this.host.conversations()[key]
    const handed = this.handed.get(key)
    const { burst, backlog } = handed ?? { burst: [...(this.inbox.bursts[key] ?? [])], backlog: [...this.backlog(key)] }
    if (!burst.length || !conversation) return
    const agent = await this.agentOf(conversation.sessionId)
    if (!handed) {
      const text = [...(backlog.length ? ['（群里之前的话）', ...backlog.map((heard) => heard.text), ''] : []), ...burst.map((heard) => heard.text)].join('\n')
      const images = await this.picturesOf(burst.flatMap((heard) => heard.images ?? []))
      this.host.timeline.begin(conversation.sessionId, key, burst[0]!.arrived)
      // Idle, this starts her turn; in the middle of one, she reads it at her next step.
      agent.steer(createUserMessage({
        content: [{ type: 'text', text }, ...images],
        source: {
          kind: 'kin-channel', channel: conversation.channel, speaker: burst.at(-1)!.speaker ?? '',
          inputs: burst.map((heard) => heard.id), source: burst.at(-1)!.source,
        },
      }))
      // Her session has it from here: keeping it there may be tried again, giving it to her may not.
      this.handed.set(key, { burst, backlog })
    }
    await this.host.ctx.get('sessions')?.flush(agent.session)
    this.handed.delete(key)
    // What arrived meanwhile waits for its own quiet.
    this.inbox.bursts[key] = (this.inbox.bursts[key] ?? []).filter((heard) => !burst.includes(heard))
    this.inbox.backlogs[key] = this.backlog(key).filter((heard) => !backlog.includes(heard))
    this.save()
    // A second try was the quiet of whatever arrived since: that goes now.
    if (handed) return this.flush(key)
  }

  /** Pictures as DSH keeps them, for her to look at; their path lines stay in the words either way. */
  private async picturesOf(paths: string[]): Promise<ContentBlock[]> {
    const store = this.host.ctx.get('attachments')
    const pictures = paths.flatMap((path) => (IMAGE_TYPES[extname(path).toLowerCase()] ? [path] : []))
    if (!store || !pictures.length) return []
    const images = await Promise.all(pictures.map(async (path) => ({ data: await readFile(path), mediaType: IMAGE_TYPES[extname(path).toLowerCase()]! })))
    const saved = await store.saveImages(images).catch((error: Error) => (this.host.log(`contact: pictures not shown to her: ${error.message}`), []))
    return saved.map((attachment) => ({ type: 'image' as const, attachment }))
  }

  /** The literal stop: what waits is dropped and what she is doing there ends. */
  private async stop(key: string) {
    clearTimeout(this.timers.get(key))
    this.timers.delete(key)
    delete this.inbox.bursts[key]
    this.save()
    const conversation = this.host.conversations()[key]
    if (!conversation) return
    const agent = await this.agentOf(conversation.sessionId)
    agent.cancel({ kind: 'user' })
    const steward = this.host.ctx.get('kinSteward')
    for (const item of steward?.list() ?? []) {
      if (item.session === conversation.sessionId && item.state === 'running') steward!.stop(item.id)
    }
  }

  /** What is kept here of a message whose source was deleted from her mind goes too (duo-host.mjs `eraseDeleted`). */
  async sweep() {
    const held = [...Object.values(this.inbox.bursts), ...Object.values(this.inbox.backlogs)].flat()
    const sources = held.map((heard) => heard.source)
    if (!sources.length) return
    const { deleted } = await this.host.mind.call<{ deleted: string[] }>('deleted-sources', { source_ids: sources })
    if (!deleted.length) return
    for (const list of [this.inbox.bursts, this.inbox.backlogs]) {
      for (const key of Object.keys(list)) list[key] = list[key]!.filter((heard) => !deleted.includes(heard.source))
    }
    this.save()
  }

  /** "Typing…" in a conversation's chat while she writes, where the platform has it. */
  typing(sessionId: string, on: boolean) {
    const found = this.channelConversation(sessionId)
    if (found) void this.transportOf(found[1].channel)?.typing?.(found[1].address!, on).catch(() => {})
  }

  /**
   * She is writing in a channel conversation: each paragraph goes out once it is finished (a blank line ends
   * one; the last waits for the message itself, `forward`). All of a step is one delivery, under the session
   * position the step began at.
   */
  stream(session: Session, frame: AssistantStreamFrame) {
    if (frame.type === 'start') {
      const last = this.writing.get(session.id)
      // A step written again after words of it went out is not followed: its message, once whole, says what is left.
      if (last?.out && last.turn === frame.turn && last.step === frame.step && !this.deliveries().get(last.id)?.written?.known) return
      this.rest(session.id)
      const id = `reply:${session.id}:live:${Number(session.seq)}`
      // A session position names one step; a delivery already under it is not this one's to add to.
      if (!this.channelConversation(session.id) || this.deliveries().get(id)) return
      this.writing.set(session.id, { turn: frame.turn, step: frame.step, attempt: frame.attemptId, id, blocks: new Map(), out: 0 })
      return
    }
    const writing = this.writing.get(session.id)
    const found = this.channelConversation(session.id)
    if (frame.type !== 'chunk' || frame.chunk.type !== 'text-delta' || writing?.attempt !== frame.attemptId || !found) return
    writing.blocks.set(frame.chunk.index, (writing.blocks.get(frame.chunk.index) ?? '') + frame.chunk.text)
    const text = [...writing.blocks].sort(([a], [b]) => a - b).map(([, block]) => answerText(block)).join('\n\n')
    // The last piece may still be growing.
    const finished = partsOf(text.split(/\n\s*\n/).slice(0, -1).join('\n\n'))
    if (finished.length <= writing.out) return
    const parts = finished.slice(writing.out)
    const known = writing.out > 0
    writing.out = finished.length
    const [key, conversation] = found
    if (known) return this.go(session, key, this.deliveries().extend(writing.id, parts))
    const { kind, notBefore } = this.replyKind(key, startedBy(session, Number(session.seq) - 1))
    this.go(session, key, this.deliveries().open({
      id: writing.id, kind, key, recipient: conversation.recipient ?? OWNER, transport: conversation.channel, to: conversation.address!,
      parts, written: { session: session.id, turn: writing.turn, step: writing.step }, ...(notBefore ? { notBefore } : {}),
    }))
  }

  /** Her turn there is over, or a new step begins: words that went out from an attempt that never became a message of hers are told to her as said. */
  rest(sessionId: string) {
    const writing = this.writing.get(sessionId)
    this.writing.delete(sessionId)
    const live = writing?.out ? this.deliveries().get(writing.id) : undefined
    const found = this.channelConversation(sessionId)
    if (live && found && !live.written?.known) this.stray(sessionId, found[0], live)
  }

  /** After a restart: what went out as she wrote and is known to no message of hers is settled as it would have been. */
  private async recover() {
    for (const live of this.deliveries().list()) {
      const found = live.written && !live.written.known ? this.channelConversation(live.written.session) : undefined
      // One she is writing right now is not left over from before.
      if (!found || [...this.writing.values()].some((writing) => writing.id === live.id)) continue
      const agent = await this.agentOf(live.written!.session)
      // A message that did form takes it up; otherwise its words are told to her.
      this.forward(agent.session)
      if (!live.written!.known) this.stray(agent.session.id, found[0], live)
    }
  }

  /**
   * What she said in a channel conversation goes out, each message once (its delivery id is the session
   * position). Called as messages are committed and again when she is done, which also catches up after a restart.
   * Paragraphs that went out as she wrote the message are not sent again: the rest joins their delivery.
   */
  forward(session: Session) {
    const found = this.channelConversation(session.id)
    if (!found) return
    const [key, conversation] = found
    const end = Number(session.seq)
    for (let seq = this.forwarded[session.id] ?? 0; seq < end; seq++) {
      const event = session.eventAt(SessionSeq(seq))
      const text = event?.type === 'assistant/message' ? textOf(event.data.message.content) : ''
      if (event?.type !== 'assistant/message' || !text) continue
      const { turn, step } = event.data
      const parts = partsOf(text)
      const live = this.deliveries().written(session.id, turn, step)
      let same = 0
      while (live && same < live.parts.length && same < parts.length && wordsIn(live.parts[same]!) === wordsIn(parts[same]!)) same++
      // What went out as she wrote is how the message begins: the rest follows in the same delivery.
      if (live && same === live.parts.length && !live.withdrawn) {
        this.deliveries().mark(live.id, { written: { ...live.written!, known: true } })
        if (parts.length > same) this.go(session, key, this.deliveries().extend(live.id, parts.slice(same)))
        continue
      }
      // Written again differently after a broken attempt: nothing more of the old words goes, and the message
      // leaves out only the leading paragraphs a platform already has.
      if (live) {
        same = Math.min(same, live.sent.length)
        if (!live.written?.known) this.stray(session.id, key, live, same)
      }
      if (parts.length === same) continue
      const { kind, notBefore } = this.replyKind(key, startedBy(session, seq))
      this.go(session, key, this.deliveries().open({
        id: `reply:${session.id}:${seq}`, kind, key, recipient: conversation.recipient ?? OWNER,
        transport: conversation.channel, to: conversation.address!, parts: parts.slice(same), ...(notBefore ? { notBefore } : {}),
      }))
    }
    this.forwarded[session.id] = end
    this.host.save('forwarded.json', this.forwarded)
  }

  /** One conversation's messages go in the order she said them. */
  private go(session: Session, key: string, delivery: Delivery) {
    void this.queue(key, async () => {
      const sent = await this.deliveries().send(delivery.id)
      if (sent.sent[0]?.state === 'accepted') this.host.timeline.landed(session.id)
    })
  }

  /**
   * Words of `live` from part `from` on are in no message of hers. What has not gone is taken back; what a
   * platform took, she said: the conversation and her memory are told so, once what was on its way has landed.
   */
  private stray(sessionId: string, key: string, live: Delivery, from = 0) {
    if (this.telling.has(live.id)) return
    this.telling.add(live.id)
    this.deliveries().withdraw(live.id)
    void this.queue(key, async () => {}).then(async () => {
      const went = live.parts.slice(from, live.sent.length)
      // Her memory keeps as said only what a platform took; the conversation is told of one still unsure, as unsure.
      const sure = went.filter((_part, index) => live.sent[from + index]?.state === 'accepted')
      if (went.length) {
        const unsure = sure.length < went.length ? '；最后一句还没确认对方收到' : ''
        await this.say(sessionId, 'told:' + live.id, went.map(wordsIn).join('\n\n'), '这几句在你写到一半时已经发给对方了，那一次没有写完' + unsure)
      }
      if (sure.length) {
        await this.host.mind.call('observe', { id: live.id, text: sure.map(wordsIn).join('\n\n'), at: live.at, ...this.host.ctx.get('kin')!.scopeOf(sessionId) })
      }
      this.deliveries().mark(live.id, { written: { ...live.written!, known: true } })
    }).catch((error: Error) => this.host.log(`contact: ${live.id} not told to her: ${error.message}`))
      .finally(() => this.telling.delete(live.id))
  }

  queue(key: string, work: () => Promise<unknown>) {
    const next = (this.sending.get(key) ?? Promise.resolve()).then(work).catch((error: Error) => this.host.log(`contact: sending in ${key}: ${error.message}`))
    const done = next.finally(() => { if (this.sending.get(key) === done) this.sending.delete(key) })
    this.sending.set(key, done)
    return done
  }

  /**
   * Her words, sent from outside this conversation, told to it as hers once they went out (outward-effects.mjs),
   * between her turns. The same id is never told twice. Returns the message's place in the session.
   */
  async say(sessionId: string, id: string, text: string, note: string, media?: { kind: 'image' | 'voice'; file: string }): Promise<string> {
    const agent = await this.agentOf(sessionId)
    const session = agent.session
    for (let seq = Number(session.seq) - 1; seq >= 0; seq--) {
      const event = session.eventAt(SessionSeq(seq))
      if (event?.type === 'user/message' && event.data.source.kind === 'kin-said' && event.data.source.id === id) {
        await this.host.ctx.get('sessions')?.flush(session)
        return String(seq)
      }
    }
    const message = createUserMessage({
      content: [{ type: 'text', text: `（${note}）\n${text}` }], source: { kind: 'kin-said', id, text, ...(media ? { media } : {}) },
    })
    for (let tries = 0; ; tries++) {
      await agent.whenIdle()
      let seq: string
      try {
        const event = await agent.runMaintenance(async () => {
          assertReceiptSurfaceReady(session)
          return session.append('user/message', message, { surfaceOp: 'append' })
        })
        seq = String(event.seq)
      } catch (error) {
        // A turn began between the idle and the append: tell it after that turn.
        if (error instanceof ReceiptSurfaceNotReadyError || tries >= 3) throw error
        continue
      }
      // Once appended, a failed disk flush must retry persistence, never append the same notice again.
      await this.host.ctx.get('sessions')?.flush(session)
      return seq
    }
  }

  /** Her desktop chat as a platform: a message she sends there is told to that conversation. */
  readonly app: Transport = {
    id: 'app',
    start: async () => {},
    close: async () => {},
    status: () => ({ state: 'connected' }),
    send: async (_to: Address, part: Part, id: string) => {
      const { sessionId } = await this.host.conversation('app', { channel: 'app', title: '和 KIN 的聊天' })
      const text = 'text' in part ? part.text : part.file
      try {
        return { state: 'accepted', platformId: await this.say(sessionId, id, text, '你主动发给主人的消息') }
      } catch (error) {
        if (error instanceof ReceiptSurfaceNotReadyError) return { state: 'failed', reason: error.message }
        throw error
      }
    },
  }

  private channelConversation(sessionId: string): [string, Conversation] | undefined {
    const found = Object.entries(this.host.conversations()).find(([, conversation]) => conversation.sessionId === sessionId)
    return found && found[1].channel !== 'app' && found[1].address ? found : undefined
  }

  private async agentOf(sessionId: string) {
    const resolved = await this.host.ctx.get('sessionController')!.resolveAgent(sessionId as never)
    if ('error' in resolved) throw new Error('her session is not open: ' + (resolved.error.message || JSON.stringify(resolved.error)))
    return resolved.agent
  }

  private save() {
    this.host.save('inbox.json', this.inbox)
  }
}

export const PLATFORMS: Record<string, string> = { wechat: '微信', feishu: '飞书' }

/** An arrival in words: its text, a voice message's transcript, and where anything else that came with it was saved. */
function wordsOf(message: Inbound): string {
  const files = (message.files ?? []).map((file) => file.transcript ?? `[${PART_WORDS[file.kind]}${file.name ? '：' + file.name : ''}] ${file.path}`)
  return [message.text?.trim(), ...files].filter(Boolean).join('\n')
}
export const IMAGE_TYPES: Record<string, ImageMediaType> = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif' }

/** A file she sends, as its platform takes it. `named`: the owner asked for this very file (explicit-wechat.mjs). */
export function fileOf(file: string, named: boolean): Part {
  const kind = /\.(png|jpe?g|webp|gif|heic)$/i.test(file) ? 'image' : /\.(mp4|mov|m4v|webm)$/i.test(file) ? 'video' : 'file'
  return { file, kind, named }
}

/** A message as she sends it: a blank line between paragraphs is a new bubble, as people send them. */
export function partsOf(text: string): Part[] {
  return text.split(/\n\s*\n/).map((piece) => piece.trim()).filter(Boolean).map((piece) => ({ text: piece }))
}

/** What began the turn a message of hers belongs to: a person (`kin-channel`), a reminder (`schedule`), … */
function startedBy(session: Session, seq: number): string {
  for (let at = seq; at >= 0; at--) {
    const event = session.eventAt(SessionSeq(at)) as SessionEvent | undefined
    if (event?.type === 'user/message' && event.data.source.kind !== 'kin-context') return event.data.source.kind
  }
  return 'user'
}

const wordsIn = (part: Part) => ('text' in part ? part.text : '')

function textOf(content: readonly { type: string; text?: string }[]): string {
  return content.flatMap((block) => (block.type === 'text' && block.text?.trim() ? [answerText(block.text).trim()] : [])).join('\n\n')
}
