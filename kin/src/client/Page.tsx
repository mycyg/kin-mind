/**
 * KIN's page in DSH: glass over her sky. Her chat stays on the left; on the right, what she is like
 * right now, her little world, or her settings, picked from a floating dock. On a narrow pane the
 * chat is one more place in the dock.
 */
import { useEffect, useRef, useState, type CSSProperties } from 'react'
import type { Context } from '@deepseek-ai/cordis'
import { api, useApi, type Home } from './api.js'
import { createChat } from './Chat.js'
import { glide, useNarrow, useSheen, useWindowActive } from './glass.js'
import { Bento, Bubble, Pulse, Sliders } from './icons.js'
import { Now } from './Now.js'
import { Orb, warmthOf } from './Orb.js'
import { Settings } from './Settings.js'
import { Sky } from './Sky.js'
import { World, type Place } from './World.js'

type Section = 'chat' | 'now' | 'world' | 'settings'

const SECTIONS: { id: Section; label: string; icon: typeof Bubble }[] = [
  { id: 'chat', label: '聊天', icon: Bubble },
  { id: 'now', label: '此刻', icon: Pulse },
  { id: 'world', label: '小世界', icon: Bento },
  { id: 'settings', label: '设置', icon: Sliders },
]

export function createPage(ctx: Context) {
  const Chat = createChat(ctx)

  return function Page() {
    const home = useApi<Home>('api/kin/home', 30_000)
    const root = useRef<HTMLDivElement>(null)
    const narrow = useNarrow(root, 860)
    const [section, setSection] = useState<Section>('chat')
    const [place, setPlace] = useState<Place>()
    const [chatId, setChatId] = useState<string>()
    useSheen(root)
    const active = useWindowActive()

    // Her desktop conversation exists from the first visit on.
    useEffect(() => {
      if (!home.data || chatId) return
      if (home.data.chat) setChatId(home.data.chat.sessionId)
      else void api<{ sessionId: string }>('api/kin/chat', {}).then((chat) => setChatId(chat.sessionId))
    }, [home.data, chatId])

    const warmth = warmthOf(home.data?.state.dimensions)
    const feeling = home.data?.overview.affect_layers?.feeling ?? home.data?.state.affect_layers?.feeling?.text
    const status = home.error ? '她的心智还没醒来：' + home.error : feeling ? `此刻：${feeling}` : '正在醒来…'
    // Side by side, the chat is always there and the dock picks what sits beside it.
    const shown: Section = !narrow && section === 'chat' ? 'now' : section
    const open = (next: Section) => glide(() => { setSection(next); setPlace(undefined) })
    const dock = <Dock sections={narrow ? SECTIONS : SECTIONS.slice(1)} current={shown} onPick={open} />

    return (
      <div className="kin" ref={root} data-section={shown} data-inactive={active ? undefined : ''} data-narrow={narrow ? '' : undefined}>
        <Sky warmth={warmth} />
        <div className="kin-stage">
          <section className="kin-chat-col kin-glass" aria-label="和 KIN 聊天">
            <header className="kin-chat-head">
              <Orb size={38} warmth={warmth} />
              <div className="kin-who">
                <strong>KIN</strong>
                <span>{status}</span>
              </div>
            </header>
            <Chat sessionId={chatId} warmth={warmth} />
          </section>
          <main className="kin-right">
            {shown === 'settings' ? <Settings />
              : shown === 'world' ? <World home={home.data} place={place} go={(next) => glide(() => setPlace(next))} openSettings={() => open('settings')} />
              : <Now home={home.data} error={home.error} />}
            {narrow ? null : dock}
          </main>
        </div>
        {narrow ? dock : null}
      </div>
    )
  }
}

/** The floating pill: a lens of thicker glass slides to the place you pick. */
function Dock({ sections, current, onPick }: { sections: typeof SECTIONS; current: Section; onPick: (section: Section) => void }) {
  const index = Math.max(0, sections.findIndex((item) => item.id === current))
  return (
    <nav className="kin-dock kin-glass" role="tablist" aria-label="KIN" style={{ '--kin-n': sections.length, '--kin-i': index } as CSSProperties}>
      <span className="kin-dock-lens" aria-hidden="true" />
      {sections.map(({ id, label, icon: Icon }) => (
        <button key={id} type="button" role="tab" aria-selected={id === current} className="kin-dock-item" onClick={() => onPick(id)}>
          <Icon size={20} />
          <span>{label}</span>
        </button>
      ))}
    </nav>
  )
}
