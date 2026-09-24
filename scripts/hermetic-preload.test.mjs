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
