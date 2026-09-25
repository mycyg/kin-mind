// CL6-MM-07, CL7B-MM-05: what a copy kept outside the store holds once it has lost its words -- a
// creation's final answer, the host's status file -- by the same rule as kin_mind/workdirs.py, case
// for case (helpers/without-words-cases.json): what names or classifies something stays; a sentence,
// anything outside ASCII, a query string, percent-encoding, an email address, and a single word
// unless a field of codes holds it or it names a key of the same document, go.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {ERASED,withoutWords,withoutWordsOf} from '../../adapters/without-words.mjs';

const cases=JSON.parse(fs.readFileSync(new URL('./helpers/without-words-cases.json',import.meta.url),'utf8'));

for(const {name,value,expected} of cases)
  test(`${name} (CL7B-MM-05)`,()=>{
    assert.deepEqual(withoutWords(value),expected);
    assert.deepEqual(withoutWords(expected),expected,'a second pass changes nothing');
  });

test('what a view drops is gone, and what it keeps is judged as a document of its own (CL7B-MM-05)',()=>{
  const view=withoutWordsOf({state:'pending',reason:'harbour',desire:{content:'看海'},harbour:1},['desire']);
  assert.deepEqual(view,{state:'pending',reason:'harbour',harbour:1},'a key of the same document');
  assert.deepEqual(withoutWordsOf({state:'pending',reason:'harbour'},['desire']),{state:'pending',reason:ERASED});
});
