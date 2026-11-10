/** What both transports share: a small state file, received media on disk, send receipts and how to read a send's answer. */
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { promisify } from 'node:util'
import type { Sent } from './transport.js'

const DAY = 86_400_000

/** What became of one send, kept under the delivery's own id: a retry of it is recognised, an unknown one can be checked. */
export interface Receipt extends Sent {
  chat: string
  /** When the message request was (first) submitted. */
  at: number
  /** The request itself, for a check that repeats it under the same idempotency key. */
  request?: unknown
}

export interface State {
  /** Platform message ids already taken in, and when: the same message arriving twice is passed on once. */
  seen: Record<string, number>
  sent: Record<string, Receipt>
}

/** A transport's own state file in her data dir, written whole (a temporary file, then a rename). */
export class Store<T extends State> {
  readonly value: T
  private closed = false
  constructor(private readonly file: string, empty: T) {
    this.value = existsSync(file) ? { ...empty, ...JSON.parse(readFileSync(file, 'utf8')) } : empty
  }

  /** Its transport was closed: it writes nothing more. The transport opened after it reads this file and owns it. */
  close() {
    this.closed = true
  }

  save() {
    if (this.closed) return
    const now = Date.now()
    // Nothing comes back after two days (Feishu's catch-up reaches one day back); receipts are kept a week.
    for (const [id, at] of Object.entries(this.value.seen)) if (now - at > 2 * DAY) delete this.value.seen[id]
    for (const [id, receipt] of Object.entries(this.value.sent)) if (now - receipt.at > 7 * DAY) delete this.value.sent[id]
    writeFileSync(this.file + '.tmp', JSON.stringify(this.value), { mode: 0o600 })
    renameSync(this.file + '.tmp', this.file)
  }

  /** True the first time a platform message id is seen. */
  fresh(id: string) {
    if (this.value.seen[id]) return false
    this.value.seen[id] = Date.now()
    return true
  }

  settle(id: string, receipt: Receipt): Sent {
    this.value.sent[id] = receipt
    this.save()
    return sent(receipt)
  }
}

export const sent = ({ state, platformId, reason }: Receipt): Sent => ({ state, ...(platformId ? { platformId } : {}), ...(reason ? { reason } : {}) })

/** A received attachment, saved privately under her data dir. */
export function keep(dir: string, name: string, bytes: Uint8Array) {
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const file = join(dir, basename(name).replace(/[\x00-\x1f<>:"/\\|?*]/g, '_').replace(/^\.+/, '_').slice(0, 120) || 'file')
  writeFileSync(file, bytes, { mode: 0o600 })
  return file
}

export const hash = (value: string) => createHash('sha256').update(value).digest('hex')

/** The extension of an image by its first bytes. */
export function imageType(bytes: Uint8Array): string | undefined {
  const head = Buffer.from(bytes.subarray(0, 12))
  if (head.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'))) return 'png'
  if (head[0] === 0xff && head[1] === 0xd8) return 'jpg'
  if (head.toString('latin1', 0, 4) === 'GIF8') return 'gif'
  if (head.toString('latin1', 0, 4) === 'RIFF' && head.toString('latin1', 8, 12) === 'WEBP') return 'webp'
}

/**
 * A JPEG preview for an image a model cannot take as it is (only PNG, JPEG and WebP up to 5 MB go as they are),
 * made by macOS's own `sips` as the old listener did. No preview is not an error: the image still has its path.
 */
export async function preview(file: string, bytes: Uint8Array) {
  const type = imageType(bytes)
  if ((type === 'png' || type === 'jpg' || type === 'webp') && bytes.length <= 5 * 1024 * 1024) return undefined
  if (process.platform !== 'darwin' || bytes.length > 60 * 1024 * 1024) return undefined
  const out = file + '.preview.jpg'
  return promisify(execFile)('/usr/bin/sips', ['-s', 'format', 'jpeg', '-s', 'formatOptions', '80', '-Z', '2048', file, '--out', out], { timeout: 60_000 })
    .then(() => out, () => undefined)
}

/** One platform answer to a request, or the error that stood in for it. */
export type Answer = { status: number; body: any; text: string } | { error: Error }

export async function call(url: string, init: RequestInit, timeoutMs: number): Promise<Answer> {
  try {
    const response = await fetch(url, { ...init, signal: AbortSignal.timeout(timeoutMs) })
    const text = await response.text()
    let body: any
    try { body = JSON.parse(text) } catch { /* an answer nobody can read proves nothing */ }
    return { status: response.status, body, text }
  } catch (error) {
    return { error: error as Error }
  }
}

/** Errors that prove the request never reached the platform. */
const UNSENT = new Set(['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'UND_ERR_CONNECT_TIMEOUT'])

/**
 * What one answer to a message request proves (legacy core/adapters/channel-contract.mjs): a refusal is a
 * 4xx other than 409, or an answer read whole with the platform's error code; acceptance names the platform's
 * message id. Anything else — a timeout, a 5xx, an unreadable body, both an id and a refusal — may have landed.
 */
export function outcome(answer: Answer, refusal: (body: any) => string | undefined, messageId: (answer: { body: any; text: string }) => string | undefined): Omit<Receipt, 'chat' | 'at'> {
  if ('error' in answer) {
    const code = (answer.error.cause as { code?: string } | undefined)?.code ?? answer.error.name
    return UNSENT.has(code) ? { state: 'failed', reason: code } : { state: 'unknown', reason: code }
  }
  const { status, body } = answer
  const id = body && typeof body === 'object' ? messageId(answer) : undefined
  const refused = body && typeof body === 'object' ? refusal(body) : undefined
  const ok = status >= 200 && status < 300
  if ((status >= 400 && status < 500 && status !== 409) || (ok && refused)) {
    return id ? { state: 'unknown', reason: 'contradictory answer', platformId: id } : { state: 'failed', reason: refused ?? 'http ' + status }
  }
  if (!ok) return { state: 'unknown', reason: 'http ' + status, ...(id ? { platformId: id } : {}) }
  return id ? { state: 'accepted', platformId: id } : { state: 'unknown', reason: body ? 'no message id' : 'unreadable answer' }
}
