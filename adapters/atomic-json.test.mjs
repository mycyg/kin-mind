import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {writeJsonAtomic,writeFileAtomic,createJsonExclusive,createFileExclusive,readJsonFile,loadJson,quarantineFile,
  probeProcess,processIdentity,processGone,readPidLock,withPidLock,releasePidLock,pidLockHeld} from './atomic-json.mjs';

const temp=t=>{const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-atomic-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return dir;};

// Every process below is made up. Nothing here asks this machine what it is
// running: a test that depends on the local process table would pass or fail by
// accident, and the one case that matters — a pid handed out to somebody new —
// cannot be arranged on a real machine at all.
const HOLDER={pid:4242,started:'Thu Sep 18 09:00:00 2026',command:'/bin/node state-writer.mjs'};
const table=processes=>pid=>processes[pid]??{alive:false,known:true,started:null,command:null};
const running=(started,command)=>({alive:true,known:true,started,command});
const lockOf=(dir,name='send.lock')=>path.join(dir,name);
function writeLock(file,holder,nonce='previous-holder') {
  fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});
  fs.writeFileSync(file,JSON.stringify({holder,nonce,at:'2026-09-18T09:00:00.000Z'}),{mode:0o600});
  return file;
}
/** A work function that records a call it must never receive. */
const forbidden=calls=>()=>{calls.push('work');throw Error('The work ran after a lock was broken');};

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

test('state that is not JSON is replaced whole, and keeps no earlier revision',t=>{
  const dir=temp(t),file=path.join(dir,'rendered','page.html');
  writeFileAtomic(file,'<p>first</p>');
  writeFileAtomic(file,Buffer.from('<p>second</p>'));
  assert.equal(fs.readFileSync(file,'utf8'),'<p>second</p>');
  assert.deepEqual(fs.readdirSync(path.dirname(file)),['page.html']);
  assert.equal(fs.statSync(file).mode&0o777,0o600);
});

test('a file named after its own digest is written once, whole, and never again',t=>{
  const dir=temp(t),bytes=Buffer.from('the bytes that were observed'),file=path.join(dir,'artifacts','digest');
  assert.equal(createFileExclusive(file,bytes),true);
  assert.equal(createFileExclusive(file,Buffer.from('different bytes, same name')),false);
  assert.deepEqual(fs.readFileSync(file),bytes);
  // Nothing half-written is left beside it for a later reader to pick up.
  assert.deepEqual(fs.readdirSync(path.dirname(file)),['digest']);
  assert.equal(fs.statSync(file).mode&0o777,0o600);
});

test('a process table that cannot answer is never read as a dead process',()=>{
  // 0 and 1 are the kernel and the init process, and a pid that is not a number
  // names nothing: those are answered without asking anybody.
  for(const pid of [0,1,-3,'','not-a-pid',null])assert.deepEqual(probeProcess(pid),{alive:false,known:true,started:null,command:null});
  // A `ps` that is not there leaves the question unanswered, and an unanswered
  // question means the process is still running.
  const missing=probeProcess(4242,{ps:path.join(os.tmpdir(),'kin-no-such-ps-'+Date.now())});
  assert.deepEqual([missing.known,missing.alive],[false,true]);
  assert.equal(processGone(4242,{started:'whenever',probe:()=>missing}),false);
  // Whatever this machine is, it is running this test.
  assert.equal(probeProcess(process.pid).alive,true);
});

test('a reused pid is the stranger it is, and an unidentified one is left alone',()=>{
  const probe=table({4242:running('Fri Sep 18 11:00:00 2026','/bin/node state-writer.mjs')});
  assert.equal(processGone(4242,{started:HOLDER.started,probe}),true);
  assert.equal(processGone(4242,{started:'Fri Sep 18 11:00:00 2026',probe}),false);
  // No start time recorded: the command answers instead, and where neither was
  // recorded a live pid is simply a live pid.
  assert.equal(processGone(4242,{command:'courier.mjs',probe}),true);
  assert.equal(processGone(4242,{command:'state-writer.mjs',probe}),false);
  assert.equal(processGone(4242,{probe}),false);
  assert.equal(processGone(9999,{started:HOLDER.started,probe}),true);
  assert.deepEqual(processIdentity(4242,{probe}),{pid:4242,started:'Fri Sep 18 11:00:00 2026',command:'/bin/node state-writer.mjs'});
  assert.deepEqual(processIdentity(9999,{probe}),{pid:9999,started:null,command:null});
});

