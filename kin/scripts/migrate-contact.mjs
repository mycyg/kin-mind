/** Recover established conversations and contact limits from the old store, without replaying messages. */
import { DatabaseSync } from 'node:sqlite'
import { join } from 'node:path'

export function contactHistory(store, scope, people, groups, ownerId = 'person:person_a') {
  const identity = (id) => id === ownerId ? 'person:person_a' : id
  const known = new Map([...people, ...groups].map((person) => [person.id, person]))
  const activity = {}, conversations = {}
  const database = new DatabaseSync(join(store, 'memory.sqlite3'), { readOnly: true })
  const key = JSON.stringify({ project: 'personal', persona: 'default', collection: 'default', world: 'real', ...scope })
  try {
    const rows = database.prepare(`SELECT occurred_at AS at, json_extract(data,'$.recipient_person_id') AS person,
      json_extract(data,'$.channel') AS channel, json_extract(data,'$.conversation_id') AS chat
      FROM mind_runtime_events WHERE scope=? AND kind='owner-message' ORDER BY occurred_at DESC`).all(key)
    for (const row of rows) {
      const id = identity(row.person ?? ownerId), person = known.get(id)
      if (!person || activity[id] || !Number.isFinite(Date.parse(row.at))) continue
      const group = id.startsWith('group:')
      const channel = row.channel
      if (!['wechat', 'feishu'].includes(channel)) continue
      if (!(group ? person.chats?.[channel] : person.accounts?.[channel])) continue
      const chat = row.chat || (channel === 'wechat' ? person.accounts?.wechat : group ? person.chats?.feishu : undefined)
      if (!chat) continue
      const via = `${channel}:${chat}`
      activity[id] = { heard: row.at, via, reached: [] }
      conversations[via] = { channel, recipient: id, address: { chat, kind: group ? 'group' : 'direct' }, title: group ? `${person.name}（群）` : `和${person.name}的${channel === 'feishu' ? '飞书' : '微信'}` }
    }
    const sent = database.prepare(`SELECT json_extract(data,'$.recipient_person_id') AS person,
      json_extract(data,'$.updated_at') AS at FROM mind_contacts WHERE scope=? AND state='accepted'
      ORDER BY json_extract(data,'$.updated_at') DESC`).all(key)
    for (const row of sent) {
      const record = activity[identity(row.person ?? ownerId)]
      if (record && record.reached.length < 24 && Number.isFinite(Date.parse(row.at))) record.reached.unshift(row.at)
    }
    return { activity, conversations }
  } finally { database.close() }
}
