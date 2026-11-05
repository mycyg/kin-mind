// Feishu and WeChat (iLink) as far as KIN uses them, as fake servers on 127.0.0.1: nothing real is reached and every
// id, key and secret is made up. They live in the harness, so they outlast a DSH restart as real platforms do.
// Used by dev/e2e-channels.mjs and dev/e2e-world.mjs.
import { build } from 'esbuild'
import { createCipheriv, randomBytes } from 'node:crypto'
import { createServer } from 'node:http'
import { join } from 'node:path'
import { WebSocketServer } from 'ws'
import { kinDir } from './env.mjs'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const body = (request) => new Promise((resolve) => {
  const chunks = []
  request.on('data', (chunk) => chunks.push(chunk)).on('end', () => resolve(Buffer.concat(chunks)))
})
const json = (response, value, status = 200) => {
  response.writeHead(status, { 'content-type': 'application/json' })
  response.end(typeof value === 'string' ? value : JSON.stringify(value))
}
const listen = (server) => new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(`http://127.0.0.1:${server.address().port}`)))

/** Feishu's long-connection frames, encoded by her own transport's code. */
async function codec() {
  const { outputFiles } = await build({
    entryPoints: [join(kinDir, 'src/channels/feishu-socket.ts')], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'warning',
  })
  return import('data:text/javascript;base64,' + Buffer.from(outputFiles[0].contents).toString('base64'))
}

/** Feishu's open platform as far as KIN uses it: token, bot info, the long connection, message list, resources, uploads, sends (deduplicated by uuid). */
export async function fakeFeishu() {
  const { encode, decode, header } = await codec()
  const state = { history: [], resources: new Map(), uploads: [], sent: new Map(), loseNextAnswer: false, sockets: [], frames: [], seq: 0 }
  const wss = new WebSocketServer({ noServer: true })
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://fake')
    const path = url.pathname
    const raw = await body(request)
    if (path === '/open-apis/auth/v3/tenant_access_token/internal') {
      const { app_id, app_secret } = JSON.parse(raw)
      return json(response, app_id === 'cli_fake' && app_secret === 'fake-secret' ? { code: 0, tenant_access_token: 't-fake', expire: 7200 } : { code: 10014, msg: 'bad secret' })
    }
    if (path === '/callback/ws/endpoint') {
      return json(response, { code: 0, data: { URL: `ws://127.0.0.1:${server.address().port}/ws?device_id=fake&service_id=33`, ClientConfig: { PingInterval: 120 } } })
    }
    if (request.headers.authorization !== 'Bearer t-fake') return json(response, { code: 99991663, msg: 'bad token' }, 400)
    if (path === '/open-apis/bot/v3/info') return json(response, { code: 0, bot: { open_id: 'ou_kin' } })
    if (path === '/open-apis/im/v1/messages' && request.method === 'GET') {
      const from = Number(url.searchParams.get('start_time')) * 1000
      return json(response, { code: 0, data: { has_more: false, items: state.history.filter((item) => item.chat_id === url.searchParams.get('container_id') && Number(item.create_time) >= from) } })
    }
    const resource = /^\/open-apis\/im\/v1\/messages\/[^/]+\/resources\/([^/]+)$/.exec(path)
    if (resource) {
      response.writeHead(200, { 'content-type': 'application/octet-stream' })
      return response.end(state.resources.get(resource[1]))
    }
    if (path === '/open-apis/im/v1/images' || path === '/open-apis/im/v1/files') {
      const form = await new Request('http://fake', { method: 'POST', headers: { 'content-type': request.headers['content-type'] }, body: raw }).formData()
      state.uploads.push(Object.fromEntries([...form].map(([key, value]) => [key, typeof value === 'string' ? value : value.size])))
      return json(response, { code: 0, data: path.endsWith('images') ? { image_key: 'img_up_' + state.uploads.length } : { file_key: 'file_up_' + state.uploads.length } })
    }
    if (path === '/open-apis/im/v1/messages' && request.method === 'POST') {
      const sent = JSON.parse(raw)
      if (!state.sent.has(sent.uuid)) state.sent.set(sent.uuid, { message_id: 'om_out_' + (state.sent.size + 1), chat_id: sent.receive_id, msg_type: sent.msg_type, content: sent.content })
      // The message lands; its answer is lost on the way back.
      if (state.loseNextAnswer) return (state.loseNextAnswer = false, json(response, 'bad gateway', 502))
      return json(response, { code: 0, data: state.sent.get(sent.uuid) })
    }
    json(response, { code: 404, msg: 'no route ' + path }, 404)
  })
  server.on('upgrade', (request, socket, head) => wss.handleUpgrade(request, socket, head, (ws) => {
    state.sockets.push(ws)
    ws.on('message', (data) => state.frames.push(decode(new Uint8Array(data))))
  }))
  const base = await listen(server)
  const message = (id, chat, type, content, { kind = 'p2p', mentions, sender = 'ou_owner' } = {}) => ({ id, chat, kind, type, content: JSON.stringify(content), mentions, sender, at: String(Date.now()) })
  return {
    base, state, message, close: () => { wss.clients.forEach((ws) => ws.terminate()); server.close() },
    /** In the chat's history (what a listing returns) and, unless missed, pushed over the long connection. */
    say(m, { missed = false } = {}) {
      state.history.push({ message_id: m.id, chat_id: m.chat, msg_type: m.type, body: { content: m.content }, create_time: m.at,
        sender: { id: m.sender, id_type: 'open_id', sender_type: 'user' }, mentions: m.mentions?.map((x) => ({ key: x.key, id: x.id.open_id, id_type: 'open_id', name: x.name })) })
      if (missed) return
      const event = { schema: '2.0', header: { event_id: 'ev_' + m.id, event_type: 'im.message.receive_v1' }, event: {
        sender: { sender_id: { open_id: m.sender }, sender_type: 'user' },
        message: { message_id: m.id, chat_id: m.chat, chat_type: m.kind, message_type: m.type, content: m.content, create_time: m.at, mentions: m.mentions } } }
      state.sockets.at(-1).send(encode([[1, BigInt(++state.seq)], [2, 0n], [3, 33n], [4, 1n], header('type', 'event'),
        header('message_id', 'frame_' + state.seq), header('sum', '1'), header('seq', '0'), [8, Buffer.from(JSON.stringify(event))]]))
    },
  }
}

