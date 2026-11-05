// Synthetic check of KIN's WeChat and Feishu transports: fake platforms on 127.0.0.1 (nothing real is reached),
// the transports built straight from src/channels, then a short real headless DSH that starts her channels
// module from saved accounts. Every id, key and secret here is made up.
// Usage: node dev/e2e-channels.mjs        (run `npm run build` first for the DSH part)
import { build } from 'esbuild'
import { createDecipheriv, randomBytes } from 'node:crypto'
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { bootDsh, kinDir, repo } from './env.mjs'
import { fakeFeishu, fakeWeChat } from './fake-platforms.mjs'

const root = join(repo, '.dev', 'channels-test')
rmSync(root, { recursive: true, force: true })
mkdirSync(join(root, 'files'), { recursive: true })
await build({
  entryPoints: ['feishu', 'wechat', 'voice', 'stickers', 'feishu-socket'].map((name) => join(kinDir, 'src/channels', name + '.ts')),
  outdir: join(root, 'lib'), bundle: true, splitting: true, format: 'esm', platform: 'node', target: 'node24', packages: 'external', logLevel: 'warning',
})
const lib = async (name) => import(pathToFileURL(join(root, 'lib', name + '.js')).href)
const { Feishu } = await lib('feishu')
const { WeChat } = await lib('wechat')
const { Voice, sweepVoice, oggDuration } = await lib('voice')
const { stickers } = await lib('stickers')
const { headersOf } = await lib('feishu-socket')

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(what, test, ms = 20_000) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) {
    const value = await test()
    if (value) return value
  }
  throw new Error('timed out waiting for ' + what)
}
const log = (message) => console.log('  · ' + message)
const checks = {}

// Synthetic media: a PNG header, a two-second Ogg Opus skeleton (OpusHead pre-skip 312, last granule 96312), a PDF.
const PNG = Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), randomBytes(64)])
const page = (granule, packet) => {
  const head = Buffer.alloc(27 + 1)
  head.write('OggS'); head.writeBigUInt64LE(BigInt(granule), 6); head[26] = 1; head[27] = packet.length
  return Buffer.concat([head, packet])
}
const opusHead = Buffer.alloc(19); opusHead.write('OpusHead'); opusHead[8] = 1; opusHead[9] = 1; opusHead.writeUInt16LE(312, 10); opusHead.writeUInt32LE(48000, 12)
const OGG = Buffer.concat([page(0, opusHead), page(96312, randomBytes(40))])
const files = { ogg: join(root, 'files', 'kin-voice.ogg'), pdf: join(root, 'files', 'notes.pdf') }
writeFileSync(files.ogg, OGG)
writeFileSync(files.pdf, '%PDF-1.4 synthetic\n')

const decrypt = (bytes, aesKey) => {
  const key = Buffer.from(Buffer.from(aesKey, 'base64').toString('ascii'), 'hex')
  const decipher = createDecipheriv('aes-128-ecb', key, null)
  return Buffer.concat([decipher.update(bytes), decipher.final()])
}

// ── Feishu ─────────────────────────────────────────────────────────────────────────────────────────
console.log('Feishu')
const lark = await fakeFeishu()
const fromFeishu = []
const feishu = new Feishu({ appId: 'cli_fake', appSecret: 'fake-secret', domain: lark.base }, join(root, 'data/channels/feishu'), log)
await feishu.start((message) => fromFeishu.push(message))
await until('the long connection', () => feishu.status().state === 'connected')
lark.state.resources.set('img_in', PNG)
lark.state.resources.set('voice_in', OGG)
lark.say(lark.message('om_1', 'oc_owner', 'text', { text: '下班啦' }))
lark.say(lark.message('om_2', 'oc_owner', 'image', { image_key: 'img_in' }))
lark.say(lark.message('om_3', 'oc_owner', 'audio', { file_key: 'voice_in', duration: 2000 }))
lark.say(lark.message('om_4', 'oc_family', 'text', { text: '@_user_1 晚上吃什么' },
  { kind: 'group', mentions: [{ key: '@_user_1', id: { open_id: 'ou_kin' }, name: 'KIN' }] }))
await until('four pushed messages', () => fromFeishu.length >= 4)
const [said, image, voice, group] = ['om_1', 'om_2', 'om_3', 'om_4'].map((id) => fromFeishu.find((message) => message.id === id))
checks['feishu: pushed text, image and voice arrive in order, attachments saved in her data dir'] = fromFeishu.map((message) => message.id).join() === 'om_1,om_2,om_3,om_4'
  && said.text === '下班啦' && said.kind === 'direct'
  && image.files?.[0]?.kind === 'image' && readFileSync(image.files[0].path).equals(PNG) && image.files[0].path.startsWith(join(root, 'data'))
  && voice.files?.[0]?.kind === 'voice' && readFileSync(voice.files[0].path).equals(OGG)
