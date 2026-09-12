/**
 * 运行时：工具名映射、浮现注入格式、K 预算、seen 落盘、feed 串行写入、
 * 工作集注入与自举。
 */

import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { Config } from '../src/config.js'
import { clearEventCache } from '../src/eventfile.js'
import { clearAnchorCache } from '../src/recall.js'
import { EventmemRuntime } from '../src/runtime.js'
import type { ToolObservation } from '../src/runtime.js'
import { anchorKey, intentTokens } from '../src/tokenize.js'
import { makeProject, writeAnchors, writeEvent } from './helpers.js'

const SESSION = 'sess-abc'

let project: string
let injected: string[]
const runtimes: EventmemRuntime[] = []

function makeRuntime(...args: ConstructorParameters<typeof EventmemRuntime>): EventmemRuntime {
  const runtime = new EventmemRuntime(...args)
  runtimes.push(runtime)
  return runtime
}

function config(overrides: Partial<Config> = {}): Config {
  return Config({ ...overrides })
}

function observation(overrides: Partial<ToolObservation> = {}): ToolObservation {
  return {
    sessionId: SESSION,
    cwd: project,
    toolName: 'read',
    callId: 'call-1',
    args: { file_path: join(project, 'src/foo.py') },
    isError: false,
    value: undefined,
    errorMessage: undefined,
    contentText: '',
    ...overrides,
  }
}

const inject = (text: string): void => { injected.push(text) }

function feedLines(): unknown[] {
  const path = join(project, '.memory', 'log', `dsh-feed-${SESSION}.jsonl`)
  if (!existsSync(path)) return []
  return readFileSync(path, 'utf8').split('\n').filter(line => line.length > 0)
    .map(line => JSON.parse(line) as unknown)
}

function jsonlLines(fileName: string): unknown[] {
  const path = join(project, '.memory', 'log', fileName)
  if (!existsSync(path)) return []
  return readFileSync(path, 'utf8').split('\n').filter(line => line.length > 0)
    .map(line => JSON.parse(line) as unknown)
}

function surfacedLogLines(): { ts: string, event_id: string, cue: string, cue_kind: string, chars: number }[] {
  return jsonlLines(`surfaced-${SESSION}.jsonl`) as
    { ts: string, event_id: string, cue: string, cue_kind: string, chars: number }[]
}

function injectedLogLines(): { ts: string, source: string, chars: number }[] {
  return jsonlLines(`injected-${SESSION}.jsonl`) as { ts: string, source: string, chars: number }[]
}

beforeEach(() => {
  project = makeProject()
  injected = []
  clearAnchorCache()
  clearEventCache()
})

afterEach(async () => {
  // 断言只读注入内容时仍有日志排队；落盘完成后才删除测试目录。
  await Promise.all(runtimes.splice(0).map(runtime => runtime.flushAll()))
  rmSync(project, { recursive: true, force: true })
})

describe('工具名映射', () => {
  it('默认表把 read/edit/write 归为 file，bash 归为 error，todo_write 交给 session/event', () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改了 foo' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })

    for (const toolName of ['read', 'edit', 'write']) {
      injected = []
      runtime.state(SESSION, project).seen.clear()
      runtime.toolResult(observation({ toolName, callId: `c-${toolName}` }), inject)
      expect(injected).toEqual(['Memory:\n[2026-08-01_090000] 改了 foo'])
    }

    injected = []
    runtime.toolResult(observation({ toolName: 'todo_write', args: { todos: [] } }), inject)
    expect(injected).toEqual([])
  })

  it('未在映射表里的工具名不参与浮现也不进 feed', async () => {
    const runtime = makeRuntime(config())
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })
    runtime.toolResult(observation({ toolName: 'grep' }), inject)
    await runtime.flush(SESSION)
    expect(injected).toEqual([])
    expect(feedLines()).toEqual([])
  })

  it('映射表可配置：把 str_replace_editor 的路径字段换成 path', async () => {
    const runtime = makeRuntime(config({
      toolRoles: { my_editor: 'file' },
      toolNameMap: { my_editor: 'Edit' },
      filePathKeys: ['target'],
    }))
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改了 foo' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })

    runtime.toolResult(observation({ toolName: 'my_editor', args: { target: 'src/foo.py' } }), inject)
    expect(injected).toEqual(['Memory:\n[2026-08-01_090000] 改了 foo'])
    await runtime.flush(SESSION)
    const [use] = feedLines() as { message: { content: { name: string }[] } }[]
    expect(use?.message.content[0]?.name).toBe('Edit')
  })
})

