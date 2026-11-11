/** KIN's input-history ceiling is independent of an adapter's physical model capacity. */
import type { GenerateOptions, ImageBlock, LlmImageRequestPricing } from '@deepseek-ai/dsh-llm'
import { Tiktoken } from 'js-tiktoken/lite'
import cl100k from 'js-tiktoken/ranks/cl100k_base'
import type { FileAttachmentRef } from '@deepseek-ai/dsh-attachment'

let tokenizer: Tiktoken | undefined

export const CONTEXT_WINDOW = 648_000
/** Default generation budget; physical adapter output ceilings remain independently configured. */
export const OUTPUT_BUDGET = 128_000
export const PRESSURE_RATIO = 0.9
export const HEADROOM_TOKENS = 32_768

/** Reserve the requested completion and a margin before a model starts producing visible output. */
export function requestThreshold(window: number, output: number): number {
  return Math.max(0, Math.floor(Math.min(CONTEXT_WINDOW * PRESSURE_RATIO, window - output - HEADROOM_TOKENS)))
}

/**
 * BPE estimate of the complete request, including tool updates, arguments/results and expanded files.
 * Bounded chunks avoid quadratic work on long unbroken strings. Chunk boundaries can slightly
 * overcount; cl100k is a shared estimate, not a claim to reproduce every provider's tokenizer.
 */
export function estimateRequestTokens(options: GenerateOptions, imagePricing?: LlmImageRequestPricing,
  fileText?: (ref: FileAttachmentRef) => string, cache = new Map<string, number>()): number {
  const images: ImageBlock[] = []
  const messages = options.messages.map(message => ({ ...message, content: message.content.map(block => {
    if (block.type === 'image') { images.push(block); return { type: 'image' } }
    if (block.type === 'file' && fileText) return { type: 'text', text: fileText(block.attachment) }
    return block
  }) }))
  const prices = imagePricing?.priceImages(images)
  const text = JSON.stringify({ system: options.system, messages, tools: options.tools, toolHistory: options.toolHistory,
    imageText: prices?.map(price => price.text) })
  // Unknown image routes get a conservative visual allowance; known adapters own exact image projection.
  const visual = prices?.reduce((sum, price) => sum + price.visualTokens, 0) ?? images.length * 16_384
  tokenizer ??= new Tiktoken(cl100k)
  let tokens = 0
  for (let start = 0; start < text.length;) {
    let end = Math.min(start + 4096, text.length)
    if (end < text.length && text.charCodeAt(end - 1) >= 0xd800 && text.charCodeAt(end - 1) <= 0xdbff) end--
    const chunk = text.slice(start, end)
    let count = cache.get(chunk)
    if (count === undefined) { count = tokenizer.encode(chunk, [], []).length; cache.set(chunk, count) }
    tokens += count
    start = end
  }
  return tokens + messages.length * 8 + visual
}
