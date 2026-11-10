/**
 * KIN's own chat box over her desktop session: the session's live event window from DSH's client
 * store, drawn as a messenger. Her words stream in as she types them; what she hands to helpers or
 * looks up shows as a small note between the bubbles. Pictures and files go to her the way DSH's
 * own composer sends them: a picture as its bytes in the prompt, any other file staged on the host first.
 * What is said into the microphone is written out on her host and lands in the box, to be looked over before sending.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent } from 'react'
import type { Context } from '@deepseek-ai/cordis'
import type { ISession, ISessions, PendingSubmission, SessionBinding, SessionEventLikeEntry } from '@deepseek-ai/dsh-api-session-controller/client'
import type { PromptContentPart } from '@deepseek-ai/dsh-api-session-controller/types'
import type { FileAttachmentRef, ImageAttachmentLimits, ImageAttachmentRef, ImageMediaType } from '@deepseek-ai/dsh-attachment/types'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import { answerText } from '../model-output.js'
import { useApi } from './api.js'
import { Close, Heart, Helper, Stop } from './icons.js'
import { Orb } from './Orb.js'
import { clock, day } from './time.js'

declare module '@deepseek-ai/dsh-api-session-controller/client' {
  interface SessionReferenceSourceMap {
    kin: unknown
  }
}

/** A picture in a person's bubble: a preview already in the browser, or one DSH keeps, read from her session. */
interface Shot { url?: string; stored?: ImageAttachmentRef; width?: number; height?: number }
/** A bubble's picture opened large. */
interface Viewing { src: string; alt: string }
/** Words her page starts a message with; each new id puts them in again. */
interface Seed { text: string; id: number }

type Item =
  | { kind: 'day'; key: string; label: string }
  | { kind: 'me'; key: string; text: string; at: number; pictures: Shot[]; files: { name: string; bytes: number }[]; pending?: boolean; via?: string }
  | { kind: 'kin'; key: string; text: string; at: number; live?: boolean; media?: { kind: 'image' | 'voice'; file: string } }
  | { kind: 'act'; key: string; text: string; at: number }

/** A file DSH has staged for her session: the receipt a prompt cites, and what it keeps. */
interface Staged { receiptId: Extract<PromptContentPart, { type: 'file' }>['receiptId']; file: FileAttachmentRef }
/** Something picked for the next message. A picture goes as its bytes; any other file is staged on the host as soon as it is picked. */
type Draft =
  | { kind: 'image'; id: number; file: File; url: string; width: number; height: number }
  | { kind: 'file'; id: number; file: File; stop: AbortController; staged?: Staged }
/** The part of DSH's upload service (`fileUpload`, mounted beside `sessions`) the chat uses. */
interface FileUpload {
  upload(sessionId: SessionId, data: Blob, name?: string, signal?: AbortSignal): Promise<{ ok: true; value: Staged } | { ok: false }>
}

const CHANNELS: Record<string, string> = { wechat: '微信', feishu: '飞书', group: '群聊' }
const SUGGESTIONS = ['今天过得怎么样？', '你在想什么呀', '帮我查点东西']
/** The pictures DSH admits (its ImageMediaType); anything else goes as a file. */
const IMAGE_TYPES: readonly string[] = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
/** Why DSH turned down a message with something attached, in her page's words; any other refusal keeps DSH's own. */
const REFUSED: Record<string, string> = { MODEL_DOES_NOT_SUPPORT_IMAGES: '她现在用的模型看不了图片', FILE_NOT_STAGED: '文件得重新选一下' }
/** Drafts made so far, for telling them apart. */
let drafted = 0
/** Longest a recording runs before it stops by itself and is heard: a couple of MB, far inside a route's body cap. */
const LONGEST = 120_000

interface Observable<T> { getSnapshot(): T; subscribe(fn: () => void): () => void }
/** The parts of the session's client snapshots the chat reads. */
interface EventWindow { readonly entries: readonly SessionEventLikeEntry[]; readonly hasMore: boolean }
interface SessionView {
  readonly pendingSubmissions: readonly PendingSubmission[]
  readonly running: boolean
  readonly openState: 'cold' | 'loading' | 'open' | 'error'
  readonly loadingOlder: boolean
  readonly lastAgentError: string | null
  readonly promptError: { readonly error: { readonly message: string; readonly details?: { readonly reason?: string } } } | null
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

