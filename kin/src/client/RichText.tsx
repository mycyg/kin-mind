/** Lightweight, zero-dependency rich text renderer for chat bubbles (paragraphs, emphasis, inline code, fenced blocks, quotes, lists, safe links). */
import { Fragment, type ReactNode } from 'react'

export function RichText({ text }: { text: string }) {
  const blocks = splitCodeBlocks(text)
  return (
    <div className="kin-rt">
      {blocks.map((block, index) => block.kind === 'code'
        ? (
          <pre key={index} className="kin-rt-pre">
            {block.lang ? <span className="kin-rt-lang">{block.lang}</span> : null}
            <code>{block.text}</code>
          </pre>
        )
        : <Prose key={index} text={block.text} />)}
    </div>
  )
}

function splitCodeBlocks(input: string): ({ kind: 'prose'; text: string } | { kind: 'code'; lang?: string; text: string })[] {
  const out: ({ kind: 'prose'; text: string } | { kind: 'code'; lang?: string; text: string })[] = []
  const fence = /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g
  let cursor = 0
  for (let match = fence.exec(input); match; match = fence.exec(input)) {
    if (match.index > cursor) out.push({ kind: 'prose', text: input.slice(cursor, match.index) })
    out.push({ kind: 'code', lang: match[1] || undefined, text: match[2].replace(/\n$/, '') })
    cursor = match.index + match[0].length
  }
  if (cursor < input.length) out.push({ kind: 'prose', text: input.slice(cursor) })
  return out
}

function Prose({ text }: { text: string }) {
  const chunks = text.split(/\n{2,}/)
  return (
    <>
      {chunks.map((chunk, index) => {
        const trimmed = chunk.trim()
        if (!trimmed) return null
        const lines = trimmed.split('\n')
        if (lines.every((line) => /^\s*[-*•]\s+/.test(line))) {
          return (
            <ul key={index} className="kin-rt-list">
              {lines.map((line, i) => <li key={i}>{inline(line.replace(/^\s*[-*•]\s+/, ''))}</li>)}
            </ul>
          )
        }
        if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
          return (
            <ol key={index} className="kin-rt-list kin-rt-ol">
              {lines.map((line, i) => <li key={i}>{inline(line.replace(/^\s*\d+[.)]\s+/, ''))}</li>)}
            </ol>
          )
        }
        if (lines.every((line) => /^\s*>\s?/.test(line))) {
          return <blockquote key={index} className="kin-rt-quote">{inlineLines(lines.map((line) => line.replace(/^\s*>\s?/, '')))}</blockquote>
        }
        if (lines.length === 1 && /^#{1,4}\s+/.test(lines[0]!)) {
          return <p key={index} className="kin-rt-h">{inline(lines[0]!.replace(/^#{1,4}\s+/, ''))}</p>
        }
        return <p key={index} className="kin-rt-p">{inlineLines(lines)}</p>
      })}
    </>
  )
}

function inlineLines(lines: string[]): ReactNode {
  return lines.map((line, idx) => (
    <Fragment key={idx}>
      {idx > 0 ? <br /> : null}
      {inline(line)}
    </Fragment>
  ))
}

const TOKEN = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))|(https?:\/\/[^\s<>"'`）)】」』]+)/g

function inline(text: string): ReactNode {
  const nodes: ReactNode[] = []
  let last = 0
  let key = 0
  for (let match = TOKEN.exec(text); match; match = TOKEN.exec(text)) {
    if (match.index > last) nodes.push(text.slice(last, match.index))
    const raw = match[0]
    if (match[1]) {
      nodes.push(<code key={key++} className="kin-rt-code">{raw.slice(1, -1)}</code>)
    } else if (match[2]) {
      nodes.push(<strong key={key++}>{raw.slice(2, -2)}</strong>)
    } else if (match[3]) {
      const mid = raw.indexOf('](')
      const label = raw.slice(1, mid)
      const href = raw.slice(mid + 2, -1)
      nodes.push(<a key={key++} className="kin-rt-link" href={href} target="_blank" rel="noopener noreferrer">{label}</a>)
    } else if (match[4]) {
      // Trim trailing punctuation commonly attached to bare URLs in Chinese/English prose.
      const clean = raw.replace(/[.,;:!?，。；：！？]+$/, '')
      const tail = raw.slice(clean.length)
      nodes.push(<a key={key++} className="kin-rt-link" href={clean} target="_blank" rel="noopener noreferrer">{clean}</a>)
      if (tail) nodes.push(tail)
    }
    last = match.index + raw.length
  }
  if (last < text.length) nodes.push(text.slice(last))
  TOKEN.lastIndex = 0
  return nodes
}