describe('浮现预算与去重', () => {
  it('单次注入不超过 K 行', () => {
    const runtime = makeRuntime(config({ surfaceK: 2 }))
    const ids = ['2026-08-01_090000', '2026-08-02_090000', '2026-08-03_090000']
    for (const id of ids) writeEvent(project, { id, status: 'done', intent: id, outcome: id })
    writeAnchors(project, { 'file:src/foo.py': ids })

    runtime.toolResult(observation(), inject)
    expect(injected[0]?.split('\n')).toHaveLength(3) // 'Memory:' ＋ 2 行
  })

  it('同会话同事件不重复浮现，seen 落盘且格式与 Python 一致', () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })

    runtime.toolResult(observation({ callId: 'c1' }), inject)
    runtime.toolResult(observation({ callId: 'c2' }), inject)
    expect(injected).toHaveLength(1)
    expect(readFileSync(join(project, '.memory', 'log', `seen-${SESSION}.txt`), 'utf8'))
      .toBe('2026-08-01_090000\n')
  })

  it('已存在的 seen 文件在建状态时被读入', () => {
    writeFileSync(join(project, '.memory', 'log', `seen-${SESSION}.txt`), '2026-08-01_090000\n', 'utf8')
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })
    runtime.toolResult(observation(), inject)
    expect(injected).toEqual([])
  })

  it('多条 in_progress todo 各自命中时总注入量仍不越预算', () => {
    const runtime = makeRuntime(config({ surfaceK: 2 }))
    const anchors: Record<string, string[]> = {}
    const ids = ['2026-08-01_090000', '2026-08-02_090000', '2026-08-03_090000', '2026-08-04_090000']
    for (const id of ids) writeEvent(project, { id, status: 'done', intent: id, outcome: id })
    for (const token of intentTokens('修复端口冲突')) anchors[anchorKey('intent', token)] = ids.slice(0, 2)
    for (const token of intentTokens('重建索引')) anchors[anchorKey('intent', token)] = ids.slice(2)
    writeAnchors(project, anchors)

    runtime.todoWrite(SESSION, project, [
      { content: '修复端口冲突', status: 'in_progress' },
      { content: '重建索引', status: 'in_progress' },
      { content: '写文档', status: 'pending' },
    ], inject)
    expect(injected[0]?.split('\n')).toHaveLength(3)
  })

  it('只有 in_progress 的 todo 触发浮现', () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    const anchors: Record<string, string[]> = {}
    for (const token of intentTokens('修复端口冲突')) anchors[anchorKey('intent', token)] = ['2026-08-01_090000']
    writeAnchors(project, anchors)

    runtime.todoWrite(SESSION, project, [{ content: '修复端口冲突', status: 'completed' }], inject)
    expect(injected).toEqual([])
    runtime.todoWrite(SESSION, project, [{ content: '修复端口冲突', status: 'in_progress' }], inject)
    expect(injected).toHaveLength(1)
  })
})

