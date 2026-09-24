import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {artifactManifest,AutonomousCreator,startAutonomousWork} from '../../adapters/autonomous-creator.mjs';

const temporary=()=>fs.mkdtempSync(path.join(os.tmpdir(),'kin-creator-test-'));
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
 try{const creator=new AutonomousCreator({command:'codex',root,spawnImpl,env:{PATH:'/usr/bin',HOME:root,FEISHU_APP_SECRET:'must-not-pass'}});
 const result=await creator.run({plan:{id:'p',goal:'Create a text'},step:{id:'s'},run:{id:'r'},brief:{known_evidence:[]}});
 assert.equal(result.state,'produced');assert.equal(result.receipt.thread_id,'isolated-only');assert.equal(result.artifacts.length,1);
 assert.equal(captured.options.env.FEISHU_APP_SECRET,undefined);assert.ok(captured.args.includes('--ignore-user-config'));assert.ok(captured.args.includes('features.apps=false'));assert.ok(captured.args.includes('sandbox_workspace_write.network_access=false'));assert.ok(!captured.args.includes('resume'));
 assert.equal(captured.args[captured.args.indexOf('--model')+1],'gpt-6-sol');assert.ok(captured.args.includes('model_reasoning_effort="medium"'));
 assert.equal(captured.args.includes('service_tier="fast"'),false,'the default creator never opts into paid Fast');
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
   modelCatalog:'/host/mobile-models.json',spawnImpl,env:{PATH:'/usr/bin',HOME:root,CODEX_HOME:'/host/state/codex-home'}});
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
