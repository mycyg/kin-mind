/**
 * Feishu, as legacy/feishu did it: messages pushed over the long connection, and a catch-up every 15 s that
 * lists each chat she knows from where the last complete one began (5 min overlap, never more than a day back),
 * so what a dropped connection missed still comes in, once. Attachments are saved to her data dir. Sends carry
 * an idempotency key Feishu keeps for an hour; delivery retries reuse that key.
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { basename, extname, join } from 'node:path'
import { connect, type Socket } from './feishu-socket.js'
import { call, hash, imageType, keep, outcome, preview, sent, Store, type State } from './store.js'
import type { Address, Inbound, Part, Sent, Transport } from './transport.js'
import { oggDuration } from './voice.js'

export interface FeishuAccount {
  appId: string
  appSecret: string
  /** The Feishu (or Lark) open platform address, e.g. https://open.<domain>. */
  domain: string
}

const MINUTE = 60_000
const CATCH_UP_EVERY = 15_000
const OVERLAP = 5 * MINUTE
const LOOKBACK = 24 * 60 * MINUTE
const FILE_TYPES: Record<string, string> = { '.pdf': 'pdf', '.doc': 'doc', '.docx': 'doc', '.xls': 'xls', '.xlsx': 'xls', '.ppt': 'ppt', '.pptx': 'ppt', '.mp4': 'mp4' }

interface FeishuState extends State {
  /** Every chat she has heard from or written to, and the time before which nothing in it is missing. */
  chats: Record<string, { kind: Address['kind']; covered: number }>
}

/** One message as a pushed event and a listed one both describe it. */
interface Message {
  id: string
  chat: string
  kind: Address['kind']
  type: string
  content: string
  sender: string
  fromBot: boolean
  mentions: { key: string; id?: string; name?: string }[]
  at: number
}

type Resource = { key: string; type: 'image' | 'file'; kind: 'image' | 'voice' | 'video' | 'file'; name?: string }

export class Feishu implements Transport {
  readonly id = 'feishu'
  private readonly store: Store<FeishuState>
  private token?: { value: string; until: number }
  private bot?: Promise<string>
  private socket?: Socket
  private timer?: NodeJS.Timeout
  private catchingUp = false
  /** Arrivals are taken in one at a time, in order: a photo still downloading is not overtaken by the words after it. */
  private intake: Promise<void> = Promise.resolve()
  private closed = false
  private problem?: string
  private current: ReturnType<Transport['status']> = { state: 'stopped' }
  private inbound: (message: Inbound) => void = () => {}

  constructor(private readonly account: FeishuAccount, private readonly dir: string, private readonly log: (message: string) => void) {
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    this.store = new Store(join(dir, 'state.json'), { seen: {}, sent: {}, chats: {} })
  }

  async start(inbound: (message: Inbound) => void) {
    this.inbound = inbound
    this.socket = connect({
      ...this.account,
      onEvent: (event) => this.pushed(event),
      onState: (state, reason) => { if (!this.closed) this.current = { state, ...(reason ? { reason } : {}) } },
    })
    this.timer = setInterval(() => void this.catchUp(), CATCH_UP_EVERY)
    void this.catchUp()
  }

  async close() {
    this.closed = true
    clearInterval(this.timer)
    this.socket?.close()
    this.current = { state: 'stopped' }
  }

  status() {
    return this.problem && this.current.state === 'connected' ? { ...this.current, reason: this.problem } : this.current
  }

  async send(to: Address, part: Part, id: string, retryUnknown = false): Promise<Sent> {
    const prior = this.store.value.sent[id]
    if (prior && (prior.state === 'accepted' || (prior.state === 'unknown' && !retryUnknown))) return sent(prior)
    this.store.value.chats[to.chat] ??= { kind: to.kind, covered: Date.now() }
    let message: { msg_type: string; content: string }
    try {
      message = 'text' in part ? { msg_type: 'text', content: JSON.stringify({ text: part.text }) } : await this.upload(part)
      await this.tenant()
    } catch (error) {
      return this.store.settle(id, { chat: to.chat, at: Date.now(), state: 'failed', reason: (error as Error).message })
    }
    // The same delivery id always gives the same uuid: Feishu makes at most one message of it within the hour.
    const request = { receive_id: to.chat, ...message, uuid: hash(id).slice(0, 32) }
    const receipt = { chat: to.chat, at: Date.now(), request }
    // On disk before the platform can see it: a stop half way leaves it unknown, never unsent.
    this.store.settle(id, { ...receipt, state: 'unknown', reason: 'sending' })
    return this.store.settle(id, { ...receipt, ...(await this.submit(request)) })
  }

  async check(_to: Address, id: string): Promise<Sent> {
    const receipt = this.store.value.sent[id]
    // Receipts are kept a week: an older unknown send can no longer be confirmed, and failed would mean "send it again".
    if (!receipt) return { state: 'unknown', reason: '这条发送的记录已经不在了，没法确认' }
    if (receipt.state !== 'unknown') return sent(receipt)
    if (receipt.platformId) {
      const data = await this.api('GET', '/open-apis/im/v1/messages/' + receipt.platformId).catch(() => null)
      if (data?.items?.some((item: { chat_id: string }) => item.chat_id === receipt.chat)) {
        return this.store.settle(id, { ...receipt, state: 'accepted', reason: undefined })
      }
    }
    return sent(receipt)
  }

