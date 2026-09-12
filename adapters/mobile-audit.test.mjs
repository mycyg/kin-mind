import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileAudit} from './mobile-audit.mjs';

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
