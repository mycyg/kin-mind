/**
 * WeChat through iLink, as legacy/wechat-acp/src/weixin did it: a QR login, a long poll for what arrives, media
 * on the WeChat CDN (AES-128-ECB), "typing…", and sends that carry the context token of the person's latest
 * message (WeChat lets her write only to someone who has written to her). iLink cannot say what became of a
 * send whose answer was lost, so this transport has no `check`; and it sends a file only when the owner named it.
 */
import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto'
import { mkdirSync, readFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { call, hash, imageType, keep, outcome, preview, sent, Store, type State } from './store.js'
import type { Address, Inbound, Part, Sent, Transport } from './transport.js'

export interface WeChatAccount {
  /** iLink's API address and its media (CDN) address. */
  baseUrl: string
  cdnBaseUrl: string
  /** Set by the QR login: the bot's token, its id, and the WeChat user who scanned. */
  token?: string
  botId?: string
  userId?: string
}

interface WeChatState extends State {
  /** iLink's long-poll cursor: what was already handed out. */
  cursor: string
  /** Each chat's latest context token. */
  contexts: Record<string, string>
}

interface Media { encrypt_query_param?: string; aes_key?: string }
interface Item {
  type?: number
  text_item?: { text?: string }
  image_item?: { media?: Media }
  voice_item?: { media?: Media; text?: string }
  file_item?: { media?: Media; file_name?: string }
  video_item?: { media?: Media }
  ref_msg?: { title?: string; message_item?: Item }
}
interface Message {
  message_id?: number | string
  client_id?: string
  from_user_id?: string
  group_id?: string
  message_type?: number
  create_time_ms?: number
  context_token?: string
  item_list?: Item[]
}

const MINUTE = 60_000
const TEXT = 1, IMAGE = 2, VOICE = 3, FILE = 4, VIDEO = 5
const BOT = 2
const KINDS: Record<number, 'image' | 'voice' | 'file' | 'video'> = { [IMAGE]: 'image', [VOICE]: 'voice', [FILE]: 'file', [VIDEO]: 'video' }
const LABELS = { image: '图片', voice: '语音', video: '视频', file: '文件' }
/** iLink's own code for a session it has paused. */
const SESSION_EXPIRED = -14

export class WeChat implements Transport {
  readonly id = 'wechat'
  private readonly store: Store<WeChatState>
  /** A new login, poll or close ends whatever loop was running. */
  private round = 0
  private poller?: AbortController
  private wake = () => {}
  private readonly tickets = new Map<string, { value: string; until: number }>()
  private current: ReturnType<Transport['status']> = { state: 'stopped' }
  private inbound: (message: Inbound) => void = () => {}
  private closed = false

  constructor(
    private readonly account: WeChatAccount,
    private readonly dir: string,
    /** Keeps the account (now holding the login) in her data dir. */
    private readonly saveAccount: () => void,
    private readonly log: (message: string) => void,
  ) {
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    this.store = new Store(join(dir, 'state.json'), { seen: {}, sent: {}, cursor: '', contexts: {} })
  }

  async start(inbound: (message: Inbound) => void) {
    this.inbound = inbound
    if (this.account.token) void this.poll()
    else await this.login()
  }

  async close() {
    this.closed = true
    this.stop()
    this.current = { state: 'stopped' }
    // It writes nothing more: the cursor on disk still stands before whatever it was taking in, so the
    // connection opened next is given that again and takes in what this one had not handed over.
    this.store.close()
  }

  status() {
    return this.current
  }

  /** Shows a QR for the page and waits (five minutes, new codes as they expire) for the owner to scan and confirm. */
  async login() {
    const round = this.stop()
    const deadline = Date.now() + 5 * MINUTE
    const fresh = async () => {
      const code = await this.get('ilink/bot/get_bot_qrcode?bot_type=3') as { qrcode: string; qrcode_img_content: string }
      this.current = { state: 'login-required', qr: code.qrcode_img_content, reason: '用微信扫一扫，再在手机上确认' }
      return code.qrcode
    }
    let qr = await fresh().catch((error: Error) => {
      this.current = { state: 'failed', reason: '拿不到登录二维码：' + error.message }
      throw error
    })
    void (async () => {
      while (round === this.round && Date.now() < deadline) {
        const answer = await this.get(`ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(qr)}`, 40_000).catch(() => ({ status: 'wait' })) as {
          status: string; bot_token?: string; baseurl?: string; ilink_bot_id?: string; ilink_user_id?: string
        }
        if (round !== this.round) return
        if (answer.status === 'scaned') this.current = { ...this.current, reason: '扫到了，在手机上点一下确认' }
        if (answer.status === 'expired') qr = await fresh()
        if (answer.status === 'confirmed') {
          // Someone else's WeChat: the old cursor and context tokens were theirs.
          if (answer.ilink_user_id !== this.account.userId) Object.assign(this.store.value, { cursor: '', contexts: {} })
          Object.assign(this.account, {
            token: answer.bot_token, baseUrl: answer.baseurl || this.account.baseUrl, botId: answer.ilink_bot_id, userId: answer.ilink_user_id,
          })
          this.saveAccount()
          this.log('wechat logged in')
          return void this.poll()
        }
        await this.pause(1500)
      }
      if (round === this.round) this.current = { state: 'login-required', reason: '二维码过期了，再要一张吧' }
    })().catch((error: Error) => {
      if (round === this.round) this.current = { state: 'failed', reason: '登录没成功：' + error.message }
    })
  }

  async send(to: Address, part: Part, id: string, retryUnknown = false): Promise<Sent> {
    const prior = this.store.value.sent[id]
    if (prior && (prior.state === 'accepted' || (prior.state === 'unknown' && !retryUnknown))) return sent(prior)
    const at = Date.now()
    const fail = (reason: string) => this.store.settle(id, { chat: to.chat, at, state: 'failed', reason })
    if ('file' in part && (part.kind === 'file' || part.kind === 'video') && !part.named) return fail('微信上只发主人点名要的文件')
    const context = this.store.value.contexts[to.chat]
    if (!context) return fail('对方还没在微信上找过她：微信不让她先开口')
    let item: Item
    try {
      item = 'text' in part ? { type: TEXT, text_item: { text: part.text } } : await this.upload(to, part)
    } catch (error) {
      return fail((error as Error).message)
    }
    const msg = {
      from_user_id: '', to_user_id: to.kind === 'direct' ? to.chat : '', ...(to.kind === 'group' ? { group_id: to.chat } : {}),
      client_id: 'kin-' + hash(id).slice(0, 32), message_type: BOT, message_state: 2, context_token: context, item_list: [item],
    }
    // On disk before the platform can see it: a stop half way leaves it unknown, never unsent.
    this.store.settle(id, { chat: to.chat, at, state: 'unknown', reason: 'sending' })
    const answer = await call(this.url('ilink/bot/sendmessage'), {
      method: 'POST', headers: this.headers(), body: JSON.stringify({ msg, base_info: BASE_INFO }),
    }, 15_000)
    // Accepted only with the server's message id: a bare `ret: 0` proves nothing (legacy delivery-receipts.mjs).
    return this.store.settle(id, {
      chat: to.chat, at,
      ...outcome(answer,
        (body) => {
          const code = [body.ret, body.errcode].find((value) => Number.isInteger(value) && value !== 0)
          return code === undefined ? undefined : `微信拒绝了：${code} ${body.errmsg ?? ''}`.trim()
        },
        ({ text }) => /"message_id"\s*:\s*"?([1-9][0-9]*)/.exec(text)?.[1]),
    })
  }

  async typing(to: Address, on: boolean) {
    const context = this.store.value.contexts[to.chat]
    if (!context || to.kind !== 'direct') return
    try {
      let ticket = this.tickets.get(to.chat)
      if (!ticket || ticket.until < Date.now()) {
        const { typing_ticket } = await this.post('ilink/bot/getconfig', { ilink_user_id: to.chat, context_token: context }, 10_000)
        if (!typing_ticket) return
        this.tickets.set(to.chat, (ticket = { value: typing_ticket, until: Date.now() + 24 * 60 * MINUTE }))
      }
      await this.post('ilink/bot/sendtyping', { ilink_user_id: to.chat, typing_ticket: ticket.value, status: on ? 1 : 2 }, 10_000)
    } catch {
      // "Typing…" is a nicety; one that did not show changes nothing.
    }
  }

  private async poll() {
    const round = this.stop()
    let failures = 0
    let wait = 35_000
    this.current = { state: 'connecting' }
    while (round === this.round) {
      this.poller = new AbortController()
      try {
        const answer = await this.post('ilink/bot/getupdates', { get_updates_buf: this.store.value.cursor }, wait + 5_000, this.poller.signal) as {
          ret?: number; errcode?: number; errmsg?: string; msgs?: Message[]; get_updates_buf?: string; longpolling_timeout_ms?: number
        }
        if (round !== this.round) return
        if (answer.longpolling_timeout_ms) wait = answer.longpolling_timeout_ms
        if (answer.ret === SESSION_EXPIRED || answer.errcode === SESSION_EXPIRED) {
          // The old bridge waited an hour and went on; the owner can also scan again.
          this.current = { state: 'login-required', reason: '微信暂停了这次登录：重新扫码，或者等一小时它自己恢复' }
          await this.pause(60 * MINUTE)
          continue
        }
        if (answer.ret || answer.errcode) throw new Error(`微信拉消息失败：${answer.ret ?? answer.errcode} ${answer.errmsg ?? ''}`.trim())
        failures = 0
        this.current = { state: 'connected' }
        for (const message of answer.msgs ?? []) await this.take(message)
        if (answer.get_updates_buf) {
          this.store.value.cursor = answer.get_updates_buf
          this.store.save()
        }
      } catch (error) {
        if (round !== this.round) return
        // A long poll nobody answered is an empty one.
        if ((error as Error).name === 'TimeoutError') continue
        failures++
        this.current = { state: 'connecting', reason: (error as Error).message }
        await this.pause(failures >= 3 ? 30_000 : 2_000)
      }
    }
  }

  private async take(message: Message) {
    if (this.closed || message.message_type === BOT) return
    const id = String(message.message_id ?? message.client_id ?? '')
    if (!id || !this.store.fresh(id)) return
    const chat = message.group_id || message.from_user_id || ''
    if (message.context_token) this.store.value.contexts[chat] = message.context_token
    const texts: string[] = []
    const files: NonNullable<Inbound['files']> = []
    for (const [index, item] of (message.item_list ?? []).entries()) {
      if (item.type === TEXT) {
        const quoted = [item.ref_msg?.title, item.ref_msg?.message_item?.text_item?.text].filter(Boolean).join(' | ')
        texts.push((quoted ? `[引用: ${quoted}]\n` : '') + (item.text_item?.text ?? ''))
        continue
      }
      const kind = KINDS[item.type ?? 0]
      if (!kind) continue
      const media = item.image_item?.media ?? item.voice_item?.media ?? item.file_item?.media ?? item.video_item?.media
      const transcript = item.voice_item?.text
      const name = item.file_item?.file_name
      try {
        if (!media?.encrypt_query_param || !media.aes_key) throw new Error('no media')
        const bytes = await this.download(media)
        const path = keep(join(this.dir, 'media', hash(id).slice(0, 16)),
          `${index + 1}-${name ?? (kind === 'image' ? `image.${imageType(bytes) ?? 'jpg'}` : kind === 'voice' ? 'voice.silk' : 'video.mp4')}`, bytes)
        const look = kind === 'image' ? await preview(path, bytes) : undefined
        files.push({ path, kind, ...(name ? { name } : {}), ...(transcript ? { transcript } : {}), ...(look ? { preview: look } : {}) })
      } catch (error) {
        if ((error as Error).message !== 'no media') this.log(`wechat ${kind} not downloaded: ${(error as Error).message}`)
        // The words of a voice message are what matters; without its audio they still come in.
        texts.push(transcript ? `[语音] ${transcript}` : `[${LABELS[kind]}没下载下来${name ? '：' + name : ''}]`)
      }
    }
    const text = texts.join('\n').trim()
    if (this.closed) return
    this.inbound({
      transport: this.id, id, chat, kind: message.group_id ? 'group' : 'direct', sender: message.from_user_id ?? '',
      ...(text ? { text } : {}), ...(files.length ? { files } : {}), at: new Date(message.create_time_ms ?? Date.now()).toISOString(),
    })
    this.store.save()
  }

  private async download(media: Media) {
    const response = await fetch(`${this.cdn()}/download?encrypted_query_param=${encodeURIComponent(media.encrypt_query_param!)}`, {
      signal: AbortSignal.timeout(60_000),
    })
    if (!response.ok) throw new Error(`微信媒体下载 ${response.status}`)
    const decipher = createDecipheriv('aes-128-ecb', aesKey(media.aes_key!), null)
    return Buffer.concat([decipher.update(Buffer.from(await response.arrayBuffer())), decipher.final()])
  }

  /** Puts a file on the WeChat CDN, encrypted with a key of its own, and describes it for a message. Voice goes as a file, as it always did. */
  private async upload(to: Address, part: Exclude<Part, { text: string }>): Promise<Item> {
    const bytes = readFileSync(part.file)
    const key = randomBytes(16)
    const filekey = randomBytes(16).toString('hex')
    const image = part.kind === 'image'
    const cipher = createCipheriv('aes-128-ecb', key, null)
    const encrypted = Buffer.concat([cipher.update(bytes), cipher.final()])
    const answer = await this.post('ilink/bot/getuploadurl', {
      filekey, media_type: image ? 1 : 3, to_user_id: to.chat, rawsize: bytes.length,
      rawfilemd5: createHash('md5').update(bytes).digest('hex'), filesize: encrypted.length, no_need_thumb: true, aeskey: key.toString('hex'),
    }) as { ret?: number; errcode?: number; upload_param?: string; upload_full_url?: string }
    if (answer.ret || answer.errcode || (!answer.upload_full_url?.trim() && !answer.upload_param)) throw new Error('微信不给上传地址')
    const response = await fetch(answer.upload_full_url?.trim()
      || `${this.cdn()}/upload?encrypted_query_param=${encodeURIComponent(answer.upload_param!)}&filekey=${filekey}`, {
      method: 'POST', headers: { 'content-type': 'application/octet-stream' }, body: encrypted, signal: AbortSignal.timeout(60_000),
    })
    const param = response.headers.get('x-encrypted-param')
    if (!response.ok || !param) throw new Error(`微信媒体上传失败：${response.status}`)
    // The key goes as base64 of its hex, the form the receiving side reads.
    const media = { encrypt_query_param: param, aes_key: Buffer.from(key.toString('hex')).toString('base64'), encrypt_type: 1 }
    return image
      ? { type: IMAGE, image_item: { media, mid_size: encrypted.length } as Item['image_item'] }
      : { type: FILE, file_item: { media, file_name: basename(part.file), len: String(bytes.length) } as Item['file_item'] }
  }

  private async get(endpoint: string, timeoutMs = 15_000) {
    const response = await fetch(this.url(endpoint), { headers: this.headers(), signal: AbortSignal.timeout(timeoutMs) })
    if (!response.ok) throw new Error(`微信 ${endpoint.split('?')[0]} ${response.status}`)
    return response.json()
  }

  private async post(endpoint: string, body: object, timeoutMs = 15_000, signal?: AbortSignal) {
    const timeout = AbortSignal.timeout(timeoutMs)
    const response = await fetch(this.url(endpoint), {
      method: 'POST', headers: this.headers(), body: JSON.stringify({ ...body, base_info: BASE_INFO }),
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    })
    const text = await response.text()
    if (!response.ok) throw new Error(`微信 ${endpoint} ${response.status}`)
    // Message ids are 64-bit: kept as their exact digits rather than rounded by JSON.parse.
    return JSON.parse(text.replace(/"message_id"(\s*:\s*)(\d{16,})/g, '"message_id"$1"$2"'))
  }

  private headers() {
    return {
      'content-type': 'application/json', AuthorizationType: 'ilink_bot_token',
      'X-WECHAT-UIN': Buffer.from(String(randomBytes(4).readUInt32BE(0))).toString('base64'),
      ...(this.account.token ? { Authorization: 'Bearer ' + this.account.token } : {}),
    }
  }

  /** Ends the running loop; returns the new round's number. */
  private stop() {
    this.poller?.abort()
    this.wake()
    return ++this.round
  }

  private pause(ms: number) {
    return new Promise<void>((resume) => {
      const timer = setTimeout(resume, ms)
      this.wake = () => { clearTimeout(timer); resume() }
    })
  }

  private url(endpoint: string) {
    return `${this.account.baseUrl.replace(/\/$/, '')}/${endpoint}`
  }

  private cdn() {
    return this.account.cdnBaseUrl.replace(/\/$/, '')
  }
}

const BASE_INFO = { channel_version: '1.0.2' }

/** The CDN key: 16 raw bytes in base64, or (as WeChat also sends it) base64 of 32 hex characters. */
function aesKey(value: string) {
  const raw = Buffer.from(value, 'base64')
  const hex = raw.toString('ascii')
  return raw.length === 32 && /^[0-9a-f]{32}$/i.test(hex) ? Buffer.from(hex, 'hex') : raw.subarray(0, 16)
}
