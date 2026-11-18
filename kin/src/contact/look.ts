/**
 * Her other conversations, as she looks at them from the one she is in: who each is with, where, and when last
 * active; or the latest of what was said in one, who said it and when, her own lines as hers. Read from the
 * conversations' DSH sessions. Looking is open to her in every conversation; what she passes on, and to whom,
 * is hers to judge (the owner's word, 2026-11-05), so the answer says so and nothing here decides it.
 */
import type { Session } from '@deepseek-ai/dsh-session'
import { SessionSeq } from '@deepseek-ai/dsh-session'
import type { Conversation, Host } from '../index.js'
import type { Delivery } from './deliveries.js'
import { PART_WORDS } from './deliveries.js'
import { stamp, when } from '../render.js'
import { textOf } from '../turn.js'
import { PLATFORMS } from './conversations.js'
import { nameOf, OWNER, type People } from './people.js'

/** How many lines of one conversation she reads at a time. */
const LINES = 12
const KEEP = '谁在哪说了什么你心里清楚；要不要告诉眼前的人、说多少由你判断，别人请你别外传的话替对方守着。'

export async function look(host: Host, people: People, sessionId: string, who?: string, deliveries: Delivery[] = []): Promise<string> {
  const nameIn = (id = OWNER) => nameOf([...people.people, ...people.groups].find((entry) => entry.id === id))
  const where = ({ channel, address }: Conversation) => channel === 'app'
    ? '桌面聊天' : `${PLATFORMS[channel] ?? channel}${address?.kind === 'group' ? '群' : '私聊'}`
  // Her conversations with someone: the desktop chat and the platform chats, not her own errands (steward's `own`).
  const all = Object.values(host.conversations())
    .filter((conversation) => conversation.channel === 'app' || conversation.address)
  const others = all.filter((conversation) => conversation.sessionId !== sessionId)

  if (!who?.trim()) {
    const rows = await Promise.all(others.map(async (conversation) => {
      const last = lines(host, await sessionOf(host, conversation), conversation, nameIn, 1)[0]
      return `- ${nameIn(conversation.recipient)}（${where(conversation)}）：${last ? '最近一句在 ' + last.when : '还没说过话'}`
    }))
    return rows.length ? `你的其他对话：\n${rows.join('\n')}\n${KEEP}` : '除了这里，你还没有别的对话。'
  }

  const asked = who.trim()
  const found = all.filter((conversation) => nameIn(conversation.recipient) === asked)
  if (!found.length) return `没有和「${asked}」的对话。有的是：${[...new Set(others.map((c) => nameIn(c.recipient)))].join('、') || '没有别的'}`
  const parts = await Promise.all(found.map(async (conversation) => {
    const said = lines(host, await sessionOf(host, conversation), conversation, nameIn, LINES).reverse()
    const files = deliveries.filter(d => d.key === host.ctx.get('kin')!.scopeOf(conversation.sessionId).conversation_key
      && d.parts.some(part => 'file' in part)).slice(0, LINES)
      .flatMap(d => d.parts.flatMap((part, i) => 'file' in part ? [
        `- ${stamp(d.at)} ${PART_WORDS[part.kind]} ${part.file}（${d.sent[i]?.state === 'accepted' ? (d.sent[i]?.preview ? '平台已接收缩小的预览' : '平台已接收') : d.state === 'cancelled' ? '发送失败' : '未确认送达'}）`,
      ] : []))
    return `和${asked}（${where(conversation)}）最近的话：\n${said.map((line) => `- ${line.when} ${line.text}`).join('\n') || '（还没说过话）'}`
      + (files.length ? '\n经手的附件：\n' + files.join('\n') : '')
  }))
  return `${parts.join('\n\n')}\n${KEEP}`
}

async function sessionOf(host: Host, conversation: Conversation): Promise<Session> {
  const resolved = await host.ctx.get('sessionController')!.resolveAgent(conversation.sessionId as never)
  if ('error' in resolved) throw new Error('that conversation is not open: ' + JSON.stringify(resolved.error))
  return resolved.agent.session
}

/** The newest `limit` things said in a session, newest first, each with who said it: a person by name, or 你 for hers. */
function lines(host: Host, session: Session, conversation: Conversation, nameIn: (id?: string) => string, limit: number) {
  const found: { when: string; text: string }[] = []
  for (let seq = Number(session.seq) - 1; seq >= 0 && found.length < limit; seq--) {
    const event = session.eventAt(SessionSeq(seq))
    let said = ''
    if (event?.type === 'assistant/message') {
      const text = textOf(event.data.message)
      said = text && '你：' + text
    } else if (event?.type === 'tool/result' && !event.data.message.isError) {
      const store = host.ctx.get('attachments')
      const paths = event.data.message.content.flatMap(block => {
        const path = block.type === 'image' ? store?.imageHostPath(block.attachment)
          : block.type === 'file' ? store?.fileHostPath(block.attachment) : undefined
        return path ? [path] : []
      })
      if (paths.length) {
        found.push({ when: when(event.time), text: '工具返回的本机附件：' + paths.join('、') })
        continue
      }
    } else if (event?.type === 'user/message') {
      const { source } = event.data
      const text = textOf(event.data)
      // What arrives from anyone but the owner in the owner's own chat already names who said it (conversations.ts).
      if (source.kind === 'kin-said') said = '你：' + source.text
      else if (source.kind === 'user' || (source.kind === 'kin-channel' && conversation.recipient === OWNER)) said = text && `${nameIn(OWNER)}：${text}`
      else if (source.kind === 'kin-channel') said = text
    }
    if (said) found.push({ when: when(event!.time), text: clip(said).replaceAll('\n', '\n  ') })
  }
  return found
}

const clip = (text: string) => {
  const line = text.replace(/\n\s*\n/g, '\n')
  return line.length > 300 ? line.slice(0, 300) + '…' : line
}
