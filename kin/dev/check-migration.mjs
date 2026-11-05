import assert from 'node:assert/strict'
import {DatabaseSync} from 'node:sqlite'
import {mkdtempSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {contactHistory} from '../scripts/migrate-contact.mjs'
const root=mkdtempSync(join(tmpdir(),'kin-contact-migration-'))
try {
  const db=new DatabaseSync(join(root,'memory.sqlite3'))
  db.exec('CREATE TABLE mind_runtime_events(scope TEXT,kind TEXT,occurred_at TEXT,data TEXT); CREATE TABLE mind_contacts(scope TEXT,state TEXT,data TEXT)')
  const scope={project:'personal',persona:'kin',collection:'default',world:'real'},key=JSON.stringify(scope)
  const add=(person,channel,chat,at,where=key)=>db.prepare('INSERT INTO mind_runtime_events VALUES(?,?,?,?)').run(where,'owner-message',at,JSON.stringify({recipient_person_id:person,channel,conversation_id:chat}))
  add('person:old_owner','wechat','wx-owner','2026-11-01T10:00:00Z')
  add('person:old_owner','feishu','oc-owner','2026-11-02T10:00:00Z')
  add('person:friend','feishu','oc-friend','2026-11-02T11:00:00Z')
  add('group:family','feishu','oc-family','2026-11-02T12:00:00Z')
  add('person:unknown','feishu','oc-unknown','2026-11-02T13:00:00Z')
  add('person:old_owner','feishu','wrong-scope','2026-11-03T10:00:00Z','other')
  for(const state of ['accepted','pending','unconfirmed'])db.prepare('INSERT INTO mind_contacts VALUES(?,?,?)').run(key,state,JSON.stringify({recipient_person_id:'person:old_owner',updated_at:'2026-11-02T14:00:00Z'}))
  db.close()
  const h=contactHistory(root,scope,[{id:'person:person_a',name:'主人',accounts:{wechat:'wx-owner',feishu:'ou-owner'}},{id:'person:friend',name:'朋友',accounts:{feishu:'ou-friend'}}],[{id:'group:family',name:'家人',chats:{feishu:'oc-family'}}],'person:old_owner')
  assert.equal(Object.keys(h.activity).length,3)
  assert.equal(h.activity['person:person_a'].via,'feishu:oc-owner')
  assert.equal(h.activity['person:friend'].via,'feishu:oc-friend')
  assert.deepEqual(h.activity['person:person_a'].reached,['2026-11-02T14:00:00Z'])
  assert.equal(h.conversations['feishu:oc-family'].address.kind,'group')
  assert.equal(h.conversations['feishu:oc-owner'].recipient,'person:person_a')
  console.log('PASS: established chats restored per person and scope; only accepted sends count; unknown sends are not replayed')
} finally {rmSync(root,{recursive:true,force:true})}
