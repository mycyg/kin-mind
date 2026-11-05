#!/usr/bin/env node
/**
 * Moves an old KIN home into dsh-kin's data directory (PARITY 6). Dry run by default: what goes where,
 * what stays behind and why, what the owner sets up again on her page. `--apply` writes.
 * - The old home is only read. A file or store of the new data directory that would change is first
 *   moved into `before-migrate-<time>/` there; a second run leaves alone what the first one wrote.
 * - Her mind (Core's store) is copied whole, under the scope the old host kept it in.
 * - A contact Core still holds as pending gets a delivery under its own id, so the new ledger settles it:
 *   never sent, it is cancelled before sending (her wish stays); begun, it stays unconfirmed and is never sent again.
 * - No keychain, login token or model key is carried; those are set up again on her page.
 * Old KIN layout: legacy/host/settings.mjs, legacy/feishu/settings.mjs, core/adapters/project-paths.mjs.
 */
import { chmodSync, constants, copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve, sep } from 'node:path'
import { parseArgs } from 'node:util'
import { contactHistory } from './migrate-contact.mjs'

const USAGE = `用法：node scripts/migrate.mjs --from <旧 KIN 目录> --to <新数据目录> [--store <旧心智库>] [--apply]

  --from   放着旧 KIN 的 host/ 和 feishu/ 的目录（各自带 state/）
  --to     新 KIN 的数据目录（插件设置里的 dataDir，默认 ~/.kin）
  --store  旧心智库（MemoryPalace）；不填就用 host/mind-config.json 里的 root
  --owner-id  旧库中主人的 person 编号；不同于清洁源码默认编号时一起迁移引用
  --apply  真的搬；不加只预演，什么都不写

先退出 DSH，停掉旧 KIN 的所有服务（宿主、飞书监听、记忆服务）。旧目录只读不改。`

let args
try {
  ({ values: args } = parseArgs({
    options: { from: { type: 'string' }, to: { type: 'string' }, store: { type: 'string' }, 'owner-id': { type: 'string' }, apply: { type: 'boolean', default: false }, help: { type: 'boolean', short: 'h' } },
  }))
} catch (error) {
  console.error(error.message + '\n')
  args = {}
}
if (args.help || !args.from || !args.to) {
  console.log(USAGE)
  process.exit(args.help ? 0 : 2)
}

const OWNER = 'person:person_a'
const oldOwner = args['owner-id'] ?? OWNER
if (!/^person:[A-Za-z0-9_-]{1,40}$/.test(oldOwner)) throw new Error('Invalid owner-id')
const identity = (id) => id === oldOwner ? OWNER : id
/** Batch item states that mean a send began (core/adapters/contact-batch.mjs BEGUN). */
const BEGUN = ['pending', 'accepted', 'unconfirmed']
/** Reply groups that are over (core/adapters/transport-manifest.mjs TERMINAL_GROUP_STATES). */
const TERMINAL = ['accepted', 'retired', 'partial', 'undeliverable']
/** The old host's own section of the persona: how its phone session used tools the new KIN does not have (sync-persona.mjs). */
const OLD_SECTION = /^# 手机会话的行动与交付\n[\s\S]*?(?=^# |(?![\s\S]))/m

const apply = args.apply
const from = resolve(args.from)
const to = resolve(args.to)
const hostDir = join(from, 'host')
const hostState = join(hostDir, 'state')
const feishuState = join(from, 'feishu', 'state')
const aside = join(to, 'before-migrate-' + new Date().toISOString().replace(/[:.]/g, '-'))
let setAsideAny = false
const left = []
const redo = []

const inside = (path, folder) => path === folder || path.startsWith(folder + sep)
const text = (value) => JSON.stringify(value, null, 2) + '\n'
const say = (line) => console.log('  · ' + line)

function json(file) {
  if (!existsSync(file)) return undefined
  try {
    return JSON.parse(readFileSync(file, 'utf8'))
  } catch {
    throw new Error('读不懂 ' + file)
  }
}

/** Moves a file or folder of the new data directory aside before it is replaced. */
function setAside(name) {
  mkdirSync(dirname(join(aside, name)), { recursive: true })
  renameSync(join(to, name), join(aside, name))
  setAsideAny = true
}

