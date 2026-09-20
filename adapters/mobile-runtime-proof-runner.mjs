/** Real, isolated compatibility execution. Caller input selects no evidence,
 * code, endpoint, command or success flag. The owned runner launches the exact
 * bundled ACP and launcher, observes their native RPC, and captures loopback
 * provider requests itself. This is a protocol test, not a live-provider test. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {spawn,execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';

const sha=b=>createHash('sha256').update(b).digest('hex');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const read=p=>JSON.parse(fs.readFileSync(p,'utf8'));
const bytes=p=>fs.readFileSync(p);
const write=(p,v)=>fs.writeFileSync(p,v,{mode:0o600});
const requireFact=(value,message)=>{if(!value)throw Error(message);};
const INPUT_SCHEMA='kin.mobile-runtime.runner-input/v2';
const RPC_TIMEOUT=25000;

export function validateRunnerInput(input,descriptor,manifestDigest,descriptorDigest){
  requireFact(input?.schema_version===INPUT_SCHEMA,'Caller-provided proof is not executable runner input');
  requireFact(Object.keys(input).every(k=>['schema_version','candidate_id','bundle_manifest_sha256','descriptor_sha256'].includes(k)),
    'Runner input cannot contain observations, code or external capture paths');
  requireFact(input.candidate_id===descriptor.candidate_id&&input.bundle_manifest_sha256===manifestDigest&&input.descriptor_sha256===descriptorDigest,'Runner input targets different bytes');
}

class Rpc {
  constructor(command,args,env,cwd){
    this.serial=0;this.pending=new Map();this.events=[];this.transcript=[];this.errors=[];
    this.child=spawn(command,args,{env,cwd,stdio:['pipe','pipe','pipe']});
    this.child.stderr.on('data',b=>{this.errors.push(b.toString());if(this.errors.length>20)this.errors.shift();});
    this.lines=createInterface({input:this.child.stdout});
    this.lines.on('line',line=>{let value;try{value=JSON.parse(line);}catch{return;}
      this.transcript.push({direction:'out',value});
      if(value.method){this.events.push(value);if(value.id!==undefined)this.send({jsonrpc:'2.0',id:value.id,error:{code:-32601,message:'Isolated compatibility probe denies tools and approvals'}});}
      else {const p=this.pending.get(String(value.id));if(p){this.pending.delete(String(value.id));value.error?p.reject(Error(p.method+': '+JSON.stringify(value.error))):p.resolve(value.result);}}
    });
    this.child.on('error',e=>this.fail(e));
    this.exited=new Promise(resolve=>this.child.once('exit',(code,signal)=>{this.exit={code,signal};this.fail(Error('Native compatibility process exited'));resolve();}));
  }
  fail(error){for(const p of this.pending.values())p.reject(error);this.pending.clear();}
  send(value){this.transcript.push({direction:'in',value});this.child.stdin.write(JSON.stringify(value)+'\n');}
  request(method,params={}){const id=String(++this.serial);return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{this.pending.delete(id);reject(Error(method+' timed out'));},RPC_TIMEOUT);
    this.pending.set(id,{method,resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}});
    this.send({jsonrpc:'2.0',id,method,params});
  });}
  async close(){if(this.exit)return;this.child.stdin.end();this.child.kill('SIGTERM');await Promise.race([this.exited,delay(2000)]);if(!this.exit){this.child.kill('SIGKILL');await this.exited;}this.lines.close();}
}

function sse(id){return [
  {type:'response.created',response:{id}},
  {type:'response.output_item.done',item:{type:'message',role:'assistant',id:'msg-'+id,content:[{type:'output_text',text:'KIN_COMPAT_OK'}]}},
  {type:'response.completed',response:{id,usage:{input_tokens:1,input_tokens_details:null,output_tokens:1,output_tokens_details:null,total_tokens:2}}},
].map(e=>'event: '+e.type+'\ndata: '+JSON.stringify(e)+'\n\n').join('');}

async function provider(){
  const captures=[];const server=createServer((req,res)=>{
    const parts=[];req.on('data',b=>parts.push(b));req.on('end',()=>{
      if(req.method!=='POST'||!req.url?.endsWith('/responses')){res.writeHead(404).end();return;}
      const raw=Buffer.concat(parts);let body;try{body=JSON.parse(raw);}catch{res.writeHead(400).end();return;}
      captures.push({raw,body,url:req.url,remoteAddress:req.socket.remoteAddress,headers:req.headers,at:Date.now()});
      const text=sse('kin-compat-'+captures.length);res.writeHead(200,{'content-type':'text/event-stream',connection:'close'});res.end(text);
    });
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  return {url:'http://127.0.0.1:'+server.address().port+'/v1',captures,close:async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));}};
}

// The proxy observes the real native protocol without changing a single RPC.
// Its child is the frozen launcher, not a caller-supplied command from input.
function proxySource(){return `#!/usr/bin/env node
const fs=require('node:fs'),{spawn}=require('node:child_process'),{createInterface}=require('node:readline');
const child=spawn(process.env.KIN_PROBE_LAUNCHER,process.argv.slice(2),{env:process.env,stdio:['pipe','pipe','pipe']});
const record=(direction,value)=>fs.appendFileSync(process.env.KIN_PROBE_TRACE,JSON.stringify({direction,value})+'\\n',{mode:0o600});
createInterface({input:process.stdin}).on('line',line=>{try{record('in',JSON.parse(line));}catch{}child.stdin.write(line+'\\n');}).on('close',()=>child.stdin.end());
createInterface({input:child.stdout}).on('line',line=>{try{record('out',JSON.parse(line));}catch{}process.stdout.write(line+'\\n');});
child.stderr.pipe(process.stderr);child.on('exit',(code,signal)=>process.exit(code??1));child.on('error',()=>process.exit(1));
for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{child.kill(signal);});
`;}

function trace(file){if(!fs.existsSync(file))return [];return fs.readFileSync(file,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);}
function rpcPairs(records){const requests=new Map(),pairs=[];for(const row of records){const m=row.value;
  if(row.direction==='in'&&m.id!==undefined&&m.method)requests.set(String(m.id),m);
  else if(row.direction==='out'&&m.id!==undefined&&requests.has(String(m.id)))pairs.push({request:requests.get(String(m.id)),response:m});
}return pairs;}
function lastPair(records,method){return rpcPairs(records).findLast(p=>p.request.method===method&&!p.response.error);}
function completedTurn(records){return records.findLast(r=>r.direction==='out'&&r.value.method==='turn/completed')?.value;}

function requestEvidence(capture,records,method,descriptor){
  requireFact(capture&&['127.0.0.1','::ffff:127.0.0.1'].includes(capture.remoteAddress),'No observed native loopback request');
  const body=capture.body,developer=fs.readFileSync(descriptor.developer.realpath,'utf8');
  const expectedWire=fs.readFileSync(descriptor.base.realpath,'utf8').trim();
  const baseIndex=(body.input??[]).findIndex(item=>item.role==='developer'&&Array.isArray(item.content)&&item.content.some(c=>c.text===expectedWire));
  const base=typeof body.instructions==='string'?body.instructions:baseIndex>=0?body.input[baseIndex].content.find(c=>c.text===expectedWire).text:null;
  const baseBasis=typeof body.instructions==='string'?'request.instructions':'request.input.developer-base';
  requireFact(typeof base==='string'&&base===expectedWire,'Native request used different base instructions: '+JSON.stringify({observed:typeof base==='string'?{sha256:sha(base),bytes:Buffer.byteLength(base)}:null,expected:sha(expectedWire),
    appliedConfigFile:lastPair(records,'thread/start')?.request?.params?.config?.model_instructions_file===descriptor.base.realpath,
    explicitBaseType:typeof lastPair(records,'thread/start')?.request?.params?.baseInstructions,
    model:body.model,keys:Object.keys(body),input:(body.input??[]).map(x=>({type:x.type,role:x.role,contents:(x.content??[]).filter(c=>typeof c?.text==='string').map(c=>({bytes:Buffer.byteLength(c.text),sha256:sha(c.text),hasBase:c.text.includes(expectedWire)}))}))}));
  const index=(body.input??[]).findIndex(item=>item.role==='developer'&&Array.isArray(item.content)&&item.content.some(c=>c.text===developer));
  requireFact(index>=0,'Native request did not retain the explicit developer layer');
  const native=lastPair(records,method==='turn/start'?'thread/resume':method)??lastPair(records,'thread/start');
  const turn=completedTurn(records),identity=native?.response?.result;
  requireFact(identity?.thread?.id&&identity.thread.sessionId&&turn?.params?.turn?.status==='completed','Native identity or completed turn missing');
  const turnId=turn.params.turn.id;
  requireFact(lastPair(records,'turn/start')?.response?.result?.turn?.id===turnId,'Turn completion belongs to another turn');
  const calls=records.filter(r=>r.direction==='out'&&r.value.method==='item/started'&&/tool|command|terminal/i.test(r.value.params?.item?.type??''));
  requireFact(calls.length===0,'Loopback compatibility probe unexpectedly invoked a tool');
  const source=identity.instructionSources;requireFact(Array.isArray(source),'Native instructionSources field unavailable');
  return {rpc_method:method,turn_status:'completed',capture:{raw_request_sha256:sha(capture.raw),
    extraction_receipt_sha256:sha(JSON.stringify({base:baseBasis,base_index:baseIndex,developer:'$.input['+index+'].content',turnId})),
    base:{sha256:sha(base),bytes:Buffer.byteLength(base),basis:baseBasis},
    developer:{sha256:sha(developer),bytes:Buffer.byteLength(developer),basis:'request.input.developer'}},
    config_sha256:descriptor.runtime.config.sha256,catalog_sha256:descriptor.runtime.catalog.sha256,
    thread_id:identity.thread.id,session_id:identity.thread.sessionId,instruction_sources:source,instruction_sources_provenance:'app-server-native',
    turn_sha256:sha(JSON.stringify(turn)),rpc_receipt_sha256:sha(JSON.stringify(lastPair(records,method))),
    tools:{definitions:Array.isArray(body.tools)?body.tools.length:0,calls:0,executions:0}};
}

function observedProfile(records,expected,capture){
  const pair=lastPair(records,'thread/resume')??lastPair(records,'thread/start'),actual=pair?.response?.result;
  const request=capture?.body;requireFact(actual?.modelProvider&&request?.model&&request.reasoning?.effort,'Native profile missing');
  const fast=['fast','priority'].includes(request.service_tier);
  return {checkpoint:expected.checkpoint,model:request.model,provider:actual.modelProvider,reasoning_effort:request.reasoning.effort,
    fast_mode:fast?'on':'off',canonical_id:request.model,canonical_match:true,
    service_tier_preference:fast?'fast':'default',actual_service_tier:null};
}

export async function produceMobileRuntimeProof({bundleDir,descriptorPath,inputPath}){
  const manifestFile=path.join(bundleDir,'manifest.json'),manifestBytes=bytes(manifestFile),manifest=JSON.parse(manifestBytes),descriptorBytes=bytes(descriptorPath),descriptor=JSON.parse(descriptorBytes),inputBytes=bytes(inputPath);
  validateRunnerInput(JSON.parse(inputBytes),descriptor,sha(manifestBytes),sha(descriptorBytes));
  const scratch=fs.mkdtempSync(path.join(os.tmpdir(),'kin-runtime-proof-'));fs.chmodSync(scratch,0o700);
  const home=path.join(scratch,'home'),cwd=path.join(scratch,'workspace'),traceFile=path.join(scratch,'native.jsonl'),proxy=path.join(scratch,'proxy.cjs');
  fs.mkdirSync(home,{mode:0o700});fs.mkdirSync(cwd,{mode:0o700});write(proxy,proxySource());fs.chmodSync(proxy,0o700);
  const codex=path.join(bundleDir,manifest.runtime.codex.path),acp=path.join(bundleDir,manifest.runtime.acp.entry_path),baseConfig=read(descriptor.runtime.config.realpath);
  const env=Object.fromEntries(['PATH','TMPDIR','LANG','LC_ALL','SHELL'].filter(k=>process.env[k]).map(k=>[k,process.env[k]]));
  Object.assign(env,{CODEX_HOME:home,HOME:home,CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG:'1',CODEX_PATH:proxy,
    KIN_PROBE_LAUNCHER:descriptor.runtime.launcher.realpath,KIN_PROBE_TRACE:traceFile,INITIAL_AGENT_MODE:'read-only',NO_BROWSER:'1',RUST_LOG:'warn'});
  const server=await provider();let client;const observations=[];
  try{
    const codexVersion=execFileSync(codex,['--version'],{encoding:'utf8',timeout:15000,env}).trim();
    const acpVersion=execFileSync(process.execPath,[acp,'--version'],{encoding:'utf8',timeout:15000,env}).trim();
    const profileDefaults={model:'deepseek-flash',provider:'kin-compat',reasoning_effort:'high',fast_mode:'off',checkpoint:'final'};
    const profiles=descriptor.expected_profiles??[];
    const current=p=>({...profileDefaults,...p});
    const config=p=>({...baseConfig,model:p.model,model_provider:p.provider,model_reasoning_effort:p.reasoning_effort,
      service_tier:p.fast_mode==='on'?'fast':null,'features.fast_mode':p.fast_mode==='on',approval_policy:'never',sandbox_mode:'read-only',
      check_for_update_on_startup:false,disable_response_storage:false,'analytics.enabled':false,
      mcp_servers:{},plugins:{},model_providers:{[p.provider]:{name:'Isolated compatibility provider',base_url:server.url,wire_api:'responses',request_max_retries:0,stream_max_retries:0,requires_openai_auth:false}},
      ...Object.fromEntries(['apps','browser_use','computer_use','hooks','image_generation','multi_agent','plugins','shell_snapshot','shell_tool','skill_search','sleep_tool','tool_suggest','unified_exec','unified_exec_tty','view_image','web_search_request','workspace_dependencies'].map(k=>['features.'+k,false]))});
    const launch=async(p,overrides={})=>{
      if(client)await client.close();write(traceFile,'');
      // An empty home prevents account credentials or configured MCP servers
      // from the interactive desktop leaking into the candidate process.
      write(path.join(home,'config.toml'),'check_for_update_on_startup = false\nweb_search = "disabled"\nmodel_provider = '+JSON.stringify(p.provider)+'\n[model_providers.'+p.provider+']\nname = "Isolated compatibility fixture"\nbase_url = '+JSON.stringify(server.url)+'\nwire_api = "responses"\nrequires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n[analytics]\nenabled = false\n[mcp_servers.computer-use]\ncommand = "/usr/bin/false"\nenabled = false\n[mcp_servers.node_repl]\ncommand = "/usr/bin/false"\nenabled = false\n');
      client=new Rpc(process.execPath,[acp],{...env,CODEX_CONFIG:JSON.stringify({...config(p),...overrides})},cwd);
      const init=await client.request('initialize',{protocolVersion:1,clientInfo:{name:'kin_runtime_compatibility',version:'2'},clientCapabilities:{fs:{readTextFile:false,writeTextFile:false},terminal:false}});
      requireFact(init.agentInfo?.version===manifest.runtime.acp.version,'ACP did not initialize with the bundled version');return init;
    };
    const sessionNew=async()=>{const result=await client.request('session/new',{cwd,mcpServers:[]});requireFact(result.sessionId,'ACP new session missing');return result.sessionId;};
    const sessionLoad=async id=>{await client.request('session/load',{sessionId:id,cwd,mcpServers:[]});};
    const prompt=async id=>{const before=server.captures.length;const result=await client.request('session/prompt',{sessionId:id,prompt:[{type:'text',text:'Isolated protocol fixture. Return KIN_COMPAT_OK without any tool calls.'}]});
      requireFact(result.stopReason==='end_turn'&&server.captures.length>before,'ACP did not complete an actual provider round trip');return server.captures.at(-1);};
    const signature=()=>({exit_code:0,acp_initialized:true,receipt_sha256:sha(JSON.stringify(client.transcript)),
      codex_sha256:sha(bytes(codex)),acp_entry_sha256:sha(bytes(acp)),acp_package_json_sha256:sha(bytes(path.join(bundleDir,manifest.runtime.acp.package_path,'package.json'))),
      base_sha256:sha(bytes(descriptor.base.realpath)),developer_sha256:sha(bytes(descriptor.developer.realpath)),
      config_sha256:sha(bytes(descriptor.runtime.config.realpath)),schema_sha256:sha(bytes(descriptor.runtime.schema.realpath)),catalog_sha256:sha(bytes(descriptor.runtime.catalog.realpath)),bundle_manifest_sha256:sha(manifestBytes)});
    let p=current(profiles.find(x=>x.scenario_id==='new_session'));await launch(p);let id=await sessionNew();
    let capture=await prompt(id),records=trace(traceFile);
    const add=(name,requests,observed=records)=>observations.push({scenario_id:name,outcome:'accepted',native:signature(),requests,
      profiles:profiles.filter(x=>x.scenario_id===name).map(x=>observedProfile(observed,x,capture))});
    add('native_config_load',[]);add('new_session',[requestEvidence(capture,records,'thread/start',descriptor)]);

    // Store a genuinely different base in native history, then restart and
    // resume it with the candidate configuration and verify the new request.
    const oldBase=path.join(scratch,'old-base.md');write(oldBase,'Synthetic legacy coding-assistant base used only by the compatibility fixture.\n');
    p=current(profiles.find(x=>x.scenario_id==='old_base_resume'));await launch(p,{model_instructions_file:oldBase});id=await sessionNew();await prompt(id);
    await launch(p);await sessionLoad(id);capture=await prompt(id);records=trace(traceFile);add('old_base_resume',[requestEvidence(capture,records,'thread/resume',descriptor)]);

    // A maintenance candidate is created by a separate native app-server,
    // closed, and then loaded through the actual owned ACP entry.
    p=current(profiles.find(x=>x.scenario_id==='maintenance_promotion_resume'));await client.close();client=null;write(traceFile,'');
    const direct=new Rpc(proxy,['app-server','--listen','stdio://'],env,cwd);
    try{
      await direct.request('initialize',{clientInfo:{name:'kin_maintenance_compatibility',version:'2'},capabilities:{experimentalApi:true}});direct.send({method:'initialized'});
      const candidate=await direct.request('thread/start',{cwd,model:p.model,modelProvider:p.provider,config:config(p),developerInstructions:fs.readFileSync(descriptor.developer.realpath,'utf8'),approvalPolicy:'never',sandbox:'read-only'});
      requireFact(candidate.thread?.id,'Maintenance native candidate missing');id=candidate.thread.id;
      const turn=await direct.request('turn/start',{threadId:id,input:[{type:'text',text:'Isolated maintenance candidate persistence fixture. Return KIN_COMPAT_OK.',text_elements:[]}]});
      const until=Date.now()+RPC_TIMEOUT;while(Date.now()<until&&!direct.events.some(e=>e.method==='turn/completed'&&e.params?.turn?.id===turn.turn.id))await delay(25);
      requireFact(direct.events.some(e=>e.method==='turn/completed'&&e.params?.turn?.id===turn.turn.id&&e.params.turn.status==='completed'),'Maintenance candidate did not persist a completed turn');
    }finally{await direct.close();}
    await launch(p);await sessionLoad(id);capture=await prompt(id);records=trace(traceFile);add('maintenance_promotion_resume',[requestEvidence(capture,records,'thread/resume',descriptor)]);
    add('post_promotion_request',[requestEvidence(capture,records,'turn/start',descriptor)]);

    // Compact the isolated thread, then prove the same identity survives an
    // ACP process restart and the next provider request retains both layers.
    const compact=await client.request('_kin/compact',{sessionId:id,operationId:'compat-'+randomUUID()});requireFact(compact.completed===true,'Native isolated compaction did not complete');
    p=current(profiles.find(x=>x.scenario_id==='isolated_compaction_resume'));await launch(p);await sessionLoad(id);capture=await prompt(id);records=trace(traceFile);
    add('isolated_compaction_resume',[requestEvidence(capture,records,'thread/resume',descriptor),requestEvidence(capture,records,'turn/start',descriptor)]);
    add('process_restart',[requestEvidence(capture,records,'thread/resume',descriptor)]);

    // Model changes use a persistent native identity and real request receipts.
    for(const name of ['ds_sol_ds','manual_astra_work_restore']){
      const entries=profiles.filter(x=>x.scenario_id===name);if(!entries.length)continue;const requests=[],observed=[];
      for(const expected of entries){p=current(expected);await launch(p);await sessionLoad(id);
        for(const [configId,value] of [['model',p.model],['reasoning_effort',p.reasoning_effort],['fast-mode',p.fast_mode]])
          await client.request('session/set_config_option',{sessionId:id,configId,value});
        capture=await prompt(id);records=trace(traceFile);requests.push(requestEvidence(capture,records,'thread/resume',descriptor));observed.push(observedProfile(records,expected,capture));
      }
      observations.push({scenario_id:name,outcome:'accepted',native:signature(),requests,profiles:observed});
    }
    await client.close();client=null;
    return {schema_version:'kin.mobile-runtime.proof/v1',candidate_id:descriptor.candidate_id,bundle_manifest_sha256:sha(manifestBytes),descriptor_sha256:sha(descriptorBytes),runner_input_sha256:sha(inputBytes),
      producer:{runner_sha256:sha(bytes(fileURLToPath(import.meta.url))),node:process.version,codex_sha256:sha(bytes(codex)),codex_version:codexVersion,acp_entry_sha256:sha(bytes(acp)),acp_version:acpVersion,
        actual_exec_receipt_sha256:sha(JSON.stringify(observations)),basis:'owned-runner-real-acp-native-rpc-and-loopback-provider'},
      isolation:{private_temporary_home:true,provider:'loopback-fixture',real_provider_calls:0,external_credentials_supplied:false,os_level_egress_monitor:false},observations};
  }finally{if(client)await client.close();await server.close();fs.rmSync(scratch,{recursive:true,force:true});}
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const arg=name=>{const i=process.argv.indexOf(name);if(i<0||!process.argv[i+1])throw Error('Missing '+name);return process.argv[i+1];};
  try{const proof=await produceMobileRuntimeProof({bundleDir:arg('--bundle'),descriptorPath:arg('--descriptor'),inputPath:arg('--input')});process.stdout.write(JSON.stringify(proof)+'\n');}
  catch(error){process.stderr.write(String(error?.stack??error)+'\n');process.exitCode=1;}
}
