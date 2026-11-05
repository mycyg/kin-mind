/** What one of her turns holds, read from DSH's session log. */
import { answerText } from './model-output.js'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { UserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq } from '@deepseek-ai/dsh-session'

/** Words a person said: typed in the app or relayed from a channel, not injected context or reminders. */
export function spoken(message: UserMessage): boolean {
  return message.source.kind === 'user' || message.source.kind === 'kin-channel'
}

export function textOf(message: { role?: string; content: readonly { type: string; text?: string }[] }): string {
  return message.content.flatMap((block) => (block.type === 'text' && block.text ? [message.role === 'assistant' ? answerText(block.text) : block.text] : [])).join('\n').trim()
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
 */
export function heard(agent: Agent): { id: string; text: string }[] {
  return lastTurn(agent).flatMap((event) => {
    const text = event.type === 'user/message' && spoken(event.data) ? textOf(event.data) : ''
    if (!text || event.type !== 'user/message') return []
    const { source } = event.data
    return source.kind === 'kin-channel' ? source.inputs.map((id) => ({ id, text })) : [{ id: event.data.id as string, text }]
  })
}
