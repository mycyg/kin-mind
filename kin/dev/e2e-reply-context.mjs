// Durable note context, restart and deletion using native synthetic Sessions.
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { Session, SessionId } from '@deepseek-ai/dsh-session'
import { createAssistantMessage, createUserMessage } from '@deepseek-ai/dsh-llm'
const dir = mkdtempSync(resolve('.note-context-'))
await build({ stdin: { contents: "export * from './reply-context.ts'; export * from './models.ts'", resolveDir: resolve('src') }, outfile: join(dir, 'bundle.mjs'), bundle: true, packages: 'external', platform: 'node', format: 'esm' })
const { replyNoteContext, toolNoteId, KinModel } = await import(join(dir, 'bundle.mjs'))
let failures = 0
const check = async (name, fn) => { try { await fn(); console.log('✓ ' + name) } catch (error) { failures++; console.error('✗ ' + name + '\n' + error.stack) } }
const session = Session.create(SessionId('synthetic-notes'))
const appendStep = (turn, step = 1) => { session.append('turn/start', { turn }); session.append('step/start', { turn, step }) }
const note = (id, text = '我刚才有点不舍，现在轻松些了。') => ({ id, at: '2026-01-01T00:00:00.000Z', text, evidence_ids: ['src_synthetic'] })
try {
  await check('startup reads only one preceding step; original note text is appended once and survives restart', async () => {
    for (let i = 1; i <= 1000; i++) appendStep(i)
    const calls = []
    const block = await replyNoteContext(session, async id => { calls.push(id); return { notes: [note(id)] } })
    assert.deepEqual(calls, ['note:synthetic-notes:1000:1'])
    assert.equal(block.source.kind, 'kin-context'); assert.deepEqual(block.source.rests, ['src_synthetic'])
    assert.match(block.content[0].text, /role_note/); assert.match(block.content[0].text, /不是用户事实/)
    assert.equal(block.content[0].text.split(note('').text).length - 1, 1)
    assert.ok(!block.content[0].text.includes('reply_text')); assert.ok(!block.content[0].text.includes('provider'))
    session.append('user/message', block, { surfaceOp: 'append' })
    const restored = Session.create(session.id, JSON.parse(JSON.stringify(session.snapshotEvents())))
    assert.equal(await replyNoteContext(restored, async () => assert.fail('already appended note must not be fetched again')), undefined)
  })
  await check('compacted note markers remain durable and never reinsert the old package', async () => {
    const context = session.snapshotEvents().find(e => e.type === 'user/message' && e.data.source.noteIds)
    session.append('user/message', createUserMessage({ source: { kind: 'synthetic-summary' }, content: [{ type: 'text', text: 'A checkpoint retains the earlier subjective feeling as a feeling, not a fact.' }] }),
      { surfaceOp: { op: 'replace', startSeq: context.seq, endSeq: context.seq }, sourceEventSeqs: [context.seq] })
    const restored = Session.create(session.id, JSON.parse(JSON.stringify(session.snapshotEvents())))
    assert.equal(await replyNoteContext(restored, async () => assert.fail('compacted note must not be reinjected')), undefined)
  })
  await check('a failed read stays recoverable and tool notes do not mutate the call/result prefix', async () => {
    appendStep(1001)
    const id = toolNoteId('note:synthetic-notes:1001:1', 'synthetic-call')
    const message = createAssistantMessage({ source: { provider: 'kin', model: 'chat' }, content: [{ type: 'tool-call', id: 'synthetic-call', name: 'kin_note', arguments: '{"text":"我有点开心。"}' }] })
    session.append('assistant/message', { turn: 1001, step: 1, stream: [], message }, { surfaceOp: 'append' })
    const before = session.snapshotEvents()
    await assert.rejects(replyNoteContext(session, async () => { throw Error('synthetic read unavailable') }), /read unavailable/)
    assert.deepEqual(session.snapshotEvents(), before)
    const requested = []
    const block = await replyNoteContext(session, async key => { requested.push(key); return { notes: key === id ? [note(key, '我有点开心。')] : [] } })
    assert.deepEqual(requested, ['note:synthetic-notes:1001:1', id]); assert.deepEqual(block.source.noteIds, [id])
    assert.deepEqual(session.snapshotEvents(), before)
    session.append('user/message', block, { surfaceOp: 'append' })
    const restored = Session.create(session.id, session.snapshotEvents())
    let reads = 0
    assert.equal(await replyNoteContext(restored, async key => { reads++; assert.notEqual(key, id); return { notes: [] } }), undefined)
    assert.equal(reads, 1)
  })
  await check('erasing the source removes private note context from the next provider request', async () => {
    const block = session.snapshotEvents().find(e => e.type === 'user/message' && e.data.source.noteIds)?.data
    let request
    const model = new KinModel({ llm: { async *stream(r) { request = r; yield { type: 'text-delta', index: 0, text: 'Synthetic answer.' }; yield { type: 'finish', reason: { kind: 'stop' } } } } }, () => [{ provider: 'synthetic', model: 'synthetic', effort: 'off' }])
    model.erased.set('src_synthetic', new Date().toISOString())
    for await (const _ of model.stream({ provider: 'kin', model: 'task', messages: [block] })) {}
    assert.ok(!JSON.stringify(request.messages).includes(note('').text)); assert.match(request.messages[0].content[0].text, /撤下/)
  })
} finally { rmSync(dir, { recursive: true, force: true }) }
if (failures) process.exitCode = 1
