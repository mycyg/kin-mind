/**
 * Mounted inside the KIN agent preset, so every listener here sees KIN's sessions only:
 * agent events are scope-filtered by agent. Ordinary coding sessions never reach this file.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type {} from '@deepseek-ai/dsh-compaction'
import { createUserMessage, type UserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq, type Session, type SessionEvent } from '@deepseek-ai/dsh-session'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from './index.js'
import { agenda } from './calendar.js'
import * as calendarTools from './calendar-tools.js'
import { OWNER } from './contact/people.js'
import { PLATFORMS } from './contact/conversations.js'
import * as contact from './contact/preset.js'
import { MindError } from './mind.js'
import { replyNoteContext } from './reply-context.js'
import { REPLY_INSTRUCTION } from './reply-output.js'
import { PROVIDER } from './models.js'
import { RECALL, renderContext, stamp, when } from './render.js'
import * as steward from './steward/preset.js'
import * as talk from './talk/preset.js'
import { heard, spoken, textOf, typed } from './turn.js'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    /**
     * What was said on WeChat / Feishu, relayed into KIN's session for that conversation. Core took each
     * message in as it arrived (contact/conversations.ts): `inputs` are their ids there, `source` the last one's source.
     */
    'kin-channel': {
      readonly kind: 'kin-channel'; readonly channel: string; readonly speaker: string
      readonly inputs: readonly string[]; readonly source: string
    }
    'kin-context': {
      readonly kind: 'kin-context'
      readonly form: 'snapshot'
      readonly sections: readonly { readonly name: string; readonly text: string }[]
      /** Core's receipt for the memories this block carries: acknowledged once the session has kept the block. */
      readonly receipt?: { readonly id: string; readonly epoch: string; readonly marker: string }
      /** The memories and sources the block's words rest on: one deleted later takes the block out of what a model is sent (models.ts). */
      readonly rests?: readonly string[]
      /** Private notes already appended; retained in the log across restart and compaction. */
      readonly noteIds?: readonly string[]
    }
  }
}

export const name = 'kin-preset'
export const inject = ['kin', 'agents', 'systemPrompt', 'tools']

/** How she keeps someone company while she works: a word first, and what is said meanwhile reaches her mid-turn. */
const PRESENCE = '回话要跟手：要花上一阵的事（上网查、叫帮手、交活），先回对方一句再去做，别让人干等。'
  + '你做事的中途对方又说了话，会在你下一步之前送到你面前：照常回应，该改主意就改，不必等手头的事做完。'

