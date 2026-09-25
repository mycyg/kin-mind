import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {artifactManifest,AutonomousCreator,startAutonomousWork} from '../../adapters/autonomous-creator.mjs';

const temporary=()=>fs.mkdtempSync(path.join(os.tmpdir(),'kin-creator-test-'));
/** The host's execution records as the creator sees them (worker-ownership.mjs): `log` says what was
 * asked of them, in order. */
const MARK='4f1c2b3a-0d9e-4a8b-9c7d-6e5f4a3b2c1d';
function records(log=[]) {
 return {log,open:spec=>{log.push('open:'+spec.kind);return {mark:MARK,track(){log.push('track');},
  end:async()=>{log.push('end');return {ended:true};},settle:async()=>{log.push('settle');return {ended:true};}};}};
}
test('manifest rejects traversal, symlink escape and duplicate artifact paths',()=>{
 const root=temporary();try{fs.mkdirSync(path.join(root,'inside'));fs.writeFileSync(path.join(root,'file.txt'),'actual output');
 fs.symlinkSync(path.join(root,'file.txt'),path.join(root,'inside','escape'));
 assert.throws(()=>artifactManifest(path.join(root,'inside'),['../file.txt']),/escaped/);
 assert.throws(()=>artifactManifest(path.join(root,'inside'),['escape']),/escaped/);
 assert.throws(()=>artifactManifest(root,['file.txt','file.txt']),/duplicated/);
 assert.equal(artifactManifest(root,['file.txt'])[0].bytes,13);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('isolated worker uses configured model and returns native and artifact receipts without phone tools',async()=>{
 const root=temporary();let captured;
 const spawnImpl=(command,args,options)=>{captured={command,args,options};const child=new EventEmitter();Object.assign(child,{pid:99999999,stdout:new PassThrough(),stderr:new PassThrough(),stdin:new PassThrough(),kill:()=>{}});
 child.stdin.on('finish',()=>{fs.writeFileSync(path.join(options.cwd,'result.txt'),'A verified creation.');fs.writeFileSync(args[args.indexOf('--output-last-message')+1],JSON.stringify({summary:'Created',artifacts:['result.txt'],verification:['Read file'],remaining:[]}));
 child.stdout.end(JSON.stringify({type:'thread.started',thread_id:'isolated-only'})+'\n'+JSON.stringify({type:'turn.completed',usage:{input_tokens:10,output_tokens:4}})+'\n');setImmediate(()=>child.emit('exit',0));});return child;};
 try{const creator=new AutonomousCreator({command:'codex',root,spawnImpl,env:{PATH:'/usr/bin',HOME:root,FEISHU_APP_SECRET:'must-not-pass'},executions:records()});
 const result=await creator.run({plan:{id:'p',goal:'Create a text'},step:{id:'s'},run:{id:'r'},brief:{known_evidence:[]}});
 assert.equal(result.state,'produced');assert.equal(result.receipt.thread_id,'isolated-only');assert.equal(result.artifacts.length,1);
 assert.equal(captured.options.env.FEISHU_APP_SECRET,undefined);assert.ok(captured.args.includes('--ignore-user-config'));assert.ok(captured.args.includes('features.apps=false'));assert.ok(captured.args.includes('sandbox_workspace_write.network_access=false'));assert.ok(!captured.args.includes('resume'));
 assert.equal(captured.args[captured.args.indexOf('--model')+1],'gpt-6-sol');assert.ok(captured.args.includes('model_reasoning_effort="medium"'));
 assert.equal(captured.args.includes('service_tier="fast"'),false,'the default creator never opts into paid Fast');
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('a step is one execution of the host\'s: recorded before its CLI starts, its mark handed to the CLI, its shell and the render worker, and ended before its workspace is read and before it returns (CR5-MM-04)',async()=>{
 const root=temporary();const log=[];let captured;
 const spawnImpl=(command,args,options)=>{log.push('spawn');captured={args,options};const child=new EventEmitter();Object.assign(child,{pid:99999996,stdout:new PassThrough(),stderr:new PassThrough(),stdin:new PassThrough(),kill:()=>{}});
  child.stdin.on('finish',()=>{fs.writeFileSync(path.join(options.cwd,'page.html'),'<p>made</p>');fs.writeFileSync(args[args.indexOf('--output-last-message')+1],JSON.stringify({summary:'Made',artifacts:['page.html'],verification:[],remaining:[]}));
  child.stdout.end(JSON.stringify({type:'turn.completed',usage:{}})+'\n');setImmediate(()=>child.emit('exit',0));});return child;};
 const verifier={capabilities:()=>({static_html_render:true}),verify:async(result,{env})=>{log.push('verify:'+env.KIN_WORKER_MARK);return result;}};
 try{
  const creator=new AutonomousCreator({command:'codex',root,spawnImpl,env:{PATH:'/usr/bin',HOME:root},executions:records(log),verifier});
  const result=await creator.run({plan:{id:'p',goal:'Make a page'},step:{id:'s'},run:{id:'r'},brief:{}});
  assert.equal(result.state,'produced');
  // The record first; the end of everything the CLI started before the workspace is read (the
  // manifest, then the render); the step's own end, render worker included, before it returns.
  assert.deepEqual(log,['open:creation','spawn','track','end','verify:'+MARK,'settle']);
  assert.equal(captured.options.env.KIN_WORKER_MARK,MARK,'the CLI has the mark by name');
  assert.ok(captured.args.includes('shell_environment_policy.set={KIN_WORKER_MARK="'+MARK+'"}'),'and every shell command it runs');
  assert.ok(captured.args.includes('shell_environment_policy.inherit="none"'),'which inherits nothing else');
  // Without the host's records, nothing runs.
  const bare=new AutonomousCreator({command:'codex',root,spawnImpl:()=>{throw Error('must not start');}});
  await assert.rejects(bare.run({plan:{id:'p'},step:{id:'s'},run:{id:'r'},brief:{}}),/execution records/);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('user work prevents claims and failed result review releases only its own finished run',async()=>{
 const calls=[];let busy=true;const loop={tick:async()=>{},review:()=>{}};
 const worker=startAutonomousWork({loop,isBusy:()=>busy,creator:{run:async()=>({state:'produced'}),stop(){}},call:async(action,input)=>{calls.push([action,input]);if(action==='plan-claim')return{state:'claimed',run:{id:'r',fence:7},plan:{id:'p'}};if(action==='plan-result')throw Error('timeout');}});
 await worker.tick();assert.equal(calls.length,0);busy=false;await worker.tick();
 assert.deepEqual(calls.map(([action])=>action),['plan-claim','plan-result','plan-interrupt']);
 // T1-06: the release names this worker's own run, owner and fence, nothing wider.
 const owner='creator-'+process.pid;
 assert.equal(calls[0][1].owner,owner);
 assert.deepEqual({run_id:calls[2][1].run_id,owner:calls[2][1].owner,fence:calls[2][1].fence},{run_id:'r',owner,fence:7});
 worker.close();
});
test('the production combination: configured model, Fast, catalogue, and no writable /tmp (T1-06, AD2-22)',async()=>{
 const root=temporary();let captured;
 const spawnImpl=(command,args,options)=>{captured={command,args,options};const child=new EventEmitter();Object.assign(child,{pid:99999998,stdout:new PassThrough(),stderr:new PassThrough(),stdin:new PassThrough(),kill:()=>{}});
 child.stdin.on('finish',()=>{child.stdout.end(JSON.stringify({type:'thread.started',thread_id:'t'})+'\n');setImmediate(()=>child.emit('exit',1));});return child;};
 try{const creator=new AutonomousCreator({command:'/runtime/versions/b/bin/codex',root,model:'gpt-5.6-sol',reasoning:'medium',fast:true,
   modelCatalog:'/host/mobile-models.json',spawnImpl,env:{PATH:'/usr/bin',HOME:root,CODEX_HOME:'/host/state/codex-home'},executions:records()});
 const result=await creator.run({plan:{id:'p',goal:'Create'},step:{id:'s'},run:{id:'r'},brief:{}});
 assert.equal(result.state,'failed');
 assert.equal(captured.command,'/runtime/versions/b/bin/codex');assert.equal(captured.options.env.CODEX_HOME,'/host/state/codex-home');
 assert.equal(captured.args[captured.args.indexOf('--model')+1],'gpt-5.6-sol');assert.ok(captured.args.includes('service_tier="fast"'));
 assert.ok(captured.args.includes('model_catalog_json="/host/mobile-models.json"'));assert.ok(captured.args.includes('mcp_servers={}'));
 assert.ok(captured.args.includes('sandbox_workspace_write.exclude_slash_tmp=true'));assert.ok(captured.args.includes('sandbox_workspace_write.exclude_tmpdir_env_var=true'));
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('without a Codex to run nothing is claimed',async()=>{
 const calls=[];const worker=startAutonomousWork({loop:{tick:async()=>{},review:()=>{}},isBusy:()=>false,creator:{command:null,run:async()=>({}),stop(){}},call:async action=>{calls.push(action);return {};}});
 await worker.tick();assert.deepEqual(calls,[]);await worker.close();
});
test('a running creation keeps its lease when 小光 writes; only a close interrupts it (N7)',async()=>{
 const calls=[];let busy=false,seen=null;const loop={tick:async()=>{},review:()=>{}};
 const worker=startAutonomousWork({loop,isBusy:()=>busy,creator:{stop(){},run:async(claimed,{onHeartbeat})=>{
   busy=true;seen=worker.current;const renewed=await onHeartbeat();assert.equal(renewed.state,'renewed');return {state:'interrupted'};}},
  call:async(action)=>{calls.push(action);if(action==='plan-claim')return{state:'claimed',run:{id:'r',fence:1},plan:{id:'p',goal:'A small clock'},step:{goal:'Draw it'}};if(action==='plan-renew')return{state:'renewed'};return{};}});
 await worker.tick();
 assert.deepEqual(seen,{plan_id:'p',goal:'A small clock',step:'Draw it',run_id:'r'});
 assert.ok(calls.includes('plan-renew'));assert.equal(worker.current,null);
 await worker.close();
});
test('a busy completion review is asked again with the same result; a refusal names its reason (K2-02)',async()=>{
 const calls=[];let results=[{state:'waiting'},{state:'waiting'},{state:'completed'}];const status=[];
 const worker=startAutonomousWork({loop:{tick:async()=>{},review:()=>{}},isBusy:()=>false,retryMs:1,recordStatus:value=>status.push(value),
  creator:{run:async()=>({state:'produced'}),stop(){}},
  call:async(action,input)=>{calls.push(action);if(action==='plan-claim')return{state:'claimed',run:{id:'r',fence:1},plan:{id:'p'}};
   if(action==='plan-renew')return{state:'renewed'};if(action==='plan-result')return results.shift();return{};}});
 await worker.tick();
 assert.deepEqual(calls,['plan-claim','plan-result','plan-renew','plan-result','plan-renew','plan-result']);
 assert.equal(status.at(-1).creation.state,'completed');
 const seen=[];
 const refusing=startAutonomousWork({loop:{tick:async()=>{},review:()=>{}},isBusy:()=>false,creator:{run:async()=>({state:'produced'}),stop(){}},
  call:async(action,input)=>{seen.push([action,input]);if(action==='plan-claim')return{state:'claimed',run:{id:'r2',fence:3},plan:{id:'p'}};
   if(action==='plan-result')throw Object.assign(Error('contract'),{failure:{code:'creation-artifact-invalid'}});return{};}});
 await refusing.tick();
 assert.equal(seen.at(-1)[0],'plan-interrupt');assert.equal(seen.at(-1)[1].reason,'creation-artifact-invalid');
 await worker.close();await refusing.close();
});
test('render dependencies are recorded with their versions; a browser that cannot start withdraws rendering (AD2-23)',async()=>{
 const {CreationVerifier}=await import('../../adapters/creation-verifier.mjs');
 const root=temporary();
 try{
  const module=path.join(root,'node_modules','playwright','index.mjs');fs.mkdirSync(path.dirname(module),{recursive:true});fs.writeFileSync(module,'');
  fs.writeFileSync(path.join(root,'node_modules','playwright','package.json'),JSON.stringify({name:'playwright',version:'1.99.0'}));
  const chrome=path.join(root,'Google Chrome.app','Contents','MacOS','Google Chrome');fs.mkdirSync(path.dirname(chrome),{recursive:true});fs.writeFileSync(chrome,'');
  fs.writeFileSync(path.join(root,'Google Chrome.app','Contents','Info.plist'),'<plist><dict><key>CFBundleShortVersionString</key>\n<string>150.0.1</string></dict></plist>');
  const spawnImpl=()=>{const child=new EventEmitter();Object.assign(child,{pid:99999997,stdout:new PassThrough(),stderr:new PassThrough(),stdin:new PassThrough(),kill:()=>{}});
   child.stdin.on('finish',()=>{child.stdout.end(JSON.stringify({state:'unavailable',reason:'browser-launch-failed'}));setImmediate(()=>child.emit('close',0));});return child;};
  const verifier=new CreationVerifier({playwrightModule:module,executablePath:chrome,spawnImpl});
  assert.deepEqual({playwright:verifier.runtime().playwright.version,browser:verifier.runtime().browser.version},{playwright:'1.99.0',browser:'150.0.1'});
  assert.equal(verifier.capabilities().static_html_render,true);
  const page=path.join(root,'index.html');fs.writeFileSync(page,'<p>hi</p>');
  const checked=await verifier.verify({state:'produced',receipt:{workspace:root},artifacts:[{path:page,sha256:'x'}]});
  assert.equal(checked.host_verification[0].reason,'browser-launch-failed');
  assert.equal(verifier.capabilities().static_html_render,false);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('a stop or a shutdown during the claim is not lost: the claimed run is interrupted, never started (CR-MIND-05)',async()=>{
 for(const [how,reason] of [['stop','owner-stop'],['close','host-closing']]) {
  const calls=[],started=[];let release;
  const loop={tick:async()=>{},review:()=>{},stopExploration:()=>{}};
  const worker=startAutonomousWork({loop,isBusy:()=>false,creator:{run:async()=>{started.push('run');return {state:'produced'};},stop(){}},
   call:async(action,input)=>{calls.push([action,input]);if(action==='plan-claim')return new Promise(resolve=>{release=()=>resolve({state:'claimed',run:{id:'r-'+how,fence:3},plan:{id:'p'}});});return {};}});
  const ticking=worker.tick();
  await new Promise(resolve=>setImmediate(resolve));
  if(how==='stop')loop.stopExploration();else void worker.close();
  release();await ticking;
  assert.deepEqual(started,[],how);
  assert.deepEqual(calls.map(([action])=>action),['plan-claim','plan-interrupt'],how);
  assert.deepEqual(calls[1][1],{run_id:'r-'+how,owner:'creator-'+process.pid,fence:3,reason},how);
 }
});

test('the creation executor starts only when the router admits it, and leaves the in-flight set when done (CR-MIND-01)',async()=>{
 const calls=[],activities=[];let frozen=true;
 const loop={tick:async()=>{},review:()=>{}};
 const worker=startAutonomousWork({loop,isBusy:()=>false,creator:{run:async()=>({state:'produced'}),stop(){}},
  beginActivity:spec=>{activities.push(spec);return frozen?{ok:false,reason:'frozen'}:{ok:true,release(){activities.push('released');}};},
  call:async(action,input)=>{calls.push([action,input]);if(action==='plan-claim')return {state:'claimed',run:{id:'r1',fence:1},plan:{id:'p'}};
   if(action==='plan-result')return {state:'completed'};return {};}});
 await worker.tick();
 assert.deepEqual(calls.map(([action])=>action),['plan-claim','plan-interrupt']);
 assert.equal(calls[1][1].reason,'dispatch-frozen');
 frozen=false;calls.length=0;activities.length=0;
 await worker.tick();
 assert.deepEqual(calls.map(([action])=>action),['plan-claim','plan-result']);
 assert.deepEqual(activities,[{kind:'creation',id:'r1'},'released']);
});

test('a completion review that timed out keeps the creation in flight until its worker has exited, on every path (CR3-MM-09)',async t=>{
 const {MobileRouter}=await import('../../adapters/mobile-router.mjs');
 const root=temporary();t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
  model:'deepseek-flash',modelProvider:'custom-gateway',reasoningEffort:'high',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
 const router=new MobileRouter({file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>Date.parse('2026-09-25T00:00:00Z'),
  classify:async()=>({route:'chat',reason:'synthetic'}),switchModel:async()=>({...runtime}),waitForIdle:async()=>{throw Error('waiting');}});
 const inFlight=()=>router.activityList().map(a=>[a.kind,a.id]);
 // As the host's mind worker answers: the answer, and apart from it the end of its process.
 const workers=[],calls=[];
 let answers=[];
 const call=(action,input)=>{
  calls.push(action);
  if(action==='plan-claim')return Promise.resolve({state:'claimed',run:{id:'run-'+calls.length,fence:1},plan:{id:'p'},step:{goal:'g'}});
  if(action!=='plan-result')return Promise.resolve({state:'renewed'});
  let exit;const answer=answers.shift();
  const running=answer instanceof Error?Promise.reject(answer):Promise.resolve(answer);
  running.exited=new Promise(resolve=>{exit=resolve;});workers.push(exit);
  return running;
 };
 const turn=()=>new Promise(resolve=>setImmediate(resolve));
 const worker=startAutonomousWork({loop:{tick:async()=>{},review:()=>{}},isBusy:()=>false,retryMs:1,call,
  creator:{run:async()=>({state:'produced'}),stop(){}},beginActivity:spec=>router.beginActivity(spec)});
 // The exception path: the review timed out; its process is still ending when the run is interrupted.
 answers=[Object.assign(Error('mind-worker-timeout'),{code:'mind-worker-timeout'})];
 const timedOut=worker.tick();
 for(let i=0;i<20&&!calls.includes('plan-interrupt');i++)await turn();
 assert.deepEqual(calls,['plan-claim','plan-result','plan-interrupt']);
 await turn();
 assert.deepEqual(inFlight(),[['creation','run-1']],'the drain still sees the creation');
 assert.equal(worker.running,true);
 workers[0]();await timedOut;
 assert.deepEqual(inFlight(),[]);
 // The retry path: a review that found no slot, then one that settled; the first worker is still ending.
 calls.length=0;workers.length=0;answers=[{state:'waiting'},{state:'completed'}];
 const retried=worker.tick();
 // The retry waits retryMs on a timer, so wait on the clock rather than on a count of event-loop turns.
 for(let i=0;i<200&&workers.length<2;i++)await new Promise(resolve=>setTimeout(resolve,5));
 assert.equal(workers.length,2,'the retried completion review started');
 workers[1]();
 for(let i=0;i<5;i++)await turn();
 assert.deepEqual(calls,['plan-claim','plan-result','plan-renew','plan-result']);
 assert.deepEqual(inFlight(),[['creation','run-1']],'held for the first review\'s worker too');
 workers[0]();await retried;
 assert.deepEqual(inFlight(),[]);
});
