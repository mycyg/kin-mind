/** Where she can find you: WeChat (scan to log in), Feishu (the app's credentials) and her voice, on her settings page. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, invalidateApi, useApi } from './api.js'
import { qr } from './qr.js'

type State = 'connected' | 'connecting' | 'login-required' | 'stopped' | 'failed'
interface Status { state: State; reason?: string; qr?: string }
interface Data {
  wechat: { baseUrl: string; cdnBaseUrl: string; userId: string | null; status: Status } | null
  feishu: { appId: string; domain: string; status: Status } | null
  voice: { provider?: 'deepseek' | 'local'; voiceId?: string; python?: string; model?: string; ffmpeg?: string; voice?: string } | null
  hasDeepSeekKey?: boolean
}

const STATES: Record<State, string> = {
  connected: '连上了', connecting: '正在连', 'login-required': '等你扫码', stopped: '没开', failed: '出错了',
}

export function Channels() {
  // While something is on its way (a QR waiting for a scan, a connection coming up) the card looks more often.
  const [eager, setEager] = useState(false)
  const { data, error, reload } = useApi<Data>('api/kin/channels', eager ? 2000 : 15000)
  useEffect(() => {
    setEager([data?.wechat?.status, data?.feishu?.status].some((status) => status && status.state !== 'connected' && status.state !== 'stopped'))
  }, [data])
  return (
    <section className="kin-card kin-channels">
      <h3 className="kin-card-title">她在哪里找你</h3>
      <p className="kin-hint">在微信、飞书上，她和你在这里聊的是同一个她。账号只存在她的数据目录里，只有你能读。</p>
      {error ? <p className="kin-error">渠道没读出来：{error}</p> : !data ? <p className="kin-quiet">正在看渠道…</p> : (
        <>
          <WeChat data={data.wechat} onChange={reload} />
          <Feishu data={data.feishu} onChange={reload} />
          <Voice data={data.voice} hasKey={data.hasDeepSeekKey} onChange={() => { invalidateApi('api/kin/hear'); void reload() }} />
        </>
      )}
    </section>
  )
}

function WeChat({ data, onChange }: { data: Data['wechat']; onChange: () => void }) {
  const [editing, setEditing] = useState(false)
  const [problem, setProblem] = useState<string>()
  const act = (route: string, body: unknown = {}) => api(route, body).then(() => { setProblem(undefined); onChange() }, (reason: Error) => setProblem(reason.message))
  const status = data?.status
  return (
    <div className="kin-channel">
      <Head title="微信" status={status} remove={data ? () => act('api/kin/channels/remove', { id: 'wechat' }) : undefined} />
      {status?.qr ? <Code text={status.qr} /> : null}
      {status?.reason ? <p className="kin-channel-reason" data-state={status.state}>{status.reason}</p> : null}
      {data?.userId && status?.state === 'connected' ? <p className="kin-dim">已登录 · {data.userId}</p> : null}
      {data && !editing ? (
        <div className="kin-channel-actions">
          <button type="button" className="kin-btn" onClick={() => act('api/kin/channels/wechat/login')}>
            {status?.state === 'login-required' ? '再要一张二维码' : '重新扫码'}
          </button>
          <button type="button" className="kin-link" onClick={() => setEditing(true)}>改地址</button>
        </div>
      ) : (
        <Fields
          fields={[
            { key: 'baseUrl', label: '接口地址', placeholder: 'iLink 接口地址，https:// 开头' },
            { key: 'cdnBaseUrl', label: '媒体地址', placeholder: '微信媒体（CDN）地址，https:// 开头' },
          ]}
          initial={{ baseUrl: data?.baseUrl ?? '', cdnBaseUrl: data?.cdnBaseUrl ?? '' }}
          action={data ? '保存' : '连上微信'}
          submit={(values) => api('api/kin/channels/wechat', values).then(() => { setEditing(false); onChange() })}
        />
      )}
      {problem ? <p className="kin-error">{problem}</p> : null}
    </div>
  )
}

function Feishu({ data, onChange }: { data: Data['feishu']; onChange: () => void }) {
  const [editing, setEditing] = useState(false)
  return (
    <div className="kin-channel">
      <Head title="飞书" status={data?.status}
        remove={data ? () => api('api/kin/channels/remove', { id: 'feishu' }).then(onChange) : undefined} />
      {data?.status.reason ? <p className="kin-channel-reason" data-state={data.status.state}>{data.status.reason}</p> : null}
      {data && !editing ? (
        <div className="kin-channel-compact">
          <span className="kin-dim">App ID · {data.appId}</span>
          <button type="button" className="kin-link" onClick={() => setEditing(true)}>修改配置</button>
        </div>
      ) : (
        <>
          <Fields
            fields={[
              { key: 'appId', label: 'App ID', placeholder: '飞书自建应用的 App ID' },
              { key: 'appSecret', label: 'App Secret', placeholder: data ? '已存好；不换就留空' : '应用的 App Secret', secret: true },
              { key: 'domain', label: '地址', placeholder: '飞书开放平台地址，https:// 开头' },
            ]}
            initial={{ appId: data?.appId ?? '', appSecret: '', domain: data?.domain ?? '' }}
            action={data ? '保存并重连' : '连上飞书'}
            submit={(values) => api('api/kin/channels/feishu', values).then(() => { setEditing(false); onChange() })}
          />
          {data ? <button type="button" className="kin-link" onClick={() => setEditing(false)}>收起</button> : null}
        </>
      )}
    </div>
  )
}

const VOICES: { id: 'stella' | 'tide'; label: string }[] = [
  { id: 'stella', label: '海星 · 女声' },
  { id: 'tide', label: '暗潮 · 男声' },
]

function Voice({ data, hasKey, onChange }: { data: Data['voice']; hasKey?: boolean; onChange: () => void }) {
  const [localOpen, setLocalOpen] = useState(data?.provider === 'local')
  const [busy, setBusy] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [note, setNote] = useState<string>()
  const audioRef = useRef<HTMLAudioElement>()
  const urlRef = useRef<string>()
  useEffect(() => () => {
    audioRef.current?.pause()
    if (urlRef.current) URL.revokeObjectURL(urlRef.current)
  }, [])

  const isLocal = data?.provider === 'local'
  const currentVoice = data?.voiceId || 'stella'

  const selectDeepSeek = (voiceId: string) => {
    setBusy(true)
    setNote(undefined)
    void api('api/kin/channels/voice', { provider: 'deepseek', voiceId })
      .then(() => onChange(), (err: Error) => setNote(err.message))
      .finally(() => setBusy(false))
  }

  const preview = async () => {
    if (previewing) return
    setPreviewing(true)
    setNote(undefined)
    try {
      const response = await fetch('api/kin/speak', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text: '喂，在听吗？我在这里呀。', voiceId: currentVoice }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.error ?? `${response.status}`)
      }
      const blob = await response.blob()
      audioRef.current?.pause()
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
      const url = URL.createObjectURL(blob)
      urlRef.current = url
      const player = new Audio(url)
      audioRef.current = player
      player.onended = () => {
        if (urlRef.current === url) { URL.revokeObjectURL(url); urlRef.current = undefined }
      }
      await player.play()
    } catch (err) {
      setNote((err as Error).message)
    } finally {
      setPreviewing(false)
    }
  }

  return (
    <div className="kin-channel">
      <div className="kin-channel-head">
        <span className="kin-lane-title">她的声音与听写</span>
        <span className="kin-pill" data-state={isLocal || hasKey ? 'connected' : 'stopped'}>
          {isLocal ? '本机语音' : hasKey ? 'DeepSeek 云端' : '未就绪'}
        </span>
      </div>
      <p className="kin-hint">
        {isLocal ? '正在使用本机语音模型；也可以随时切回更快的 DeepSeek 云端合成与识别。'
          : hasKey ? '使用 DeepSeek 语音合成与识别，发语音和实时语音陪伴都会快很多。'
          : '需要在 DSH 凭据里配置 DEEPSEEK_API_KEY，才能启用快速云端语音与听写。'}
      </p>
      <div className="kin-voice-voices">
        {VOICES.map((voice) => (
          <button key={voice.id} type="button" className="kin-chip" disabled={busy}
            data-active={!isLocal && currentVoice === voice.id ? '' : undefined}
            onClick={() => selectDeepSeek(voice.id)}>
            {voice.label}
          </button>
        ))}
        {hasKey ? (
          <button type="button" className="kin-btn kin-voice-preview" disabled={previewing} onClick={() => void preview()}>
            {previewing ? '合成中…' : '试听一下'}
          </button>
        ) : null}
        {isLocal && hasKey ? (
          <button type="button" className="kin-link" disabled={busy} onClick={() => selectDeepSeek(currentVoice)}>
            切回 DeepSeek 云端
          </button>
        ) : null}
      </div>
      {note ? <p className="kin-error">{note}</p> : null}
      <div className="kin-channel-compact">
        <button type="button" className="kin-link" onClick={() => setLocalOpen((v) => !v)}>
          {localOpen ? '收起本机语音配置' : '高级：配置本机 MLX 语音模型'}
        </button>
      </div>
      {localOpen ? (
        <Fields
          fields={[
            { key: 'python', label: 'Python', placeholder: '装了 mlx-audio 的 Python 路径' },
            { key: 'model', label: '模型', placeholder: '语音模型所在的文件夹' },
            { key: 'ffmpeg', label: 'ffmpeg', placeholder: 'ffmpeg 的路径' },
            { key: 'voice', label: '音色', placeholder: '不填就是 serena' },
          ]}
          initial={{ python: data?.python ?? '', model: data?.model ?? '', ffmpeg: data?.ffmpeg ?? '', voice: data?.voice ?? '' }}
          action="改用本机语音"
          submit={(values) => api('api/kin/channels/voice', { ...values, provider: 'local' }).then(onChange)}
        />
      ) : null}
    </div>
  )
}

function Head({ title, status, remove }: { title: string; status?: Status; remove?: () => void }) {
  return (
    <div className="kin-channel-head">
      <span className="kin-lane-title">{title}</span>
      <span className="kin-pill" data-state={status?.state ?? 'stopped'}>{status ? STATES[status.state] : '还没设'}</span>
      {remove ? <button type="button" className="kin-link kin-channel-remove" onClick={remove}>断开</button> : null}
    </div>
  )
}

/** The login link as a QR code: always dark on white, whatever the theme, so a phone can read it. */
function Code({ text }: { text: string }) {
  // A quiet zone of four modules all round.
  const { path, size } = useMemo(() => {
    const modules = qr(text)
    return { size: modules.length + 8, path: modules.flatMap((row, y) => row.map((dark, x) => (dark ? `M${x + 4} ${y + 4}h1v1h-1z` : ''))).join('') }
  }, [text])
  return (
    <div className="kin-qr" role="img" aria-label="微信登录二维码">
      <svg viewBox={`0 0 ${size} ${size}`} shapeRendering="crispEdges"><rect width={size} height={size} fill="#fff" /><path d={path} fill="#1d1420" /></svg>
    </div>
  )
}

