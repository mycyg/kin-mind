/**
 * Core's own model calls, answered by KIN's task lane. Core speaks the Anthropic Messages API to a
 * relay named in its model file (core/src/kin_mind/model_config.py); this is that relay, on a
 * loopback port with a key made fresh at every start. So her reviews, diary pages, dreams and
 * memory work walk the same fallback routes as her helpers, and DSH keeps every credential.
 */
import type { Context } from '@deepseek-ai/cordis'
import { admitEncodedImages, type ImageMediaType } from '@deepseek-ai/dsh-attachment'
import {
  BlockAssembler, ToolCallId, createAssistantMessage, createToolResultMessage,
  type ContentBlock, type GenerateOptions, type RequestMessage, type ToolSchema,
} from '@deepseek-ai/dsh-llm'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { dirname, join } from 'node:path'
import { LANES, PROVIDER, type Lane } from './models.js'

const MAX_BODY = 32 * 1024 * 1024

export interface Bridge {
  /** The relay's base URL, once it listens. */
  readonly url: Promise<string>
  readonly key: string
  /**
   * Another loopback route for Core beside its model calls (e.g. `/v1/kin/helper`), under the same key.
   * The handler answers with JSON; its signal aborts when Core stops waiting. Returns the disposer.
   */
  serve(path: string, handler: BridgeRoute): () => void
  close(): void
}

type BridgeRoute = (body: Record<string, any>, signal: AbortSignal) => Promise<unknown>

export function startBridge(ctx: Context, log: (message: string) => void): Bridge {
  const key = randomBytes(32).toString('hex')
  const routes = new Map<string, BridgeRoute>()
  const server = createServer((request, response) => {
    answer(ctx, key, routes, request, response).catch((error: Error) => {
      log('model bridge: ' + error.message)
      if (!response.headersSent) fail(response, 500, 'api_error', 'bridge error')
    })
  })
  const url = new Promise<string>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve(`http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`))
  })
  return {
    url, key,
    serve(path, handler) {
      routes.set(path, handler)
      return () => routes.delete(path)
    },
    close: () => void server.close(),
  }
}

async function answer(ctx: Context, key: string, routes: Map<string, BridgeRoute>, request: IncomingMessage, response: ServerResponse) {
  const route = routes.get(request.url ?? '')
  if (request.method !== 'POST' || (request.url !== '/v1/messages' && !route)) return fail(response, 404, 'not_found_error', 'not found')
  const given = Buffer.from(String(request.headers['x-api-key'] ?? ''))
  if (given.length !== key.length || !timingSafeEqual(given, Buffer.from(key))) return fail(response, 401, 'authentication_error', 'invalid key')
  const body = await readJson(request)
  if (!body) return fail(response, 400, 'invalid_request_error', 'the body is not a JSON object')
  if (route) {
    const gone = new AbortController()
    response.on('close', () => { if (!response.writableFinished) gone.abort() })
    return send(response, 200, await route(body, gone.signal))
  }
  const lane = body.model as Lane
  if (!LANES.includes(lane)) return fail(response, 404, 'not_found_error', 'unknown model')

  let call: Pick<GenerateOptions, 'system' | 'messages' | 'tools' | 'maxTokens'>
  try {
    call = await translate(ctx, lane, body)
  } catch (error) {
    return fail(response, 400, 'invalid_request_error', (error as Error).message)
  }
  // Core gave up on this call (its own timeout): stop the model too.
  const stop = new AbortController()
  response.on('close', () => { if (!response.writableFinished) stop.abort() })
  // The route's own effort setting decides how hard it thinks (models.ts), not Core's request.
  const assembler = new BlockAssembler()
  for await (const chunk of ctx.llm.stream({ ...call, provider: PROVIDER, model: lane, signal: stop.signal })) assembler.push(chunk)
  const finish = assembler.finish
  if (finish.kind === 'error' || finish.kind === 'aborted') return fail(response, 502, 'api_error', finish.failure.message)
  const usage = assembler.usage
  const id = 'msg_' + randomBytes(12).toString('hex')
  send(response, 200, {
    id,
    type: 'message',
    role: 'assistant',
    // The name Core asked for: what answered is the lane's business (models.ts records it).
    model: lane,
    content: assembler.blocks().flatMap(toAnthropic),
    stop_reason: finish.kind === 'tool-calls' ? 'tool_use' : finish.kind === 'max-tokens' ? 'max_tokens' : 'end_turn',
    usage: usage ? {
      input_tokens: usage.inputTokens,
      output_tokens: usage.outputTokens,
      ...(usage.cacheReadTokens ? { cache_read_input_tokens: usage.cacheReadTokens } : {}),
      ...(usage.cacheWriteTokens ? { cache_creation_input_tokens: usage.cacheWriteTokens } : {}),
    } : undefined,
    // The host confirms the call ran on her own lane (model_runtime.verified_decision): what her reviews
    // decide this way, a wish to reach out among them, is hers to act on.
    native_receipt: {
      provider: PROVIDER, model: lane, reasoning: 'lane-default', native_turn_id: id, native_session_id: 'kin-' + lane,
      verified_at: new Date().toISOString(),
    },
  })
}

