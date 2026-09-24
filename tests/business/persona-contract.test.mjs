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

test('only an owner-confirmed contract whose texts match their digests is read',t=>{
  assert.equal(readPersonaContract(contract(t)).core,CORE);
  for(const change of [{core_sha256:personaDigest('another core')},{requires_owner_confirmation:false},{approved_source:''},
    {core:'没有标记的人设'},{voice:'x'.repeat(16001)},{schema:2}])
    assert.throws(()=>readPersonaContract(contract(t,change)),/needs host review/,JSON.stringify(change).slice(0,60));
  const broken=contract(t);fs.writeFileSync(broken,'{');
  assert.throws(()=>readPersonaContract(broken),/needs host review/);
});

test('instructions must start with the approved core, ahead of everything else',t=>{
  const policy=readPersonaContract(contract(t));
  assertPersonaPrefix(CORE.trimEnd()+'\n\n# SOUL\n合成的其余指令',policy);
  for(const instructions of [CORE.replace('简短','冗长').trimEnd()+'\n# SOUL\n','# SOUL\n'+CORE,CORE.trimEnd()+'\n没有 SOUL 段',''])
    assert.throws(()=>assertPersonaPrefix(instructions,policy),/explicit owner approval/);
});
