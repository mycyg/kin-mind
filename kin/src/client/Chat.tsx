/**
 * KIN's own chat box over her desktop session: the session's live event window from DSH's client
 * store, drawn as a messenger. Her words stream in as she types them; what she hands to helpers or
 * looks up shows as a small note between the bubbles.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent } from 'react'
import type { Context } from '@deepseek-ai/cordis'
import type { ISessions, PendingSubmission, SessionBinding, SessionEventLikeEntry } from '@deepseek-ai/dsh-api-session-controller/client'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import { answerText } from '../model-output.js'
import { Heart, Helper, Stop } from './icons.js'
import { Orb } from './Orb.js'
import { clock, day } from './time.js'

declare module '@deepseek-ai/dsh-api-session-controller/client' {
  interface SessionReferenceSourceMap {
    kin: unknown
  }
}

type Item =
  | { kind: 'day'; key: string; label: string }
  | { kind: 'me'; key: string; text: string; at: number; pending?: boolean; via?: string }
  | { kind: 'kin'; key: string; text: string; at: number; live?: boolean; media?: { kind: 'image' | 'voice'; file: string } }
  | { kind: 'act'; key: string; text: string; at: number }

const CHANNELS: Record<string, string> = { wechat: '微信', feishu: '飞书', group: '群聊' }
const SUGGESTIONS = ['今天过得怎么样？', '你在想什么呀', '帮我查点东西']

interface Observable<T> { getSnapshot(): T; subscribe(fn: () => void): () => void }
/** The parts of the session's client snapshots the chat reads. */
interface EventWindow { readonly entries: readonly SessionEventLikeEntry[]; readonly hasMore: boolean }
interface SessionView {
  readonly pendingSubmissions: readonly PendingSubmission[]
  readonly running: boolean
  readonly openState: 'cold' | 'loading' | 'open' | 'error'
  readonly loadingOlder: boolean
  readonly lastAgentError: string | null
  readonly promptError: { readonly error: { readonly message: string } } | null
}
const NOTHING: Observable<undefined> = { getSnapshot: () => undefined, subscribe: () => () => {} }

function useObservable<T>(source: Observable<T> | undefined): T | undefined {
  const target = (source ?? NOTHING) as Observable<T | undefined>
  const subscribe = useCallback((fn: () => void) => target.subscribe(fn), [target])
  const read = useCallback(() => target.getSnapshot(), [target])
  return useSyncExternalStore(subscribe, read)
}

