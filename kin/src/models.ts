/**
 * KIN's models: two virtual routes over the model routes DSH already has, each an ordered list.
 * - `kin/chat`: her replies in conversation, where feeling matters.
 * - `kin/task`: everything else she does — helpers, exploration, reviews and background work.
 * A call tries its lane's routes in order and moves on while a route has not started answering:
 * an error, or no first chunk within a minute. Once a route answers it keeps the turn.
 * An empty lane speaks with DSH's default model. Credentials stay in DSH.
 */
import { answerText, modelOutput } from './model-output.js'
import type { Context } from '@deepseek-ai/cordis'
import {
  LlmAdapter, ReasoningEffortId, type GenerateOptions, type LlmFailure, type LlmResolvedModelInfo, type StreamChunk,
} from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-agent-default-model'

export const PROVIDER = 'kin'
export const LANES = ['chat', 'task'] as const
export type Lane = (typeof LANES)[number]
const NAMES: Record<Lane, string> = { chat: 'KIN 聊天', task: 'KIN 做事' }
const FIRST_CHUNK_MS = 60_000

export interface Route {
  provider: string
  model: string
  /** Reasoning effort for this route; unset, the chat lane thinks lightly and the task lane uses the model's default. */
  effort?: string
}

/** How a lane's latest call went: which route answered and why the ones before it did not. */
export interface LastCall {
  at: string
  answered: Route | null
  failed: { route: Route; reason: string }[]
}

const laneOf = (model: string): Lane => (model === 'task' ? 'task' : 'chat')

export class KinModel extends LlmAdapter {
  readonly last: Partial<Record<Lane, LastCall>> = {}
  private readonly light = new Map<string, Promise<string | undefined>>()

  constructor(private readonly ctx: Context, private readonly configured: (lane: Lane) => Route[]) {
    super()
  }

  /**
   * The effort a route runs at. Chatting needs feeling, not long hidden reasoning, and a model left
   * at its default (DeepSeek's is `high`) reasons at length before every reply; so an unset chat
   * route asks for `low` when the model offers it. Task routes keep the model's own default.
   */
  private effortOf(lane: Lane, route: Route): Promise<string | undefined> {
    if (route.effort || lane === 'task') return Promise.resolve(route.effort)
    const key = route.provider + '/' + route.model
    if (!this.light.has(key)) {
      this.light.set(key, this.ctx.llm.resolveModelInfo(route.provider, route.model)
        .then((info) => info.reasoning?.efforts.find((effort) => effort.id === 'low')?.id as string | undefined)
        .catch(() => undefined))
    }
    return this.light.get(key)!
  }

  /** The routes a call on this lane walks. */
  routes(lane: Lane): Route[] {
    const chosen = this.configured(lane)
    if (chosen.length) return chosen
    const fallback = this.ctx.get('agentDefaultModel')?.currentSelection()
    return fallback ? [{ provider: fallback.provider, model: fallback.model }] : []
  }

  override providerInfo() {
    return { id: PROVIDER, name: 'KIN' }
  }

  // Not advertised: DSH's model picker is for coding sessions; KIN's sessions ask for her lanes directly.
  override async listModels() {
    return []
  }

  // Any route of the lane may take the turn, so the conversation must fit the smallest of them.
  override async resolveModel(_provider: string, model: string, signal?: AbortSignal): Promise<LlmResolvedModelInfo> {
    const lane = laneOf(model)
    const settled = await Promise.allSettled(this.routes(lane).map((route) => this.ctx.llm.resolveModelInfo(route.provider, route.model, signal)))
    const infos = settled.flatMap((result) => (result.status === 'fulfilled' ? [result.value] : []))
    const windows = infos.flatMap((info) => (info.context ? [info.context.contextWindow] : []))
    const images = infos.length > 0 && infos.every((info) => info.inputModalities?.includes('image'))
    return {
      provider: PROVIDER, id: lane, name: NAMES[lane],
      inputModalities: images ? ['text', 'image'] : ['text'],
      ...(windows.length ? { context: { contextWindow: Math.min(...windows) } } : {}),
    }
  }