/** One file of the new data directory: written (when applying) unless it already says this; the words say what happens. */
function put(name, content, mode) {
  const file = join(to, name)
  const old = existsSync(file) ? readFileSync(file, 'utf8') : undefined
  if (old === content) return '（已经是这样，不动）'
  if (apply) {
    if (old !== undefined) setAside(name)
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(file, content, { mode })
  }
  return old === undefined ? '' : '（现有的先挪进备份）'
}

const alive = (pid) => {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error.code === 'EPERM'
  }
}

try {
  main()
} catch (error) {
  console.error('\n没搬完：' + error.message)
  process.exit(1)
}

function main() {
  const mind = json(join(hostDir, 'mind-config.json'))
  if (!mind) throw new Error(`没找到 ${join(hostDir, 'mind-config.json')}：--from 要指向放着旧 host/ 和 feishu/ 的目录`)
  const store = args.store ? resolve(args.store) : mind.root && resolve(hostDir, mind.root)
  if (!store || !existsSync(join(store, 'memory.sqlite3'))) throw new Error(`没找到旧心智库 ${store ?? ''}（memory.sqlite3）：用 --store 指一下`)
  if (!mind.scope || typeof mind.scope !== 'object') throw new Error('旧的 host/mind-config.json 里没有 scope，不知道她的记忆记在哪个作用域下')
  if (inside(to, from) || inside(to, store) || inside(store, to)) throw new Error('新数据目录要和旧 KIN、旧心智库分开')
  const pidFile = join(hostState, 'bridge.pid')
  const pid = existsSync(pidFile) ? Number(readFileSync(pidFile, 'utf8').trim()) : 0
  if (pid > 1 && alive(pid)) {
    if (apply) throw new Error(`旧 KIN 还在运行（进程 ${pid}）：先停掉它的所有服务`)
    console.log(`注意：旧 KIN 好像还在运行（进程 ${pid}），--apply 之前要先停掉。\n`)
  }

  console.log(`旧 KIN：${from}\n旧心智库：${store}\n新数据目录：${to}\n\n${apply ? '搬过去：' : '会搬过去（预演，什么都不写）：'}`)
  if (apply) mkdirSync(to, { recursive: true })

  // Her mind, whole: memories, diary, dreams, letters, wishes, plans, vectors, the prompts she edited.
  const db = join(to, 'db')
  const ours = json(join(to, 'migrated.json'))?.store === store && existsSync(db)
  if (ours) say('心智库：db/ 已经是从这里搬来的，不再动')
  else {
    say(`心智库 → db/（整份复制：记忆、日记、梦、信、心愿、计划、向量、她改过的提示词；旧的不动）${existsSync(db) ? '（现有的 db/ 先挪进备份）' : ''}`)
    if (apply) {
      if (existsSync(db)) setAside('db')
      cpSync(store, db, {
        recursive: true, dereference: true, errorOnExist: true, force: false, mode: constants.COPYFILE_FICLONE,
        filter: (source) => !source.endsWith('-shm'),
      })
      chmodSync(db, 0o700)
      remapOwner(db)
      put('migrated.json', text({ from, store }))
    }
  }
  // Core keys everything by its scope: without the old one the new KIN finds an empty mind.
  const config = json(join(to, 'mind-config.json')) ?? {}
  const retained = Object.fromEntries(['exploration_decisions_enabled', 'exploration_budget_seconds', 'exploration_max_running', 'creation_static_verification', 'creation_verifier', 'background_model_limit'].filter((key) => mind[key] !== undefined).map((key) => [key, mind[key]]))
  say(`她的记忆所在的作用域 ${JSON.stringify(mind.scope)} → mind-config.json${put('mind-config.json', text({ ...retained, ...config, scope: mind.scope }))}`)
  if (mind.review_paused) left.push('旧 KIN 的后台评估是暂停的（review_paused）；新 KIN 照常想事情，要停就在「她的状况」里冻结')

  const persona = personaOf(store)
  if (persona) say(`人设 → persona.md（${persona.source}${persona.trimmed ? '；旧手机会话那一节讲的是旧工具，去掉了' : ''}）${put('persona.md', persona.text)}`)
  else left.push('人设：旧心智库里没找到（prompts/persona.json、persona-policy.json），新 KIN 先用默认的')

  // Who she knows: Core knows them by the same ids, so each person's memories stay theirs.
  const companions = json(join(feishuState, 'companions.json'))
  const binding = json(join(feishuState, 'binding.json'))
  // Of the old WeChat login only the id of the owner who scanned is read; the login itself is not carried.
  const wechatOwner = json(join(hostState, 'token.json'))?.userId
  const bound = companions?.enabled === true
  const people = Object.entries(companions?.people ?? {}).map(([oldId, person]) => {
    const id = identity(oldId)
    return {
      id, name: person.name ?? '', proactive: id === OWNER || person.proactive === true,
      accounts: bound && person.binding?.open_id ? { feishu: person.binding.open_id } : {},
    }
  })
  if (!people.some((person) => person.id === OWNER)) people.unshift({ id: OWNER, name: '', proactive: true, accounts: {} })
  const owner = people.find((person) => person.id === OWNER)
  owner.name ||= mind.owner_name ?? ''
  if (binding?.ownerId) owner.accounts.feishu ??= binding.ownerId
  if (wechatOwner) owner.accounts.wechat = wechatOwner
  const groups = Object.entries(companions?.groups ?? {}).map(([id, group]) => ({
    id, name: group.name ?? '', chats: bound && group.binding?.chat_id ? { feishu: group.binding.chat_id } : {}, chime: group.mention_only === false,
  }))
  const names = (entries) => entries.map((entry) => entry.name || entry.id).join('、')
  say(`她认识的人（${names(people)}${groups.length ? `；群 ${names(groups)}` : ''}）→ people.json${put('people.json', text({ people, groups }))}`)
  if (companions && !bound) left.push('旧的联系人目录没启用：除了主人，其他人和群只搬了名字，没搬飞书账号')

  // Familiar people stay familiar; accepted contacts still count toward today's limit.
  const history = contactHistory(ours || apply ? db : store, mind.scope, people, groups, oldOwner)
  for (const [id, activity] of Object.entries(history.activity)) {
    const legacyId = id === OWNER ? oldOwner : id
    const previous = json(join(feishuState, 'activity-' + legacyId.replace(':', '-') + '.json'))
    if (previous?.lastReplyAt) activity.answered = previous.lastReplyAt
  }
  say(`既往联系状态 ${Object.keys(history.activity).length} 位 → activity.json${put('activity.json', text({ ...history.activity, ...json(join(to, 'activity.json')) }))}`)
  say(`既往会话地址 → conversation-import.json${put('conversation-import.json', text(history.conversations))}`)

  const policy = json(join(hostState, 'proactive-policy.json')) ?? json(join(hostDir, 'proactive-policy.json'))
  if (policy) {
    const rules = Object.fromEntries(Object.entries({
      enabled: policy.enabled, maxPerDay: policy.checkinMaxPerDay, timeZone: policy.timeZone, quietStart: policy.quietStartHour, quietEnd: policy.quietEndHour,
      minGapMinutes: typeof policy.minimumGapHours === 'number' ? Math.round(policy.minimumGapHours * 60) : undefined,
      waitForReply: policy.waitForReply,
    }).filter(([, value]) => value !== undefined))
    const quiet = rules.quietStart === rules.quietEnd ? '没有安静时段' : `${rules.quietStart}–${rules.quietEnd} 点安静`
    say(`主动联系的规矩（${rules.enabled === false ? '关着' : '开着'}，${quiet}，间隔 ${rules.minGapMinutes ?? 60} 分钟${rules.waitForReply ? '，等回了再找' : ''}）→ contact.json${put('contact.json', text(rules))}`)
  } else left.push('主动联系的规矩：旧 KIN 没有 proactive-policy.json，新 KIN 用默认的（22–8 点安静，每天最多 3 条，间隔 60 分钟，等回了再找）')

  const tts = mind.tts
  if (tts?.python && tts.model && tts.ffmpeg) {
    const voice = Object.fromEntries(['python', 'model', 'ffmpeg', 'voice', 'referenceAudio', 'referenceText']
      .filter((key) => typeof tts[key] === 'string').map((key) => [key, tts[key]]))
    say(`她的声音（本机语音模型的路径）→ channels.json${put('channels.json', text({ ...json(join(to, 'channels.json')), voice }), 0o600)}`)
    redo.push('声音：路径照旧搬了，确认 Python、模型、ffmpeg 还在原处')
  }

  const memes = join(hostState, 'memes')
  if (existsSync(join(memes, 'index.json'))) {
    const ready = Object.values(json(join(memes, 'index.json')).items ?? {}).filter((item) => item.state === 'ready').length
    say(`表情库 ${ready} 张 → memes/${put('memes/index.json', readFileSync(join(memes, 'index.json'), 'utf8'))}`)
    if (apply && existsSync(join(memes, 'originals'))) {
      cpSync(join(memes, 'originals'), join(to, 'memes', 'originals'), { recursive: true, force: false, mode: constants.COPYFILE_FICLONE })
    }
  }

  // Contacts Core holds as pending would hold her reaching that person for good: each gets a delivery under its own id.
  const ledger = json(join(to, 'deliveries.json')) ?? {}
  const attempts = contacts(ours || apply ? db : store, mind.scope)
  const fresh = attempts.filter((attempt) => attempt.state === 'pending' && !ledger[attempt.id])
  if (fresh.length) {
    for (const attempt of fresh) ledger[attempt.id] = deliveryOf(attempt, batchOf(attempt.id))
    const begun = fresh.filter((attempt) => ledger[attempt.id].state === 'unknown').length
    say(`旧 KIN 写好、还没结清的主动消息 ${fresh.length} 条 → deliveries.json，按原来的编号：`
      + `还没发的 ${fresh.length - begun} 条记成没发出（心愿还在，她会重新想要不要说），已经开始发的 ${begun} 条记成待确认（不再发）${put('deliveries.json', text(ledger))}`)
  }
  const unconfirmed = attempts.filter((attempt) => attempt.state === 'unconfirmed').length
  if (unconfirmed) left.push(`旧 KIN 结果没确认的主动消息 ${unconfirmed} 条：照旧留在她的记忆里（她知道可能没送到），新 KIN 不核对，也不重发`)

  console.log('\n不搬：')
  for (const line of [
    ...left,
    '会话：旧的手机会话（Codex / ACP）和桌面聊天文件留在旧目录；聊过的话已经在她的记忆里',
    '旧的技术失败暂停：由新通道的实际结果重新判断；既往联系时间、地址和已送达次数保留',
    '模型路线和旧心智的密钥：新 KIN 的模型和密钥都在 DSH 里',
    '旧的后台任务、运行包、发布记录：换成了 DSH 自己的',
    '她做过的作品：留在旧目录，记忆里记的就是那里的路径',
  ]) say(line)

  const unsettled = [
    [count(join(feishuState, 'memory-events')), '件她说过、听过的事还没写进记忆（feishu/state/memory-events）'],
    [count(join(hostState, 'inject', 'pending')) + count(join(hostState, 'inject', 'processing')), '条主人发来的话还没交给她（host/state/inject）'],
    [count(join(hostState, 'reply-manifests'), (group) => !TERMINAL.includes(group.state)), '组她的回复没发完（host/state/reply-manifests）'],
    [count(join(feishuState, 'outbox'), (receipt) => receipt.state === 'unconfirmed' || (receipt.state === 'pending' && receipt.submissionStarted === true)),
      '条飞书发送结果未知（feishu/state/outbox）'],
    [count(join(hostState, 'outbox'), (record) => !record.deliveredAt), '条提醒或定时消息没送完（host/state/outbox）'],
  ].filter(([n]) => n > 0)
  if (unsettled.length) {
    console.log('\n旧 KIN 还没收尾的（不搬：新 KIN 不补发，也不重发）：')
    for (const [n, what] of unsettled) say(n + ' ' + what)
    say('最好再开旧 KIN 一会儿，等它在健康页上都收完尾，停下后重新预演')
  }

  const app = json(join(feishuState, 'config.json'))?.profiles?.kin?.accounts?.app
  if (existsSync(join(hostState, 'token.json'))) redo.unshift(`渠道 → 微信：填 iLink 的两个地址，扫码登录（旧的登录不搬）${wechatOwner ? '；主人的微信已经记在「她认识的人」里' : ''}`)
  if (app?.id) {
    redo.unshift(`渠道 → 飞书：填 App ID（旧 KIN 用的是 ${app.id}${app.tenant === 'lark' ? '，Lark' : ''}）、App Secret 和地址。`
      + 'Secret 在旧 KIN 的钥匙串或飞书桥的加密存储里，脚本不读；用同一个应用，「她认识的人」里的飞书账号才对得上')
  }
  redo.push('模型：把聊天、做事两条线从 DSH 里已配好的模型排好（旧的路线在 host/state/model-config.json，不搬）')
  if (persona) redo.push('人设：看一遍搬来的原文')
  console.log('\n搬完后在她的页面（KIN → 设置）上要做的：')
  redo.forEach((line, index) => console.log(`  ${index + 1}. ${line}`))

  console.log(apply
    ? `\n搬好了。${setAsideAny ? `被替换下来的在 ${aside}。` : ''}打开 DSH，进 KIN 页面看看她。`
    : '\n这是预演，什么都没写。看过没问题，加上 --apply 再跑一次。')
}

