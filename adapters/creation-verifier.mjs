import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

/** The version a package declares, read from the nearest package.json above a module file. */
function packageVersion(file){
 for(let dir=path.dirname(file??'');dir&&dir!==path.dirname(dir);dir=path.dirname(dir)){
  try{const pkg=JSON.parse(fs.readFileSync(path.join(dir,'package.json'),'utf8'));if(pkg.name&&pkg.version)return pkg.version;}catch{}
 }
 return null;
}
/** A macOS app's own version, from the bundle that holds its executable. Read, never run. */
function bundleVersion(executable){
 const match=String(executable??'').match(/^(.*?\.app)\//);if(!match)return null;
 try{const plist=fs.readFileSync(path.join(match[1],'Contents','Info.plist'),'utf8');
  return plist.match(/<key>CFBundleShortVersionString<\/key>\s*<string>([^<]+)<\/string>/)?.[1]??null;}catch{return null;}
}

/** Bounded optional host capability; failure preserves the produced artifact. Both external
 * dependencies (Playwright from a desktop runtime cache, a self-updating Chrome) are recorded
 * with the path and version they resolved to, for the operator's check; a renderer that could
 * not start withdraws the capability until the host restarts, so the creator is not promised a
 * render the host cannot make (AD2-23). */
export class CreationVerifier {
 constructor({playwrightModule,executablePath,node=process.execPath,timeoutMs=45000,spawnImpl=spawn}){
  Object.assign(this,{playwrightModule,executablePath,node,timeoutMs,spawnImpl});
  this.environmentFailure=null;
 }
 runtime(){return {playwright:{path:this.playwrightModule??null,present:!!this.playwrightModule&&fs.existsSync(this.playwrightModule),version:packageVersion(this.playwrightModule)},
  browser:{path:this.executablePath??null,present:!this.executablePath||fs.existsSync(this.executablePath),version:bundleVersion(this.executablePath)},
  withdrawn:this.environmentFailure};}
 capabilities(){return {static_html_render:!this.environmentFailure&&!!this.playwrightModule&&fs.existsSync(this.playwrightModule)&&(!this.executablePath||fs.existsSync(this.executablePath)),
  network:false,scripts:false,visual_quality:'requires-review'};}
 /** `env`: what the render worker's environment adds, the creation step's mark (CR5-MM-04): the
  * worker and the browser it starts are the step's, and the host ends them with it. */
 async verify(result,{signal,env={}}={}){
  if(result.state!=='produced')return result;
  const checks=[];
  for(const artifact of result.artifacts.filter(a=>path.extname(a.path).toLowerCase()==='.html').slice(0,2)){
   if(signal?.aborted)break;
   if(!this.capabilities().static_html_render){checks.push({state:'unavailable',source_sha256:artifact.sha256,reason:'static-browser-unavailable'});continue;}
   const check=await this.render(result.receipt.workspace,artifact,signal,env);
   if(['renderer-start-failed','browser-launch-failed'].includes(check.reason))this.environmentFailure=check.reason;
   checks.push(check);
  }
  return {...result,host_verification:checks};
 }
 render(workspace,artifact,signal,env={}){return new Promise(resolve=>{
  const marked=typeof env?.KIN_WORKER_MARK==='string'?{KIN_WORKER_MARK:env.KIN_WORKER_MARK}:{};
  const child=this.spawnImpl(this.node,[fileURLToPath(new URL('./creation-render-worker.mjs',import.meta.url)),this.playwrightModule,this.executablePath??''],
   {stdio:['pipe','pipe','pipe'],env:{...Object.fromEntries(['PATH','HOME','TMPDIR','LANG'].filter(k=>process.env[k]).map(k=>[k,process.env[k]])),...marked},detached:true});
  let output='',settled=false,timer;
  const finish=value=>{if(settled)return;settled=true;clearTimeout(timer);signal?.removeEventListener('abort',cancel);resolve({...value,source_sha256:artifact.sha256});};
  const cancel=()=>{try{process.kill(-child.pid,'SIGKILL');}catch{child.kill('SIGKILL');}finish({state:'unavailable',reason:signal?.aborted?'interrupted':'render-timeout'});};
  timer=setTimeout(cancel,this.timeoutMs);signal?.addEventListener('abort',cancel,{once:true});
  child.stdout.on('data',chunk=>{output+=chunk;if(output.length>250000)cancel();});child.stderr.on('data',()=>{});
  child.once('error',()=>finish({state:'unavailable',reason:'renderer-start-failed'}));
  child.once('close',code=>{if(code!==0)return finish({state:'failed',reason:'renderer-exit',exit_code:code});
   try{const value=JSON.parse(output);finish(value);}catch{finish({state:'failed',reason:'invalid-render-receipt'});}});
  child.stdin.on('error',()=>{});child.stdin.end(JSON.stringify({workspace,path:artifact.path,sha256:artifact.sha256}));
 });}
}
