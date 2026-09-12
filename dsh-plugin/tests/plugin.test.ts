/**
 * 插件装配层：监听器注册、注入消息的来源标记、idle 去抖、卸载兜底，
 * 以及最要紧的护栏纪律——任何监听器内的异常都不向宿主抛出。
 */

import { readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'

import { Config, apply, name } from '../src/index.js'
import { clearEventCache } from '../src/eventfile.js'
import { clearAnchorCache } from '../src/recall.js'
import { makeProject, readIfExists, writeAnchors, writeEvent } from './helpers.js'

type Listener = (...args: never[]) => unknown
const pendingDisposers: (() => Promise<void> | void)[] = []

interface Harness {
  ctx: Context
  listeners: Map<string, Listener[]>
  disposers: (() => Promise<void> | void)[]
  fire: (event: string, ...args: unknown[]) => unknown
}

function harness(): Harness {
  const listeners = new Map<string, Listener[]>()
  const disposers: (() => Promise<void> | void)[] = []
  const raw = {
    on(event: string, listener: Listener) {
      const bucket = listeners.get(event) ?? []
      bucket.push(listener)
      listeners.set(event, bucket)
      return () => { /* noop dispose */ }
    },
    effect(execute: () => unknown) {
      const produced = execute()
      if (typeof produced === 'function') {
        const dispose = produced as () => Promise<void> | void
        disposers.push(dispose)
        pendingDisposers.push(dispose)
      }
      return { [Symbol.asyncDispose]: async () => { /* noop */ } }
    },
    get() {
      return undefined
    },
  }
  const fire = (event: string, ...args: unknown[]): unknown => {
    let last: unknown
    for (const listener of listeners.get(event) ?? []) {
      last = (listener as (...rest: unknown[]) => unknown)(...args)
    }
    return last
  }
  return { ctx: raw as unknown as Context, listeners, disposers, fire }
}

interface FakeAgent {
  agent: Agent
  injected: { content: { type: string, text: string }[], source: unknown }[]
  maintenanceCalls: number
}

function fakeAgent(sessionId: string, cwd: string, options: { broken?: boolean } = {}): FakeAgent {
  const injected: FakeAgent['injected'] = []
  const state = { maintenanceCalls: 0 }
  const session = { id: sessionId, header: { id: sessionId, cwd } }
  const raw = {
    get session() {
      if (options.broken === true) throw new Error('session 不可用')
      return session
    },
    inject(message: { content: { type: string, text: string }[], source: unknown }) {
      injected.push(message)
    },
    async runMaintenance<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T> {
      state.maintenanceCalls += 1
      return task(new AbortController().signal)
    },
  }
  return {
    agent: raw as unknown as Agent,
    injected,
    get maintenanceCalls() {
      return state.maintenanceCalls
    },
  }
}

const SESSION = 'sess-plugin'
let project: string

beforeEach(() => {
  project = makeProject('eventmem-plugin-')
  clearAnchorCache()
  clearEventCache()
})

afterEach(async () => {
  // 插件卸载先排空异步日志，目录清理不能与 mkdir/appendFile 争抢。
  await Promise.all(pendingDisposers.splice(0).map(dispose => dispose()))
  vi.useRealTimers()
  rmSync(project, { recursive: true, force: true })
  // 会话不可用时护栏日志退回当前目录（与 Python 侧 _guard_log 同口径），清掉这个副产物
  rmSync(join(process.cwd(), '.memory'), { recursive: true, force: true })
})

describe('监听器注册', () => {
  it('订阅 DSH-ADAPTER §2.2 里的六个扩展点', () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    for (const event of [
      'agent/session-start',
      'tools/result',
      'session/event',
      'session/flush',
      'agent/status',
      'agent/disposed',
      'session/disposed',
    ]) {
      expect(h.listeners.get(event), event).toHaveLength(1)
    }
    expect(h.disposers).toHaveLength(1)
  })

  it('enabled 为 false 时一个监听器都不注册', () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true, enabled: false }))
    expect(h.listeners.size).toBe(0)
    expect(h.disposers).toHaveLength(0)
  })
})

