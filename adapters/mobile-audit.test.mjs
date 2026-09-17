import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileAudit,appraisalProgress} from './mobile-audit.mjs';
import {createLeaseClient} from './model-lease.mjs';

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

test('the background health audit yields its run at capacity and when the ledger is unreachable, and never blocks',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=0,collected=0,reviewed=0;
  const lanes=[];
  const build=request=>new MobileAudit({file:path.join(root,'audit.json'),now:()=>now,
    lease:createLeaseClient({request,now:()=>now,setTimer:()=>0,clearTimer:()=>{}}),
    collect:async()=>{collected++;return{healthy:true};},review:async()=>{reviewed++;return{status:'healthy',findings:[]};}});
  // The background pool is full: the audit says so and spends nothing.
  const full=build(async(route,body)=>{lanes.push(body.lane);
    return{state:'wait',reason:'deepseek-background-capacity',retry_after_seconds:30,capacity:{limit:4,held:4}};});
  const atCapacity=await full.tick();
  assert.equal(atCapacity.state,'skipped');
  assert.equal(atCapacity.reason,'deepseek-background-capacity');
  assert.deepEqual(lanes,['background']);
  assert.equal(collected,0);assert.equal(reviewed,0);
  // The interval is not consumed by a run that never happened.
  assert.equal(full.state.nextAt,0);
  const unreachable=build(async()=>{throw Error('ledger down');});
  const down=await unreachable.tick();
  assert.equal(down.state,'skipped');
  assert.equal(down.reason,'lease-service-unreachable');
  assert.equal(collected,0);assert.equal(reviewed,0);
  // With the ledger back, the same audit runs normally.
  const back=build(async(route,body)=>route.endsWith('acquire')
    ?{state:'admitted',lease:{id:body.id,lane:body.lane,purpose:body.purpose,ttl_seconds:90,renew_after_seconds:30}}:{state:'released'});
  assert.equal((await back.tick()).state,'healthy');
  assert.equal(collected,1);assert.equal(reviewed,1);
});

test('a lane refused inside the review is a skip, not a failed audit',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=1000;
  const audit=new MobileAudit({file:path.join(root,'audit.json'),now:()=>now,collect:async()=>({healthy:true}),
    review:async()=>{throw Object.assign(Error('model-lane-degraded'),{leaseSkipped:true,lease:{lane:'background',leaseState:'degraded',leaseReason:'lease-service-unreachable'}});}});
  const result=await audit.tick();
  assert.equal(result.state,'skipped');
  assert.equal(result.reason,'lease-service-unreachable');
  assert.equal(audit.state.status,'skipped');
  assert.equal(audit.state.failures,undefined);
  assert.equal(audit.state.nextAt,now+5*60000);
});
