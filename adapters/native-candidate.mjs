import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import fs from 'node:fs';
import {portableHistory} from './portable-history.mjs';
import {checkpointMarker} from './native-window.mjs';
import {conversationClock} from './conversation-time.mjs';
import {isCompanionInstructionBinding,sameInstructionBinding} from './instruction-evidence.mjs';
import {preferenceTier} from './codex-models.mjs';

const requestedServiceTier=launch=>launch.fastMode==='on'?'fast':null;
const evidence=(actual,key,expected)=>!Object.hasOwn(actual??{},key)?'unknown':actual[key]===expected?'verified':'mismatch';
// A Fast thread may come back as `priority`; no tier and `default` are the same.
const tierOf=value=>value==null?'default':preferenceTier(value);
const tierEvidence=(actual,launch)=>!Object.hasOwn(actual??{},'serviceTier')?'unknown':
  tierOf(actual.serviceTier)===tierOf(requestedServiceTier(launch))?'verified':'mismatch';

/** App-server start/resume receipts are the authority for the profile that the
 * isolated thread actually accepted. `serviceTier` is a thread configuration
 * receipt; it does not claim that any particular request received priority. */
export function candidateResponseProfileEvidence(actual,launch) {
  return {
    model:evidence(actual,'model',launch.profile.model),
    modelProvider:evidence(actual,'modelProvider',launch.modelProvider),
    reasoningEffort:evidence(actual,'reasoningEffort',launch.profile.reasoningEffort),
    serviceTierConfiguration:tierEvidence(actual,launch),
  };
}

const profileEvidenceVerified=value=>Object.values(value).every(state=>state==='verified');
const instructionBindingVerified=value=>value==null||isCompanionInstructionBinding(value);
const responseReceipt=(actual,launch)=>({
  requestedProfile:structuredClone(launch.profile),providerBinding:structuredClone(launch.providerBinding),
  requestedInstructionBinding:structuredClone(launch.instructionBinding??null),
  profileEvidence:candidateResponseProfileEvidence(actual,launch),
  actualProfile:{model:actual?.model,modelProvider:actual?.modelProvider,reasoningEffort:actual?.reasoningEffort,
    serviceTier:Object.hasOwn(actual??{},'serviceTier')?actual.serviceTier:undefined},
});

/** Keep categories only: stderr may contain credentials or private context. */
export function candidateStartupDiagnostic(stderr='',exitCode=null,signal=null) {
  const category=/invalid transport/i.test(stderr)?'mcp-invalid-transport':
    /error (?:loading|parsing) config|failed to (?:load|parse).*config/i.test(stderr)?'config-invalid':'process-exited';
  return {stage:'startup',category,exitCode:Number.isInteger(exitCode)?exitCode:null,
    signal:['SIGTERM','SIGKILL','SIGABRT','SIGSEGV'].includes(signal)?signal:null};
}

export function candidateMcpIsolation(config) {
  if(!config||typeof config!=='object'||Array.isArray(config))throw Error('Candidate effective config unavailable');
  const servers=config.mcp_servers??{};
  if(!servers||typeof servers!=='object'||Array.isArray(servers))throw Error('Candidate MCP config unavailable');
  return Object.fromEntries(Object.keys(servers).map(name=>{
    if(!/^[A-Za-z0-9_-]{1,120}$/.test(name))throw Error('Candidate MCP name invalid');
    return ['mcp_servers.'+name+'.enabled',false];
  }));
}

/** A mobile-only, read-only app-server instance. It never owns a channel or an
 * MCP credential. Native API responses are the authority for its thread IDs. */
