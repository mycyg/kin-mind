/**
 * KIN's mind, Core (`kin_mind.host`), in two kinds of process:
 * - one resident `serve` process for the short reads and writes, one JSON line per request;
 * - a process of its own for each action that may call a model (a review, the day's review, a
 *   memory job), so a review that thinks for ten minutes never holds up a page read or a recall.
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { createInterface } from 'node:readline'

export interface MindOptions {
  python: string
  coreSource: string
  dataDir: string
  /** Directory holding tiktoken's cl100k_base cache; without it Core counts bytes, not tokens. */
  tokenizerCache?: string
  /** Dev pace (index.ts `pace`): the worker's restart and Core's foreground lease take this many times as long. */
  pace: number
  log: (message: string) => void
}

export class MindError extends Error {
  constructor(readonly action: string, readonly detail: { error?: string; kind?: string; code?: string }) {
    super(`mind ${action} failed: ${detail.code ?? detail.error ?? 'unknown'}`)
  }
}

interface Pending {
  action: string
  resolve: (value: any) => void
  reject: (error: Error) => void
  timer: NodeJS.Timeout
}

/** How long an action that may call a model has, as the old host allowed (legacy/host/mind-host.mjs). */
export function timeoutOf(action: string): number {
  if (action === 'explore') return 1_500_000
  if (action === 'prepare-memory') return 390_000
  if (action === 'review' || action === 'review-enrichment' || action === 'daily') return 660_000
  return 180_000
}

export class Mind {
  private child?: ChildProcess
  private seq = 0
  private readonly pending = new Map<number, Pending>()
  private readonly workers = new Set<ChildProcess>()
  private readonly configFile: string
  private closed = false
  private worker?: ChildProcess
  private resting = false
  /** The memory worker, for her health check: its process while it runs, and how it ended lately. */
  readonly memoryWorker: { pid?: number; since?: string; ends: { at: string; how: string }[] } = { ends: [] }
  /** Core's store directory and the scope it keeps KIN's mind under. */
  readonly root: string
  readonly scope: object

  constructor(private readonly options: MindOptions) {
    this.configFile = join(options.dataDir, 'mind-config.json')
    ;({ root: this.root, scope: this.scope } = writeMindConfig(this.configFile, options.dataDir, options.pace))
  }

  /** The file that routes Core's own model calls to KIN's lanes (bridge.ts writes it), where Core looks for it (host.py, load_config). */
  get modelFile(): string {
    return join(this.options.dataDir, 'state', 'model-config.json')
  }

