import test from 'node:test';
import assert from 'node:assert/strict';
import {reconcileRoutingAlarm} from './routing-recovery.mjs';
const status={routingError:'input-acceptance-unconfirmed'};
const proof={id:'before',state:'failed-before-submit',reconciliation:{kind:'native-submit-not-reached',source_sha256:'synthetic-digest'}};
test('proven never-submitted input clears only the historical routing alarm',()=>{
  const result=reconcileRoutingAlarm(status,{inputs:{before:proof}});
  assert.equal(result.routingError,null);
  assert.deepEqual(result.routingRecovery.inputIds,['before']);
  assert.deepEqual(reconcileRoutingAlarm({routingError:'different-fault'},{inputs:{before:proof}}),{});
});
test('new uncertainty and missing provenance keep the alarm',()=>{
  for(const state of ['submitting','unconfirmed','preparing','selected'])
    assert.deepEqual(reconcileRoutingAlarm(status,{inputs:{before:proof,new:{state}}}),{});
  assert.deepEqual(reconcileRoutingAlarm(status,{inputs:{before:{...proof,reconciliation:{}}}}),{});
});
