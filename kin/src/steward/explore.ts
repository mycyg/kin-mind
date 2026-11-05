/**
 * Her exploring (PARITY 2). Core decides everything — curiosity becomes a sourced question, her review
 * leaves a wake file when one is ready, `prepare-exploration` picks it, `explore` runs it within its
 * budget and keeps what it found — and the run itself is a DSH helper on her task lane with web search
 * and fetch: Core's executor (core dsh_executor.py) reaches it through the bridge's `/v1/kin/helper`.
 * A stop is Core's own stop file, which the executor reads and answers by ending the helper.
 */
import { existsSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Host } from '../index.js'
import type { Helpers } from './helpers.js'

/** One exploration's budget, the old host's (selection included). */
const BUDGET_SECONDS = 1200

export interface Outing {
  /** What she is out doing right now. */
  current(): { title: string; since: string } | null
  /** What came of the last one. */
  last(): { title: string; state: string; at: string; summary?: string; folder?: string } | null
  stop(): void
}

export function exploring(host: Host, run: Helpers): Outing {
  const state = join(host.config.dataDir, 'state')
  // Core's review writes it (host.py, review-due) for her owner's own scope; its stop file is the mind config's.
  const wake = join(state, 'mind-exploration-request.json')
  const stopFile = join(state, 'exploration-stop.json')
  let current: ReturnType<Outing['current']> = null
  let last: ReturnType<Outing['last']> = null

  host.ctx.effect(() => host.bridge.serve('/v1/kin/helper', async (body, signal) => {
    const helper = await run({ label: '出门：' + body.label, prompt: body.prompt, cwd: body.cwd, timeoutMs: body.timeout_ms, signal })
    return { state: helper.state, reason: helper.state === 'complete' ? undefined : helper.state, text: helper.text, session: helper.session, reads: helper.reads }
  }), 'kin helper route')

  host.heartbeat.every('exploration', async () => {
    if (!existsSync(wake)) return
    rmSync(wake, { force: true })
    rmSync(stopFile, { force: true })
    const begun = Date.now()
    // Nothing ready to explore after all (a wake can outlive the question it was for): an idle minute.
    const context = await host.mind.run('prepare-exploration')
    if (context.state !== 'ready') return
    current = { title: context.desire?.topic || context.brief, since: new Date().toISOString() }
    host.heartbeat.note('exploration', { state: 'running' })
    try {
      const result = await host.mind.run('explore', {
        brief: context.brief, desire_id: context.desire.id, budget_seconds: Math.floor(BUDGET_SECONDS - (Date.now() - begun) / 1000),
      })
      last = { title: current.title, state: result.state, at: new Date().toISOString(), summary: result.result?.summary }
      // What she found is hers to think over now.
      host.heartbeat.nudge()
      return { state: result.state, reason: result.reason ?? result.waiting_reason ?? result.error }
    } finally {
      current = null
    }
  })

  return {
    current: () => current,
    last: () => last,
    stop() {
      if (current) writeFileSync(stopFile, JSON.stringify({ kind: 'owner-stop', at: new Date().toISOString() }) + '\n')
    },
  }
}