/** Her persona as the old host last gave it to her: the edited one, else its default, else the policy it grew from. */
function personaOf(store) {
  const edited = json(join(store, 'prompts', 'persona.json'))
  const initial = json(join(store, 'prompts', '.defaults', 'persona.json'))
  const policy = json(join(store, 'persona-policy.json'))
  const [source, words] = edited?.edited === true || (edited?.edited !== false && edited?.version > 1) ? ['她改过的 prompts/persona.json', edited.text]
    : initial?.text ? ['prompts/.defaults/persona.json', initial.text]
    : policy?.core ? ['persona-policy.json', `${policy.core}\n\n# SOUL\n\n${`${policy.voice ?? ''}\n${policy.maintenance ?? ''}`.replaceAll('声口', '说话风格')}`]
    : []
  if (typeof words !== 'string' || !words.trim()) return undefined
  const kept = words.replace(OLD_SECTION, '').trim()
  return { source, trimmed: kept !== words.trim(), text: `<!-- 从旧 KIN 搬来（${source}）。请在设置页的人设里看一遍。 -->\n${kept}\n` }
}

/** Core's contact attempts still open (pending, unconfirmed), read from a throwaway clone of the store's database. */
function contacts(storeDir, scope) {
  const temp = mkdtempSync(join(tmpdir(), 'kin-migrate-'))
  try {
    for (const name of ['memory.sqlite3', 'memory.sqlite3-wal']) {
      if (existsSync(join(storeDir, name))) copyFileSync(join(storeDir, name), join(temp, name), constants.COPYFILE_FICLONE)
    }
    const { DatabaseSync } = process.getBuiltinModule('node:sqlite')
    const database = new DatabaseSync(join(temp, 'memory.sqlite3'))
    // Core's scope key: the Scope model as pydantic writes it (eventmem/core/models.py).
    const key = JSON.stringify({ project: 'personal', persona: 'default', collection: 'default', world: 'real', ...scope })
    try {
      return database.prepare("SELECT state, data FROM mind_contacts WHERE scope = ? AND state IN ('pending', 'unconfirmed')").all(key)
        .map((row) => ({ ...JSON.parse(row.data), state: row.state }))
    } finally {
      database.close()
    }
  } catch (error) {
    left.push(`主动消息的记录：读不了旧心智库里的 mind_contacts（${error.message}），跳过`)
    return []
  } finally {
    rmSync(temp, { recursive: true, force: true })
  }
}

