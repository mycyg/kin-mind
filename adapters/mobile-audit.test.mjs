import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileAudit,appraisalProgress} from './mobile-audit.mjs';

test('collection time cannot promote an old failed appraisal into a fresh failure',()=>{
  const old={at:'2026-09-15T15:00:00Z',appraisal:{state:'failed'}};
  assert.equal(appraisalProgress({},old).state,'unknown');
  assert.equal(appraisalProgress({},old).at,null);
  assert.equal(appraisalProgress({action:{last_success:'2026-09-15T14:59:00Z'}},old).state,'complete');
  const current=appraisalProgress({queues:[{lane:'action',state:'running',count:1,attempt_started_at:'2026-09-15T14:58:00Z',lease_expires_unix:1789000000}]},old);
  assert.equal(current.state,'running');
  assert.equal(current.at,'2026-09-15T14:58:00Z');
});

test('four-hour review is durable and unchanged failures cannot create repair loops',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=100,calls=0;
  const args={file:path.join(root,'audit.json'),now:()=>now,collect:async()=>({connected:false}),review:async()=>{calls++;return{status:'repair_needed',findings:[{code:'connection',evidence:'connected=false',summary:'Offline'}]};}};
  let audit=new MobileAudit(args);await audit.tick();assert.equal(calls,1);assert.equal(audit.pending().length,1);
  now+=60000;await audit.tick();assert.equal(calls,1);
  audit=new MobileAudit(args);await audit.tick();assert.equal(calls,1);
  now+=4*3600000;await audit.tick();assert.equal(calls,2);assert.equal(audit.pending().length,1);
});

test('healthy reviews remain quiet; review failure does not retry every minute',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=0,calls=0;
  const audit=new MobileAudit({file:path.join(root,'audit.json'),now:()=>now,collect:async()=>({healthy:true}),review:async()=>{calls++;if(calls===2)throw Error('offline');return{status:'healthy',findings:[]};}});
  await audit.tick();assert.equal(audit.pending().length,0);
  now+=4*3600000;await audit.tick();assert.equal(audit.state.status,'failed');
  now+=60000;await audit.tick();assert.equal(calls,2);
});

test('failed audits retry at five then fifteen minutes and return to four hours',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=0,calls=0;
  const a=new MobileAudit({file:path.join(root,'audit.json'),now:()=>now,collect:async()=>({}),
    review:async()=>{if(++calls<3)throw Error('incomplete');return{status:'healthy',findings:[]};}});
  await a.tick();assert.equal(a.state.nextAt,300000);
  now=300000;await a.tick();assert.equal(a.state.nextAt,now+900000);
  now=a.state.nextAt;await a.tick();assert.equal(a.state.nextAt,now+14400000);assert.equal(a.state.failures,0);
});
