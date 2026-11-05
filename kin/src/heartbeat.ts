/**
 * KIN's heartbeat: the minute that keeps her inner life going while nobody is talking to her,
 * as the old host's clock did (legacy/host/mind-host.mjs, core/adapters/owner-host.mjs).
 *
 * - Each minute Core does its own bookkeeping in `review-due` (wishes falling due, plans, the idle
 *   review she set herself, boredom, letters whose day has come) and says whether an appraisal is
 *   waiting. Only then does a review process start; rounds go on while they complete, at most 8.
 * - A completed appraisal leaves the day's review and the memory warm-up due.
 * - The enrichment lane (this stretch's note, memories of archived wishes, memory jobs) has its own minute.
 * - Her conversation comes first: nothing new starts while she is in the middle of a reply, and the
 *   end of a reply looks again at once.
 * - Frozen by the owner (her health controls), no lane starts a round and the memory worker rests;
 *   rounds already going finish. Thawed, the next minute starts at once.
 */
import type { Mind } from './mind.js'

const MINUTE = 60_000
const ROUNDS = 8
/** A day's review that waits on anything else is asked again after this. */
const DAILY_RETRY_MS = 15 * MINUTE
/** Waits of the day's review that only a newly completed appraisal can end. */
const APPRAISAL_WAITS = new Set([
  'need-personality-proposal', 'proposal-incompatible', 'need-three-independent-interactions', 'need-prospective-behavioral-check',
])

/** review, daily, enrichment, preparation, and whatever lanes her other modules add with `every`. */
export type Lane = string

/** How a lane's latest round went, for her page, and when one last completed. */
export interface Beat {
  state: string
  reason?: string
  at: string
  done?: string
  /** Rounds in a row that failed. */
  failures?: number
}

/** What a round reports, for her page. */
export interface Answer {
  state?: string
  reason?: string
  /** Why an appraisal is still waiting, or what failed it. */
  waiting_reason?: string
  error?: string
  result?: { already_integrated?: boolean }
}

interface Due {
  state?: string
  reason?: string
  action?: boolean
  enrichment?: boolean
  daily?: boolean
}

export class Heartbeat {
  readonly beats: Partial<Record<Lane, Beat>> = {}
  /** Rounds under way, and since when. */
  readonly rounds = new Map<Lane, string>()
  /** When the minute last started its rounds. */
  lastMinute?: string
  private readonly lanes = new Map<Lane, () => Promise<Answer | void>>()
  private timer?: NodeJS.Timeout
  private closed = false
  private paused = false
  private preparationDue = false
  private dailyDue = false
  private dailyRetryAt = 0

  constructor(
    private readonly mind: Mind,
    /** Whether she is in the middle of a reply. */
    private readonly replying: () => boolean,
    private readonly log: (message: string) => void,
    /** Dev pace (index.ts `pace`): the minute passes this many times as long. */
    private readonly pace: number,
  ) {}

  /** After start-up recovery: what a previous run left half done is settled before anything new runs. */
  async start() {
    await this.mind.run('recover')
    await this.mind.run('plan-recover', { workers_stopped: true })
    if (this.closed) return
    if (!this.paused) this.mind.keepWorker()
    this.timer = setInterval(() => this.minute(), MINUTE * this.pace)
    this.timer.unref()
    this.minute()
  }

  close() {
    this.closed = true
    clearInterval(this.timer)
  }

  get frozen() {
    return this.paused
  }

  /** Frozen, nothing of her background life starts (her own lanes and every module's) and the memory worker rests. */
  freeze(on: boolean) {
    if (on === this.paused) return
    this.paused = on
    if (!this.timer) return
    if (on) {
      this.mind.restWorker()
    } else {
      this.mind.keepWorker()
      this.minute()
    }
  }

  /** Something happened (she was spoken to, she answered): look now rather than at the next minute. */
  nudge() {
    if (this.timer) void this.review()
  }

  /**
   * Another minute lane of hers (exploring, creating, reaching out…), kept like the others: one round at a
   * time, none while she is replying, a failure noted rather than thrown. What the round returns is noted.
   */
  every(lane: Lane, round: () => Promise<Answer | void>) {
    this.lanes.set(lane, round)
  }

