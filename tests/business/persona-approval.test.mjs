// The hard rule: a change to the persona canon needs 小光's own confirmation. The canon file checks
// only itself (its hashes sit beside its texts, so whoever edits the texts can rewrite them); the
// owner's approval is the record the host keeps outside it (mind-config `persona_contract`). These
// cases pin that record's check in the core reader every host consumer uses. Synthetic roles only.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {personaDigest,readPersonaContract} from '../../adapters/persona-contract.mjs';

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

test('all persona parts can change without an approval record',t=>{
  for(const part of ['core','voice','maintenance']) {
    const {file,write}=canon(t);const changed={...TEXTS,[part]:'KIN 自己改写了这一段。'};write(changed);
    for(const historical of [undefined,null,{},record()])assert.equal(readPersonaContract(file,historical)[part],changed[part]);
  }
});
test('historical marker, source and digest metadata never freezes the current persona',t=>{
  const {file,write}=canon(t);write({...TEXTS,core:'我有自己的想法，也记得小光和一一。'},{requires_owner_confirmation:false,approved_source:'',core_sha256:'old',version:'next'});
  const value=readPersonaContract(file);assert.equal(value.core,'我有自己的想法，也记得小光和一一。');assert.equal(value.core_sha256,personaDigest(value.core));
});
test('malformed JSON or a nontext persona is reported rather than silently loading a stale copy',t=>{
  const {file}=canon(t);fs.writeFileSync(file,'{');assert.throws(()=>readPersonaContract(file),SyntaxError);
  fs.writeFileSync(file,JSON.stringify({...TEXTS,core:null}));assert.throws(()=>readPersonaContract(file),/must be text/);
});
