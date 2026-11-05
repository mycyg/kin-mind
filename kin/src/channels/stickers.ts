/**
 * Stickers from her meme library (core/adapters/meme-library.mjs): `<dataDir>/memes/index.json` and the
 * originals beside it. Only reading lives here — what fits some words, best first — so a sticker can go out
 * as an image part; collecting and sorting memes stay with the library.
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Sticker } from './transport.js'

interface Meme {
  id: string
  file: string
  state: string
  description?: string
  ocr?: string
  tags?: string[]
  contexts?: string[]
  aliases?: string[]
  emotions?: string[]
  uses?: { when: string; reading: string }[]
  lastUsedAt?: number | null
}

const normalize = (text: string) => text.normalize('NFKC').toLowerCase()
// Words as the library ranks them: latin words whole, Chinese as overlapping pairs of characters.
const terms = (text: string) => [...new Set((text.match(/[a-z0-9]+|[\p{Script=Han}]+/gu) ?? [])
  .flatMap((t) => (/^\p{Script=Han}{2,}$/u.test(t) ? Array.from({ length: t.length - 1 }, (_, i) => t.slice(i, i + 2)) : [t])))]

/** Notes that a sticker was sent, as the library itself did, so the next pick among equals varies. */
export function markUsed(dir: string, id: string) {
  const index = join(dir, 'index.json')
  if (!existsSync(index)) return
  const data = JSON.parse(readFileSync(index, 'utf8'))
  if (!data.items?.[id]) return
  data.items[id].lastUsedAt = Date.now()
  writeFileSync(index, JSON.stringify(data, null, 2) + '\n')
}

export function stickers(dir: string, words: string, limit = 3): Sticker[] {
  const index = join(dir, 'index.json')
  const query = normalize(words.trim())
  if (!query || !existsSync(index)) return []
  const items = Object.values(JSON.parse(readFileSync(index, 'utf8')).items as Record<string, Meme>)
  return items.filter((item) => item.state === 'ready').map((item) => {
    const corpus = normalize([item.description, item.ocr, ...item.tags ?? [], ...item.contexts ?? [], ...item.aliases ?? [],
      ...item.emotions ?? [], ...(item.uses ?? []).flatMap((use) => [use.when, use.reading])].join(' '))
    const score = terms(query).filter((term) => corpus.includes(term)).length + (corpus.includes(query) ? 3 : 0)
    return { item, score }
  }).filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || (a.item.lastUsedAt ?? 0) - (b.item.lastUsedAt ?? 0))
    .slice(0, limit)
    .map(({ item }) => ({ id: item.id, file: join(dir, 'originals', item.file), description: item.description ?? '' }))
}
