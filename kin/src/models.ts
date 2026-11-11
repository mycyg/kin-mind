/**
 * KIN's models: two virtual routes over the model routes DSH already has, each an ordered list.
 * - `kin/chat`: her replies in conversation, where feeling matters.
 * - `kin/task`: everything else she does — helpers, exploration, reviews and background work.
 * A call tries its lane's routes in order and moves on while a route has not started answering:
 * an error, or no first chunk in time (a minute; thirty seconds for a chat route with another behind it,
 * since someone is waiting). Once a route answers it keeps the turn. A route that just failed is tried
 * after the others for a few minutes, so a route that is down does not cost every reply its wait.
 * An empty lane speaks with DSH's default model. Credentials stay in DSH.
 */
import { OUTPUT_BUDGET, estimateRequestTokens, requestThreshold } from './model-budget.js'
import { answerText, modelOutput } from './model-output.js'
import { modelRequest, rememberRoute } from './model-history.js'
import type { Context } from '@deepseek-ai/cordis'
import {
  LlmAdapter, LlmError, ReasoningEffortId, type GenerateOptions, type LlmFailure, type LlmResolvedModelInfo, type StreamChunk,
} from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import type {} from '@deepseek-ai/dsh-compaction'

export const PROVIDER = 'kin'
export const LANES = ['chat', 'task'] as const
export type Lane = (typeof LANES)[number]
const NAMES: Record<Lane, string> = { chat: 'KIN 聊天', task: 'KIN 做事' }
const FIRST_CHUNK_MS = 60_000
const CHAT_FIRST_CHUNK_MS = 30_000
const COOL_MS = 5 * 60_000

