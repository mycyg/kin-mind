/**
 * How far her helpers may go (帮手的权限), the owner's choice on her page, in DSH's own permission
 * presets: 问我 (workspace-write: the folder it works in, and DSH asks the owner before anything
 * beyond), 自动审核 (Auto: the model reviews each step first; a step it turns down waits for the
 * owner, or is refused outright in a delegated child), 全权 (full access, never asks). Only what this
 * DSH composes is offered; left unchosen, 全权 (the owner's call: work handed over should not stall on
 * approvals, and Auto costs a review call per step).
 *
 * It applies wherever her helpers act for the owner: to a worker session when kin_delegate creates it,
 * and to her own conversations at the start of each turn, since DSH's subagents inherit their
 * conversation's Auto or full access (with asking pinned off). Her own outings are not the owner's work
 * and act on whatever the web says, so their helpers stay in her own workspace (helpers.ts). Work
 * already going keeps what it started with.
 */
import type {} from '@deepseek-ai/dsh-permission-presets'
import type { Session } from '@deepseek-ai/dsh-session'
import type { Host } from '../index.js'

/** What the page can offer, in DSH's preset names, most careful first. */
const OFFERED = ['workspace-write', 'auto', 'danger-full-access']

/** Puts the owner's choice on a session, or the given preset instead. */
export type Permit = (session: Session, preset?: string) => void

export function permission(host: Host): Permit {
  const saved = host.load<{ preset?: string }>('permission.json', {})
  const presets = () => host.ctx.get('permissionPresets')
  const offered = () => OFFERED.filter((name) => presets()?.names.includes(name))
  const current = () => {
    const names = offered()
    return names.find((name) => name === saved.preset) ?? (names.includes('danger-full-access') ? 'danger-full-access' : names[0])
  }

  host.route('/api/kin/permission', {
    GET: async () => ({ offered: offered(), current: current() ?? null }),
    POST: async (request) => {
      const { preset } = await host.body<{ preset: string }>(request)
      if (!preset || !offered().includes(preset)) throw new Error('这个 DSH 里没有这一档权限')
      saved.preset = preset
      host.save('permission.json', saved)
      return { saved: true }
    },
  })

  return (session, preset) => {
    const name = preset ?? current()
    // DSH writes only what differs, so the same choice again changes nothing.
    if (name) presets()!.set(session, name)
  }
}