  return function Chat({ sessionId, warmth, seed }: { sessionId: string | undefined; warmth: number; seed?: Seed }) {
    const binding = useBinding(sessionId)
    const events = useObservable(binding?.eventSource as Observable<EventWindow> | undefined)
    const session = useObservable(binding?.session as Observable<SessionView> | undefined)
    const items = useMemo(() => assemble(events?.entries ?? [], session?.pendingSubmissions ?? []), [events, session?.pendingSubmissions])
    const running = session?.running ?? false
    const typing = running && items.at(-1)?.kind !== 'kin'

    const [drafts, setDrafts] = useState<Draft[]>([])
    const [notice, setNotice] = useState<string>()
    const [dropping, setDropping] = useState(false)
    const [viewing, setViewing] = useState<Viewing>()
    // The microphone shows only where her host can write out what is said (channels/index.ts).
    const hearing = useApi<{ hearing: boolean }>('api/kin/hear').data?.hearing ?? false
    // A file still on its way to the host holds the message back.
    const waiting = drafts.some((draft) => draft.kind === 'file' && !draft.staged)
    // Read without `inject`, so the chat still opens where the service is not mounted: pictures only then.
    const upload: FileUpload | undefined = ctx.get('fileUpload')
    // Previews of the pictures picked here; one that was sent keeps showing through its preview until the chat leaves the screen.
    const previews = useRef(new Set<string>())
    useEffect(() => () => previews.current.forEach((url) => URL.revokeObjectURL(url)), [])

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
    }, [items, typing, drafts.length])

    const loadOlder = () => {
      if (!list.current || !binding) return
      older.current = list.current.scrollHeight - list.current.scrollTop
      void binding.session.loadOlder()
    }

    /** What was picked, pasted or dropped, readied for the next message: pictures within what her session admits, other files staged on the host. */
    const attach = async (files: File[]) => {
      if (!binding || !files.length) return
      setNotice(undefined)
      const limits = binding.session.projections.faceOf('imageLimits').getSnapshot() as ImageAttachmentLimits | undefined
      const pictures = [...drafts.flatMap((draft) => (draft.kind === 'image' ? [draft.file] : [])), ...files.filter((file) => IMAGE_TYPES.includes(file.type))]
      if (limits && pictures.length > limits.maxImagesPerMessage) return setNotice(`一条消息最多带 ${limits.maxImagesPerMessage} 张图片`)
      if (limits && (pictures.some((file) => file.size > limits.maxImageBytes) || pictures.reduce((sum, file) => sum + file.size, 0) > limits.maxMessageImageBytes)) {
        return setNotice(`图片太大了：一张别超过 ${sizeOf(limits.maxImageBytes)}，一条消息别超过 ${sizeOf(limits.maxMessageImageBytes)}`)
      }
      for (const file of files) {
        if (IMAGE_TYPES.includes(file.type)) {
          // Its size is known before a bubble shows it, and one that will not open is turned away here rather than by the host.
          const url = URL.createObjectURL(file)
          const probe = new Image()
          probe.src = url
          if (await probe.decode().then(() => true, () => false)) {
            previews.current.add(url)
            const draft: Draft = { kind: 'image', id: ++drafted, file, url, width: probe.naturalWidth, height: probe.naturalHeight }
            setDrafts((now) => [...now, draft])
          } else {
            URL.revokeObjectURL(url)
            setNotice(`「${file.name}」打不开，换一张试试`)
          }
        } else if (upload) {
          const draft: Extract<Draft, { kind: 'file' }> = { kind: 'file', id: ++drafted, file, stop: new AbortController() }
          setDrafts((now) => [...now, draft])
          void upload.upload(binding.sessionId, file, file.name || undefined, draft.stop.signal).catch(() => undefined).then((result) => {
            if (draft.stop.signal.aborted) return
            if (result?.ok) return setDrafts((now) => now.map((it) => (it.id === draft.id ? { ...draft, staged: result.value } : it)))
            setDrafts((now) => now.filter((it) => it.id !== draft.id))
            setNotice(`「${file.name}」没传上去，再试一次吧`)
          })
        } else {
          setNotice('现在只能给她看图片')
        }
      }
    }

    const discard = (draft: Draft) => {
      setDrafts((now) => now.filter((it) => it.id !== draft.id))
      if (draft.kind === 'file') return draft.stop.abort()
      previews.current.delete(draft.url)
      URL.revokeObjectURL(draft.url)
    }

    /** Takes a message, or answers false when it cannot yet (her session still opening), so the box keeps it. */
    const send = (text: string, refused?: () => void): boolean => {
      if (!binding || waiting || !(text.trim() || drafts.length)) return false
      pinned.current = true
      const going = drafts
      setDrafts([])
      setNotice(undefined)
      // While she is busy, what is said reaches her at her next step instead of waiting for the whole turn.
      const mode = running ? 'steer' : 'queue'
      const session: ISession = binding.session
      const handle = session.beginSubmission({
        mode, text,
        attachments: going.map((draft) => draft.kind === 'image'
          ? { type: 'image', value: { previewUrl: draft.url, name: draft.file.name || undefined, width: draft.width, height: draft.height } }
          : { type: 'file', value: draft.staged!.file }),
        // Turned down (her model cannot take pictures, say) or dropped before she read it: back into the box, not lost.
        onRetire: (end) => {
          if (end.reason !== 'failed') return
          setDrafts((now) => [...going, ...now])
          refused?.()
        },
      })
      void (async () => {
        try {
          const parts = await Promise.all(going.map(async (draft): Promise<PromptContentPart> => draft.kind === 'image'
            ? { type: 'image', mediaType: draft.file.type as ImageMediaType, data: await base64Of(draft.file), name: draft.file.name || undefined }
            : { type: 'file', receiptId: draft.staged!.receiptId }))
          void session.prompt([...parts, ...(text ? [{ type: 'text' as const, text }] : [])], mode, undefined, handle.requestId)
        } catch {
          handle.abandon()
          setNotice('图片读不出来了，重新选一下吧')
        }
      })()
      return true
    }

    const ready = binding && session?.openState === 'open'
    const refusal = session?.promptError?.error
    return (
      <div className="kin-chat" onDragEnter={(event) => { if (binding && event.dataTransfer.types.includes('Files')) setDropping(true) }}>
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
            const attached = item.kind === 'me' && item.pictures.length + item.files.length > 0
            return (
              <div key={item.key} className="kin-msg" data-from={item.kind} data-first={first ? '' : undefined}
                data-pending={item.kind === 'me' && item.pending ? '' : undefined}>
                {item.kind === 'kin' ? (first ? <Orb size={28} warmth={warmth} /> : <span className="kin-msg-gap" />) : null}
                <div className="kin-bubble" title={clock(item.at)} data-attached={attached ? '' : undefined}>
                  {item.kind === 'me' && item.via ? <span className="kin-via">{item.via}</span> : null}
                  {item.kind === 'me' && attached ? <Attached pictures={item.pictures} files={item.files} text={item.text} session={binding?.session} onView={setViewing} />
                    : item.kind === 'kin' && item.media ? <Media {...item.media} words={item.text} /> : item.text}
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
        <Composer running={running} drafts={drafts} waiting={waiting} picturesOnly={!upload} hearing={hearing} seed={seed}
          onAttach={attach} onDiscard={discard} onSend={send} onStop={() => void binding?.session.cancel()} onNotice={setNotice}
          error={notice ?? (refusal ? `没发出去：${REFUSED[refusal.details?.reason ?? ''] ?? refusal.message}` : undefined)} />
        {dropping ? (
          // The veil takes the drag once it is up, so leaving it is leaving the chat.
          <div className="kin-drop" onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDropping(false)}
            onDrop={(event) => {
              event.preventDefault()
              setDropping(false)
              void attach([...event.dataTransfer.files])
            }}>
            <span><i><Clip size={16} /></i>松手，就给她看</span>
          </div>
        ) : null}
        {viewing ? <Viewer {...viewing} onClose={() => setViewing(undefined)} /> : null}
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

function Composer({ running, drafts, waiting, picturesOnly, hearing, seed, onAttach, onDiscard, onSend, onStop, onNotice, error }: {
  running: boolean; drafts: Draft[]; waiting: boolean; picturesOnly: boolean; hearing: boolean; seed?: Seed
  onAttach: (files: File[]) => void; onDiscard: (draft: Draft) => void
  onSend: (text: string, refused: () => void) => boolean; onStop: () => void; onNotice: (notice?: string) => void; error?: string
}) {
  const [text, setText] = useState('')
  const box = useRef<HTMLTextAreaElement>(null)
  const picker = useRef<HTMLInputElement>(null)
  // Words put in from elsewhere go onto the box at once too, so the caret has its place before React renders them.
  const place = (next: string, caret: number) => {
    setText(next)
    const area = box.current!
    area.value = next
    area.focus()
    area.setSelectionRange(caret, caret)
  }
  // Her page's seed starts the message; what was already written moves down a line rather than being lost.
  useEffect(() => {
    if (!seed) return
    const now = box.current!.value
    place(now.trim() ? `${seed.text}\n${now}` : seed.text, seed.text.length)
  }, [seed?.id])
  // What the microphone heard joins the end (a space only between Latin words), for a look before it is sent.
  const heard = (words: string) => {
    const now = box.current!.value
    const next = now + (!now || /[\s\u3000-\u9fff\uff00-\uffef]$/.test(now) ? '' : ' ') + words
    place(next, next.length)
  }
  const empty = !text.trim() && !drafts.length
  const submit = () => {
    if (empty || waiting) return
    const said = text.trim()
    // A message turned down comes back with its words, above whatever has been written since;
    // one the chat could not take yet stays where it is.
    if (onSend(said, () => setText((now) => (said && now.trim() ? `${said}\n${now}` : now || said)))) setText('')
  }
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }
  const attachLabel = picturesOnly ? '给她看图片' : '给她看图片或文件'
  return (
    <div className="kin-composer">
      {error ? <div className="kin-composer-error">{error}</div> : null}
      <div className="kin-composer-box">
        {drafts.length ? (
          <div className="kin-drafts">
            {drafts.map((draft) => (
              <div key={draft.id} className="kin-draft" data-staging={draft.kind === 'file' && !draft.staged ? '' : undefined}>
                {draft.kind === 'image' ? <img src={draft.url} alt={draft.file.name} />
                  : <FileChip name={draft.file.name} note={draft.staged ? sizeOf(draft.file.size) : '正在送过去…'} />}
                <button type="button" className="kin-draft-remove" onClick={() => onDiscard(draft)} aria-label={`不发「${draft.file.name}」了`}><Close size={11} /></button>
              </div>
            ))}
          </div>
        ) : null}
        <button type="button" className="kin-attach" onClick={() => picker.current?.click()} aria-label={attachLabel} title={attachLabel}><Clip size={18} /></button>
        <input ref={picker} type="file" multiple hidden accept={picturesOnly ? IMAGE_TYPES.join(',') : undefined}
          onChange={(event) => {
            onAttach([...(event.target.files ?? [])])
            event.target.value = ''
          }} />
        <textarea ref={box} rows={1} value={text} placeholder="和 KIN 说点什么…" aria-label="给 KIN 的消息"
          onChange={(event) => setText(event.target.value)} onKeyDown={onKeyDown}
          onPaste={(event) => onAttach([...event.clipboardData.files])} />
        {hearing ? <Mic onHeard={heard} onNotice={onNotice} /> : null}
        {running && empty ? (
          <button type="button" className="kin-send" data-stop="" onClick={onStop} aria-label="让她先停一下"><Stop size={14} /></button>
        ) : (
          <button type="button" className="kin-send" onClick={submit} aria-label="发送" data-empty={empty || waiting ? '' : undefined}><Heart size={16} /></button>
        )}
      </div>
    </div>
  )
}