/** The old host's journal of one contact's sends: the owner's in feishu/state, another person's in their host/state/companions folder. */
function batchOf(id) {
  const companions = join(hostState, 'companions')
  const folders = [feishuState, hostState, ...(existsSync(companions) ? readdirSync(companions).map((name) => join(companions, name)) : [])]
  return folders.map((folder) => json(join(folder, 'contact-batches', id + '.json'))).find(Boolean)
}

/**
 * A pending contact as the new ledger takes it (kin/src/contact/deliveries.ts). On the `legacy` transport, which no
 * channel answers to: it is never sent or asked about again, only settled with Core — cancelled before sending when no
 * part of it began, otherwise unconfirmed.
 */
function deliveryOf(attempt, batch) {
  const parts = (attempt.outbound?.bubbles ?? [attempt.outbound?.text ?? attempt.text_excerpt])
    .filter((words) => typeof words === 'string' && words.trim()).map((words) => ({ text: words }))
  const items = batch?.items ?? []
  const begun = items.some((item) => BEGUN.includes(item.state))
  const accepted = items.filter((item) => item.state === 'accepted').map((item) => ({ state: 'accepted', ...(item.messageId ? { platformId: item.messageId } : {}) }))
  const recipient = identity(attempt.recipient_person_id ?? OWNER)
  return {
    id: attempt.id, kind: 'contact', key: 'legacy', recipient, transport: 'legacy',
    to: { chat: 'legacy', kind: recipient.startsWith('group:') ? 'group' : 'direct' }, parts,
    sent: begun ? [...accepted, ...(accepted.length < items.length ? [{ state: 'unknown' }] : [])] : [],
    state: begun ? 'unknown' : 'cancelled',
    at: new Date(Date.parse(attempt.updated_at ?? attempt.created_at) || Date.now()).toISOString(),
    reason: begun ? `旧 KIN 已经开始发（平台收下 ${accepted.length}/${items.length} 段），搬家后问不到旧平台，不再发` : '旧 KIN 写好还没发，搬家时取消',
    remembered: false,
    excerpt: parts.map((part) => part.text).join(' ').slice(0, 80),
  }
}