checks['feishu: in a group, an @ to her is seen and taken out of the text'] = group.kind === 'group' && group.mentioned === true && group.text === '晚上吃什么'
checks['feishu: every pushed event is answered 200'] = await until('acks', () => lark.state.frames.filter((frame) => headersOf(frame).biz_rt
  && JSON.parse(Buffer.from(frame.find(([n]) => n === 8)[1]).toString()).code === 200).length === 4)

// Said while the connection was down: only in the history. The catch-up (every 15 s) finds it, and nothing twice.
lark.say(lark.message('om_5', 'oc_owner', 'text', { text: '刚才断网时说的' }), { missed: true })
await until('the catch-up', () => fromFeishu.some((message) => message.id === 'om_5'), 25_000)
lark.say(lark.message('om_5', 'oc_owner', 'text', { text: '刚才断网时说的' })) // Feishu's late redelivery of it
await sleep(16_000) // one more catch-up round over the same history
checks['feishu: the catch-up brings in a missed message, and nothing arrives twice'] = fromFeishu.length === 5
  && new Set(fromFeishu.map((message) => message.id)).size === 5

const owner = { chat: 'oc_owner', kind: 'direct' }
const sentText = await feishu.send(owner, { text: '我也想你' }, 'd-text')
const sentFile = await feishu.send(owner, { file: files.pdf, kind: 'file' }, 'd-file')
const sentVoice = await feishu.send(owner, { file: files.ogg, kind: 'voice' }, 'd-voice')
const out = [...lark.state.sent.values()]
checks['feishu: text, file and voice go out (voice as audio with its length)'] = [sentText, sentFile, sentVoice].every((sent) => sent.state === 'accepted')
  && out.map((message) => message.msg_type).join() === 'text,file,audio'
  && lark.state.uploads.some((upload) => upload.file_type === 'pdf') && lark.state.uploads.some((upload) => upload.file_type === 'opus' && upload.duration === '2000')
lark.state.loseNextAnswer = true
const lost = await feishu.send(owner, { text: '这句的回执丢了' }, 'd-lost')
const retried = await feishu.send(owner, { text: '这句的回执丢了' }, 'd-lost')
const pending = await feishu.check(owner, 'd-lost')
const checked = await feishu.send(owner, { text: '这句的回执丢了' }, 'd-lost', true)
checks['feishu: check precedes an explicit same-id retry; the platform keeps one message'] = lost.state === 'unknown' && retried.state === 'unknown' && pending.state === 'unknown'
  && checked.state === 'accepted' && [...lark.state.sent.values()].filter((message) => message.content.includes('回执丢了')).length === 1
  && (await feishu.send(owner, { text: '这句的回执丢了' }, 'd-lost')).platformId === checked.platformId
await feishu.close()
lark.close()

// ── WeChat ─────────────────────────────────────────────────────────────────────────────────────────
console.log('WeChat')
const ilink = await fakeWeChat()
const account = { baseUrl: ilink.base, cdnBaseUrl: ilink.base + '/cdn' }
let savedLogins = 0
const fromWeChat = []
const wechat = new WeChat(account, join(root, 'data/channels/wechat'), () => savedLogins++, log)
await wechat.start((message) => fromWeChat.push(message))
const waiting = wechat.status()
ilink.state.scanned = true
await until('the login', () => wechat.status().state === 'connected')
checks['wechat: login shows a QR, then connects once it is scanned'] = waiting.state === 'login-required'
  && waiting.qr === 'https://example.invalid/wechat-login?qrcode=qr-1' && account.token === 'fake-bot-token' && account.userId === 'wx-owner' && savedLogins === 1

const now = Date.now()
ilink.state.queue.push(
  { message_id: 7000000000000000001n.toString(), from_user_id: 'wx-owner', message_type: 1, create_time_ms: now, context_token: 'ctx-1', item_list: [{ type: 1, text_item: { text: '在吗' } }] },
  { message_id: 2, from_user_id: 'wx-owner', message_type: 1, create_time_ms: now, context_token: 'ctx-2', item_list: [{ type: 2, image_item: { media: ilink.media('dl-img', PNG) } }] },
  { message_id: 3, from_user_id: 'wx-owner', message_type: 1, create_time_ms: now, context_token: 'ctx-3', item_list: [{ type: 3, voice_item: { media: ilink.media('dl-voice', OGG), text: '今天好累呀' } }] },
  { message_id: 3, from_user_id: 'wx-owner', message_type: 1, create_time_ms: now, context_token: 'ctx-3', item_list: [{ type: 1, text_item: { text: '重复投递' } }] },
)
await until('three messages', () => fromWeChat.length >= 3)
await sleep(500)
const [hello, photo, spoken] = fromWeChat
checks['wechat: text, image and voice (with its transcript) arrive, media decrypted, nothing twice'] = fromWeChat.length === 3
  && hello.text === '在吗' && hello.id === '7000000000000000001' && readFileSync(photo.files[0].path).equals(PNG)
  && spoken.files[0].kind === 'voice' && spoken.files[0].transcript === '今天好累呀' && readFileSync(spoken.files[0].path).equals(OGG)

