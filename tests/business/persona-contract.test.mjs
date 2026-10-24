import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {assertPersonaPrefix,personaDigest,readPersonaContract} from '../../adapters/persona-contract.mjs';

const CORE='【MY_PERSONA_LOAD】\n合成人设：说话简短。\n【/MY_PERSONA_LOAD】\n';
function contract(t,change={}) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-persona-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const texts={core:CORE,voice:'自然。',maintenance:'按需维护。',...Object.fromEntries(Object.entries(change).filter(([k])=>['core','voice','maintenance'].includes(k)))};
  const policy={schema:1,requires_owner_confirmation:true,version:'v1',approved_source:'owner-confirmed',...texts,
    ...Object.fromEntries(Object.entries(texts).map(([k,v])=>[k+'_sha256',personaDigest(v)])),...change};
  const file=path.join(directory,'persona-policy.json');fs.writeFileSync(file,JSON.stringify(policy));return file;
}

test('a persona seed preserves arbitrary complete text without marker or length gates',t=>{
  for(const change of [{requires_owner_confirmation:false},{approved_source:''},{core:'没有标记的人格'},{voice:'自然地说话。'.repeat(4000)}]) {
    const value=readPersonaContract(contract(t,change));for(const part of ['core','voice','maintenance'])assert.equal(typeof value[part],'string');
    if(change.core)assert.equal(value.core,change.core);if(change.voice)assert.equal(value.voice,change.voice);
  }
});
test('the compatibility prefix entry no longer freezes an editable developer layer',()=>{
  for(const text of ['新的完整指令。','没有 SOUL 标记。',''])assert.doesNotThrow(()=>assertPersonaPrefix(text));
  assert.throws(()=>assertPersonaPrefix(null),/must be text/);
});
