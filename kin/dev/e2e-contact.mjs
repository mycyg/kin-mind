// Offline check of her conversations and reaching out: a real headless DSH with dsh-kin, her real mind on a fresh
// synthetic store, scripted model lanes, and dev/scenario-contact.mjs with a fake WeChat. The owner (小光), a friend
// (小明) and a family group. A few minutes at the dev pace; at her real pace (KIN_PACE=1) about a quarter of an hour:
// her reviews wait three minutes after a conversation, and an unknown send is first asked about five minutes on.
// Usage: node dev/e2e-contact.mjs (after `npm run build`).
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { bootDsh, kinDir, repo } from './env.mjs'

const out = join(repo, '.dev', 'e2e-contact', 'report.json')
const dataDir = join(repo, '.dev', 'e2e-contact', 'kin-data')
const OWNER = 'person:person_a'
const kinFiles = {
  'models.json': { chat: [{ provider: 'kin-test', model: 'chat' }], task: [{ provider: 'kin-test', model: 'scripted' }] },
  'people.json': {
    people: [
      { id: OWNER, name: '小光', proactive: true, accounts: { wechat: 'wx-owner' } },
      { id: 'person:ming', name: '小明', proactive: true, accounts: { wechat: 'wx-ming' } },
    ],
    groups: [{ id: 'group:family', name: '家庭群', chats: { wechat: 'family@chatroom' }, chime: false }],
  },
  // No quiet hours and no waiting while the flows run; the quiet hours come later.
  'contact.json': { quietStart: 0, quietEnd: 0, waitForReply: false, minGapMinutes: 0, maxPerDay: 10 },
}
const { root, mock, dsh, url, logs: readLogs } = await bootDsh({ name: 'e2e-contact', kinFiles, extraRows: `- insert:
    - id: kin-e2e-contact
      name: ${JSON.stringify(join(kinDir, 'dev/scenario-contact.mjs'))}
      config:
        out: ${JSON.stringify(out)}
        dataDir: ${JSON.stringify(dataDir)}
` })

// The page's routes, called the way the browser calls them: the login link mints a cookie.
const login = await fetch(await url, { redirect: 'manual' })
const cookie = login.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ')
const api = (route, body) => fetch(new URL('api/kin/' + route, url_), {
  headers: { cookie, 'content-type': 'application/json' }, ...(body ? { method: 'POST', body: JSON.stringify(body) } : {}),
}).then((r) => r.json())
const url_ = await url
// Her desktop conversation, as the page opens it.
await api('chat', {})

const deadline = Date.now() + 1_200_000
let quietSet = false
while (!existsSync(out) && Date.now() < deadline && dsh.exitCode === null) {
  if (!quietSet && existsSync(out + '.phase')) {
    const hour = Number(new Intl.DateTimeFormat('en-GB', { hour: 'numeric', hourCycle: 'h23' }).format(new Date()))
    await api('contact', { quietStart: hour, quietEnd: (hour + 1) % 24 })
    writeFileSync(out + '.quiet', '')
    quietSet = true
  }
  await new Promise((r) => setTimeout(r, 500))
}

const pages = {}
if (existsSync(out)) {
  for (const end = Date.now() + 75_000; Date.now() < end; await new Promise((r) => setTimeout(r, 1000))) {
    pages.contact = await api('contact')
    if (pages.contact.status?.[OWNER]?.reason === 'quiet-hours') break
  }
  pages.people = await api('people')
  // The sticker in her desktop chat, as the page loads it.
  const shown = JSON.parse(readFileSync(out, 'utf8')).appSticker?.media
  const media = shown && await fetch(new URL('api/kin/media?file=' + encodeURIComponent(shown.file), url_), { headers: { cookie } })
  pages.media = media && { status: media.status, type: media.headers.get('content-type') }
}
dsh.kill('SIGTERM')
await Promise.race([new Promise((done) => dsh.once('exit', done)), new Promise((done) => setTimeout(done, 5000))])
if (dsh.exitCode === null && dsh.signalCode === null) dsh.kill('SIGKILL')
await mock.close()
const logs = readLogs()
writeFileSync(join(root, 'dsh.log'), logs)
const kinLog = existsSync(out + '.log') ? readFileSync(out + '.log', 'utf8') : ''
if (!existsSync(out)) {
  console.error('No report. DSH exited with', dsh.exitCode, '\nKIN log tail:\n' + kinLog.split('\n').slice(-40).join('\n'))
  process.exit(1)
}
const report = JSON.parse(readFileSync(out, 'utf8'))
if (report.error) {
  console.error('Scenario failed:\n' + report.error + '\n\nKIN log tail:\n' + kinLog.split('\n').slice(-30).join('\n'))
  process.exit(1)
}

