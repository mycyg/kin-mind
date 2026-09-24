// The hard rule: a change to the persona canon needs 小光's own confirmation. The canon file checks
// only itself (its hashes sit beside its texts, so whoever edits the texts can rewrite them); the
// owner's approval is the record the host keeps outside it (mind-config `persona_contract`). These
// cases pin that record's check in the core reader every host consumer uses. Synthetic roles only.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {approvalRecordProblems,personaDigest,readPersonaContract} from '../../adapters/persona-contract.mjs';

const TEXTS={core:'【MY_PERSONA_LOAD】\n合成角色：说话简短。\n【/MY_PERSONA_LOAD】\n',voice:'合成的说话方式。',maintenance:'合成的维护约定。'};
function canon(t,texts=TEXTS,extra={}) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-persona-approval-'));
  t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const file=path.join(directory,'persona-policy.json');
  const write=(values,more={})=>fs.writeFileSync(file,JSON.stringify({schema:1,requires_owner_confirmation:true,version:'persona-v3',
    approved_source:'src_'+'a'.repeat(32),...values,...Object.fromEntries(Object.entries(values).map(([k,v])=>[k+'_sha256',personaDigest(v)])),...more}));
  write(texts,extra);
  return {file,write};
}
const record=(texts=TEXTS,version='persona-v3')=>({version,core_sha256:personaDigest(texts.core),
  voice_sha256:personaDigest(texts.voice),maintenance_sha256:personaDigest(texts.maintenance)});

test('the canon the owner approved is read as approved',t=>{
  const {file}=canon(t);
  const policy=readPersonaContract(file,record());
  assert.equal(policy.version,'persona-v3');assert.equal(policy.core,TEXTS.core);
});

test('an edit to any part of the canon is refused, even with its own hashes rewritten to match',t=>{
  for(const part of ['core','voice','maintenance']) {
    const {file,write}=canon(t);
    const edited={...TEXTS,[part]:part==='core'?TEXTS.core.replace('简短','冗长'):TEXTS[part]+'未经确认的改动。'};
    write(edited);
    assert.doesNotThrow(()=>readPersonaContract(file),'the file alone is self-consistent: it cannot prove approval');
    assert.throws(()=>readPersonaContract(file,record()),/differs from the approved record/,part);
    // Only a record the owner's confirmation produced for the new text lets it in.
    assert.equal(readPersonaContract(file,record(edited)).version,'persona-v3');
  }
});

test('a new version the record does not name is refused, and so is a canon that asks no confirmation',t=>{
  const {file,write}=canon(t);
  write(TEXTS,{version:'persona-v4'});
  assert.throws(()=>readPersonaContract(file,record()),/differs from the approved record/);
  write(TEXTS,{requires_owner_confirmation:false});
  assert.throws(()=>readPersonaContract(file,record()),/needs host review/);
  write(TEXTS,{approved_source:''});
  assert.throws(()=>readPersonaContract(file,record()),/needs host review/);
});

test('a record that leaves out any part of the canon approves nothing, not even the canon it was taken from',t=>{
  const {file,write}=canon(t);
  const {voice_sha256,maintenance_sha256,...coreOnly}=record();
  assert.ok(voice_sha256&&maintenance_sha256);
  // With only the version and the core hash, an edit to voice or maintenance would pass unseen: refused, edited or not.
  for(const part of [null,'voice','maintenance']) {
    if(part) write({...TEXTS,[part]:TEXTS[part]+'未经确认的改动。'});
    assert.throws(()=>readPersonaContract(file,coreOnly),error=>error.code==='approval-record-incomplete'&&
      /approval-record-incomplete:voice_sha256, approval-record-incomplete:maintenance_sha256/.test(error.message),part??'unchanged');
  }
  for(const gap of [{version:undefined},{version:' '},{core_sha256:'0'.repeat(63)},{voice_sha256:'X'.repeat(64)},{maintenance_sha256:null}])
    assert.throws(()=>readPersonaContract(file,{...record(),...gap}),/approval record is incomplete/,JSON.stringify(gap));
  assert.throws(()=>readPersonaContract(file,false),error=>error.code==='approval-record-missing');
  // The codes are the rehearsal's (kin_mind.deploy_checks), field by field.
  assert.deepEqual(approvalRecordProblems(coreOnly),['approval-record-incomplete:voice_sha256','approval-record-incomplete:maintenance_sha256']);
  assert.deepEqual(approvalRecordProblems(undefined),['approval-record-missing']);
  assert.deepEqual(approvalRecordProblems(record()),[]);
  // A complete record for the edited text is what lets it in.
  const edited={...TEXTS,maintenance:TEXTS.maintenance+'未经确认的改动。'};
  assert.equal(readPersonaContract(file,record(edited)).maintenance,edited.maintenance);
});
