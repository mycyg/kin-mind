import test from 'node:test';
import assert from 'node:assert/strict';
import {conversationClock,timestamp,timeContext} from '../adapters/conversation-time.mjs';
test('historical event, receipt and current clock stay distinct across midnight',()=>{
  const c=conversationClock({id:'original',kind:'owner',at:'2026-09-15T15:30:00Z',receivedAt:'2026-09-15T15:31:00Z'},'2026-09-16T01:00:00Z');
  assert.equal(c.local_time,'2026-09-16T09:00:00.000+08:00');
  assert.equal(c.occurred_at,'2026-09-15T15:30:00.000Z');
  assert.equal(c.received_at,'2026-09-15T15:31:00.000Z');
  assert.equal(c.input_id,'original');
});
test('proactive drafts receive fresh host time without posing as owner input',()=>{
  const c=conversationClock({id:'draft',kind:'proactive'},'2026-09-16T01:00:00Z');
  assert.equal(c.occurred_at,null);
  assert.equal(c.input_kind,'proactive');
  assert.match(timeContext({kind:'proactive'},'2026-09-16T01:00:00Z'),/Asia\/Singapore/);
  assert.equal(timestamp('missing'),null);
  assert.equal(timestamp('2026-09-16T09:00:00'),null);
  assert.equal(timestamp(1700000000),timestamp(1700000000000));
});
