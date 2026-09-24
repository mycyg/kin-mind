/**
 * 对 `exec.arguments`（类型 `unknown`）与 `result.value`（类型 `JsonValue`）的收窄校验。
 *
 * 工具自己校验各自的 schema，注册表不代为收窄（`packages/core/tools/src/index.ts:322-323`），
 * 因此这里一律保护式取值，不假设任何字段存在。
 *
 * @module
 */

/** todo 快照的一条。 */
export interface FeedTodo {
  /** 任务文本。 */
  content: string
  /** 生命周期状态。 */
  status: string
}

/**
 * 收窄成普通对象。
 *
 * @param value - 任意取值。
 * @returns 对象本体，或 undefined。
 */
export function asObject(value: unknown): Record<string, unknown> | undefined {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined
  return value as Record<string, unknown>
}

/**
 * 取一个非空字符串字段。
 *
 * @param source - 对象。
 * @param key - 字段名。
 * @returns 去除两端空白后的字符串，或 undefined。
 */
export function asText(source: Record<string, unknown> | undefined, key: string): string | undefined {
  if (source === undefined) return undefined
  const value = source[key]
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

/**
 * 取 todo 快照。
 *
 * @param todos - `todo/write` 事件的 `data.todos` 或 todo 工具的入参。
 * @returns 规约后的 todo 列表；无有效条目时为空数组。
 */
export function asTodos(todos: unknown): FeedTodo[] {
  if (!Array.isArray(todos)) return []
  const out: FeedTodo[] = []
  for (const raw of todos) {
    const item = asObject(raw)
    if (item === undefined) continue
    const content = asText(item, 'content') ?? asText(item, 'activeForm') ?? asText(item, 'task')
    if (content === undefined) continue
    const status = (asText(item, 'status') ?? 'pending').toLowerCase()
    out.push({ content, status })
  }
  return out
}

/**
 * 把 dsh 的内容块拍平成纯文本，只保留 `type === 'text'` 的块
 * （口径同 `hooks-claude-code/src/index.ts:318-320` 的 `blocksToText`）。
 *
 * @param blocks - 内容块数组。
 * @returns 拼接后的文本。
 */
export function blocksToText(blocks: readonly unknown[]): string {
  const parts: string[] = []
  for (const raw of blocks) {
    const block = asObject(raw)
    if (block === undefined) continue
    if (block['type'] === 'text' && typeof block['text'] === 'string') parts.push(block['text'])
  }
  return parts.join('\n')
}
