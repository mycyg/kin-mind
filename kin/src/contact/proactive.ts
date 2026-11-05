/**
 * Her minute for reaching out (core/adapters/owner-host.mjs, the contact half): for each person who may hear
 * from her, the owner's rules first, then Core's claim → her draft → check → pending → send → settle, all in
 * that person's own view of her mind; for a group she may chime into, whether to say something about what
 * was said there. A send of unknown outcome or a message under way is finished by its delivery, never
 * drafted again. Technical failures in a row hold new drafts, up to two hours apart.
 */
import type { Answer } from '../heartbeat.js'
import type { Host } from '../index.js'
import type { Deliveries, Delivery } from './deliveries.js'
import { draft, type Decision } from './draft.js'
import { isGroup, nameOf, OWNER, type Group, type People, type Person } from './people.js'
import { hold, quiet, type Activity, type Hold, type Rules } from './rules.js'
import type { Conversations } from './conversations.js'

const PAUSE_MAX_MINUTES = 120

/** Why she did or did not reach someone this minute, for her page. */
export interface Reach extends Answer {
  at: string
}

export interface Place {
  key: string
  channel: string
  address: { chat: string; kind: 'direct' | 'group' }
}

export class Reaching {
  readonly status: Record<string, Reach> = {}

  constructor(
    private readonly host: Host,
    private readonly directory: () => People,
    private readonly rules: () => Rules,
    private readonly activity: (id: string) => Activity,
    private readonly note: (id: string, change: Partial<Activity>) => void,
    private readonly deliveries: Deliveries,
    private readonly conversations: Conversations,
  ) {}

  /** One minute: what is due of what she sent, someone else's review, then each person and group in turn. */
  async round(): Promise<Answer> {
    await this.deliveries.tick((delivery) => this.may(delivery))
    await this.othersReview()
    const { people, groups } = this.directory()
    for (const target of [...people, ...groups]) {
      const answer = await this.reach(target).catch((error: Error) => ({ state: 'failed', reason: error.message }))
      this.status[target.id] = { ...answer, at: new Date().toISOString() }
    }
    return this.status[OWNER]!
  }

  /** Where she would reach someone: the conversation they last spoke to her in (the desktop chat for the owner otherwise). */
  placeOf(id: string): Place | undefined {
    const key = this.activity(id).via ?? (id === OWNER ? 'app' : undefined)
    const conversation = key ? this.host.conversations()[key] : undefined
    if (key === 'app') return { key, channel: 'app', address: { chat: 'app', kind: 'direct' } }
    return conversation?.address && { key: key!, channel: conversation.channel, address: conversation.address }
  }

  /** Whether one of her own messages may go out now; a reply may always. */
  may(delivery: Delivery): boolean {
    const rules = this.rules()
    if (delivery.kind === 'reply' || delivery.kind === 'notice') return true
    if (quiet(rules)) return false
    if (delivery.kind === 'reminder' || delivery.kind === 'relay') return true
    return rules.enabled && !this.host.replying() && this.allowed(delivery.recipient)
  }

  private allowed(id: string) {
    const { people, groups } = this.directory()
    return people.find((person) => person.id === id)?.proactive ?? groups.find((group) => group.id === id)?.chime ?? false
  }

  private held(target: Person | Group, chime = false): Hold | 'proactive-off' | 'paused' | undefined {
    if (!this.allowed(target.id)) return 'proactive-off'
    const activity = this.activity(target.id)
    const failures = activity.failures
    if (failures && Date.now() < Date.parse(failures.at) + this.host.ms(Math.min(PAUSE_MAX_MINUTES, 2 ** (failures.count - 1)) * 60_000)) return 'paused'
    return hold(this.rules(), activity, new Date(), { chime })
  }

