/** Public conversation output has an explicit boundary; no ordinary model text bypasses it. */
import { BlockAssembler, LlmError, type ReplayEnvelope, type StreamChunk } from '@deepseek-ai/dsh-llm'

export const REPLY_INSTRUCTION = '我（Kin）的普通文字只用一个完整 JSON 对象，只有 private_note、action、bubbles 三个字段：'
  + '{"private_note":"我有点惦记眼前这件小事。","action":"send","bubbles":[{"text":"我想当面对方说的话"}]}。不加代码围栏，不在 JSON 外写旁白。'
  + 'private_note 是我写给自己的心情札记。我每次公开回应前都留下札记，用第一人称自然口语记下此刻的心情、想念、猜测或没说出口的话；'
  + '我不刻意压缩、缩写或凑字数，心情可以舒展开；语气词、停顿和小剧场自然出现。'
  + '即使没有强烈情绪，也记一句我此刻的想法。它不是分析步骤、回复方案、工具安排，也不是模型的隐藏推理，我不把这些过程抄进札记。'
  + '札记单独保存，只有 bubbles 会成为公开正文。bubbles 每项是只有 text 字符串字段的对象，表示一条完整的公开气泡；我按自己的性格自然说话。'
  + '这轮不想说话时，我用 {"private_note":"","action":"silent","bubbles":[]}，也可以在 private_note 留下心情。'
  + '我调用工具仍走原生工具接口，不把工具调用写成 JSON 正文；纯工具轮可以没有文字、不另写札记。若同时有文字，我也遵守同一个封套。'
  + '做事前想先跟对方说一句，我把那句放进 bubbles，不在封套外写分析、札记或工作旁白。'

export const REPLY_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['private_note', 'action', 'bubbles'],
  properties: {
    private_note: { type: 'string', maxLength: 4000 }, action: { type: 'string', enum: ['send', 'silent'] },
    bubbles: { type: 'array', maxItems: 64, items: { type: 'object', additionalProperties: false, required: ['text'], properties: { text: { type: 'string', minLength: 1 } } } },
  },
  allOf: [
    { if: { properties: { action: { const: 'send' } } }, then: { properties: { private_note: { minLength: 1 }, bubbles: { minItems: 1 } } } },
    { if: { properties: { action: { const: 'silent' } } }, then: { properties: { bubbles: { maxItems: 0 } } } },
  ],
} as const

export interface ReplyTarget { id: string; sessionId: string; inputId?: string }
export interface ReplyEnvelope { private_note: string; action: 'send' | 'silent'; bubbles: { text: string }[] }
const invalid = (reason: string): never => { throw new LlmError('公开回复封套无效：' + reason, 'INVALID_REPLY_ENVELOPE') }

export function parseReply(text: string): ReplyEnvelope {
  let value: unknown
  try { value = JSON.parse(text) } catch { return invalid('必须是完整 JSON，且没有额外文字') }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid('需要一个对象')
  const v = value as Record<string, unknown>
  if (Object.keys(v).length !== 3 || !['private_note', 'action', 'bubbles'].every(k => Object.hasOwn(v, k))) return invalid('字段不匹配')
  if (typeof v.private_note !== 'string' || v.private_note.length > 4000) return invalid('札记须为不超过 4000 字符的字符串')
  if (!Array.isArray(v.bubbles) || v.bubbles.length > 64 || !v.bubbles.every(b => b && typeof b === 'object' && !Array.isArray(b) && Object.keys(b).length === 1 && Object.hasOwn(b, 'text') && typeof b.text === 'string' && b.text.trim())) return invalid('每个气泡须为只有非空 text 字符串的对象')
  if (v.action === 'send' && !v.private_note.trim()) return invalid('公开回应前须有一段心情札记')
  if (v.action === 'send' && v.bubbles.length || v.action === 'silent' && !v.bubbles.length) return v as unknown as ReplyEnvelope
  return invalid('send 须有气泡，silent 须为空数组')
}