  private minute() {
    if (this.paused) return
    this.lastMinute = new Date().toISOString()
    void this.review()
    void this.enrich()
    for (const [lane, round] of this.lanes) {
      void this.lane(lane, async () => {
        const answer = await round()
        if (answer) this.note(lane, answer)
      })
    }
  }

  /** How a lane is doing, for her page. */
  note(lane: Lane, answer: Answer) {
    const at = new Date().toISOString()
    const reason = answer.reason ?? answer.waiting_reason ?? answer.error
    const previous = this.beats[lane]
    const done = answer.state === 'complete' ? at : previous?.done
    const failures = answer.state === 'failed' ? (previous?.failures ?? 0) + 1 : answer.state === 'running' ? previous?.failures : 0
    this.beats[lane] = {
      state: answer.state ?? 'unknown', ...(reason ? { reason } : {}), at, ...(done ? { done } : {}), ...(failures ? { failures } : {}),
    }
  }

  /** Nothing to run: a pause is worth saying, an idle minute is not (the last round that ran stays on show). */
  private quiet(lane: Lane, due: Due) {
    if (due.state === 'paused') this.note(lane, due)
  }

  /** One lane's round, never two at once and none while she is replying or frozen. */
  private async lane(lane: Lane, round: () => Promise<void>) {
    if (this.closed || this.paused || this.rounds.has(lane) || this.replying()) return
    this.rounds.set(lane, new Date().toISOString())
    try {
      await round()
    } catch (error) {
      this.note(lane, { state: 'failed', reason: (error as Error).message })
      this.log(`heartbeat ${lane}: ${(error as Error).message}`)
    } finally {
      this.rounds.delete(lane)
    }
  }

  private review() {
    return this.lane('review', async () => {
      for (let round = 0; round < ROUNDS && !this.closed && !this.paused; round++) {
        // The minute's bookkeeping happens in this call; the answer says whether a review is waiting.
        const due = await this.mind.call<Due>('review-due', {})
        if (due.action !== true) return this.quiet('review', due)
        this.note('review', { state: 'running' })
        const answer = await this.mind.run<Answer>('review')
        this.note('review', answer)
        if (answer.state !== 'complete') break
        if (!answer.result?.already_integrated) {
          this.dailyDue = true
          this.preparationDue = true
        }
      }
    }).then(() => {
      void this.prepare()
      void this.daily()
    })
  }

  /** Deep recall for the conversation, pressed ahead of time from what was just taken in. */
  private prepare() {
    if (!this.preparationDue) return
    return this.lane('preparation', async () => {
      this.preparationDue = false
      this.note('preparation', { state: 'running' })
      this.note('preparation', await this.mind.run<Answer>('prepare-memory'))
    })
  }

  /** The day's review: who she is becoming, from what the appraisals found. */
  private daily() {
    if (!this.dailyDue && this.dailyRetryAt && Date.now() >= this.dailyRetryAt) {
      this.dailyDue = true
      this.dailyRetryAt = 0
    }
    if (!this.dailyDue) return
    return this.lane('daily', async () => {
      this.dailyDue = false
      const due = await this.mind.call<Due>('review-due', { tick: false })
      if (due.daily !== true && (due.state === 'paused' || typeof due.daily === 'boolean')) {
        this.dailyRetryAt = 0
        return this.quiet('daily', due)
      }
      this.note('daily', { state: 'running' })
      const answer = await this.mind.run<Answer>('daily')
      this.note('daily', answer)
      this.dailyRetryAt = answer.state === 'waiting' && !APPRAISAL_WAITS.has(answer.reason ?? '') ? Date.now() + DAILY_RETRY_MS * this.pace : 0
    })
  }

  /** The enrichment lane: this stretch's note, memories of archived wishes, and memory jobs. */
  private enrich() {
    return this.lane('enrichment', async () => {
      const due = await this.mind.call<Due>('review-due', { tick: false })
      if (due.enrichment !== true) return this.quiet('enrichment', due)
      this.note('enrichment', { state: 'running' })
      this.note('enrichment', await this.mind.run<Answer>('review-enrichment'))
    })
  }
}
