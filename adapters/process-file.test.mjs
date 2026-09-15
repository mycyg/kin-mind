import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {claimProcessFile,releaseProcessFile} from './process-file.mjs';
test('a duplicate starter cannot replace or remove the active service PID',t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-pid-')),file=path.join(dir,'pid');t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const owner=claimProcessFile(file,{pid:111,isAlive:()=>true});
  assert.equal(claimProcessFile(file,{pid:222,isAlive:()=>true}),null);
  releaseProcessFile(file,{pid:222});assert.equal(fs.readFileSync(file,'utf8'),'111');
  releaseProcessFile(file,owner);assert.ok(!fs.existsSync(file));
});
test('a stale owner can be recovered while a partial concurrent claim is preserved',t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-pid-')),file=path.join(dir,'pid');t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  fs.writeFileSync(file,'333');assert.equal(claimProcessFile(file,{pid:444,isAlive:()=>false}).pid,444);
  fs.writeFileSync(file,'');assert.equal(claimProcessFile(file,{pid:555,isAlive:()=>false}),null);
});
