// Offline check of her health and her freeze: a real headless DSH with dsh-kin and her real mind on synthetic data.
// Her task lane's only route goes nowhere and dev/health-probe.mjs kills her memory worker: both show on the health
// route, with what to do, and in DSH's log. A freeze stops her heartbeat and memory worker and holds across a
// restart; a thaw starts both again.
// Usage: node dev/e2e-health.mjs        (run `npm run build` first; about a minute)
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { bootDsh, kinDir, PACE, repo, signIn, stopDsh } from './env.mjs'

const out = join(repo, '.dev', 'e2e-health', 'probe.json')
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const problem = (view, key) => view.problems?.find((p) => p.key === key)
const checks = {}
const seen = {}
// Slower than the other suites: the stopped worker must still be down when the route is asked.
const pace = Math.max(PACE, 0.5)

const first = await bootDsh({
  name: 'e2e-health', pace,
  kinFiles: { 'models.json': { chat: [], task: [{ provider: 'nowhere', model: 'gone' }] } },
  extraRows: `- insert:
    - id: kin-e2e-health
      name: ${JSON.stringify(join(kinDir, 'dev/health-probe.mjs'))}
      config:
        out: ${JSON.stringify(out)}
`,
})
try {
  const page = await signIn(await first.url)
  for (const end = Date.now() + 180_000; !existsSync(out) && Date.now() < end && first.dsh.exitCode === null;) await wait(250)
  const probe = existsSync(out) ? JSON.parse(readFileSync(out, 'utf8')) : { error: 'no probe report' }
  if (probe.error) throw new Error(probe.error + '\n' + first.logs().slice(-3000))

  seen.broken = await page.post('api/kin/health/check', {})
  const lane = problem(seen.broken, 'model:task')
  checks['a dead task route shows on the health route, with what to do'] = lane?.level === 'error' && lane.detail.includes('nowhere/gone') && Boolean(lane.todo)
  checks['a stopped memory worker shows on the health route'] = problem(seen.broken, 'worker')?.level === 'error'
  checks['problems reach DSH\'s log'] = readFileSync(out + '.log', 'utf8').includes('health: 做事线接不上模型')

  const beating = seen.broken.now.lastMinute
  seen.frozen = await page.post('api/kin/health/freeze', { frozen: true })
  await wait(75_000 * pace) // past a minute: an unfrozen heartbeat beats again, a crashed worker is started again
  seen.frozenLater = await page.get('api/kin/health')
  checks['freeze stops her heartbeat and memory worker'] = Boolean(beating && seen.frozen.frozen)
    && seen.frozenLater.now.lastMinute === beating && !seen.frozenLater.worker.pid
} finally {
  await stopDsh(first)
}

const second = await bootDsh({ name: 'e2e-health', fresh: false, pace })
try {
  const page = await signIn(await second.url)
  // The route answers once her mind is up, after her heartbeat would have beaten for the first time.
  seen.restarted = await page.get('api/kin/health')
  checks['still frozen after a restart, nothing beat'] = Boolean(seen.restarted.frozen)
    && seen.restarted.now.lastMinute === null && !seen.restarted.worker.pid
  seen.thawed = await page.post('api/kin/health/freeze', { frozen: false })
  await wait(5_000)
  seen.thawedLater = await page.get('api/kin/health')
  checks['thaw starts her heartbeat and memory worker again'] = seen.thawed.frozen === null && Boolean(seen.thawed.now.lastMinute)
    && Boolean(seen.thawedLater.worker.pid)
} finally {
  await stopDsh(second)
}

for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
console.log(`\nproblems found: ${seen.broken.problems.map((p) => `${p.level} ${p.title}`).join(' · ')}`)
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