const me = { chat: 'wx-owner', kind: 'direct' }
const text = await wechat.send(me, { text: '收到啦' }, 'w-text')
const spokenBack = await wechat.send(me, { file: files.ogg, kind: 'voice' }, 'w-voice')
const unnamed = await wechat.send(me, { file: files.pdf, kind: 'file' }, 'w-file')
const named = await wechat.send(me, { file: files.pdf, kind: 'file', named: true }, 'w-file-named')
await wechat.typing(me, true)
const fileItem = ilink.state.sent.at(-1).item_list[0].file_item
checks['wechat: text and voice go out; a file only when the owner named it'] = text.state === 'accepted' && text.platformId === '7234567890123456790'
  && spokenBack.state === 'accepted' && unnamed.state === 'failed' && named.state === 'accepted' && ilink.state.sent.length === 3
  && ilink.state.sent.every((msg) => msg.context_token === 'ctx-3')
  && decrypt(ilink.state.cdn.get(fileItem.media.encrypt_query_param), fileItem.media.aes_key).toString() === readFileSync(files.pdf, 'utf8')
checks['wechat: typing reaches WeChat'] = ilink.state.typing.join() === '1'
ilink.state.loseNextAnswer = true
const wxLost = await wechat.send(me, { text: '回执测试' }, 'w-lost')
const beforeRetry = ilink.state.sent.length
const wxCached = await wechat.send(me, { text: '回执测试' }, 'w-lost')
const noImplicitRetry = ilink.state.sent.length === beforeRetry
const wxRetried = await wechat.send(me, { text: '回执测试' }, 'w-lost', true)
const afterRetry = ilink.state.sent.length
await wechat.send(me, { text: '回执测试' }, 'w-lost', true)
checks['wechat: only an explicit unknown retry sends again, with the same client id; accepted never repeats'] = wxLost.state === 'unknown'
  && wxCached.state === 'unknown' && noImplicitRetry && wxRetried.state === 'accepted'
  && ilink.state.sent.at(-1).client_id === ilink.state.sent.at(-2).client_id
  && afterRetry === beforeRetry + 1 && ilink.state.sent.length === afterRetry
await wechat.close()
ilink.close()

// ── Her voice and stickers ─────────────────────────────────────────────────────────────────────────
console.log('Voice and stickers')
// Stands in for the Qwen speech worker: same protocol (a JSON line in, the file written, a JSON line out).
const fakePython = join(root, 'files', 'python')
writeFileSync(fakePython, `#!/bin/sh\nshift\nexec "${process.execPath}" "$@"\n`)
chmodSync(fakePython, 0o755)
const worker = join(root, 'files', 'speech-worker.mjs')
writeFileSync(worker, `import { appendFileSync, copyFileSync } from 'node:fs'
import { createInterface } from 'node:readline'
for await (const line of createInterface({ input: process.stdin })) {
  const request = JSON.parse(line)
  copyFileSync(${JSON.stringify(files.ogg)}, request.file)
  appendFileSync(${JSON.stringify(join(root, 'files', 'spoken.log'))}, request.text + '\\n')
  console.log(JSON.stringify({ ok: true, durationMs: 2000 }))
}
`)
const voiceDir = join(root, 'data/voice')
const speech = new Voice({ python: fakePython, model: 'fake-model', ffmpeg: 'fake-ffmpeg' }, voiceDir, worker)
const first = await speech.speak('今天也辛苦了', 'd-say')
const second = await speech.speak('今天也辛苦了', 'd-say')
speech.close()
checks['voice: her speech worker makes an Ogg file, kept under the delivery id'] = first === second && oggDuration(readFileSync(first)) === 2000
  && readFileSync(join(root, 'files', 'spoken.log'), 'utf8') === '今天也辛苦了\n'