/**
 * The microphone: press to speak, press again and her host writes out what was said into the box; never sent by
 * itself. A recording stops by itself at LONGEST. Whatever the browser records goes as it is; the host reads it.
 */
function Mic({ onHeard, onNotice }: { onHeard: (words: string) => void; onNotice: (notice?: string) => void }) {
  const [phase, setPhase] = useState<'asking' | 'recording' | 'hearing'>()
  const [seconds, setSeconds] = useState(0)
  const recorder = useRef<MediaRecorder>()
  const stop = () => { if (recorder.current?.state === 'recording') recorder.current.stop() }
  // While it records, the seconds go by, up to the longest.
  useEffect(() => {
    if (phase !== 'recording') return
    const started = Date.now()
    setSeconds(0)
    const tick = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 250)
    const cap = setTimeout(stop, LONGEST)
    return () => {
      clearInterval(tick)
      clearTimeout(cap)
    }
  }, [phase])
  // Leaving the screen lets go of the microphone, one granted only afterwards too; what was being said is not heard.
  const gone = useRef(false)
  useEffect(() => {
    gone.current = false
    return () => {
      gone.current = true
      if (!recorder.current) return
      recorder.current.onstop = null
      recorder.current.stream.getTracks().forEach((track) => track.stop())
    }
  }, [])

  const start = async () => {
    onNotice(undefined)
    setPhase('asking')
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (error) {
      // No mediaDevices at all (a page not on this machine or https) lands here too.
      const name = (error as Error).name
      setPhase(undefined)
      return onNotice(name === 'NotAllowedError' ? '麦克风没被允许：在系统设置或浏览器里允许后再试试'
        : name === 'NotFoundError' ? '没找到麦克风' : '这里现在用不了麦克风')
    }
    if (gone.current) return stream.getTracks().forEach((track) => track.stop())
    const take = new MediaRecorder(stream)
    const chunks: Blob[] = []
    take.ondataavailable = (event) => chunks.push(event.data)
    take.onstop = () => {
      stream.getTracks().forEach((track) => track.stop())
      recorder.current = undefined
      void hear(new Blob(chunks, { type: take.mimeType }))
    }
    take.start()
    recorder.current = take
    setPhase('recording')
  }

  const hear = async (audio: Blob) => {
    setPhase('hearing')
    try {
      if (!audio.size) throw new Error('没录到声音')
      const response = await fetch('api/kin/hear', { method: 'POST', body: audio })
      const answer: { text?: string; error?: string } = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(answer.error ?? `${response.status} ${response.statusText}`)
      if (answer.text?.trim()) onHeard(answer.text.trim())
      else onNotice('没听清说了什么，再说一次吧')
    } catch (error) {
      onNotice('没听写出来：' + (error as Error).message)
    } finally {
      setPhase(undefined)
    }
  }

  return (
    <button type="button" className="kin-mic" data-phase={phase} disabled={phase === 'asking' || phase === 'hearing'}
      onClick={phase === 'recording' ? stop : () => void start()}
      aria-label={phase === 'recording' ? '说完了' : phase === 'hearing' ? '在听写' : '语音输入'}
      title={phase === 'recording' ? '说完再按一下，最长两分钟' : phase ? undefined : '语音输入，说完再按一下'}>
      {phase === 'recording' ? <><i />{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, '0')}</>
        : phase === 'hearing' ? '在听写…' : <Microphone size={18} />}
    </button>
  )
}

