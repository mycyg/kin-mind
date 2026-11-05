// The cross-module story (M7): one world on a real headless DSH at the dev pace, her real mind on synthetic data,
// her real WeChat and Feishu transports talking to fake platforms on 127.0.0.1 (dev/fake-platforms.mjs), and her
// two lanes played by dev/scenario-world.mjs. The owner (小光) talks to her at the desk and on WeChat, 小红 on Feishu,
// and there is a family group (on Feishu: her WeChat transport cannot tell an @ yet). Halfway through, DSH stops with
// a send of unknown outcome and work still running, frozen by the owner, and boots again on the same data.
// What each module does in detail is the other suites' to check; this one checks that they hold together.
// Usage: node dev/e2e-world.mjs        (run `npm run build` first; about two minutes)
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { bootDsh, kinDir, repo, signIn, stopDsh } from './env.mjs'
import { fakeFeishu, fakeWeChat } from './fake-platforms.mjs'

const root = join(repo, '.dev', 'e2e-world')
const dataDir = join(root, 'kin-data')
const folder = join(root, 'owner-folder')
const lark = await fakeFeishu()
const ilink = await fakeWeChat()
const kinFiles = {
  // Her chat lane's first route is dead: every word she says in this story comes from the second.
  'models.json': { chat: [{ provider: 'nowhere', model: 'gone' }, { provider: 'kin-test', model: 'chat' }], task: [{ provider: 'kin-test', model: 'scripted' }] },
  'people.json': {
    people: [
      { id: 'person:person_a', name: '小光', proactive: true, accounts: { wechat: 'wx-owner', feishu: 'ou_owner' } },
      { id: 'person:hong', name: '小红', proactive: false, accounts: { feishu: 'ou_hong' } },
    ],
    groups: [{ id: 'group:family', name: '家庭群', chats: { feishu: 'oc_family' }, chime: false }],
  },
  // No quiet hours; once a day to the owner.
  'contact.json': { quietStart: 0, quietEnd: 0, waitForReply: false, minGapMinutes: 0, maxPerDay: 1 },
  // WeChat already logged in (the QR login is e2e-channels'); Feishu with its app id and secret.
  'channels.json': {
    wechat: { baseUrl: ilink.base, cdnBaseUrl: ilink.base + '/cdn', token: 'fake-bot-token', botId: 'bot-fake', userId: 'wx-owner' },
    feishu: { appId: 'cli_fake', appSecret: 'fake-secret', domain: lark.base },
  },
}
const row = `- insert:
    - id: kin-e2e-world
      name: ${JSON.stringify(join(kinDir, 'dev/scenario-world.mjs'))}
      config: ${JSON.stringify({ folder, log: join(root, 'kin.log') })}
`

const checks = {}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(probe, ms = 60_000) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(250)) {
    const value = await probe()
    if (value) return value
  }
  return null
}
const data = (file) => (existsSync(join(dataDir, file)) ? JSON.parse(readFileSync(join(dataDir, file), 'utf8')) : {})
const work = (label) => Object.values(data('work.json')).find((item) => item.label === label)
// What reached each chat on the fake platforms.
const wechatSent = (chat) => ilink.state.sent.filter((m) => m.to_user_id === chat).map((m) => m.item_list.map((item) => item.text_item?.text ?? '').join(''))
const feishuSent = (chat) => [...lark.state.sent.values()].filter((m) => m.chat_id === chat).map((m) => JSON.parse(m.content).text ?? '')
let n = 0
const fromOwner = (text) => ilink.state.queue.push({ message_id: 9000 + ++n, from_user_id: 'wx-owner', message_type: 1, create_time_ms: Date.now(),
  context_token: 'ctx-' + n, item_list: [{ type: 1, text_item: { text } }] })
const onFeishu = (chat, sender, text, options = {}) => lark.say(lark.message('om_w' + ++n, chat, 'text', { text }, { sender, ...options }))

