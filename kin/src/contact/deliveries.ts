/**
 * Everything she sends, kept in her data directory before it goes out: a reply in a conversation, a
 * message she decided to send on her own, a relay, a reminder, a system notice. Each delivery has one id
 * for good and each of its parts the id `<delivery>:<n>`, so a restart or a retry never sends anything
 * under a new name (PARITY "搬的时候最容易出事" 1):
 * - an unknown part is checked after a minute, then retried under its own id at most three times;
 *   confirmed parts never repeat, and only exhausted retries ask the owner to look;
 * - a delivery that began is finished: the parts after it go once the earlier ones are settled;
 * - one that never got a part out is cancelled after a few refused tries; one cut short half way needs review.
 */
import type { Address, Part, Sent, Transport } from '../channels/transport.js'
import type { Host } from '../index.js'

export type State = 'pending' | 'accepted' | 'unknown' | 'cancelled' | 'needs-review'
export type Kind = 'reply' | 'contact' | 'chime' | 'relay' | 'reminder' | 'notice'

export interface Delivery {
  id: string
  kind: Kind
  /** The conversation it goes to, and whose it is. */
  key: string
  recipient: string
  /** A platform, or `app` for her desktop chat. */
  transport: string
  to: Address
  parts: Part[]
  /** What became of each part tried so far, in order. */
  sent: Sent[]
  state: State
  at: string
  /** Waits until then: quiet hours. */
  notBefore?: string
  /** An unknown outcome is asked about again then. */
  checkAt?: string
  checks?: number
  /** Retries of the current uncertain part, persisted before each attempt. */
  retries?: number
  /** Refusals of the next part so far. */
  tries?: number
  reason?: string
  /** Settled with Core (a contact attempt) or in her memory (what she sent). */
  remembered?: boolean
  /** Its first words, for her page. */
  excerpt?: string
}

const MINUTE = 60_000
/** A file part in words: what it is. */
export const PART_WORDS = { image: '图片', voice: '语音', video: '视频', file: '文件' }
const TRIES = 3
const KEEP_MS = 7 * 24 * 60 * MINUTE
const FILE = 'deliveries.json'

export const partId = (delivery: Delivery, index: number) => `${delivery.id}:${index}`

export class Deliveries {
  private readonly all: Record<string, Delivery>
  private readonly busy = new Map<string, Promise<Delivery>>()

  constructor(
    private readonly host: Host,
    private readonly transportOf: (id: string) => Transport | undefined,
    /** Told once a delivery reaches a state that is not `pending`: accepted, unknown, cancelled, needs review. */
    private readonly settled: (delivery: Delivery) => Promise<void>,
  ) {
    this.all = host.load<Record<string, Delivery>>(FILE, {})
  }

  get sending() {
    return this.busy.size > 0
  }

  get(id: string): Delivery | undefined {
    return this.all[id]
  }

  list(): Delivery[] {
    return Object.values(this.all).sort((a, b) => b.at.localeCompare(a.at))
  }

  /** A delivery under its own id: made once, and the same one when asked again. Its words change only while none has gone out. */
  open(given: Pick<Delivery, 'id' | 'kind' | 'key' | 'recipient' | 'transport' | 'to' | 'parts'> & { notBefore?: string }): Delivery {
    const known = this.all[given.id]
    if (known && (known.sent.length || known.state !== 'pending')) return known
    const excerpt = given.parts.map((part) => ('text' in part ? part.text : `[${PART_WORDS[part.kind]}]`)).join(' ').slice(0, 80)
    this.all[given.id] = { ...given, sent: [], state: 'pending', at: known?.at ?? new Date().toISOString(), excerpt }
    this.save()
    return this.all[given.id]!
  }

  /** Sends what is left of it, part by part; `may` is asked before each part (her conversation's rules). One run per delivery at a time. */
  send(id: string, may: () => boolean = () => true): Promise<Delivery> {
    const running = this.busy.get(id)
    if (running) return running
    const run = this.run(this.all[id]!, may).finally(() => this.busy.delete(id))
    this.busy.set(id, run)
    return run
  }

