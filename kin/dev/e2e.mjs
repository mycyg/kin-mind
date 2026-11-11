// Offline end-to-end check: a real headless DSH (this package's devDependency) with dsh-kin installed,
// a scripted mock model, KIN's real Python mind on a fresh synthetic store, and dev/scenario.mjs.
// Usage: node dev/e2e.mjs        (run `npm run build` first)
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { bootDsh, kinDir, repo } from './env.mjs'

const out = join(repo, '.dev', 'e2e', 'report.json')
const dataDir = join(repo, '.dev', 'e2e', 'kin-data')
// KIN's chat lane starts with a route that does not exist, so every reply has to fall over to DSH's own.
// Her task lane is the scenario's scripted model, which answers her background reviews.
const kinFiles = { 'models.json': {
  chat: [{ provider: 'nowhere', model: 'gone' }, { provider: 'deepseek-official', model: 'deepseek-flash' }],
  task: [{ provider: 'kin-test', model: 'scripted' }],
} }
const { root, mock, dsh, url, logs: readLogs } = await bootDsh({ name: 'e2e', mockText: '辛苦啦，火锅走起！', kinFiles, extraRows: `- insert:
    - id: kin-e2e
      name: ${JSON.stringify(join(kinDir, 'dev/scenario.mjs'))}
      config:
        out: ${JSON.stringify(out)}
        dataDir: ${JSON.stringify(dataDir)}
` })

const deadline = Date.now() + 540_000
while (!existsSync(out) && Date.now() < deadline && dsh.exitCode === null) await new Promise((r) => setTimeout(r, 500))

// The page's routes, called the way the browser calls them: the login link mints a cookie.
const pages = {}
if (existsSync(out)) {
  const login = await fetch(await url, { redirect: 'manual' })
  const cookie = login.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ')
  // The review that changed her mood has its round on the page once its process has ended.
  for (const end = Date.now() + 30_000; Date.now() < end; await new Promise((r) => setTimeout(r, 500))) {
    const home = await fetch(new URL('api/kin/home', await url), { headers: { cookie } }).then((r) => r.json()).catch(() => null)
    if (home?.heartbeat?.review?.done) break
  }
  for (const route of ['home', 'diary', 'dreams', 'letters', 'explore', 'settings']) {
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
  console.error('No report. DSH exited with', dsh.exitCode, '— last log lines:\n' + logs.split('\n').slice(-40).join('\n')
    + '\nKIN log tail:\n' + kinLog.split('\n').slice(-40).join('\n'))
  process.exit(1)
}

const report = JSON.parse(readFileSync(out, 'utf8'))
if (report.error) {
  console.error('Scenario failed:\n' + report.error + '\n\nlog tail:\n' + logs.split('\n').slice(-30).join('\n'))
  process.exit(1)
}

// What the model was actually sent: KIN's requests carry the persona and recall; the coding session's do not.
const bodies = mock.requests.map((request) => JSON.stringify(request.body))
const kinRequests = bodies.filter((body) => body.includes('你是 KIN'))
const kinTools = (mock.requests.find((request) => String(request.body.system ?? '').includes('你是 KIN'))?.body?.tools ?? [])
  .map((tool) => tool.name ?? tool.function?.name)
const kinEvents = report.kinTurns.flatMap((turn) => turn.events)
const checks = {
  'first memory is her awakening, not a placeholder': !kinEvents.some((e) => e.text?.includes('KIN starts here')),
  'KIN can recall and delegate': kinTools.includes('kin_recall') && kinTools.includes('kin_delegate'),
  'KIN session runs the kin preset': report.kinPreset === 'kin',
  'KIN replied twice': report.kinTurns.every((turn) => turn.events.some((e) => e.type === 'assistant/message' && e.text)),
  'her inner state reaches her first reply': report.kinTurns[0].events.some((e) => e.source === 'kin-context'),
  'a new conversation recalls the hotpot': report.laterRecall !== null,
  'both turns written to memory': report.recorded > 0 && report.recall.includes('想吃火锅'),
  'persona reaches the model': kinRequests.length >= 2,
  'coding session runs its own preset': report.workPreset === 'standard',
  'coding session gets no KIN context': !report.workTurn.events.some((e) => e.source === 'kin-context'),
  'coding session not recorded': report.workRecorded === null && report.workInMemory === false,
  'coding session requests carry no KIN persona': bodies.filter((b) => b.includes('帮我看看这个目录')).every((b) => !b.includes('你是 KIN')),
  'KIN speaks on her chat lane, coding on DSH\'s model': report.kinRoute === 'kin/chat' && report.workRoute === 'deepseek-official/deepseek-flash',
  'chat lane fell over from the dead route': (() => {
    const last = pages.settings?.body?.models?.lanes?.chat?.last
    return last?.failed?.[0]?.route.provider === 'nowhere' && last?.answered?.provider === 'deepseek-official'
  })(),
  'every page route answers': Object.values(pages).length === 6 && Object.values(pages).every((page) => page.status === 200 && !page.body?.error),
  // Token use: light thinking for chat, no tools she cannot use, her mood said once while it stays the same.
  'KIN chats at low effort, coding keeps its own': (() => {
    const effort = (request) => request.body?.output_config?.effort
    const kin = mock.requests.filter((r) => JSON.stringify(r.body).includes('你是 KIN'))
    const work = mock.requests.filter((r) => JSON.stringify(r.body).includes('帮我看看这个目录') && r.body?.tools?.length)
    return kin.length > 0 && kin.every((r) => effort(r) === 'low') && work.every((r) => effort(r) !== 'low')
  })(),
  'KIN carries no todo or job tools': kinTools.length > 0 && !kinTools.some((name) => /^(todo_|job_)/.test(name)),
  'mood said once while unchanged': kinEvents.filter((e) => e.source === 'kin-context' && e.text.includes('你此刻的感受')).length === 1,
  // Her heartbeat: Core's own review reaches the task lane through the bridge, with her persona, and commits.
  // A round completed (`done`); the latest one may already be waiting again, the minute being short at the dev pace.
  'heartbeat reviewed what was said, on the task lane': report.review !== null && Boolean(pages.home?.body?.heartbeat?.review?.done),
  'the review speaks with her persona': report.review?.system.includes('这是 KIN 当前可编辑的人格') && report.review.system.includes('你是 KIN'),
  'the review changed her mood': typeof report.moodAfter === 'number' && report.moodAfter < report.moodBefore,
}
for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
console.log(`\nKIN tools: ${kinTools.join(', ')}`)
console.log(`heartbeat: ${JSON.stringify(pages.home?.body?.heartbeat ?? null)}`)
console.log(`model requests: ${bodies.length} (KIN ${kinRequests.length}) · report: ${out}`)
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
