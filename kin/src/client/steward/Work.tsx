/**
 * 手头的活: what she has handed to her helpers for the owner (src/steward/work.ts) — what, in which
 * folder, how far, and a way to stop it. Nothing shows while there is nothing to show.
 */
import { useState } from 'react'
import { api, useApi } from '../api.js'
import { ago } from '../time.js'

interface Work {
  id: string
  label: string
  folder: string
  state: 'running' | 'done' | 'stopped' | 'failed' | 'interrupted'
  started: string
  ended?: string
  result?: string
  progress?: string
}

const STATES: Record<Work['state'], string> = { running: '在做', done: '做完了', stopped: '停下了', failed: '没做成', interrupted: '断在半路' }

export function Work() {
  const { data, reload } = useApi<{ work: Work[] }>('api/kin/work', 10_000)
  const [stopping, setStopping] = useState<string>()
  const [error, setError] = useState<string>()
  const items = data?.work ?? []
  if (!items.length) return null
  const stop = (id: string) => {
    setStopping(id)
    api('api/kin/work/stop', { id })
      .then(() => { setError(undefined); void reload() })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setStopping(undefined))
  }
  return (
    <section className="kin-card">
      <h3 className="kin-card-title">手头的活</h3>
      <ul className="kin-list kin-list-scroll" tabIndex={0}>
        {items.map((item) => (
          <li key={item.id} className="kin-work" data-state={item.state}>
            <div className="kin-work-main">
              <p className="kin-work-what">
                <span className="kin-tag">{STATES[item.state]}</span>{item.label}
              </p>
              <p className="kin-work-where" title={item.folder}>
                {folderName(item.folder)} · {item.state === 'running' ? `${ago(item.started)}开始` : ago(item.ended)}
              </p>
              {item.state === 'running' ? (item.progress ? <p className="kin-work-note">{item.progress}</p> : null)
                : item.result ? <p className="kin-work-note">{item.result}</p> : null}
            </div>
            {item.state === 'running' ? (
              <button type="button" className="kin-btn" disabled={stopping === item.id} onClick={() => stop(item.id)}>
                {stopping === item.id ? '在叫停…' : '停下'}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      {error ? <p className="kin-error">{error}</p> : null}
    </section>
  )
}

/** A folder by its last two names, the whole path on hover. */
function folderName(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts.length ? (parts.length > 2 ? '…/' + parts.slice(-2).join('/') : '/' + parts.join('/')) : path
}
