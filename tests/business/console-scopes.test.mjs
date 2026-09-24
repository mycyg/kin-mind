// The console's scope picker reads every page of `list_scopes`, following the cursor the service
// returns, and can go on from where it stopped (CR-MEM-13). The service is a stand-in that pages
// like `/v1/scopes`: in key order, the cursor being the last key shown.
import test from 'node:test';
import assert from 'node:assert/strict';
import {listScopes} from '../../console/src/scopes.mjs';

function service(count) {
  const keys = Array.from({length: count}, (_, i) => `scope-${String(i).padStart(4, '0')}`);
  const asked = [];
  const call = async ({limit, cursor}) => {
    asked.push({limit, cursor: cursor ?? null});
    const start = cursor ? keys.indexOf(cursor) + 1 : 0;
    const page = keys.slice(start, start + limit);
    const more = start + limit < keys.length;
    return {items: page.map(key => ({project: key, persona: 'Kin', collection: 'default', world: 'real'})),
            cursor: more ? page.at(-1) : null};
  };
  return {keys, asked, call};
}

test('every page is read, not only the first', async () => {
  const {keys, asked, call} = service(450);
  const found = await listScopes(call);
  assert.deepEqual(found.items.map(s => s.project), keys);
  assert.equal(found.cursor, null);
  assert.deepEqual(asked, [{limit: 200, cursor: null}, {limit: 200, cursor: 'scope-0199'}, {limit: 200, cursor: 'scope-0399'}]);
});

test('a list longer than the pages read at once keeps its cursor and goes on from it', async () => {
  const {keys, call} = service(450);
  const first = await listScopes(call, {pages: 1});
  assert.equal(first.items.length, 200);
  assert.equal(first.cursor, 'scope-0199');
  const rest = await listScopes(call, {cursor: first.cursor, items: first.items});
  assert.deepEqual(rest.items.map(s => s.project), keys);
  assert.equal(rest.cursor, null);
});

test('an empty store lists nothing and asks once', async () => {
  const {asked, call} = service(0);
  assert.deepEqual(await listScopes(call), {items: [], cursor: null});
  assert.equal(asked.length, 1);
});