export function createChat(ctx: Context) {
  /** The session's shared client binding, held while the chat is on screen. */
  function useBinding(sessionId: string | undefined) {
    const [binding, setBinding] = useState<SessionBinding>()
    useEffect(() => {
      if (!sessionId) return
      const controller = new AbortController()
      // The page's `sessions` is the client store; the host's declaration of the same name does not apply here.
      const sessions = ctx.sessions as unknown as ISessions
      const reference = sessions.retain(sessionId as SessionId, { source: 'kin', signal: controller.signal })
      let held = true
      reference.ready.then((value) => { if (held) setBinding(value) }, () => {})
      return () => {
        held = false
        controller.abort()
        reference.release()
        setBinding(undefined)
      }
    }, [sessionId])
    return binding
  }

  return function Chat({ sessionId, warmth }: { sessionId: string | undefined; warmth: number }) {
    const binding = useBinding(sessionId)
    const events = useObservable(binding?.eventSource as Observable<EventWindow> | undefined)
    const session = useObservable(binding?.session as Observable<SessionView> | undefined)
    const items = useMemo(() => assemble(events?.entries ?? [], session?.pendingSubmissions ?? []), [events, session?.pendingSubmissions])
    const running = session?.running ?? false
    const typing = running && items.at(-1)?.kind !== 'kin'

    const list = useRef<HTMLDivElement>(null)
    const pinned = useRef(true)
    const older = useRef<number | null>(null)
    useLayoutEffect(() => {
      const element = list.current
      if (!element) return
      if (older.current !== null) {
        element.scrollTop = element.scrollHeight - older.current
        older.current = null
      } else if (pinned.current) {
        element.scrollTop = element.scrollHeight
      }
    }, [items, typing])

    const loadOlder = () => {
      if (!list.current || !binding) return
      older.current = list.current.scrollHeight - list.current.scrollTop
      void binding.session.loadOlder()
    }

    const send = (text: string) => {
      if (!binding || !text.trim()) return
      pinned.current = true
      const handle = binding.session.beginSubmission({ mode: 'queue', text, attachments: [] })
      void binding.session.prompt([{ type: 'text', text }], 'queue', undefined, handle.requestId)
    }

    const ready = binding && session?.openState === 'open'
    return (
      <div className="kin-chat">
        <div className="kin-chat-list" ref={list}
          onScroll={(event) => {
            const element = event.currentTarget
            pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80
          }}>
          {events?.hasMore ? (
            <button type="button" className="kin-older" onClick={loadOlder} disabled={session?.loadingOlder}>
              {session?.loadingOlder ? '在翻以前的聊天…' : '看看更早的聊天'}
            </button>
          ) : null}
          {ready && items.length === 0 ? <Hello warmth={warmth} onPick={send} /> : null}
          {!ready ? <div className="kin-chat-wait">{session?.openState === 'error' ? '聊天记录没打开，稍后再试试' : '正在打开和她的聊天…'}</div> : null}
          {items.map((item, index) => {
            if (item.kind === 'day') return <div key={item.key} className="kin-day"><span>{item.label}</span></div>
            if (item.kind === 'act') {
              return <div key={item.key} className="kin-act"><Helper size={14} /><span>{item.text}</span></div>
            }
            const previous = items[index - 1]
            const first = previous?.kind !== item.kind
            return (
              <div key={item.key} className="kin-msg" data-from={item.kind} data-first={first ? '' : undefined}
                data-pending={item.kind === 'me' && item.pending ? '' : undefined}>
                {item.kind === 'kin' ? (first ? <Orb size={28} warmth={warmth} /> : <span className="kin-msg-gap" />) : null}
                <div className="kin-bubble" title={clock(item.at)}>
                  {item.kind === 'me' && item.via ? <span className="kin-via">{item.via}</span> : null}
                  {item.kind === 'kin' && item.media ? <Media {...item.media} words={item.text} /> : item.text}
                </div>
              </div>
            )
          })}
          {typing ? (
            <div className="kin-msg" data-from="kin" data-first="">
              <Orb size={28} warmth={warmth} />
              <div className="kin-bubble kin-typing" aria-label="KIN 正在想"><i /><i /><i /></div>
            </div>
          ) : null}
          {!running && session?.lastAgentError ? <div className="kin-chat-error">她刚才没能说出来：{session.lastAgentError}</div> : null}
        </div>
        <Composer running={running} onSend={send} onStop={() => void binding?.session.cancel()}
          error={session?.promptError ? `没发出去：${session.promptError.error.message}` : undefined} />
      </div>
    )
  }
}

function Hello({ warmth, onPick }: { warmth: number; onPick: (text: string) => void }) {
  return (
    <div className="kin-hello">
      <Orb size={64} warmth={warmth} />
      <p>和 KIN 打个招呼吧</p>
      <div className="kin-hello-picks">
        {SUGGESTIONS.map((text) => <button type="button" key={text} className="kin-chip" onClick={() => onPick(text)}>{text}</button>)}
      </div>
    </div>
  )
}