/** A bubble's picture, large over the chat on a veil of glass. Esc or a click puts it back, and focus returns where it was. */
function Viewer({ src, alt, onClose }: Viewing & { onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null
    close.current!.focus()
    return () => before?.focus()
  }, [])
  return (
    <div className="kin-viewer" role="dialog" aria-label={alt} onClick={onClose}
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return
        event.stopPropagation()
        onClose()
      }}>
      <img src={src} alt={alt} />
      <button type="button" ref={close} className="kin-viewer-close" aria-label="收起图片"><Close size={14} /></button>
    </div>
  )
}

/** What a person sent along with their words: pictures first, then files, then what they said. */
function Attached({ pictures, files, text, session, onView }: {
  pictures: Shot[]; files: { name: string; bytes: number }[]; text: string; session: Pick<ISession, 'readAttachment'> | undefined
  onView: (picture: Viewing) => void
}) {
  return (
    <>
      {pictures.length ? (
        <div className="kin-pictures" data-many={pictures.length > 1 ? '' : undefined}>
          {pictures.map((shot, index) => <Picture key={index} {...shot} alone={pictures.length === 1} session={session} onView={onView} />)}
        </div>
      ) : null}
      {files.map((file, index) => <FileChip key={index} name={file.name} note={sizeOf(file.bytes)} />)}
      {text ? <span className="kin-caption">{text}</span> : null}
    </>
  )
}

