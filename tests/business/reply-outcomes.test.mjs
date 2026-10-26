import test from 'node:test';
import assert from 'node:assert/strict';
import {replyOutcomeFacts,readReplyOutcomeFacts} from '../../adapters/reply-outcomes.mjs';
const at=10000;
const group=(state,reason=null)=>({group_id:'g',reply_id:'owner',answered_input_ids:['owner','steered'],updated_at:at,reason,bubbles:[{state,fragments:[]}]});
test('delivery context distinguishes generation, format, explicit rejection and unknown sends without carrying prose',()=>{
  const inputs={generation:{id:'generation',nativeFailure:{at},text:'PRIVATE INPUT'}};
  const format=group('canceled','private-note-envelope-incomplete');format.bubbles[0].text='PRIVATE NOTE';
  const rejected={...group('rejected'),group_id:'rejected',reply_id:'r',answered_input_ids:[]};
  const unknown={...group('unknown'),group_id:'unknown',reply_id:'u',answered_input_ids:[],bubbles:[{state:'accepted',fragments:[{state:'accepted'}]},{state:'unknown',fragments:[{state:'unknown'}]}]};
  const facts=replyOutcomeFacts(inputs,{format,rejected,unknown},{now:at});
  assert.deepEqual(facts.map(f=>f.state),['format-failed','send-failed','send-unknown','generation-failed']);
  assert.deepEqual(facts[0].inputIds,['owner','steered']);assert.equal(facts[2].sentBubbles,1);assert.equal(facts[2].unknownFragments,1);
  assert.doesNotMatch(JSON.stringify(facts),/PRIVATE/);
  assert.deepEqual(replyOutcomeFacts(inputs,{format},{now:at+86400001}),[]);
});
test('an unknown fragment is looked up by its existing group before context or notice selection, never resent',async()=>{
  const g=group('unknown');g.bubbles[0].fragments=[{transport_id:'original-fragment',state:'unknown'}];const calls=[];
  const manifests={status:()=>({groups:{g}}),reconcileGroup:async id=>{calls.push(id);g.bubbles[0].state='accepted';g.bubbles[0].fragments[0].state='accepted';}};
  assert.deepEqual(await readReplyOutcomeFacts({},manifests,{now:at}),[]);assert.deepEqual(calls,['g']);
});
test('an inbound turn hears only unsettled delivered groups, and unknown receipts are looked up for a day at a bounded rate',async()=>{
  const accepted={...group('accepted'),group_id:'done',state:'accepted'},partial={...group('accepted'),group_id:'partial',state:'pending',bubbles:[{state:'accepted',fragments:[]},{state:'unsent',fragments:[]}]};
  assert.deepEqual(replyOutcomeFacts({},{done:accepted,partial},{now:at,includeAccepted:true}).map(f=>f.groupId),['partial']);
  const fresh=group('unknown'),stale={...group('unknown'),group_id:'stale',updated_at:at-86400001};
  for(const g of [fresh,stale])g.bubbles[0].fragments=[{transport_id:'synthetic-'+g.group_id,state:'unknown'}];
  const calls=[],manifests={status:()=>({groups:{g:fresh,stale}}),reconcileGroup:async id=>{calls.push(id);}};
  await readReplyOutcomeFacts({},manifests,{now:at});await readReplyOutcomeFacts({},manifests,{now:at+60000});
  assert.deepEqual(calls,['g'],'only the recent group, and not again within the interval');
  await readReplyOutcomeFacts({},manifests,{now:at+5*60000});assert.deepEqual(calls,['g','g']);
});
