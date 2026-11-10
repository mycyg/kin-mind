/** What one of her turns holds, read from DSH's session log. */
import { answerText } from './model-output.js'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { UserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import { OWNER } from './contact/people.js'

/** Words a person said: typed in the app or relayed from a channel, not injected context or reminders. */
export function spoken(message: UserMessage): boolean {
  return message.source.kind === 'user' || message.source.kind === 'kin-channel'
}

export function textOf(message: { role?: string; content: readonly { type: string; text?: string }[] }): string {
  return message.content.flatMap((block) => (block.type === 'text' && block.text ? [message.role === 'assistant' ? answerText(block.text) : block.text] : [])).join('\n').trim()
}

/** What the owner typed in the app, with a word for each picture or file sent along; a platform's arrivals name theirs already. */
export function typed(message: UserMessage): string {
  const sent = message.content.flatMap((block) => (block.type === 'image' ? ['[图片]'] : block.type === 'file' ? [`[文件：${block.attachment.name}]`] : []))
  return [textOf(message), ...sent].filter(Boolean).join('\n')
}

/**
 * Whether the latest words of this turn are the owner's own: typed in the app, or said under the owner's own
 * account on a platform. What only the owner may decide (a helper's approval) is answered on such a turn alone.
 */
export function ownerSpoke(agent: Agent): boolean {
  const last = lastTurn(agent).findLast((event) => event.type === 'user/message' && spoken(event.data))
  if (last?.type !== 'user/message') return false
  const { source } = last.data
  return source.kind === 'user' || (source.kind === 'kin-channel' && source.speaker === OWNER)
}

/** The session's latest turn, oldest event first. */
export function lastTurn(agent: Agent) {
  const session = agent.session
  const events = []
  for (let seq = Number(session.seq) - 1; seq >= 0; seq--) {
    const event = session.eventAt(SessionSeq(seq))
    if (event?.type === 'turn/start') break
    if (event) events.unshift(event)
  }
  return events
}

/**
 * What was said to her in the latest turn, by the ids Core knows it by: a message typed in the app by its own
 * id (preset.ts records it as the turn starts), what came from a channel by each arrival's (contact/conversations.ts).
 * Once she has answered and more was said mid-turn, it is what came after her last words.
 */
export function heard(agent: Agent): { id: string; text: string }[] {
  const said = (events: ReturnType<typeof lastTurn>) => events.flatMap((event) => {
    if (event.type !== 'user/message' || !spoken(event.data)) return []
    const { source } = event.data
    const text = source.kind === 'kin-channel' ? textOf(event.data) : typed(event.data)
    if (!text) return []
    return source.kind === 'kin-channel' ? source.inputs.map((id) => ({ id, text })) : [{ id: event.data.id as string, text }]
  })
  const turn = lastTurn(agent)
  const since = said(turn.slice(turn.findLastIndex((event) => event.type === 'assistant/message' && Boolean(textOf(event.data.message))) + 1))
  return since.length ? since : said(turn)
}
