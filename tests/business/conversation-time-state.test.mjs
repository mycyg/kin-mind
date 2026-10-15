import test from 'node:test';
import assert from 'node:assert/strict';
import {conversationClock,STATE_TIME_GUIDANCE} from '../../adapters/conversation-time.mjs';

test('elapsed context keeps the original statement time distinct from current time',()=>{
 const clock=conversationClock({occurredAt:'2026-10-15T13:00:00Z',previousOwnerAt:'2026-10-15T08:00:00Z'},'2026-10-15T14:00:00Z');
 assert.equal(clock.local_time,'2026-10-15T22:00:00.000+08:00');
 assert.equal(clock.input_age_seconds,3600);
 assert.equal(clock.seconds_since_previous_owner_message,21600);
 assert.equal(clock.occurred_at,'2026-10-15T13:00:00.000Z');
 assert.equal(clock.state_inference_guidance,STATE_TIME_GUIDANCE);
 assert.equal('inferred_state' in clock,false);
});

test('missing or future event times do not manufacture a duration or activity conclusion',()=>{
 const clock=conversationClock({occurredAt:'2026-10-16T13:00:00Z'},'2026-10-15T14:00:00Z');
 assert.equal(clock.input_age_seconds,null);
 assert.equal(clock.previous_owner_message_at,null);
 assert.equal(clock.seconds_since_previous_owner_message,null);
});
