// Dev-only, mounted by dev/e2e-health.mjs inside a real headless DSH: breaks two things her health check should
// find — one call on her task lane, whose only route goes nowhere, and her memory worker, killed — then writes
// `config.out` for the harness, which asks for a check at once (the worker is started again half a minute later, at the suite's pace).
import { writeFileSync } from 'node:fs'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { keepLog } from './scenario.mjs'

export const name = 'kin-e2e-health'
export const inject = ['kin', 'llm']

export function apply(ctx, config) {
  keepLog(ctx, config.out + '.log')
  setTimeout(() => probe(ctx).then(
    (report) => writeFileSync(config.out, JSON.stringify(report)),
    (error) => writeFileSync(config.out, JSON.stringify({ error: error.stack ?? String(error) })),
  ), 300)
}

async function probe(ctx) {
  const { mind } = ctx.kin
  // Her heartbeat starts the worker once start-up recovery is done.
  for (const end = Date.now() + 120_000; !mind.memoryWorker.pid; await new Promise((resolve) => setTimeout(resolve, 250))) {
    if (Date.now() > end) throw new Error('her memory worker never started')
  }
  const messages = [createUserMessage({ content: [{ type: 'text', text: '在吗' }], source: { kind: 'user' } })]
  for await (const chunk of ctx.llm.stream({ provider: 'kin', model: 'task', messages })) void chunk
  const worker = mind.memoryWorker.pid
  process.kill(worker, 'SIGKILL')
  while (mind.memoryWorker.pid) await new Promise((resolve) => setTimeout(resolve, 100))
  return { worker }
}
