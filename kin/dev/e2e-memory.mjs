// Who said what, to whom and when (the owner's old bug: talking with several people on Feishu at once, what A said
// was remembered as said in the chat with B). The owner says something at the desk; 小美 and 小红 write in their
// Feishu private chats at the same moment, interleaved, with chatter in the family group. What Core keeps, what her
// review is told and what she recalls in each chat must keep each line with its speaker, its conversation and its
// time; a correction the owner makes on her page stays with the memory. Real headless DSH at the dev pace, her real
// mind on synthetic data, her real Feishu transport against a fake Feishu (dev/fake-platforms.mjs), her lanes
// played by dev/scenario-memory.mjs. Usage: node dev/e2e-memory.mjs   (run `npm run build` first; about a minute)
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { bootDsh, kinDir, repo, signIn, stopDsh } from './env.mjs'
import { fakeFeishu } from './fake-platforms.mjs'

const root = join(repo, '.dev', 'e2e-memory')
const lark = await fakeFeishu()
const OWNER = 'person:person_a', MEI = 'person:mei', HONG = 'person:hong', FAMILY = 'group:family'
const kinFiles = {
  'models.json': { chat: [{ provider: 'kin-test', model: 'chat' }], task: [{ provider: 'kin-test', model: 'scripted' }] },
  'people.json': {
    people: [
      { id: OWNER, name: '小光', proactive: false, accounts: { feishu: 'ou_owner' } },
      { id: MEI, name: '小美', proactive: false, accounts: { feishu: 'ou_mei' } },
      { id: HONG, name: '小红', proactive: false, accounts: { feishu: 'ou_hong' } },
    ],
    groups: [{ id: FAMILY, name: '家庭群', chats: { feishu: 'oc_family' }, chime: false }],
  },
  'channels.json': { feishu: { appId: 'cli_fake', appSecret: 'fake-secret', domain: lark.base } },
}
const row = `- insert:
    - id: kin-e2e-memory
      name: ${JSON.stringify(join(kinDir, 'dev/scenario-memory.mjs'))}
      config: ${JSON.stringify({ log: join(root, 'kin.log') })}
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
let n = 0
const onFeishu = (chat, sender, text, kind = 'p2p') => lark.say(lark.message('om_m' + ++n, chat, 'text', { text }, { sender, kind }))
const sent = (chat) => [...lark.state.sent.values()].filter((m) => m.chat_id === chat).map((m) => JSON.parse(m.content).text ?? '')
/** A local stamp as her recall writes one (render.ts `stamp`): "2026-11-05 周四 14:20". */
const STAMP = /\d{4}-\d\d-\d\d 周[日一二三四五六] \d\d:\d\d/

let dsh, page
try {
  dsh = await bootDsh({ name: 'e2e-memory', kinFiles, extraRows: row })
  page = await signIn(await dsh.url)
  const data = (file) => JSON.parse(readFileSync(join(dsh.dataDir, file), 'utf8'))
  await until(() => lark.state.sockets.length)
  const app = (await page.post('api/kin/chat', {})).sessionId

  // ── The owner at the desk; 小美 and 小红 on Feishu at the same moment, and the family group ──────────
  await page.post('api/kin-memory/say', { session: app, text: '我12月底回中国' })
  onFeishu('oc_mei', 'ou_mei', '下周三去杭州出差')
  onFeishu('oc_hong', 'ou_hong', '其实我有点怕坐飞机，别告诉别人哦')
  onFeishu('oc_family', 'ou_hong', '今晚吃什么呀', 'group')
  onFeishu('oc_mei', 'ou_mei', '周三早上的高铁')
  onFeishu('oc_family', 'ou_mei', '吃火锅吧', 'group')
  await until(() => sent('oc_mei').length && sent('oc_hong').length)

  // ── What Core keeps: each line with its own speaker and conversation, and the speaker as its actor ─────
  // Read beside her mind, which may be writing: wait for its lock as Core does.
  const db = new DatabaseSync(join(dsh.dataDir, 'db', 'memory.sqlite3'), { readOnly: true, timeout: 30_000 })
  // Each message as Core's runtime ledger keeps it, under the id its event node names (`runtime_…`).
  const expected = {
    '我12月底回中国': [OWNER, OWNER, 'app'],
    '小美：下周三去杭州出差': [MEI, MEI, 'feishu:oc_mei'],
    '小美：周三早上的高铁': [MEI, MEI, 'feishu:oc_mei'],
    '小红：其实我有点怕坐飞机，别告诉别人哦': [HONG, HONG, 'feishu:oc_hong'],
    '小红：今晚吃什么呀': [HONG, FAMILY, 'feishu:oc_family'],
    '小美：吃火锅吧': [MEI, FAMILY, 'feishu:oc_family'],
  }
  // Private-chat replies do not acknowledge silent group arrivals. Wait for every source and its graph node.
  let said = {}, actors = {}
  const complete = await until(() => {
    said = Object.fromEntries(db.prepare("SELECT id,data FROM mind_runtime_events WHERE kind='owner-message'").all()
    .map(({ id, data }) => ({ ...JSON.parse(data), row: id })).map((event) => [event.text, event]))
    actors = Object.fromEntries(db.prepare(`SELECT n.data AS node, a.data AS actor FROM mind_graph_nodes n
    JOIN mind_graph_edges e ON e.object=n.id AND e.predicate='participates' AND e.state='active'
    JOIN mind_graph_nodes a ON a.id=e.subject WHERE json_extract(n.data,'$.runtime_event_id') IS NOT NULL`).all()
    .map((found) => [JSON.parse(found.node).runtime_event_id, JSON.parse(found.actor).title]))
    return Object.keys(expected).every(text => said[text] && actors[said[text].row])
  })
  if (!complete) console.log('incomplete memory arrivals:', Object.keys(expected).filter(text => !said[text] || !actors[said[text].row]))

  db.close()
  checks['Core keeps every line with its own speaker and conversation, the speaker as its actor'] = Boolean(complete) && Object.entries(expected)
    .every(([text, [speaker, recipient, key]]) => {
      const event = said[text]
      return event?.speaker_person_id === speaker && event.recipient_person_id === recipient && event.conversation_key === key
        && actors[event.row] === (speaker === OWNER ? 'owner' : speaker)
    })

  // ── Her review of 小美's chat: in 小美's view, told who 小美 is and when she said it ──────────────────
  checks['her review of 小美\'s chat knows who said it and when'] = Boolean(await until(async () =>
    (await page.post('api/kin-memory/reviews', {})).find((review) => review.includes('person:mei：小美')
      && review.includes('下周三去杭州出差') && /speaker_person_id\\*"\s*:\s*\\*"person:mei/.test(review) && review.includes('occurred_at')), 90_000))

  // ── What she recalls in 小红's chat and in 小美's: each line names who said it, to whom, and when ─────
  const recalled = async (chat, sender) => {
    const before = sent(chat).length
    onFeishu(chat, sender, '你还记得大家最近说的事吗')
    await until(() => sent(chat).length > before && sent(chat).at(-1) === '嗯，我想起来了。')
    const session = data('sessions.json')['feishu:' + chat].sessionId
    const events = await page.post('api/kin-memory/session', { session })
    // Her kin_recall answer: the last thing in the conversation that says what she recalled.
    const strings = (value) => (typeof value === 'string' ? [value] : value && typeof value === 'object' ? Object.values(value).flatMap(strings) : [])
    const answer = events.flatMap((event) => strings(JSON.parse(event))).filter((text) => text.includes('想起来的事')).at(-1) ?? ''
    return answer.split('\n').filter((line) => line.startsWith('- '))
  }
  const line = (lines, words) => lines.find((entry) => entry.includes(words)) ?? ''
  const hong = await recalled('oc_hong', 'ou_hong')
  const mei = await recalled('oc_mei', 'ou_mei')
  checks['小红\'s recall: 小美\'s trip is 小美\'s, the owner\'s return is the owner\'s, never 小红\'s'] =
    line(hong, '杭州').includes('小美私下跟你说') && line(hong, '回中国').includes('主人私下跟你说')
    && !hong.some((entry) => entry.includes('杭州') && entry.includes('小红私下'))
  checks['小美\'s recall: what 小红 said in confidence shows as 小红\'s, said in private'] = line(mei, '怕坐飞机').includes('小红私下跟你说')
  checks['every recalled line says when it was said, "下周三去杭州出差" with its date and weekday'] = hong.length > 0 && mei.length > 0
    && [...hong, ...mei].every((entry) => STAMP.test(entry)) && STAMP.test(line(hong, '下周三去杭州出差'))

  // ── The owner's 记错了 on her page stays with the memory ──────────────────────────────────────────────
  const trip = (await page.post('api/kin/recall', { query: '杭州出差' })).memories?.find((memory) => memory.text.includes('杭州'))
  const corrected = trip && await page.post('api/kin/correct', { id: trip.id, text: '她说的是下周四', memory: trip.text })
  const after = (await page.post('api/kin/recall', { query: '杭州出差' })).memories ?? []
  checks['the page shows who and when, and the owner\'s correction stays with that memory'] = trip?.who === '小美私下跟她说'
    && Boolean(trip.at) && Boolean(corrected?.corrected) && after.some((memory) => memory.id === trip.id && memory.text.includes('更正：她说的是下周四'))
    && after.some((memory) => memory.who === '你私下跟她说' && memory.text.includes('她说的是下周四'))
} catch (error) {
  console.error('The story broke off: ' + (error.stack ?? error))
  checks['the story ran to its end'] = false
} finally {
  if (dsh) await stopDsh(dsh)
  lark.close()
}

for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
if (!Object.values(checks).every(Boolean)) {
  const log = existsSync(join(root, 'kin.log')) ? readFileSync(join(root, 'kin.log'), 'utf8') : ''
  console.log('\nKIN log tail:\n' + log.split('\n').slice(-30).join('\n'))
}
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