describe('注入', () => {
  it('会话启动注入工作集，来源标记为 plugin/eventmem/recall', () => {
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), '# 工作集\n\n- 一条\n', 'utf8')
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    const a = fakeAgent(SESSION, project)
    h.fire('agent/session-start', { agent: a.agent, source: 'startup' })

    expect(a.injected).toHaveLength(1)
    expect(a.injected[0]?.content).toEqual([{ type: 'text', text: '# 工作集\n\n- 一条\n' }])
    expect(a.injected[0]?.source).toEqual({ kind: 'plugin', plugin: name, form: 'recall' })
  })

  it('compact 后的重启同样注入', () => {
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), '# 工作集\n', 'utf8')
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    const a = fakeAgent(SESSION, project)
    h.fire('agent/session-start', { agent: a.agent, source: 'compact' })
    expect(a.injected).toHaveLength(1)
  })

  it('todo/write 走 session/event，通过已登记的 agent 注入', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改端口区间' })
    writeAnchors(project, { 'intent:端口': ['2026-08-01_090000'], 'intent:口冲': ['2026-08-01_090000'] })
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    const a = fakeAgent(SESSION, project)
    h.fire('agent/session-start', { agent: a.agent, source: 'startup' })
    a.injected.length = 0

    h.fire(
      'session/event',
      { id: SESSION, header: { id: SESSION, cwd: project } },
      { type: 'todo/write', seq: 3, time: 0, data: { todos: [{ content: '端口冲突', status: 'in_progress' }] } },
    )
    expect(a.injected[0]?.content[0]?.text).toBe('Memory:\n[2026-08-01_090000] 改端口区间')
  })

  it('tools/result 上的浮现通过 exec.agent 注入', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改了 foo' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    const a = fakeAgent(SESSION, project)

    h.fire(
      'tools/result',
      { agent: a.agent, name: 'read', callId: 'c1', arguments: { file_path: 'src/foo.py' } },
      { isError: false, value: undefined, content: [] },
    )
    expect(a.injected[0]?.content[0]?.text).toBe('Memory:\n[2026-08-01_090000] 改了 foo')
  })

  it('exec.agent 缺失时跳过本次浮现（R-5）', () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    expect(() => {
      h.fire(
        'tools/result',
        { name: 'read', callId: 'c1', arguments: { file_path: 'src/foo.py' } },
        { isError: false, value: undefined, content: [] },
      )
    }).not.toThrow()
  })
})

describe('护栏纪律', () => {
  it('负载内部抛出时异常不外泄，且落到该项目的 eventmem-dsh.log', () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    const a = fakeAgent(SESSION, project)
    const exec = {
      agent: a.agent,
      callId: 'c1',
      arguments: {},
      get name(): string { throw new Error('工具名不可读') },
    }

    expect(() => {
      h.fire('tools/result', exec, { isError: false, value: undefined, content: [] })
    }).not.toThrow()
    expect(readIfExists(join(project, '.memory', 'log', 'eventmem-dsh.log')))
      .toContain('tools/result 异常 Error: 工具名不可读')
  })

  it('连会话都取不到时也不外泄异常', () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    const broken = fakeAgent(SESSION, project, { broken: true })

    expect(() => { h.fire('agent/session-start', { agent: broken.agent, source: 'startup' }) }).not.toThrow()
    expect(() => {
      h.fire('tools/result', { agent: broken.agent, name: 'read', callId: 'c', arguments: {} },
        { isError: false, value: undefined, content: [] })
    }).not.toThrow()
    expect(() => { h.fire('agent/status', { agent: broken.agent, status: 'idle' }) }).not.toThrow()
    expect(() => { h.fire('agent/disposed', { agent: broken.agent }) }).not.toThrow()
  })

  it('畸形负载不外泄异常', () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    const cases: [string, unknown[]][] = [
      ['session/event', [{ id: SESSION, header: {} }, { type: 'todo/write', data: {} }]],
      ['session/event', [{ id: SESSION, header: { cwd: project } }, { type: '未知类型', data: null }]],
      ['tools/result', [{ agent: undefined, name: 'bash', callId: 'c', arguments: null },
        { isError: true, error: { message: 'boom' }, content: null }]],
      ['session/disposed', [{ id: 'never-seen', header: { cwd: project } }]],
    ]
    for (const [event, args] of cases) {
      expect(() => { h.fire(event, ...args) }, event).not.toThrow()
    }
  })

  it('session/flush 被 await 且异常不外泄', async () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    await expect(h.fire('session/flush', { id: SESSION, header: { cwd: project } })).resolves.toBeUndefined()
    await expect(h.fire('session/flush', null)).resolves.toBeUndefined()
  })
})