/** A picture in a bubble, opened large on a click. One DSH keeps is read through her session into an object URL, let go when the bubble leaves. */
function Picture({ url, stored, width, height, alone, session, onView }: Shot & {
  alone: boolean; session: Pick<ISession, 'readAttachment'> | undefined; onView: (picture: Viewing) => void
}) {
  // undefined while its bytes are on their way, null when they could not be read.
  const [src, setSrc] = useState<string | null | undefined>(url)
  useEffect(() => {
    if (url || !stored || !session) return
    let made: string | undefined
    let here = true
    void session.readAttachment(stored).then((result) => {
      if (!here) return
      if (!result.ok) return setSrc(null)
      made = URL.createObjectURL(new Blob([result.value.data as Uint8Array<ArrayBuffer>], { type: stored.mediaType }))
      setSrc(made)
    })
    return () => {
      here = false
      if (made) URL.revokeObjectURL(made)
    }
  }, [])
  // A lone picture keeps its shape, with its room held before the bytes arrive; several sit as even tiles.
  const room = alone && width && height
    ? { width: Math.round(width * Math.min(1, 260 / width, 300 / height)), aspectRatio: `${width} / ${height}` }
    : undefined
  if (!src) return <i className="kin-picture" style={room}>{src === null ? '图片不见了' : null}</i>
  const alt = stored?.name ?? '图片'
  return (
    <button type="button" className="kin-picture-open" onClick={() => onView({ src, alt })} aria-label={`看大图：${alt}`}>
      <img className="kin-picture" style={room} src={src} alt={alt} />
    </button>
  )
}