  private async submit(request: unknown) {
    const answer = await call(this.url('/open-apis/im/v1/messages?receive_id_type=chat_id'), {
      method: 'POST', headers: { authorization: 'Bearer ' + await this.tenant(), 'content-type': 'application/json; charset=utf-8' },
      body: JSON.stringify(request),
    }, 12_000)
    return outcome(answer,
      (body) => (Number.isInteger(body.code) && body.code !== 0 ? `飞书拒绝了：${body.code} ${body.msg ?? ''}`.trim() : undefined),
      ({ body }) => body.data?.message_id)
  }

  private async upload(part: Exclude<Part, { text: string }>) {
    const bytes = readFileSync(part.file)
    const name = basename(part.file)
    const form = new FormData()
    if (part.kind === 'image') {
      form.set('image_type', 'message')
      form.set('image', new Blob([bytes]), name)
      const { image_key } = await this.api('POST', '/open-apis/im/v1/images', form)
      return { msg_type: 'image', content: JSON.stringify({ image_key }) }
    }
    const voice = part.kind === 'voice'
    form.set('file_type', voice ? 'opus' : FILE_TYPES[extname(name).toLowerCase()] ?? 'stream')
    form.set('file_name', name)
    const duration = voice ? oggDuration(bytes) : undefined
    if (duration) form.set('duration', String(duration))
    form.set('file', new Blob([bytes]), name)
    const { file_key } = await this.api('POST', '/open-apis/im/v1/files', form)
    return { msg_type: voice ? 'audio' : 'file', content: JSON.stringify({ file_key }) }
  }

  private async pushed(event: any) {
    if (event.header?.event_type !== 'im.message.receive_v1') return
    const { message, sender } = event.event
    await this.enqueue({
      id: message.message_id, chat: message.chat_id, kind: message.chat_type === 'p2p' ? 'direct' : 'group',
      type: message.message_type, content: message.content, sender: sender.sender_id?.open_id, fromBot: sender.sender_type !== 'user',
      mentions: (message.mentions ?? []).map((m: any) => ({ key: m.key, id: m.id?.open_id, name: m.name })), at: Number(message.create_time),
    })
  }

  /** Lists every known chat from where its coverage ends; a listing that fails leaves the gap for the next round. */
  private async catchUp() {
    if (this.catchingUp) return
    this.catchingUp = true
    try {
      for (const [chat, known] of Object.entries(this.store.value.chats)) {
        const began = Date.now()
        const from = Math.max(known.covered - OVERLAP, began - LOOKBACK)
        let page = ''
        do {
          const data = await this.api('GET', `/open-apis/im/v1/messages?container_id_type=chat&container_id=${encodeURIComponent(chat)}`
            + `&start_time=${Math.floor(from / 1000)}&sort_type=ByCreateTimeAsc&page_size=50${page && '&page_token=' + encodeURIComponent(page)}`)
          for (const item of data.items ?? []) {
            if (item.deleted || item.upper_message_id) continue
            await this.enqueue({
              id: item.message_id, chat, kind: known.kind, type: item.msg_type, content: item.body?.content ?? '{}',
              sender: item.sender?.id, fromBot: item.sender?.sender_type !== 'user',
              mentions: (item.mentions ?? []).map((m: any) => ({ key: m.key, id: m.id, name: m.name })), at: Number(item.create_time),
            })
          }
          page = data.has_more ? data.page_token ?? '' : ''
        } while (page)
        known.covered = began
      }
      this.store.save()
      this.problem = undefined
    } catch (error) {
      if (this.problem !== (error as Error).message) this.log(`feishu catch-up failed: ${(error as Error).message}`)
      this.problem = (error as Error).message
    } finally {
      this.catchingUp = false
    }
  }

  private enqueue(message: Message) {
    const taken = this.intake.then(() => this.take(message))
    this.intake = taken.catch(() => {})
    return taken
  }

