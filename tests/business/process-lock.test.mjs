// The hosts' one process lock (AD1-20, AD2-24): a claim judged by pid and start
// time, used for scoped work, a lifetime lease and a service's pid file. Probes are
// injected, so no test depends on which numbers the machine happens to hand out.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {claimPidLock,claimPidFile,readPidLock,releasePidFile,releasePidLock,withPidLock,processIdentity} from '../../adapters/atomic-json.mjs';
import {SessionManager} from '../../adapters/session-manager.mjs';

const LIVE={alive:true,known:true,started:'Thu Sep 24 09:00:00 2026',command:'node service.mjs'};
const GONE={alive:false,known:true,started:null,command:null};
const DEAD=424242;
// This process is alive as itself; the fake pid DEAD is gone; anything else is alive.
const probe=pid=>Number(pid)===DEAD?GONE:Number(pid)===process.pid?{alive:true,known:true,...processIdentity(process.pid)}:LIVE;
function directory(t){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-process-lock-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return dir;}
const stale=(file,extra={})=>fs.writeFileSync(file,JSON.stringify({holder:{pid:DEAD,started:'Wed Sep 23 08:00:00 2026',command:'node'},nonce:'stale-nonce',at:'2026-09-23T08:00:00Z',...extra}));

test('two contenders that judged the same stale lock never both hold it',t=>{
  // AD2-24: B reads the stale claim, and while it asks whether that holder is gone, A
  // replaces it with its own. B must not then break A's fresh claim and take a second.
  const file=path.join(directory(t),'send.lock');stale(file);
  let first=null;
  const racing=pid=>{if(Number(pid)===DEAD&&!first)first=claimPidLock(file,{probe});return probe(pid);};
  const second=claimPidLock(file,{probe:racing});
  assert.equal(first.state,'held');assert.equal(first.broken.reason,'holder-exited');
  assert.equal(second.state,'busy');
  assert.equal(JSON.parse(fs.readFileSync(file,'utf8')).nonce,first.claim.nonce,'the lock is the first contender\'s');
  assert.deepEqual(fs.readdirSync(path.dirname(file)).filter(name=>name.includes('.break.')),[],'no break marker is left behind');
  assert.equal(fs.readdirSync(path.join(path.dirname(file),'broken')).length,1,'the stale claim is kept as evidence');
});

test('a contender that died while replacing a stale lock is superseded, a live one is waited for',t=>{
  const file=path.join(directory(t),'send.lock');stale(file);
  const marker=path.join(path.dirname(file),path.basename(file)+'.break.'+createHash('sha256').update(fs.readFileSync(file)).digest('hex').slice(0,24)+'.1');
  fs.writeFileSync(marker,JSON.stringify({holder:{pid:process.pid+1,started:LIVE.started,command:LIVE.command},nonce:'live-breaker'}));
  assert.equal(claimPidLock(file,{probe}).state,'busy','a live breaker is still replacing it');
  fs.writeFileSync(marker,JSON.stringify({holder:{pid:DEAD,started:'x',command:'node'},nonce:'dead-breaker'}));
  const taken=claimPidLock(file,{probe});
  assert.equal(taken.state,'held');assert.equal(taken.broken.owner.nonce,'stale-nonce');
  assert.equal(fs.existsSync(marker),false);
});

test('records written by earlier releases are judged by what they recorded',t=>{
  const dir=directory(t),lease=path.join(dir,'registry.json.lease');
  // A SessionManager lease: a bare pid and a nonce. Alive means held, as it always did.
  fs.writeFileSync(lease,JSON.stringify({pid:process.pid+1,nonce:'old'}));
  assert.equal(readPidLock(lease,{probe}).state,'held');
  fs.writeFileSync(lease,JSON.stringify({pid:DEAD,nonce:'old'}));
  assert.equal(claimPidLock(lease,{probe}).state,'held');
  // A pid file owner record: pid, start time and command. A reused pid is gone.
  const owner=path.join(dir,'bridge.pid.owner.json');
  fs.writeFileSync(owner,JSON.stringify({pid:process.pid,started:'Thu Jan  1 00:00:00 1970',command:'node'}));
  assert.equal(readPidLock(owner,{probe}).reason,'pid-reused');
  fs.writeFileSync(path.join(dir,'unreadable.lock'),'{');
  assert.equal(readPidLock(path.join(dir,'unreadable.lock'),{probe}).reason,'unreadable-lock');
});

