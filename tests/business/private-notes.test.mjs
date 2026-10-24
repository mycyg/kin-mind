import test from 'node:test';
import assert from 'node:assert/strict';
import {parsePrivateReply,publicReplyText,privateNoteInstructions} from '../../adapters/private-notes.mjs';

test('private envelope separates natural notes from referenced public bubbles',()=>{
 const raw=JSON.stringify({private_note:'突然有点开心，也许她刚忙完。',action:'send',bubbles:['来啦。',{text:'一起听吧。',references:[]}]});
 const parsed=parsePrivateReply(raw);assert.equal(parsed.state,'send');assert.equal(parsed.private_note,'突然有点开心，也许她刚忙完。');
 assert.equal(publicReplyText(raw),'来啦。\n\n一起听吧。');assert.ok(!publicReplyText(raw).includes('突然'));
});
test('truncated and mixed envelopes never yield public text',()=>{
 for(const raw of ['普通文字','{"private_note":"秘密','前言 '+JSON.stringify({private_note:'秘密',action:'send',bubbles:['你好']})]) {
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


test('legacy private note is optional and a voice bubble without references keeps the entire reply',()=>{
 for(const private_note of [undefined,null,'','只留在心里。']) {
  const raw=JSON.stringify({private_note,presentation:{mode:'text',style:''},action:'send',bubbles:['第一句。','第二句。',{presentation:{mode:'voice',style:''},text:'小光，莫慌嘛。'}]});
  const parsed=parsePrivateReply(raw);assert.equal(parsed.state,'send');
  assert.deepEqual(parsed.bubbles,['第一句。','第二句。','小光，莫慌嘛。']);
  assert.deepEqual(parsed.references,[undefined,undefined,[]]);
  assert.deepEqual(parsed.presentations,[null,null,{mode:'voice',style:''}]);
  assert.doesNotMatch(publicReplyText(raw),/只留在心里/);
 }
 for(const references of ['bad',{},42])assert.equal(parsePrivateReply(JSON.stringify({action:'send',bubbles:[{text:'公开正文',references}]})).state,'invalid');
 assert.deepEqual(parsePrivateReply(JSON.stringify({action:'silent',bubbles:[]})),{state:'silent'});
});


test('null optional ownership and bubble identity use the same absent-field defaults',()=>{
 const parsed=parsePrivateReply(JSON.stringify({private_note:null,input_ids:null,silent_input_ids:null,presentation:null,action:'send',bubbles:[{text:'公开正文',bubble_id:null,references:null,presentation:null}]}));
 assert.equal(parsed.state,'send');assert.deepEqual(parsed.references,[[]]);assert.deepEqual(parsed.reference_bubble_ids,[null]);
 assert.equal(parsed.input_ids,undefined);assert.equal(parsed.silent_input_ids,undefined);
});
