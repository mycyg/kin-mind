import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const preload=fileURLToPath(new URL('./hermetic-preload.mjs',import.meta.url));
// Runs inside a child that has the preload; every path comes from HERMETIC_ROOT. The service
// manager's name is assembled at run time: the parent refuses to start a child that names it.
const probe=`import fs from 'node:fs';import net from 'node:net';import {spawnSync} from 'node:child_process';
const root=process.env.HERMETIC_ROOT,guarded=root+'/protected',free=root+'/free',out={};
const attempt=async(name,work)=>{try {out[name]=(await work())??'ok';} catch(error){out[name]=/^hermetic: blocked/.test(error.message)?'blocked':'error: '+error.message;}};
await attempt('write',()=>fs.writeFileSync(guarded+'/a','x'));
await attempt('throughSymlink',()=>fs.writeFileSync(free+'/link/b','x'));
await attempt('promise',()=>fs.promises.writeFile(guarded+'/c','x'));
await attempt('mkdir',()=>fs.mkdirSync(guarded+'/d/e',{recursive:true}));
await attempt('rename',()=>{fs.writeFileSync(free+'/f','x');fs.renameSync(free+'/f',guarded+'/f');});
await attempt('read',()=>{fs.readdirSync(guarded);});
await attempt('temp',()=>fs.writeFileSync(free+'/g','x'));
await attempt('spawnEnv',()=>{spawnSync('/usr/bin/true',[],{env:{...process.env,EXTRA:guarded}});});
await attempt('spawnCwd',()=>{spawnSync('/usr/bin/true',[],{cwd:free+'/link'});});
await attempt('spawnFree',()=>String(spawnSync('/usr/bin/true',[]).status));
await attempt('promisified',async()=>{const {promisify}=await import('node:util');const {execFile}=await import('node:child_process');
  return (await promisify(execFile)('/bin/echo',['kept'])).stdout.trim();});
await attempt('serviceManager',()=>{spawnSync(['launch','ctl'].join(''),['list']);});
await attempt('grandchild',()=>{const run=spawnSync(process.execPath,['-e',"require('fs').writeFileSync(process.env.HERMETIC_ROOT+'/protected/h','x')"],
  {env:{PATH:process.env.PATH,HERMETIC_ROOT:root},encoding:'utf8'});return run.status!==0&&run.stderr.includes('hermetic: blocked')?'blocked':'unguarded';});
await attempt('remap',async()=>(await import(root+'/prod/where.mjs')).default);
await attempt('network',()=>net.connect(9,'192.0.2.1'));
await attempt('busyPort',()=>new Promise((done,fail)=>net.connect(Number(process.env.BUSY_PORT),'127.0.0.1',done).on('error',fail)));
await attempt('signal',()=>{process.kill(Number(process.env.PARENT_PID),'SIGCONT');});
await attempt('probe',()=>{process.kill(Number(process.env.PARENT_PID),0);});
await attempt('loopback',async()=>{const server=net.createServer(socket=>socket.end());await new Promise(done=>server.listen(0,'127.0.0.1',done));
  await new Promise((done,fail)=>net.connect(server.address().port,'127.0.0.1',done).on('error',fail));server.close();});
console.log(JSON.stringify(out));`;

test('the preload refuses what reaches a protected root, remaps modules, and leaves the rest alone',async t=>{
  const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'kin-hermetic-')));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const busy=net.createServer(socket=>socket.end());await new Promise(done=>busy.listen(0,'127.0.0.1',done));t.after(()=>busy.close());
  for(const dir of ['protected','free','prod','dev'])fs.mkdirSync(path.join(root,dir));
  fs.symlinkSync(path.join(root,'protected'),path.join(root,'free','link'));
  for(const where of ['prod','dev'])fs.writeFileSync(path.join(root,where,'where.mjs'),`export default '${where}';\n`);
  const run=spawnSync(process.execPath,['--import',preload,'--input-type=module','-e',probe],{encoding:'utf8',
    env:{PATH:process.env.PATH,HERMETIC_ROOT:root,KIN_PROTECTED_ROOTS:path.join(root,'protected'),
      KIN_REMAP:`${path.join(root,'prod')}=${path.join(root,'dev')}`,KIN_BLOCK_NETWORK:'1',
      KIN_BLOCKED_PORTS:String(busy.address().port),BUSY_PORT:String(busy.address().port),
      KIN_PROTECTED_PIDS:String(process.pid),PARENT_PID:String(process.pid)}});
  assert.deepEqual(JSON.parse(run.stdout),{write:'blocked',throughSymlink:'blocked',promise:'blocked',mkdir:'blocked',
    rename:'blocked',read:'ok',temp:'ok',spawnEnv:'blocked',spawnCwd:'blocked',spawnFree:'0',promisified:'kept',serviceManager:'blocked',
    grandchild:'blocked',remap:'dev',network:'blocked',busyPort:'blocked',signal:'blocked',probe:'ok',loopback:'ok'},run.stderr);
  assert.equal(run.status,1,'caught violations still fail the process');
  assert.match(run.stderr,/hermetic: 11 blocked operation\(s\)/);
  assert.deepEqual(fs.readdirSync(path.join(root,'protected')),[]);
});

test('ordinary writes pass through the preload untouched',()=>{
  const run=spawnSync(process.execPath,['--import',preload,'-e',"require('fs').writeFileSync(require('os').devNull,'x');console.log('ok')"],
    {encoding:'utf8',env:{PATH:process.env.PATH}});
  assert.equal(run.status,0,run.stderr);
  assert.equal(run.stdout.trim(),'ok');
});