/** iLink as far as KIN uses it: QR login, long poll, CDN with AES-128-ECB, uploads, sends, typing. */
export async function fakeWeChat() {
  const state = { loseNextAnswer: false, scanned: false, queue: [], cdn: new Map(), sent: [], typing: [], cursor: 0 }
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://fake')
    const path = url.pathname
    const raw = await body(request)
    if (path === '/ilink/bot/get_bot_qrcode') return json(response, { qrcode: 'qr-1', qrcode_img_content: 'https://example.invalid/wechat-login?qrcode=qr-1' })
    if (path === '/ilink/bot/get_qrcode_status') {
      return json(response, state.scanned ? { status: 'confirmed', bot_token: 'fake-bot-token', ilink_bot_id: 'bot-fake', ilink_user_id: 'wx-owner' } : { status: 'wait' })
    }
    if (path === '/cdn/download') {
      response.writeHead(200)
      return response.end(state.cdn.get(url.searchParams.get('encrypted_query_param')))
    }
    if (path === '/cdn/upload') {
      const param = 'dl-up-' + state.cdn.size
      state.cdn.set(param, raw)
      response.writeHead(200, { 'x-encrypted-param': param })
      return response.end()
    }
    if (request.headers.authorization !== 'Bearer fake-bot-token') return json(response, { ret: -1, errmsg: 'not logged in' }, 401)
    const asked = JSON.parse(raw)
    if (path === '/ilink/bot/getupdates') {
      if (!state.queue.length) await sleep(200)
      // Message ids go out as WeChat writes them: bare 64-bit numbers.
      return json(response, JSON.stringify({ ret: 0, msgs: state.queue.splice(0), get_updates_buf: 'cursor-' + ++state.cursor, longpolling_timeout_ms: 2000 })
        .replace(/"message_id":"(\d{16,})"/g, '"message_id":$1'))
    }
    if (path === '/ilink/bot/getuploadurl') return json(response, { ret: 0, upload_param: 'up-' + asked.filekey })
    if (path === '/ilink/bot/sendmessage') {
      state.sent.push(asked.msg)
      if (state.loseNextAnswer) return (state.loseNextAnswer = false, json(response, 'bad gateway', 502))
      // A 64-bit id, beyond what a JS number holds.
      return json(response, `{"ret":0,"message_id":${7234567890123456789n + BigInt(state.sent.length)}}`)
    }
    if (path === '/ilink/bot/getconfig') return json(response, { ret: 0, typing_ticket: 'ticket-fake' })
    if (path === '/ilink/bot/sendtyping') return (state.typing.push(asked.status), json(response, { ret: 0 }))
    json(response, { ret: -1, errmsg: 'no route ' + path }, 404)
  })
  const base = await listen(server)
  const key = randomBytes(16)
  /** Media on the CDN as WeChat keeps it: encrypted, its key as base64 of hex. */
  const media = (name, bytes) => {
    const cipher = createCipheriv('aes-128-ecb', key, null)
    state.cdn.set(name, Buffer.concat([cipher.update(bytes), cipher.final()]))
    return { encrypt_query_param: name, aes_key: Buffer.from(key.toString('hex')).toString('base64') }
  }
  return { base, state, media, close: () => server.close() }
}