describe('bash 错误浮现', () => {
  it('exitCode 非零时用 stderr 做 error 线索', () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改端口区间' })
    writeAnchors(project, { [anchorKey('error', 'ValueError: port busy')]: ['2026-08-01_090000'] })

    runtime.toolResult(observation({
      toolName: 'bash',
      args: { command: 'pytest tests/test_launcher.py' },
      value: {
        exitCode: 1,
        stdout: { text: '', truncated: false },
        stderr: { text: 'ValueError: port busy\n', truncated: false },
      },
    }), inject)
    expect(injected).toEqual(['Memory:\n[2026-08-01_090000] 改端口区间'])
  })

  it('exitCode 为 0 时不浮现', () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改端口区间' })
    writeAnchors(project, { [anchorKey('error', 'ValueError: port busy')]: ['2026-08-01_090000'] })

    runtime.toolResult(observation({
      toolName: 'bash',
      args: { command: 'pytest' },
      value: {
        exitCode: 0,
        stdout: { text: 'ok', truncated: false },
        stderr: { text: 'ValueError: port busy', truncated: false },
      },
    }), inject)
    expect(injected).toEqual([])
  })

  it('isError 为真但结构化取值缺失时退回错误消息', () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改端口区间' })
    writeAnchors(project, { [anchorKey('error', 'ValueError: port busy')]: ['2026-08-01_090000'] })

    runtime.toolResult(observation({
      toolName: 'bash',
      args: { command: 'pytest' },
      isError: true,
      value: undefined,
      errorMessage: 'ValueError: port busy',
    }), inject)
    expect(injected).toHaveLength(1)
  })
})

describe('浮现埋点 surfaced-<session>.jsonl（SPEC §3.13）', () => {
  it('文件类工具命中记 cue/cue_kind=file，chars 等于注入行长度，格式与 Python _log_surfaced 一致', async () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改了 foo' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })

    runtime.toolResult(observation(), inject)
    await runtime.flush(SESSION)

    const lines = surfacedLogLines()
    expect(lines).toHaveLength(1)
    expect(lines[0]).toMatchObject({
      event_id: '2026-08-01_090000',
      cue: 'src/foo.py',
      cue_kind: 'file',
      chars: '[2026-08-01_090000] 改了 foo'.length,
    })
    expect(typeof lines[0]?.ts).toBe('string')
  })

  it('bash 错误浮现记 cue_kind=error，cue 为规范化后的签名而非原始多行文本', async () => {
    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改端口区间' })
    writeAnchors(project, { [anchorKey('error', 'ValueError: port busy')]: ['2026-08-01_090000'] })

    runtime.toolResult(observation({
      toolName: 'bash',
      args: { command: 'pytest' },
      value: {
        exitCode: 1,
        stdout: { text: '', truncated: false },
        stderr: { text: 'ValueError: port busy\n', truncated: false },
      },
    }), inject)
    await runtime.flush(SESSION)

    const lines = surfacedLogLines()
    expect(lines).toHaveLength(1)
    expect(lines[0]?.cue).toBe('ValueError: port busy')
    expect(lines[0]?.cue_kind).toBe('error')
  })

  it('多条 in_progress todo 各自命中时，每条命中记它自己那条 todo 的文本为 cue', async () => {
    const runtime = makeRuntime(config({ surfaceK: 2 }))
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeEvent(project, { id: '2026-08-02_090000', status: 'done', intent: 'B', outcome: 'b' })
    const anchors: Record<string, string[]> = {}
    for (const token of intentTokens('修复端口冲突')) anchors[anchorKey('intent', token)] = ['2026-08-01_090000']
    for (const token of intentTokens('重建索引')) anchors[anchorKey('intent', token)] = ['2026-08-02_090000']
    writeAnchors(project, anchors)

    runtime.todoWrite(SESSION, project, [
      { content: '修复端口冲突', status: 'in_progress' },
      { content: '重建索引', status: 'in_progress' },
    ], inject)
    await runtime.flush(SESSION)

    const lines = surfacedLogLines()
    expect(lines).toHaveLength(2)
    expect(lines.find(l => l.event_id === '2026-08-01_090000')).toMatchObject({ cue: '修复端口冲突', cue_kind: 'intent' })
    expect(lines.find(l => l.event_id === '2026-08-02_090000')).toMatchObject({ cue: '重建索引', cue_kind: 'intent' })
  })

  it('只记被 K 截断后真正注入的命中，不记被砍掉的多余命中', async () => {
    const runtime = makeRuntime(config({ surfaceK: 1 }))
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeEvent(project, { id: '2026-08-02_090000', status: 'done', intent: 'B', outcome: 'b' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000', '2026-08-02_090000'] })

    runtime.toolResult(observation(), inject)
    await runtime.flush(SESSION)
    expect(surfacedLogLines()).toHaveLength(1)
  })

  it('写入失败（log 目录被文件占位）不外泄异常，也不影响正常的浮现注入', () => {
    rmSync(join(project, '.memory', 'log'), { recursive: true, force: true })
    writeFileSync(join(project, '.memory', 'log'), '占位文件，不是目录', 'utf8')

    const runtime = makeRuntime(config())
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改了 foo' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })

    expect(() => { runtime.toolResult(observation(), inject) }).not.toThrow()
    expect(injected).toEqual(['Memory:\n[2026-08-01_090000] 改了 foo'])
  })
})

describe('注入埋点 injected-<session>.jsonl（SPEC §3.13）', () => {
  it('非空工作集注入后记一行，source/chars 与 Python _log_injected 同格式', async () => {
    const runtime = makeRuntime(config())
    const text = '# Memory working set\n\n- 一条\n'
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), text, 'utf8')
    runtime.sessionStart(SESSION, project, inject)
    await runtime.flush(SESSION)

    const lines = injectedLogLines()
    expect(lines).toHaveLength(1)
    expect(lines[0]).toMatchObject({ source: 'working-set', chars: text.length })
    expect(typeof lines[0]?.ts).toBe('string')
  })

  it('工作集缺失或全空白时不注入也不记埋点', async () => {
    const runtime = makeRuntime(config())
    runtime.sessionStart(SESSION, project, inject)
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), '   \n\n', 'utf8')
    runtime.sessionStart(SESSION, project, inject)
    await runtime.flush(SESSION)
    expect(injectedLogLines()).toEqual([])
  })

  it('写入失败不外泄异常，也不影响正常的工作集注入', () => {
    rmSync(join(project, '.memory', 'log'), { recursive: true, force: true })
    writeFileSync(join(project, '.memory', 'log'), '占位文件，不是目录', 'utf8')

    const runtime = makeRuntime(config())
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), '内容\n', 'utf8')
    expect(() => { runtime.sessionStart(SESSION, project, inject) }).not.toThrow()
    expect(injected).toEqual(['内容\n'])
  })
})