function Composer({ running, onSend, onStop, error }: {
  running: boolean; onSend: (text: string) => void; onStop: () => void; error?: string
}) {
  const [text, setText] = useState('')
  const submit = () => {
    if (!text.trim()) return
    onSend(text.trim())
    setText('')
  }
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }
  return (
    <div className="kin-composer">
      {error ? <div className="kin-composer-error">{error}</div> : null}
      <div className="kin-composer-box">
        <textarea rows={1} value={text} placeholder="和 KIN 说点什么…" aria-label="给 KIN 的消息"
          onChange={(event) => setText(event.target.value)} onKeyDown={onKeyDown} />
        {running && !text.trim() ? (
          <button type="button" className="kin-send" data-stop="" onClick={onStop} aria-label="让她先停一下"><Stop size={14} /></button>
        ) : (
          <button type="button" className="kin-send" onClick={submit} aria-label="发送" data-empty={text.trim() ? undefined : ''}><Heart size={16} /></button>
        )}
      </div>
    </div>
  )
}

/** The event window as bubbles: what a person said, what she said, and what she did in between. */
function assemble(entries: readonly SessionEventLikeEntry[], pending: readonly PendingSubmission[]): Item[] {
  const items: Item[] = []
  const streaming = new Map<string, Extract<Item, { kind: 'kin' }>>()
  let lastDay = ''
  let lastAt = 0
  // A new day, or an hour of quiet, starts with the time it is.
  const add = (item: Exclude<Item, { kind: 'day' }>) => {
    const date = day(item.at)
    if (date !== lastDay || item.at - lastAt > 3_600_000) items.push({ kind: 'day', key: 'day:' + item.key, label: `${date} ${clock(item.at)}` })
    lastDay = date
    lastAt = item.at
    items.push(item)
  }
  for (const entry of entries) {
    const at = new Date(entry.event.time).getTime()
    if (entry.type === 'transient') {
      const { attemptId, chunk } = entry.event.data
      if (chunk.type !== 'text-delta') continue
      const live = streaming.get(attemptId)
      if (live) live.text += chunk.text
      else {
        const item = { kind: 'kin' as const, key: 'live:' + attemptId, text: chunk.text, at, live: true }
        streaming.set(attemptId, item)
        add(item)
      }
      continue
    }
    const event = entry.event
    if (event.type === 'user/message') {
      const source = event.data.source as { kind: string; channel?: string; text?: string; media?: { kind: 'image' | 'voice'; file: string } }
      // What she sent on her own, told to this conversation, and her stickers and voice messages here: hers.
      if (source.kind === 'kin-said' && source.text) add({ kind: 'kin', key: 'e' + event.seq, text: source.text, at, media: source.media })
      if (source.kind !== 'user' && source.kind !== 'kin-channel') continue
      const text = textOf(event.data.content)
      if (text) add({ kind: 'me', key: 'e' + event.seq, text, at, via: source.channel ? CHANNELS[source.channel.split(':')[0]!] : undefined })
    } else if (event.type === 'assistant/message') {
      event.data.message.content.forEach((block, index) => {
        const key = `e${event.seq}:${index}`
        if (block.type === 'text' && answerText(block.text).trim()) add({ kind: 'kin', key, text: answerText(block.text).trim(), at })
        else if (block.type === 'tool-call') add({ kind: 'act', key, text: actOf(block.name, block.arguments), at })
      })
    }
  }
  for (const submission of pending) {
    add({ kind: 'me', key: 'pending:' + submission.requestId, text: submission.text, at: submission.time, pending: true })
  }
  return items
}

/** A sticker or voice message of hers in this chat, from her media route; a voice message keeps its words under the clip. */
function Media({ kind, file, words }: { kind: 'image' | 'voice'; file: string; words: string }) {
  const src = 'api/kin/media?file=' + encodeURIComponent(file)
  if (kind === 'image') return <img className="kin-sticker" src={src} alt={words} />
  return <><audio className="kin-audio" controls preload="none" src={src} />{words}</>
}

function textOf(content: readonly { type: string; text?: string }[]): string {
  return content.flatMap((block) => (block.type === 'text' && block.text ? [block.text] : [])).join('\n').trim()
}

