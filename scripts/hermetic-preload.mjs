/** Test-only preload that keeps a checkout's test run away from a live install.
 *
 *   node --import ./scripts/hermetic-preload.mjs --test ...   (or put the --import in NODE_OPTIONS)
 *
 * Everything comes from the environment, and nothing happens when it is unset:
 *   KIN_REMAP="prodPrefix=devPrefix;..."  modules below prodPrefix load from devPrefix instead
 *   KIN_PROTECTED_ROOTS="root1:root2:..."  writes, and child processes that name, run or start
 *                                          inside these roots, fail with `hermetic: blocked ...`
 *   KIN_BLOCK_NETWORK=1                    connections to anything but loopback fail the same way
 *   KIN_BLOCKED_PORTS="8319,..."           loopback ports that belong to something else (a live
 *                                          service); connections to them fail too
 *   KIN_PROTECTED_PIDS="1,..."             processes that must not be signalled (all that ran
 *                                          before the tests); signal 0 still probes them
 * Paths are compared after resolving symlinks. A child started with its own environment gets
 * the guard carried into it (roots and remaps are merged, never dropped). Every blocked
 * operation is also reported on stderr at exit and turns a zero exit status into 1, so a caller
 * that catches the error cannot hide it. The Python half lives in ./pyguard/sitecustomize.py. */
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import childProcess from 'node:child_process';
import {register,syncBuiltinESMExports} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {promisify} from 'node:util';

