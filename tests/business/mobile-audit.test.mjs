import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileAudit,AUDIT_FAILURE_RETRY_MINUTES,AUDIT_SCHEMA,INCIDENT_REOPEN_MS,withDetected} from '../../adapters/mobile-audit.mjs';
import {AUDIT_CODES,REVIEWER_LANES,createMobileReviewer} from '../../adapters/mobile-reviewer.mjs';

const HOUR=3600000;
function fixture(t,{readings=[],state=null,snapshot=()=>({})}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const file=path.join(root,'mobile-audit.json');if(state)fs.writeFileSync(file,JSON.stringify(state));
  const clock={now:1000};let reviews=0;
  const audit=new MobileAudit({file,collect:async()=>({checkedAt:clock.now,...snapshot()}),now:()=>clock.now,
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

test('repairs queued before this change are kept as history and never run; the state says it is another schema (OPS-06)',async t=>{
  const f=fixture(t,{state:{schema:1,nextAt:0,history:[],repairs:{'audit-1':{id:'audit-1',state:'pending'}},lastIncident:'x'}});
  assert.equal(f.audit.state.repairs,undefined);
  assert.equal(f.audit.state.retiredRepairs.repairs['audit-1'].state,'pending','the record is kept, not deleted');
  const written=JSON.parse(fs.readFileSync(f.file,'utf8'));
  assert.deepEqual(written.retiredRepairs.repairs['audit-1'],{id:'audit-1',state:'pending'});
  assert.equal(written.schema,AUDIT_SCHEMA,'without repairs it is not schema 1, whose readers read repairs');
  assert.equal(AUDIT_SCHEMA,2);
});

function files(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-audit-schema-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  return {root,file:path.join(root,'mobile-audit-v2.json'),legacyFile:path.join(root,'mobile-audit.json')};
}
const audit=(paths,extra={})=>new MobileAudit({...paths,collect:async()=>({}),review:async()=>({status:'healthy',findings:[]}),now:()=>5000,...extra});

test('the schema-1 file of the releases before is read once and never written: a rollback with the data kept reads it as it left it (OPS-06)',async t=>{
  const f=files(t);
  // What the release before 5fb4f85 wrote, and a reader of that release, which read `repairs` every second.
  const before={schema:1,nextAt:0,history:[],repairs:{'audit-1':{id:'audit-1',state:'pending'}}};
  const oldReader=state=>{if(state.schema!==1)throw Error('Unknown audit schema');return Object.values(state.repairs).filter(r=>r.state==='pending');};
  fs.writeFileSync(f.legacyFile,JSON.stringify(before));
  const bytes=fs.readFileSync(f.legacyFile);
  const a=audit(f);
  assert.equal(a.refused,null);
  const written=JSON.parse(fs.readFileSync(f.file,'utf8'));
  assert.equal(written.schema,2);assert.equal(written.repairs,undefined);assert.deepEqual(written.retiredRepairs.repairs,before.repairs);
  assert.deepEqual(written.importedFrom,{legacy:'schema-1',at:5000});
  await a.tick();a.markTold(['none']);a.reschedule(6);
  assert.deepEqual(fs.readFileSync(f.legacyFile),bytes,'the legacy file is never written');
  assert.equal(oldReader(JSON.parse(bytes)).length,1,'so a reader of the release before still reads it');
  // The next start reads its own file, not the legacy one again.
  fs.writeFileSync(f.legacyFile,JSON.stringify({...before,nextAt:123}));
  assert.equal(audit(f).state.importedFrom.at,5000);
});

test('a state of a schema this code does not know, or one nobody can read, is refused: nothing throws, nothing runs, nothing is overwritten (OPS-06)',async t=>{
  for(const [content,reason,schema] of [[JSON.stringify({schema:3,incidents:'another shape'}),'unknown-audit-schema',3],
    ['{"schema":2,',"unreadable-audit-state",null],[JSON.stringify(['not','an','object']),'unknown-audit-schema',null]]) {
    const f=files(t);fs.writeFileSync(f.file,content);
    let reviews=0;
    const a=audit(f,{review:async()=>{reviews++;return {status:'needs_attention',findings:[]};}});
    assert.deepEqual(a.refused,{reason,schema},content);
    assert.deepEqual(await a.tick(),{state:'refused',reason});assert.equal(reviews,0,'no reading runs');
    assert.deepEqual(a.untold(),[]);assert.equal(a.markTold(['x']),false);assert.equal(a.reschedule(1),false);
    assert.deepEqual(a.view(),{status:'refused',reason,schema,incidents:[]});
    assert.equal(fs.readFileSync(f.file,'utf8'),content,'left for the release that wrote it');
  }
});

test('a legacy file of no schema this code knows is left alone, and the audit starts afresh (OPS-06)',t=>{
  const f=files(t);const content=JSON.stringify({schema:7});fs.writeFileSync(f.legacyFile,content);
  const a=audit(f);
  assert.equal(a.refused,null);assert.equal(a.state.schema,2);assert.deepEqual(a.state.incidents,{});
  assert.deepEqual(a.state.importedFrom,{legacy:'left-alone',at:5000});
  assert.equal(fs.readFileSync(f.legacyFile,'utf8'),content);
});

test('incidents nobody can read are no facts, and never stop what asks for them (OPS-06)',t=>{
  const f=files(t);
  fs.writeFileSync(f.file,JSON.stringify({schema:2,nextAt:0,history:null,incidents:{a:null,b:{id:'b',state:'open'},c:{id:'c',state:'open',findings:[{code:'other'}]}}}));
  const a=audit(f);
  assert.deepEqual(a.untold().map(i=>i.id),['c']);
  assert.deepEqual(a.view().incidents.map(i=>i.id),['b','c']);
  assert.deepEqual(a.state.history,[]);
});

test('fault classes are a fixed set, and no reviewer decides what becomes of an interrupted reply (N4)',()=>{
  assert.ok(AUDIT_CODES.includes('other'));
  assert.equal('tail' in REVIEWER_LANES,false);
  assert.equal(createMobileReviewer({key:'unused'}).tail,undefined);
});

// 2026-09-28, 09:43 SGT: every proactive draft had failed for fifty minutes and the self-check answered
// healthy with no codes. What the host finds by its own rule (health.mjs contactFailing) is now in the
// reading as `detected`, and is a finding whatever the review answers.
const contactFailing={code:'contact-failing',summary:'主动联系连续因技术故障取消（宿主规则判定）',
  evidence:JSON.stringify({error:'contact-failing',code:'mind-worker-output-limit',within:8,streak:8,lastAt:'2026-09-28T01:35:05Z',wishId:'desire_b257'})};

test('what the host found by its own rule is a finding with its code, though the review answered healthy (2026-09-28)',async t=>{
  let detected=[contactFailing];
  const f=fixture(t,{snapshot:()=>({detected}),readings:[{status:'healthy',findings:[]},
    {status:'needs_attention',findings:[finding('contact-failing','the review saw it too'),finding('memory-stalled')]},{status:'healthy',findings:[]}]});
  assert.ok(AUDIT_CODES.includes('contact-failing'),'a code of the fixed set, for the review as well');
  assert.deepEqual(await f.audit.tick(),{state:'needs_attention'});
  assert.deepEqual(f.audit.state.lastReview.codes,['contact-failing']);
  assert.deepEqual(f.audit.untold().map(i=>[i.codes,i.findings.map(x=>x.code)]),[[['contact-failing'],['contact-failing']]]);
  assert.match(f.audit.untold()[0].findings[0].evidence,/mind-worker-output-limit/);
  // The review naming it too: the host's finding stands in its place, the review's others beside it.
  f.clock.now+=4*HOUR;await f.audit.tick();
  assert.deepEqual(f.audit.state.lastReview.codes,['contact-failing','memory-stalled']);
  const incident=f.audit.incidentList().find(i=>i.state==='open'&&i.codes.includes('memory-stalled'));
  assert.deepEqual(incident.findings.map(x=>[x.code,x.summary]),[['contact-failing',contactFailing.summary],['memory-stalled','worded differently each time']]);
  // Cleared by the host's facts, not by the review: once nothing is detected and the review is healthy, it clears.
  detected=[];f.clock.now+=4*HOUR;assert.deepEqual(await f.audit.tick(),{state:'healthy'});
  assert.deepEqual(f.audit.view().incidents,[]);
});

test('the host\'s finding does not wait for the review: a reading whose review failed still records it (2026-09-28)',async t=>{
  const f=fixture(t,{snapshot:()=>({detected:[contactFailing]}),readings:[Error('deepseek-http-503')]});
  assert.equal((await f.audit.tick()).state,'failed');
  assert.equal(f.audit.state.failures,1);assert.equal(f.audit.state.lastError.reason,'deepseek-http-503');
  assert.deepEqual(f.audit.untold().map(i=>i.codes),[['contact-failing']]);
});

test('only a code of the fixed set is taken from what the host detected, at most eight findings, the host\'s first',()=>{
  const review={status:'needs_attention',findings:Array.from({length:8},(_,i)=>finding(i%2?'task-stuck':'other'))};
  const merged=withDetected(review,{detected:[contactFailing,{code:'made-up',summary:'x',evidence:'y'},{code:'other',summary:'x',evidence:'y'}]});
  assert.equal(merged.findings.length,8);assert.equal(merged.findings[0].code,'contact-failing');
  assert.equal(merged.findings.some(x=>x.code==='made-up'),false);
  assert.deepEqual(withDetected({status:'healthy',findings:[]},{}),{status:'healthy',findings:[]},'nothing detected: the review stands as it is');
  assert.deepEqual(withDetected({status:'healthy',findings:[]},{detected:[{code:'made-up'}]}),{status:'healthy',findings:[]});
});