describe('idle 去抖与整理调度', () => {
  it('连续 idle 达配置秒数才触发 runMaintenance', async () => {
    vi.useFakeTimers()
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true, idleDebounceSeconds: 30, pythonExecutable: '/nonexistent/python-xyz' }))
    const a = fakeAgent(SESSION, project)

    // 先制造 feed 内容，否则没有脏量不会触发
    h.fire('session/event', { id: SESSION, header: { cwd: project } },
      { type: 'turn/end', seq: 1, time: 0, data: { turn: 1, reason: 'completed' } })

    h.fire('agent/status', { agent: a.agent, status: 'idle' })
    await vi.advanceTimersByTimeAsync(29_000)
    expect(a.maintenanceCalls).toBe(0)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(a.maintenanceCalls).toBe(1)
  })

  it('去抖窗口内转回 running 则取消', async () => {
    vi.useFakeTimers()
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true, idleDebounceSeconds: 30, pythonExecutable: '/nonexistent/python-xyz' }))
    const a = fakeAgent(SESSION, project)
    h.fire('session/event', { id: SESSION, header: { cwd: project } },
      { type: 'turn/end', seq: 1, time: 0, data: { turn: 1, reason: 'completed' } })

    h.fire('agent/status', { agent: a.agent, status: 'idle' })
    await vi.advanceTimersByTimeAsync(10_000)
    h.fire('agent/status', { agent: a.agent, status: 'running' })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(a.maintenanceCalls).toBe(0)
  })

  it('feed 无新内容时不触发整理', async () => {
    vi.useFakeTimers()
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true, idleDebounceSeconds: 1, pythonExecutable: '/nonexistent/python-xyz' }))
    const a = fakeAgent(SESSION, project)
    h.fire('agent/status', { agent: a.agent, status: 'idle' })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(a.maintenanceCalls).toBe(0)
  })

  it('runMaintenance 同步抛出时只记日志', async () => {
    vi.useFakeTimers()
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true, idleDebounceSeconds: 1, pythonExecutable: '/nonexistent/python-xyz' }))
    const refusing = {
      session: { id: SESSION, header: { id: SESSION, cwd: project } },
      inject() { /* noop */ },
      runMaintenance() { throw new Error('agent 正被驱动') },
    } as unknown as Agent

    h.fire('session/event', { id: SESSION, header: { cwd: project } },
      { type: 'turn/end', seq: 1, time: 0, data: { turn: 1, reason: 'completed' } })
    h.fire('agent/status', { agent: refusing, status: 'idle' })
    await vi.advanceTimersByTimeAsync(3_000)
    expect(readIfExists(join(project, '.memory', 'log', 'eventmem-dsh.log')))
      .toContain('runMaintenance 拒绝')
  })
})

describe('卸载兜底', () => {
  it('disposer flush 掉排队中的 feed 写入', async () => {
    const h = harness()
    apply(h.ctx, Config({ legacyMode: true,}))
    h.fire('session/event', { id: SESSION, header: { cwd: project } },
      { type: 'turn/start', seq: 1, time: 0, data: { turn: 1 } })

    for (const dispose of h.disposers) await dispose()
    const feed = join(project, '.memory', 'log', `dsh-feed-${SESSION}.jsonl`)
    expect(readFileSync(feed, 'utf8')).toContain('"type":"dsh/turn/start"')
  })
})
