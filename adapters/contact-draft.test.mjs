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
test('a decision split across output segments arrives whole and keeps every character',()=>{
 const bubbles=[Array.from({length:100},(_,index)=>`Sentence ${index+1} of a long synthetic note. `).join('').trim(),'A closing paragraph.'];
 const payload=JSON.stringify({action:'send',bubbles:bubbles.map(text=>({text,references:[]}))});
 const segments=[payload.slice(0,7),payload.slice(7,120),payload.slice(120,2000),payload.slice(2000)];
 const parsed=parseContactDraft(segments);
 assert.deepEqual(parsed.bubbles,bubbles);
 assert.ok(parsed.text.length>3000);
 const plain=JSON.stringify({action:'send',text:bubbles.join('\n\n')});
 assert.equal(parseContactDraft([plain.slice(0,40),plain.slice(40)]).text,bubbles.join('\n\n'));
 assert.equal(parseContactDraft(['commentary first','```json\n'+plain.slice(0,15),plain.slice(15)+'\n```']).text.length,bubbles.join('\n\n').length);
 assert.equal(parseContactDraft(['a note with an unclosed { brace','{"action":"send","text":"Hello"}']).text,'Hello');
});

test('malformed output never becomes a semantic empty decision',()=>{
 for(const value of ['','not json','{"text":""}','{"action":"wait"}','{"action":"wait","condition":"time","reason":"x","retry_after_seconds":1}','{"action":"wait","condition":"invalid","reason":"x"}'])
  assert.throws(()=>parseContactDraft([value]),/invalid-result/);
 assert.throws(()=>parseContactDraft(['{"action":"send","text":"Stale draft"}','invalid final']),/invalid-result/);
});
