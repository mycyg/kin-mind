// Resident Core IPC and heartbeat recovery regressions. Synthetic serial worker; no models or contacts.
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs'
import { dirname, resolve, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const kin = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const temp = mkdtempSync(join(kin, '.mind-regression-'))
const source = process.env.KIN_MIND_SOURCE || join(kin, 'src')
await build({ entryPoints: [join(source, 'mind.ts'), join(source, 'heartbeat.ts'), join(source, 'contact/proactive.ts')], outbase: source, outdir: temp, format: 'esm', platform: 'node', bundle: true, packages: 'external' })
const { Mind } = await import(join(temp, 'mind.js'))
const { Heartbeat } = await import(join(temp, 'heartbeat.js'))
const { Reaching } = await import(join(temp, 'contact/proactive.js'))
const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
let failures = 0
async function check(name, run) {
  try { await run(); console.log('✓ ' + name) } catch (error) { failures++; console.error('✗ ' + name + '\n' + error.stack) }
}
// Keep the real Mind spawn/stdin/line parser/deadline/exit path. Only Core's work is delayed.
const worker = join(temp, 'worker.sh')
const script = join(temp, 'worker.cjs')
writeFileSync(worker, '#!/bin/sh\nexec ' + JSON.stringify(process.execPath) + ' ' + JSON.stringify(script) + ' "$@"\n', { mode: 0o700 })
writeFileSync(script, `const { createInterface } = require('node:readline');
const { appendFileSync } = require('node:fs');
let queue = Promise.resolve();
createInterface({input:process.stdin}).on('line', line => {
 queue=queue.then(async()=>{const f=JSON.parse(line);appendFileSync(${JSON.stringify(join(temp,'seen'))},f.action+'\\n');
 await new Promise(r=>setTimeout(r,f.args.delay||0));
 if(f.action==='crash')process.exit(2);
 process.stdout.write(JSON.stringify({id:f.id,ok:true,result:{state:'idle',action:f.action}})+'\\n');});
});
`, { mode: 0o700 })
let sequence = 0
const makeMind = () => new Mind({ python: worker, coreSource: temp, dataDir: join(temp, 'data-' + sequence++), pace: 1, log() {} })
try {
  await check('queued Core calls each get their execution deadline, not the time spent behind another action', async () => {
    const mind = makeMind()
    try {
      // Separate process startup (especially during parallel suites) from the queue deadline under test.
      await mind.call('warmup', {}, 10000)
      const answers = await Promise.all(['read','plan-claim','review-due'].map(action => mind.call(action, { delay: 900 }, 2000)))
      assert.deepEqual(answers.map(a => a.action), ['read','plan-claim','review-due'])
    } finally { mind.close() }
  })
  await check('an actually stuck action still times out; unsent writes are rejected, not replayed on a fresh worker', async () => {
    writeFileSync(join(temp,'seen'),'')
    const mind = makeMind()
    try {
      const results = await Promise.allSettled([mind.call('stuck', {delay:1000}, 150), mind.call('must-not-run', {}, 150)])
      assert.equal(results[0].status,'rejected'); assert.match(results[0].reason.message,/timeout/)
      assert.equal(results[1].status,'rejected'); assert.match(results[1].reason.message,/exited/)
      assert.equal(readFileSync(join(temp,'seen'),'utf8').includes('must-not-run'), false)
      assert.equal((await mind.call('read',{},1500)).state,'idle')
    } finally { mind.close() }
  })
  await check('worker exit rejects both active and queued requests and the next call starts a clean worker', async () => {
    const mind=makeMind()
    try {
      const results=await Promise.allSettled([mind.call('crash',{},1500),mind.call('unsent',{},1500)])
      assert.ok(results.every(r=>r.status==='rejected'))
      assert.equal((await mind.call('read',{},1500)).state,'idle')
    } finally {mind.close()}
  })
  await check('a failed spawn rejects queued requests without waiting for an exit event',async()=>{
    const mind=new Mind({python:join(temp,'missing-python'),coreSource:temp,dataDir:join(temp,'missing-data'),pace:1,log(){}})
    try {
      const results=await Promise.allSettled([mind.call('read',{},100),mind.call('plan-claim',{},100)])
      assert.ok(results.every(r=>r.status==='rejected' && /cannot start/.test(r.reason.message)))
    } finally {mind.close()}
  })
  await check('successful no-work creation/others rounds clear consecutive failure alerts without claiming task completion', async () => {
    const heartbeat=new Heartbeat({call:async()=>({state:'idle'}),run:async()=>({state:'complete'})},()=>false,()=>{},1)
    for(const lane of ['creation','others']) {
      heartbeat.every(lane,async()=>{})
      for(let n=0;n<3;n++)heartbeat.note(lane,{state:'failed',reason:'mind timeout'})
    }
    heartbeat.minute(); await wait(10)
    for(const lane of ['creation','others']) {
      assert.equal(heartbeat.beats[lane].state,'idle');assert.equal(heartbeat.beats[lane].failures,undefined)
      assert.equal(heartbeat.beats[lane].done,undefined)
    }
    heartbeat.close()
  })
  await check('a failed native review-due followed by a successful idle check clears the old failure', async()=>{
    const heartbeat=new Heartbeat({call:async()=>({state:'idle'}),run:async()=>({state:'complete'})},()=>false,()=>{},1)
    heartbeat.note('review',{state:'complete'});const done=heartbeat.beats.review.done
    heartbeat.note('review',{state:'failed',reason:'timeout'});await heartbeat.review()
    assert.equal(heartbeat.beats.review.state,'idle');assert.equal(heartbeat.beats.review.failures,undefined)
    assert.equal(heartbeat.beats.review.done,done);heartbeat.close()
  })
  await check('other-person review returns its real failed outcome to the heartbeat and a later idle check recovers',async()=>{
    let due=true
    const mind={call:async()=>({state:'idle',action:due}),run:async()=>({state:'failed',reason:'synthetic-review-failure'})}
    const reaching=new Reaching({mind},()=>({people:[{id:'person:synthetic'}],groups:[]}),()=>({}),()=>({via:'synthetic'}),()=>{},{},{})
    reaching.reviewTargets=async()=>({})
    const heartbeat=new Heartbeat(mind,()=>false,()=>{},1)
    heartbeat.review=async()=>{};heartbeat.enrich=async()=>{}
    heartbeat.every('others',()=>reaching.othersReview())
    heartbeat.minute();await wait(10)
    assert.equal(heartbeat.beats.others.state,'failed');assert.equal(heartbeat.beats.others.failures,1)
    assert.equal(heartbeat.beats.others.reason,'synthetic-review-failure')
    due=false;heartbeat.minute();await wait(10)
    assert.equal(heartbeat.beats.others.state,'idle');assert.equal(heartbeat.beats.others.done,undefined)
    heartbeat.close()
  })
  await check('empty polling preserves a real completed activity, while new errors still increment',async()=>{
    const heartbeat=new Heartbeat({},()=>false,()=>{},1)
    heartbeat.note('creation',{state:'complete'});const original=heartbeat.beats.creation
    heartbeat.every('creation',async()=>{}); // call the public registered round through minute; disable unrelated lanes
    heartbeat.review=async()=>{};heartbeat.enrich=async()=>{}
    heartbeat.minute();await wait(10);assert.equal(heartbeat.beats.creation,original)
    heartbeat.every('creation',async()=>{throw Error('still broken')})
    heartbeat.minute();await wait(10);heartbeat.minute();await wait(10)
    assert.equal(heartbeat.beats.creation.failures,2);heartbeat.close()
  })
} finally {rmSync(temp,{recursive:true,force:true})}
process.exitCode=failures?1:0
