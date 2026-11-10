/**
 * 手头的活: what she has handed to her helpers for the owner (src/steward/work.ts) — what, in which
 * folder, how far, and a way to stop it; for work a team does, who is on the team and its task board.
 * Nothing shows while there is nothing to show.
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
  crew?: {
    members: { name: string; status: 'running' | 'inactive' | 'provisioning' | 'failed'; job?: string }[]
    tasks: { title: string; status: 'pending' | 'in_progress' | 'completed' | 'deleted'; owner?: string }[]
  }
}

const STATES: Record<Work['state'], string> = { running: '在做', done: '做完了', stopped: '停下了', failed: '没做成', interrupted: '断在半路' }
const MEMBERS = { running: '在做', inactive: '歇着', provisioning: '刚叫来', failed: '没叫来' }
const TASKS = { pending: '还没开始', in_progress: '在做', completed: '做完了', deleted: '' }

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
              {item.crew ? <Crew {...item.crew} /> : null}
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

/** A team at work: its members (a dot beats on the ones working) and the shared task board. */
function Crew({ members, tasks }: NonNullable<Work['crew']>) {
  return (
    <div className="kin-crew">
      <p className="kin-crew-members" aria-label="队里的帮手">
        {members.map((member) => (
          <span key={member.name} className="kin-crew-member" data-status={member.status}
            title={[member.job, MEMBERS[member.status]].filter(Boolean).join(' · ')}>
            <i aria-hidden="true" />{member.name === 'lead' ? '队长' : member.name}
          </span>
        ))}
      </p>
      {tasks.length ? (
        <ul className="kin-crew-tasks" aria-label="任务板">
          {tasks.map((task, index) => (
            <li key={index} data-status={task.status} title={TASKS[task.status]}>
              <span className="kin-crew-task">{task.title}</span>
              {task.owner ? <span className="kin-dim">{task.owner === 'lead' ? '队长' : task.owner}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

/** A folder by its last two names, the whole path on hover. */
function folderName(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts.length ? (parts.length > 2 ? '…/' + parts.slice(-2).join('/') : '/' + parts.join('/')) : path
}
