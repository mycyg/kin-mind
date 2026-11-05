/**
 * Her own ways (talk/ on the host): whether answering, and when, is hers to decide; whether she tires.
 */
import { useEffect, useState } from 'react'
import { api, useApi } from './api.js'

interface Talk {
  /** Replies she is holding to give later. */
  held: number
  replyChoice: 'always' | 'autonomous'
  energy: { enabled: boolean; value?: number; tier?: 'low' | 'normal' | 'high' }
}

export function TalkSettings() {
  const { data, reload } = useApi<Talk>('api/kin/talk')
  if (!data) return null
  return (
    <>
      <OwnWays data={data} onSaved={reload} />
    </>
  )
}

function OwnWays({ data, onSaved }: { data: Talk; onSaved: () => void }) {
  const [state, setState] = useState({ reply: data.replyChoice === 'autonomous', energy: data.energy.enabled })
  useEffect(() => setState({ reply: data.replyChoice === 'autonomous', energy: data.energy.enabled }), [data])
  const flip = (key: 'reply' | 'energy') => {
    const value = !state[key]
    setState({ ...state, [key]: value })
    api('api/kin/talk/choices', key === 'reply' ? { replyChoice: value ? 'autonomous' : 'always' } : { energy: value })
      .then(onSaved, () => setState(state))
  }
  const energy = data.energy.enabled && typeof data.energy.value === 'number' ? data.energy.value : undefined
  return (
    <section className="kin-card">
      <h3 className="kin-card-title">她自己拿主意</h3>
      <div className="kin-switch-row">
        <div>
          <p>回不回、什么时候回，她自己定</p>
          <span className="kin-dim">
            闲聊时她可以先不回、晚点再回，或者等你下一句一起回
            {data.held ? ` · 现在有 ${data.held} 条她说晚点再回` : ''}
          </span>
        </div>
        <button type="button" role="switch" aria-checked={state.reply} aria-label="回不回、什么时候回，她自己定"
          className="kin-switch" onClick={() => flip('reply')} />
      </div>
      <div className="kin-switch-row">
        <div>
          <p>会累{energy !== undefined ? <span className="kin-energy"><b className="kin-numeral">{Math.round(energy)}</b> 精力</span> : null}</p>
          <span className="kin-dim">聊得多、做得多会累，歇一歇会回来；累的时候说话不太一样，也可能晚点再回你</span>
        </div>
        <button type="button" role="switch" aria-checked={state.energy} aria-label="会累" className="kin-switch" onClick={() => flip('energy')} />
      </div>
    </section>
  )
}
