import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {artifactManifest,AutonomousCreator,startAutonomousWork} from './autonomous-creator.mjs';

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
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('user work prevents claims and failed result review releases only its own finished run',async()=>{
 const calls=[];let busy=true;const loop={tick:async()=>{},review:()=>{}};
 const worker=startAutonomousWork({loop,isBusy:()=>busy,ownerEpoch:()=>1,creator:{run:async()=>({state:'produced'}),stop(){}},call:async(action)=>{calls.push(action);if(action==='plan-claim')return{state:'claimed',run:{id:'r',fence:1},plan:{id:'p'}};if(action==='plan-result')throw Error('timeout');}});
 await worker.tick();assert.equal(calls.length,0);busy=false;await worker.tick();assert.deepEqual(calls,['plan-claim','plan-result','plan-interrupt']);worker.close();
});
