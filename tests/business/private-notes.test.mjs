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