export interface Route {
  provider: string
  model: string
  /** Reasoning effort for this route; unset, the chat lane thinks lightly and the task lane uses the model's default. */
  effort?: string
  /** Verified relay input ceiling when tool definitions are present; independent of model capacity. */
  toolInputLimit?: number
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
  /** Routes that failed lately: why, and until when they wait behind the others. */
  private readonly cooling = new Map<string, { until: number; reason: string }>()
  /**
   * Memories and sources deleted since she was shown them, and when that was learned (preset.ts asks Core as each
   * turn starts; index.ts keeps the list): a block of her context that rests on one is not sent to a model again,
   * in a reply or in a summary of the conversation.
   */
  readonly erased = new Map<string, number>()
  /**
   * What each summary of a conversation (a DSH compaction, by its id) rests on, and when its writing began
   * (preset.ts reads both from the session log). One written before a deletion among these was learned of
   * carries the deleted words and is not sent again either; one written after was made without them.
   */
  readonly summaries = new Map<string, { at: number; rests: Set<string> }>()

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
    const sized = infos.flatMap((info) => (info.context ? [{ window: info.context.contextWindow, output: info.defaultMaxTokens ?? OUTPUT_BUDGET }] : []))
    const images = infos.length > 0 && infos.every((info) => info.inputModalities?.includes('image'))
    // What a request leaves for the conversation is its route's window less the output that route reserves:
    // the lane says the smallest window and, as its own reservation, what makes the smallest such room.
    const window = Math.min(...sized.map((route) => route.window))
    const reserved = window - Math.min(...sized.map((route) => route.window - Math.min(OUTPUT_BUDGET, route.output)))
    return {
      provider: PROVIDER, id: lane, name: NAMES[lane],
      inputModalities: images ? ['text', 'image'] : ['text'],
      ...(sized.length ? { context: { contextWindow: window }, ...(reserved > 0 ? { defaultMaxTokens: reserved } : {}) } : {}),
    }
  }

  async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    const lane = laneOf(options.model)
    // Older sessions can already contain the relay envelope. Do not feed it back as spoken dialogue.
    const messages = options.messages.map((message) => {
      // A block of her context that rests on something deleted since: its words are not sent again.
      if (message.role === 'user' && message.source?.kind === 'kin-context' && message.source.rests?.some((id) => this.erased.has(id))) {
        return { ...message, content: [{ type: 'text' as const, text: '（这里原来有一段你想起来的事，其中有后来被删掉的内容，已经撤下。）' }] }
      }
      const summary = message.role === 'user' && message.source?.kind === 'compact-checkpoint' ? this.summaries.get(message.source.compactionId) : undefined
      if (summary && [...summary.rests].some((id) => (this.erased.get(id) ?? 0) >= summary.at)) {
        return { ...message, content: [{ type: 'text' as const, text: '（这里原来是前面对话的摘要，其中有后来被删掉的内容，已经撤下；前面说过的事，需要时用 kin_recall 找。）' }] }
      }
      if (message.role !== 'assistant') return message
      const content = message.content.map((block) => block.type === 'text' ? { ...block, text: answerText(block.text) } : block)
      if (content.every((block, i) => block.type !== 'text' || block.text === (message.content[i] as { text: string }).text)) return message
      return { ...message, content, source: { ...message.source, replayState: undefined } }
    })
    const tokenCache = new Map<string, number>()
    // This is after the loop assembled the newest system prompt, tools and incoming messages.
    // A pressure finish here reaches DSH's normal compaction recovery before any route can answer.
    if (options.sessionId && !options.purpose) {
      const info = await this.resolveModel(PROVIDER, lane, options.signal)
      if (info.context) {
        const output = options.maxTokens ?? info.defaultMaxTokens ?? 0
        const threshold = requestThreshold(info.context.contextWindow, output)
        const estimates = this.routes(lane).map(route => {
          const request = modelRequest({ ...options, messages, provider: route.provider, model: route.model }, route)
          return estimateRequestTokens(request, this.ctx.llm.imageRequestPricing(route.provider, route.model),
            ref => this.ctx.llm.fileRequestText(ref), tokenCache)
        })
        const estimate = Math.max(...estimates)
        if (estimate >= threshold) {
          yield { type: 'finish', reason: { kind: 'error', failure: { code: 'CONTEXT_WINDOW_EXCEEDED',
            message: `Request preflight: estimated ${estimate} input tokens reaches ${threshold}; context ${info.context.contextWindow}, output reservation ${output}. Compact before generation.` } } }
          return
        }
      }
    }
    const failed: LastCall['failed'] = []
    let failure: LlmFailure | undefined
    const key = (route: Route) => route.provider + '/' + route.model
    const cool = (route: Route) => (this.cooling.get(key(route))?.until ?? 0) > Date.now()
    const routes = this.routes(lane)
    const order = [...routes.filter((route) => !cool(route)), ...routes.filter(cool)]
    for (const [index, route] of order.entries()) {
      if (options.signal?.aborted) return
      if (route.toolInputLimit && options.tools?.length) {
        const request = modelRequest({ ...options, messages, provider: route.provider, model: route.model }, route)
        const estimate = estimateRequestTokens(request, this.ctx.llm.imageRequestPricing?.(route.provider, route.model),
          ref => this.ctx.llm.fileRequestText(ref), tokenCache)
        if (estimate >= Math.max(0, route.toolInputLimit - 8192)) {
          failure = { code: 'CONTEXT_WINDOW_EXCEEDED', message: `Route tool-input limit: estimated ${estimate} tokens exceeds safe budget ${route.toolInputLimit - 8192} (relay limit ${route.toolInputLimit}).` }
          failed.push({ route, reason: failure.message })
          continue
        }
      }
      const attempt = new AbortController()
      const stop = () => attempt.abort()
      options.signal?.addEventListener('abort', stop, { once: true })
      const patience = lane === 'chat' && index < order.length - 1 ? CHAT_FIRST_CHUNK_MS : FIRST_CHUNK_MS
      let firstTokenTimedOut = false
      const timer = setTimeout(() => { firstTokenTimedOut = true; stop() }, patience)
      const held: StreamChunk[] = []
      let answering = false
      let finished = false
      failure = undefined
      // A model's own facts that never come are not waited on past a stop or this route's patience.
      const effort = await Promise.race([
        this.effortOf(lane, route),
        new Promise<undefined>((resolve) => attempt.signal.addEventListener('abort', () => resolve(undefined), { once: true })),
      ])
      // Observe transport tokens before envelope decoding: hidden reasoning is progress too.
      const observe = async function* (source: AsyncIterable<StreamChunk>) {
        for await (const chunk of source) {
          if (((chunk.type === 'text-delta' || chunk.type === 'reasoning-delta') && Boolean(chunk.text))
            || (chunk.type === 'tool-call-delta' && Boolean(chunk.argumentsDelta))
            || (chunk.type === 'block-end' && (chunk.block.type === 'tool-call'
              || ((chunk.block.type === 'text' || chunk.block.type === 'reasoning') && Boolean(chunk.block.text))))) clearTimeout(timer)
          yield chunk
        }
      }
      try {
        const info = await this.ctx.llm.resolveModelInfo?.(route.provider, route.model, attempt.signal)
        const maxTokens = Math.min(options.maxTokens ?? OUTPUT_BUDGET, info?.defaultMaxTokens ?? OUTPUT_BUDGET)
        for await (const chunk of modelOutput(observe(this.ctx.llm.stream(modelRequest({
          ...options, messages, maxTokens, provider: route.provider, model: route.model, signal: attempt.signal,
          reasoningEffort: effort ? ReasoningEffortId(effort) : undefined,
        }, route))))) {
          if (chunk.type === 'finish') {
            finished = true
            if (answering && (chunk.reason.kind === 'error' || chunk.reason.kind === 'aborted')) {
              const cause = chunk.reason.failure
              // Keep the original terminal failure; the compaction engine guards agent-level
              // recovery after partial output, including transports whose receipts are still pending.
              throw new LlmError(cause.message, cause.code, cause)
            }
          }
          if (!answering) {
            if (chunk.type === 'finish' && (chunk.reason.kind === 'error' || chunk.reason.kind === 'aborted')) {
              failure = chunk.reason.failure
              break
            }
            if (chunk.type === 'finish') break
            // Thinking and incomplete tool arguments have not answered or executed anything yet.
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
          yield rememberRoute(chunk, route)
        }
        if (answering && !finished) throw new LlmError('模型开始回复后连接中断，未收到结束确认', 'INCOMPLETE_RESPONSE')
        if (!answering && !failure) failure = { code: 'NO_RESPONSE', message: '模型没有返回内容' }
      } catch (error) {
        if (answering) {
          const reason = (error as Error).message
          this.cooling.set(key(route), { until: Date.now() + COOL_MS, reason })
          this.last[lane] = { at: new Date().toISOString(), answered: null, failed: [...failed, { route, reason }] }
          throw error
        }
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
        this.cooling.delete(key(route))
        // A route still waiting its minutes out was not tried this time; her page keeps saying why.
        const resting = order.slice(index + 1).filter(cool).map((route) => ({ route, reason: this.cooling.get(key(route))!.reason }))
        this.last[lane] = { at: new Date().toISOString(), answered: route, failed: [...failed, ...resting] }
        return
      }
      const reason = firstTokenTimedOut ? `${patience / 1000} 秒未收到首个 token（含思考）` : describe(failure)
      this.cooling.set(key(route), { until: Date.now() + COOL_MS, reason })
      failed.push({ route, reason })
    }
    this.last[lane] = { at: new Date().toISOString(), answered: null, failed }
    yield {
      type: 'finish',
      reason: { kind: 'error', failure: failure ?? { code: 'NO_ROUTE', message: 'KIN 还没有可用的模型路线' } },
    }
  }
}