  /** What is due: unknown outcomes asked about again, and what waited (quiet hours, a channel that was down, a refusal) tried again. */
  async tick(may: (delivery: Delivery) => boolean) {
    const now = new Date().toISOString()
    for (const delivery of Object.values(this.all)) {
      if (delivery.state === 'unknown' && delivery.checkAt! <= now) await this.check(delivery, () => may(delivery))
      else if (delivery.state === 'pending' && !this.busy.has(delivery.id)) await this.send(delivery.id, () => may(delivery))
      else if (delivery.state !== 'pending' && !delivery.remembered) await this.settled(delivery)
    }
  }

  /** Notes what became of a delivery outside its sending (settled with Core, cancelled before it went). */
  mark(id: string, changes: Partial<Delivery>) {
    Object.assign(this.all[id]!, changes)
    this.save()
  }

  private async run(delivery: Delivery, may: () => boolean): Promise<Delivery> {
    if (delivery.state !== 'pending' || (delivery.notBefore && delivery.notBefore > new Date().toISOString())) return delivery
    const transport = this.transportOf(delivery.transport)
    // Its channel is not up: nothing went out, and it waits.
    if (!transport) return delivery
    for (let index = delivery.sent.length; index < delivery.parts.length; index++) {
      if (!may()) return delivery
      const sent: Sent = await transport.send(delivery.to, delivery.parts[index]!, partId(delivery, index))
        .catch((error: Error) => ({ state: 'unknown', reason: error.message }))
      if (sent.state === 'failed') {
        delivery.tries = (delivery.tries ?? 0) + 1
        delivery.reason = sent.reason
        if (delivery.tries < TRIES) return this.save(), delivery
        return this.finish(delivery, delivery.sent.length ? 'needs-review' : 'cancelled')
      }
      delivery.sent.push(sent)
      delivery.tries = 0
      if (sent.state === 'unknown') {
        delivery.checks = 0
        delivery.checkAt = new Date(Date.now() + this.host.ms(MINUTE)).toISOString()
        delivery.reason = sent.reason
        return this.finish(delivery, 'unknown')
      }
      this.save()
    }
    return this.finish(delivery, 'accepted')
  }

  /** Check an uncertain part before a bounded retry under the same id; only confirmed parts let the rest go. */
  private async check(delivery: Delivery, may: () => boolean) {
    const index = delivery.sent.length - 1
    const transport = this.transportOf(delivery.transport)
    let answer: Sent = transport?.check
      ? await transport.check(delivery.to, partId(delivery, index)).catch(() => ({ state: 'unknown' as const }))
      : { state: 'unknown' }
    if (answer.state === 'unknown' && transport && may() && (delivery.retries ?? 0) < TRIES) {
      delivery.retries = (delivery.retries ?? 0) + 1
      this.save()
      const retried = await transport.send(delivery.to, delivery.parts[index]!, partId(delivery, index), true)
        .catch((error: Error): Sent => ({ state: 'unknown', reason: error.message }))
      // A refused retry cannot prove the original uncertain attempt never arrived.
      if (retried.state === 'accepted') answer = retried
      else if (retried.reason) delivery.reason = retried.reason
    }
    if (answer.state === 'unknown') {
      delivery.checks = (delivery.checks ?? 0) + 1
      if ((delivery.retries ?? 0) >= TRIES) return void await this.finish(delivery, 'needs-review')
      delivery.checkAt = new Date(Date.now() + this.host.ms(MINUTE)).toISOString()
      return this.save()
    }
    delete delivery.retries
    if (answer.state === 'accepted') delivery.sent[index] = answer
    else delivery.sent.pop()
    delivery.state = 'pending'
    delivery.remembered = false
    delete delivery.checkAt
    await this.send(delivery.id, may)
  }

  private async finish(delivery: Delivery, state: State) {
    delivery.state = state
    delivery.remembered = false
    if (state === 'accepted' || state === 'cancelled' || state === 'needs-review') delete delivery.checkAt
    this.save()
    await this.settled(delivery)
    return delivery
  }

  private save() {
    const old = new Date(Date.now() - KEEP_MS).toISOString()
    for (const [id, delivery] of Object.entries(this.all)) {
      if ((delivery.state === 'accepted' || delivery.state === 'cancelled') && delivery.remembered && delivery.at < old) delete this.all[id]
    }
    this.host.save(FILE, this.all)
  }
}