test('the work runs once under its own lock and gives it back afterwards',async t=>{
  const dir=temp(t),file=lockOf(dir),probe=table({});
  const held=[];
  const result=await withPidLock(file,{probe,work:({claim})=>{held.push(pidLockHeld(file,claim));return 'sent';}});
  assert.deepEqual([result.state,result.ran,result.value,held],['ran',true,'sent',[true]]);
  assert.equal(fs.existsSync(file),false);
  assert.equal(fs.existsSync(path.join(dir,'broken')),false);
  // A lock is given back even where the work fails, so one failure does not
  // block every attempt after it.
  await assert.rejects(withPidLock(file,{probe,work:()=>{throw Error('the send failed');}}),/the send failed/);
  assert.equal(fs.existsSync(file),false);
});

test('a holder that is still running is waited for, and nothing runs beside it',async t=>{
  const dir=temp(t),file=writeLock(lockOf(dir),HOLDER),calls=[];
  const probe=table({4242:running(HOLDER.started,HOLDER.command)});
  const result=await withPidLock(file,{probe,work:forbidden(calls),reconcile:()=>{calls.push('reconcile');}});
  assert.deepEqual([result.state,result.ran,calls],['busy',false,[]]);
  assert.equal(result.owner.holder.pid,4242);
  assert.deepEqual(readPidLock(file,{probe}).state,'held');
  // The holder's lock is still theirs: nothing was moved aside or overwritten.
  assert.equal(fs.existsSync(path.join(dir,'broken')),false);
  assert.equal(readJsonFile(file).value.nonce,'previous-holder');
});

test('a lock whose holder has gone leads to reconciliation, never to the send again',async t=>{
  const dir=temp(t),calls=[];
  // Three ways for a holder to be gone, and none of them is permission to send
  // the owner the same message a second time.
  const gone={
    'holder-exited':[writeLock(lockOf(dir,'exited.lock'),HOLDER),table({})],
    'pid-reused':[writeLock(lockOf(dir,'reused.lock'),HOLDER),table({4242:running('Fri Sep 18 11:00:00 2026','/bin/node another-program.mjs')})],
    'command-changed':[writeLock(lockOf(dir,'renamed.lock'),{pid:4242,started:null,command:'state-writer.mjs'}),table({4242:running('Fri Sep 18 11:00:00 2026','/usr/bin/grep')})],
  };
  for(const [reason,[file,probe]] of Object.entries(gone)) {
    assert.deepEqual(readPidLock(file,{probe}).reason,reason);
    const result=await withPidLock(file,{probe,work:forbidden(calls),reconcile:({owner})=>{calls.push(reason);return 'settled '+owner.holder.pid;}});
    assert.deepEqual([result.state,result.ran,result.reason,result.value],['reconciled',false,reason,'settled 4242']);
    // The broken lock is kept as the only record that an attempt was interrupted.
    assert.equal(readJsonFile(result.kept).value.nonce,'previous-holder');
    assert.equal(fs.existsSync(file),false);
  }
  assert.deepEqual(calls,['holder-exited','pid-reused','command-changed']);
});

test('a lock nobody can read is broken rather than left to wedge the work for good',async t=>{
  const dir=temp(t),file=lockOf(dir),calls=[];
  fs.mkdirSync(dir,{recursive:true});fs.writeFileSync(file,'{"holder":');
  const probe=table({});
  const result=await withPidLock(file,{probe,work:forbidden(calls),reconcile:()=>'settled'});
  assert.deepEqual([result.state,result.reason,result.value,calls],['reconciled','unreadable-lock','settled',[]]);
  assert.equal(fs.readFileSync(result.kept,'utf8'),'{"holder":');
});

test('a stale lock with nothing to reconcile it refuses instead of doing the work',async t=>{
  const dir=temp(t),file=writeLock(lockOf(dir),HOLDER),calls=[];
  const result=await withPidLock(file,{probe:table({}),work:forbidden(calls)});
  assert.deepEqual([result.state,result.ran,result.reason,calls],['stale',false,'holder-exited',[]]);
  // Once the interrupted attempt is out of the way, the next call is an ordinary
  // one: the refusal is about that attempt, not about the file for ever.
  const again=await withPidLock(file,{probe:table({}),work:()=>'sent'});
  assert.deepEqual([again.state,again.value],['ran','sent']);
});

test('only the holder gives a lock back',async t=>{
  const dir=temp(t),file=lockOf(dir),probe=table({});
  await withPidLock(file,{probe,work:({claim})=>{
    assert.equal(releasePidLock(file,{nonce:'somebody-else'}),false);
    assert.equal(pidLockHeld(file,{nonce:'somebody-else'}),false);
    assert.equal(fs.existsSync(file),true);
    assert.equal(claim.holder.pid,process.pid);
  }});
  assert.equal(releasePidLock(file,{nonce:'anyone'}),false);
  assert.deepEqual(readPidLock(file,{probe}),{state:'free',owner:null,reason:null});
});
