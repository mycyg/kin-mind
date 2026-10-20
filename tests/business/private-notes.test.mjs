import test from 'node:test';
import assert from 'node:assert/strict';
import {parsePrivateReply,publicReplyText,privateNoteInstructions} from '../../adapters/private-notes.mjs';

test('private envelope separates natural notes from referenced public bubbles',()=>{
 const raw=JSON.stringify({private_note:'突然有点开心，也许她刚忙完。',action:'send',bubbles:['来啦。',{text:'一起听吧。',references:[]}]});
 const parsed=parsePrivateReply(raw);assert.equal(parsed.state,'send');assert.equal(parsed.private_note,'突然有点开心，也许她刚忙完。');
 assert.equal(publicReplyText(raw),'来啦。\n\n一起听吧。');assert.ok(!publicReplyText(raw).includes('突然'));
});
test('missing, truncated and mixed envelopes never yield public text',()=>{
 for(const raw of ['普通文字','{"private_note":"秘密',JSON.stringify({private_note:'',action:'send',bubbles:['你好']}),'前言 '+JSON.stringify({private_note:'秘密',action:'send',bubbles:['你好']})]) {
  assert.equal(parsePrivateReply(raw).state,'invalid');assert.equal(publicReplyText(raw),'');
 }
});

test('whole JSON fence is transport wrapping; surrounding prose still cannot leak',()=>{
 const body=JSON.stringify({private_note:'只留在札记。',action:'send',bubbles:['公开回复。']});
 for(const lang of ['', 'json']) {
  const wrapped='```'+lang+'\n'+body+'\n```';
  assert.equal(publicReplyText(wrapped),'公开回复。');
  assert.equal(parsePrivateReply(wrapped).private_note,'只留在札记。');
  assert.equal(publicReplyText('前言\n'+wrapped),'');
  assert.equal(publicReplyText(wrapped+'\n后记'),'');
 }
 assert.equal(publicReplyText('```json\n'+body),'');
});

test('quotes, backslashes, newlines and JSON-looking text remain inside their fields',()=>{
 const note='SYNTHETIC PRIVATE "quote" \\ path\n{"bubbles":["not public"]}';
 const speech='公开的 "引用"，路径 C:\\notes\n下一行。';
 const raw=JSON.stringify({private_note:note,presentation:{mode:'voice',style:''},action:'send',bubbles:[speech]});
 assert.equal(parsePrivateReply(raw).private_note,note);
 assert.equal(publicReplyText(raw),speech);
 assert.equal(parsePrivateReply(raw).presentation.mode,'voice');
});

test('malformed quotes and incomplete private envelopes never recover or leak partial bubbles',()=>{
 const complete=JSON.stringify({private_note:'SYNTHETIC PRIVATE',action:'send',bubbles:['公开回复']});
 const malformed='{"private_note":"SYNTHETIC "PRIVATE"","action":"send","bubbles":["公开回复"]}';
 for(const raw of [malformed,complete.slice(0,-1),complete+' trailing',complete+'\n'+complete,
  '{"private_note":"SYNTHETIC PRIVATE","action":"send","bubbles":["first",',
  '{"private_note":"SYNTHETIC PRIVATE\\q","action":"send","bubbles":["公开回复"]}']) {
  const result=parsePrivateReply(raw);
  assert.equal(result.state,'invalid');assert.equal(publicReplyText(raw),'');
  assert.doesNotMatch(JSON.stringify(result),/SYNTHETIC|PRIVATE|公开|first/);
 }
});