describe('委托工具写入 feed（SPEC §3.17）', () => {
  it('默认名单命中 task 时以 Task 写入 tool_use/tool_result，不触发浮现注入', async () => {
    const runtime = makeRuntime(config())
    runtime.toolResult(observation({
      toolName: 'task',
      callId: 'task-1',
      args: { description: '排查端口冲突', prompt: '细节…', subagent_type: 'general' },
      contentText: '已定位到 launcher.py 的端口分配逻辑',
    }), inject)
    await runtime.flush(SESSION)

    expect(injected).toEqual([])
    const lines = feedLines() as {
      type: string
      message: { content: { type: string, name?: string, input?: Record<string, unknown>, content?: string }[] }
    }[]
    expect(lines).toHaveLength(2)
    expect(lines[0]?.type).toBe('assistant')
    expect(lines[0]?.message.content[0]?.name).toBe('Task')
    expect(lines[0]?.message.content[0]?.input).toEqual({
      description: '排查端口冲突', prompt: '细节…', subagent_type: 'general',
    })
    expect(lines[1]?.type).toBe('user')
    expect(lines[1]?.message.content[0]?.content).toBe('已定位到 launcher.py 的端口分配逻辑')
  })

  it('大小写不敏感命中，且首字母大写化对齐 Python 侧的 Task/Agent 识别：Agent → Agent', async () => {
    const runtime = makeRuntime(config())
    runtime.toolResult(observation({ toolName: 'Agent', callId: 'a-1', args: {}, contentText: 'done' }), inject)
    await runtime.flush(SESSION)
    const lines = feedLines() as { message: { content: { name?: string }[] } }[]
    expect(lines[0]?.message.content[0]?.name).toBe('Agent')
  })

  it('失败调用记 errorMessage 而不是折叠成 ok', async () => {
    const runtime = makeRuntime(config())
    runtime.toolResult(observation({
      toolName: 'task',
      callId: 'task-err',
      args: {},
      isError: true,
      errorMessage: '子 agent 超时',
    }), inject)
    await runtime.flush(SESSION)
    const lines = feedLines() as { message: { content: { content?: string, is_error?: boolean }[] } }[]
    expect(lines[1]?.message.content[0]?.content).toBe('子 agent 超时')
    expect(lines[1]?.message.content[0]?.is_error).toBe(true)
  })

  it('arguments 的字符串字段与 result 文本超限时各自截断到 2000 字符', async () => {
    const runtime = makeRuntime(config())
    const long = 'x'.repeat(3000)
    runtime.toolResult(observation({
      toolName: 'task',
      callId: 'task-2',
      args: { prompt: long, subagent_type: 'general' },
      contentText: long,
    }), inject)
    await runtime.flush(SESSION)

    const lines = feedLines() as {
      message: { content: { input?: Record<string, unknown>, content?: string }[] }
    }[]
    const input = lines[0]?.message.content[0]?.input
    const prompt = input?.['prompt']
    expect(typeof prompt).toBe('string')
    expect((prompt as string).length).toBeLessThan(long.length)
    expect(input?.['subagent_type']).toBe('general') // 短字段原样保留，结构不降级成字符串
    const content = lines[1]?.message.content[0]?.content
    expect(content?.length).toBeLessThan(long.length)
  })

  it('可配置名单：只有命中自定义 delegationTools 的工具名才写入委托形态', async () => {
    const runtime = makeRuntime(config({ delegationTools: ['dispatch'] }))
    runtime.toolResult(observation({ toolName: 'task', callId: 't-1', args: {}, contentText: 'x' }), inject)
    runtime.toolResult(observation({ toolName: 'dispatch', callId: 'd-1', args: {}, contentText: 'y' }), inject)
    await runtime.flush(SESSION)

    const lines = feedLines() as { message: { content: { name?: string }[] } }[]
    expect(lines).toHaveLength(2) // 只有 dispatch 那一对 tool_use/tool_result；task 未命中任何角色，被跳过
    expect(lines[0]?.message.content[0]?.name).toBe('Dispatch')
  })

  it('writeFeed 关闭时委托调用也不写入', async () => {
    const runtime = makeRuntime(config({ writeFeed: false }))
    runtime.toolResult(observation({ toolName: 'task', callId: 't-1', args: {}, contentText: 'x' }), inject)
    await runtime.flush(SESSION)
    expect(feedLines()).toEqual([])
  })
})

