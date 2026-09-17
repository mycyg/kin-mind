import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {writeJsonAtomic,createJsonExclusive,readJsonFile,loadJson,quarantineFile} from './atomic-json.mjs';

const temp=t=>{const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-atomic-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return dir;};

test('a write keeps the replaced revision as .prev and leaves no temporary file behind',t=>{
  const dir=temp(t),file=path.join(dir,'state','value.json');
  writeJsonAtomic(file,{revision:1});
  assert.equal(fs.existsSync(file+'.prev'),false);
  writeJsonAtomic(file,{revision:2});writeJsonAtomic(file,{revision:3});
  assert.deepEqual(readJsonFile(file).value,{revision:3});
  assert.deepEqual(readJsonFile(file+'.prev').value,{revision:2});
  assert.deepEqual(fs.readdirSync(path.dirname(file)).sort(),['value.json','value.json.prev']);
  assert.equal(fs.statSync(file).mode&0o777,0o600);
});

test('an unreadable current file never replaces a good .prev',t=>{
  const dir=temp(t),file=path.join(dir,'value.json');
  writeJsonAtomic(file,{revision:1});writeJsonAtomic(file,{revision:2});
  fs.writeFileSync(file,'{"revision":');
  writeJsonAtomic(file,{revision:3});
  assert.deepEqual(readJsonFile(file+'.prev').value,{revision:1});
  assert.deepEqual(readJsonFile(file).value,{revision:3});
});

test('loading falls back to .prev, quarantines the bad file and keeps other files loadable',t=>{
  const dir=temp(t),quarantine=path.join(dir,'quarantine'),broken=path.join(dir,'broken.json'),healthy=path.join(dir,'healthy.json'),lost=path.join(dir,'lost.json');
  writeJsonAtomic(broken,{revision:1});writeJsonAtomic(broken,{revision:2});fs.writeFileSync(broken,'not json');
  writeJsonAtomic(healthy,{ok:true});
  fs.writeFileSync(lost,'{');fs.writeFileSync(lost+'.prev','[');
  const recovered=loadJson(broken,{quarantine});
  assert.deepEqual([recovered.value,recovered.source,recovered.quarantined.length],[{revision:1},'previous',1]);
  assert.equal(fs.readFileSync(recovered.quarantined[0],'utf8'),'not json');
  assert.deepEqual(loadJson(healthy,{quarantine}).value,{ok:true});
  const gone=loadJson(lost,{quarantine});
  assert.deepEqual([gone.value,gone.source,gone.quarantined.length],[undefined,'none',2]);
  assert.equal(fs.existsSync(lost),false);
  assert.equal(loadJson(path.join(dir,'absent.json'),{quarantine}).source,'none');
  assert.equal(loadJson(healthy,{validate:value=>value.schema===1}).source,'none');
});

test('exclusive creation has one winner and leaves the first contents alone',t=>{
  const dir=temp(t),file=path.join(dir,'created.json');
  assert.equal(createJsonExclusive(file,{owner:'first'}),true);
  assert.equal(createJsonExclusive(file,{owner:'second'}),false);
  assert.deepEqual(readJsonFile(file).value,{owner:'first'});
  assert.deepEqual(fs.readdirSync(dir),['created.json']);
  assert.equal(quarantineFile(path.join(dir,'never-there.json'),path.join(dir,'quarantine')),null);
});