const old = join(voiceDir, 'old.ogg')
writeFileSync(old, OGG)
utimesSync(old, new Date(Date.now() - 15 * 86_400_000), new Date(Date.now() - 15 * 86_400_000))
const trash = join(root, 'Trash')
mkdirSync(trash)
checks['voice: audio older than 14 days goes to the Trash, newer stays'] = sweepVoice(voiceDir, { trash }) === 1 && !existsSync(old) && existsSync(first)
  && readdirSync(join(trash, readdirSync(trash)[0])).join() === 'old.ogg'

const memes = join(root, 'data/memes')
mkdirSync(join(memes, 'originals'), { recursive: true })
writeFileSync(join(memes, 'originals', 'happy.png'), PNG)
writeFileSync(join(memes, 'index.json'), JSON.stringify({ schema: 1, items: {
  'meme-happy': { id: 'meme-happy', file: 'happy.png', state: 'ready', description: '一只开心转圈的小猫', tags: ['开心'], uses: [{ when: '高兴的时候', reading: '好耶' }] },
  'meme-sad': { id: 'meme-sad', file: 'sad.png', state: 'ready', description: '趴着叹气', tags: ['难过'] },
} }))
const [sticker] = stickers(memes, '好开心')
checks['stickers: her meme library gives a fitting one as an image file'] = sticker?.id === 'meme-happy' && readFileSync(sticker.file).equals(PNG)

// ── Inside DSH ─────────────────────────────────────────────────────────────────────────────────────
// What only a running DSH shows: the module starts from saved accounts, her page reads their status, and another
// plugin hears arrivals through the `kinTransports` service.
console.log('Inside DSH')
const lark2 = await fakeFeishu()
const ilink2 = await fakeWeChat()
const heard = join(repo, '.dev', 'e2e-channels', 'heard.jsonl')
const kinFiles = { 'channels.json': {
  feishu: { appId: 'cli_fake', appSecret: 'fake-secret', domain: lark2.base },
  wechat: { baseUrl: ilink2.base, cdnBaseUrl: ilink2.base + '/cdn' },
} }
const dsh = await bootDsh({ name: 'e2e-channels', kinFiles, extraRows: `- insert:
    - id: kin-channels-probe
      name: ${JSON.stringify(join(kinDir, 'dev/channels-probe.mjs'))}
      config:
        out: ${JSON.stringify(heard)}
` })
try {
  const base = await Promise.race([dsh.url, sleep(120_000).then(() => { throw new Error('DSH did not start:\n' + dsh.logs().slice(-3000)) })])
  const login = await fetch(base, { redirect: 'manual' })
  const cookie = login.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ')
  const page = await until('her channels on the page', async () => {
    const view = await fetch(new URL('api/kin/channels', base), { headers: { cookie } }).then((r) => r.json()).catch(() => null)
    return view?.feishu?.status.state === 'connected' && view.wechat?.status.qr ? view : null
  }, 180_000)
  ilink2.state.scanned = true
  const loggedIn = await until('the WeChat login', async () => {
    const view = await fetch(new URL('api/kin/channels', base), { headers: { cookie } }).then((r) => r.json()).catch(() => null)
    return view?.wechat?.status.state === 'connected' ? view : null
  })
  checks['in DSH: saved accounts start; the page shows Feishu connected and WeChat\'s QR, then the login'] = page.wechat.status.state === 'login-required'
    && loggedIn.wechat.userId === 'wx-owner'
  checks['in DSH: secrets stay in channels.json (owner-only), never on the page'] = (statSync(join(dsh.dataDir, 'channels.json')).mode & 0o777) === 0o600
    && readFileSync(join(dsh.dataDir, 'channels.json'), 'utf8').includes('fake-bot-token')
    && ![page, loggedIn].some((view) => /fake-secret|fake-bot-token/.test(JSON.stringify(view)))
  lark2.say(lark2.message('om_dsh', 'oc_owner', 'text', { text: '从 DSH 里听到了吗' }))
  checks['in DSH: another plugin hears a Feishu message through kinTransports'] = await until('the probe', () => existsSync(heard)
    && readFileSync(heard, 'utf8').includes('从 DSH 里听到了吗'), 30_000)
} finally {
  dsh.dsh.kill('SIGTERM')
  await Promise.race([new Promise((done) => dsh.dsh.once('exit', done)), sleep(5000)])
  if (dsh.dsh.exitCode === null && dsh.dsh.signalCode === null) dsh.dsh.kill('SIGKILL')
  await dsh.mock.close()
  writeFileSync(join(dsh.root, 'dsh.log'), dsh.logs())
  lark2.close()
  ilink2.close()
}

console.log('')
for (const [name, ok] of Object.entries(checks)) console.log(`${ok ? '✓' : '✗'} ${name}`)
process.exit(Object.values(checks).every(Boolean) ? 0 : 1)
