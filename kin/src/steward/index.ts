/**
 * KIN as everyone's chief steward (M5): the owner's work goes to DSH workers she follows (work.ts,
 * preset.ts), and her own exploring and making run as DSH helpers on her task lane, on her minute
 * (explore.ts, create.ts, through helpers.ts). Her page shows both and can stop them, and holds how far
 * her helpers may go (permission.ts).
 */
import type { Host } from '../index.js'
import { creating } from './create.js'
import { exploring } from './explore.js'
import { helpers } from './helpers.js'
import { permission } from './permission.js'
import { work } from './work.js'

export function steward(host: Host) {
  const permit = permission(host)
  work(host, permit)
  const run = helpers(host, permit)
  const outings = { exploration: exploring(host, run), creation: creating(host, run) }

  // 出门 and 心愿: what she is out exploring or making right now, what came of the last one, and a stop.
  host.route('/api/kin/outing', {
    GET: async () => Object.fromEntries(Object.entries(outings).map(([kind, outing]) => [kind, {
      current: outing.current(), last: outing.last(), beat: host.heartbeat.beats[kind] ?? null,
    }])),
  })
  host.route('/api/kin/outing/stop', {
    POST: async (request) => {
      const { kind } = await host.body<{ kind: keyof typeof outings }>(request)
      if (!kind || !(kind in outings)) throw new Error('不知道要停哪一件')
      outings[kind].stop()
      return { stopping: true }
    },
  })
}