type Block = Record<string, any>

/** Core's Messages request as DSH sees a one-shot call. Thinking comes back as thinking, so a tool loop replays it. */
async function translate(ctx: Context, lane: Lane, body: Block) {
  const system = typeof body.system === 'string' ? body.system
    : Array.isArray(body.system) ? body.system.map((block: Block) => block.text ?? '').join('\n') : undefined
  const messages: RequestMessage[] = []
  for (const message of body.messages ?? []) {
    const blocks: Block[] = typeof message.content === 'string' ? [{ type: 'text', text: message.content }] : message.content
    if (message.role === 'assistant') {
      const content = blocks.flatMap((block): ContentBlock[] => {
        if (block.type === 'text') return [{ type: 'text', text: block.text }]
        if (block.type === 'thinking') return [{ type: 'reasoning', text: block.thinking ?? '' }]
        if (block.type === 'tool_use') return [{ type: 'tool-call', id: ToolCallId(block.id), name: block.name, arguments: JSON.stringify(block.input ?? {}) }]
        return []
      })
      messages.push(createAssistantMessage({ content, source: { provider: PROVIDER, model: lane } }))
      continue
    }
    // A user turn answers the tool calls before it says anything else.
    const content: ContentBlock[] = []
    for (const block of blocks) {
      if (block.type === 'tool_result') {
        messages.push(createToolResultMessage({
          callId: ToolCallId(block.tool_use_id), content: [{ type: 'text', text: textOf(block.content) }], isError: block.is_error === true,
        }))
      } else if (block.type === 'text') {
        content.push({ type: 'text', text: block.text })
      } else if (block.type === 'image' && block.source?.type === 'base64') {
        const store = ctx.get('attachments')
        if (!store) throw new Error('images are not available in this DSH')
        const [attachment] = await admitEncodedImages(store, [{ mediaType: block.source.media_type as ImageMediaType, data: block.source.data }])
        content.push({ type: 'image', attachment })
      } else {
        throw new Error('unsupported content: ' + block.type)
      }
    }
    if (content.length) messages.push({ role: 'user', content })
  }
  const tools: ToolSchema[] | undefined = body.tools?.map((tool: Block) => ({
    name: tool.name, description: tool.description ?? '', parameters: tool.input_schema,
  }))
  return { system, messages, tools, maxTokens: typeof body.max_tokens === 'number' ? body.max_tokens : undefined }
}

function toAnthropic(block: ContentBlock): Block[] {
  if (block.type === 'text') return block.text ? [{ type: 'text', text: block.text }] : []
  if (block.type === 'reasoning') return [{ type: 'thinking', thinking: block.text, signature: '' }]
  if (block.type === 'tool-call') return [{ type: 'tool_use', id: block.id, name: block.name, input: parseArguments(block.arguments) }]
  return []
}

function parseArguments(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    // Core validates the input against its schema and repairs or retries; an unreadable one is empty.
    return {}
  }
}

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) return content.map((block: Block) => block.text ?? '').join('')
  return content === undefined ? '' : JSON.stringify(content)
}

async function readJson(request: IncomingMessage): Promise<Block | undefined> {
  const parts: Buffer[] = []
  let size = 0
  for await (const part of request) {
    size += (part as Buffer).length
    if (size > MAX_BODY) return undefined
    parts.push(part as Buffer)
  }
  try {
    const value = JSON.parse(Buffer.concat(parts).toString('utf8'))
    return value && typeof value === 'object' && !Array.isArray(value) ? value : undefined
  } catch {
    return undefined
  }
}

function send(response: ServerResponse, status: number, body: unknown) {
  response.writeHead(status, { 'content-type': 'application/json' })
  response.end(JSON.stringify(body))
}

function fail(response: ServerResponse, status: number, type: string, message: string) {
  send(response, status, { type: 'error', error: { type, message } })
}

/** Core's roles that call a model themselves (model_config.py, DIRECT_ROLES). Every one works on the task lane. */
const ROLES = [
  'memoryCompression', 'memoryRecall', 'memoryMaintenance', 'memoryStructured', 'memoryExtraction', 'memoryConflict',
  'memorySummary', 'memoryPrediction', 'memoryQuery', 'memoryRerank', 'memoryVision', 'memoryAnswer', 'memoryJudge',
]

/** Point Core's model file at the bridge: one relay, every role on the task lane, this start's key in its own file. */
export function writeModelFile(file: string, url: string, key: string) {
  const credentials = join(dirname(file), 'credentials')
  mkdirSync(credentials, { recursive: true, mode: 0o700 })
  writeFileSync(join(credentials, 'kin-bridge.env'), `RESPONSES_RELAY_API_KEY=${key}\n`, { mode: 0o600 })
  writeFileSync(file, JSON.stringify({
    version: 1,
    providers: { kin: { id: 'kin', baseUrl: url, protocol: 'anthropic-messages', credentialFile: 'kin-bridge.env', models: ['task'] } },
    roleProfiles: Object.fromEntries(ROLES.map((role) => [role, { providerId: 'kin', model: 'task' }])),
  }, null, 2) + '\n')
}
