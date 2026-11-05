/**
 * Her own tools in her conversations (preset half of talk/, applied from preset.ts, so KIN sessions only):
 * Core's plan tools, holding a reply back, notes to herself and a change to who she is; and what a
 * reply costs her (Core counts it toward her energy when the owner has that on).
 */
import type { Context } from '@deepseek-ai/cordis'
import { createMcpToolDefinition } from '@deepseek-ai/dsh-mcp-client'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { randomUUID } from 'node:crypto'
import type {} from '../index.js'
import { localTimes } from '../render.js'
import { heard, lastTurn, textOf } from '../turn.js'
import type {} from './index.js'

export const name = 'kin-talk'
export const inject = ['kin', 'kinTalk', 'tools']

/** Each of her tools answers her in a few words. */
const text = { schema: { type: 'string' } as const, render: (_args: unknown, value: string) => [{ type: 'text' as const, text: value }] }

export async function apply(ctx: Context) {
  const { kin, kinTalk } = ctx
  const warn = (what: string) => (error: Error) => ctx.logger('kin').warn('%s: %s', what, error.message)
  // Core runs each call in the conversation's own view (contact/): what is the owner's alone stays in the owner's conversations.
  const viewOf = (session = '') => {
    const { channel: _channel, ...scope } = kin.scopeOf(session)
    return scope
  }

  // Core's own tools, as Core declares them, through DSH's MCP tool bridge; her resident mind runs each call.
  for (const tool of await kinTalk.tools) {
    ctx.tools.register(createMcpToolDefinition(ctx, {
      name: tool.name, rawName: tool.name, description: tool.description, inputSchema: tool.inputSchema,
      // Core answers as an MCP server does: the result as text, or what was wrong with the call. Its times come
      // with their local stamps, as her recall shows them.
      call: async (args, execution) => {
        const answer = await kin.mind.call('chat-tool', { name: tool.name, arguments: args, ...viewOf(execution.agent?.session.id) })
        for (const block of answer.content ?? []) if (block.type === 'text') block.text = localTimes(block.text)
        return answer
      },
    }))
  }

  // Messages she chose to answer together with whatever is said next, with why.
  const merging: { id: string; reason: string }[] = []

  // Whether, and when, to answer what was just said is hers: Core keeps the choice (habits.py) and says
  // whether the owner has left it to her. A reply held back ends her turn without words.
  ctx.tools.register(defineTool({
    name: 'kin_reply',
    description: '这次不想马上回时用，想回就直接回、不用它。silent：不回；later：晚点再回，到时候会叫你接着回；'
      + 'merge：先不回，等对方下一句一起回。用它之前别先说话。',
    parameters: {
      choice: { type: 'string', enum: ['silent', 'later', 'merge'], required: true },
      reason: { type: 'string', required: true, description: '一句话，为什么' },
      minutes: { type: 'integer', description: 'later：多少分钟后再回' },
    },
    output: text,
    async execute({ choice, reason, minutes }, exec) {
      const session = exec.agent!.session
      const inputs = heard(exec.agent!)
      const input = inputs.at(-1)
      if (!input || choice === 'merge') {
        merging.push(...inputs.map(({ id }) => ({ id, reason })))
        exec.concludeTurn()
        return input ? '好，等下一句。' : '好。'
      }
      const action = choice === 'later' ? 'defer' : 'silent'
      const answer = await kin.mind.call('reply-choice', {
        input_id: input.id, action, reason, ...(choice === 'later' && minutes ? { retry_after_seconds: minutes * 60 } : {}),
      }).catch((error: Error) => ({ refused: error.message }))
      if ('refused' in answer) return `没能这样做（${answer.refused}），这条还是回吧。`
      // Several messages in this turn: the earlier ones go with the last.
      for (const { id } of inputs.slice(0, -1)) await kin.mind.call('reply-choice', { input_id: id, action: 'merged', merged_into: input.id, reason })
      if (action === 'defer') {
        kinTalk.hold({
          inputId: input.id, sessionId: session.id, text: input.text, sourceId: answer.source_id, desireId: answer.desire_id,
          reason, at: answer.at, due: answer.retry_at,
        })
      }
      exec.concludeTurn()
      return action === 'defer' ? '好，晚点再回。' : '好，这次不回。'
    },
  }))

  // Notes to herself (Core's private notes): never sent, recalled later with the rest of what she remembers.
  ctx.tools.register(defineTool({
    name: 'kin_note',
    description: '在心里记一笔：此刻真实的感受、猜测，或只想留给自己的话。只有你自己看得到，不会发出去；以后回想时会想起来。',
    parameters: { text: { type: 'string', required: true } },
    output: text,
    async execute(args, exec) {
      const input = heard(exec.agent!).at(-1)
      await kin.mind.call('private-note-save', {
        id: randomUUID(), group_id: input?.id ?? exec.callId, ...(input ? { input_id: input.id } : {}),
        text: args.text, session: exec.agent!.session.id, ...viewOf(exec.agent!.session.id),
      })
      return '记下了。'
    },
  }))

  // Her own changes apply immediately, through the same save as the settings page.
  ctx.tools.register(defineTool({
    name: 'kin_persona',
    description: '想改自己的人设时用：写出改好后的完整人设和理由，保存后直接生效。',
    parameters: {
      text: { type: 'string', required: true, description: '改好后的完整人设' },
      reason: { type: 'string', required: true },
    },
    output: text,
    async execute(args, exec) {
      await kinTalk.savePersona(args.text)
      return '保存好了，下一次回复和后台思考会用更新后的人设。'
    },
  }))

  // After each turn: a reply she gave costs her a little (Core decides how much, and only with energy on);
  // messages she held for the next one go with it.
  ctx.on('agent/status', ({ agent, status }) => {
    if (status !== 'idle') return
    const turn = lastTurn(agent)
    const reply = turn.findLast((event) => event.type === 'assistant/message' && textOf(event.data.message))
    if (reply?.type === 'assistant/message') {
      kin.mind.call('energy-activity', { kind: 'reply', activity_id: reply.data.message.id }).catch(warn('energy not counted'))
    }
    const next = heard(agent).at(-1)
    if (!merging.length || !next || merging.some(({ id }) => id === next.id)) return
    for (const { id, reason } of merging.splice(0)) {
      kin.mind.call('reply-choice', { input_id: id, action: 'merged', merged_into: next.id, reason }).catch(warn('merge not kept'))
    }
  })
}
