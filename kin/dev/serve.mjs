// A long-running isolated DSH web instance with dsh-kin, for looking at the KIN page in a browser.
// Usage: node dev/serve.mjs  → prints the login URL. Mind data persists in .dev/serve between runs.
import { bootDsh } from './env.mjs'

const { dsh, url, logs, mock } = await bootDsh({ name: 'serve', fresh: false, mockText: '嗯嗯，我在呢。今天想聊点什么？' })
const timer = setTimeout(() => { console.error('DSH did not start:\n' + logs().slice(-3000)); process.exit(1) }, 90_000)
console.log('KIN dev DSH: ' + await url)
clearTimeout(timer)
dsh.on('exit', async (code) => { await mock.close(); console.log('DSH exited', code); process.exit(code ?? 0) })
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => dsh.kill(signal))
