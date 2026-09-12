import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {personaDigest, readPersonaContract, assertPersonaPrefix} from './persona-contract.mjs';

test('approved core survives channel changes and rejects silent persona drift', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'persona-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const policy = {schema: 1, version: 'synthetic-v1', approved_source: 'test-owner-request', requires_owner_confirmation: true,
    core: '【MY_PERSONA_LOAD】 SYNTHETIC_HELPER\n【/MY_PERSONA_LOAD】', voice: 'Write complete sentences.', maintenance: 'Preserve quotations.'};
  for (const name of ['core', 'voice', 'maintenance']) policy[name + '_sha256'] = personaDigest(policy[name]);
  const file = path.join(dir, 'persona-policy.json');
  fs.writeFileSync(file, JSON.stringify(policy));
  const approved = readPersonaContract(file);
  assert.doesNotThrow(() => assertPersonaPrefix(policy.core + '\n\n# SOUL\nUpdated channel rules.', approved));
  for (const prefix of ['', '\n' + policy.core, policy.core.replace('HELPER', 'BOSS')]) {
    assert.throws(() => assertPersonaPrefix(prefix + '\n# SOUL', approved), /owner approval/);
  }
  fs.writeFileSync(file, JSON.stringify({...policy, core: policy.core.replace('HELPER', 'BOSS')}));
  assert.throws(() => readPersonaContract(file), /host review/);
});
