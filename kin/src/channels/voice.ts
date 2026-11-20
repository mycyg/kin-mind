/**
 * Her voice: the local speech model the old host ran (core/adapters/local-speech.mjs driving
 * core/scripts/local_speech.py, Qwen3 TTS on MLX, Ogg Opus out through ffmpeg). One lazy worker answers one
 * line of JSON per request; a delivery's audio is kept under its id so a resend does not speak it again.
 * Kept audio goes to the owner's Trash after 14 days (legacy/host/speech.mjs): a permanent delete stays hers.
 */
import { execFile, spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { createInterface } from 'node:readline'
import { promisify } from 'node:util'
import { hash } from './store.js'

const DEEPSEEK_API = 'https://api.deepseek.com'

export interface VoiceConfig {
  /** 'deepseek' for fast cloud TTS/ASR, or 'local' for the MLX Qwen3 speech worker. */
  provider?: 'deepseek' | 'local'
  /** DeepSeek TTS voice ('stella' or 'tide'). */
  voiceId?: 'stella' | 'tide' | string
  /** A Python with mlx-audio, the speech model's directory, and ffmpeg. */
  python?: string
  model?: string
  ffmpeg?: string
  /** The model's speaker (default serena), or a reference recording and its words. */
  voice?: string
  referenceAudio?: string
  referenceText?: string
}

export const KEEP_DAYS = 14

export function resolveFfmpeg(configured?: string, fallbacks: string[] = []): string {
  for (const candidate of [configured, ...fallbacks, '/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg']) {
    if (candidate && existsSync(candidate)) return candidate
  }
  return configured?.trim() || 'ffmpeg'
}

/** Synthesize natural speech WAV via DeepSeek TTS (`POST /v1/tools/tts`). */
export async function synthesizeDeepSeekWav(
  text: string,
  options: { apiKey: string; voiceId?: string; baseUrl?: string; signal?: AbortSignal },
): Promise<Buffer> {
  const clean = text.trim()
  if (!clean) throw new Error('没有可以朗读的内容')
  const url = `${(options.baseUrl ?? DEEPSEEK_API).replace(/\/$/, '')}/v1/tools/tts`
  const response = await fetch(url, {
    method: 'POST',
    headers: { authorization: `Bearer ${options.apiKey}`, 'content-type': 'application/json' },
    body: JSON.stringify({ text: clean.slice(0, 9500), voice_id: options.voiceId || 'stella', format: 'wav', filter_markdown: true }),
    signal: options.signal ?? AbortSignal.timeout(45_000),
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`DeepSeek 语音合成失败（${response.status}）${detail ? '：' + detail.slice(0, 120) : ''}`)
  }
  const wav = Buffer.from(await response.arrayBuffer())
  if (wav.length <= 44 || wav.subarray(0, 4).toString('latin1') !== 'RIFF') {
    throw new Error('DeepSeek 语音合成返回不完整')
  }
  return wav
}

/** DeepSeek TTS cached to `.ogg` (Ogg Opus) under `dir` by delivery `id` for channels and retries. */
export async function speakDeepSeek(
  text: string,
  id: string,
  dir: string,
  options: { apiKey: string; voiceId?: string; ffmpeg?: string; baseUrl?: string },
): Promise<string> {
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const file = join(dir, hash(id).slice(0, 32) + '.ogg')
  if (existsSync(file)) return file
  const wav = await synthesizeDeepSeekWav(text, options)
  const ffmpeg = resolveFfmpeg(options.ffmpeg)
  const tag = `${process.pid}.${randomUUID().slice(0, 8)}`
  const wavTmp = join(dir, `tts-${tag}.wav`)
  const oggTmp = join(dir, `tts-${tag}.ogg`)
  writeFileSync(wavTmp, wav, { mode: 0o600 })
  try {
    await promisify(execFile)(ffmpeg, ['-nostdin', '-y', '-loglevel', 'error', '-f', 'wav', '-i', wavTmp, '-c:a', 'libopus', '-b:a', '32k', '-ar', '48000', '-ac', '1', oggTmp], {
      timeout: 60_000,
    })
    renameSync(oggTmp, file)
    return file
  } finally {
    rmSync(wavTmp, { force: true })
    rmSync(oggTmp, { force: true })
  }
}

/** Transcribe audio (path or raw bytes) via DeepSeek ASR (`POST /v1/tools/asr`), converting containers with ffmpeg when needed. */
export async function hearDeepSeek(
  fileOrBytes: string | Buffer,
  options: { apiKey: string; ffmpeg?: string; baseUrl?: string; workDir?: string },
): Promise<string | undefined> {
  const raw = typeof fileOrBytes === 'string' ? readFileSync(fileOrBytes) : fileOrBytes
  if (!raw.length) return undefined
  const head = raw.subarray(0, 4).toString('latin1')
  const isOgg = head === 'OggS'
  const isPcmWav = head === 'RIFF' && raw.length >= 36 && raw.subarray(8, 12).toString('latin1') === 'WAVE' && raw.readUInt16LE(34) === 16
  let uploadBytes = raw
  let filename = isOgg ? 'audio.ogg' : 'audio.wav'
  let mime = isOgg ? 'audio/ogg' : 'audio/wav'
  const scratch: string[] = []
  try {
    if (!isOgg && !isPcmWav) {
      const dir = options.workDir ?? tmpdir()
      mkdirSync(dir, { recursive: true, mode: 0o700 })
      const base = join(dir, `asr-${process.pid}-${randomUUID().slice(0, 8)}`)
      const inFile = typeof fileOrBytes === 'string' ? fileOrBytes : `${base}.bin`
      if (typeof fileOrBytes !== 'string') {
        writeFileSync(inFile, raw, { mode: 0o600 })
        scratch.push(inFile)
      }
      const outWav = `${base}.wav`
      scratch.push(outWav)
      const ffmpeg = resolveFfmpeg(options.ffmpeg)
      await promisify(execFile)(ffmpeg, ['-nostdin', '-y', '-loglevel', 'error', '-i', inFile, '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-f', 'wav', outWav], {
        timeout: 60_000,
      })
      uploadBytes = readFileSync(outWav)
      filename = 'audio.wav'
      mime = 'audio/wav'
    }
    const form = new FormData()
    form.set('file', new Blob([new Uint8Array(uploadBytes)], { type: mime }), filename)
    const url = `${(options.baseUrl ?? DEEPSEEK_API).replace(/\/$/, '')}/v1/tools/asr`
    const response = await fetch(url, {
      method: 'POST',
      headers: { authorization: `Bearer ${options.apiKey}` },
      body: form,
      signal: AbortSignal.timeout(60_000),
    })
    if (!response.ok) {
      const detail = await response.text().catch(() => '')
      throw new Error(`DeepSeek 语音识别失败（${response.status}）${detail ? '：' + detail.slice(0, 120) : ''}`)
    }
    const answer = await response.json() as { text?: string }
    return answer.text?.trim() || undefined
  } finally {
    for (const file of scratch) rmSync(file, { force: true })
  }
}

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
    const worker = spawn(this.config.python!, ['-u', this.script, this.config.model!, this.config.ffmpeg!], {
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
