/** Some relays wrap private reasoning in a text block instead of emitting a reasoning block. */
import type { StreamChunk } from '@deepseek-ai/dsh-llm'

const THINKING = '🧠\n'
const ANSWER = '🪒'

/** Decode the relay's explicit thought/answer envelope; ordinary text and emoji stay intact. */
export function answerText(text: string): string {
  if (!text.startsWith(THINKING)) return text
  const end = text.indexOf(ANSWER, THINKING.length)
  return end < 0 ? '' : text.slice(end + ANSWER.length)
}

/** Decode before any text delta reaches the desktop, a channel, or the remembered assistant message. */
export async function* modelOutput(source: AsyncIterable<StreamChunk>): AsyncIterable<StreamChunk> {
  const blocks = new Map<number, { mode: 'prefix' | 'thinking' | 'answer'; held: string }>()
  for await (const chunk of source) {
    if (chunk.type === 'text-delta') {
      const state = blocks.get(chunk.index) ?? { mode: 'prefix', held: '' }
      blocks.set(chunk.index, state)
      if (state.mode === 'answer') { yield chunk; continue }
      state.held += chunk.text
      if (state.mode === 'prefix') {
        if (THINKING.startsWith(state.held)) continue
        state.mode = state.held.startsWith(THINKING) ? 'thinking' : 'answer'
      }
      if (state.mode === 'thinking') {
        const end = state.held.indexOf(ANSWER, THINKING.length)
        if (end < 0) continue
        state.held = state.held.slice(end + ANSWER.length)
        state.mode = 'answer'
      }
      if (state.held) yield { ...chunk, text: state.held }
      state.held = ''
    } else if (chunk.type === 'block-end' && chunk.block.type === 'text') {
      const state = blocks.get(chunk.index)
      const text = answerText(chunk.block.text)
      // A short ordinary emoji may also be a prefix of the envelope. Release it once the block ends.
      if (state?.mode === 'prefix' && text) yield { type: 'text-delta', index: chunk.index, text }
      blocks.delete(chunk.index)
      yield { ...chunk, block: { ...chunk.block, text } }
    } else yield chunk
  }
}
