// Offline check of what she does in a conversation (src/talk/): Core's plan tools in her requests, notes to
// herself, a reply held for later and given when its time comes, a persona change that waits for the owner,
// and the per-route connectivity test. A real headless DSH, her real mind on synthetic data, scripted models.
// Usage: node dev/e2e-talk.mjs        (run `npm run build` first; takes about eight minutes)
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { bootDsh, kinDir, repo } from './env.mjs'

const out = join(repo, '.dev', 'e2e-talk', 'report.json')
const dataDir = join(repo, '.dev', 'e2e-talk', 'kin-data')
// Her chat lane is the scenario's scripted model, then DSH's own (the mock) for the turn the harness measures.
const kinFiles = { 'models.json': {
  chat: [{ provider: 'kin-chat-test', model: 'scripted' }, { provider: 'deepseek-official', model: 'deepseek-flash' }],
  task: [{ provider: 'kin-test', model: 'scripted' }],
} }
const { root, mock, dsh, url, logs: readLogs } = await bootDsh({ name: 'e2e-talk', kinFiles, extraRows: `- insert:
    - id: kin-e2e-talk
      name: ${JSON.stringify(join(kinDir, 'dev/scenario-talk.mjs'))}
      config:
        out: ${JSON.stringify(out)}
        dataDir: ${JSON.stringify(dataDir)}
` })

// Her page, called the way the browser calls it: the login link mints a cookie.
const base = await url
const login = await fetch(base, { redirect: 'manual' })
const cookie = login.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ')
const page = (route, body) => fetch(new URL('api/kin/' + route, base), {
  method: body ? 'POST' : 'GET', headers: { cookie, 'content-type': 'application/json' }, body: body && JSON.stringify(body),
}).then((response) => response.json())

const pages = {}
pages.choices = await page('talk/choices', { replyChoice: 'autonomous', energy: true })
// A letter the owner seals for her, a week from now.
pages.features = await page('features', { sealed_entries: true })
const day = new Date(Date.now() + 7 * 86_400_000).toISOString().slice(0, 10)
pages.letter = await page('letters', { text: '等你拆开的时候，我们应该已经一起读完那本书了。', unlock_at: day })
// The owner approves her persona proposal when it shows up on her page.
const deadline = Date.now() + 720_000
while (!existsSync(out) && Date.now() < deadline && dsh.exitCode === null) {
  const talk = await page('talk').catch(() => null)
  if (talk?.held) pages.held = talk.held
  await new Promise((r) => setTimeout(r, 1000))
}
if (existsSync(out)) {
  pages.broken = await page('models/test', { provider: 'nowhere', model: 'gone' })
  pages.working = await page('models/test', { provider: 'deepseek-official', model: 'deepseek-flash' })
  pages.after = await page('talk')
}
dsh.kill('SIGTERM')
await Promise.race([new Promise((done) => dsh.once('exit', done)), new Promise((done) => setTimeout(done, 5000))])
if (dsh.exitCode === null && dsh.signalCode === null) dsh.kill('SIGKILL')
await mock.close()
const logs = readLogs()
writeFileSync(join(root, 'dsh.log'), logs)
if (!existsSync(out)) {
  const kinLog = existsSync(out + '.log') ? readFileSync(out + '.log', 'utf8') : ''
  console.error('No report. DSH exited with', dsh.exitCode, '— last log lines:\n' + logs.split('\n').slice(-40).join('\n')
    + '\nKIN log tail:\n' + kinLog.split('\n').slice(-40).join('\n'))
  process.exit(1)
}
const report = JSON.parse(readFileSync(out, 'utf8'))
if (report.error) {
  console.error('Scenario failed:\n' + report.error + '\n\nlog tail:\n' + logs.split('\n').slice(-30).join('\n'))
  process.exit(1)
}

// The KIN request that went to DSH's own model, kept for measuring what every request of hers carries.
const measured = mock.requests.find((request) => JSON.stringify(request.body).includes('量一下'))?.body
writeFileSync(join(root, 'kin-request.json'), JSON.stringify(measured ?? null, null, 2))
const toolsOf = (body) => (body?.tools ?? []).map((tool) => tool.name ?? tool.function?.name)
const coding = mock.requests.find((request) => JSON.stringify(request.body).includes('看看这里有什么'))?.body
const said = (turn) => turn.events.filter((e) => e.type === 'assistant/message' && e.text).map((e) => e.text)
const OWN = ['read_autonomous_plans', 'manage_autonomous_plan', 'manage_desire', 'read_affective_state', 'read_letters', 'read_diary',
  'read_graph', 'kin_reply', 'kin_note', 'kin_persona', 'kin_recall']

const checks = {
  'her tools ride in her requests': OWN.every((tool) => report.chatTools.includes(tool) && toolsOf(measured).includes(tool)),
  'a coding session gets none of them': coding !== undefined && !toolsOf(coding).some((tool) => OWN.includes(tool)),
  'a plan made in conversation, named after the message': report.plans.includes('这周和对方一起读完那本书'),
  'a note to herself, tied to what was said': report.notes.some((note) => note.text.includes('心疼') && note.input_id === report.noteInput),
  'recall brings the note back': report.recallResult?.includes('你当时心里记下的') && report.recallResult.includes('担心他睡不好'),
  '"later" holds the reply': said(report.later).length === 0 && report.laterChoice.action === 'defer' && pages.held === 1,
  'the held reply comes back into the conversation': typeof report.cameBack === 'string' && report.laterWish === 'completed' && pages.after?.held === 0,
  'persona changes directly and reaches the next request': report.personaApproved
    && report.afterApprovalSystem.includes('下雨天') && pages.after?.proposal === undefined,
  'owner choices reach her mind; a reply costs her': report.ownerChoices && report.energy?.recent_costs?.reply >= 1,
  'a wish made with Core\'s own tool; a wrong call is told why': report.wishCalls.length === 2 && report.wishCalls[0].includes('expiry')
    && report.wishes.length === 1 && report.wishes[0] !== 'abandoned',
  'the letter the owner sealed shows up in her letters, unopened': pages.letter?.state === 'sealed'
    && report.lettersResult?.includes(`一封 ${day} 才能打开的信`) && !report.lettersResult.includes('一起读完那本书'),
  'connectivity test: a broken route says so, a working one answers': pages.broken?.ok === false && Boolean(pages.broken.reason)
    && pages.working?.ok === true && pages.working.ms >= 0,
}
for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
console.log(`\nKIN tools: ${toolsOf(measured).join(', ')}`)
console.log(`broken route: ${JSON.stringify(pages.broken)} · working: ${JSON.stringify(pages.working)}`)
console.log(`report: ${out} · measured request: ${join(root, 'kin-request.json')}`)
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