  private async reach(target: Person | Group): Promise<Answer> {
    if (isGroup(target.id)) return this.chime(target as Group)
    const place = this.placeOf(target.id)
    const call = <T = any>(action: string, args: object = {}) =>
      this.host.mind.call<T>(action, { ...args, recipient_person_id: target.id, conversation_key: place?.key ?? 'app' })
    // The rules hold what is new; a message under way or of unknown outcome is its delivery's to finish.
    const held = this.held(target)
    if (held) return { state: 'waiting', reason: held }
    const epoch = this.activity(target.id).heard ?? ''
    // Waits she set herself (a time, their reply) come round.
    await call('reconsider', { owner_epoch: epoch })
    const candidate = await call('candidate')
    if (!candidate.eligible) return { state: candidate.state === 'pending' ? 'pending' : 'idle', reason: candidate.reason }
    const attempt = await call('claim', { owner_epoch: epoch })
    if (attempt.state !== 'drafting') return { state: attempt.state, reason: attempt.reason }
    const cancel = (reason: string, extra: object = {}) =>
      call('settle', { attempt_id: attempt.id, state: 'canceled', reason, ...extra }).then(() => ({ state: 'idle', reason }))

    let decision: Decision, shown: string[]
    try {
      ({ decision, shown } = await draft(this.host, {
        who: this.who(target), scope: { channel: place?.channel ?? 'app', recipient_person_id: target.id, conversation_key: place?.key ?? 'app' },
        wishes: offered(attempt), unknown: attempt.unconfirmed,
      }))
    } catch (error) {
      this.failed(target.id)
      return cancel('draft-failed', { failure: { category: 'model-unavailable', stage: 'contact-draft', code: 'contact-draft-failed', retry_condition: 'backoff' } })
    }
    this.note(target.id, { failures: undefined })
    const chosen = decision.desire_ids ? { desire_ids: decision.desire_ids } : {}
    const text = decision.bubbles.join('\n\n')
    const checked = await call('check', { attempt_id: attempt.id, owner_epoch: epoch, ...chosen, ...(text ? { text } : {}) })
    const read = { shown_ids: shown }
    if (checked.reason === 'desire-not-offered') {
      return cancel('draft-failed', { ...read, failure: { category: 'model-output', stage: 'contact-draft-output', code: 'contact-draft-unknown-desire', retry_condition: 'deepseek-decision' } })
    }
    if (!checked.eligible || epoch !== (this.activity(target.id).heard ?? '')) return cancel('Draft or delivery conditions changed', read)
    if (decision.action !== 'send') {
      const { action, reason, condition, retry_after_seconds } = decision
      return cancel('draft-decision', { ...read, ...chosen, decision: { action, reason, ...(condition ? { condition } : {}), ...(retry_after_seconds ? { retry_after_seconds } : {}) } })
    }
    if (!text) return cancel('draft-failed', { ...read, failure: { category: 'model-output', stage: 'contact-draft-output', code: 'contact-draft-empty-output', retry_condition: 'deepseek-decision' } })
    if (!place || this.held(target)) return cancel('Delivery conditions changed before sending', read)
    // What she shares from what she found out is kept per person, so the same thing is not told twice.
    if (decision.references?.length) {
      await call('sharing-preflight', { draft_id: attempt.id, recipient: target.id, references: decision.references })
    }
    const delivery = this.deliveries.open({
      id: attempt.id, kind: 'contact', key: place.key, recipient: target.id, transport: place.channel,
      to: place.address, parts: decision.bubbles.map((bubble) => ({ text: bubble })),
    })
    const pending = await call('settle', { attempt_id: attempt.id, state: 'pending', ...chosen, ...read, text, outbound: { id: attempt.id, text, bubbles: decision.bubbles } })
    if (pending.state === 'canceled') {
      this.deliveries.mark(delivery.id, { state: 'cancelled', remembered: true, reason: pending.reason })
      return { state: 'idle', reason: pending.reason }
    }
    const sent = await this.conversations.queue(place.key, () => this.deliveries.send(delivery.id, () => this.may(delivery))) as Delivery
    return { state: sent.state === 'accepted' ? 'complete' : sent.state, reason: sent.reason }
  }

  /** A group she may chime into: what was said there since she last thought about it, and whether to say something. */
  private async chime(group: Group): Promise<Answer> {
    const held = this.held(group, true)
    if (held) return { state: 'waiting', reason: held }
    const place = this.placeOf(group.id)
    const fresh = place ? this.conversations.backlog(place.key).filter((heard) => !heard.considered) : []
    if (!place || !fresh.length) return { state: 'idle', reason: 'nothing-new' }
    const { decision } = await draft(this.host, {
      who: `群「${nameOf(group)}」里的人`, wishes: [], group: fresh.map((heard) => heard.text),
      scope: { channel: place.channel, recipient_person_id: group.id, conversation_key: place.key },
    }).catch((error) => {
      this.failed(group.id)
      throw error
    })
    this.conversations.considered(place.key, fresh.map((heard) => heard.id))
    if (decision.action !== 'send' || !decision.bubbles.length) return { state: 'idle', reason: 'chose-to-wait' }
    const delivery = this.deliveries.open({
      id: `chime:${place.key}:${fresh.at(-1)!.id}`, kind: 'chime', key: place.key, recipient: group.id,
      transport: place.channel, to: place.address, parts: decision.bubbles.map((bubble) => ({ text: bubble })),
    })
    const sent = await this.conversations.queue(place.key, () => this.deliveries.send(delivery.id, () => this.may(delivery))) as Delivery
    return { state: sent.state === 'accepted' ? 'complete' : sent.state }
  }

  /** The heartbeat of someone other than the owner: what they said is taken in by a review in their own view, one a minute. */
  private async othersReview() {
    for (const person of this.directory().people) {
      const key = this.activity(person.id).via
      if (person.id === OWNER || !key) continue
      const scope = { recipient_person_id: person.id, conversation_key: key }
      const due = await this.host.mind.call('review-due', scope)
      if (due.action !== true) continue
      this.status['review:' + person.id] = { ...(await this.host.mind.run<Answer>('review', scope)), at: new Date().toISOString() }
      return
    }
  }

  private failed(id: string) {
    const failures = this.activity(id).failures
    this.note(id, { failures: { count: (failures?.count ?? 0) + 1, at: new Date().toISOString() } })
  }

  private who(target: Person | Group) {
    return nameOf(target)
  }
}

/** The ready wishes a claim offers her, as a draft reads them. */
function offered(attempt: { desires?: Record<string, unknown>[]; desire?: Record<string, unknown> }) {
  return (attempt.desires ?? [attempt.desire]).filter(Boolean).map((wish) => Object.fromEntries(
    ['id', 'topic', 'content', 'completion', 'strength', 'expires_at', 'exploration_id', 'activity_ref'].flatMap((key) => (wish![key] !== undefined ? [[key, wish![key]]] : [])),
  ))
}