/** How many of a folder's JSON records pass `test`; one nobody can read counts, since nobody can say it is done. */
function count(folder, test = () => true) {
  if (!existsSync(folder)) return 0
  return readdirSync(folder).filter((name) => name.endsWith('.json')).filter((name) => {
    try {
      return test(JSON.parse(readFileSync(join(folder, name), 'utf8')))
    } catch {
      return true
    }
  }).length
}

/** Keep the clean Core's owner identity consistent across every reference in the copied store. */
function remapOwner(store) {
  if (oldOwner === OWNER) return
  const { DatabaseSync } = process.getBuiltinModule('node:sqlite')
  const database = new DatabaseSync(join(store, 'memory.sqlite3'))
  const quote = (name) => '"' + name.replaceAll('"', '""') + '"'
  database.function('migrated_identity', (value) => typeof value === 'string'
    ? value.replace(new RegExp(oldOwner + '(?![A-Za-z0-9_-])', 'g'), OWNER) : value)
  const changes = {}
  try {
    database.exec('BEGIN IMMEDIATE')
    const tables = database.prepare("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").all()
    const virtual = tables.filter((table) => /CREATE VIRTUAL TABLE/i.test(table.sql)).map((table) => table.name)
    for (const table of tables) {
      if (virtual.some((name) => table.name === name || table.name.startsWith(name + '_'))) continue
      const columns = database.prepare('PRAGMA table_info(' + quote(table.name) + ')').all().filter((column) => /TEXT/i.test(column.type))
      for (const column of columns) {
        const field = quote(column.name)
        const result = database.prepare('UPDATE ' + quote(table.name) + ' SET ' + field + '=migrated_identity(' + field + ') WHERE instr(' + field + ', ?) > 0').run(oldOwner)
        if (result.changes) changes[table.name + '.' + column.name] = result.changes
      }
    }
    database.exec('COMMIT')
    writeFileSync(join(to, 'identity-migration.json'), text({ from: oldOwner, to: OWNER, changes }))
    say('主人的内部编号已在复制的库里统一；原始消息编号、时间、文字称呼不变')
  } catch (error) {
    database.exec('ROLLBACK')
    throw error
  } finally { database.close() }
}
