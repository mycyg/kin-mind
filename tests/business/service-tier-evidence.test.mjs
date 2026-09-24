import test from 'node:test';
import assert from 'node:assert/strict';
import {FAST_SERVICE_TIERS,preferenceTier,runtimeProfile,serviceTierVerified} from '../../adapters/codex-models.mjs';
import {candidateResponseProfileEvidence} from '../../adapters/native-candidate.mjs';

test('a service tier is verified only when the runtime explicitly says so', () => {
  assert.equal(runtimeProfile({serviceTier: 'priority', fastMode: 'on'}).serviceTierVerified, false);
  assert.equal(runtimeProfile({serviceTier: null, serviceTierVerified: false, fastMode: 'on'}).serviceTierVerified, false);
  assert.equal(runtimeProfile({serviceTier: 'priority', serviceTierVerified: true}).serviceTierVerified, true);
  assert.equal(serviceTierVerified({serviceTierVerified: true}), false, 'no tier, nothing to verify');
  assert.equal(runtimeProfile({fastMode: 'on'}).serviceTierPreference, 'fast', 'the preference stays a preference');
});

test('a Fast thread reported as priority is the configuration that was asked for', () => {
  for (const tier of FAST_SERVICE_TIERS) assert.equal(preferenceTier(tier), 'fast');
  const launch = fastMode => ({fastMode, modelProvider: 'openai-15m', profile: {model: 'gpt-6-sol', reasoningEffort: 'medium'}});
  const actual = serviceTier => ({model: 'gpt-6-sol', modelProvider: 'openai-15m', reasoningEffort: 'medium', serviceTier});
  assert.equal(candidateResponseProfileEvidence(actual('priority'), launch('on')).serviceTierConfiguration, 'verified');
  assert.equal(candidateResponseProfileEvidence(actual('fast'), launch('on')).serviceTierConfiguration, 'verified');
  assert.equal(candidateResponseProfileEvidence(actual(null), launch('off')).serviceTierConfiguration, 'verified');
  assert.equal(candidateResponseProfileEvidence(actual('default'), launch('off')).serviceTierConfiguration, 'verified');
  assert.equal(candidateResponseProfileEvidence(actual('priority'), launch('off')).serviceTierConfiguration, 'mismatch');
  assert.equal(candidateResponseProfileEvidence(actual(null), launch('on')).serviceTierConfiguration, 'mismatch');
  const {serviceTier, ...unknown} = actual('fast');
  assert.equal(candidateResponseProfileEvidence(unknown, launch('on')).serviceTierConfiguration, 'unknown');
});
