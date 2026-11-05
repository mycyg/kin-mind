// Offline check of KIN as chief steward (M5): a real headless DSH with dsh-kin and DSH's Auto review
// switched on, her chat and task lanes scripted (dev/scenario-steward.mjs), KIN's real mind on a fresh
// synthetic store.
// Usage: node dev/e2e-steward.mjs        (run `npm run build` first; takes several minutes)
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { bootDsh, kinDir, repo } from './env.mjs'

const root = join(repo, '.dev', 'e2e-steward')
const out = join(root, 'report.json')
const dataDir = join(root, 'kin-data')
const kinFiles = {
  'conversation-import.json': { 'app:friend': { channel: 'app', recipient: 'person:friend', title: '朋友的测试对话' } },
  'models.json': { chat: [{ provider: 'kin-test', model: 'chat' }], task: [{ provider: 'kin-test', model: 'scripted' }] },
  // The owner chose 自动审核 for her helpers (left unchosen it is 全权).
  'permission.json': { preset: 'auto' },
  // Work a previous DSH left going, whose worker is gone.
  'work.json': { 'gone-worker': { id: 'gone-worker', session: 'gone-session', label: '旧的活', folder: '/tmp', state: 'running', started: '2026-11-01T00:00:00.000Z' } },
}
const { dsh, mock, url, logs: readLogs } = await bootDsh({ name: 'e2e-steward', kinFiles, extraRows: `- insert:
    - id: auto-review
      name: '@deepseek-ai/dsh-experimental-auto-review'
    - id: kin-e2e-steward
      name: ${JSON.stringify(join(kinDir, 'dev/scenario-steward.mjs'))}
      config: ${JSON.stringify({
        out, dataDir, kinDir, workDir: join(root, 'work'),
        python: join(repo, 'core/.venv/bin/python'), coreSource: join(repo, 'core/src'),
      })}
` })

const deadline = Date.now() + 1_500_000
while (!existsSync(out) && Date.now() < deadline && dsh.exitCode === null) await new Promise((r) => setTimeout(r, 1000))

// Her page's routes, as the browser calls them.
const pages = {}
if (existsSync(out)) {
  const login = await fetch(await url, { redirect: 'manual' })
  const cookie = login.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ')
  for (const route of ['work', 'outing', 'permission', 'home']) {
    const response = await fetch(new URL('api/kin/' + route, await url), { headers: { cookie } })
    pages[route] = { status: response.status, body: await response.json().catch(() => null) }
  }
}
dsh.kill('SIGTERM')
await Promise.race([new Promise((done) => dsh.once('exit', done)), new Promise((done) => setTimeout(done, 5000))])
if (dsh.exitCode === null && dsh.signalCode === null) dsh.kill('SIGKILL')
await mock.close()
const logs = readLogs()
writeFileSync(join(root, 'dsh.log'), logs)
const kinLog = existsSync(out + '.log') ? readFileSync(out + '.log', 'utf8') : ''
if (!existsSync(out)) {
  console.error('No report. Last DSH log lines:\n' + logs.split('\n').slice(-40).join('\n') + '\nKIN log tail:\n' + kinLog.split('\n').slice(-40).join('\n'))
  process.exit(1)
}
const report = JSON.parse(readFileSync(out, 'utf8'))
if (report.error) {
  console.error('Scenario failed:\n' + report.error + '\n\nKIN log tail:\n' + kinLog.split('\n').slice(-30).join('\n'))
  process.exit(1)
}

const events = (turn) => turn?.events ?? []
const checks = {
  'KIN has her steward tools and DSH\'s subagent ones': ['kin_delegate', 'kin_work', 'kin_work_tell', 'kin_work_stop', 'subagent', 'send_message', 'list_agents']
    .every((tool) => report.kinTools.includes(tool)),
  'with no folder named, no worker starts': report.ledgerAfterNoFolder === 0 && events(report.noFolder).some((e) => e.type === 'tool/result'),
  'a worker did the owner\'s work in their folder': report.work?.state === 'done' && report.work.folder === join(root, 'work') && report.file?.includes('周一'),
  'the worker ran on her task lane': report.workerLanes.length > 0 && report.workerLanes.every((model) => model === 'scripted'),
  'its result came back to the conversation that asked, and she told it': typeof report.toldOwner === 'string' && report.toldOwner.includes('整理'),
  'the result joined her memory': typeof report.workInMemory === 'string',
  'a friend’s worker result retains its recipient and source conversation': report.friendWork?.state === 'done' && report.taskEvents.some((event) => event.task_id === report.friendWork.id && event.recipient_person_id === 'person:friend' && event.conversation_key === 'app:friend'),
  'the worker runs with the owner\'s 帮手的权限 (Auto, as chosen)': report.workerPreset === 'auto',
  'under Auto, a subagent she called wrote in the owner\'s folder, its step reviewed first': report.note?.includes('周五') && report.reviews.some((review) => review.note),
  'work a restart cut off is marked, not lost': report.reconciled === 'interrupted',
  'an exploration ran on a DSH helper and landed in her explorations': report.exploration?.state === 'complete',
  'a plan step was claimed, made and completed': report.creation?.state === 'completed',
  'her page shows the work, her outings and the helpers\' permission': pages.work?.status === 200 && pages.work.body?.work?.some((item) => item.label === '整理笔记')
    && pages.outing?.status === 200 && pages.outing.body?.exploration?.last?.state === 'complete' && pages.outing.body?.creation?.last?.state === 'completed'
    && pages.permission?.body?.current === 'auto' && pages.permission.body.offered.length === 3,
}
for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
console.log(`\nseeded: ${JSON.stringify(report.seeded)} · worker's write: ${report.workerWrite ?? null}`)
console.log(`Auto reviews: ${report.reviews?.length ?? 0}, largest ${Math.max(0, ...(report.reviews ?? []).map((review) => review.chars))} chars`)
console.log(`outing: ${JSON.stringify(pages.outing?.body ?? null)}`)
console.log(`report: ${out}`)
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