  /** A short action in the resident process. */
  call<T = any>(action: string, args: object = {}, timeoutMs = 20_000): Promise<T> {
    const child = this.child ?? this.start()
    const id = ++this.seq
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new MindError(action, { code: 'timeout' }))
        // The worker answers in order, so a stuck request blocks every later one: restart it.
        this.stop()
      }, timeoutMs)
      this.pending.set(id, { action, resolve, reject, timer })
      child.stdin!.write(JSON.stringify({ id, action, args }) + '\n')
    })
  }

  /** An action that may call a model, in a process of its own; it ends when the action does or its time is up. */
  run<T = any>(action: string, args: object = {}, timeoutMs = timeoutOf(action)): Promise<T> {
    const child = spawn(this.options.python, ['-m', 'kin_mind.host', '--config', this.configFile, action], {
      env: this.env(), stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.workers.add(child)
    let out = ''
    child.stdout!.setEncoding('utf8').on('data', (text: string) => { out += text })
    this.relay(child, action)
    child.stdin!.on('error', () => {}).end(JSON.stringify(args))
    return new Promise<T>((resolve, reject) => {
      let late = false
      const timer = setTimeout(() => { late = true; end(child) }, timeoutMs)
      child.on('error', (error) => reject(new MindError(action, { code: 'cannot start: ' + error.message })))
      child.on('close', (code, signal) => {
        clearTimeout(timer)
        this.workers.delete(child)
        if (late) return reject(new MindError(action, { code: 'timeout' }))
        let result: any
        try {
          // The answer is the last line; whatever the action printed before it is not.
          result = JSON.parse(out.trim().split('\n').pop() ?? '')
        } catch {
          return reject(new MindError(action, { code: `exited (${signal ?? code})` }))
        }
        if (typeof result?.error === 'string' && typeof result.kind === 'string') return reject(new MindError(action, result))
        resolve(result)
      })
    })
  }

  /**
   * eventmem's own worker, for as long as KIN is here: its memory jobs, its upkeep and its schedules.
   * It is started again a little after it ends, unless it was told to rest.
   */
  keepWorker(): void {
    this.resting = false
    this.startWorker()
  }

  /** While she is frozen the memory worker rests: it is stopped and not started again until `keepWorker`. */
  restWorker(): void {
    this.resting = true
    const worker = this.worker
    this.worker = this.memoryWorker.pid = this.memoryWorker.since = undefined
    if (worker) end(worker)
  }

  private startWorker(): void {
    if (this.closed || this.resting || this.worker) return
    const child = spawn(this.options.python, ['-c', 'import sys; from eventmem.core.cli import main; sys.exit(main())', 'worker', '--root', this.root], {
      env: this.env(), stdio: ['ignore', 'ignore', 'pipe'],
    })
    this.worker = child
    this.workers.add(child)
    this.memoryWorker.pid = child.pid
    this.memoryWorker.since = new Date().toISOString()
    this.relay(child, 'memory worker')
    child.on('close', (code, signal) => {
      this.workers.delete(child)
      // One told to rest is no longer the worker.
      if (this.worker !== child) return
      this.worker = this.memoryWorker.pid = this.memoryWorker.since = undefined
      if (this.closed) return
      const how = String(signal ?? code)
      this.memoryWorker.ends = [...this.memoryWorker.ends, { at: new Date().toISOString(), how }].slice(-10)
      this.options.log(`mind memory worker ended (${how}); starting it again shortly`)
      setTimeout(() => this.startWorker(), 30_000 * this.options.pace).unref()
    })
  }

  close(): void {
    this.closed = true
    this.stop()
    for (const worker of this.workers) end(worker)
  }

  // Core's environment: its sources, the token cache, and the model file (Core's kin_mind host
  // processes find it by themselves; eventmem's worker is told). Its model calls go to the relay on
  // this machine, so no proxy stands in between.
  private env(): NodeJS.ProcessEnv {
    const { coreSource, tokenizerCache } = this.options
    return {
      ...process.env,
      PYTHONPATH: coreSource,
      PYTHONDONTWRITEBYTECODE: '1',
      KIN_MODEL_CONFIG_FILE: this.modelFile,
      NO_PROXY: [process.env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(','),
      ...(tokenizerCache ? { TIKTOKEN_CACHE_DIR: tokenizerCache } : {}),
    }
  }

  private relay(child: ChildProcess, label: string) {
    createInterface({ input: child.stderr! }).on('line', (line) => {
      // jieba prints SyntaxWarnings on import; everything else is worth seeing.
      if (!/SyntaxWarning|re\.compile|^\s*$/.test(line)) this.options.log(`mind ${label}: ${line}`)
    })
  }

  private start(): ChildProcess {
    const { python, log } = this.options
    const child = spawn(python, ['-m', 'kin_mind.host', '--config', this.configFile, 'serve'], {
      env: this.env(), stdio: ['pipe', 'pipe', 'pipe'],
    })
    createInterface({ input: child.stdout! }).on('line', (line) => {
      let frame: { id: number; ok: boolean; result?: unknown; error?: MindError['detail'] }
      try {
        frame = JSON.parse(line)
      } catch {
        return log('mind: unexpected output: ' + line.slice(0, 200))
      }
      const request = this.pending.get(frame.id)
      if (!request) return
      this.pending.delete(frame.id)
      clearTimeout(request.timer)
      if (frame.ok) request.resolve(frame.result)
      else request.reject(new MindError(request.action, frame.error ?? {}))
    })
    this.relay(child, 'serve')
    // A process that died takes its pipe with it; the exit below answers every waiting request.
    child.stdin!.on('error', () => {})
    child.on('exit', (code, signal) => {
      if (this.child === child) this.child = undefined
      for (const [id, request] of this.pending) {
        clearTimeout(request.timer)
        request.reject(new MindError(request.action, { code: `exited (${signal ?? code})` }))
        this.pending.delete(id)
      }
    })
    child.on('error', (error) => log('mind: cannot start ' + python + ': ' + error.message))
    this.child = child
    return child
  }

  private stop(): void {
    const child = this.child
    this.child = undefined
    child?.stdin?.end()
    child?.kill()
  }
}

/** Asked to stop, then made to. */
function end(child: ChildProcess) {
  child.kill('SIGTERM')
  setTimeout(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL') }, 10_000).unref()
}

/**
 * Core's host config lives beside its store. Values the owner set stay; ones this release needs
 * and the file lacks are added (an exploration's stop and wake files sit in `state/`).
 */
function writeMindConfig(file: string, dataDir: string, pace: number): { root: string; scope: object } {
  mkdirSync(join(dataDir, 'state'), { recursive: true })
  const credentials = join(dataDir, 'credentials.env')
  if (!existsSync(credentials)) writeFileSync(credentials, '', { mode: 0o600 })
  const current = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : {}
  const defaults = {
    root: join(dataDir, 'db'),
    scope: { persona: 'kin' },
    credentials_file: credentials,
    agent_version: 'dsh-kin-0.1',
    session_id: 'kin',
    exploration_stop_file: join(dataDir, 'state', 'exploration-stop.json'),
    exploration_directory: join(dataDir, 'explorations'),
    // Her explorations and creations run as DSH helpers on her task lane (steward/), not Codex.
    exploration_backend: 'dsh',
    // Inside her own workspace (where her conversations live): a helper may write only there.
    creation_directory: join(dataDir, 'KIN', 'creations'),
    creation_model: 'kin/task',
    // At most this many background model calls at once (Core's own lanes; her replies come first).
    background_model_limit: 2,
  }
  const config = { ...defaults, ...current }
  // A dev pace reaches Core (lifecycle.PACE); in use there is none.
  if (pace === 1) delete config.pace
  else config.pace = pace
  if (Object.keys(defaults).some((key) => !(key in current)) || config.pace !== current.pace) {
    writeFileSync(file, JSON.stringify(config, null, 2) + '\n')
  }
  return config
}
