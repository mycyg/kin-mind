/** The contact loop's `holdDraft` (the host's contact pause, 2026-10-01): asked right before a new attempt
 * is claimed. It holds the new draft only: what is under way, resumed or reconciled goes on, nothing is
 * sent for it, and a hold that cannot answer holds nothing. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {MindLoop} from '../../adapters/owner-host.mjs';

function fixture({candidate={eligible:true},holdDraft,resume}={}) {
  const events=[];
  const loop=new MindLoop({call:async(action,request)=>{events.push(action);
      if(action==='candidate')return typeof candidate==='function'?candidate():candidate;
      if(action==='claim')return {id:'attempt-1',state:'drafting'};
      if(action==='check')return {eligible:true};
      return request;},
    eligibility:()=>({eligible:true}),ownerEpoch:()=>'owner-1',isBusy:()=>false,holdDraft,resume,
    draft:async()=>({action:'send',text:'hello'}),send:async()=>({state:'accepted',messageId:'m-1'})});
  loop.review=async()=>{};
  return {loop,events};
}

test('a held draft is never claimed: the tick waits and says why',async()=>{
  const pause={code:'mind-worker-output-limit',until:'2026-09-28T03:10:00.000Z',streak:3};
  const {loop,events}=fixture({holdDraft:async()=>pause});
  const result=await loop.tick();
  assert.deepEqual(result,{state:'waiting',reason:'contact-paused',pause});
  assert.deepEqual(events,['reconsider','candidate'],'no claim, no draft, no send');
  assert.equal(loop.contactRunning,false);
});

test('nothing held, or a hold that cannot answer: the draft goes on as before',async()=>{
  for(const holdDraft of [undefined,async()=>null,async()=>{throw Error('worker-exit');},async()=>'paused']) {
    const {loop,events}=fixture({holdDraft});
    assert.equal((await loop.tick()).state,'accepted');
    assert.ok(events.includes('claim'));
  }
});

test('a hold leaves what is under way alone: a resumed attempt and a send of unknown outcome are settled as before',async()=>{
  const resumed=[];const resume=async candidate=>{resumed.push(candidate.attempt_id);return {state:'accepted',messageId:'m-'+candidate.attempt_id};};
  const holdDraft=async()=>({code:'mind-worker-output-limit',until:'later'});
  const pending=fixture({candidate:{reason:'attempt-in-progress',state:'pending',attempt_id:'attempt-0'},holdDraft,resume});
  assert.equal((await pending.loop.tick()).state,'accepted');assert.deepEqual(resumed,['attempt-0']);
  const due=fixture({candidate:{eligible:true,reconcile:[{attempt_id:'attempt-9',next_check_at:'now'}]},holdDraft,resume});
  const result=await due.loop.tick();
  assert.deepEqual(resumed,['attempt-0','attempt-9'],'the unknown send is checked under its own id');
  assert.equal(result.reason,'contact-paused','and only the new draft is held');
  assert.equal(due.events.includes('claim'),false);
});
