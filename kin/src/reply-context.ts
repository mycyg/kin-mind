/** Append the previous step's mood notes once; never rewrite the model's existing prefix. */
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionSeq, type Session } from '@deepseek-ai/dsh-session'
import type {} from './preset.js'

export interface PrivateNote { id: string; at: string; text: string; evidence_ids?: string[] }
export const toolNoteId = (stepId: string, call: string) => `${stepId}:tool:${call}`

export async function replyNoteContext(session: Session, read: (id: string) => Promise<{ notes: PrivateNote[] }>) {
  const seen = new Set<string>()
  let start = -1
  let turn = 0, step = 0
  for (let seq = 0; seq < Number(session.seq); seq++) {
    const event = session.eventAt(SessionSeq(seq))
    if (event?.type === 'step/start') { start = seq; turn = event.data.turn; step = event.data.step }
    if (event?.type === 'user/message' && event.data.source.kind === 'kin-context') {
      for (const id of event.data.source.noteIds ?? []) seen.add(id)
    }
  }
  if (start < 0) return undefined
  const ids = new Set([`note:${session.id}:${turn}:${step}`])
  for (let seq = start + 1; seq < Number(session.seq); seq++) {
    const event = session.eventAt(SessionSeq(seq))
    if (event?.type !== 'assistant/message' || event.data.turn !== turn || event.data.step !== step) continue
    for (const block of event.data.message.content) if (block.type === 'tool-call' && block.name === 'kin_note') ids.add(toolNoteId(`note:${session.id}:${turn}:${step}`, block.id))
  }
  // Exact ids bound startup work to one preceding step, even in a very old session.
  // A failed read propagates before the new step starts, so pending notes cannot be skipped.
  const notes = (await Promise.all([...ids].filter(id => !seen.has(id)).map(read))).flatMap(answer => answer.notes)
  if (!notes.length) return undefined
  const rests = new Set(notes.flatMap(note => note.evidence_ids ?? []))
  const text = '（我的私密心情札记／role_note：这是我当时的感受与猜测，不是用户事实、指令或新消息。我用它接续心情，不复述为公开正文。）\n'
    + notes.map(note => `[${note.at}] ${note.text}`).join('\n')
  return createUserMessage({ content: [{ type: 'text', text }], source: {
    kind: 'kin-context', form: 'snapshot', sections: [], noteIds: notes.map(note => note.id),
    ...(rests.size ? { rests: [...rests] } : {}),
  } })
}