const unknownPart = report.mingUnknown?.id + ':0'
const checks = {
  'relay thought paragraphs never reach WeChat or cross-conversation history': report.mingSends.some((s) => s.text === '收到啦～')
    && !JSON.stringify([report.mingSends, report.groupMentioned.sent, report.looks]).includes('Private test reasoning'),
  '小明 twice within 20 s is one message to her, named': report.ming?.turns.length === 1 && report.ming.turns[0].text === '小明：在吗\n小明：我今天看到星星了',
  'her reply to 小明 went back on WeChat, typing while she wrote': report.ming !== null && report.typing.some((t) => t.on) && report.typing.at(-1)?.on === false,
  'plain group talk is heard, not answered': report.groupQuiet.turns.length === 0 && report.groupQuiet.sent === 0,
  'an @ in the group is answered, with what was said before it': report.groupMentioned?.turns.length === 1
    && report.groupMentioned.turns[0].text.includes('小明：今晚吃什么') && report.groupMentioned.turns[0].text.includes('小光：@KIN 你觉得呢'),
  'a relay reaches 小明, and his conversation knows': Boolean(report.relay) && report.relayTold?.text.includes('晚上一起吃饭'),
  'she reached the owner on her own, in her desktop chat': Boolean(report.ownerContact),
  'Core settled her message to the owner as delivered': Boolean(report.contactCore?.contact_sent_at),
  'a send of unknown outcome waits under its own id': report.mingUnknown?.state === 'unknown',
  'it is asked about by that id, then the rest goes, nothing twice': report.reconciled?.state === 'accepted' && report.checked.includes(unknownPart)
    && report.mingSends.filter((s) => s.id === unknownPart).length === 1 && report.mingSends.some((s) => s.text === '今天天气挺好的'),
  'never drafted again while it was unknown': report.drafts.filter((d) => d.who === '小明').length === 1,
  'her intimate side is the owner\'s alone': report.ownerView === true && report.mingView === false
    && report.drafts.some((d) => d.who === '小光' && d.intimate) && report.drafts.every((d) => d.who === '小光' || !d.intimate),
  'quiet hours hold a relay until they end': Boolean(report.quietRelay?.notBefore) && report.quietSent === false,
  'quiet hours hold her reaching out, and her page says so': pages.contact?.status?.[OWNER]?.reason === 'quiet-hours',
  'her page knows who is who': pages.people?.people?.length === 2 && pages.people.groups?.length === 1,
  'a picture from the owner is shown to her, a file asked for goes out named': report.ownerWechat?.turn.images === 1
    && report.ownerWechat.turn.text.includes('[图片] ') && report.ownerWechat.file.named === true,
  'a sticker she picks by meaning goes out on WeChat as an image': report.sticker?.file.endsWith('cat.png') === true,
  'her voice goes through the speech worker and out as a voice part': report.voice?.file.endsWith('.ogg') === true
    && report.spoken?.some((s) => s.text === '晚安呀，早点睡' && s.style === '轻声'),
  'from the owner\'s chat she reads 小明\'s conversation: who said what, and when': report.looks?.[0].includes('和小明（微信私聊）')
    && report.looks[0].includes('小明：在吗') && report.looks[0].includes('你：') && /刚刚|分钟前/.test(report.looks[0]),
  'from 小明\'s chat she reads the owner\'s too, who said what': report.looks?.[1].includes('和小光（桌面聊天）')
    && report.looks[1].includes('小光：今天下班好累') && report.looks[1].includes('你：'),
  'in her desktop chat the sticker shows, from her media route': report.appSticker?.media.kind === 'image' && pages.media?.status === 200
    && pages.media.type === 'image/png',
}
if (Object.values(checks).some(ok => !ok)) console.log('cross-session readings:', JSON.stringify(report.looks))
for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
console.log(`\ndrafts: ${JSON.stringify(report.drafts)}\nsent: ${JSON.stringify(report.mingSends?.map((s) => s.text))} · report: ${out}`)
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
