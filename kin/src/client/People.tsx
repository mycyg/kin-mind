/**
 * Settings: who she talks with and the rules for her reaching out. The owner says who is who (the owner first),
 * their account on each platform, whom she may reach on her own, and which groups she may chime into;
 * someone who wrote to her and is not placed yet can be added from here.
 */
import { useEffect, useState } from 'react'
import { api, useApi } from './api.js'
import { Close } from './icons.js'
import { ago } from './time.js'

export const OWNER = 'person:person_a'
export const PLATFORMS: Record<string, string> = { wechat: '微信', feishu: '飞书' }

interface Person { id?: string; name: string; proactive: boolean; accounts: Record<string, string> }
interface Group { id?: string; name: string; chime: boolean; chats: Record<string, string> }
interface Stranger { transport: string; chat: string; kind: 'direct' | 'group'; sender: string; senderName?: string; at: string }
interface Directory {
  people: Person[]
  groups: Group[]
  strangers: Stranger[]
}
export interface Rules {
  enabled: boolean; quietStart: number; quietEnd: number; maxPerDay: number; minGapMinutes: number; waitForReply: boolean; timeZone: string
}

export function People() {
  const { data, error, reload } = useApi<Directory>('api/kin/people')
  const [draft, setDraft] = useState<Pick<Directory, 'people' | 'groups'>>()
  const [state, setState] = useState<string>()
  useEffect(() => { if (data) setDraft({ people: data.people, groups: data.groups }) }, [data])
  if (error) return <section className="kin-card"><h3 className="kin-card-title">她认识的人</h3><p className="kin-quiet">没读出来：{error}</p></section>
  if (!data || !draft) return null

  const platforms = Object.keys(PLATFORMS)
  const person = (index: number, change: Partial<Person>) => { setState(undefined); setDraft({ ...draft, people: draft.people.map((p, i) => (i === index ? { ...p, ...change } : p)) }) }
  const group = (index: number, change: Partial<Group>) => { setState(undefined); setDraft({ ...draft, groups: draft.groups.map((g, i) => (i === index ? { ...g, ...change } : g)) }) }
  const adopt = (stranger: Stranger) => setDraft(stranger.kind === 'group'
    ? { ...draft, groups: [...draft.groups, { name: '', chime: false, chats: { [stranger.transport]: stranger.chat } }] }
    : { ...draft, people: [...draft.people, { name: stranger.senderName ?? '', proactive: false, accounts: { [stranger.transport]: stranger.sender } }] })
  const save = () => api('api/kin/people', draft).then(() => { setState('saved'); reload() }).catch((reason: Error) => setState(reason.message))

  return (
    <section className="kin-card kin-people">
      <h3 className="kin-card-title">她认识的人</h3>
      <p className="kin-hint">填上每个人在各个平台上的账号，她就认得是谁在说话。群里只有 @ 她时她才回话，除非你让她可以插话。微信群暂时认不出 @ 她，只能靠插话。</p>
      <ul className="kin-list">
        {draft.people.map((entry, index) => (
          <li key={entry.id ?? 'new' + index} className="kin-person">
            <div className="kin-person-head">
              <input className="kin-field" value={entry.name} placeholder={entry.id === OWNER ? '你的名字（她这样叫你）' : '名字'}
                aria-label="名字" onChange={(event) => person(index, { name: event.target.value })} />
              {entry.id === OWNER ? <span className="kin-tag">你</span> : (
                <button type="button" className="kin-icon-btn" aria-label="不再认识这个人"
                  onClick={() => setDraft({ ...draft, people: draft.people.filter((_, i) => i !== index) })}><Close size={14} /></button>
              )}
            </div>
            <div className="kin-accounts">
              {platforms.map((platform) => (
                <label key={platform}><span>{PLATFORMS[platform] ?? platform}</span>
                  <input className="kin-field" value={entry.accounts[platform] ?? ''} placeholder="账号 id" spellCheck={false}
                    onChange={(event) => person(index, { accounts: { ...entry.accounts, [platform]: event.target.value } })} /></label>
              ))}
            </div>
            <Toggle label="她可以主动找" checked={entry.proactive} onChange={(proactive) => person(index, { proactive })} />
          </li>
        ))}
        {draft.groups.map((entry, index) => (
          <li key={entry.id ?? 'group' + index} className="kin-person">
            <div className="kin-person-head">
              <input className="kin-field" value={entry.name} placeholder="群名" aria-label="群名" onChange={(event) => group(index, { name: event.target.value })} />
              <span className="kin-tag">群</span>
              <button type="button" className="kin-icon-btn" aria-label="不再认识这个群"
                onClick={() => setDraft({ ...draft, groups: draft.groups.filter((_, i) => i !== index) })}><Close size={14} /></button>
            </div>
            <div className="kin-accounts">
              {platforms.map((platform) => (
                <label key={platform}><span>{PLATFORMS[platform] ?? platform}</span>
                  <input className="kin-field" value={entry.chats[platform] ?? ''} placeholder="群 id" spellCheck={false}
                    onChange={(event) => group(index, { chats: { ...entry.chats, [platform]: event.target.value } })} /></label>
              ))}
            </div>
            <Toggle label="没人 @ 她时也可以插话" checked={entry.chime} onChange={(chime) => group(index, { chime })} />
          </li>
        ))}
      </ul>
      {data.strangers.length ? (
        <div className="kin-strangers">
          <p className="kin-hint">有人找过她，她还不认识：</p>
          {data.strangers.map((stranger) => (
            <div key={stranger.transport + stranger.chat + stranger.sender} className="kin-stranger">
              <span>{PLATFORMS[stranger.transport] ?? stranger.transport}{stranger.kind === 'group' ? '群' : ''} · {stranger.senderName ?? stranger.sender}
                <span className="kin-dim"> {stranger.kind === 'group' ? stranger.chat : stranger.sender} · {ago(stranger.at)}</span></span>
              <button type="button" className="kin-link" onClick={() => adopt(stranger)}>{stranger.kind === 'group' ? '认识这个群' : '认识这个人'}</button>
            </div>
          ))}
        </div>
      ) : null}
      <div className="kin-row-end">
        <button type="button" className="kin-link" onClick={() => setDraft({ ...draft, people: [...draft.people, { name: '', proactive: false, accounts: {} }] })}>加一个人</button>
        <button type="button" className="kin-link" onClick={() => setDraft({ ...draft, groups: [...draft.groups, { name: '', chime: false, chats: {} }] })}>加一个群</button>
        <span className="kin-dim">{state === 'saved' ? '记住啦' : state ?? ''}</span>
        <button type="button" className="kin-btn" data-primary="" onClick={save}>保存</button>
      </div>
    </section>
  )
}