function Fields({ fields, initial, action, submit }: {
  fields: { key: string; label: string; placeholder: string; secret?: boolean }[]
  initial: Record<string, string>
  action: string
  submit: (values: Record<string, string>) => Promise<unknown>
}) {
  const [values, setValues] = useState(initial)
  const [state, setState] = useState<'idle' | 'busy' | 'saved' | string>('idle')
  useEffect(() => setValues(initial), [JSON.stringify(initial)])
  return (
    <form className="kin-channel-form" onSubmit={(event) => {
      event.preventDefault()
      setState('busy')
      submit(values).then(() => setState('saved'), (reason: Error) => setState(reason.message))
    }}>
      {fields.map((field) => (
        <label key={field.key} className="kin-inline-form">
          <span>{field.label}</span>
          <input type={field.secret ? 'password' : 'text'} value={values[field.key] ?? ''} placeholder={field.placeholder}
            autoComplete="off" spellCheck={false} onChange={(event) => { setValues({ ...values, [field.key]: event.target.value }); setState('idle') }} />
        </label>
      ))}
      <div className="kin-row-end">
        <span className={state === 'saved' || state === 'idle' || state === 'busy' ? 'kin-dim' : 'kin-error'}>
          {state === 'saved' ? '存好了' : state === 'busy' ? '正在连…' : state === 'idle' ? '' : state}
        </span>
        <button type="submit" className="kin-btn" data-primary="" disabled={state === 'busy'}>{action}</button>
      </div>
    </form>
  )
}
