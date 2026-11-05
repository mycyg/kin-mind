/** 帮手的权限 (src/steward/permission.ts): how far her helpers may go, in the permission modes this DSH has. */
import { useState } from 'react'
import { api, useApi } from '../api.js'

const CHOICES: Record<string, { title: string; hint: string }> = {
  'workspace-write': { title: '问我', hint: '帮手只在交给它的文件夹里动手，别的先在 DSH 里问你；对话里的小帮手只能在她自己那儿写东西' },
  auto: { title: '自动审核', hint: '每一步先让模型审一遍：寻常的直接做，拿不准的停下来问你，小帮手则直接不做。每一步多花一次模型调用' },
  'danger-full-access': { title: '全权', hint: '什么都不问，哪儿都能动，交给她的活不会卡在等你点头上（默认）。她自己出门、做东西时的帮手不跟这一档，只在她自己那儿动手' },
}

export function Permission() {
  const { data, reload } = useApi<{ offered: string[]; current: string | null }>('api/kin/permission')
  const [picking, setPicking] = useState<string>()
  const [error, setError] = useState<string>()
  if (!data?.current) return null
  const pick = (preset: string) => {
    setPicking(preset)
    api('api/kin/permission', { preset })
      .then(() => { setError(undefined); return reload() })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setPicking(undefined))
  }
  const chosen = picking ?? data.current
  return (
    <section className="kin-card">
      <h3 className="kin-card-title">帮手的权限</h3>
      <p className="kin-hint">交给帮手的活、对话里叫的小帮手、她自己出门和创作，都按这一档。改了从下一件活算起，正在做的不变。</p>
      <div role="radiogroup" aria-label="帮手的权限">
        {data.offered.map((preset) => (
          <button type="button" role="radio" key={preset} aria-checked={chosen === preset} aria-label={CHOICES[preset].title}
            className="kin-switch-row kin-choice" onClick={() => pick(preset)}>
            <div><p>{CHOICES[preset].title}</p><span className="kin-dim">{CHOICES[preset].hint}</span></div>
            <i className="kin-choice-dot" aria-hidden="true" />
          </button>
        ))}
      </div>
      {data.offered.includes('auto') ? null : <p className="kin-hint">想要自动审核，先在 DSH 的插件里打开 Auto review。</p>}
      {error ? <p className="kin-error">{error}</p> : null}
    </section>
  )
}