test('child PYTHONPATH uses only an existing explicit copy remap and keeps every other guard',t=>{
  const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'kin-hermetic-pythonpath-')));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  for(const dir of ['prod/src','prod/missing','prod/linked','dev/src','other/src','prod-lookalike/src'])
    fs.mkdirSync(path.join(root,dir),{recursive:true});
  fs.symlinkSync(path.join(root,'prod'),path.join(root,'linked-target'));
  const script=`import {spawnSync,execFile} from 'node:child_process';import {promisify} from 'node:util';
const root=process.env.HERMETIC_ROOT,prod=root+'/prod',out={};
const child=(overrides={})=>({PATH:process.env.PATH,PYTHONPATH:prod+'/src',...overrides});
const attempt=(name,work)=>{try{out[name]=work();}catch(error){out[name]=error.code==='EHERMETIC'?'blocked':error.message;}};
attempt('copy',()=>{
  const run=spawnSync('/usr/bin/env',[],{env:child({KIN_PROTECTED_ROOTS:root+'/extra',KIN_PROTECTED_PIDS:'2',KIN_BLOCKED_PORTS:'4321'}),encoding:'utf8'});
  const result=Object.fromEntries(run.stdout.trim().split('\\n').map(line=>{const at=line.indexOf('=');return [line.slice(0,at),line.slice(at+1)];}));
  return {status:run.status,python:result.PYTHONPATH.split(':'),roots:result.KIN_PROTECTED_ROOTS.split(':'),pids:result.KIN_PROTECTED_PIDS.split(','),
    ports:result.KIN_BLOCKED_PORTS.split(','),network:result.KIN_BLOCK_NETWORK,bytecode:result.PYTHONDONTWRITEBYTECODE,node:result.NODE_OPTIONS};
});
attempt('missing',()=>spawnSync('/usr/bin/true',[],{env:child({PYTHONPATH:prod+'/missing',KIN_REMAP:prod+'/missing='+root+'/does-not-exist'})}).status);
attempt('linked',()=>spawnSync('/usr/bin/true',[],{env:child({PYTHONPATH:prod+'/linked',KIN_REMAP:prod+'/linked='+root+'/linked-target'})}).status);
attempt('protectedTarget',()=>spawnSync('/usr/bin/true',[],{env:child({KIN_REMAP:prod+'='+root+'/other'})}).status);
attempt('unmapped',()=>spawnSync('/usr/bin/true',[],{env:child({PYTHONPATH:root+'/other/src'})}).status);
attempt('otherEnv',()=>spawnSync('/usr/bin/true',[],{env:child({EXTRA:prod})}).status);
attempt('cwd',()=>spawnSync('/usr/bin/true',[],{env:child(),cwd:prod}).status);
attempt('command',()=>spawnSync(prod+'/program',[],{env:child()}).status);
attempt('lookalike',()=>spawnSync('/usr/bin/env',[],{env:child({PYTHONPATH:root+'/prod-lookalike/src'}),encoding:'utf8'}).stdout.includes(root+'/prod-lookalike/src'));
out.callback=(await promisify(execFile)('/bin/echo',['callback-kept'])).stdout.trim();
console.log(JSON.stringify(out));`;
  const run=spawnSync(process.execPath,['--import',preload,'--input-type=module','-e',script],{encoding:'utf8',
    env:{PATH:process.env.PATH,HERMETIC_ROOT:root,KIN_PROTECTED_ROOTS:[path.join(root,'prod'),path.join(root,'other')].join(':'),
      KIN_REMAP:`${path.join(root,'prod')}=${path.join(root,'dev')}`,KIN_BLOCK_NETWORK:'1',PYTHONDONTWRITEBYTECODE:'1',
      KIN_PROTECTED_PIDS:'1',KIN_BLOCKED_PORTS:'9876'}});
  const result=JSON.parse(run.stdout);
  assert.equal(result.copy.status,0,run.stderr);
  assert.deepEqual(result.copy.python,[path.join(path.dirname(preload),'pyguard'),path.join(root,'dev/src')]);
  assert.deepEqual(result.copy.roots.slice(0,3),[path.join(root,'extra'),path.join(root,'prod'),path.join(root,'other')]);
  for(const inherited of (process.env.KIN_PROTECTED_ROOTS??'').split(':').filter(Boolean))assert.ok(result.copy.roots.includes(inherited));
  for(const inherited of ['2','1',...(process.env.KIN_PROTECTED_PIDS??'').split(',').filter(Boolean)])assert.ok(result.copy.pids.includes(inherited));
  for(const inherited of ['4321','9876',...(process.env.KIN_BLOCKED_PORTS??'').split(',').filter(Boolean)])assert.ok(result.copy.ports.includes(inherited));
  assert.equal(result.copy.network,'1');assert.equal(result.copy.bytecode,'1');assert.ok(result.copy.node.includes(preload));
  for(const name of ['missing','linked','protectedTarget','unmapped','otherEnv','cwd','command'])assert.equal(result[name],'blocked',name);
  assert.equal(result.lookalike,true);assert.equal(result.callback,'callback-kept');
  assert.equal(run.status,1,'caught unsafe paths still fail the process');
  assert.match(run.stderr,/hermetic: 7 blocked operation\(s\)/);
});