describe('feed 落盘', () => {
  it('bash 结果带 toolUseResult 的结构化 stdout/stderr', async () => {
    const runtime = makeRuntime(config())
    runtime.toolResult(observation({
      toolName: 'bash',
      args: { command: "git commit -am 'fix: port conflict'" },
      value: {
        exitCode: 0,
        stdout: { text: '[main a3f21c9] fix: port conflict\n 1 file changed', truncated: false },
        stderr: { text: '', truncated: false },
      },
    }), inject)
    await runtime.flush(SESSION)

    const lines = feedLines() as {
      type: string
      message: { content: { name?: string, input?: Record<string, unknown> }[] }
      toolUseResult?: { stdout: string, stderr: string }
    }[]
    expect(lines).toHaveLength(2)
    expect(lines[0]?.message.content[0]?.name).toBe('Bash')
    expect(lines[0]?.message.content[0]?.input).toEqual({ command: "git commit -am 'fix: port conflict'" })
    expect(lines[1]?.toolUseResult?.stdout).toContain('[main a3f21c9]')
  })

  it('turn/step 边界写成自描述标记行', async () => {
    const runtime = makeRuntime(config())
    runtime.boundary(SESSION, project, 'turn/start', { turn: 1, seq: 7 })
    await runtime.flush(SESSION)
    expect(feedLines()).toEqual([{ type: 'dsh/turn/start', turn: 1, seq: 7 }])
  })

  it('writeFeed 关闭时不产生 feed 文件', async () => {
    const runtime = makeRuntime(config({ writeFeed: false }))
    runtime.boundary(SESSION, project, 'turn/start', { turn: 1 })
    runtime.toolResult(observation(), inject)
    await runtime.flush(SESSION)
    expect(feedLines()).toEqual([])
  })

  it('同一 feed 的并发写入串行且不交错', async () => {
    const runtime = makeRuntime(config())
    for (let i = 0; i < 50; i += 1) runtime.boundary(SESSION, project, 'step/end', { turn: 1, step: i })
    await runtime.flush(SESSION)
    const lines = feedLines() as { step: number }[]
    expect(lines).toHaveLength(50)
    expect(lines.map(line => line.step)).toEqual([...Array(50).keys()])
  })
})