/** When she may reach out on her own: the switch, quiet hours, how often, and whether to wait for a reply. */
export function ContactRules() {
  const { data, reload } = useApi<{ rules: Rules }>('api/kin/contact')
  const [rules, setRules] = useState<Rules>()
  useEffect(() => { if (data) setRules(data.rules) }, [data])
  if (!rules) return null
  const change = (next: Partial<Rules>) => {
    setRules({ ...rules, ...next })
    void api('api/kin/contact', next).then(reload, reload)
  }
  const hours = Array.from({ length: 24 }, (_, hour) => hour)
  return (
    <section className="kin-card">
      <h3 className="kin-card-title">主动联系</h3>
      <Toggle label="她可以主动找人" hint="想说什么、什么时候说由她自己定；下面这些是她要守的规矩" checked={rules.enabled} onChange={(enabled) => change({ enabled })} />
      <div className="kin-switch-row">
        <div><p>安静时段</p><span className="kin-dim">这段时间她不主动出声；到点的提醒和转告也等安静时段过了再发</span></div>
        <span className="kin-hours">
          <select className="kin-effort" value={rules.quietStart} aria-label="安静时段从" onChange={(event) => change({ quietStart: Number(event.target.value) })}>
            {hours.map((hour) => <option key={hour} value={hour}>{hour}:00</option>)}
          </select>
          到
          <select className="kin-effort" value={rules.quietEnd} aria-label="安静时段到" onChange={(event) => change({ quietEnd: Number(event.target.value) })}>
            {hours.map((hour) => <option key={hour} value={hour}>{hour}:00</option>)}
          </select>
        </span>
      </div>
      <div className="kin-switch-row">
        <div><p>每个人一天最多</p><span className="kin-dim">两次之间至少隔多久</span></div>
        <span className="kin-hours">
          <select className="kin-effort" value={rules.maxPerDay} aria-label="一天最多几次" onChange={(event) => change({ maxPerDay: Number(event.target.value) })}>
            {[1, 2, 3, 4, 5, 6, 8, 10].map((n) => <option key={n} value={n}>{n} 次</option>)}
          </select>
          <select className="kin-effort" value={rules.minGapMinutes} aria-label="至少隔多久" onChange={(event) => change({ minGapMinutes: Number(event.target.value) })}>
            {[0, 30, 60, 120, 180, 360].map((n) => <option key={n} value={n}>{n ? (n < 60 ? `${n} 分钟` : `${n / 60} 小时`) : '不限'}</option>)}
          </select>
        </span>
      </div>
      <Toggle label="等回了再找" hint="上一条没回之前，她不再主动找同一个人" checked={rules.waitForReply} onChange={(waitForReply) => change({ waitForReply })} />
    </section>
  )
}

function Toggle({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <div className="kin-switch-row">
      <div><p>{label}</p>{hint ? <span className="kin-dim">{hint}</span> : null}</div>
      <button type="button" role="switch" aria-checked={checked} aria-label={label} className="kin-switch" onClick={() => onChange(!checked)} />
    </div>
  )
}