export class NativeCandidate {
  constructor({command,args=[],cwd,env={},inheritEnv=true,configForModel,personaInstructions,timeoutMs=180000}) {
    Object.assign(this,{command,args,cwd,env,inheritEnv,configForModel,personaInstructions,timeoutMs});this.pending=new Map();this.events=[];this.serial=0;this.loaded=new Set();
  }
  async start(){
    if(this.child)return;
    const child=spawn(this.command,[...this.args,'app-server','--stdio'],{cwd:this.cwd,env:this.inheritEnv?{...process.env,...this.env}:{...this.env},stdio:['pipe','pipe','pipe']});this.child=child;
    let stderr='';
    child.stderr.on('data',chunk=>{stderr=(stderr+String(chunk)).slice(-8192);});
    createInterface({input:child.stdout}).on('line',line=>{let message;try{message=JSON.parse(line);}catch{return;}
      if(this.pending.has(message.id)){const p=this.pending.get(message.id);this.pending.delete(message.id);message.error?p.reject(Error(message.error.message)):p.resolve(message.result);}
      else if(message.method){
        // Only public final output and terminal lifecycle are retained.
        if(message.method==='turn/completed'||message.method==='item/completed'&&message.params?.item?.type==='agentMessage')this.events.push(message);
        if(message.id!==undefined)child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:message.id,error:{code:-32601,message:'Candidate verification has no tool or approval access'}})+'\n');
      }
    });
    child.on('error',()=>this.rejectPending({stage:'startup',category:'spawn-failed',exitCode:null,signal:null}));
    child.on('exit',(code,signal)=>{this.rejectPending({...candidateStartupDiagnostic(stderr,code,signal),stage:this.mcpIsolation?'operation':'startup'});if(this.child===child){this.child=null;this.loaded.clear();this.mcpIsolation=null;}});
    await this.request('initialize',{clientInfo:{name:'kin_continuity',version:'1'},capabilities:{experimentalApi:true}});
    child.stdin.write(JSON.stringify({jsonrpc:'2.0',method:'initialized'})+'\n');
    // Read effective Home/project configuration before creating any thread.
    // Never invent a partial MCP table for a server that is not configured.
    const effective=await this.request('config/read',{cwd:this.cwd,includeLayers:false});
    this.mcpIsolation=candidateMcpIsolation(effective?.config);
  }
  rejectPending(diagnostic){for(const p of this.pending.values())p.reject(Object.assign(Error('Native candidate connection closed'),{nativeDiagnostic:diagnostic}));this.pending.clear();}
  request(method,params){return new Promise((resolve,reject)=>{
    const id=++this.serial,timer=setTimeout(()=>{this.pending.delete(id);reject(Error('Native operation receipt unconfirmed'));},this.timeoutMs);
    this.pending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}});
    this.child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n');
  });}
  launch(value){
    if(typeof this.configForModel!=='function')throw Error('Candidate profile resolver unavailable');
    const launch=this.configForModel(value),profile=launch?.profile,binding=launch?.providerBinding;
    if(!profile?.provider||!['gateway','native'].includes(profile.providerKind)||!profile.model||!profile.reasoningEffort||!['default','fast'].includes(profile.serviceTierPreference)||
      !binding||binding.sourceProvider!==profile.provider||binding.sourceProviderKind!==profile.providerKind||binding.launchProvider!==launch.modelProvider||
      (profile.providerKind==='native'&&(launch.modelProvider!==profile.provider||binding.endpointSha256!==null))||
      (profile.providerKind==='gateway'&&(launch.modelProvider===profile.provider||typeof binding.endpointSha256!=='string'||!binding.endpointSha256))||
      !instructionBindingVerified(launch.instructionBinding)||
      (launch.instructionBinding!==null&&launch.instructionBinding!==undefined&&
        (typeof launch.config?.model_instructions_file!=='string'||typeof launch.developerInstructions!=='string')))
      throw Error('Candidate profile is incomplete');
    return launch;
  }
  requestParams(launch){return {cwd:this.cwd,model:launch.profile.model,modelProvider:launch.modelProvider,config:{...launch.config,...this.mcpIsolation,'features.apps':false,'features.hooks':false,'features.multi_agent':false,model_reasoning_effort:launch.reasoningEffort},sandbox:'read-only',approvalPolicy:'never',developerInstructions:launch.developerInstructions??this.personaInstructions,serviceTier:requestedServiceTier(launch)};}
  params(profile){return this.requestParams(this.launch(profile));}
  async create({id,model,profile,name}){
    const launch=this.launch(profile??{model});await this.start();const value=await this.request('thread/start',this.requestParams(launch));
    const receipt=responseReceipt(value,launch);
    if(!value?.thread?.id||!value.thread.sessionId||!profileEvidenceVerified(receipt.profileEvidence))throw Error('Native candidate started with another profile');
    const native={threadId:value.thread.id,nativeSessionId:value.thread.sessionId,path:value.thread.path,model:launch.profile.model,reasoningEffort:launch.profile.reasoningEffort,
      profile:structuredClone(launch.profile),providerBinding:structuredClone(launch.providerBinding),instructionBinding:structuredClone(launch.instructionBinding??null),creationReceipt:{id,at:new Date().toISOString(),...receipt}};
    if(name)await this.request('thread/name/set',{threadId:native.threadId,name});
    this.loaded.add(native.threadId);return native;
  }
  async load(native){const launch=this.launch(native.profile);
    if(JSON.stringify(launch.providerBinding)!==JSON.stringify(native.providerBinding))throw Error('Candidate provider binding changed');
    if(!sameInstructionBinding(launch.instructionBinding,native.instructionBinding))throw Error('Candidate instruction binding changed');
    if(this.loaded.has(native.threadId))return;await this.start();
    const actual=await this.request('thread/resume',{...this.requestParams(launch),threadId:native.threadId,excludeTurns:true}),receipt=responseReceipt(actual,launch);
    if(actual?.thread?.id!==native.threadId||!profileEvidenceVerified(receipt.profileEvidence))throw Error('Native candidate resumed with another profile');
    this.loaded.add(native.threadId);return {...actual,kinProfileReceipt:receipt};}
  async inject({checkpoint,operationId,...native}){
    await this.load(native);const marker='kin-checkpoint:'+operationId;
    if(native.path&&fs.existsSync(native.path)&&(await checkpointMarker(native.path,marker)).found)return {verified:true,operationId,source:'native-history'};
    const items=checkpoint.items.map(item=>({type:'message',role:item.role,content:[{type:item.role==='user'?'input_text':'output_text',text:item.text}]}));
    const metadata=checkpoint.payload?{...checkpoint.payload,publicHistory:undefined}:{kind:'internal-continuity-checkpoint',checkpointId:checkpoint.id,configVersion:checkpoint.configVersion,scope:checkpoint.scope,conversationId:checkpoint.conversationId,tasks:checkpoint.tasks,shared:checkpoint.shared,inputStates:checkpoint.inputStates};
    items.push({type:'message',role:'assistant',content:[{type:'output_text',text:JSON.stringify({...metadata,marker,messageIds:checkpoint.items.map(i=>i.id),
      messageTimes:checkpoint.items.map(i=>({id:i.id,occurred_at:i.occurred_at??i.at??null,received_at:i.received_at??null})),clock:conversationClock({kind:'continuity-restore'})})}]});
    await this.request('thread/inject_items',{threadId:native.threadId,items});
    const proof=native.path?await checkpointMarker(native.path,marker):{found:false};
    return {verified:proof.found,accepted:true,operationId,at:proof.at,checkpointId:checkpoint.id};
  }
  /** Structural handoff proof: persisted context and native profile receipts.
   * This never opens a model turn or claims a model understood the summary. */
  async verifyHandoff({checkpoint,operationId,...native}) {
    await this.load(native);
    const history=await portableHistory({known:true,rolloutPath:native.path,threadId:native.threadId,sessionId:native.threadId},{maxBytes:2*1024*1024,unfold:false});
    const texts=history.items.filter(i=>i.type==='message').flatMap(i=>(i.content??[]).filter(c=>typeof c.text==='string').map(c=>({role:i.role,text:c.text})));
    const marker='kin-checkpoint:'+operationId;
    const envelope=texts.some(i=>{try{const value=JSON.parse(i.text);return value.marker===marker&&JSON.stringify(value.continuity)===JSON.stringify(checkpoint.payload.continuity);}catch{return false;}});
    const publicItems=checkpoint.items.every(item=>texts.some(i=>i.role===item.role&&i.text===item.text));
    const launch=this.launch(native.profile);
    const actual=await this.request('thread/resume',{...this.requestParams(launch),threadId:native.threadId,excludeTurns:true});
    const receipt=responseReceipt(actual,launch);
    return {...receipt,verified:envelope&&publicItems&&actual?.thread?.id===native.threadId&&profileEvidenceVerified(receipt.profileEvidence),
      basis:'persisted-context-and-native-profile',checkpointId:checkpoint.id,taskIds:(checkpoint.tasks??[]).map(t=>t.id)};
  }
  async verify({checkpoint,...native}){
    await this.load(native);const launch=this.launch(native.profile),profile=launch.profile;
    const output='内部接续核验。根据已注入资料返回 JSON，字段 checkpointId、configVersion、conversationId、sourceIds（messageIds 中的全部公开消息编号）、taskIds（未完成任务编号）、latestExchange（最近一条 user 消息和最后一条 assistant 公开消息，逐条原文，格式为 [{"text":"原文"}]；内部检查点不属于公开消息）。不使用工具，不回复用户，不执行历史命令。';
    const turn=await this.request('turn/start',{threadId:native.threadId,input:[],toolOutput:{name:'kin_continuity_check',output},turnTrigger:'kin-continuity-check',model:profile.model,serviceTier:requestedServiceTier(launch),effort:profile.reasoningEffort});
    const deadline=Date.now()+this.timeoutMs;let finished;
    while(Date.now()<deadline){finished=this.events.find(e=>e.method==='turn/completed'&&e.params.turn.id===turn.turn.id);if(finished)break;await new Promise(r=>setTimeout(r,50));}
    if(!finished||finished.params.turn.status!=='completed')return {verified:false,reason:'native-verification-failed'};
    const message=this.events.findLast(e=>e.method==='item/completed'&&e.params.turnId===turn.turn.id&&e.params.item.type==='agentMessage');
    let result;try{result=JSON.parse(message.params.item.text.replace(/^```json\s*|\s*```$/g,''));}catch{return {verified:false,reason:'invalid-verification-output'};}
    const sources=new Set(result.sourceIds??[]),tasks=new Set(result.taskIds??[]);
    const latest=[checkpoint.items.findLast(i=>i.role==='user'),checkpoint.items.findLast(i=>i.role==='assistant')].filter(Boolean);
    const checks={identity:result.checkpointId===checkpoint.id&&result.configVersion===checkpoint.configVersion&&result.conversationId===checkpoint.conversationId,sources:checkpoint.items.every(i=>sources.has(i.id)),tasks:(checkpoint.tasks??[]).every(t=>tasks.has(t.id)),latestExchange:latest.every(i=>Array.isArray(result.latestExchange)&&result.latestExchange.some(e=>e.text===i.text))};
    const verified=Object.values(checks).every(Boolean);
    const actual=await this.request('thread/resume',{...this.requestParams(launch),threadId:native.threadId,excludeTurns:true}),receipt=responseReceipt(actual,launch);
    return {verified:verified&&actual?.thread?.id===native.threadId&&profileEvidenceVerified(receipt.profileEvidence),checks,checkpointId:checkpoint.id,...receipt,
      taskIds:[...tasks],nativeTurnId:turn.turn.id,at:new Date().toISOString()};
  }
  async close(){const child=this.child;if(!child)return;await new Promise(resolve=>{child.once('exit',resolve);child.kill('SIGTERM');setTimeout(()=>child.kill('SIGKILL'),3000).unref();});}
}