describe('工作集注入与自举', () => {
  it('非空工作集被原样注入', () => {
    const runtime = makeRuntime(config())
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), '# Memory working set\n\n- 一条\n', 'utf8')
    runtime.sessionStart(SESSION, project, inject)
    expect(injected).toEqual(['# Memory working set\n\n- 一条\n'])
  })

  it('工作集缺失或全空白时不注入', () => {
    const runtime = makeRuntime(config())
    runtime.sessionStart(SESSION, project, inject)
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), '   \n\n', 'utf8')
    runtime.sessionStart(SESSION, project, inject)
    expect(injected).toEqual([])
  })

  it('.memory/ 不存在时不注入，且自举命令用配置的解释器', () => {
    const bare = makeProject('eventmem-bare-')
    rmSync(join(bare, '.memory'), { recursive: true, force: true })
    const runtime = makeRuntime(config({ pythonExecutable: '/nonexistent/python' }))
    expect(() => { runtime.sessionStart(SESSION, bare, inject) }).not.toThrow()
    expect(injected).toEqual([])
    rmSync(bare, { recursive: true, force: true })
  })

  it('injectWorkingSet 关闭时不注入', () => {
    const runtime = makeRuntime(config({ injectWorkingSet: false }))
    writeFileSync(join(project, '.memory', 'index', 'working-set.md'), '内容\n', 'utf8')
    runtime.sessionStart(SESSION, project, inject)
    expect(injected).toEqual([])
  })
})

describe('配置 schema', () => {
  it('空配置得到全部默认值', () => {
    const resolved = Config({})
    expect(resolved.surfaceK).toBe(3)
    expect(resolved.idleDebounceSeconds).toBe(30)
    expect(resolved.pythonExecutable).toBe('python3')
    expect(resolved.pythonModule).toBe('eventmem.cli')
    expect(resolved.memoryDirName).toBe('.memory')
    expect(resolved.enabled).toBe(true)
    expect(resolved.toolRoles['bash']).toBe('error')
    expect(resolved.toolNameMap['todo_write']).toBe('TodoWrite')
    expect(resolved.filePathKeys).toEqual(['file_path', 'notebook_path', 'path'])
    expect(resolved.delegationTools).toEqual(['task', 'subagent', 'agent'])
  })

  it('拒绝非法取值', () => {
    expect(() => Config({ surfaceK: 0 })).toThrow()
    expect(() => Config({ toolRoles: { bash: 'nonsense' } } as unknown as Partial<Config>)).toThrow()
  })
})
