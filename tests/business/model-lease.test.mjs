import test from 'node:test';
import assert from 'node:assert/strict';
import {createLeaseClient} from '../../adapters/model-lease.mjs';

test('a degraded answer is released by its id; an informed no releases nothing (AD2-28)',async()=>{
  const calls=[];let answer;
  const client=createLeaseClient({request:async(op,body)=>{calls.push([op,body.id]);if(op==='model-leases/acquire'){if(answer instanceof Error)throw answer;return answer;}return {state:'released'};}});
  answer=Error('socket hang up');
  const degraded=await client.acquire({lane:'background',purpose:'probe',id:'lease-1'});
  assert.equal(degraded.state,'degraded');
  await degraded.release();await degraded.release();
  assert.deepEqual(calls.filter(([op])=>op==='model-leases/release'),[['model-leases/release','lease-1']]);
  calls.length=0;answer={state:'wait',retry_after_seconds:5};
  const refused=await client.acquire({lane:'background',purpose:'probe',id:'lease-2'});
  await refused.release();
  assert.deepEqual(calls.filter(([op])=>op==='model-leases/release'),[]);
});
