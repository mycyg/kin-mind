import test from 'node:test';
import assert from 'node:assert/strict';
import {parseContactDraft} from './contact-draft.mjs';
test('uses a structured final decision after ordinary commentary',()=>{
 assert.deepEqual(parseContactDraft(['checking','```json\n{"action":"send","text":"Hello"}\n```']),{action:'send',text:'Hello'});
 assert.equal(parseContactDraft(['{"action":"abandon","reason":"Already shared"}']).action,'abandon');
});
test('wait carries a bounded resume condition',()=>{
 assert.equal(parseContactDraft(['{"action":"wait","condition":"time","reason":"Brief delay","retry_after_seconds":600}']).retry_after_seconds,600);
 assert.equal(parseContactDraft(['{"text":null}']).condition,'new_evidence');
});
test('malformed output never becomes a semantic empty decision',()=>{
 for(const value of ['','not json','{"text":""}','{"action":"wait"}','{"action":"wait","condition":"time","reason":"x","retry_after_seconds":1}','{"action":"wait","condition":"invalid","reason":"x"}'])
  assert.throws(()=>parseContactDraft([value]),/invalid-result/);
 assert.throws(()=>parseContactDraft(['{"action":"send","text":"Stale draft"}','invalid final']),/invalid-result/);
});
