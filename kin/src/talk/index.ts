/**
 * What KIN does in a conversation besides speaking, and the owner's say over it (host half; the
 * preset half, talk/preset.ts, offers her the tools):
 * - Core's chat tools (her plans), with the schemas Core declares;
 * - a reply she chose to give later goes back into its conversation when its time comes;
 * - changes to her persona apply to her next reply and background review;
 * - the owner's choices on her page: whether answering (and when) is hers to decide, whether she tires;
 * - a tiny call through one model route, to see whether it answers.
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { SessionId } from '@deepseek-ai/dsh-session'
import { randomUUID } from 'node:crypto'
import type { Host } from '../index.js'
import { probe, type Route } from '../models.js'
import { stamp } from '../render.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    kinTalk: Talk
  }
}

/** One of Core's tools as MCP declares it (core/src/kin_mind/chat_tools.py). */
export interface CoreTool {
  name: string
  description: string
  inputSchema: Record<string, unknown>
}

/** A reply she chose to give later, and where. */
export interface Held {
  inputId: string
  sessionId: string
  /** What was said, and Core's source and wish for it. */
  text: string
  sourceId: string
  desireId: string
  reason: string
  at: string
  due: string
}

export interface Talk {
  /** Core's chat tools; none when her mind could not say. */
  readonly tools: Promise<CoreTool[]>
  hold(reply: Held): void
  savePersona(text: string): Promise<void>
}

/** The owner's words for each answer to "may she choose whether and when to answer?", kept as the owner's statement. */
const REPLY_CHOICE: Record<string, string> = {
  autonomous: '我在你的设置页里说：闲聊的时候，回不回、什么时候回，你自己决定就好。',
  always: '我在你的设置页里说：我发的消息，你都回我吧。',
}

export function talk(host: Host) {
  const { ctx, mind } = host
  const held = host.load<Held[]>('replies.json', [])

  // A note to her, read with the next thing said to her in that conversation (or, with `now`, at once).
  const tell = async (sessionId: string, text: string, now = false) => {
    const resolved = await ctx.get('sessionController')!.resolveAgent(sessionId as SessionId)
    if (!('agent' in resolved)) return host.log(`cannot reach conversation ${sessionId}: ${JSON.stringify(resolved.error)}`)
    const message = createUserMessage({
      content: [{ type: 'text', text }],
      source: { kind: 'kin-context', form: 'snapshot', sections: [{ name: 'kin-context', text }] },
    })
    if (now) resolved.agent.followup(message)
    else resolved.agent.inject(message)
  }

  ctx.provide('kinTalk', {
    tools: mind.call<{ tools: CoreTool[] }>('chat-tools').then((answer) => answer.tools, (error: Error) => {
      host.log('her chat tools are not offered: ' + error.message)
      return []
    }),
    hold(reply) {
      held.push(reply)
      host.save('replies.json', held)
    },
    savePersona: (text) => host.savePersona(text),
  } satisfies Talk)

  // When a held reply's time comes, it goes back into its conversation, unless she has since let the wish go
  // (or a contact attempt took it up). Core's wish for it is then fulfilled, so nothing sends it a second time.
  host.heartbeat.every('replies', async () => {
    // At the dev pace (index.ts `pace`) the wait Core set passes faster too.
    const due = held.filter((reply) => Date.parse(reply.at) + host.ms(Date.parse(reply.due) - Date.parse(reply.at)) <= Date.now())
    if (!due.length) return
    const { state } = await mind.call('read')
    for (const reply of due) {
      const wish = (state.desires as { id: string; status: string }[]).find((desire) => desire.id === reply.desireId)
      if (wish && ['wanted', 'waiting'].includes(wish.status)) {
        const done = await mind.call('chat-tool', { name: 'manage_desire', arguments: { request: {
          command_id: 'reply-later:' + reply.inputId, agent_version: state.agent_version, expected_revision: state.revision,
          action: 'complete', desire_id: reply.desireId, evidence_ids: [reply.sourceId], reason: '回到原来的对话里接着回',
        } } })
        if (done.isError) throw new Error(done.content[0].text)
        await tell(reply.sessionId, `（${stamp(reply.at)}，你想晚点再回这条：「${reply.text}」——当时想的是：${reply.reason}。`
          + '现在可以接着回了；已经不必回的话，用 kin_reply 选不回。）', true)
      }
      held.splice(held.indexOf(reply), 1)
      host.save('replies.json', held)
    }
    return { state: 'complete', reason: `接回了 ${due.length} 条` }
  })

  // Her page: what waits on the owner, and the owner's say over how she answers and whether she tires.
  host.route('/api/kin/talk', {
    GET: async () => {
      const overview = await mind.call('state-overview', { query: '' })
      return {
        held: held.length,
        replyChoice: overview.conversation_habits?.preferences?.reply_choice ?? 'always',
        energy: overview.energy_context ?? { enabled: false },
      }
    },
  })
  host.route('/api/kin/talk/choices', {
    POST: async (request) => {
      const { replyChoice, energy } = await host.body<{ replyChoice: string; energy: boolean }>(request)
      if (replyChoice && REPLY_CHOICE[replyChoice]) {
        await mind.call('owner-habit', {
          command_id: randomUUID(), text: REPLY_CHOICE[replyChoice], preferences: { reply_choice: replyChoice },
        })
      }
      if (typeof energy === 'boolean') await mind.call('energy-configure', { enabled: energy })
      return { saved: true }
    },
  })

  // 测一下: a tiny call through exactly this route.
  host.route('/api/kin/models/test', {
    POST: async (request) => {
      const { provider, model, effort } = await host.body<Route>(request)
      return probe(ctx, { provider: provider!, model: model!, ...(effort ? { effort } : {}) })
    },
  })
}
