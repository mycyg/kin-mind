/**
 * A message she might send on her own, drafted as herself (contact-draft.mjs): her persona, what she feels
 * and remembers in this person's own view, their recent talk, and the wishes that are ready, on her chat lane.
 * Whether to send, wait or let it go is hers; nothing here makes up a topic or a phrase. A draft that fails or
 * says nothing usable is tried once more, on the chat lane's next route.
 */
import { BlockAssembler, type RequestMessage } from '@deepseek-ai/dsh-llm'
import type { Host, Scope } from '../index.js'
import { PROVIDER, type Lane, type Route } from '../models.js'
import { renderContext } from '../render.js'
import { OWNER } from './people.js'

export interface Decision {
  action: 'send' | 'wait' | 'abandon'
  bubbles: string[]
  desire_ids?: string[]
  reason: string
  condition?: 'time' | 'new_evidence' | 'owner_reply' | 'review'
  retry_after_seconds?: number
  /** What she shares that Core keeps a record of (findings), as the sharing ledger names it. */
  references?: unknown[]
}

export interface Occasion {
  /** Who it would go to, as she knows them. */
  who: string
  scope: Scope
  /** The ready wishes she may act on (Core's claim), or none for a group she may chime into. */
  wishes: Record<string, unknown>[]
  /** Sends of unknown outcome she should know about. */
  unknown?: unknown[]
  /** A group's talk she has not answered. */
  group?: string[]
}

const WAIT = { min: 300, max: 259_200 }
const NAMED = /\b(?:src|mem)_[0-9a-f]{32}\b/g

const INSTRUCTIONS = `这一刻没有人在等你回话。下面是你此刻的状态、想起来的事、和对方最近说过的话，以及你心里已经准备好的联系愿望。你可以主动给对方发消息，也可以先不。

想发，就用你平时说话的样子写，像熟人发消息，可以分几条。想不想说、说什么、说多少，都由你。
暂时不想说，可以等：到某个时间以后再想（condition 写 time，并写 retry_after_seconds），等对方先开口（owner_reply），等有了新的事（new_evidence），或者下次再想（review）。
已经不想做这件事了，可以放下（abandon）。

最后只输出一个 JSON 对象：
{"action":"send","bubbles":["……"],"desire_ids":["你这次回应的愿望 id，不写表示全部"]}
{"action":"wait","condition":"review","reason":"……"}
{"action":"abandon","reason":"……"}`

const GROUP = `群里有人在聊天，没有人 @ 你。想自然地接一句就接，不想就等；不必一条条回，拿不准就不说。只属于某个人的私事不在群里讲。`