test('a service pid file stays one number, with its claim recorded beside it',t=>{
  const dir=directory(t),file=path.join(dir,'bridge.pid');
  const claim=claimPidFile(file,{probe});
  assert.equal(fs.readFileSync(file,'utf8'),String(process.pid));
  const record=JSON.parse(fs.readFileSync(file+'.owner.json','utf8'));
  assert.deepEqual([record.pid,record.started,record.command],[process.pid,processIdentity(process.pid).started,processIdentity(process.pid).command],'readers of the owner record keep their fields');
  assert.equal(record.nonce,claim.nonce);
  assert.equal(claimPidFile(file,{probe}),null,'a second starter stands down');
  assert.equal(releasePidFile(file,claim),true);
  assert.equal(fs.existsSync(file),false);assert.equal(fs.existsSync(file+'.owner.json'),false);
  // A crashed service: its claim is taken over, and the number follows.
  fs.writeFileSync(file,String(DEAD));fs.writeFileSync(file+'.owner.json',JSON.stringify({pid:DEAD,started:'x',command:'node service.mjs'}));
  const next=claimPidFile(file,{probe});
  assert.ok(next);assert.equal(next.broken.reason,'holder-exited');assert.equal(fs.readFileSync(file,'utf8'),String(process.pid));
  releasePidFile(file,next);
  // A number written by a release that recorded nobody: alive means it is still theirs.
  fs.writeFileSync(file,String(process.pid+1));
  assert.equal(claimPidFile(file,{probe}),null);
  fs.writeFileSync(file,String(DEAD));
  assert.ok(claimPidFile(file,{probe}));
});

test('scoped work never runs on a lock it had to replace',async t=>{
  const file=path.join(directory(t),'grant.lock');stale(file);let ran=0;
  const outcome=await withPidLock(file,{probe,work:async()=>{ran++;},reconcile:async({owner})=>owner.nonce});
  assert.deepEqual([outcome.state,outcome.value,ran],['reconciled','stale-nonce',0]);
  assert.equal(fs.existsSync(file),false,'released after the reconciliation');
  assert.equal((await withPidLock(file,{probe,work:async()=>{ran++;return 'sent';}})).state,'ran');assert.equal(ran,1);
  const held=claimPidLock(file,{probe});
  assert.equal((await withPidLock(file,{probe,work:async()=>{ran++;}})).state,'busy');assert.equal(ran,1);
  releasePidLock(file,held.claim);
});

test('a session manager lease left by a crash whose pid was handed on does not block the next start (AD1-20)',t=>{
  const dir=directory(t),file=path.join(dir,'registry.json');
  const options={file,binding:{threadId:'main',nativeSessionId:'main'},coordinator:{locked:f=>f()}};
  fs.writeFileSync(file+'.lease',JSON.stringify({holder:{pid:process.pid,started:'Thu Jan  1 00:00:00 1970',command:'node service.mjs'},nonce:'crashed',at:'2026-09-23T00:00:00Z'}));
  const manager=new SessionManager(options);
  assert.equal(JSON.parse(fs.readFileSync(file+'.lease','utf8')).holder.pid,process.pid);
  assert.throws(()=>new SessionManager(options),/ALREADY_RUNNING/,'a second live manager is refused');
  manager.close();assert.equal(fs.existsSync(file+'.lease'),false);
  const again=new SessionManager(options);again.close();
});