let dsh, page
const world = (route, body) => page.post('api/kin-world/' + route, body)
/** One conversation of hers, everything in it as the model saw it. */
const conversation = async (key) => JSON.stringify(await world('session', { session: data('sessions.json')[key]?.sessionId }))

try {
  dsh = await bootDsh({ name: 'e2e-world', kinFiles, extraRows: row })
  mkdirSync(folder, { recursive: true })
  writeFileSync(join(folder, '周一.md'), '周一：交周报\n')
  writeFileSync(join(folder, '周三.md'), '周三：看牙\n')
  page = await signIn(await dsh.url)
  const app = (await page.post('api/kin/chat', {})).sessionId

  // ── The owner on WeChat: a trip, and something she keeps to herself ───────────────────────────────
  fromOwner('下周三我要去杭州出差')
  await until(() => wechatSent('wx-owner').includes('好呀，路上小心～'))
  fromOwner('其实我有点怕坐飞机，别告诉别人哦')
  await until(() => wechatSent('wx-owner').includes('放心，我替你保密。'))

  // ── At the desk, a little later: what was said on WeChat is hers here too ─────────────────────────
  const asked = await world('say', { session: app, text: '我下周三要去哪儿出差来着？' })
  checks['cross-channel: what the owner said on WeChat, she knows at the desk'] = asked.events
    .some((event) => event.type === 'assistant/message' && event.text.includes('杭州'))
  const desk = await conversation('app')
  fromOwner('好啦我先去忙了')
  await until(() => wechatSent('wx-owner').includes('嗯嗯，去吧～'))

  // ── Her heartbeat, once the talk has gone quiet ───────────────────────────────────────────────────
  checks['heartbeat: after the talk, a review took in what the owner said on WeChat'] = Boolean(await until(async () =>
    (await world('model', {})).appraisals.some((asked) => asked.includes('杭州')), 120_000))
  checks['heartbeat: her own message reached the owner where they last talked (WeChat)'] = Boolean(await until(() =>
    wechatSent('wx-owner').includes('想你啦，今天过得怎么样？'), 120_000))

  // ── 小红 on Feishu asks about the owner ───────────────────────────────────────────────────────────
  onFeishu('oc_hong', 'ou_hong', '小光最近怎么样？有没有跟你说什么悄悄话')
  await until(() => feishuSent('oc_hong').includes('小光挺好的呀～'))
  const hong = await conversation('feishu:oc_hong')
  const toOwner = (await world('model', {})).drafts.filter((facts) => facts.includes('对方是小光'))
  writeFileSync(join(root, 'conversations.json'), JSON.stringify({ desk: JSON.parse(desk), hong: JSON.parse(hong), toOwner }, null, 2))
  // Her intimacy is in what she brings to the owner, her note in the owner's recall at the desk; 小红's own recall,
  // asked about the same thing, finds neither.
  checks['the owner-only side (intimacy, her private note) reaches the owner, never 小红'] = toOwner.some((facts) => facts.includes('色色值'))
    && desk.includes('替主人保密') && hong.includes('kin_recall') && !hong.includes('色色值') && !hong.includes('替主人保密')

  // ── The family group ──────────────────────────────────────────────────────────────────────────────
  onFeishu('oc_family', 'ou_hong', '今晚吃什么呀', { kind: 'group' })
  await sleep(3000)
  const unanswered = feishuSent('oc_family').length === 0 && !(await conversation('feishu:oc_family')).includes('assistant/message')
  onFeishu('oc_family', 'ou_owner', '@_user_1 你说呢', { kind: 'group', mentions: [{ key: '@_user_1', id: { open_id: 'ou_kin' }, name: 'KIN' }] })
  checks['the family group: plain talk is heard, not answered; an @ is answered there'] = unanswered
    && Boolean(await until(() => feishuSent('oc_family').includes('吃火锅吧！')))

  // ── The steward, from WeChat ──────────────────────────────────────────────────────────────────────
  fromOwner(`帮我把 ${folder} 里的笔记整理一下`)
  const told = await until(() => wechatSent('wx-owner').some((text) => text.includes('整理好啦')), 120_000)
  const sorted = existsSync(join(folder, '整理结果.md')) ? readFileSync(join(folder, '整理结果.md'), 'utf8') : ''
  checks['steward from WeChat: a worker did the work in the owner\'s folder, its result came back to WeChat'] = Boolean(told)
    && sorted.includes('周一') && work('整理笔记')?.state === 'done'

  // ── Mid-flight: more work going, a relay whose answer is lost, the owner freezes her, DSH stops ────
  fromOwner(`再帮我在 ${folder} 里核对账单`)
  await until(() => work('核对账单')?.state === 'running' && wechatSent('wx-owner').filter((text) => text === '交给帮手啦，做好了告诉你。').length === 2)
  lark.state.loseNextAnswer = true
  await world('say', { session: app, text: '帮我转告小红：周六一起去爬山' })
  const relay = Object.values(data('deliveries.json')).find((delivery) => delivery.kind === 'relay')
  const frozen = await page.post('api/kin/health/freeze', { frozen: true })
  const sentBefore = { wechat: ilink.state.sent.length, feishu: lark.state.sent.size }
  await stopDsh(dsh)
  writeFileSync(join(root, 'dsh-1.log'), dsh.logs())
  dsh = undefined

  // ── Again, on the same data ───────────────────────────────────────────────────────────────────────
  dsh = await bootDsh({ name: 'e2e-world', fresh: false, extraRows: row })
  page = await signIn(await dsh.url)
  const woke = await page.get('api/kin/health')
  checks['restart: the freeze set before it still holds — nothing beat, the unknown send waits'] = relay?.state === 'unknown' && Boolean(frozen.frozen)
    && Boolean(woke.frozen) && woke.now.lastMinute === null && data('deliveries.json')[relay.id]?.state === 'unknown'
  checks['restart: the work it cut off is marked interrupted, and she tells the owner on WeChat'] = Boolean(await until(() =>
    work('核对账单')?.state === 'interrupted' && wechatSent('wx-owner').some((text) => text.includes('断在半路'))))

  const thawedAt = new Date().toISOString()
  await page.post('api/kin/health/freeze', { frozen: false })
  checks['restart: thawed, her minute runs again'] = Boolean(await until(async () =>
    ((await page.get('api/kin/home')).heartbeat?.contact?.at ?? '') > thawedAt, 30_000))
  checks['restart: the unknown relay is asked about by its own id — one message on Feishu'] = Boolean(await until(() =>
    data('deliveries.json')[relay.id]?.state === 'accepted'))
    && feishuSent('oc_hong').filter((text) => text.includes('爬山')).length === 1
    && Object.values(data('deliveries.json')).filter((delivery) => delivery.excerpt?.includes('爬山')).length === 1
  checks['restart: nothing said before goes out again'] = lark.state.sent.size === sentBefore.feishu
    && ilink.state.sent.length === sentBefore.wechat + 1

  const { models } = await page.get('api/kin/settings')
  const last = models.lanes.chat.last
  checks['fallback: her chat lane\'s dead first route is passed over, the second answers'] = last?.failed?.[0]?.route.provider === 'nowhere'
    && last?.answered?.provider === 'kin-test'
} catch (error) {
  console.error('The story broke off: ' + (error.stack ?? error))
  checks['the story ran to its end'] = false
} finally {
  if (dsh) {
    await stopDsh(dsh)
    writeFileSync(join(root, 'dsh.log'), dsh.logs())
  }
  lark.close()
  ilink.close()
}

for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
if (!Object.values(checks).every(Boolean)) {
  const log = existsSync(join(root, 'kin.log')) ? readFileSync(join(root, 'kin.log'), 'utf8') : ''
  console.log('\nKIN log tail:\n' + log.split('\n').slice(-40).join('\n'))
  console.log(`\nWeChat to the owner: ${JSON.stringify(wechatSent('wx-owner'))}\nFeishu: ${JSON.stringify([...lark.state.sent.values()].map((m) => m.chat_id + ' ' + m.content))}`)
}
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