  async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    const lane = laneOf(options.model)
    // Older sessions can already contain the relay envelope. Do not feed it back as spoken dialogue.
    const messages = options.messages.map((message) => {
      if (message.role !== 'assistant') return message
      const content = message.content.map((block) => block.type === 'text' ? { ...block, text: answerText(block.text) } : block)
      if (content.every((block, i) => block.type !== 'text' || block.text === (message.content[i] as { text: string }).text)) return message
      return { ...message, content, source: { ...message.source, replayState: undefined } }
    })
    const failed: LastCall['failed'] = []
    let failure: LlmFailure | undefined
    for (const route of this.routes(lane)) {
      if (options.signal?.aborted) return
      const attempt = new AbortController()
      const stop = () => attempt.abort()
      options.signal?.addEventListener('abort', stop, { once: true })
      const timer = setTimeout(stop, FIRST_CHUNK_MS)
      const held: StreamChunk[] = []
      let answering = false
      failure = undefined
      const effort = await this.effortOf(lane, route)
      try {
        for await (const chunk of modelOutput(this.ctx.llm.stream({
          ...options, messages, provider: route.provider, model: route.model, signal: attempt.signal,
          reasoningEffort: effort ? ReasoningEffortId(effort) : undefined,
        }))) {
          if (!answering) {
            if (chunk.type === 'finish' && (chunk.reason.kind === 'error' || chunk.reason.kind === 'aborted')) {
              failure = chunk.reason.failure
              break
            }
            if (chunk.type === 'finish') break
            // Thinking and incomplete tool arguments have not answered or executed anything yet.
            if (chunk.type !== 'usage') clearTimeout(timer)
            const visible = (chunk.type === 'text-delta' && Boolean(chunk.text))
              || (chunk.type === 'block-end' && (chunk.block.type === 'tool-call' || (chunk.block.type === 'text' && Boolean(chunk.block.text))))
            if (!visible) {
              held.push(chunk)
              continue
            }
            answering = true
            clearTimeout(timer)
            yield* held
          }
          yield chunk
        }
        if (!answering && !failure) failure = { code: 'NO_RESPONSE', message: '模型没有返回内容' }
      } catch (error) {
        if (answering) throw error
        failure = { code: 'ROUTE_FAILED', message: (error as Error).message }
      } finally {
        clearTimeout(timer)
        options.signal?.removeEventListener('abort', stop)
      }
      if (options.signal?.aborted) {
        if (failure) yield { type: 'finish', reason: { kind: 'aborted', failure } }
        return
      }
      if (!failure) {
        this.last[lane] = { at: new Date().toISOString(), answered: route, failed }
        return
      }
      failed.push({ route, reason: attempt.signal.aborted ? '一分钟没有回应' : describe(failure) })
    }
    this.last[lane] = { at: new Date().toISOString(), answered: null, failed }
    yield {
      type: 'finish',
      reason: { kind: 'error', failure: failure ?? { code: 'NO_ROUTE', message: 'KIN 还没有可用的模型路线' } },
    }
  }
}

/** How one route answers a tiny call of its own: whether it started answering, and how long that took. */
export async function probe(ctx: Context, route: Route): Promise<{ ok: boolean; ms: number; reason?: string }> {
  const started = Date.now()
  const ms = () => Date.now() - started
  try {
    for await (const chunk of ctx.llm.stream({
      provider: route.provider, model: route.model, reasoningEffort: route.effort ? ReasoningEffortId(route.effort) : undefined,
      messages: [{ role: 'user', content: [{ type: 'text', text: '只回复：连通成功' }] }], maxTokens: 4096, signal: AbortSignal.timeout(FIRST_CHUNK_MS),
    })) {
      if (chunk.type === 'usage') continue
      if (chunk.type === 'finish' && (chunk.reason.kind === 'error' || chunk.reason.kind === 'aborted')) {
        return { ok: false, ms: ms(), reason: chunk.reason.failure ? describe(chunk.reason.failure) : '一分钟没有回应' }
      }
      if (chunk.type === 'finish') break
      return { ok: true, ms: ms() }
    }
    return { ok: false, ms: ms(), reason: '没有回应' }
  } catch (error) {
    return { ok: false, ms: ms(), reason: (error as Error).message.slice(0, 200) }
  }
}

function describe(failure: LlmFailure): string {
  if (failure.status) return `${failure.status} ${failure.message}`.slice(0, 200)
  return failure.message.slice(0, 200) || failure.code
}
