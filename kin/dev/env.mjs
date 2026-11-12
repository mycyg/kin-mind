import { replyEnvelope } from './reply-fixture.mjs'
// An isolated DSH home with dsh-kin linked in, a scripted mock model and KIN's real mind on synthetic data.
// Nothing here touches ~/.dsh: DSH_HOME and HOME both point inside .dev/.
import { spawn } from 'node:child_process'
import { mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startMockLlmServer } from '@deepseek-ai/dsh-llm-mock-server'

export const kinDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export const repo = resolve(kinDir, '..')
/** How fast her waits pass in a suite (the plugin's `pace`): a minute is three seconds. KIN_PACE=1 runs at her real pace. */
export const PACE = Number(process.env.KIN_PACE || 0.05)

export async function bootDsh({ name, extraRows = '', mockText = '嗯嗯，我在呢。', fresh = true, kinFiles = {}, pace = PACE }) {
  const root = join(repo, '.dev', name)
  if (fresh) rmSync(root, { recursive: true, force: true })
  const home = join(root, 'dsh-home'), dataDir = join(root, 'kin-data')
  const profile = join(home, 'profiles', name)
  mkdirSync(join(profile, 'node_modules'), { recursive: true })
  mkdirSync(dataDir, { recursive: true })
  for (const [file, value] of Object.entries(kinFiles)) writeFileSync(join(dataDir, file), JSON.stringify(value, null, 2))
  writeFileSync(join(profile, 'package.json'), JSON.stringify({
    name: 'dsh-profile-' + name, private: true,
    dependencies: { 'dsh-kin': 'link:' + kinDir },
    dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', 'dsh-kin'] } },
  }, null, 2))
  try { symlinkSync(kinDir, join(profile, 'node_modules', 'dsh-kin')) } catch { /* kept from an earlier run */ }
  const overlay = join(root, 'dev.patch.yml')
  writeFileSync(overlay, `- id: kin
  config:
    dataDir: ${JSON.stringify(dataDir)}
    python: ${JSON.stringify(join(repo, 'core/.venv/bin/python'))}
    coreSource: ${JSON.stringify(join(repo, 'core/src'))}
    tokenizerCache: ${JSON.stringify(join(repo, '.dev/tiktoken'))}
    pace: ${pace}
${extraRows}`)

  const mock = await startMockLlmServer({ sequence: ['success'], repeatLast: true, successText: replyEnvelope(mockText) })
  const dsh = spawn(process.execPath, [join(kinDir, 'node_modules/@deepseek-ai/dsh/lib/bin.js'),
    '--profile', name, '--patch', overlay, '--no-open', '--port', '0'], {
    cwd: root,
    env: { ...process.env, HOME: root, DSH_HOME: home, DSH_TELEMETRY_DISABLED: '1',
      DEEPSEEK_BASE_URL: mock.baseURL, DEEPSEEK_API_KEY: 'mock-key' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let logs = ''
  const url = new Promise((resolveUrl) => {
    const watch = (chunk) => {
      logs += chunk
      writeFileSync(join(root, 'dsh.log'), logs)
      const found = /dsh web: (\S+)/.exec(logs)
      if (found) resolveUrl(found[1])
    }
    dsh.stdout.on('data', watch)
    dsh.stderr.on('data', watch)
  })
  return { root, dataDir, mock, dsh, url, logs: () => logs }
}

/** The page's routes, called the way the browser calls them: the login link mints a cookie. */
export async function signIn(url) {
  const login = await fetch(url, { redirect: 'manual' })
  const cookie = login.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ')
  const call = (route, body) => fetch(new URL(route, url), body === undefined ? { headers: { cookie } }
    : { method: 'POST', headers: { cookie, 'content-type': 'application/json' }, body: JSON.stringify(body) }).then((r) => r.json())
  return { get: (route) => call(route), post: call }
}

/** DSH asked to stop, then made to. */
export async function stopDsh({ dsh, mock }) {
  dsh.kill('SIGTERM')
  await Promise.race([new Promise((done) => dsh.once('exit', done)), new Promise((done) => setTimeout(done, 5000))])
  if (dsh.exitCode === null && dsh.signalCode === null) dsh.kill('SIGKILL')
  await mock.close()
}
