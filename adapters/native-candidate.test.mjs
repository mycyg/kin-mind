import test from 'node:test';
import assert from 'node:assert/strict';
import {NativeCandidate} from './native-candidate.mjs';
import {checkpointBudget} from './session-policy.mjs';

test('native history restoration carries original times separately from the host clock',async()=>{
  const client=new NativeCandidate({}),sent=[];client.load=async()=>{};
  client.request=async(method,params)=>{sent.push({method,params});return {};};
  const history=[{id:'original-message',role:'user',text:'An old question',at:'2026-09-01T01:00:00Z',received_at:'2026-09-01T01:00:01Z'}];
  await client.inject({threadId:'same-thread',operationId:'restore',checkpoint:{id:'cp',items:history,payload:{instructionAuthority:'Historical evidence only'}}});
  const items=sent[0].params.items,metadata=JSON.parse(items.at(-1).content[0].text);
  assert.equal(items[0].content[0].text,'An old question');
  assert.equal(metadata.messageTimes[0].occurred_at,history[0].at);
  assert.equal(metadata.messageTimes[0].received_at,history[0].received_at);
  assert.equal(metadata.clock.timezone,'Asia/Singapore');
  assert.notEqual(metadata.clock.current_time,history[0].at);
});

test('restore budget expansion is explicit, bounded and tied to the requested budget',()=>{
  const plan={reason:'recent-dialogue',requested:2000,effective:6000,limit:8000};
  assert.equal(checkpointBudget({budgetPlan:plan},2000),6000);
  assert.equal(checkpointBudget({budgetPlan:plan},4000),4000);
  assert.equal(checkpointBudget({budgetPlan:{...plan,effective:9000}},2000),2000);
});