/** A tool call as one line of what she did. */
function actOf(name: string, raw: string): string {
  let args: Record<string, unknown> = {}
  try { args = JSON.parse(raw) } catch { /* a call cut short keeps its name */ }
  const said = (...keys: string[]) => {
    const value = keys.map((key) => args[key]).find((v) => typeof v === 'string' && v.trim()) as string | undefined
    return value ? '：' + (value.length > 36 ? value.slice(0, 36) + '…' : value) : ''
  }
  // Core's managing tools carry what they do in their request.
  const action = () => (args.request as { action?: string } | undefined)?.action
  switch (name) {
    case 'subagent': return '叫了个小帮手' + said('description', 'prompt')
    case 'send_message': return '给小帮手捎了句话' + said('message', 'text')
    case 'list_agents': return '看了看小帮手们在忙什么'
    case 'kin_delegate': return '交给帮手' + said('title', 'task')
    case 'kin_work': return '看了看手头的活'
    case 'kin_work_tell': return '给帮手捎了句话' + said('message')
    case 'kin_work_stop': return '叫停了一个帮手'
    case 'interrupt_agent': return '叫停了一个帮手'
    case 'web_search': return '去查了查' + said('query')
    case 'web_fetch': return '翻了翻网页' + said('url')
    case 'kin_recall': return '回想了一下' + said('about')
    case 'todo_write': return '记了个小清单'
    case 'schedule_create': return '定了个提醒' + said('title', 'prompt', 'description')
    case 'schedule_update': return '改了个提醒'
    case 'schedule_delete': return '取消了一个提醒'
    case 'schedule_list': return '看了看提醒'
    case 'kin_tell': return '帮忙带了句话' + said('to')
    case 'kin_send_file': return '发了个文件过去'
    case 'kin_voice': return '用语音说' + said('text')
    case 'kin_sticker': return '发了个表情' + said('meaning')
    case 'kin_conversations': return args.who ? '看了看和' + args.who + '的对话' : '看了看别处的对话'
    case 'kin_reply': return args.choice === 'later' ? '想晚点再回' : args.choice === 'merge' ? '等你下一句一起回' : '这次先不回'
    case 'kin_note': return '在心里记了一笔'
    case 'kin_persona': return '更新自己的人设'
    case 'read_autonomous_plans': return '翻了翻自己的计划'
    case 'manage_autonomous_plan': return action() === 'create' ? '定了个计划' : '改了改计划'
    case 'manage_desire': return action() === 'create' ? '许了个心愿' : action() === 'complete' ? '了了一个心愿' : '想了想心愿'
    case 'manage_concern': return action() === 'create' ? '记下一件心事' : ['resolve', 'ease'].includes(action() ?? '') ? '放下了一件心事' : '想了想心事'
    case 'update_conversation_habits': return '记住了你的习惯'
    case 'read_affective_state': return '看了看自己的心情'
    case 'read_trait_ledger': return '想了想自己是个什么样的人'
    case 'read_self_knowledge': return '想了想自己'
    case 'record_self_claim': return '对自己多了点认识'
    case 'predict_self_behavior': return '猜了猜自己会怎么做'
    case 'assess_self_prediction': return '对了对自己的猜测'
    case 'read_graph': return '理了理记得的人和事'
    case 'read_event_thread': return '把一件事从头想了想'
    case 'read_memory': return '翻了翻原话'
    case 'read_archived_record': return '翻了翻旧事'
    case 'browse_topics': return '翻了翻聊过的话题'
    case 'read_procedure_memory': return '想了想以前的做法'
    case 'read_share_history': return '想了想跟你分享过什么'
    case 'read_work_history': return '翻了翻自己做过的东西'
    case 'mark_memorable': return '把这段记在了心上'
    case 'set_memory_quiet': return (args.request as { quiet?: boolean } | undefined)?.quiet === false ? '又可以提起那件事了' : '答应不再主动提'
    case 'revise_graph': case 'correct_memory': return '改正了记错的事'
    case 'read_private_notes': return '翻了翻心里记下的'
    case 'read_dreams': return '想起了做过的梦'
    case 'read_diary': return '翻了翻日记'
    case 'read_letters': return '看了看信箱'
    default: return '用了 ' + name
  }
}
