import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileAudit,AUDIT_FAILURE_RETRY_MINUTES,INCIDENT_REOPEN_MS} from '../../adapters/mobile-audit.mjs';
import {AUDIT_CODES,TAIL_DECISIONS} from '../../adapters/mobile-reviewer.mjs';

const HOUR=3600000;
function fixture(t,{readings=[],state=null}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const file=path.join(root,'mobile-audit.json');if(state)fs.writeFileSync(file,JSON.stringify(state));
  const clock={now:1000};let reviews=0;
  const audit=new MobileAudit({file,collect:async()=>({checkedAt:clock.now}),now:()=>clock.now,
    review:async()=>{const next=readings[reviews++];if(next instanceof Error)throw next;return next;}});
  return {audit,clock,file,reviews:()=>reviews};
}
const finding=(code,summary='worded differently each time')=>({code,summary,evidence:'field=value'});

test('the same fault classes are one incident however they are worded; findings are facts for Kin, never a repair turn (N13, AD2-27)',async t=>{
  const f=fixture(t,{readings:[
    {status:'needs_attention',findings:[finding('delivery-uncertain','one way')]},
    {status:'needs_attention',findings:[finding('delivery-uncertain','another way')]},
    {status:'healthy',findings:[]},
    {status:'needs_attention',findings:[finding('delivery-uncertain','back again')]},
  ]});
  await f.audit.tick();f.clock.now+=4*HOUR;await f.audit.tick();
  assert.equal(Object.keys(f.audit.state.incidents).length,1);
  const [incident]=Object.values(f.audit.state.incidents);assert.equal(incident.seen,2);
  assert.equal(f.audit.state.repairs,undefined,'no repair is ever queued');
  assert.deepEqual(f.audit.untold().map(i=>i.codes),[['delivery-uncertain']]);
  f.audit.markTold([incident.id]);assert.deepEqual(f.audit.untold(),[]);
  f.clock.now+=4*HOUR;await f.audit.tick();assert.equal(incident.state,'cleared');
  // Seen again within a day of clearing: the same incident, reopened.
  f.clock.now+=4*HOUR;await f.audit.tick();
  assert.equal(Object.keys(f.audit.state.incidents).length,1);assert.equal(incident.state,'open');
  assert.ok(INCIDENT_REOPEN_MS>=24*HOUR);
  assert.deepEqual(f.audit.view().incidents.map(i=>i.codes),[['delivery-uncertain']]);
});

test('a failed reading retries soon twice, then waits for its ordinary interval (AD2-27)',async t=>{
  const f=fixture(t,{readings:[Error('deepseek-invalid-audit'),Error('deepseek-invalid-audit'),Error('deepseek-invalid-audit')]});
  await f.audit.tick();assert.equal(f.audit.state.nextAt-f.clock.now,AUDIT_FAILURE_RETRY_MINUTES[0]*60000);
  f.clock.now=f.audit.state.nextAt;await f.audit.tick();assert.equal(f.audit.state.nextAt-f.clock.now,AUDIT_FAILURE_RETRY_MINUTES[1]*60000);
  f.clock.now=f.audit.state.nextAt;await f.audit.tick();assert.equal(f.audit.state.nextAt-f.clock.now,4*HOUR,'no unbounded fast retry');
});

test('repairs queued before this change are kept as history and never run',async t=>{
  const f=fixture(t,{state:{schema:1,nextAt:0,history:[],repairs:{'audit-1':{id:'audit-1',state:'pending'}},lastIncident:'x'}});
  assert.equal(f.audit.state.repairs,undefined);
  assert.equal(f.audit.state.retiredRepairs.repairs['audit-1'].state,'pending','the record is kept, not deleted');
  assert.deepEqual(JSON.parse(fs.readFileSync(f.file,'utf8')).retiredRepairs.repairs['audit-1'],{id:'audit-1',state:'pending'});
});

test('fault classes are a fixed set, and an interrupted reply is never discarded by a model (N4)',()=>{
  assert.ok(AUDIT_CODES.includes('other'));
  assert.deepEqual([...TAIL_DECISIONS],['continue','rewrite_remainder']);
});
