// Dev-only, mounted by dev/e2e-channels.mjs inside a real headless DSH: another plugin listening on KIN's
// channels through the `kinTransports` service, writing each arrival to `config.out`.
import { appendFileSync } from 'node:fs'

export const name = 'kin-channels-probe'
export const inject = ['kinTransports']

export function apply(ctx, config) {
  ctx.effect(() => ctx.kinTransports.onInbound((message) => appendFileSync(config.out, JSON.stringify(message) + '\n')), 'kin channels probe')
}
