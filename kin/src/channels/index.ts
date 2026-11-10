/**
 * KIN's channels: the WeChat and Feishu accounts on her page, the transports they start, her voice and her
 * stickers, all shared as the `kinTransports` service. Accounts (with the Feishu secret and the WeChat login)
 * live in `<dataDir>/channels.json`, readable by the owner only; each transport keeps its own state and
 * received media under `<dataDir>/channels/<id>/`.
 */
import { execFile } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { promisify } from 'node:util'
import { chmodSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Host } from '../index.js'
import { Feishu, type FeishuAccount } from './feishu.js'
import { markUsed, stickers } from './stickers.js'
import type { Inbound, Transport, Transports } from './transport.js'
import { sweepVoice, Voice, type VoiceConfig } from './voice.js'
import { WeChat, type WeChatAccount } from './wechat.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    kinTransports: Transports
  }
}

interface Accounts {
  wechat?: WeChatAccount
  feishu?: FeishuAccount
  voice?: VoiceConfig
  hearing?: { python: string; model?: string }
}

type Id = 'wechat' | 'feishu'

const address = (value: unknown): value is string => typeof value === 'string' && /^https?:\/\/\S+$/.test(value.trim())

export function channels(host: Host) {
  const accounts = host.load<Accounts>('channels.json', {})
  const save = () => {
    const file = host.file('channels.json')
    writeFileSync(file, JSON.stringify(accounts, null, 2) + '\n', { mode: 0o600 })
    chmodSync(file, 0o600)
  }

  const running = new Map<Id, Transport>()
  const listeners = new Set<(message: Inbound) => void>()
  // What arrives before anyone listens waits for the first listener rather than being lost.
  const early: Inbound[] = []
  const arrive = (message: Inbound) => {
    if (!listeners.size) return void early.push(message)
    for (const listener of listeners) {
      try {
        listener(message)
      } catch (error) {
        host.log(`a channel listener failed on ${message.transport} ${message.id}: ${(error as Error).message}`)
      }
    }
  }

  const open = async (id: Id) => {
    await running.get(id)?.close()
    running.delete(id)
    const dir = host.file(join('channels', id))
    const transport = id === 'wechat'
      ? accounts.wechat && new WeChat(accounts.wechat, dir, save, host.log)
      : accounts.feishu && new Feishu(accounts.feishu, dir, host.log)
    if (!transport) return
    running.set(id, transport)
    await transport.start(arrive).catch((error: Error) => host.log(`${id} did not start: ${error.message}`))
  }
  void open('wechat')
  void open('feishu')

  const voiceDir = host.file('voice')
  let voice: Voice | undefined
  const sweep = () => {
    try {
      const moved = sweepVoice(voiceDir)
      if (moved) host.log(`moved ${moved} voice file(s) older than 14 days to the Trash`)
    } catch (error) {
      host.log(`old voice files not swept: ${(error as Error).message}`)
    }
  }
  sweep()
  const daily = setInterval(sweep, 86_400_000)
  daily.unref()

  host.ctx.effect(() => () => {
    clearInterval(daily)
    voice?.close()
    for (const transport of running.values()) void transport.close()
  }, 'kin channels')

  const transports = {
    get: (id) => running.get(id as Id),
    onInbound(listener) {
      listeners.add(listener)
      for (const message of early.splice(0)) arrive(message)
      return () => void listeners.delete(listener)
    },
    async hear(file) {
      if (!accounts.hearing) return undefined
      const { stdout } = await promisify(execFile)(accounts.hearing.python,
        [join(host.config.coreSource, '../scripts/transcribe.py'), file], {
          timeout: 180_000,
          env: { ...process.env, HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1',
            ...(accounts.hearing.model ? { KIN_ASR_MODEL: accounts.hearing.model } : {}) },
        })
      const line = stdout.split('\n').find((line) => line.startsWith(file + '\t'))
      const [_, duration, ...words] = line?.split('\t') ?? []
      if (!line || duration === 'ERR') throw new Error('本地语音转写失败')
      return words.join('\t').trim() || undefined
    },
    speak(text, id, style) {
      if (!accounts.voice) return Promise.reject(new Error('她的声音还没设好'))
      voice ??= new Voice(accounts.voice, voiceDir, join(host.config.coreSource, '../scripts/local_speech.py'))
      return voice.speak(text, id, style)
    },
    stickers: (words) => stickers(host.file('memes'), words),
    usedSticker: (id) => markUsed(host.file('memes'), id),
  } satisfies Transports
  host.ctx.provide('kinTransports', transports)

  // Her chat box's microphone (client/Chat.tsx): whether she can hear here, and one recording heard the way a voice
  // message is. It waits in <dataDir>/hearing/ only while it is being heard.
  host.route('/api/kin/hear', {
    GET: async () => ({ hearing: Boolean(accounts.hearing) }),
    POST: async (request) => {
      const bytes = Buffer.from(await request.arrayBuffer())
      const dir = host.file('hearing')
      mkdirSync(dir, { recursive: true, mode: 0o700 })
      const file = join(dir, randomUUID())
      writeFileSync(file, bytes, { mode: 0o600 })
      try {
        // Ogg Opus, WAV and FLAC are read as they are; what else a browser records (WebM, MP4) goes through her voice's ffmpeg first.
        let heard = file
        if (!['OggS', 'RIFF', 'fLaC'].includes(bytes.toString('latin1', 0, 4))) {
          if (!accounts.voice?.ffmpeg) throw new Error('录下的声音得先用 ffmpeg 转一下，可还没设 ffmpeg（在设置的「她的声音」里填）')
          heard = file + '.wav'
          await promisify(execFile)(accounts.voice.ffmpeg, ['-nostdin', '-loglevel', 'error', '-i', file, '-ac', '1', '-ar', '16000', heard], { timeout: 60_000 })
            .catch((error: Error) => {
              host.log(`a dictation was not converted: ${error.message}`)
              throw new Error('ffmpeg 没能把这段录音转过来')
            })
        }
        const text = await transports.hear(heard).catch((error: Error) => {
          host.log(`a dictation was not heard: ${error.message}`)
          throw new Error('本地听写出了错，再试一次吧')
        })
        return { text: text ?? '' }
      } finally {
        rmSync(file, { force: true })
        rmSync(file + '.wav', { force: true })
      }
    },
  })

  // Her page: each account's status, never its secret.
  const view = () => {
    const status = (id: Id) => running.get(id)?.status() ?? { state: 'stopped' as const }
    const { wechat, feishu, voice: speech } = accounts
    return {
      wechat: wechat ? { baseUrl: wechat.baseUrl, cdnBaseUrl: wechat.cdnBaseUrl, userId: wechat.userId ?? null, status: status('wechat') } : null,
      feishu: feishu ? { appId: feishu.appId, domain: feishu.domain, status: status('feishu') } : null,
      voice: speech ?? null,
    }
  }
  host.route('/api/kin/channels', { GET: async () => view() })
  host.route('/api/kin/channels/wechat', {
    POST: async (request) => {
      const { baseUrl, cdnBaseUrl } = await host.body<WeChatAccount>(request)
      if (!address(baseUrl) || !address(cdnBaseUrl)) throw new Error('两个地址都要以 http:// 或 https:// 开头')
      accounts.wechat = { ...accounts.wechat, baseUrl: baseUrl.trim(), cdnBaseUrl: cdnBaseUrl.trim() }
      save()
      await open('wechat')
      return view()
    },
  })
  host.route('/api/kin/channels/wechat/login', {
    POST: async () => {
      const wechat = running.get('wechat') as WeChat | undefined
      if (!wechat) throw new Error('先填好微信的地址')
      await wechat.login()
      return view()
    },
  })
  host.route('/api/kin/channels/feishu', {
    POST: async (request) => {
      const { appId, appSecret, domain } = await host.body<FeishuAccount>(request)
      // A secret left empty keeps the one already saved.
      const secret = appSecret?.trim() || accounts.feishu?.appSecret
      if (!appId?.trim() || !secret || !address(domain)) throw new Error('App ID、App Secret 和地址都要填')
      accounts.feishu = { appId: appId.trim(), appSecret: secret, domain: domain.trim() }
      save()
      await open('feishu')
      return view()
    },
  })
  host.route('/api/kin/channels/voice', {
    POST: async (request) => {
      const { python, model, ffmpeg, voice: speaker } = await host.body<VoiceConfig>(request)
      voice?.close()
      voice = undefined
      if (python?.trim() || model?.trim() || ffmpeg?.trim()) {
        if (!python?.trim() || !model?.trim() || !ffmpeg?.trim()) throw new Error('Python、模型和 ffmpeg 三个路径都要填')
        accounts.voice = { ...accounts.voice, python: python.trim(), model: model.trim(), ffmpeg: ffmpeg.trim(), ...(speaker?.trim() ? { voice: speaker.trim() } : {}) }
      } else delete accounts.voice
      save()
      return view()
    },
  })
  host.route('/api/kin/channels/remove', {
    POST: async (request) => {
      const { id } = await host.body<{ id: Id }>(request)
      if (id !== 'wechat' && id !== 'feishu') throw new Error('没有这个渠道')
      await running.get(id)?.close()
      running.delete(id)
      delete accounts[id]
      save()
      return view()
    },
  })
}
