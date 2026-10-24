import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {AutonomousCreator,startAutonomousWork} from '../../adapters/autonomous-creator.mjs';
const records={open:()=>({mark:'fixture',runsIn(){},track(){},workerExited(){},end:async()=>{},settle:async()=>{}})};
const claim={plan:{id:'plan',goal:'synthetic'},step:{id:'step'},run:{id:'run',fence:2,energy_activity_id:'plan:stable'},brief:{}};
function root(t){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-energy-creator-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return dir;}
test('actual CLI spawn is charged even on exit failure, while missing executable costs nothing',async t=>{
 let starts=0;const creator=new AutonomousCreator({root:root(t),command:'fixture',executions:records,
  spawnImpl:()=>spawn(process.execPath,['-e','process.stdin.resume();setTimeout(()=>process.exit(1),20)'],{stdio:['pipe','pipe','pipe']})});
 const result=await creator.run(claim,{onStarted:async()=>{starts++;}});assert.equal(result.state,'failed');assert.equal(starts,1);
 const missing=new AutonomousCreator({root:root(t),command:'/not-a-real-kin-executable',executions:records});
 await assert.rejects(missing.run(claim,{onStarted:async()=>{starts++;}}),/ENOENT/);assert.equal(starts,1);
});
test('native start callback waits for real receipt; failed started work counts, waiting and unknown do not',async t=>{
 let finish,starts=0;
 const creator=new AutonomousCreator({root:root(t),command:'native',executions:records,nativeRun:()=>new Promise(resolve=>{finish=resolve;})});
 const run=creator.run(claim,{onStarted:async()=>{starts++;}});await new Promise(r=>setImmediate(r));assert.equal(starts,0);
 finish({state:'waiting',reason:'native-failed',started:true,receipt:{native_turn_id:'actual-turn',fork_thread_id:'actual-fork'}});
 assert.equal((await run).state,'failed');assert.equal(starts,1);
 for(const answer of [{state:'waiting',model_invoked:false},{state:'waiting',started:null,receipt:{}},{state:'waiting',started:true,model_invoked:false,receipt:{native_turn_id:'admission-refused'}}]) {
  creator.nativeRun=async()=>answer;await creator.run(claim,{onStarted:async()=>{starts++;}});
 }
 assert.equal(starts,1);
});
test('plan claim alone does not charge and completion receipt propagates exact owner/fence',async()=>{
 const calls=[],loop={tick:async()=>{},review(){}};let shouldStart=false;
 const creator={command:'native',stop:async()=>{},run:async(_,options)=>{if(shouldStart)await options.onStarted();return {state:'failed'};}};
 const work=startAutonomousWork({loop,creator,isBusy:()=>false,call:async(action,input)=>{calls.push({action,input});return action==='plan-claim'?{state:'claimed',...claim}: {state:'done'};}});
 await work.tick();assert.equal(calls.filter(c=>c.action==='plan-started').length,0);
 shouldStart=true;await work.tick();const started=calls.filter(c=>c.action==='plan-started');assert.equal(started.length,1);
 assert.deepEqual(started[0].input,{run_id:'run',owner:'creator-'+process.pid,fence:2});await work.close();
});

test('confirmed creation always finishes energy after executor cleanup and before a rejected plan result',async()=>{
 const order=[],loop={tick:async()=>{},review(){}};
 const creator={command:'native',stop:async()=>{},run:async(_,options)=>{await options.onStarted();order.push('executor-settled');return {state:'failed'};}};
 const work=startAutonomousWork({loop,creator,isBusy:()=>false,recordStarted:async()=>{order.push('started');throw Error('stale fence');},recordFinished:async()=>{order.push('finished');},
  call:async action=>{order.push(action);if(action==='plan-claim')return {state:'claimed',...claim};if(action==='plan-result')throw Error('stale fence');return {state:'interrupted'};}});
 await work.tick();assert.deepEqual(order,['plan-claim','started','executor-settled','finished','plan-result','plan-interrupt']);await work.close();
});
test('executor exception after proven start finishes energy; unknown/unstarted exception cannot invent work',async()=>{
 const finished=[],loop={tick:async()=>{},review(){}};let started=true;
 const creator={command:'native',stop:async()=>{},run:async(_,options)=>{if(started)await options.onStarted();throw Error('executor ended');}};
 const work=startAutonomousWork({loop,creator,isBusy:()=>false,recordStarted:async()=>{},recordFinished:async({run})=>finished.push(run.id),
  call:async action=>action==='plan-claim'?{state:'claimed',...claim}:{state:'interrupted'}});
 await work.tick();assert.deepEqual(finished,['run']);started=false;await work.tick();assert.deepEqual(finished,['run']);await work.close();
});
