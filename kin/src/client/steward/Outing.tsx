/**
 * Her own outings (src/steward/explore.ts, create.ts): on 出门, the exploration she is out on and what
 * came of the last one; on 心愿, the thing she is making. Either can be stopped from here.
 */
import { useState } from 'react'
import { api, useApi, type Beat } from '../api.js'
import { ago } from '../time.js'

type Kind = 'exploration' | 'creation'

interface Outing {
  current: { title: string; since: string } | null
  last: { title: string; state: string; at: string; summary?: string; folder?: string } | null
  beat: Beat | null
}

const WORDS: Record<Kind, { out: string; since: string; stop: string; done: string; undone: string }> = {
  exploration: { out: '她出门了', since: '出发', stop: '叫她回来', done: '上次出门带回来', undone: '上次出门没带回结果' },
  creation: { out: '她在做', since: '动手', stop: '先停下', done: '上次做好了', undone: '上次没做完' },
}

export function Outing({ kind }: { kind: Kind }) {
  const { data, reload } = useApi<Record<Kind, Outing>>('api/kin/outing', 15_000)
  // The outing a stop was asked for, so a later one gets its own button.
  const [asked, setAsked] = useState<string>()
  const outing = data?.[kind]
  const words = WORDS[kind]
  if (!outing?.current && !outing?.last) return null
  if (outing.current) {
    const { since } = outing.current
    const stop = () => {
      setAsked(since)
      void api('api/kin/outing/stop', { kind }).then(reload).catch(() => setAsked(undefined))
    }
    return (
      <section className="kin-card kin-outing" data-kind={kind}>
        <i className="kin-outing-dot" aria-hidden="true" />
        <div className="kin-outing-main">
          <p className="kin-outing-what">{words.out}：{outing.current.title}</p>
          <p className="kin-dim">{ago(outing.current.since)}{words.since}</p>
        </div>
        <button type="button" className="kin-btn" disabled={asked === since} onClick={stop}>{asked === since ? '跟她说了' : words.stop}</button>
      </section>
    )
  }
  const last = outing.last!
  const done = last.state === 'complete' || last.state === 'completed'
  return (
    <p className="kin-footnote kin-outing-last">
      {done ? `${ago(last.at)}，${words.done}「${last.title}」` : `${ago(last.at)}，${words.undone}「${last.title}」`}
      {done && last.summary ? <><br />{last.summary}</> : null}
      {done && last.folder ? <><br /><span title={last.folder}>放在 {last.folder}</span></> : null}
    </p>
  )
}
