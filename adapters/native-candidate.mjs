import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import fs from 'node:fs';
import {checkpointMarker} from './native-window.mjs';
import {conversationClock} from './conversation-time.mjs';
import {isCompanionInstructionBinding,sameInstructionBinding} from './instruction-evidence.mjs';

const requestedServiceTier=launch=>launch.fastMode==='on'?'fast':null;
const evidence=(actual,key,expected)=>!Object.hasOwn(actual??{},key)?'unknown':actual[key]===expected?'verified':'mismatch';

/** App-server start/resume receipts are the authority for the profile that the
 * isolated thread actually accepted. `serviceTier` is a thread configuration
 * receipt; it does not claim that any particular request received priority. */
export function candidateResponseProfileEvidence(actual,launch) {
  return {
    model:evidence(actual,'model',launch.profile.model),
    modelProvider:evidence(actual,'modelProvider',launch.modelProvider),
    reasoningEffort:evidence(actual,'reasoningEffort',launch.profile.reasoningEffort),
    serviceTierConfiguration:evidence(actual,'serviceTier',requestedServiceTier(launch)),
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

/** A mobile-only, read-only app-server instance. It never owns a channel or an
 * MCP credential. Native API responses are the authority for its thread IDs. */
export class NativeCandidate {
  constructor({command,args=[],cwd,env={},configForModel,personaInstructions,timeoutMs=180000}) {
    Object.assign(this,{command,args,cwd,env,configForModel,personaInstructions,timeoutMs});this.pending=new Map();this.events=[];this.serial=0;this.loaded=new Set();
  }
  async start(){
    if(this.child)return;
    const child=spawn(this.command,[...this.args,'app-server','--stdio'],{cwd:this.cwd,env:{...process.env,...this.env},stdio:['pipe','pipe','pipe']});this.child=child;
    child.stderr.on('data',()=>{});
    createInterface({input:child.stdout}).on('line',line=>{let message;try{message=JSON.parse(line);}catch{return;}
      if(this.pending.has(message.id)){const p=this.pending.get(message.id);this.pending.delete(message.id);message.error?p.reject(Error(message.error.message)):p.resolve(message.result);}
      else if(message.method){
        // Only public final output and terminal lifecycle are retained.
        if(message.method==='turn/completed'||message.method==='item/completed'&&message.params?.item?.type==='agentMessage')this.events.push(message);
        if(message.id!==undefined)child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:message.id,error:{code:-32601,message:'Candidate verification has no tool or approval access'}})+'\n');
      }
    });
    child.on('error',()=>this.rejectPending());child.on('exit',()=>{this.rejectPending();if(this.child===child){this.child=null;this.loaded.clear();}});
    await this.request('initialize',{clientInfo:{name:'kin_continuity',version:'1'},capabilities:{experimentalApi:true}});
    child.stdin.write(JSON.stringify({jsonrpc:'2.0',method:'initialized'})+'\n');
  }
  rejectPending(){for(const p of this.pending.values())p.reject(Error('Native candidate connection closed'));this.pending.clear();}
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
  requestParams(launch){return {cwd:this.cwd,model:launch.profile.model,modelProvider:launch.modelProvider,config:{...launch.config,'mcp_servers':{},'features.apps':false,'features.hooks':false,'features.multi_agent':false,model_reasoning_effort:launch.reasoningEffort},sandbox:'read-only',approvalPolicy:'never',developerInstructions:launch.developerInstructions??this.personaInstructions,serviceTier:requestedServiceTier(launch)};}
  params(profile){return this.requestParams(this.launch(profile));}
  async create({id,model,profile}){
    const launch=this.launch(profile??{model});await this.start();const value=await this.request('thread/start',this.requestParams(launch));
    const receipt=responseReceipt(value,launch);
    if(!value?.thread?.id||!value.thread.sessionId||!profileEvidenceVerified(receipt.profileEvidence))throw Error('Native candidate started with another profile');
    const native={threadId:value.thread.id,nativeSessionId:value.thread.sessionId,path:value.thread.path,model:launch.profile.model,reasoningEffort:launch.profile.reasoningEffort,
      profile:structuredClone(launch.profile),providerBinding:structuredClone(launch.providerBinding),instructionBinding:structuredClone(launch.instructionBinding??null),creationReceipt:{id,at:new Date().toISOString(),...receipt}};
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
