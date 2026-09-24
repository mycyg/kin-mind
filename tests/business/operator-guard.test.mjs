import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {operatorRefusal,phoneAppServerAncestor,processTable,PHONE_SESSION_ENV} from '../../adapters/operator-guard.mjs';

const PHONE = '/Users/k/kin-wechat/state/mobile-runtime/versions/codex-0.156.1-acp-1.11.0-companion-abc/bin/codex -c model_catalog_json="/x" app-server';
const DESKTOP = '/Applications/Codex.app/Contents/Resources/codex app-server --analytics-default-enabled';
// pid 50 is the entry; its ancestors are listed from the parent up.
const tree = (...commands) => {
  const table = new Map([[50, {pid: 50, ppid: 40, command: 'node entry.mjs'}]]);
  commands.forEach((command, index) => table.set(40 - index * 10, {pid: 40 - index * 10, ppid: index === commands.length - 1 ? 1 : 30 - index * 10, command}));
  return table;
};
const refusal = options => operatorRefusal({name: 'entry.mjs', pid: 50, env: {}, argv: ['node', 'entry.mjs'], ...options});

test('an effectful entry needs --operator and runs from a terminal or the desktop Codex', () => {
  assert.match(refusal({table: tree('/bin/zsh -l', 'Terminal')}), /run it by hand with --operator/);
  assert.equal(refusal({table: tree('/bin/zsh -l', 'Terminal'), argv: ['node', 'entry.mjs', '--operator']}), null);
  assert.equal(refusal({table: tree('/bin/zsh -lc node entry.mjs', DESKTOP), argv: ['node', 'entry.mjs', '--operator']}), null,
    'the operator may work through a desktop Codex');
  assert.equal(refusal({table: tree('launchd-job'), flag: false}), null, 'automation-started entries keep only the phone refusal');
});

test('the phone session is refused by its mark or by its app-server ancestor, flag or not', () => {
  const argv = ['node', 'entry.mjs', '--operator'];
  assert.match(refusal({env: {[PHONE_SESSION_ENV]: '1'}, table: tree('/bin/zsh'), argv}), /phone session's tools/);
  assert.match(refusal({table: tree('/bin/zsh -lc node entry.mjs', PHONE, 'node codex-acp.mjs'), argv}), /phone session's app-server \(pid 30\)/);
  assert.match(refusal({table: tree('/bin/zsh', PHONE), flag: false}), /app-server/);
  assert.deepEqual(phoneAppServerAncestor({pid: 50, table: tree('sh', PHONE)}), {pid: 30, ppid: 1, command: PHONE});
  assert.equal(phoneAppServerAncestor({pid: 50, table: tree('sh', DESKTOP)}), null);
});

test('the memory server may run its own backend entry; a shell command posing as it may not', () => {
  const mcp = '/opt/kin/bin/python /Users/k/kin-wechat/kin_memory_mcp.py';
  const allowParent = /kin_memory_mcp\.py/;
  assert.equal(refusal({table: tree(mcp, PHONE), allowParent}), null);
  assert.match(refusal({table: tree(mcp, PHONE), allowParent, env: {[PHONE_SESSION_ENV]: '1'}}), /phone session's tools/);
  assert.match(refusal({table: tree('/bin/zsh', mcp, PHONE), allowParent}), /app-server/);
});

test('the process table is read from one ps listing, and a refusal exits 2 before the entry acts', t => {
  const table = processTable(() => '  1     0 /sbin/launchd\n 77     1 /bin/zsh -l\n  garbage\n');
  assert.deepEqual([...table.keys()], [1, 77]);
  assert.equal(table.get(77).command, '/bin/zsh -l');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kin-operator-guard-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const entry = path.join(dir, 'entry.mjs');
  fs.writeFileSync(entry, `import {requireOperator} from ${JSON.stringify(new URL('../../adapters/operator-guard.mjs', import.meta.url).href)};
requireOperator({name:'entry.mjs'});
console.log('acted');\n`);
  const env = {...process.env};
  delete env[PHONE_SESSION_ENV];
  const refused = spawnSync(process.execPath, [entry], {env: {...env, [PHONE_SESSION_ENV]: '1'}, encoding: 'utf8'});
  assert.equal(refused.status, 2);
  assert.match(refused.stderr, /phone session's tools/);
  assert.equal(refused.stdout, '');
  const plain = spawnSync(process.execPath, [entry], {env, encoding: 'utf8'});
  assert.equal(plain.status, 2);
  assert.match(plain.stderr, /--operator/);
});
