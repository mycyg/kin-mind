import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {claimProcessFile,releaseProcessFile} from './process-file.mjs';

const temp=t=>{const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-pid-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return dir;};
// Made-up processes throughout: which numbers this machine happens to be using
// must never decide whether these tests pass.
const table=processes=>pid=>processes[pid]??{alive:false,known:true,started:null,command:null};
const running=started=>({alive:true,known:true,started,command:'/bin/node long-running-host.mjs'});
const nobody=table({});

test('a duplicate starter cannot replace or remove the active service PID',t=>{
  const dir=temp(t),file=path.join(dir,'pid');
  const owner=claimProcessFile(file,{pid:111,isAlive:()=>true,probe:nobody});
  assert.equal(claimProcessFile(file,{pid:222,isAlive:()=>true,probe:nobody}),null);
  releaseProcessFile(file,{pid:222});assert.equal(fs.readFileSync(file,'utf8'),'111');
  releaseProcessFile(file,owner);assert.ok(!fs.existsSync(file));
  // The identity is kept beside the number and goes back with it.
  assert.deepEqual(fs.readdirSync(dir),[]);
});

test('a stale owner can be recovered while a partial concurrent claim is preserved',t=>{
  const dir=temp(t),file=path.join(dir,'pid');
  fs.writeFileSync(file,'333');assert.equal(claimProcessFile(file,{pid:444,isAlive:()=>false,probe:nobody}).pid,444);
  fs.writeFileSync(file,'');assert.equal(claimProcessFile(file,{pid:555,isAlive:()=>false,probe:nobody}),null);
});

test('a service PID handed out again is recognised rather than waited for',t=>{
  const dir=temp(t),file=path.join(dir,'pid');
  const first=claimProcessFile(file,{pid:4242,probe:table({4242:running('Thu Sep 17 08:00:00 2026')})});
  assert.equal(first.holder.started,'Thu Sep 17 08:00:00 2026');
  // The number is alive, so the coarse check says the service is running. It is
  // not: the process wearing that number now started at a different moment.
  const reused=table({4242:running('Fri Sep 18 11:00:00 2026')});
  assert.equal(claimProcessFile(file,{pid:5555,isAlive:()=>true,probe:reused}).pid,5555);
  assert.equal(fs.readFileSync(file,'utf8'),'5555');
});

test('the service that recorded what it is keeps its PID file, whatever else is asked',t=>{
  const dir=temp(t),file=path.join(dir,'pid'),live=table({4242:running('Thu Sep 17 08:00:00 2026')});
  claimProcessFile(file,{pid:4242,probe:live});
  assert.equal(claimProcessFile(file,{pid:5555,isAlive:()=>false,probe:live}),null);
  // A process table that cannot answer is not evidence that the service has
  // stopped, so the claim is refused there too.
  const silent=()=>({alive:true,known:false,started:null,command:null});
  assert.equal(claimProcessFile(file,{pid:5555,isAlive:()=>false,probe:silent}),null);
});

test('a PID file written before identities were recorded is claimed the way it always was',t=>{
  const dir=temp(t),file=path.join(dir,'pid');
  fs.writeFileSync(file,'4242');
  assert.equal(claimProcessFile(file,{pid:5555,isAlive:()=>true,probe:nobody}),null);
  assert.equal(claimProcessFile(file,{pid:5555,isAlive:()=>false,probe:nobody}).pid,5555);
  // And the file it leaves behind is one plain number, for every reader that
  // has only ever parsed it as one.
  assert.equal(fs.readFileSync(file,'utf8'),'5555');
});
