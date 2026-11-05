/**
 * Who she talks with: the people and groups the owner set up on her page, and who each one is on
 * each platform. Core knows them by these ids alone (`person:<id>`, `group:<id>`); the owner is
 * always `person:person_a`, whom everything unattributed in her mind belongs to.
 */
import { randomBytes } from 'node:crypto'
import type { Inbound } from '../channels/transport.js'

export const OWNER = 'person:person_a'

export interface Person {
  id: string
  name: string
  /** Whether she may reach out to them on her own. */
  proactive: boolean
  /** Their user id on each platform, e.g. `{ wechat: 'wxid_…', feishu: 'ou_…' }`. */
  accounts: Record<string, string>
}

export interface Group {
  id: string
  name: string
  /** The group's chat on each platform. */
  chats: Record<string, string>
  /** Whether she may chime in without being @-mentioned; otherwise she only answers an @. */
  chime: boolean
}

export interface People {
  people: Person[]
  groups: Group[]
}

export const EMPTY: People = { people: [{ id: OWNER, name: '', proactive: true, accounts: {} }], groups: [] }

/** What an arrival is: whose conversation it belongs to and who said it. Unknown chats and strangers are not hers. */
export function placeOf(directory: People, message: Inbound): { recipient: Person | Group; speaker?: Person } | undefined {
  const speaker = directory.people.find((person) => person.accounts[message.transport] === message.sender)
  if (message.kind === 'group') {
    const group = directory.groups.find((entry) => entry.chats[message.transport] === message.chat)
    return group && { recipient: group, speaker }
  }
  return speaker && { recipient: speaker, speaker }
}

/** How she calls someone in her notes to herself. */
export const nameOf = (entry: Person | Group | undefined) => entry?.name.trim() || (entry?.id === OWNER ? '主人' : '对方')

export const isGroup = (id: string) => id.startsWith('group:')

/** How she calls whoever an id names in her memories (Core's speaker and recipient): the owner is 主人. */
export function callOf(directory: People, id: string): string {
  if (id === OWNER) return '主人'
  const entry = [...directory.people, ...directory.groups].find((e) => e.id === id)
  return entry ? nameOf(entry) : isGroup(id) ? '一个群' : '有人'
}

/** Who she knows, in her persona: every model of hers then reads the ids her memories carry as people. */
export function rosterOf(directory: People): string {
  const owner = directory.people.find((person) => person.id === OWNER)?.name.trim()
  const lines = [
    ...directory.people.map((person) => `${person.id}：${person.id === OWNER ? '主人' + (owner ? `（${owner}）` : '') : nameOf(person)}`),
    ...directory.groups.map((group) => `${group.id}：${nameOf(group)}（群）`),
  ]
  return '你认识的人（记忆里用这些编号记是谁说的、在和谁的对话里说的；agent:kin 是你自己）：\n' + lines.join('\n')
}

/** The owner's edit of the directory, kept as given where it is well formed: ids are made here, once. */
export function revise(next: Partial<People>): People {
  const accounts = (value: unknown) => Object.fromEntries(Object.entries(value && typeof value === 'object' ? value : {})
    .flatMap(([platform, id]) => (typeof id === 'string' && id.trim() ? [[platform, id.trim()]] : [])))
  const idOf = (kind: 'person' | 'group', id: unknown) => (typeof id === 'string' && new RegExp(`^${kind}:[A-Za-z0-9_-]{1,40}$`).test(id)
    ? id : `${kind}:${randomBytes(5).toString('hex')}`)
  const people: Person[] = (next.people ?? []).map((person) => ({
    id: idOf('person', person.id), name: String(person.name ?? '').trim(), proactive: person.proactive === true, accounts: accounts(person.accounts),
  }))
  if (!people.some((person) => person.id === OWNER)) people.unshift({ ...EMPTY.people[0]! })
  const groups: Group[] = (next.groups ?? []).map((group) => ({
    id: idOf('group', group.id), name: String(group.name ?? '').trim(), chats: accounts(group.chats), chime: group.chime === true,
  }))
  // One platform account is one person, one chat one group: an arrival must never be two of them.
  for (const [what, entries] of [['账号', people.map((p) => p.accounts)], ['群', groups.map((g) => g.chats)]] as const) {
    const seen = new Set<string>()
    for (const [platform, id] of entries.flatMap((entry) => Object.entries(entry))) {
      if (seen.has(platform + ':' + id)) throw new Error(`同一个${what}填在了两个地方：${id}`)
      seen.add(platform + ':' + id)
    }
  }
  return { people, groups }
}
