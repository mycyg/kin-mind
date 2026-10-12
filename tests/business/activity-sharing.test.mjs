import test from 'node:test';
import assert from 'node:assert/strict';
import {parseContactDraft} from '../../adapters/contact-draft.mjs';

test('a subjective wait needs neither a cause nor an exact time',()=>{
  assert.deepEqual(parseContactDraft('{"action":"wait"}'),{action:'wait',reason:'',condition:'review'});
  assert.deepEqual(parseContactDraft('{"action":"abandon","reason":""}'),{action:'abandon',reason:''});
});

test('a dream draft carries its original activity pointer intact',()=>{
  const activity_ref={kind:'dream',id:'source-original',source_id:'source-original',version:1,occurred_at:'2026-10-12T00:00:00Z',content_class:'dream_imagination'};
  const result=parseContactDraft(JSON.stringify({action:'send',bubbles:[{text:'梦里有扇蓝门',references:[{activity_ref,mode:'new',reason:''}]}]}));
  assert.deepEqual(result.references,[[{activity_ref,mode:'new',reason:''}]]);
});