export async function draft(host: Host, occasion: Occasion): Promise<{ decision: Decision; shown: string[] }> {
  const kin = host.ctx.get('kin')!
  const topic = occasion.wishes.map((wish) => wish.topic ?? wish.content ?? '').join('\n') || occasion.group?.join('\n') || ''
  const { channel: _channel, ...scope } = occasion.scope
  const memory = await host.mind.call('memory-context', { query: topic, purpose: 'proactive', mode: 'light', recent_dialogue: true, ...scope })
  // Only this conversation's own talk: what she and someone else said stays with them.
  const recent = ((memory.recent_dialogue ?? []) as { role: string; text?: string; recipient_person_id?: string }[])
    .filter((turn) => (turn.recipient_person_id ?? OWNER) === occasion.scope.recipient_person_id && turn.text)
    .slice(-12).map((turn) => `${turn.role === 'user' ? occasion.who : '你'}：${turn.text}`)
  const facts = [
    `现在是 ${new Date().toLocaleString('zh-CN', { hour12: false })}。对方是${occasion.who}。`,
    renderContext(memory, kin.dimensions(), kin.nameOf).text,
    recent.length ? '你们最近的对话：\n' + recent.join('\n') : '',
    occasion.wishes.length ? '此刻已经准备好的联系愿望（由你挑）：' + JSON.stringify(occasion.wishes) : '',
    occasion.unknown?.length ? '结果还没确认的消息（可能已经送到，会按原来的编号核对，不要换个说法再发一遍）：' + JSON.stringify(occasion.unknown) : '',
    occasion.group?.length ? '群里的新消息：\n' + occasion.group.join('\n') : '',
  ].filter(Boolean).join('\n\n')
  const system = kin.persona() + '\n\n' + (occasion.group ? GROUP + '\n\n' : '') + INSTRUCTIONS
  const messages: RequestMessage[] = [{ role: 'user', content: [{ type: 'text', text: facts }] } as RequestMessage]
  const next = host.load<Record<Lane, Route[]>>('models.json', { chat: [], task: [] }).chat[1]
  let decision: Decision | undefined
  for (const route of [{ provider: PROVIDER, model: 'chat' }, next ?? { provider: PROVIDER, model: 'chat' }]) {
    decision = await ask(host, route, system, messages).then(decide, (error: Error) => (host.log(`contact draft: ${error.message}`), undefined))
    if (decision) break
  }
  if (!decision) throw new Error('contact-draft-invalid-result')
  return { decision, shown: [...new Set(JSON.stringify([memory, occasion.wishes]).match(NAMED) ?? [])] }
}

async function ask(host: Host, route: { provider: string; model: string }, system: string, messages: RequestMessage[]) {
  const assembler = new BlockAssembler()
  for await (const chunk of host.ctx.llm.stream({ ...route, system, messages })) assembler.push(chunk)
  const finish = assembler.finish
  if (finish.kind === 'error' || finish.kind === 'aborted') throw new Error(finish.failure.message)
  return assembler.blocks().flatMap((block) => (block.type === 'text' ? [block.text] : [])).join('')
}

/** Her decision: the one JSON object her answer ends with, read the safe way where a field is missing. */
export function decide(text: string): Decision | undefined {
  const value = finalObject(text) as Partial<Decision> & { text?: string } | undefined
  if (!value || !['send', 'wait', 'abandon'].includes(value.action as string)) return
  const desire_ids = Array.isArray(value.desire_ids) ? value.desire_ids.filter((id) => typeof id === 'string' && id.trim()) : []
  const reason = typeof value.reason === 'string' ? value.reason.trim().slice(0, 1200) : ''
  const common = { reason, ...(desire_ids.length ? { desire_ids } : {}) }
  if (value.action === 'send') {
    const bubbles = (Array.isArray(value.bubbles) ? value.bubbles : [value.text])
      .flatMap((bubble) => (typeof bubble === 'string' && bubble.trim() ? [bubble.trim()] : []))
    return { action: 'send', bubbles, ...common, ...(Array.isArray(value.references) ? { references: value.references } : {}) }
  }
  if (value.action === 'abandon') return { action: 'abandon', bubbles: [], ...common }
  const condition = ['time', 'new_evidence', 'owner_reply', 'review'].includes(value.condition as string) ? value.condition : 'review'
  const seconds = Math.min(WAIT.max, Math.max(WAIT.min, Math.round(Number(value.retry_after_seconds) || 1800)))
  return { action: 'wait', bubbles: [], ...common, condition, ...(condition === 'time' ? { retry_after_seconds: seconds } : {}) }
}

/** The last JSON object the text ends with, whatever comes before it. */
function finalObject(text: string): unknown {
  const end = text.replace(/[\s`~]*$/, '').length
  if (text[end - 1] !== '}') return
  for (let start = text.lastIndexOf('{', end - 1); start >= 0; start = start > 0 ? text.lastIndexOf('{', start - 1) : -1) {
    try {
      const value = JSON.parse(text.slice(start, end))
      if (value && typeof value === 'object' && !Array.isArray(value)) return value
    } catch { /* a wider one may still be there */ }
  }
}
