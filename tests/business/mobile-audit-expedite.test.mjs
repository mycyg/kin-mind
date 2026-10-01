/** A fault between two readings brings the next one forward (MobileAudit `expedite`, 2026-10-01): soon
 * instead of at the interval, at most one early in an hour, never under a release freeze. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileAudit,EXPEDITE_MIN_GAP_MS} from '../../adapters/mobile-audit.mjs';

const MINUTE=60000,HOUR=60*MINUTE;
function fixture(t,{state=null}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-expedite-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const file=path.join(root,'mobile-audit-v2.json');if(state)fs.writeFileSync(file,JSON.stringify(state));
  const clock={now:10*HOUR},gate={frozen:false};let reviews=0;
  const make=()=>new MobileAudit({file,collect:async()=>({checkedAt:clock.now}),now:()=>clock.now,
    activity:()=>gate.frozen?{ok:false,reason:'frozen'}:{ok:true,release(){}},
    review:async()=>{reviews++;return {status:'healthy',findings:[]};}});
  return {audit:make(),make,clock,gate,file,reviews:()=>reviews};
}

test('a new fault brings the next reading forward; the same fault seen again does not',async t=>{
  const f=fixture(t);
  await f.audit.tick();assert.equal(f.reviews(),1);
  const regular=f.audit.state.nextAt;assert.equal(regular,f.clock.now+4*HOUR);
  f.clock.now+=90*MINUTE;
  assert.equal((await f.audit.tick()).state,'not-due');
  const asked=f.audit.expedite(['contact-failing']);
  assert.deepEqual(asked,{state:'expedited',nextAt:f.clock.now,codes:['contact-failing']});
  assert.deepEqual(f.audit.view().expedited,{at:f.clock.now,codes:['contact-failing'],nextAt:f.clock.now});
  await f.audit.tick();assert.equal(f.reviews(),2,'read at once');
  assert.equal(f.audit.state.nextAt,f.clock.now+4*HOUR,'and the regular interval goes on from there');
  // Still failing at the next looks: known, nothing moves.
  f.clock.now+=10*MINUTE;
  assert.deepEqual(f.audit.expedite(['contact-failing']),{state:'known'});
  assert.equal(f.audit.state.nextAt,f.clock.now-10*MINUTE+4*HOUR);
  // A persisted state keeps what it knew: a restart asks for nothing again.
  assert.deepEqual(f.make().expedite(['contact-failing']),{state:'known'});
});

test('at most one reading early in an hour; a fault right after a regular reading is read at once; one that clears and comes back asks again',async t=>{
  const f=fixture(t);
  assert.equal(EXPEDITE_MIN_GAP_MS,HOUR);
  await f.audit.tick();const read=f.clock.now;
  // The regular reading could not see a fault that began after it: read now, not in four hours.
  f.clock.now+=10*MINUTE;
  assert.deepEqual(f.audit.expedite(['embedding-failed']),{state:'expedited',nextAt:read+10*MINUTE,codes:['embedding-failed']});
  await f.audit.tick();assert.equal(f.reviews(),2);
  const early=read+10*MINUTE;
  // A second fault besides the first ten minutes later: an hour after the early reading.
  f.clock.now+=10*MINUTE;
  assert.deepEqual(f.audit.expedite(['contact-failing','embedding-failed']),{state:'expedited',nextAt:early+HOUR,codes:['contact-failing']});
  f.clock.now+=20*MINUTE;assert.equal((await f.audit.tick()).state,'not-due');
  f.clock.now=early+HOUR;await f.audit.tick();assert.equal(f.reviews(),3);
  // Both clear; the contact one comes back: asked again, an hour after the last early reading.
  f.clock.now+=5*MINUTE;assert.deepEqual(f.audit.expedite([]),{state:'clear'});
  f.clock.now+=5*MINUTE;assert.deepEqual(f.audit.expedite(['contact-failing']),{state:'expedited',nextAt:early+2*HOUR,codes:['contact-failing']});
  // The gap is the caller's to set, and nothing is ever put later than it already is.
  assert.deepEqual(f.audit.expedite(['router-pump-failing'],{minGapMs:2*HOUR}),{state:'due',nextAt:early+2*HOUR,codes:['router-pump-failing']});
});

test('it only ever moves a reading earlier, and asks for nothing while a reading runs, while the state is refused, or for a code that is not one',async t=>{
  const f=fixture(t);
  await f.audit.tick();
  // A failed reading retried in five minutes, an early one ten minutes ago: due as it is.
  f.audit.state.nextAt=f.clock.now+5*MINUTE;f.audit.state.expedited={at:f.clock.now-10*MINUTE,codes:['embedding-failed'],nextAt:f.clock.now-10*MINUTE};
  assert.deepEqual(f.audit.expedite(['contact-failing']),{state:'due',nextAt:f.clock.now+5*MINUTE,codes:['contact-failing']});
  assert.equal(f.audit.state.nextAt,f.clock.now+5*MINUTE);
  f.audit.running=true;assert.deepEqual(f.audit.expedite(['embedding-stalled']),{state:'running'});f.audit.running=false;
  assert.deepEqual(f.audit.expedite(['Not a code','',null,42]),{state:'clear'});
  const refused=fixture(t,{state:{schema:99,nextAt:0}});
  assert.deepEqual(refused.audit.expedite(['contact-failing']),{state:'refused'});
  assert.equal(JSON.parse(fs.readFileSync(refused.file,'utf8')).schema,99,'a refused state is left as it is');
});

test('a reading brought forward under a release freeze waits for the thaw',async t=>{
  const f=fixture(t);
  await f.audit.tick();f.clock.now+=2*HOUR;
  f.gate.frozen=true;
  assert.equal(f.audit.expedite(['contact-failing']).state,'expedited');
  assert.deepEqual(await f.audit.tick(),{state:'skipped',reason:'dispatch-frozen'});
  f.clock.now+=10*MINUTE;assert.equal((await f.audit.tick()).state,'skipped');
  assert.equal(f.reviews(),1,'nothing read while frozen');
  f.gate.frozen=false;await f.audit.tick();assert.equal(f.reviews(),2,'read once thawed');
});