/** A file as a small card: what it is called, and how big it is or where it is on its way to. */
function FileChip({ name, note }: { name: string; note: string }) {
  return <span className="kin-file"><i><Sheet size={16} /></i><span><b>{name}</b><small>{note}</small></span></span>
}

/** Her chat's own icons, drawn like the rest (icons.tsx): one stroke weight, round ends, a 24 grid. */
const glyph = (path: string) => function Glyph({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" className="kin-icon" fill="none" stroke="currentColor"
      strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d={path} /></svg>
  )
}
const Clip = glyph('M19.5 11.6l-7.3 7.3a4.9 4.9 0 0 1-6.9-6.9l7.6-7.6a3.3 3.3 0 0 1 4.6 4.6l-7.5 7.5a1.6 1.6 0 0 1-2.3-2.3l6.9-6.9')
const Sheet = glyph('M13.5 3.5H8a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8zM13.5 3.5V8H18')
const Microphone = glyph('M12 3.5a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0v-5a3 3 0 0 1 3-3zM18.5 11a6.5 6.5 0 0 1-13 0M12 17.5v3')

/** A picture's bytes as base64, read the way DSH's own composer reads them. */
function base64Of(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const url = reader.result as string
      resolve(url.slice(url.indexOf(',') + 1))
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

/** A byte count the way a person reads it. */
function sizeOf(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 ** 2).toFixed(1).replace(/\.0$/, '')} MB`
}

/** The event window as bubbles: what a person said, what she said, and what she did in between. */
function assemble(entries: readonly SessionEventLikeEntry[], pending: readonly PendingSubmission[]): Item[] {
  const items: Item[] = []
  const streaming = new Map<string, Extract<Item, { kind: 'kin' }>>()
  const settled = new Set<string>()
  // DSH keeps a reply's live rows until its step ends (a tool may still be running); once the reply of that
  // turn and step is committed, its bubble is the one shown, as in DSH's own chat.
  const committed = new Set(entries.flatMap(({ event }) => (event.type === 'assistant/message' ? [`${event.data.turn}:${event.data.step}`] : [])))
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
      const { attemptId, turn, step, chunk } = entry.event.data
      if (chunk.type !== 'text-delta' || committed.has(`${turn}:${step}`)) continue
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
      const source = event.data.source as { kind: string; rpcId?: string; channel?: string; text?: string; media?: { kind: 'image' | 'voice'; file: string } }
      // What she sent on her own, told to this conversation, and her stickers and voice messages here: hers.
      if (source.kind === 'kin-said' && source.text) add({ kind: 'kin', key: 'e' + event.seq, text: source.text, at, media: source.media })
      if (source.kind !== 'user' && source.kind !== 'kin-channel') continue
      const content = event.data.content
      const text = textOf(content)
      const pictures = content.flatMap((block) => (block.type === 'image' ? [{ stored: block.attachment, width: block.attachment.width, height: block.attachment.height }] : []))
      const files = content.flatMap((block) => (block.type === 'file' ? [block.attachment] : []))
      if (!text && !pictures.length && !files.length) continue
      // A message sent from here keeps the key of its echo, so its bubble (and a picture's preview) stays put as it settles.
      if (source.rpcId) settled.add(source.rpcId)
      add({
        kind: 'me', key: source.rpcId ? 'r' + source.rpcId : 'e' + event.seq, text, pictures, files, at,
        via: source.channel ? CHANNELS[source.channel.split(':')[0]!] : undefined,
      })
    } else if (event.type === 'assistant/message') {
      event.data.message.content.forEach((block, index) => {
        const key = `e${event.seq}:${index}`
        if (block.type === 'text' && answerText(block.text).trim()) add({ kind: 'kin', key, text: answerText(block.text).trim(), at })
        else if (block.type === 'tool-call') add({ kind: 'act', key, text: actOf(block.name, block.arguments), at })
      })
    }
  }
  for (const submission of pending) {
    // DSH lets an echo go a frame after its message lands; the message is already above.
    if (settled.has(submission.requestId)) continue
    add({
      kind: 'me', key: 'r' + submission.requestId, text: submission.text, at: submission.time, pending: true,
      pictures: submission.attachments.flatMap((it) => (it.type === 'image' ? [{ url: it.value.previewUrl, width: it.value.width, height: it.value.height }] : [])),
      files: submission.attachments.flatMap((it) => (it.type === 'file' ? [it.value] : [])),
    })
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
    case 'kin_delegate': return (args.team ? '交给一队帮手' : '交给帮手') + said('title', 'task')
    case 'kin_work': return '看了看手头的活'
    case 'kin_work_tell': return '给帮手捎了句话' + said('message')
    case 'kin_work_stop': return '叫停了一个帮手'
    case 'kin_work_answer': return args.allow ? '替你答应了帮手要做的事' : '替你回绝了帮手要做的事'
    case 'interrupt_agent': return '叫停了一个帮手'
    case 'web_search': return '去查了查' + said('query')
    case 'web_fetch': return '翻了翻网页' + said('url')
    case 'kin_recall': return '回想了一下' + said('about')
    case 'kin_calendar': return '翻了翻日程'
    case 'kin_calendar_mark': return args.remove ? '从日程上去掉了一段日子' : args.text ? '在日程上记了一段日子' + said('text') : '看了看日程上记着的日子'
    case 'kin_routine': return args.action === 'add' ? '给自己定了件常做的事' + said('what') : args.action === 'remove' ? '去掉了一件常做的事' : '看了看给自己定的事'
    case 'todo_write': return '记了个小清单'
    case 'schedule_create': return '定了个提醒' + said('title', 'prompt', 'description')
    case 'schedule_update': return '改了个提醒'
    case 'schedule_delete': return '取消了一个提醒'
    case 'schedule_list': return '看了看提醒'
    case 'kin_tell': return '帮忙带了句话' + said('to')
    case 'kin_quiet': return args.minutes === 0 ? '记下了：不用等了' : '记下了：这一阵先不打扰' + said('until')
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
