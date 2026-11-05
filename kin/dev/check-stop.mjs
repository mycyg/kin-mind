import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = root + '/kin/.stop-check'
await build({ entryPoints: [root + '/kin/src/contact/conversations.ts'], outdir: output, bundle: true, platform: 'node', format: 'esm', packages: 'external' })
try {
  const { Conversations } = await import(output + '/conversations.js')
  const stopped = []
  let cancelled = false
  const steward = {
    list: () => [
      { id: 'mine', session: 'owner-chat', state: 'running' },
      { id: 'finished', session: 'owner-chat', state: 'done' },
      { id: 'theirs', session: 'friend-chat', state: 'running' },
    ],
    stop: id => stopped.push(id),
  }
  const controller = { resolveAgent: async () => ({ agent: { cancel: () => { cancelled = true } } }) }
  const host = {
    ctx: { get: name => name === 'kinSteward' ? steward : name === 'sessionController' ? controller : undefined },
    ready: Promise.resolve(), load: (_name, empty) => empty, save: () => {}, log: () => {},
    conversations: () => ({ 'wechat:owner': { sessionId: 'owner-chat' } }),
  }
  const directory = () => ({ people: [{ id: 'person:person_a', accounts: { wechat: 'owner' } }], groups: [] })
  const conversations = new Conversations(host, directory, () => ({}), () => undefined, () => {}, () => ({}))
  conversations.receive({ transport: 'wechat', id: 'stop-1', chat: 'owner', sender: 'owner', kind: 'direct', text: '停止任务', at: new Date().toISOString() })
  await conversations.arrivals
  assert.equal(cancelled, true)
  assert.deepEqual(stopped, ['mine'])
  conversations.close()
  console.log('PASS: channel stop ends the current conversation and its running workers, leaving other sessions alone')
} finally {
  rmSync(output, { recursive: true, force: true })
}
