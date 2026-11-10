/**
 * Her voice: the local speech model the old host ran (core/adapters/local-speech.mjs driving
 * core/scripts/local_speech.py, Qwen3 TTS on MLX, Ogg Opus out through ffmpeg). One lazy worker answers one
 * line of JSON per request; a delivery's audio is kept under its id so a resend does not speak it again.
 * Kept audio goes to the owner's Trash after 14 days (legacy/host/speech.mjs): a permanent delete stays hers.
 */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, renameSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { basename, join } from 'node:path'
import { createInterface } from 'node:readline'
import { hash } from './store.js'

export interface VoiceConfig {
  /** A Python with mlx-audio, the speech model's directory, and ffmpeg. */
  python: string
  model: string
  ffmpeg: string
  /** The model's speaker (default serena), or a reference recording and its words. */
  voice?: string
  referenceAudio?: string
  referenceText?: string
}

export const KEEP_DAYS = 14

export class Voice {
  private worker?: ChildProcessWithoutNullStreams
  private waiting?: (answer: { ok: boolean; stage?: string }) => void
  private queue: Promise<unknown> = Promise.resolve()

  constructor(private readonly config: VoiceConfig, private readonly dir: string, private readonly script: string) {}

  speak(text: string, id: string, style = '') {
    const run = async () => {
      mkdirSync(this.dir, { recursive: true, mode: 0o700 })
      const file = join(this.dir, hash(id).slice(0, 32) + '.ogg')
      if (existsSync(file)) return file
      const answer = await new Promise<{ ok: boolean; stage?: string }>((resolve, reject) => {
        const timer = setTimeout(() => {
          // Let go of the stuck worker before it is gone: the next request starts one of its own, and nothing
          // the old one still says is taken for that request's answer.
          const stuck = this.worker
          this.worker = undefined
          this.waiting = undefined
          stuck?.kill()
          reject(new Error('她的声音三分钟没合成出来'))
        }, 180_000)
        this.waiting = (value) => { clearTimeout(timer); resolve(value) }
        this.start().stdin.write(JSON.stringify({
          text, style, voice: this.config.voice ?? 'serena', referenceAudio: this.config.referenceAudio,
          referenceText: this.config.referenceText, generation: {}, file,
        }) + '\n')
      })
      if (!answer.ok) throw new Error(`她的声音没合成出来（${answer.stage ?? '未知'}）`)
      return file
    }
    const result = this.queue.then(run)
    this.queue = result.catch(() => {})
    return result
  }

  close() {
    this.worker?.stdin.end()
  }

  private start() {
    if (this.worker) return this.worker
    const env = Object.fromEntries(['PATH', 'HOME', 'TMPDIR', 'LANG'].filter((key) => process.env[key]).map((key) => [key, process.env[key]!]))
    const worker = spawn(this.config.python, ['-u', this.script, this.config.model, this.config.ffmpeg], {
      env: { ...env, HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1', PYTHONDONTWRITEBYTECODE: '1', TOKENIZERS_PARALLELISM: 'false' },
      stdio: ['pipe', 'pipe', 'ignore'],
    }) as unknown as ChildProcessWithoutNullStreams
    createInterface({ input: worker.stdout }).on('line', (line) => {
      if (this.worker !== worker) return
      const waiting = this.waiting
      this.waiting = undefined
      try { waiting?.(JSON.parse(line)) } catch { waiting?.({ ok: false, stage: 'protocol' }) }
    })
    const gone = () => {
      if (this.worker !== worker) return
      this.worker = undefined
      this.waiting?.({ ok: false, stage: 'worker stopped' })
      this.waiting = undefined
    }
    worker.on('exit', gone)
    worker.on('error', gone)
    return (this.worker = worker)
  }
}

/** Audio older than `days` moves into the owner's Trash, one folder a pass; nothing is deleted. Returns how many files moved. */
export function sweepVoice(dir: string, { days = KEEP_DAYS, trash = join(homedir(), '.Trash'), now = Date.now() } = {}) {
  if (!existsSync(dir)) return 0
  const old = readdirSync(dir).filter((name) => name.endsWith('.ogg') && statSync(join(dir, name)).mtimeMs < now - days * 86_400_000)
  if (!old.length) return 0
  const holder = mkdtempSync(join(dir, 'kin-voice-'))
  for (const name of old) renameSync(join(dir, name), join(holder, name))
  renameSync(holder, join(trash, basename(holder)))
  return old.length
}

/** How long an Ogg Opus file plays, in ms: the last page's granule position less the header's pre-skip, at 48 kHz. */
export function oggDuration(bytes: Uint8Array) {
  const data = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const head = data.indexOf('OpusHead')
  const last = data.lastIndexOf('OggS')
  if (head < 0 || last < 0 || last + 14 > data.length) return undefined
  const samples = Number(data.readBigUInt64LE(last + 6)) - data.readUInt16LE(head + 10)
  return samples > 0 ? Math.round(samples / 48) : undefined
}
