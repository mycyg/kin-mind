import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

/** Bounded optional host capability; failure preserves the produced artifact. */
export class CreationVerifier {
 constructor({playwrightModule,executablePath,node=process.execPath,timeoutMs=45000,spawnImpl=spawn}){
  Object.assign(this,{playwrightModule,executablePath,node,timeoutMs,spawnImpl});
 }
 capabilities(){return {static_html_render:!!this.playwrightModule&&fs.existsSync(this.playwrightModule)&&(!this.executablePath||fs.existsSync(this.executablePath)),
  network:false,scripts:false,visual_quality:'requires-review'};}
 async verify(result,{signal}={}){
  if(result.state!=='produced')return result;
  const checks=[];
  for(const artifact of result.artifacts.filter(a=>path.extname(a.path).toLowerCase()==='.html').slice(0,2)){
   if(signal?.aborted)break;
   if(!this.capabilities().static_html_render){checks.push({state:'unavailable',source_sha256:artifact.sha256,reason:'static-browser-unavailable'});continue;}
   checks.push(await this.render(result.receipt.workspace,artifact,signal));
  }
  return {...result,host_verification:checks};
 }
 render(workspace,artifact,signal){return new Promise(resolve=>{
  const child=this.spawnImpl(this.node,[fileURLToPath(new URL('./creation-render-worker.mjs',import.meta.url)),this.playwrightModule,this.executablePath??''],
   {stdio:['pipe','pipe','pipe'],env:Object.fromEntries(['PATH','HOME','TMPDIR','LANG'].filter(k=>process.env[k]).map(k=>[k,process.env[k]])),detached:true});
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