const KEY=Symbol.for('kin.hermetic'),env=process.env,self=import.meta.url;
const pyguard=fileURLToPath(new URL('./pyguard',self));
const EXEMPT=new Set(['KIN_REMAP','KIN_PROTECTED_ROOTS']);
const LAUNCHCTL=/(?:^|[\s;&|()`'"=/])launchctl(?=$|[\s;&|()`'"])/;
const fold=process.platform==='darwin'||process.platform==='win32'?s=>s.toLowerCase():s=>s;
const trim=p=>p.length>1?p.replace(/\/+$/,''):p;

// The same file, loaded again with ?hooks, is the module resolve hook (it runs off-thread).
const hooksInstance=new URL(self).search==='?hooks';
let remapPairs=[];
export function initialize(data){remapPairs=data.pairs;}
function moved(url) {
  if(!url.startsWith('file:'))return null;
  const u=new URL(url),p=fileURLToPath(u);
  for(const [from,to] of remapPairs)if(p===from||p.startsWith(from+'/')){const v=pathToFileURL(to+p.slice(from.length));v.search=u.search;v.hash=u.hash;return v.href;}
  return null;
}
export async function resolve(specifier,context,next) {
  const early=moved(specifier.startsWith('/')?new URL(specifier,'file:///').href:specifier);
  const result=await next(early??specifier,context);
  const late=result.url&&moved(result.url);
  return late?{...result,url:late}:result;
}

function parseRemap(value='') {
  const pairs=[];
  for(const entry of value.split(';')){
    const at=entry.indexOf('=');if(at<1)continue;
    const [from,to]=[entry.slice(0,at),entry.slice(at+1)].map(s=>s.trim()).map(s=>trim(s.startsWith('file:')?fileURLToPath(s):s));
    if(from&&to&&!pairs.some(([known])=>known===from))pairs.push([from,to]);
  }
  return pairs.sort((a,b)=>b[0].length-a[0].length);
}

// The deepest existing ancestor decides where a path really is; `follow` false keeps the
// last component itself (unlinking a symlink touches the link, not what it points at).
function nearestReal(p,follow) {
  let base=follow?p:path.dirname(p),tail=follow?'':path.basename(p);
  for(;;){
    try {const real=fs.realpathSync.native(base);return tail?path.join(real,tail):real;}
    catch {const up=path.dirname(base);if(up===base)return p;tail=tail?path.join(path.basename(base),tail):path.basename(base);base=up;}
  }
}

// Each root as [compared spelling, reported spelling], symlinks resolved as well as given.
function protectedRoots(value='') {
  const roots=new Map();
  for(const raw of value.split(':').map(s=>s.trim()).filter(Boolean)){
    const root=trim(path.resolve(raw));
    for(const spelling of [root,trim(nearestReal(root,true))])if(!roots.has(fold(spelling)))roots.set(fold(spelling),spelling);
  }
  return [...roots];
}

function asPath(target) {
  if(typeof target==='string')return target;
  if(Buffer.isBuffer(target))return target.toString();
  if(target instanceof URL||(target&&typeof target.href==='string'&&target.protocol==='file:'))return fileURLToPath(target);
  return null;
}

function install() {
  const state=globalThis[KEY]={blocked:[],roots:protectedRoots(env.KIN_PROTECTED_ROOTS),pairs:parseRemap(env.KIN_REMAP)};
  const {roots,pairs}=state;
  if(pairs.length)register(new URL('?hooks',self).href,{data:{pairs}});
  const blocked=message=>{const error=new Error('hermetic: blocked '+message);error.code='EHERMETIC';state.blocked.push(error.message);throw error;};
  const inside=p=>{const f=fold(p);return roots.some(([r])=>f===r||f.startsWith(r==='/'?r:r+'/'));};
  const mentions=text=>{
    const f=fold(text);
    for(const [r,shown] of roots)for(let at=f.indexOf(r);at>=0;at=f.indexOf(r,at+1))if(!/[\w.-]/.test(f[at+r.length]??''))return shown;
    return null;
  };
  const check=(op,target,follow=true)=>{
    const p=asPath(target);if(p==null)return;
    const absolute=path.resolve(p);
    if(inside(absolute)||inside(nearestReal(absolute,follow)))blocked(`${op} on protected path ${p}`);
  };
  process.on('exit',()=>{
    if(!state.blocked.length)return;
    fs.writeSync(2,`hermetic: ${state.blocked.length} blocked operation(s) in pid ${process.pid}\n${state.blocked.map(m=>'  '+m).join('\n')}\n`);
    if(!process.exitCode)process.exitCode=1;
  });

  if(roots.length){
    const guard=(owner,name,args,promise=false)=>{
      const original=owner?.[name];if(typeof original!=='function')return;
      owner[name]={[name](...values){
        try {for(const [at,follow=true] of args)check(name,values[at],follow);}
        catch(error){if(promise)return Promise.reject(error);throw error;}
        return original.apply(this,values);
      }}[name];
    };
    const table=[['writeFile',[[0]]],['appendFile',[[0]]],['truncate',[[0]]],['mkdir',[[0]]],['mkdtemp',[[0]]],
      ['chmod',[[0]]],['chown',[[0]]],['utimes',[[0]]],['lchmod',[[0,false]]],['lchown',[[0,false]]],['lutimes',[[0,false]]],
      ['rm',[[0,false]]],['rmdir',[[0,false]]],['unlink',[[0,false]]],['rename',[[0,false],[1,false]]],
      ['copyFile',[[1]]],['cp',[[1]]],['symlink',[[1,false]]],['link',[[0],[1,false]]]];
    for(const [name,args] of table){guard(fs,name,args);guard(fs,name+'Sync',args);guard(fs.promises,name,args,true);}
    const {O_WRONLY,O_RDWR,O_CREAT,O_TRUNC,O_APPEND}=fs.constants;
    const writes=flags=>typeof flags==='number'?(flags&(O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND))!==0:typeof flags==='string'&&/[wa+]/.test(flags);
    for(const [owner,name,promise] of [[fs,'open'],[fs,'openSync'],[fs.promises,'open',true]]){
      const original=owner[name];
      owner[name]={[name](file,flags,...rest){
        try {if(writes(flags))check(name,file);}
        catch(error){if(promise)return Promise.reject(error);throw error;}
        return original.call(this,file,flags,...rest);
      }}[name];
    }
    const createWriteStream=fs.createWriteStream;
    fs.createWriteStream=function createWriteStream_(file,options){if(options?.fd==null)check('createWriteStream',file);return createWriteStream.call(this,file,options);};
  }

  if(roots.length||pairs.length){
    const merged=(key,separator,own)=>[...new Set([...(own??'').split(separator),...(env[key]??'').split(separator)].filter(Boolean))].join(separator);
    const carry=childEnv=>{
      const out={...childEnv};
      if(env.KIN_PROTECTED_ROOTS)out.KIN_PROTECTED_ROOTS=merged('KIN_PROTECTED_ROOTS',':',out.KIN_PROTECTED_ROOTS);
      if(env.KIN_REMAP)out.KIN_REMAP=merged('KIN_REMAP',';',out.KIN_REMAP);
      if(env.KIN_BLOCK_NETWORK==='1')out.KIN_BLOCK_NETWORK='1';
      if(env.PYTHONDONTWRITEBYTECODE)out.PYTHONDONTWRITEBYTECODE??=env.PYTHONDONTWRITEBYTECODE;
      if(env.KIN_BLOCKED_PORTS)out.KIN_BLOCKED_PORTS=merged('KIN_BLOCKED_PORTS',',',out.KIN_BLOCKED_PORTS);
      if(env.KIN_PROTECTED_PIDS)out.KIN_PROTECTED_PIDS=merged('KIN_PROTECTED_PIDS',',',out.KIN_PROTECTED_PIDS);
      if(!(out.NODE_OPTIONS??'').includes(self))out.NODE_OPTIONS=`${out.NODE_OPTIONS??''} --import=${self}`.trim();
      if(!(out.PYTHONPATH??'').split(':').includes(pyguard))out.PYTHONPATH=out.PYTHONPATH?pyguard+':'+out.PYTHONPATH:pyguard;
      return out;
    };
    const which=(command,searchPath)=>{
      for(const dir of (searchPath??'').split(':').filter(Boolean)){
        const file=path.join(dir,command);
        try {if(fs.statSync(file).isFile()){fs.accessSync(file,fs.constants.X_OK);return file;}} catch {}
      }
      return null;
    };
    const inspect=(op,command,args,options,shell)=>{
      const strings=[command,...args].filter(s=>typeof s==='string');
      for(const s of strings)if(LAUNCHCTL.test(s)||path.basename(s)==='launchctl')blocked(`${op} of launchctl`);
      if(!roots.length)return;
      for(const s of strings){
        const root=mentions(s);if(root)blocked(`${op} on protected path ${root}`);
        if(s.startsWith('/'))check(op,s);
        else if(s.startsWith('file://')){let url=null;try {url=new URL(s);} catch {}if(url)check(op,url);}
      }
      const childEnv=options?.env??env;
      if(!shell&&typeof command==='string'&&command&&!command.includes('/')){const found=which(command,childEnv.PATH);if(found)check(op,found);}
      if(options?.cwd!=null)check(op,options.cwd);
      for(const [key,value] of Object.entries(childEnv)){
        if(EXEMPT.has(key)||typeof value!=='string')continue;
        const root=mentions(value);if(root)blocked(`${op} on protected path ${root} (env ${key})`);
      }
    };
    // spawn(cmd, args?, options?) accepts a null args slot; exec(cmd, options?) has none.
    const wrap=(name,withArgs)=>{
      const original=childProcess[name];
      const wrapped=childProcess[name]={[name](...values){
        let at=1,args=[];
        if(withArgs&&(Array.isArray(values[1])||(values[1]==null&&values.length>2))){args=values[1]??[];at=2;}
        const options=values[at]&&typeof values[at]==='object'?values[at]:null;
        inspect(name,values[0],args,options,!withArgs||Boolean(options?.shell));
        if(options?.env){values=[...values];values[at]={...options,env:carry(options.env)};}
        return original.apply(this,values);
      }}[name];
      // exec and execFile promisify to {stdout, stderr}; the original helper would bypass the guard.
      if(original[promisify.custom])Object.defineProperty(wrapped,promisify.custom,{configurable:true,value:(...values)=>{
        let child;
        const promise=new Promise((resolve,reject)=>{child=wrapped(...values,(error,stdout,stderr)=>{
          if(error){error.stdout=stdout;error.stderr=stderr;reject(error);} else resolve({stdout,stderr});});});
        promise.child=child;return promise;}});
    };
    for(const name of ['spawn','spawnSync','execFile','execFileSync','fork'])wrap(name,true);
    for(const name of ['exec','execSync'])wrap(name,false);
  }

  const offline=env.KIN_BLOCK_NETWORK==='1';
  const ports=new Set((env.KIN_BLOCKED_PORTS??'').split(',').map(Number).filter(Boolean));
  if(offline||ports.size||roots.length){
    const connect=net.Socket.prototype.connect;
    const loopback=host=>/^(localhost|127\.\d+\.\d+\.\d+|::1|\[::1\]|::ffff:127\.\d+\.\d+\.\d+|0\.0\.0\.0)$/i.test(host);
    net.Socket.prototype.connect=function(...values){
      const [first,second]=Array.isArray(values[0])?values[0]:values;
      let host=null,port=null,socketPath=null;
      if(first&&typeof first==='object'){if(first.path!=null)socketPath=first.path;else{host=String(first.host??'localhost');port=first.port;}}
      else if(typeof first==='string'&&!/^\d+$/.test(first))socketPath=first;
      else {host=typeof second==='string'?second:'localhost';port=first;}
      if(socketPath!=null)check('connect',socketPath);
      else if(!loopback(host)){if(offline)blocked(`network connection to ${host}:${port}`);}
      else if(ports.has(Number(port)))blocked(`network connection to ${host}:${port}, a port another process was already serving`);
      return connect.apply(this,values);
    };
  }
  const pids=new Set((env.KIN_PROTECTED_PIDS??'').split(',').map(Number).filter(Boolean));
  if(pids.size){
    const kill=process.kill;
    process.kill=function(pid,signal){
      const target=Math.abs(Number(pid));
      if(signal!==0&&signal!=='0'&&pids.has(target)&&target!==process.pid)blocked(`signal ${signal??'SIGTERM'} to pid ${pid}, which was running before the tests`);
      return kill.call(this,pid,signal);
    };
  }
  syncBuiltinESMExports();
}

if(!hooksInstance&&!globalThis[KEY])install();
