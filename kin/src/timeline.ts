/**
 * How long her latest replies took, step by step, for her health page: a message's arrival, its way into
 * her conversation, her turn coming round, her recall, her first word and, on a platform, the first bubble
 * landing there. Kept for this run only: it says where the time goes, nothing else reads it.
 */

/** The moments of one reply, in the order they come. */
const MOMENTS = ['arrived', 'entered', 'step', 'recalled', 'spoke', 'sent'] as const
type Moment = (typeof MOMENTS)[number]

/** One reply as her page shows it; every span in milliseconds, absent when it did not happen. */
export interface Reply {
  /** The conversation's key (`app`, `wechat:<chat>`…) and when the message arrived. */
  key: string
  at: string
  /** Waiting for the quiet that ends a burst (platforms). */
  gathered?: number
  /** Waiting for her turn or her next step: she was in the middle of something. */
  waited?: number
  recall?: number
  /** From her recall to her first word. */
  firstWord?: number
  /** From her first word to the first bubble accepted by the platform. */
  sent?: number
  total: number
}

const KEPT = 20

export class Timeline {
  private readonly open = new Map<string, { key: string; moments: Partial<Record<Moment, number>> }>()
  private readonly done: Reply[] = []

  /** Something was said to her in this session; what is said before she has answered joins the same reply. */
  begin(session: string, key: string, arrived = Date.now()) {
    const known = this.open.get(session)
    if (known && !known.moments.spoke) return
    if (known) this.end(session)
    this.open.set(session, { key, moments: { arrived, entered: Date.now() } })
  }

  /** A moment of the reply under way in this session, noted the first time it comes. */
  mark(session: string, moment: 'step' | 'recalled' | 'spoke') {
    const reply = this.open.get(session)
    if (reply) reply.moments[moment] ??= Date.now()
  }

  /** Her turn is over. On a platform a reply she gave is still on its way out and ends when it lands. */
  rest(session: string, platform: boolean) {
    const reply = this.open.get(session)
    if (reply && !(platform && reply.moments.spoke)) this.end(session)
  }

  /** A bubble of hers was accepted by the platform: the reply she has spoken is there. */
  landed(session: string) {
    const reply = this.open.get(session)
    if (!reply?.moments.spoke) return
    reply.moments.sent = Date.now()
    this.end(session)
  }

  private end(session: string) {
    const reply = this.open.get(session)
    if (!reply) return
    this.open.delete(session)
    const { moments } = reply
    const span = (from: Moment, to: Moment) => (moments[from] && moments[to] ? { [NAMES[to]]: moments[to] - moments[from] } : {})
    const last = Math.max(...MOMENTS.map((moment) => moments[moment] ?? 0))
    this.done.unshift({
      key: reply.key, at: new Date(moments.arrived!).toISOString(),
      ...span('arrived', 'entered'), ...span('entered', 'step'), ...span('step', 'recalled'), ...span('recalled', 'spoke'), ...span('spoke', 'sent'),
      total: last - moments.arrived!,
    })
    this.done.length = Math.min(this.done.length, KEPT)
  }

  list(): Reply[] {
    return this.done
  }
}

/** The span that ends at each moment. */
const NAMES: Record<Moment, string> = { arrived: '', entered: 'gathered', step: 'waited', recalled: 'recall', spoke: 'firstWord', sent: 'sent' }
