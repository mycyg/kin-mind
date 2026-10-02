import test from 'node:test';
import assert from 'node:assert/strict';
import {safeBoundary,safeReadOnlyPreparation} from '../../adapters/session-policy.mjs';

test('idle runtime can prepare recovery facts while unresolved tool outcomes remain protected',()=>{
  const runtime={known:true,active:false,nativeStatus:'idle',backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const tasks=[{id:'task',tools:{old:{status:'in_progress'}}}];
  assert.deepEqual(safeBoundary({runtime,tasks}),{safe:false,reason:'unfinished-tool'});
  const prepared=safeReadOnlyPreparation({runtime,tasks});
  assert.equal(prepared.safe,true);
  assert.deepEqual(prepared.unresolvedTools,[{taskId:'task',id:'old',status:'in_progress'}]);
  assert.equal(tasks[0].tools.old.status,'in_progress');
  for(const change of [{known:false},{active:true},{backgroundTasks:1},{queued:1},{pendingDeliveries:1}])
    assert.equal(safeReadOnlyPreparation({runtime:{...runtime,...change},tasks}).safe,false);
  assert.equal(safeReadOnlyPreparation({runtime,tasks,inputs:[{state:'unconfirmed'}]}).safe,false);
});