/** Buffer through the successful terminal event, then save the note before releasing any public text or tool. */
export async function* replyOutput(source: AsyncIterable<StreamChunk>, options: {
  signal?: AbortSignal
  save(note: string, reply: string): Promise<void>
  accepted(empty: boolean): void
}): AsyncIterable<StreamChunk> {
  const assembled = new BlockAssembler()
  let terminal: Extract<StreamChunk, { type: 'finish' }> | undefined
  let bytes = 0
  for await (const chunk of source) {
    if (terminal) return invalid('结束事件之后仍有内容')
    bytes += Buffer.byteLength(JSON.stringify(chunk))
    if (bytes > 16 * 1024 * 1024) return invalid('响应超过缓冲上限')
    assembled.push(chunk)
    if (chunk.type === 'finish') terminal = chunk
  }
  if (options.signal?.aborted) throw new LlmError('回复已取消，未公开输出', 'ABORTED')
  if (!terminal) return invalid('缺少结束确认')
  if (terminal.reason.kind === 'error' || terminal.reason.kind === 'aborted') { yield terminal; return }
  if (!['stop', 'tool-calls'].includes(terminal.reason.kind)) return invalid('响应未完整完成')
  const blocks = assembled.blocks()
  if (blocks.some(b => !['text', 'reasoning', 'tool-call'].includes(b.type))) return invalid('不支持的输出块')
  const calls = blocks.filter(b => b.type === 'tool-call')
  if ((terminal.reason.kind === 'tool-calls') !== Boolean(calls.length)) return invalid('工具调用与结束类型不一致')
  if (new Set(calls.map(c => c.id)).size !== calls.length) return invalid('工具调用编号重复')
  const replay = assembled.replayState
  if (terminal.replayState && (!replay || replay.blocks?.length !== blocks.length)) return invalid('原生回放元数据与输出块不匹配')
  const raw = blocks.filter(b => b.type === 'text').map(b => b.text).join('')
  const envelope = raw.trim() ? parseReply(raw) : calls.length ? undefined : invalid('缺少回复封套')
  const publicText = envelope?.bubbles.map(b => b.text).join('\n\n') ?? ''
  if (envelope?.private_note.trim()) {
    try { await options.save(envelope.private_note, publicText) }
    catch { throw new LlmError('心情札记未确认保存，本步没有公开输出或执行工具', 'REPLY_NOTE_SAVE_FAILED') }
  }
  if (options.signal?.aborted) throw new LlmError('回复已取消，未公开输出', 'ABORTED')
  const projected: typeof blocks = []
  const metadata: unknown[] = []
  let textAdded = false
  for (const [index, block] of blocks.entries()) {
    if (block.type === 'text') {
      if (!textAdded && publicText) { projected.push({ type: 'text', text: publicText }); metadata.push({ type: 'text' }); textAdded = true }
    } else { projected.push(block); metadata.push(replay?.blocks?.[index]) }
  }
  // Adapter replay is index-aligned. New public text must not reuse a signature of the private JSON.
  const replayState: ReplayEnvelope | undefined = replay?.blocks?.length === blocks.length
    ? { ...replay, blocks: metadata } : undefined
  options.accepted(!publicText && !calls.length)
  for (const [index, block] of projected.entries()) {
    yield { type: 'block-start', index, blockType: block.type }
    if (block.type === 'text') yield { type: 'text-delta', index, text: block.text }
    else if (block.type === 'reasoning') yield { type: 'reasoning-delta', index, text: block.text }
    else if (block.type === 'tool-call') yield { type: 'tool-call-delta', index, id: block.id, name: block.name, argumentsDelta: block.arguments }
    yield { type: 'block-end', index, block }
  }
  if (assembled.usage) yield { type: 'usage', usage: assembled.usage }
  yield replayState ? { ...terminal, replayState } : terminal
}

/** One same-route format repair, with no rejected text or calls added to conversation history. */
export async function* replyWithCorrection(
  source: (feedback?: string) => AsyncIterable<StreamChunk>,
  options: Parameters<typeof replyOutput>[1] & { rejected(message: string): void },
): AsyncIterable<StreamChunk> {
  let feedback: string | undefined
  for (let attempt = 0; attempt < 2; attempt++) {
    try { yield* replyOutput(source(feedback), options); return }
    catch (error) {
      if (options.signal?.aborted || (error as { code?: string }).code !== 'INVALID_REPLY_ENVELOPE') throw error
      const message = (error as Error).message
      options.rejected(message)
      if (attempt) throw error
      feedback = '本步输出被本地 JSON 契约校验拒绝：' + message
        + '。本步没有公开文字、保存札记或执行任何工具。请只纠正本步输出，遵守原有 private_note/action/bubbles 契约；'
        + '每个 bubbles 项为 {"text":"公开正文"}，send 前 private_note 须非空。不要重做历史中已经完成的工具。'
        + '不要解释这次校验错误，不要在 JSON 外加文字；仍需工具时使用原生工具调用。'
    }
  }
}
