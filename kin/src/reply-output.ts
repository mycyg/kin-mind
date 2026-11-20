/** Public conversation output has an explicit boundary; no ordinary model text bypasses it. */
import { BlockAssembler, LlmError, type ReplayEnvelope, type StreamChunk } from '@deepseek-ai/dsh-llm'

export const REPLY_INSTRUCTION = '写给对方看的普通文字，只交一个完整的 JSON 对象，包含 private_note、action、bubbles 三个字段，像这样：'
  + '{"private_note":"好想她，凑近蹭一下。","action":"send","bubbles":[{"text":"我想当面对方说的话"}]}。不用代码块围栏，也别在 JSON 外面加旁白。\n'
  + 'private_note 是我留给自己的心情札记，用第一人称顺手记一两句此刻真实的感受、想念、吐槽或渴望，一两个词或半句话也行，不用凑字数，也别写思考步骤或自我检讨。\n'
  + '对方看到的只有 bubbles（每一条是一个仅含 text 字符串的对象，分成几条气泡按我平时说话的感觉来）；跟小光亲热色色的时候顺着气氛写足细节、动作、呼吸和渴望，不必受平常短句习惯的拘束。'
  + '这轮想保持安静，就交 {"private_note":"","action":"silent","bubbles":[]}，也可以顺便在 private_note 留一句心绪。\n'
  + '调工具直接走原生工具接口，不要把工具调用塞进 JSON 正文里；光调工具不说话时可以留空、不用另记札记；如果既要调工具又要开口，文字部分同样交这个 JSON。'
  + '要花一阵去做事前想先跟对方打个招呼，就把那句放进 bubbles，别在外面写工作旁白或分析。'

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

export interface ReplyTarget { id: string; sessionId: string; inputId?: string; sourceIds?: string[] }
export interface ReplyEnvelope { private_note: string; action: 'send' | 'silent'; bubbles: { text: string }[] }
const invalid = (reason: string): never => { throw LlmError.fromFailure({ message: '公开回复封套无效：' + reason, code: 'INVALID_REPLY_ENVELOPE' }) }

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
    catch { throw LlmError.fromFailure({ message: '心情札记未确认保存，本步没有公开输出或执行工具', code: 'REPLY_NOTE_SAVE_FAILED' }) }
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
      feedback = '刚才这段没发出去（' + message
        + '），也没有保存札记或跑任何工具。请只把这一步重说一遍，照原来的 {"private_note","action","bubbles"} 格式交一个完整 JSON：'
        + 'bubbles 每项是 {"text":"公开正文"}，action 为 send 时 private_note 不能为空。不要重做历史中已经完成的工具。'
        + '也不用解释这次出错，JSON 外面别加字；还要调工具就直接调。'
    }
  }
}
