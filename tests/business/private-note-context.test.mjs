import test from 'node:test';
import assert from 'node:assert/strict';
import {privateNoteContext} from '../../adapters/private-note-context.mjs';
test('automatic notes keep continuity and uncertain delivery while complete read stays intact',()=>{
  const recent={notice:'role notes, not facts',notes:[{id:'n1',at:'2026-10-21',text:'subjective continuation',basis:'role_note',visibility:'kin_only',evidence_ids:['src1'],corrects:'n0',
    model:'fixture',provider:'fixture-provider',session:'s1',turn_id:'t1',reply_text:'full old public reply',later_notes:[{id:'n2',text:'nested repeated note'}],delivery:[{state:'unknown',channel:'feishu',bubbles:[{text:'old reply again'}]}]}]};
  const saved=structuredClone(recent),projected=privateNoteContext(recent);
  assert.deepEqual(recent,saved);assert.equal(projected.notes[0].text,recent.notes[0].text);assert.equal(projected.notes[0].corrects,'n0');
  assert.deepEqual(projected.notes[0].evidence_ids,['src1']);assert.deepEqual(projected.notes[0].later_note_ids,['n2']);
  for(const key of ['model','provider','session','turn_id'])assert.equal(projected.notes[0][key],recent.notes[0][key]);
  assert.deepEqual(projected.notes[0].delivery,[{state:'unknown',channel:'feishu'}]);assert.ok(!JSON.stringify(projected).includes('full old public reply'));
});
