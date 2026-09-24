import test from 'node:test';
import assert from 'node:assert/strict';
import {ALLOWED_SUPPLEMENTAL_LAYERS,NOT_COVERED,PROOF_SCOPE,layerKind} from '../../adapters/mobile-runtime-proof-runner.mjs';

test('every developer layer a native request can carry is named, and only two kinds may reach the phone',()=>{
  const cases=[['<permissions instructions>\nFilesystem sandboxing…','permissions'],['<model_switch>\nThe user was previously using…','model-switch'],
    ['<skills_instructions>\n## Skills','skills'],['<multi_agent_role>\nYou are `/root`, the root agent','multi-agent-role'],
    ['You are `/root`, the root agent in a team','multi-agent-role'],['<multi_agent_mode>\nSpawn agents when…','multi-agent-mode'],
    ['<collaboration_mode># Collaboration Mode: Default','collaboration-mode'],['<persistent_mode>…','persistent-mode'],['something new','unknown']];
  for(const [text,kind] of cases)assert.equal(layerKind(text),kind,text);
  assert.deepEqual([...ALLOWED_SUPPLEMENTAL_LAYERS],['permissions','model-switch']);
  assert.equal(ALLOWED_SUPPLEMENTAL_LAYERS.includes('unknown'),false,'an unrecognised layer fails the proof');
});

test('the proof says what it is and what it cannot show',()=>{
  assert.equal(PROOF_SCOPE,'protocol');
  for(const gap of ['live-provider-requests','provider-side-remote-compaction','tool-servers','account-credentials','host-launch-script'])
    assert.ok(NOT_COVERED.includes(gap),gap);
});
