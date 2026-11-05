/**
 * Feishu's long connection: events are pushed over a WebSocket the bot opens itself, so no public address is
 * needed. The protocol is the one the official SDK speaks (legacy listener ran it through @larksuiteoapi/node-sdk):
 * ask `/callback/ws/endpoint` for a URL, then binary protobuf `Frame`s — pings from us, events from them, and an
 * answer to every event frame. Ported without the SDK: the frame is nine fields.
 */

/** A protobuf field: varints as bigint, length-delimited fields as bytes. */
export type Field = [number, bigint | Uint8Array]

export function decode(bytes: Uint8Array): Field[] {
  const fields: Field[] = []
  let at = 0
  const varint = () => {
    let value = 0n
    for (let shift = 0n; ; shift += 7n) {
      const byte = bytes[at++]
      value |= BigInt(byte & 0x7f) << shift
      if (byte < 0x80) return value
    }
  }
  while (at < bytes.length) {
    const key = Number(varint())
    if ((key & 7) === 0) fields.push([key >> 3, varint()])
    else if ((key & 7) === 2) {
      const length = Number(varint())
      fields.push([key >> 3, bytes.subarray(at, (at += length))])
    } else throw new Error('unexpected protobuf wire type ' + (key & 7))
  }
  return fields
}

export function encode(fields: Field[]): Uint8Array {
  const parts: Uint8Array[] = []
  const varint = (value: bigint) => {
    const out: number[] = []
    do {
      const byte = Number(value & 0x7fn)
      value >>= 7n
      out.push(value ? byte | 0x80 : byte)
    } while (value)
    parts.push(Uint8Array.from(out))
  }
  for (const [number, value] of fields) {
    if (typeof value === 'bigint') {
      varint(BigInt(number << 3))
      varint(value)
    } else {
      varint(BigInt((number << 3) | 2))
      varint(BigInt(value.length))
      parts.push(value)
    }
  }
  return Buffer.concat(parts)
}

// Frame: 1 SeqID, 2 LogID, 3 service, 4 method (0 control, 1 data), 5 headers {1 key, 2 value}, 8 payload.
const text = new TextDecoder()
const bytes = (value: string) => new TextEncoder().encode(value)
export const header = (key: string, value: string): Field => [5, encode([[1, bytes(key)], [2, bytes(value)]])]
export function headersOf(frame: Field[]) {
  return Object.fromEntries(frame.filter(([n]) => n === 5).map(([, value]) => {
    const pair = decode(value as Uint8Array)
    return [text.decode(pair[0][1] as Uint8Array), text.decode(pair[1][1] as Uint8Array)]
  })) as Record<string, string>
}

export interface Socket {
  close(): void
}

/**
 * Keeps the long connection up, reconnecting after a drop (5 s, then up to 2 min apart), and hands every pushed
 * event to `onEvent`. A rejected `onEvent` answers the frame with 500, so the platform delivers it again.
 */
export function connect({ domain, appId, appSecret, onEvent, onState }: {
  domain: string
  appId: string
  appSecret: string
  onEvent: (event: any) => Promise<void>
  onState: (state: 'connected' | 'connecting' | 'failed', reason?: string) => void
}): Socket {
  let closed = false
  let socket: WebSocket | undefined
  let wake = () => {}

  const session = (url: string, pingMs: number) => new Promise<void>((done) => {
    const service = BigInt(new URL(url).searchParams.get('service_id') ?? 0)
    const ws = (socket = new WebSocket(url))
    ws.binaryType = 'arraybuffer'
    const parts = new Map<string, Uint8Array[]>()
    let ping: NodeJS.Timeout | undefined
    ws.onopen = () => {
      onState('connected')
      ping = setInterval(() => ws.send(encode([[1, 0n], [2, 0n], [3, service], [4, 0n], header('type', 'ping')])), pingMs)
    }
    const receive = async (frame: Field[]) => {
      const headers = headersOf(frame)
      if (frame.find(([n]) => n === 4)?.[1] !== 1n || headers.type !== 'event') return
      // A large event comes in `sum` parts under one message id.
      const sum = Number(headers.sum ?? 1)
      const got = parts.get(headers.message_id) ?? new Array<Uint8Array>(sum)
      got[Number(headers.seq ?? 0)] = (frame.find(([n]) => n === 8)?.[1] as Uint8Array | undefined) ?? new Uint8Array()
      if (got.filter(Boolean).length < sum) return void parts.set(headers.message_id, got)
      parts.delete(headers.message_id)
      const started = Date.now()
      const code = await onEvent(JSON.parse(text.decode(Buffer.concat(got)))).then(() => 200, () => 500)
      ws.send(encode([...frame.filter(([n]) => n !== 8), header('biz_rt', String(Date.now() - started)), [8, bytes(JSON.stringify({ code }))]]))
    }
    // A frame nobody can read is left unanswered: the platform sends that event again.
    ws.onmessage = ({ data }) => void Promise.resolve().then(() => receive(decode(new Uint8Array(data as ArrayBuffer)))).catch(() => {})
    ws.onclose = () => {
      clearInterval(ping)
      done()
    }
  })

  void (async () => {
    for (let failures = 0; !closed; ) {
      onState('connecting')
      try {
        const response = await fetch(domain.replace(/\/$/, '') + '/callback/ws/endpoint', {
          method: 'POST', headers: { 'content-type': 'application/json', locale: 'zh' },
          body: JSON.stringify({ AppID: appId, AppSecret: appSecret }), signal: AbortSignal.timeout(15_000),
        })
        const answer = await response.json() as { code: number; msg?: string; data?: { URL: string; ClientConfig?: { PingInterval?: number } } }
        if (answer.code !== 0 || !answer.data) throw new Error(`飞书不给长连接：${answer.code} ${answer.msg ?? ''}`.trim())
        const opened = Date.now()
        await session(answer.data.URL, (answer.data.ClientConfig?.PingInterval ?? 120) * 1000)
        failures = Date.now() - opened > 60_000 ? 0 : failures + 1
        if (!closed) onState('connecting', '长连接断了，正在重连')
      } catch (error) {
        failures++
        onState('failed', (error as Error).message)
      }
      if (closed) break
      await new Promise<void>((resume) => {
        const timer = setTimeout(resume, Math.min(5_000 * 2 ** Math.max(failures - 1, 0), 120_000))
        wake = () => { clearTimeout(timer); resume() }
      })
    }
  })()

  return {
    close() {
      closed = true
      socket?.close()
      wake()
    },
  }
}
