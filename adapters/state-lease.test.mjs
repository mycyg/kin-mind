import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fork} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {acquireLease,inspectLease,removeLease,LeaseLost} from './state-lease.mjs';

const temp=t=>{const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-lease-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return dir;};
const timing={ttlMs:90000,graceMs:30000,marginMs:15000};

test('one holder at a time; every acquisition is a new generation',t=>{
  const directory=temp(t);let now=1000;const options={directory,id:'group-a',clock:()=>now,...timing};
  const first=acquireLease({...options,role:'service'});
  assert.equal(first.generation,1);assert.equal(first.owner.role,'service');assert.equal(first.owner.pid,process.pid);
  assert.equal(acquireLease({...options,role:'cli'}),null);
  assert.equal(inspectLease(options).state,'held');
  first.release();assert.equal(inspectLease(options).state,'released');
  const second=acquireLease({...options,role:'cli'});
  assert.equal(second.generation,2);assert.equal(first.check().ok,false);
  assert.deepEqual(fs.readdirSync(directory).filter(n=>n.includes('.takeover.')),['group-a.takeover.g2']);
  assert.throws(()=>acquireLease({...options,id:'../escape'}),/plain file name/);
});

test('the holder renews at a third of the TTL; takeover waits for expiry plus grace',t=>{
  const directory=temp(t);let now=0;const options={directory,id:'group-b',clock:()=>now,...timing};
  const holder=acquireLease(options);
  now=29999;assert.equal(holder.renewIfDue(),true);assert.equal(holder.expiresAt,90000);
  now=30000;assert.equal(holder.renewIfDue(),true);assert.equal(holder.expiresAt,120000);
  now=120000+29999;assert.equal(acquireLease(options),null,'expired, but still inside the grace period');
  now=120000+30000;
  const next=acquireLease(options);
  assert.equal(next.generation,2);assert.equal(holder.renew(),false);
  assert.throws(()=>holder.assertHeld(),error=>error instanceof LeaseLost&&error.reason==='taken-over');
});

test('writes are refused near expiry, after expiry, and once a higher takeover marker exists',t=>{
  const directory=temp(t);let now=0;const options={directory,clock:()=>now,...timing};
  const holder=acquireLease({...options,id:'group-c',ttlMs:30000,marginMs:25000});
  now=6000;assert.deepEqual(holder.check(),{ok:false,reason:'expiring'},'expiry inside the safety margin');
  now=10000;assert.equal(holder.check().ok,true,'renewal at a third of the TTL moved the expiry out');
  now=10000+30000;assert.deepEqual(holder.check(),{ok:false,reason:'expired'});
  const fenced=acquireLease({...options,id:'group-d'});
  fs.mkdirSync(path.join(directory,'group-d.takeover.g2'));
  assert.deepEqual(fenced.check(),{ok:false,reason:'taken-over'},'the marker alone fences the old holder, before any new lease file');
  fenced.release();
  assert.equal(JSON.parse(fs.readFileSync(path.join(directory,'group-d.lease.json'),'utf8')).released,undefined,'a fenced holder writes nothing, not even its release');
});

test('a claimant that died before writing its lease blocks for one TTL, then the next generation goes on',t=>{
  const directory=temp(t);let now=1000;const options={directory,id:'group-e',clock:()=>now,...timing};
  fs.mkdirSync(path.join(directory,'group-e.takeover.g1'),{recursive:true});
  fs.writeFileSync(path.join(directory,'group-e.takeover.g1','claim.json'),JSON.stringify({owner:{pid:1,nonce:'dead',role:'cli'},at:1000}));
  assert.equal(inspectLease(options).state,'claiming');assert.equal(acquireLease(options),null);
  now=1000+90000+30000;
  assert.equal(acquireLease(options).generation,2);
});

test('generations never run backwards, even when the lease files were removed',t=>{
  const directory=temp(t);let now=0;const options={directory,id:'group-f',clock:()=>now,...timing};
  acquireLease(options).release();acquireLease(options).release();
  removeLease(options);assert.equal(inspectLease(options).state,'free');
  assert.equal(acquireLease({...options,floor:2}).generation,3);
});

test('forked processes racing for one lease: exactly one owner per generation',async t=>{
  const directory=temp(t),script=fileURLToPath(new URL('./testing/lease-contender.mjs',import.meta.url));
  const children=await Promise.all(Array.from({length:4},()=>new Promise((resolve,reject)=>{
    const child=fork(script,[directory,'group-g']);child.once('error',reject);child.once('message',()=>resolve(child));
  })));
  t.after(()=>children.forEach(child=>child.kill()));
  const round=now=>Promise.all(children.map(child=>new Promise(resolve=>{child.once('message',resolve);child.send({now});})));
  const first=await round(1000);
  assert.deepEqual(first.filter(r=>r.won).map(r=>r.generation),[1]);
  assert.equal((await round(1000+90000)).filter(r=>r.won).length,0,'an unexpired lease is never taken, by anyone');
  const second=await round(1000+90000+30000);
  assert.deepEqual(second.filter(r=>r.won).map(r=>r.generation),[2]);
  assert.equal(new Set([...first,...second].filter(r=>r.won).map(r=>r.generation)).size,2);
});