/** A successful connectivity check requires visible text and a successful terminal event. */
export async function probe(ctx: Context, route: Route): Promise<{ ok: boolean; ms: number; reason?: string }> {
  const started = Date.now()
  const ms = () => Date.now() - started
  let visible = false
  try {
    for await (const chunk of modelOutput(ctx.llm.stream({
      provider: route.provider, model: route.model, reasoningEffort: route.effort ? ReasoningEffortId(route.effort) : undefined,
      messages: [{ role: 'user', content: [{ type: 'text', text: '只回复：连通成功' }] }], maxTokens: 4096, signal: AbortSignal.timeout(FIRST_CHUNK_MS),
    }))) {
      if ((chunk.type === 'text-delta' && chunk.text.trim())
        || (chunk.type === 'block-end' && chunk.block.type === 'text' && chunk.block.text.trim())) visible = true
      if (chunk.type !== 'finish') continue
      if (chunk.reason.kind === 'error' || chunk.reason.kind === 'aborted') {
        return { ok: false, ms: ms(), reason: describe(chunk.reason.failure) }
      }
      return visible && chunk.reason.kind === 'stop' ? { ok: true, ms: ms() }
        : { ok: false, ms: ms(), reason: visible ? '回复未正常完成：' + chunk.reason.kind : '没有可见回复' }
    }
    return { ok: false, ms: ms(), reason: visible ? '回复未收到结束确认' : '没有回应' }
  } catch (error) {
    return { ok: false, ms: ms(), reason: (error as Error).message.slice(0, 200) }
  }
}

function describe(failure: LlmFailure): string {
  if (failure.status) return `${failure.status} ${failure.message}`.slice(0, 200)
  return failure.message.slice(0, 200) || failure.code
}
