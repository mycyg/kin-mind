import test from 'node:test';
import assert from 'node:assert/strict';
import {safeBoundary,safeReadOnlyPreparation} from './session-policy.mjs';

const idle={known:true,active:false,nativeStatus:'idle',backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};

// KIN-ITER-20260918-03: read-only roster preparation is split from the boundary
// that compaction, injection and segment swaps must still pass.
test('read-only preparation proceeds on unconfirmed notifications, annotated; the strict boundary still refuses',()=>{
  const notices=[{id:'n1',kind:'model-switched',state:'unconfirmed'},{id:'n2',kind:'status',state:'accepted'}];
  assert.deepEqual(safeBoundary({runtime:idle,notices}),{safe:false,reason:'notification-unconfirmed'},'the strict default is unchanged');
  const preparation=safeReadOnlyPreparation({runtime:idle,notices});
  assert.equal(preparation.safe,true);assert.equal(preparation.readOnly,true);
  assert.deepEqual(preparation.unconfirmedDeliveries,[{id:'n1',kind:'model-switched',state:'unconfirmed'}],
    'every delivery the owner never confirmed receiving is named on the snapshot');
  // Deliveries that were refused or gave up were never confirmed either.
  assert.deepEqual(safeReadOnlyPreparation({runtime:idle,notices:[{id:'n3',kind:'status',state:'failed'},{id:'n4',kind:'status',state:'unresolved'}]}).unconfirmedDeliveries.map(n=>n.id),['n3','n4']);
  // Settled and never-sent notices need no annotation.
  assert.equal(safeReadOnlyPreparation({runtime:idle,notices:[{id:'n2',kind:'status',state:'accepted'},{id:'n5',kind:'model-switched',state:'suppressed'}]}).unconfirmedDeliveries,undefined);
});

test('read-only preparation keeps every other protection of the strict boundary',()=>{
  assert.deepEqual(safeReadOnlyPreparation({runtime:{...idle,active:true}}),{safe:false,reason:'native-or-delivery-busy'});
  assert.deepEqual(safeReadOnlyPreparation({runtime:{...idle,known:false}}),{safe:false,reason:'native-or-delivery-busy'});
  assert.equal(safeReadOnlyPreparation({runtime:idle,contactRunning:true}).safe,false);
  assert.equal(safeReadOnlyPreparation({runtime:idle,inputs:[{id:'i',state:'unconfirmed'}]}).reason,'input-awaiting-dispatch-or-reconciliation');
  assert.equal(safeReadOnlyPreparation({runtime:idle,inputs:[{id:'i',state:'selected'}]}).reason,'input-awaiting-dispatch-or-reconciliation');
  assert.equal(safeReadOnlyPreparation({runtime:idle,tasks:[{tools:{t:{status:'running'}}}]}).reason,'unfinished-tool');
  assert.deepEqual(safeReadOnlyPreparation({runtime:idle}),{safe:true,readOnly:true});
});
