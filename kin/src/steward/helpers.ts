/**
 * KIN's own helpers: DSH sub-sessions (the subagent service's spawn provider) on her task lane, for
 * the exploring and making she does by herself. They hang under a conversation of her own, so the
 * owner's chats list only the work the owner asked for, and DSH shows them like any delegated work. The
 * result comes back to the caller here, never as a notice into a conversation.
 */
import type {} from '@deepseek-ai/dsh-subagent'
import type { SessionId } from '@deepseek-ai/dsh-session'
import type { ObjectJsonSchema } from '@deepseek-ai/dsh-tools'
import type { Host } from '../index.js'
import { PROVIDER } from '../models.js'
import type { Permit } from './permission.js'

/** The DSH preset of the owner's own coding sessions — shell, files, web — which every worker of hers runs. */
export const WORKER_PRESET = 'standard'

/** A page a helper fetched, as its tool returned it. */
export interface Read {
  call: string
  url: string
  final_url?: string
  status?: number
  text: string
  truncated?: boolean
  at: string
}

export interface HelperTask {
  label: string
  prompt: string
  /** The helper's working directory. */
  cwd: string
  timeoutMs: number
  signal: AbortSignal
  outputSchema?: ObjectJsonSchema
  /** The helper exists and has its task. */
  onStarted?: (session: string) => void
}

export interface HelperRun {
  /** `complete`, or how it stopped: aborted, error, max-tokens, refusal, timed-out. */
  state: string
  text: string
  structured?: unknown
  diagnostic?: string
  session: string
  reads: Read[]
}

export type Helpers = (task: HelperTask) => Promise<HelperRun>

export function helpers(host: Host, permit: Permit): Helpers {
  const { ctx } = host
  const reads = new Map<string, Read[]>()
  let own: string | undefined

  // What her helpers fetch, for an exploration's source ledger (core dsh_executor.py).
  ctx.on('tools/result', (exec, result) => {
    const session = exec.agent?.session
    if (exec.name !== 'web_fetch' || !own || session?.header.parentSession !== own) return
    const value = result.isError ? undefined : result.value as { url?: string; statusCode?: number; truncated?: boolean }
    const list = reads.get(session.id) ?? []
    list.push({
      call: exec.callId, url: String((exec.arguments as { url?: unknown }).url ?? ''), final_url: value?.url,
      status: value?.statusCode, text: textOf(result.content), truncated: value?.truncated, at: new Date().toISOString(),
    })
    reads.set(session.id, list)
  })

  return async ({ label, prompt, cwd, timeoutMs, signal, outputSchema, onStarted }) => {
    const conversation = await host.conversation('own', { channel: 'own', title: 'KIN 自己的事：出门和创作' })
    own = conversation.sessionId
    const resolved = await ctx.get('sessionController')!.resolveAgent(own as SessionId)
    if (!('agent' in resolved)) throw new Error('her own conversation is not available: ' + resolved.error.code)
    const parent = resolved.agent
    // Her outings read the web on their own, so their helpers keep to her own workspace whatever the
    // owner lets helpers do for them; the helper inherits this as it starts.
    permit(parent.session, 'workspace-write')
    const activation = await ctx.get('subagents')!.startActivation({
      provider: 'spawn', label, signal, delivery: 'caller',
      request: {
        prompt: [{ type: 'text', text: prompt }], parent, cwd, agentPreset: WORKER_PRESET,
        agentOptions: { provider: PROVIDER, model: 'task' }, ...(outputSchema ? { outputSchema } : {}),
      },
    })
    const session = activation.childId as string
    onStarted?.(session)
    let late = false
    const stop = () => void activation.dispose().catch(() => {})
    const timer = setTimeout(() => { late = true; stop() }, timeoutMs)
    signal.addEventListener('abort', stop, { once: true })
    if (signal.aborted) stop()
    try {
      const result = await activation.result
      return {
        state: result.stopReason === 'completed' ? 'complete' : late ? 'timed-out' : result.stopReason,
        text: textOf(result.output), structured: result.structured, diagnostic: result.diagnostic, session,
        reads: reads.get(session) ?? [],
      }
    } finally {
      clearTimeout(timer)
      signal.removeEventListener('abort', stop)
      reads.delete(session)
    }
  }
}

export function textOf(content: readonly { type: string; text?: string }[] | undefined): string {
  return (content ?? []).flatMap((block) => (block.type === 'text' && block.text ? [block.text] : [])).join('\n').trim()
}