  private async take(message: Message) {
    this.store.value.chats[message.chat] ??= { kind: message.kind, covered: message.at || Date.now() }
    if (message.fromBot || !this.store.fresh(message.id)) return
    const { text, resources, mentioned } = await this.read(message)
    const files: NonNullable<Inbound['files']> = []
    const notes: string[] = []
    for (const [index, resource] of resources.entries()) {
      try {
        const bytes = await this.download(message.id, resource)
        const name = resource.name ?? (resource.kind === 'image' ? `image.${imageType(bytes) ?? 'png'}` : 'file')
        const path = keep(join(this.dir, 'media', hash(message.id).slice(0, 16)), `${index + 1}-${name}`, bytes)
        const look = resource.kind === 'image' ? await preview(path, bytes) : undefined
        files.push({ path, kind: resource.kind, ...(resource.name ? { name: resource.name } : {}), ...(look ? { preview: look } : {}) })
      } catch (error) {
        // The message still goes in; this attachment is named as missing.
        this.log(`feishu attachment not downloaded: ${(error as Error).message}`)
        notes.push(`[${{ image: '图片', voice: '语音', video: '视频', file: '文件' }[resource.kind]}没下载下来${resource.name ? '：' + resource.name : ''}]`)
      }
    }
    const said = [text, ...notes].filter(Boolean).join('\n')
    this.inbound({
      transport: this.id, id: message.id, chat: message.chat, kind: message.kind, sender: message.sender,
      ...(message.kind === 'group' ? { mentioned } : {}), ...(said ? { text: said } : {}), ...(files.length ? { files } : {}),
      at: new Date(message.at || Date.now()).toISOString(),
    })
    this.store.save()
  }

  /** Text and attachments of one message; mentions read as @name, hers taken out. */
  private async read(message: Message) {
    const content = JSON.parse(message.content || '{}') ?? {}
    const resources: Resource[] = []
    let text = ''
    switch (message.type) {
      case 'text': text = content.text ?? ''; break
      case 'post': {
        // Pushed and listed posts may come wrapped in a locale.
        const post = content.content ? content : (Object.values(content)[0] as any) ?? {}
        const lines = (post.content ?? []).map((line: any[]) => line.map((element) => {
          if (element.tag === 'img') resources.push({ key: element.image_key, type: 'image', kind: 'image' })
          if (element.tag === 'media') resources.push({ key: element.file_key, type: 'file', kind: 'video', name: element.file_name })
          if (element.tag === 'at') return String(element.user_id ?? '').startsWith('@_') ? element.user_id : '@' + (element.user_name ?? '')
          return element.text ?? ''
        }).join(''))
        text = [post.title, ...lines].filter(Boolean).join('\n')
        break
      }
      case 'image': resources.push({ key: content.image_key, type: 'image', kind: 'image' }); break
      case 'file': resources.push({ key: content.file_key, type: 'file', kind: 'file', name: content.file_name }); break
      case 'audio': resources.push({ key: content.file_key, type: 'file', kind: 'voice', name: 'voice.opus' }); break
      case 'media': resources.push({ key: content.file_key, type: 'file', kind: 'video', name: content.file_name ?? 'video.mp4' }); break
      case 'sticker': text = '[表情]'; break
      default: text = `[${message.type}]`
    }
    // Without her own id (Feishu not answering) a mention of her reads as @name; the message still comes in.
    const bot = message.mentions.length ? await this.botId().catch(() => undefined) : undefined
    for (const mention of message.mentions) text = text.replaceAll(mention.key, mention.id === bot ? '' : '@' + (mention.name ?? ''))
    return { text: text.trim(), resources, mentioned: message.mentions.some((mention) => mention.id === bot) }
  }

  private async download(messageId: string, resource: Resource) {
    for (let attempt = 1; ; attempt++) {
      try {
        const response = await fetch(this.url(`/open-apis/im/v1/messages/${messageId}/resources/${resource.key}?type=${resource.type}`), {
          headers: { authorization: 'Bearer ' + await this.tenant() }, signal: AbortSignal.timeout(120_000),
        })
        if (!response.ok) throw new Error(`飞书附件下载 ${response.status}`)
        return new Uint8Array(await response.arrayBuffer())
      } catch (error) {
        if (attempt === 3) throw error
        await new Promise((resume) => setTimeout(resume, attempt * 2000))
      }
    }
  }

  private botId() {
    this.bot ??= this.api('GET', '/open-apis/bot/v3/info').then((data) => data.bot.open_id as string)
    return this.bot.catch((error) => {
      this.bot = undefined
      throw error
    })
  }

  private async api(method: string, path: string, body?: FormData) {
    const response = await fetch(this.url(path), {
      method, headers: { authorization: 'Bearer ' + await this.tenant() }, body, signal: AbortSignal.timeout(body ? 180_000 : 30_000),
    })
    const value = await response.json() as { code: number; msg?: string; data?: any }
    if (value.code !== 0) throw new Error(`飞书拒绝了 ${path.split('?')[0]}：${value.code} ${value.msg ?? ''}`.trim())
    return value.data ?? value
  }

  private async tenant() {
    if (this.token && this.token.until > Date.now()) return this.token.value
    const response = await fetch(this.url('/open-apis/auth/v3/tenant_access_token/internal'), {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ app_id: this.account.appId, app_secret: this.account.appSecret }), signal: AbortSignal.timeout(15_000),
    })
    const value = await response.json() as { code: number; msg?: string; tenant_access_token: string; expire: number }
    if (value.code !== 0) throw new Error(`飞书不认这个应用：${value.code} ${value.msg ?? ''}`.trim())
    this.token = { value: value.tenant_access_token, until: Date.now() + (value.expire - 300) * 1000 }
    return this.token.value
  }

  private url(path: string) {
    return this.account.domain.replace(/\/$/, '') + path
  }
}
