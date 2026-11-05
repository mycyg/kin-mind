/**
 * One messaging platform (WeChat, Feishu) as KIN's conversations see it. A transport only moves messages:
 * who is speaking, which conversation it belongs to and what she says back are decided elsewhere.
 */

/** A chat on the platform: a person's direct chat or a group. */
export interface Address {
  chat: string
  kind: 'direct' | 'group'
}

/** Something that arrived. Media is already saved to a local file (voice with its transcript where there is one). */
export interface Inbound extends Address {
  transport: string
  /** The platform's own message id: the same message arriving twice is recognised by it. */
  id: string
  /** Who said it, in the platform's own user id, and the name the platform shows. */
  sender: string
  senderName?: string
  /** In a group: whether she was @-mentioned. */
  mentioned?: boolean
  text?: string
  files?: {
    path: string
    kind: 'image' | 'voice' | 'video' | 'file'
    name?: string
    transcript?: string
    /** A JPEG a model can look at, for an image that is not one itself (too large, or HEIC, GIF…). */
    preview?: string
  }[]
  at: string
}

/**
 * One piece of what she sends. A long message may go out as several. `named`: the owner asked for this
 * very file; WeChat sends a `file` or `video` part only when so (who asked is decided by the caller).
 */
export type Part = { text: string } | { file: string; kind: 'image' | 'voice' | 'video' | 'file'; named?: boolean }

/**
 * What became of a send. `unknown` (the request may or may not have landed) is never resent under a new id:
 * it is checked first, then retried under that same id within the delivery retry budget.
 */
export interface Sent {
  state: 'accepted' | 'unknown' | 'failed'
  platformId?: string
  reason?: string
}

export interface Transport {
  readonly id: string
  /** Starts listening (and catching up on what was missed); every arrival goes to `inbound`. */
  start(inbound: (message: Inbound) => void): Promise<void>
  close(): Promise<void>
  /** Sends one part. `id` is the delivery's own id, so a retry of the same part is recognised, not sent twice. */
  send(to: Address, part: Part, id: string, retryUnknown?: boolean): Promise<Sent>
  /** What became of a send whose outcome was unknown, where the platform can say. */
  check?(to: Address, id: string): Promise<Sent>
  /** "Typing…" while she writes, where the platform has it. */
  typing?(to: Address, on: boolean): Promise<void>
  /** For her page: connected or not, logged in or waiting for a QR scan, and why. */
  status(): { state: 'connected' | 'connecting' | 'login-required' | 'stopped' | 'failed'; reason?: string; qr?: string }
}

/** A sticker from her meme library, ready to go out as an `image` part. */
export interface Sticker {
  id: string
  file: string
  description: string
}

/** The running transports, provided by the channels module as the `kinTransports` cordis service. */
export interface Transports {
  get(id: string): Transport | undefined
  /** Every arrival on any transport; returns a function that stops listening. */
  onInbound(listener: (message: Inbound) => void): () => void
  /**
   * Her voice saying `text`, as an Ogg Opus file for a `voice` part (the same `id` gives the same file); `style` is
   * how she says it, in words the speech model follows. Rejects when no voice is set up.
   */
  /** Locally transcribe incoming voice when hearing is configured. */
  hear?(file: string): Promise<string | undefined>
  speak(text: string, id: string, style?: string): Promise<string>
  /** Stickers from her meme library that fit `words`, best first. */
  stickers(words: string): Sticker[]
  /** A sticker she sent: among equally fitting ones, the least recently used goes first. */
  usedSticker(id: string): void
}