export function apply(ctx: Context) {
  const { kin } = ctx
  const warn = (what: string) => (error: Error) => ctx.logger('kin').warn('%s: %s', what, error.message)

  // Her conversations on other platforms and the relay tool (contact/preset.ts).
  ctx.plugin(contact)

  // The persona row renders `{{kin_persona}}`; the text lives in the data directory and can change live.
  ctx.effect(() => ctx.systemPrompt.variable('kin_persona', () => kin.persona()), 'kin persona variable')
  ctx.effect(() => ctx.systemPrompt.section({ name: 'kin-presence', order: ctx.systemPrompt.getSectionOrder('TOOL_SUBAGENT'), text: PRESENCE }),
    'kin presence guide')

  ctx.effect(() => ctx.systemPrompt.section({ name: 'kin-reply-envelope', order: ctx.systemPrompt.getSectionOrder('TOOL_SUBAGENT'), text: REPLY_INSTRUCTION }), 'kin reply envelope')
  ctx.on('agent/request', async ({ agent, turn, step }, next) => {
    const request = await next()
    const input = heard(agent).at(-1)
    kin.replyTargets.set(agent.session.id, { id: `note:${agent.session.id}:${turn}:${step}`, sessionId: agent.session.id, ...(input ? { inputId: input.id } : {}) })
    return request
  })

  ctx.on('agent/request-error', async ({ failure }, next) => failure.code === 'REPLY_NOTE_SAVE_FAILED' ? undefined : next(), { prepend: true })

  // The feeling line each conversation last showed her, so an unchanged mood is not repeated every turn. It
  // counts as shown once the session has kept the block that carries it (`showing`: that block, until then):
  // a turn stopped before that shows it again.
  const feelings = new Map<string, string>()
  const showing = new Map<string, { message: string; feeling: string }>()

  // A compaction DSH just finished is being told to Core, by session: her next recall waits for it, so it reads
  // the new window (what the summary replaced may be offered again) and not the old one's.
  const compacted = new Map<string, Promise<unknown>>()

  // What each session's context blocks rest on (memories and their sources), read from its log once and then
  // kept as blocks are added. Deletion reaches these blocks too: as a turn starts Core is asked which of them
  // are gone, and her lanes leave out every block resting on one (models.ts `erased`). A summary DSH writes of
  // the conversation rests on what the blocks it replaced did, and on what an earlier summary among them did.
  const resting = new Map<string, Set<string>>()
  const rest = (ids: Set<string>, session: Session, event: SessionEvent | undefined) => {
    if (event?.type === 'user/message' && event.data.source.kind === 'kin-context') {
      for (const id of event.data.source.rests ?? []) ids.add(id)
    } else if (event?.type === 'compaction/start') {
      kin.summaries.set(event.data.compactionId, { at: new Date(event.time).getTime(), rests: new Set() })
    } else if (event?.type === 'compaction/summary') {
      const summary = kin.summaries.get(event.data.compactionId)
      for (const seq of summary ? event.data.shadowedSeqs : []) {
        const replaced = session.eventAt(seq)
        const source = replaced?.type === 'user/message' ? replaced.data.source : undefined
        const from = source?.kind === 'kin-context' ? source.rests : source?.kind === 'compact-checkpoint' ? kin.summaries.get(source.compactionId)?.rests : undefined
        for (const id of from ?? []) summary!.rests.add(id)
      }
    }
  }
  const restsOf = (session: Session) => {
    let ids = resting.get(session.id)
    if (ids) return ids
    resting.set(session.id, ids = new Set())
    for (let seq = 0; seq < Number(session.seq); seq++) rest(ids, session, session.eventAt(SessionSeq(seq)))
    return ids
  }
  async function forget(session: Session) {
    const ids = [...restsOf(session)].filter((id) => !kin.erased.has(id))
    if (!ids.length) return
    kin.forgot((await kin.mind.call<{ deleted: string[] }>('deleted-sources', { source_ids: ids })).deleted)
  }

  /** Core's receipt for a block of memories the session has kept; given again it changes nothing. */
  const acknowledge = (session: Session, event: SessionEvent, receipt: { id: string; epoch: string; marker: string }) =>
    kin.mind.call('context-delivery-ack', {
      session: session.id, ...receipt, actual_session: session.id, verified: true,
      native_at: new Date(event.time).toISOString(), native_item_id: String(event.seq),
    })

  // Before the first step of every turn, and before a later step that brings what someone said while she
  // was busy: what KIN feels and remembers about what was just said.
  // Every block stays in the conversation, so each turn adds only what is new: Core leaves out what it
  // already gave this session, and a fixed budget keeps one turn's share small. Core counts a memory as given
  // only on a receipt: taken up before the block is shown (memories whose sources changed since are not shown),
  // and acknowledged once the session has kept the block (the `session/event` below).
  ctx.on('agent/pre-step', async ({ agent, step, signal }, next) => {
    // What is gone is asked before anything further down reads the conversation: a compaction may summarize it in this very step.
    if (step === 1) await forget(agent.session).catch(warn('deleted memories not checked'))
    const decision = await next()
    if (decision.kind === 'reject' || signal.aborted) return decision
    const { channel: _noteChannel, ...noteScope } = kin.scopeOf(agent.session.id)
    const noteContext = await replyNoteContext(agent.session, id => kin.mind.call('private-notes', { identifier: id, limit: 1, semantic: false, ...noteScope }))
    const withNotes = (messages: UserMessage[]) => noteContext ? [...messages, noteContext] : messages
    const said = decision.messages.some(spoken)
    if (step !== 1 && !said) return { ...decision, messages: withNotes(decision.messages) }
    const query = decision.messages.map(textOf).join('\n').trim()
    const session = agent.session.id
    if (said) kin.timeline.mark(session, 'step')
    // Recall in this conversation's own view: what is the owner's alone (her intimacy) is shown to the owner only.
    const { channel, ...scope } = kin.scopeOf(session)
    await compacted.get(session)
    let receipt: { id: string; epoch: string; marker: string } | undefined
    let rests: string[] = []
    let feeling: string | undefined
    const [recalled] = await Promise.all([
      kin.mind.call('memory-context', { query, purpose: 'chat', mode: 'light', session, ...scope })
        .then(async (answer) => {
          if (answer.injection?.state === 'prepared') {
            const { id, epoch, marker } = answer.injection
            const begun = await kin.mind.call('context-delivery-begin', { session, epoch, id })
            if (!begun.acquired) return ''
            receipt = { id, epoch, marker }
          }
          const rendered = renderContext(answer, kin.dimensions(), kin.nameOf, { shown: feelings.get(session) })
          feeling = rendered.feeling
          rests = rendered.rests
          return rendered.text
        })
        .catch((error) => (warn('recall skipped')(error), '')),
      // A later step that brings new words asks again; the first step asked above.
      step !== 1 && forget(agent.session).catch(warn('deleted memories not checked')),
    ])
    // What was just said joins her memory now: after her recall, so it is not recalled back to her, and
    // before she answers, so what she does with it this turn (a plan made of it, a reply held back) can name it.
    const source = await hear(session, decision.messages).catch((error) => (warn('not recorded yet')(error), ''))
    if (said) kin.timeline.mark(session, 'recalled')
    // Her own conversation (her outings, her making, the routines she set herself) reaches no one.
    const here = channel === 'own' ? '（当前对话：你自己的对话，这里说的话不会发给任何人）'
      : `（当前对话：${kin.nameOf(scope.recipient_person_id)}（${channel === 'app' ? '桌面' : PLATFORMS[channel] ?? channel}，${scope.recipient_person_id.startsWith('group:') ? '群聊' : '私聊'}））`
    // A turn nobody here started (a reminder come due, word of handed-out work) may find the person elsewhere:
    // when and where they last spoke is hers to weigh (kin_tell reaches them there).
    const last = step === 1 && !said && channel !== 'own' ? ctx.get('kinContact')?.heardFrom(scope.recipient_person_id) : undefined
    const page = scope.recipient_person_id !== OWNER || !kin.seen() ? ''
      : Date.now() - kin.seen() < 90_000 ? '；你的桌面页面此刻正开在对方面前' : `；对方上一次打开你的桌面页面是 ${when(kin.seen())}`
    const away = last && `（这一轮不是对方开口引起的。对方上一次说话是 ${when(last.at)}，在${last.where}${page}。）`
    // How the work she handed out from here has moved since she last heard (steward/work.ts), so she need not go and look.
    const work = ctx.get('kinSteward')?.brief(session)
    // The stretch of days she is in (a trip of the owner's, an exam week), as her calendar has it.
    const days = step === 1 && ctx.get('kinCalendar')?.current()
    // They asked her to leave them be for a while: she may be here on a reminder, and they may be the one to take it back.
    const wait = step === 1 && channel !== 'own' && ctx.get('kinContact')?.askedToWait(scope.recipient_person_id)
    const asked = wait && `（对方说过先别找他，到 ${wait} 为止。）`
    const text = [step === 1 && here, days, away, asked, recalled, work, source && `这条消息的来源编号：${source}`].filter(Boolean).join('\n')
    if (!text) return { ...decision, messages: withNotes(decision.messages) }
    const block = createUserMessage({
      content: [{ type: 'text', text }],
      source: {
        kind: 'kin-context', form: 'snapshot', sections: [{ name: 'kin-context', text }],
        ...(recalled && receipt ? { receipt } : {}), ...(recalled && rests.length ? { rests } : {}),
      },
    })
    if (recalled && feeling) showing.set(session, { message: block.id, feeling })
    return { ...decision, messages: withNotes([...decision.messages, block]) }
  }, { prepend: true })

  // The session has kept a block of her memories: Core counts them as given to this session from now, so a
  // later step of this very turn is not offered them again (`record` gives the same receipt again at the end of
  // the turn, which is what makes it sure). And a compaction that just finished is told to Core at once.
  ctx.on('session/event', (session, event) => {
    if (event.type === 'compaction/end' && !event.data.error) {
      feelings.delete(session.id)
      compacted.set(session.id, kin.mind.call('memory-compact-ack', {
        session: session.id, actual_session: session.id, epoch: event.data.compactionId, completed: true,
      }).catch(warn('compaction not told to her mind yet')))
    }
    // A session read for the first time just now has this event in its reading already.
    const read = resting.has(session.id)
    const ids = restsOf(session)
    if (read) rest(ids, session, event)
    if (event.type !== 'user/message' || event.data.source.kind !== 'kin-context') return
    const shown = showing.get(session.id)
    if (shown && shown.message === event.data.id) {
      feelings.set(session.id, shown.feeling)
      showing.delete(session.id)
    }
    if (event.data.source.receipt) acknowledge(session, event, event.data.source.receipt).catch(warn('recall receipt not kept yet'))
  })

  // When a turn settles, what was said joins KIN's memory, in log order.
  // Her background work waits while she is replying, and looks again once she has.
  ctx.on('agent/status', ({ agent, status }) => {
    // DSH fixes a step's model before agent/request; select her lane when the turn starts.
    if (status === 'running') agent.model.select({ provider: PROVIDER, model: 'chat' })
    kin.replying(agent.session.id, status === 'running')
    if (status !== 'idle') return
    kin.replyTargets.delete(agent.session.id)
    kin.timeline.rest(agent.session.id, kin.channelOf(agent.session.id) !== 'app')
    void record(agent).then(() => kin.nudge(), warn('recording failed'))
  })

  // Her reply timeline: what the owner types in the app begins one as it enters her inbox (a platform's
  // arrivals begin theirs in contact/conversations.ts), and her first word is the first text she streams.
  ctx.on('agent/inbox/inserted', ({ agent, message }) => {
    if (message.source.kind === 'user') kin.timeline.begin(agent.session.id, kin.scopeOf(agent.session.id).conversation_key)
  })
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta' && frame.chunk.text) kin.timeline.mark(agent.session.id, 'spoke')
  })

  // Messages heard as their turn started (by message id, which is how Core knows them), not to be taken in twice.
  const taken = new Set<string>()

  /** The owner's words join her memory; what came from a channel already did as it arrived. */
  async function hear(session: string, messages: UserMessage[]) {
    let source = ''
    for (const message of messages) {
      if (message.source.kind === 'kin-channel') {
        source = message.source.source
        continue
      }
      const text = spoken(message) ? typed(message) : ''
      if (!text) continue
      const at = new Date().toISOString()
      ;({ source_id: source } = await kin.mind.call('ingest', {
        id: message.id, text, at, session, speaker_person_id: OWNER, ...kin.scopeOf(session),
      }))
      taken.add(message.id)
    }
    return source
  }

  async function record(agent: Agent) {
    const session = agent.session
    const end = Number(session.seq)
    const scope = kin.scopeOf(session.id)
    for (let seq = kin.recorded(session.id); seq < end; seq++) {
      const event = session.eventAt(SessionSeq(seq))
      const at = event && new Date(event.time).toISOString()
      const id = `${session.id}:${seq}`
      if (event?.type === 'user/message' && event.data.source.kind === 'user') {
        const text = typed(event.data)
        if (text && !taken.delete(event.data.id)) {
          await kin.mind.call('ingest', { id: event.data.id, text, at, session: session.id, speaker_person_id: OWNER, ...scope })
        }
      } else if (event?.type === 'user/message' && event.data.source.kind === 'kin-context' && event.data.source.receipt) {
        // A receipt Core itself refuses (its window has moved on, the record is gone) is let go; one her mind
        // could not take just now stops here and is given again when the next turn settles.
        await acknowledge(session, event, event.data.source.receipt).catch((error: Error) => {
          if (!(error instanceof MindError) || !error.detail.error || error.detail.error === 'OperationalError') throw error
          warn('recall receipt refused')(error)
        })
      } else if (event?.type === 'assistant/message') {
        const text = textOf(event.data.message)
        if (text) await kin.mind.call('observe', { id, text, at, ...scope })
      } else if (event?.type === 'compaction/end' && !event.data.error) {
        // DSH summarized the older turns, the recall blocks in them included: Core may offer those
        // memories again, and her mood is worth saying once more.
        await kin.mind.call('memory-compact-ack', {
          session: session.id, actual_session: session.id, epoch: event.data.compactionId, completed: true,
        })
        feelings.delete(session.id)
      }
      kin.markRecorded(session.id, seq + 1)
    }
  }

  // Handing the owner's work to DSH workers and keeping track of it (steward/).
  ctx.plugin(steward)

  ctx.tools.register(defineTool({
    name: 'kin_recall',
    description: '回想和某件事、某个人、某段时间有关的记忆，连同你当时心里记下的；每条带着是谁说的、跟谁说的、哪天说的，和它的编号。'
      + '当前对话里已经给过你的不用再查；对方问起很久以前的事、或你想确认细节时再用。'
      + '有人说你记错了，或你自己发现记错了：用它找到那条，再用 correct_memory 把它改对。',
    parameters: {
      about: { type: 'string', required: true, description: '想回想的内容，用自然语言描述' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      // Her memories, and the notes she kept to herself about it (Core's private notes), as this conversation may see them.
      const session = exec.agent?.session.id ?? ''
      const { channel: _channel, ...scope } = kin.scopeOf(session)
      const [answer, { notes }] = await Promise.all([
        kin.mind.call('memory-context', { query: args.about, ...RECALL, session, ...scope }),
        kin.mind.call<{ notes: { at: string; text: string }[] }>('private-notes', { query: args.about, limit: 3, ...scope }),
      ])
      const mine = notes.map((note) => `- ${stamp(note.at)}：${note.text}`)
      return [renderContext(answer, kin.dimensions(), kin.nameOf, { ids: true }).text, mine.length ? '你当时心里记下的（只有你自己知道）：\n' + mine.join('\n') : '']
        .filter(Boolean).join('\n') || '没想起什么相关的。'
    },
  }))

  // Her calendar, the same gathering her page shows the owner (calendar.ts).
  ctx.tools.register(defineTool({
    name: 'kin_calendar',
    description: '翻翻你的日程，和对方在你页面的日历上看到的是同一份：这几天里定了时间的事（提醒、你给自己定的打算、晚点要回的话、信拆开的日子、计划的步骤）、记着的一段日子和纪念日、'
      + '已经做过的（日记、梦、出门、帮手做完的活、你主动发出的话），和还没做的（计划、帮手手头的活、心愿、约好的事）。'
      + '对方问起安排、哪天做过什么、还有什么没做时用。',
    parameters: {
      from: { type: 'string', description: '从哪天看起，YYYY-MM-DD；不填就是今天' },
      days: { type: 'integer', description: '看几天；不填看 7 天' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const today = new Date()
      const from = args.from ? new Date(args.from + 'T00:00:00') : new Date(today.getFullYear(), today.getMonth(), today.getDate())
      if (Number.isNaN(from.getTime())) return '日期写成 2026-01-31 这样。'
      const to = new Date(from)
      to.setDate(from.getDate() + Math.min(62, Math.max(1, args.days ?? 7)))
      const calendar = ctx.get('kinCalendar')
      return calendar ? agenda(await calendar.items(from, to)) : '日程现在读不出来。'
    },
  }))

  // Marking a stretch of days on her calendar, and routines of her own (calendar-tools.ts).
  ctx.plugin(calendarTools)

  // Her own tools in conversation: plans, holding a reply back, notes to herself, a change to who she is.
  ctx.plugin(talk)
}
